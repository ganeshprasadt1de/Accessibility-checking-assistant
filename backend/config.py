from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RuleLimits:
    """Indoor wheelchair route limits used by the checker."""

    door_width_m: float = 0.90
    door_height_m: float = 2.05
    corridor_width_m: float = 1.50
    corridor_slope_percent: float = 3.0
    short_corridor_length_m: float = 10.0
    short_corridor_slope_percent: float = 4.0
    corridor_movement_interval_m: float = 15.0
    corridor_movement_space_m: float = 1.80
    route_door_width_m: float = 0.90
    route_door_height_m: float = 2.05
    ramp_slope_percent: float = 6.0
    ramp_width_m: float = 1.20
    ramp_run_length_m: float = 6.00
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
    return ROOT / "IFCtoLBD-master.zip"
