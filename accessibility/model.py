from dataclasses import dataclass


@dataclass
class Element:
    key: str
    name: str
    kind: str
    clear_width_m: float | None = None
    clear_height_m: float | None = None
    approach_space_m: float | None = None
    reveal_depth_m: float | None = None
    threshold_height_m: float | None = None
    handle_height_m: float | None = None
    slope_percent: float | None = None
    usable_width_m: float | None = None
    length_m: float | None = None
    platform_length_m: float | None = None
    has_handrails: bool | None = None
    has_edge_protection: bool | None = None
    has_cross_slope: bool | None = None
    handrail_height_m: float | None = None
    handrail_diameter_m: float | None = None
    handrail_extension_m: float | None = None
    start_area_width_m: float | None = None
    start_area_depth_m: float | None = None
    end_area_width_m: float | None = None
    end_area_depth_m: float | None = None
    passing_space_m: float | None = None
    door_width_m: float | None = None
    cabin_width_m: float | None = None
    cabin_depth_m: float | None = None
    movement_area_width_m: float | None = None
    movement_area_depth_m: float | None = None
    turning_diameter_m: float | None = None
    opens_inward: bool | None = None
    has_washbasin: bool | None = None
    side_approach_width_m: float | None = None
    side_approach_depth_m: float | None = None
    has_emergency_call: bool | None = None
    route_door_width_m: float | None = None
    route_level_change_m: float | None = None
    route_pass: bool | None = None
    source: str = "IFCtoLBD"


@dataclass
class Issue:
    element_key: str
    element_name: str
    element_kind: str
    rule: str
    message: str
    value: str
    required: str
    explanation: str


@dataclass
class GeometryFinding:
    category: str
    element: str
    check: str
    result: str
    reason: str
    fix: str


@dataclass
class ViolationPoint:
    element: str
    rule: str
    explanation: str
    x: float
    y: float
    z: float
