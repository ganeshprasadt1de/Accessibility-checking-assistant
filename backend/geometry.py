from __future__ import annotations

import math
from pathlib import Path

from .config import RULE_LIMITS
from .model import Element


def _safe_name(obj) -> str:
    name = getattr(obj, "Name", None)
    return str(name) if name else getattr(obj, "GlobalId", "unnamed")


def _bbox_from_shape(shape) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    verts = getattr(shape.geometry, "verts", None)
    if not verts:
        return None
    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _bbox_from_placement(obj, unit_scale: float = 1.0) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    placement = getattr(obj, "ObjectPlacement", None)
    try:
        loc = placement.RelativePlacement.Location.Coordinates
        x = float(loc[0]) * unit_scale
        y = float(loc[1]) * unit_scale
        z = float(loc[2] if len(loc) > 2 else 0) * unit_scale
        return (x - 0.25, y - 0.25, z), (x + 0.25, y + 0.25, z + 2.1)
    except Exception:
        return None


def _storey_name(obj) -> str | None:
    try:
        for rel in getattr(obj, "ContainedInStructure", []) or []:
            structure = rel.RelatingStructure
            if structure:
                return _safe_name(structure)
    except Exception:
        return None
    return None


def extract_elements(ifc_path: Path) -> tuple[list[Element], list[str]]:
    import ifcopenshell
    import ifcopenshell.geom
    try:
        from ifcopenshell.util.unit import calculate_unit_scale
    except Exception:
        calculate_unit_scale = None

    model = ifcopenshell.open(str(ifc_path))
    unit_scale = calculate_unit_scale(model) if calculate_unit_scale else 1.0
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    wanted = [
        "IfcSpace",
        "IfcDoor",
        "IfcWall",
        "IfcSlab",
        "IfcRamp",
        "IfcRampFlight",
        "IfcStair",
        "IfcStairFlight",
        "IfcColumn",
        "IfcTransportElement",
    ]
    elements: list[Element] = []
    missing_geometry: list[str] = []
    for ifc_type in wanted:
        for obj in model.by_type(ifc_type):
            guid = getattr(obj, "GlobalId", None) or str(obj.id())
            bbox = None
            try:
                bbox = _bbox_from_shape(ifcopenshell.geom.create_shape(settings, obj))
            except Exception:
                bbox = _bbox_from_placement(obj, unit_scale)
            if not bbox:
                missing_geometry.append(guid)
                element = Element(guid, ifc_type, _safe_name(obj), f"{ifc_type} {guid}", storey=_storey_name(obj))
                elements.append(element)
                continue
            mn, mx = bbox
            width = abs(mx[0] - mn[0])
            depth = abs(mx[1] - mn[1])
            height = abs(mx[2] - mn[2])
            center = ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2)
            extra: dict[str, float | str | bool | None] = {}
            if ifc_type == "IfcDoor":
                declared_width = _length_attribute_number(obj, "OverallWidth", unit_scale) or _length_property_number(obj, ["OverallWidth", "Width", "ClearWidth"], unit_scale)
                extra["derivedDoorWidthM"] = declared_width or _door_opening_width(width, depth)
                extra["routeDoorCenterPoint"] = ",".join(f"{v:.4f}" for v in center)
            if ifc_type in {"IfcRamp", "IfcRampFlight"}:
                run = max(width, depth)
                rise = height
                extra["rampRunLengthM"] = run
                extra["rampUsableWidthM"] = min(width, depth)
                extra["rampSlopePercent"] = (rise / run * 100) if run > 0 else None
                extra["rampPlatformLengthM"] = _length_property_number(obj, ["PlatformLength", "LandingLength"], unit_scale)
            if ifc_type == "IfcSpace":
                extra["derivedClearSpaceWidthM"] = min(width, depth)
                extra["movementAreaWidthM"] = min(width, depth)
                extra["movementAreaDepthM"] = max(width, depth)
                extra["turningSpaceM"] = min(width, depth)
            elements.append(
                Element(
                    guid=guid,
                    ifc_type=ifc_type,
                    name=_safe_name(obj),
                    label=f"{ifc_type} {_safe_name(obj)}",
                    width=width,
                    depth=depth,
                    height=height,
                    center=center,
                    bbox_min=mn,
                    bbox_max=mx,
                    storey=_storey_name(obj),
                    extra=extra,
                )
            )
    return elements, missing_geometry


def _property_number(obj, names: list[str]) -> float | None:
    try:
        for rel in getattr(obj, "IsDefinedBy", []) or []:
            pset = getattr(rel, "RelatingPropertyDefinition", None)
            props = getattr(pset, "HasProperties", []) or []
            for prop in props:
                if getattr(prop, "Name", None) in names:
                    val = getattr(getattr(prop, "NominalValue", None), "wrappedValue", None)
                    if val is not None:
                        return float(val)
    except Exception:
        return None
    return None


def _attribute_number(obj, name: str) -> float | None:
    try:
        value = getattr(obj, name, None)
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _length_property_number(obj, names: list[str], unit_scale: float) -> float | None:
    value = _property_number(obj, names)
    return _scale_if_project_length(value, unit_scale)


def _length_attribute_number(obj, name: str, unit_scale: float) -> float | None:
    value = _attribute_number(obj, name)
    return _scale_if_project_length(value, unit_scale)


def _scale_if_project_length(value: float | None, unit_scale: float) -> float | None:
    if value is None:
        return None
    # IFC attributes such as IfcDoor.OverallWidth are stored in project units.
    # IfcOpenShell geometry is already returned in metres in this setup.
    return value * unit_scale if unit_scale != 1.0 else value


def _door_opening_width(width: float, depth: float) -> float | None:
    horizontal = sorted([abs(width), abs(depth)])
    if horizontal[1] <= 0:
        return None
    if horizontal[0] <= 0.30:
        return horizontal[1]
    return horizontal[0]


def obstacle_elements(elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.ifc_type in {"IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"} and e.bbox_min and e.bbox_max]


def intersects_box(a_min, a_max, b_min, b_max) -> bool:
    return all(a_min[i] <= b_max[i] and a_max[i] >= b_min[i] for i in range(3))


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))
