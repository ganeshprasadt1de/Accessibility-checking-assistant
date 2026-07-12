import json
import unittest
from unittest.mock import patch

from backend.short_explainer import _allowed_actions, _summary, explain_question


STAIR_CONTEXT = {
    "detectedIssueTypes": ["stair_block"],
    "issueCountsByType": {"stair_block": 1},
    "affectedElements": [{"name": "Treppe-EG", "type": "IfcStair", "rule": "stair_block", "details": "route intersects stair"}],
    "failedRoutes": [{"edgeId": "E00565", "from": "Door 18", "to": "Treppe-EG", "floor": "E00_OKRD", "distanceM": 4.2, "reasons": ["stair_block"]}],
    "floorsWithFailures": [{"name": "E00_OKRD", "failedRouteEdges": 1, "failureReasons": {"stair_block": 1}}],
}


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ShortExplainerTests(unittest.TestCase):
    def test_stair_action_does_not_offer_ramp_lift_elevator_or_slope(self):
        text = " ".join(item["text"] for item in _allowed_actions(STAIR_CONTEXT)).lower()
        for unsupported in ("ramp", "lift", "elevator", "slope"):
            self.assertNotIn(unsupported, text)
        self.assertIn("does not intersect", text)
        self.assertIn("e00565", text)

    def test_route_rule_aliases_are_not_omitted(self):
        context = {
            "detectedIssueTypes": ["route_door_width", "route_ramp_slope", "missing_door_width"],
            "issueCountsByType": {"route_door_width": 2, "route_ramp_slope": 1, "missing_door_width": 3},
            "affectedElements": [], "failedRoutes": [], "floorsWithFailures": [],
        }
        summary = _summary(context)
        self.assertIn("2 door too narrow issues", summary)
        self.assertIn("1 ramp too steep issue", summary)
        self.assertIn("3 missing data issues", summary)
        self.assertEqual(len(_allowed_actions(context)), 3)

    @patch("backend.short_explainer.urllib.request.urlopen")
    def test_general_repair_question_is_filled_with_distinct_grounded_actions(self, urlopen):
        context = {
            "detectedIssueTypes": ["door_width", "corridor_width", "stair_block"],
            "issueCountsByType": {"door_width": 2, "corridor_width": 1, "stair_block": 1},
            "affectedElements": [],
            "failedRoutes": [{"edgeId": "E00565", "from": "Door 18", "to": "Stair", "floor": "Ground", "reasons": ["stair_block"]}],
            "floorsWithFailures": [{"name": "Ground"}],
        }
        model_json = {
            "evidenceReview": [{"issueType": "door_width", "evidence": "door_width is detected", "selectedActionId": "action_1"}],
            "selectedActionIds": ["action_1"],
        }
        urlopen.return_value = FakeResponse({"response": json.dumps(model_json)})
        result = explain_question("What are the ways I can fix the building?", context)
        self.assertEqual(len(result["blocks"][2]["items"]), 3)

    @patch("backend.short_explainer.urllib.request.urlopen")
    def test_response_is_structured_and_uses_backend_action_text(self, urlopen):
        model_json = {
            "evidenceReview": [{"issueType": "stair_block", "evidence": "E00565 intersects stair geometry", "selectedActionId": "action_1"}],
            "selectedActionIds": ["action_1"],
        }
        urlopen.return_value = FakeResponse({"response": json.dumps(model_json)})
        result = explain_question("How can I fix this?", STAIR_CONTEXT)
        self.assertEqual([item["type"] for item in result["blocks"]], ["paragraph", "heading", "list"])
        rendered = json.dumps(result).lower()
        self.assertNotIn("**", rendered)
        self.assertNotIn("elevator", rendered)
        self.assertIn("e00565", rendered)
        request_payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(request_payload["options"]["temperature"], 0)
        self.assertEqual(request_payload["format"]["type"], "object")

    @patch("backend.short_explainer.urllib.request.urlopen")
    def test_mismatched_rule_and_action_uses_grounded_fallback(self, urlopen):
        bad = {
            "evidenceReview": [{"issueType": "ramp_slope", "evidence": "invented", "selectedActionId": "action_1"}],
            "selectedActionIds": ["action_1"],
        }
        urlopen.return_value = FakeResponse({"response": json.dumps(bad)})
        result = explain_question("How can I fix this?", STAIR_CONTEXT)
        self.assertTrue(result["groundedFallback"])
        self.assertIn("incomplete Ollama output was rejected", result["source"])
        self.assertNotIn("ramp", result["answer"].lower())


if __name__ == "__main__":
    unittest.main()
