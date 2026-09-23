"""Offline acquisition tests: python -m unittest discover -s tests."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from openpyxl import Workbook


MODULE_PATH = Path(__file__).resolve().parents[1] / "code" / "00_fetch_data.py"
SPEC = importlib.util.spec_from_file_location("fetch_data", MODULE_PATH)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class DownloadTests(unittest.TestCase):
    fred = b"observation_date,UNRATE\n2020-01-01,3.6\n2020-02-01,.\n2020-03-01,4.4\n"

    def workbook(self, sheet="GSCPI Monthly Data", rows=None):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = sheet
        worksheet.append(["Date", "GSCPI", None, "Copyright"])
        for row in rows or [
            [None, None, None, "New York Fed Economic Research"],
            ["29-Feb-2020", "1.25", None, None],
            ["31-Jan-2020", -0.5, None, None],
            ["Footer", "not an observation", None, "terms"],
            [None, None, None, None],
        ]:
            worksheet.append(row)
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()

    def test_fred_missing_observations_are_preserved(self):
        self.assertEqual(fetch.validate_csv(self.fred, "UNRATE"), {
            "rows": 3, "numeric_observations": 2,
            "first_date": "2020-01-01", "last_date": "2020-03-01",
        })

    def test_invalid_source_responses_rejected(self):
        invalid = [
            b"<html>Service unavailable</html>",
            b"observation_date,UNRATE\n2020-01-01,.\n",
            b"observation_date,UNRATE\n2020-01-01,inf\n",
            b"observation_date,UNRATE\n2020-01-01,1\n2020-01-01,2\n",
            b"observation_date,UNRATE\nnot-a-date,1\n",
            b"observation_date,OTHER\n2020-01-01,1\n",
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                fetch.validate_csv(payload, "UNRATE")

    def test_gscpi_filters_blank_and_copyright_rows_and_sorts_dates(self):
        for sheet in ("GSCPI Monthly Data", "Monthly Data"):
            with self.subTest(sheet=sheet):
                result = fetch.parse_gscpi_workbook(self.workbook(sheet=sheet))
                self.assertEqual(result.decode(), "date,gscpi\n2020-01-31,-0.5\n2020-02-29,1.25\n")

    def test_gscpi_rejects_duplicates_and_missing_sheet(self):
        with self.assertRaises(ValueError):
            fetch.parse_gscpi_workbook(self.workbook(sheet="Unrelated"))
        with self.assertRaises(ValueError):
            fetch.parse_gscpi_workbook(self.workbook(rows=[
                ["31-Jan-2020", 1], ["31-Jan-2020", 2],
            ]))

    def test_existing_valid_input_reused_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            (raw / "UNRATE.csv").write_bytes(self.fred)
            with patch.object(fetch, "download", side_effect=AssertionError("network used")):
                fetch.acquire("UNRATE", raw)
                first_metadata = (raw / "UNRATE.provenance.json").read_bytes()
                fetch.acquire("UNRATE", raw)
                self.assertEqual(first_metadata, (raw / "UNRATE.provenance.json").read_bytes())
            metadata = json.loads(first_metadata)
            self.assertIsNone(metadata["retrieved_at_utc"])
            self.assertEqual(metadata["sha256"], hashlib.sha256(self.fred).hexdigest())

    def test_force_download_records_provenance_and_rejects_invalid_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            path = raw / "UNRATE.csv"
            path.write_bytes(self.fred)
            replacement = b"observation_date,UNRATE\n2020-01-01,3.7\n"
            with patch.object(fetch, "download", return_value=replacement) as download:
                fetch.acquire("UNRATE", raw, force=True)
                download.assert_called_once()
            metadata = json.loads((raw / "UNRATE.provenance.json").read_text())
            self.assertEqual(path.read_bytes(), replacement)
            self.assertIsNotNone(metadata["retrieved_at_utc"])
            self.assertEqual(metadata["source_sha256"], hashlib.sha256(replacement).hexdigest())
            provenance = (raw / "UNRATE.provenance.json").read_bytes()
            with patch.object(fetch, "download", return_value=b"<html>Error</html>"):
                with self.assertRaises(ValueError):
                    fetch.acquire("UNRATE", raw, force=True)
            self.assertEqual(path.read_bytes(), replacement)
            self.assertEqual(provenance, (raw / "UNRATE.provenance.json").read_bytes())

    def test_invalid_existing_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            path = raw / "UNRATE.csv"
            path.write_bytes(b"Service unavailable")
            with patch.object(fetch, "download", return_value=self.fred):
                fetch.acquire("UNRATE", raw)
            self.assertEqual(path.read_bytes(), self.fred)

    def test_network_retries_are_bounded(self):
        with patch.object(fetch, "urlopen", side_effect=URLError("offline")) as request:
            with patch.object(fetch.time, "sleep") as sleep:
                with self.assertRaises(URLError):
                    fetch.download(fetch.source_url("UNRATE"), timeout=7, attempts=3)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == 7 for call in request.call_args_list))


if __name__ == "__main__":
    unittest.main()
