from __future__ import annotations

import math
from pathlib import Path

from .config import RULE_LIMITS
from .model import Element

SEMANTIC_PROPERTY_NAMES = {
    "Category Description",
    "Name",
    "OmniClass Table 13 Category",
    "Reference",
}


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


def _storey_name(obj) -> str | None:
    try:
        for rel in getattr(obj, "ContainedInStructure", []) or []:
            structure = rel.RelatingStructure
            if structure:
                return _safe_name(structure)
    except Exception:
        return None
    return None


def _semantic_extra(obj, ifc_type: str, height: float | None = None) -> dict[str, float | str | bool | None]:
    values = _semantic_values(obj)
    extra: dict[str, float | str | bool | None] = {}
    for source, value in values:
        if source == "Name":
            extra["semanticName"] = value
        elif source == "LongName":
            extra["semanticLongName"] = value
        elif source == "ObjectType":
            extra["semanticObjectType"] = value
        elif source == "PredefinedType":
            extra["semanticPredefinedType"] = value
        elif source == "PropertyName":
            extra["semanticPsetName"] = value
        elif source == "Category Description":
            extra["semanticCategory"] = value
        elif source == "OmniClass Table 13 Category":
            extra["semanticOmniClass"] = value
        elif source == "Reference":
            extra["semanticReference"] = value

    text = _join_unique(value for _source, value in values)
    if text:
        extra["semanticText"] = text.lower()
    if ifc_type == "IfcSpace":
        usage = _space_usage(extra.get("semanticText"))
        if usage:
            extra["spaceUsage"] = usage
        extra["isCorridorLike"] = usage in {"corridor", "vestibule"}
        extra["isExcludedSpace"] = usage in {"roof", "service", "mechanical", "electrical", "heating", "toilet"}
    elif ifc_type == "IfcDoor":
        reason = _door_exclusion_reason(extra.get("semanticText"), height)
        extra["isExcludedRouteDoor"] = bool(reason)
        extra["isRouteRelevantDoor"] = not bool(reason)
        if reason:
            extra["routeDoorExclusionReason"] = reason
    return extra


def _semantic_values(obj) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for name in ["Name", "LongName", "ObjectType", "PredefinedType"]:
        value = _text_attribute(obj, name)
        if value:
            values.append((name, value))
    try:
        for rel in getattr(obj, "IsDefinedBy", []) or []:
            pset = getattr(rel, "RelatingPropertyDefinition", None)
            props = getattr(pset, "HasProperties", []) or []
            for prop in props:
                name = _text_attribute(prop, "Name")
                if name in SEMANTIC_PROPERTY_NAMES:
                    value = _property_text(prop)
                    if value and value.lower() != name.lower():
                        source = "PropertyName" if name == "Name" else name
                        values.append((source, value))
    except Exception:
        pass
    return _dedupe_values(values)


def _text_attribute(obj, name: str) -> str | None:
    try:
        value = getattr(obj, name, None)
        if value is None:
            return None
        text = str(value).strip()
        return text or None
    except Exception:
        return None


def _property_text(prop) -> str | None:
    value = getattr(prop, "NominalValue", None)
    if value is None:
        return None
    wrapped = getattr(value, "wrappedValue", value)
    text = str(wrapped).strip()
    return text or None


def _dedupe_values(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for source, value in values:
        key = (source, value.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append((source, value))
    return result


def _join_unique(values) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return " | ".join(result)


def _space_usage(text_value) -> str | None:
    text = str(text_value or "").lower()
    if any(word in text for word in ["corridor", "korridor", "flur", "hall", "gang"]):
        return "corridor"
    if "vestibule" in text or "vest." in text:
        return "vestibule"
    if "roof" in text:
        return "roof"
    if "mechanical" in text:
        return "mechanical"
    if "electrical" in text:
        return "electrical"
    if "heating" in text:
        return "heating"
    if "service" in text or "shaft" in text or "chase" in text or "gfa" in text or "volume" in text:
        return "service"
    if "toilet" in text or " wc" in f" {text} " or "rwc" in text:
        return "toilet"
    return None


def _door_exclusion_reason(text_value, height: float | None) -> str | None:
    text = str(text_value or "").lower()
    if "toilet partition" in text:
        return "toilet_partition"
    if "curtain wall" in text:
        return "curtain_wall"
    if "partition" in text and height is not None and height < 1.80:
        return "low_partition"
    return None


def _element_label(ifc_type: str, name: str, extra: dict[str, float | str | bool | None]) -> str:
    label = f"{ifc_type} {name}"
    if ifc_type != "IfcSpace":
        return label
    for key in ["semanticLongName", "semanticPsetName", "semanticCategory"]:
        value = extra.get(key)
        if value and str(value).lower() != name.lower():
            return f"{label} {value}"
    return label


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
            name = _safe_name(obj)
            bbox = None
            try:
                bbox = _bbox_from_shape(ifcopenshell.geom.create_shape(settings, obj))
            except Exception:
                pass
            if not bbox:
                missing_geometry.append(guid)
                extra = _semantic_extra(obj, ifc_type)
                element = Element(
                    guid,
                    ifc_type,
                    name,
                    _element_label(ifc_type, name, extra),
                    storey=_storey_name(obj),
                    extra=extra,
                )
                elements.append(element)
                continue
            mn, mx = bbox
            width = abs(mx[0] - mn[0])
            depth = abs(mx[1] - mn[1])
            height = abs(mx[2] - mn[2])
            center = ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2)
            extra = _semantic_extra(obj, ifc_type, height)
            if ifc_type == "IfcDoor":
                declared_width = _length_attribute_number(obj, "OverallWidth", unit_scale) or _length_property_number(
                    obj,
                    ["OverallWidth", "Width", "ClearWidth"],
                    unit_scale,
                )
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
                    name=name,
                    label=_element_label(ifc_type, name, extra),
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
