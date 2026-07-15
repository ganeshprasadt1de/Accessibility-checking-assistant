from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Element:
    guid: str
    ifc_type: str
    name: str
    label: str
    source: str = "IFC geometry"
    width: float | None = None
    depth: float | None = None
    height: float | None = None
    center: tuple[float, float, float] | None = None
    bbox_min: tuple[float, float, float] | None = None
    bbox_max: tuple[float, float, float] | None = None
    storey: str | None = None
    extra: dict[str, float | str | bool | None] = field(default_factory=dict)
    issue_regions: list[dict] = field(default_factory=list)
    passing_area_gaps: list[dict] = field(default_factory=list)


@dataclass
class RouteEdge:
    edge_id: str
    start_guid: str
    end_guid: str
    distance_m: float
    status: str
    reasons: list[str]
    path: list[tuple[float, float, float]]
    source: str = "IfcOpenShell geometry and route graph"
    via_space_guid: str | None = None
    via_space_label: str | None = None
    measurements: dict[str, float | str | bool | None] = field(default_factory=dict)


@dataclass
class Issue:
    issue_id: str
    element_guid: str
    element_label: str
    element_type: str
    rule_id: str
    severity: str
    measured: float | None
    required: float | None
    unit: str
    source: str
    short_text: str
    details: str
    evidence_id: str | None = None
