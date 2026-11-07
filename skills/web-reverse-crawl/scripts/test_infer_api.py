"""Small offline checks for the new capture-format adapter; standard library only."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from infer_api import body_text, convert, rea_records, unique_object


class CaptureChecks(unittest.TestCase):
    def test_body_integrity(self):
        data = b'{"items": [1]}'
        body = {"state": "captured", "encoding": "base64", "content": base64.b64encode(data).decode(),
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.assertEqual(body_text(body), data.decode())
        with self.assertRaisesRegex(ValueError, "digest"):
            body_text({**body, "sha256": "0" * 64})
        self.assertIsNone(body_text({"state": "unavailable"}))

    def test_reject_failure_and_misbound_content(self):
        with self.assertRaisesRegex(ValueError, "successful"):
            rea_records({"error": {"partial_observation": {}}})
        events = [{"sequence": 1, "kind": "request", "transaction_id": "one"},
                  {"sequence": 2, "kind": "network-content", "transaction_id": "two", "phase": "request",
                   "source_event_sequence": 1, "body": {"state": "not_exposed"}}]
        with self.assertRaisesRegex(ValueError, "transaction"):
            rea_records({"events": {"items": events}})

    def test_har_origin_and_missing_body(self):
        def entry(url):
            return {"request": {"method": "GET", "url": url, "headers": [{"name": "Authorization", "value": "secret"}]},
                    "response": {"status": 200, "content": {"mimeType": "application/json"}}, "_resourceType": "fetch"}
        document = {"log": {"version": "1.2", "entries": [entry("https://example.test/api/items"), entry("https://other.test/api/items")]}}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            info = convert(document, output, {"https://example.test"})
            self.assertEqual(info["selected_records"], 1)
            self.assertEqual(info["omissions"]["outside_origins"], 1)
            self.assertFalse(info["records"][0]["response_text_joined"])
            request = json.loads((output / "cdp/network/requests.jsonl").read_text())
            self.assertEqual(request["params"]["request"]["headers"], {})
            with self.assertRaises(FileExistsError):
                convert(document, output, {"https://example.test"})

    def test_duplicate_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            json.loads('{"status": 200, "status": 500}', object_pairs_hook=unique_object)

    def test_har_to_openapi_pipeline(self):
        body = json.dumps({"items": [{"id": "one", "title": "Sample"}], "total": 1})
        entry = {"request": {"method": "GET", "url": "https://example.test/api/items?page=1"},
                 "response": {"status": 200, "content": {"mimeType": "application/json", "text": body}},
                 "_resourceType": "fetch"}
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / "fixture.har"
            capture.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
            output = Path(tmp) / "run"
            subprocess.run([sys.executable, str(Path(__file__).with_name("infer_api.py")),
                            str(capture), "--out", str(output), "--origin", "https://example.test"],
                           check=True, capture_output=True, text=True, timeout=20)
            spec = json.loads((output / "api-spec/openapi.json").read_text())
            self.assertEqual(spec["openapi"], "3.1.0")
            operation = spec["paths"]["/api/items"]["get"]
            self.assertIn("200", operation["responses"])
            self.assertTrue(operation["responses"]["200"]["content"]["application/json"]["schema"])
            self.assertEqual(operation["x-sample-count"], 1)


if __name__ == "__main__":
    unittest.main()
