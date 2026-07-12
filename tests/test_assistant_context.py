import unittest

from server import assistant_context


class AssistantContextTests(unittest.TestCase):
    def test_counts_and_issue_types_use_the_complete_shacl_result_set(self):
        issues = [
            {
                "element_label": f"Door {index}",
                "element_type": "IfcDoor",
                "rule_id": "door_width",
                "details": "too narrow",
            }
            for index in range(55)
        ]
        issues.append({"element_label": "Main stair", "element_type": "IfcStair", "rule_id": "stair_block", "details": "route intersects stair"})
        context = assistant_context({"issues": issues, "elements": [], "routeEdges": [], "floors": [], "summary": {"issueCount": 56}})
        self.assertEqual(context["issueCountsByType"], {"door_width": 55, "stair_block": 1})
        self.assertEqual(context["detectedIssueTypes"], ["door_width", "stair_block"])
        self.assertEqual(len(context["affectedElements"]), 40)

    def test_failed_route_examples_cover_each_failure_reason(self):
        edges = []
        for index in range(25):
            edges.append({"edgeId": f"E{index:05d}", "status": "fail", "startGuid": "a", "endGuid": "b", "distanceM": 1, "reasons": ["door_width"]})
        edges.append({"edgeId": "E00565", "status": "fail", "startGuid": "a", "endGuid": "stair", "distanceM": 2, "reasons": ["stair_block"]})
        data = {
            "issues": [],
            "elements": [
                {"guid": "a", "name": "Door 18", "storey": "E00_OKRD"},
                {"guid": "b", "name": "Door 19", "storey": "E00_OKRD"},
                {"guid": "stair", "name": "Treppe-EG", "storey": "E00_OKRD"},
            ],
            "routeEdges": edges,
            "floors": [],
            "summary": {},
        }
        context = assistant_context(data)
        examples = {route["edgeId"]: route for route in context["failedRoutes"]}
        self.assertIn("E00000", examples)
        self.assertIn("E00565", examples)
        self.assertEqual(examples["E00565"]["reasons"], ["stair_block"])


if __name__ == "__main__":
    unittest.main()
