import unittest

from switch_tax.core import TraceError, analyze, validate


def event(eid, session, seq, kind, start=None, end=None, **other):
    result = {"schema_version": 1, "event_id": eid, "task_id": "task", "session_id": session,
              "seq": seq, "kind": kind, **other}
    if start is not None:
        result["timestamp_start"] = f"2026-01-01T00:00:{start:02d}Z"
    if end is not None:
        result["timestamp_end"] = f"2026-01-01T00:00:{end:02d}Z"
    return result


def switch(**other):
    return event("switch", "old", 3, "switch", switch_id="s1", from_session="old",
                 to_session="new", at="2026-01-01T00:00:10Z", handoff_event_ids=["handoff"], **other)


class SwitchTaxTests(unittest.TestCase):
    def fixture(self):
        return [event("check-old", "old", 1, "tool_call", 1, 2, action="check",
                      command_fingerprint="pytest", source_revision="abc", check_passed=True),
                event("handoff", "old", 2, "handoff", 3, 5), switch(),
                event("read", "new", 1, "context_load", 11, 13),
                event("setup", "new", 2, "tool_call", 12, 14, action="environment_setup"),
                event("check-new", "new", 3, "tool_call", 15, 17, action="check",
                      command_fingerprint="pytest", source_revision="abc"),
                event("progress", "new", 4, "progress", 18, 19, verified=True)]

    def test_complete_switch_and_overlap(self):
        report = analyze(self.fixture())
        result = report["switches"][0]
        self.assertEqual(result["first_progress_event_id"], "progress")
        self.assertEqual(result["metrics"]["observed_wall_ms"], 8000)
        self.assertEqual(result["metrics"]["active_event_ms"], 8000)
        self.assertEqual(result["metrics"]["tool_calls"], 2)
        self.assertEqual(result["categories"]["validation"]["event_ids"], ["check-new"])
        self.assertEqual(result["categories"]["orientation"]["event_ids"], ["read"])

    def test_changed_revision_is_productive(self):
        rows = self.fixture()
        rows[-2]["source_revision"] = "def"
        result = analyze(rows)["switches"][0]
        self.assertEqual(result["categories"]["productive"]["event_ids"], ["check-new", "progress"])

    def test_missing_data_is_unknown(self):
        rows = self.fixture()
        del rows[3]["timestamp_end"]
        metrics = analyze(rows)["switches"][0]["metrics"]
        self.assertIsNone(metrics["active_event_ms"])
        self.assertIsNone(metrics["input_tokens"])

    def test_wait_and_override(self):
        rows = self.fixture()
        rows.insert(-1, event("wait", "new", 4, "wait", 17, 18))
        rows[-1]["seq"] = 5
        result = analyze(rows, {"setup": {"category": "unknown", "reason": "unclear"}})["switches"][0]
        self.assertEqual(result["metrics"]["external_wait_ms"], 1000)
        self.assertEqual(result["metrics"]["net_observed_ms"], 7000)
        self.assertIn("setup", result["categories"]["unknown"]["event_ids"])

    def test_open_window(self):
        rows = self.fixture()[:-1]
        report = analyze(rows)
        self.assertEqual(report["switches"][0]["status"], "incomplete")
        self.assertIsNone(report["switches"][0]["metrics"]["observed_wall_ms"])

    def test_two_switches_claim_distinct_events(self):
        rows = self.fixture()
        rows += [event("handoff-2", "new", 5, "handoff", 20, 22),
                 event("switch-2", "new", 6, "switch", switch_id="s2", from_session="new",
                       to_session="third", at="2026-01-01T00:00:25Z",
                       handoff_event_ids=["handoff-2"]),
                 event("read-2", "third", 1, "context_load", 26, 27),
                 event("progress-2", "third", 2, "progress", 28, 29, verified=True)]
        report = analyze(rows)
        self.assertEqual(report["aggregates"]["complete"], 2)
        ids = [[item["event_id"] for item in result["classifications"]] for result in report["switches"]]
        self.assertEqual(ids[0], ["handoff", "read", "setup", "check-new", "progress"])
        self.assertEqual(ids[1], ["handoff-2", "read-2", "progress-2"])
        self.assertEqual(len(set(ids[0] + ids[1])), len(ids[0] + ids[1]))

    def test_clock_skew_keeps_boundary_evidence_but_marks_elapsed_unknown(self):
        rows = self.fixture()
        rows[-1]["timestamp_start"] = "2026-01-01T00:00:09Z"
        rows[-1]["timestamp_end"] = "2026-01-01T00:00:09Z"
        report = analyze(rows)
        result = report["switches"][0]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["first_progress_event_id"], "progress")
        self.assertIsNone(result["metrics"]["observed_wall_ms"])
        self.assertTrue(any("before boundary" in warning for warning in report["warnings"]))

    def test_category_usage_reconciles_with_switch_total(self):
        rows = self.fixture()
        for item in rows:
            if item["event_id"] not in {"check-old", "switch"}:
                item["usage"] = {"input_tokens": 2, "output_tokens": 1, "cost_usd": 0.01}
        result = analyze(rows)["switches"][0]
        metrics = result["metrics"]
        for key, expected in (("input_tokens", 10), ("output_tokens", 5), ("cost_usd", 0.05)):
            self.assertAlmostEqual(metrics[key], expected)
            self.assertAlmostEqual(sum(category[key] or 0 for category in result["categories"].values()), expected)

    def test_invalid_duplicate_and_negative_interval(self):
        rows = self.fixture()
        rows[-1]["event_id"] = "read"
        with self.assertRaises(TraceError):
            validate(rows)
        rows = self.fixture()
        rows[-1]["timestamp_end"] = "2026-01-01T00:00:01Z"
        with self.assertRaises(TraceError):
            validate(rows)

    def test_cross_task_links_and_switch_sessions_are_rejected(self):
        rows = self.fixture()
        foreign = event("foreign", "other-session", 1, "handoff", 1, 2)
        foreign["task_id"] = "other-task"
        rows.append(foreign)
        rows[3]["links"] = ["foreign"]
        with self.assertRaisesRegex(TraceError, "reference foreign belongs to another task"):
            validate(rows)
        rows[3].pop("links")
        rows[2]["to_session"] = "other-session"
        with self.assertRaisesRegex(TraceError, "to_session belongs to another task"):
            validate(rows)

    def test_session_cannot_change_task_and_handoff_must_be_handoff(self):
        rows = self.fixture()
        rows[3]["task_id"] = "other-task"
        with self.assertRaisesRegex(TraceError, "session_id belongs to another task"):
            validate(rows)
        rows = self.fixture()
        rows[2]["handoff_event_ids"] = ["check-old"]
        with self.assertRaisesRegex(TraceError, "not a handoff event"):
            validate(rows)

    def test_usage_and_reference_shapes_are_validated_before_analysis(self):
        invalid_usage = ["redacted", {"input_tokens": True}, {"output_tokens": 1.5},
                         {"cost_usd": -0.1}, {"cost_usd": float("nan")},
                         {"cost_usd": float("inf")}]
        for usage in invalid_usage:
            with self.subTest(usage=usage):
                rows = self.fixture()
                rows[3]["usage"] = usage
                with self.assertRaisesRegex(TraceError, "usage"):
                    validate(rows)
        for links in ("read", ["read", "read"], [1], None):
            with self.subTest(links=links):
                rows = self.fixture()
                rows[4]["links"] = links
                with self.assertRaisesRegex(TraceError, "links must be"):
                    validate(rows)
        rows = self.fixture()
        rows[3]["usage"] = {"input_tokens": 0, "output_tokens": None, "cost_usd": 0.0}
        validate(rows)

    def test_stable_output(self):
        rows = self.fixture()
        self.assertEqual(analyze(rows), analyze(rows))


if __name__ == "__main__":
    unittest.main()
