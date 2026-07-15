import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Namespace

from backend.shacl_runner import _required
from backend.ifc_tools import add_geometry_to_graph
from backend.model import Element
from backend.shacl_runner import issues_from_shacl_report, run_shacl


ROOT = Path(__file__).resolve().parents[1]
SH = Namespace("http://www.w3.org/ns/shacl#")


class AccessibilityRuleTests(unittest.TestCase):
    def test_door_height_and_ramp_run_length_rules(self):
        data = Graph().parse(
            data="""
@prefix acc: <https://example.org/wheelchair-accessibility#> .

acc:lowDoor a acc:IfcDoor ;
    acc:isRouteRelevantDoor true ;
    acc:derivedDoorWidthM 1.0 ;
    acc:derivedDoorHeightM 2.0 .

acc:missingDoorHeight a acc:IfcDoor ;
    acc:isRouteRelevantDoor true ;
    acc:derivedDoorWidthM 1.0 .

acc:longRamp a acc:IfcRamp ;
    acc:rampSlopePercent 5.0 ;
    acc:rampUsableWidthM 1.2 ;
    acc:rampRunLengthM 6.1 .

acc:route a acc:RouteEdge ;
    acc:routeDoorWidthMinM 1.0 ;
    acc:routeDoorHeightMinM 2.0 ;
    acc:routeRampRunLengthM 6.1 .
""",
            format="turtle",
        )
        shapes = Graph().parse(ROOT / "rules" / "accessibility_rules.shacl.ttl", format="turtle")
        conforms, report, _text = validate(data, shacl_graph=shapes)
        rules = {str(message).split("|", 1)[0] for message in report.objects(None, SH.resultMessage)}
        self.assertFalse(conforms)
        self.assertEqual(
            rules,
            {"door_height", "missing_door_height", "ramp_run_length", "route_door_height", "route_ramp_run_length"},
        )

    def test_height_and_ramp_length_thresholds_are_inclusive(self):
        data = Graph().parse(
            data="""
@prefix acc: <https://example.org/wheelchair-accessibility#> .

acc:door a acc:IfcDoor ;
    acc:isRouteRelevantDoor true ;
    acc:derivedDoorWidthM 0.90 ;
    acc:derivedDoorHeightM 2.05 .

acc:excludedDoor a acc:IfcDoor ;
    acc:isRouteRelevantDoor false ;
    acc:derivedDoorWidthM 0.50 .

acc:ramp a acc:IfcRamp ;
    acc:rampSlopePercent 6.0 ;
    acc:rampUsableWidthM 1.20 ;
    acc:rampRunLengthM 6.00 .

acc:route a acc:RouteEdge ;
    acc:routeDoorWidthMinM 0.90 ;
    acc:routeDoorHeightMinM 2.05 ;
    acc:routeRampSlopePercent 6.0 ;
    acc:routeRampUsableWidthM 1.20 ;
    acc:routeRampRunLengthM 6.00 .
""",
            format="turtle",
        )
        shapes = Graph().parse(ROOT / "rules" / "accessibility_rules.shacl.ttl", format="turtle")
        conforms, _report, _text = validate(data, shacl_graph=shapes)
        self.assertTrue(conforms)

    def test_corridor_slope_and_passing_area_rules(self):
        data = Graph().parse(
            data="""
@prefix acc: <https://example.org/wheelchair-accessibility#> .

acc:longCorridor a acc:IfcSpace ;
    acc:isCorridorLike true ;
    acc:derivedClearSpaceWidthM 1.50 ;
    acc:derivedCorridorLengthM 12.0 ;
    acc:derivedCorridorSlopePercent 3.1 .

acc:shortCorridor a acc:IfcSpace ;
    acc:isCorridorLike true ;
    acc:derivedClearSpaceWidthM 1.50 ;
    acc:derivedCorridorLengthM 10.0 ;
    acc:derivedCorridorSlopePercent 4.0 .

acc:shortSteepCorridor a acc:IfcSpace ;
    acc:isCorridorLike true ;
    acc:derivedClearSpaceWidthM 1.50 ;
    acc:derivedCorridorLengthM 10.0 ;
    acc:derivedCorridorSlopePercent 4.1 .

acc:gap1 a acc:CorridorPassingAreaGap ;
    acc:inCorridor acc:longCorridor ;
    acc:passingAreaGapLengthM 15.1 .

acc:gap2 a acc:CorridorPassingAreaGap ;
    acc:inCorridor acc:longCorridor ;
    acc:passingAreaGapLengthM 16.2 .

acc:gap3 a acc:CorridorPassingAreaGap ;
    acc:inCorridor acc:shortCorridor ;
    acc:passingAreaGapLengthM 15.0 .
""",
            format="turtle",
        )
        shapes = Graph().parse(ROOT / "rules" / "accessibility_rules.shacl.ttl", format="turtle")
        conforms, report, _text = validate(data, shacl_graph=shapes)
        rules = [str(message).split("|", 1)[0] for message in report.objects(None, SH.resultMessage)]
        self.assertFalse(conforms)
        self.assertEqual(rules.count("corridor_slope"), 2)
        self.assertEqual(rules.count("corridor_movement_area"), 2)
        acc = Namespace("https://example.org/wheelchair-accessibility#")
        self.assertEqual(_required("corridor_slope", data, acc.shortCorridor), (4.0, "%"))
        self.assertEqual(_required("corridor_slope", data, acc.longCorridor), (3.0, "%"))

    def test_passing_area_results_keep_separate_evidence_ids(self):
        corridor = Element("S", "IfcSpace", "S", "IfcSpace S")
        corridor.passing_area_gaps = [
            {"evidence_id": "G1", "region_id": "R1", "measured": 15.1, "movement_space_m": 1.8},
            {"evidence_id": "G2", "region_id": "R2", "measured": 17.2, "movement_space_m": 1.8},
            {"evidence_id": "G3", "region_id": "R3", "measured": 15.0, "movement_space_m": 1.8},
        ]
        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "data.ttl"
            report_path = Path(directory) / "report.ttl"
            data = Graph()
            add_geometry_to_graph(data, [corridor])
            data.serialize(data_path, format="turtle")
            run_shacl(data_path, ROOT / "rules" / "accessibility_rules.shacl.ttl", report_path)
            issues = issues_from_shacl_report(report_path, data_path, [corridor], [])
        self.assertEqual([issue.evidence_id for issue in issues], ["G1", "G2"])
        self.assertEqual([issue.element_guid for issue in issues], ["S", "S"])


if __name__ == "__main__":
    unittest.main()
