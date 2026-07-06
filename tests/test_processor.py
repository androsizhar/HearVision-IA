"""
Tests for core/processor.py -- specifically the recorder-artifact filter
(_filter_recorder_artifacts) and structural plan validation (validate_plan).

These don't call analyze_session()/complete_plan() directly since those
require a live ANTHROPIC_API_KEY and network access; instead they test the
pure, deterministic logic that runs on an already-generated plan.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used-by-these-tests")

from core.processor import _filter_recorder_artifacts, validate_plan, _parse_json


class TestFilterRecorderArtifacts(unittest.TestCase):

    def test_trims_trailing_step_referencing_the_app_itself(self):
        plan = {
            "steps": [
                {"number": 1, "action": "navigate", "intent": "Go to the target site", "value": "https://example.com"},
                {"number": 2, "action": "click", "intent": "Submit the form", "value": ""},
                {"number": 3, "action": "navigate", "intent": "Return to the app to stop recording", "value": "http://localhost:8000"},
            ]
        }
        _filter_recorder_artifacts(plan)
        self.assertEqual(len(plan["steps"]), 2)
        self.assertEqual(plan["steps"][-1]["number"], 2)

    def test_does_not_touch_real_steps(self):
        plan = {
            "steps": [
                {"number": 1, "action": "navigate", "intent": "Go to the target site", "value": "https://example.com"},
                {"number": 2, "action": "click", "intent": "Submit the form", "value": ""},
            ]
        }
        _filter_recorder_artifacts(plan)
        self.assertEqual(len(plan["steps"]), 2)

    def test_only_trims_from_the_tail_not_the_middle(self):
        # A step that happens to mention "localhost" in the MIDDLE of a
        # legitimate process should not be removed -- only trailing artifacts are.
        plan = {
            "steps": [
                {"number": 1, "action": "navigate", "intent": "Go to the target site", "value": "https://example.com"},
                {"number": 2, "action": "verify", "intent": "Confirm the environment says localhost is disabled", "value": ""},
                {"number": 3, "action": "click", "intent": "Submit the form", "value": ""},
            ]
        }
        _filter_recorder_artifacts(plan)
        self.assertEqual(len(plan["steps"]), 3)

    def test_stops_trimming_after_three_checks(self):
        # Safety limit: even if several trailing steps look artifact-like,
        # only up to 3 are ever trimmed, so a genuinely weird but real plan
        # doesn't get silently gutted.
        plan = {
            "steps": [
                {"number": i, "action": "navigate", "intent": "stop recording", "value": ""}
                for i in range(1, 6)
            ]
        }
        _filter_recorder_artifacts(plan)
        self.assertEqual(len(plan["steps"]), 2)

    def test_empty_plan_does_not_crash(self):
        plan = {}
        _filter_recorder_artifacts(plan)  # should not raise
        self.assertEqual(plan.get("steps", []), [])


class TestValidatePlan(unittest.TestCase):

    def _valid_plan(self):
        return {
            "source_platform": "Spreadsheet",
            "target_platform": "Portal",
            "goal": "Register entries",
            "steps": [
                {"number": 1, "action": "navigate", "intent": "Open the portal"},
                {"number": 2, "action": "type", "intent": "Fill the name field"},
            ],
            "field_mappings": [],
        }

    def test_well_formed_plan_has_no_errors(self):
        self.assertEqual(validate_plan(self._valid_plan()), [])

    def test_missing_required_key_is_reported(self):
        plan = self._valid_plan()
        del plan["goal"]
        errors = validate_plan(plan)
        self.assertTrue(any("goal" in e for e in errors))

    def test_empty_steps_is_reported(self):
        plan = self._valid_plan()
        plan["steps"] = []
        errors = validate_plan(plan)
        self.assertTrue(any("empty" in e.lower() for e in errors))

    def test_unknown_action_is_reported(self):
        plan = self._valid_plan()
        plan["steps"][0]["action"] = "hack_the_mainframe"
        errors = validate_plan(plan)
        self.assertTrue(any("unknown action" in e for e in errors))

    def test_duplicate_step_numbers_are_reported(self):
        plan = self._valid_plan()
        plan["steps"][1]["number"] = 1
        errors = validate_plan(plan)
        self.assertTrue(any("Duplicate" in e for e in errors))

    def test_non_dict_plan_is_reported(self):
        errors = validate_plan(["not", "a", "dict"])
        self.assertEqual(len(errors), 1)

    def test_steps_not_a_list_is_reported(self):
        plan = self._valid_plan()
        plan["steps"] = "step one, then step two"
        errors = validate_plan(plan)
        self.assertTrue(any("'steps' must be a list" in e for e in errors))


class TestParseJson(unittest.TestCase):

    def test_extracts_json_surrounded_by_prose(self):
        raw = 'Here is the plan you asked for:\n{"goal": "test"}\nLet me know if you need changes.'
        self.assertEqual(_parse_json(raw), {"goal": "test"})

    def test_extracts_json_from_code_fence(self):
        raw = '```json\n{"goal": "test", "steps": []}\n```'
        self.assertEqual(_parse_json(raw), {"goal": "test", "steps": []})

    def test_handles_nested_objects(self):
        raw = '{"goal": "test", "nested": {"a": 1, "b": [1, 2, 3]}}'
        self.assertEqual(_parse_json(raw), {"goal": "test", "nested": {"a": 1, "b": [1, 2, 3]}})

    def test_ignores_braces_inside_strings(self):
        raw = '{"intent": "click the button labeled {Submit}"}'
        result = _parse_json(raw)
        self.assertEqual(result["intent"], "click the button labeled {Submit}")

    def test_returns_empty_dict_when_no_json_present(self):
        self.assertEqual(_parse_json("no json here at all"), {})

    def test_returns_empty_dict_for_empty_input(self):
        self.assertEqual(_parse_json(""), {})


if __name__ == "__main__":
    unittest.main()
