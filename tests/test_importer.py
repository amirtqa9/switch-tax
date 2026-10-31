import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from switch_tax.cli import main
from switch_tax.core import TraceError, analyze, validate
from switch_tax.importer import import_trace


class ImporterTests(unittest.TestCase):
    def fixture(self):
        common = {"task": "private-task", "prompt": "secret prompt", "response": "secret output"}
        return [
            {**common, "type": "tool_call", "id": "old-check", "session": "private-old", "seq": 1,
             "action": "check", "command": "secret test command", "revision": "private-revision",
             "check_passed": True, "start": "2026-01-01T00:00:01Z", "end": "2026-01-01T00:00:02Z"},
            {**common, "type": "handoff", "id": "handoff", "session": "private-old", "seq": 2,
             "start": "2026-01-01T00:00:03Z", "end": "2026-01-01T00:00:05Z"},
            {**common, "type": "switch", "id": "boundary", "session": "private-old", "seq": 3,
             "switch_id": "private-switch", "from": "private-old", "to": "private-new",
             "at": "2026-01-01T00:00:10Z", "handoff_ids": ["handoff"]},
            {**common, "type": "context", "id": "read", "session": "private-new", "seq": 1,
             "start": "2026-01-01T00:00:11Z", "end": "2026-01-01T00:00:13Z"},
            {**common, "type": "tool_call", "id": "new-check", "session": "private-new", "seq": 2,
             "action": "check", "command": "secret test command", "revision": "private-revision",
             "start": "2026-01-01T00:00:14Z", "end": "2026-01-01T00:00:16Z"},
            {**common, "type": "progress", "id": "progress", "session": "private-new", "seq": 3,
             "verified": True, "start": "2026-01-01T00:00:18Z", "end": "2026-01-01T00:00:19Z"},
        ]

    def test_import_redacts_and_preserves_analysis(self):
        events = import_trace(self.fixture())
        validate(events)
        report = analyze(events)
        self.assertEqual(report["aggregates"]["complete"], 1)
        self.assertEqual(report["switches"][0]["categories"]["validation"]["tool_calls"], 1)
        serialized = json.dumps(events)
        for secret in ("secret prompt", "secret output", "secret test command", "private-task",
                       "private-old", "private-new", "private-switch", "private-revision"):
            self.assertNotIn(secret, serialized)

    def test_cli_import_and_invalid_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "raw.jsonl", Path(tmp) / "redacted.jsonl"
            rows = self.fixture()
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["import", str(source), "--out", str(output)]), 0)
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), len(rows))
            rows[2]["handoff_ids"] = ["missing"]
            with self.assertRaises(TraceError):
                import_trace(rows)

    def test_malformed_types_are_clear_errors(self):
        cases = [(0, "type", []), (0, "action", []), (0, "check_passed", "yes"),
                 (3, "category", {}), (5, "verified", "yes")]
        for index, field, value in cases:
            with self.subTest(field=field):
                rows = self.fixture()
                rows[index][field] = value
                if field == "action":
                    # Unrecognized actions are deliberately omitted from the normalized trace.
                    self.assertNotIn("action", import_trace(rows)[0])
                else:
                    with self.assertRaises(TraceError):
                        import_trace(rows)


if __name__ == "__main__":
    unittest.main()
