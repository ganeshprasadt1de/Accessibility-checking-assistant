from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIDTERM_ROOT = ROOT.parent


@dataclass(frozen=True)
class RuleLimits:
    """Simple indoor wheelchair route limits used by the prototype."""

    door_width_m: float = 0.90
    corridor_width_m: float = 1.50
    route_door_width_m: float = 0.90
    ramp_slope_percent: float = 6.0
    ramp_width_m: float = 1.20
    movement_width_m: float = 1.50
    movement_depth_m: float = 1.50
    turning_space_m: float = 1.50
    clearance_width_m: float = 0.90
    clearance_height_m: float = 2.05


RULE_LIMITS = RuleLimits()

NS = {
    "acc": "https://example.org/wheelchair-accessibility#",
    "bot": "https://w3id.org/bot#",
    "props": "https://w3id.org/props#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


def default_ifctolbd_zip() -> Path:
    return MIDTERM_ROOT / "IFCtoLBD-master.zip"
