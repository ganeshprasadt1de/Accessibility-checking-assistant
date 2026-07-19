from __future__ import annotations

import math
import os
from pathlib import Path

from .config import RULE_LIMITS
from .model import Element
from .resource_control import low_end_enabled

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


def _shape_plan_size(shape) -> tuple[float, float] | None:
    from shapely.geometry import MultiPoint

    verts = getattr(shape.geometry, "verts", None)
    if not verts:
        return None
    rectangle = MultiPoint(
        [(float(verts[index]), float(verts[index + 1])) for index in range(0, len(verts), 3)]
    ).minimum_rotated_rectangle
    if rectangle.is_empty or not hasattr(rectangle, "exterior"):
        return None
    coordinates = list(rectangle.exterior.coords)
    lengths = [math.dist(first, second) for first, second in zip(coordinates, coordinates[1:])]
    lengths = [value for value in lengths if value > 1e-6]
    return (min(lengths), max(lengths)) if lengths else None


def _shape_floor_slope_percent(shape) -> float | None:
    verts = getattr(shape.geometry, "verts", None)
    faces = getattr(shape.geometry, "faces", None)
    if not verts or not faces:
        return None
    points = [
        (float(verts[index]), float(verts[index + 1]), float(verts[index + 2]))
        for index in range(0, len(verts), 3)
    ]
    min_z = min(point[2] for point in points)
    max_z = max(point[2] for point in points)
    limit = min_z + min(1.5, (max_z - min_z) * 0.45)
    areas: dict[float, float] = {}
    for index in range(0, len(faces), 3):
        first, second, third = [points[int(value)] for value in faces[index : index + 3]]
        if max(first[2], second[2], third[2]) > limit:
            continue
        first_edge = tuple(second[value] - first[value] for value in range(3))
        second_edge = tuple(third[value] - first[value] for value in range(3))
        normal = (
            first_edge[1] * second_edge[2] - first_edge[2] * second_edge[1],
            first_edge[2] * second_edge[0] - first_edge[0] * second_edge[2],
            first_edge[0] * second_edge[1] - first_edge[1] * second_edge[0],
        )
        projected_area = abs(normal[2]) / 2
        if projected_area <= 1e-6:
            continue
        slope = round(math.hypot(normal[0], normal[1]) / abs(normal[2]) * 100, 2)
        areas[slope] = areas.get(slope, 0.0) + projected_area
    if not areas:
        return None
    total = sum(areas.values())
    significant = [slope for slope, area in areas.items() if area >= max(0.01, total * 0.005)]
    return max(significant or areas)


def _shape_footprint_mapping(shape) -> dict | None:
    from shapely.geometry import Polygon, mapping
    from shapely.ops import unary_union

    verts = getattr(shape.geometry, "verts", None)
    faces = getattr(shape.geometry, "faces", None)
    if not verts or not faces:
        return None
    polygons = []
    for index in range(0, len(faces), 3):
        points = []
        for vertex_index in faces[index : index + 3]:
            offset = int(vertex_index) * 3
            points.append((float(verts[offset]), float(verts[offset + 1])))
        polygon = Polygon(points)
        if polygon.is_valid and polygon.area > 1e-6:
            polygons.append(polygon)
    if not polygons:
        return None
    footprint = unary_union(polygons).buffer(0).simplify(0.005, preserve_topology=True)
    return mapping(footprint) if not footprint.is_empty else None


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
    objects_by_type = {ifc_type: list(model.by_type(ifc_type)) for ifc_type in wanted}
    geometry_targets = [obj for ifc_type in wanted for obj in objects_by_type[ifc_type]]
    geometry_boxes: dict[int, tuple[tuple[float, float, float], tuple[float, float, float]]] = {}
    geometry_inspection: dict[int, dict] = {}
    inspection_types = {
        "IfcSpace",
        "IfcRamp",
        "IfcRampFlight",
        "IfcWall",
        "IfcColumn",
        "IfcStair",
        "IfcStairFlight",
    }
    inspection_ids = {
        obj.id()
        for ifc_type in inspection_types
        for obj in objects_by_type[ifc_type]
    }
    slope_inspection_ids = {
        obj.id()
        for ifc_type in {"IfcSpace", "IfcRamp", "IfcRampFlight"}
        for obj in objects_by_type[ifc_type]
    }
    geometry_threads = 1 if low_end_enabled() else max(1, os.cpu_count() or 1)
    if geometry_targets:
        iterator = ifcopenshell.geom.iterator(
            settings,
            model,
            num_threads=geometry_threads,
            include=geometry_targets,
        )
        if iterator.initialize():
            while True:
                shape = iterator.get()
                bbox = _bbox_from_shape(shape)
                if bbox:
                    geometry_boxes[shape.id] = bbox
                if shape.id in inspection_ids:
                    geometry_inspection[shape.id] = {
                        "planSize": _shape_plan_size(shape),
                        "floorSlopePercent": _shape_floor_slope_percent(shape) if shape.id in slope_inspection_ids else None,
                        "footprint": _shape_footprint_mapping(shape),
                    }
                if not iterator.next():
                    break

    elements: list[Element] = []
    missing_geometry: list[str] = []
    for ifc_type in wanted:
        for obj in objects_by_type[ifc_type]:
            guid = getattr(obj, "GlobalId", None) or str(obj.id())
            name = _safe_name(obj)
            bbox = geometry_boxes.get(obj.id())
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
            inspection = geometry_inspection.get(obj.id(), {})
            if inspection.get("footprint"):
                extra["_inspectionFootprint"] = inspection["footprint"]
            if ifc_type == "IfcDoor":
                declared_width = _length_attribute_number(obj, "OverallWidth", unit_scale) or _length_property_number(
                    obj,
                    ["OverallWidth", "Width", "ClearWidth"],
                    unit_scale,
                )
                extra["derivedDoorWidthM"] = declared_width or _door_opening_width(width, depth)
                declared_height = _length_property_number(obj, ["ClearHeight"], unit_scale)
                if declared_height is not None:
                    extra["derivedDoorHeightM"] = declared_height
                    extra["doorHeightSource"] = "ClearHeight property"
                    extra["inspectionDoorWidthM"] = extra["derivedDoorWidthM"]
                    extra["inspectionDoorHeightM"] = declared_height
                else:
                    declared_height = _length_attribute_number(obj, "OverallHeight", unit_scale) or _length_property_number(
                        obj,
                        ["OverallHeight", "Height"],
                        unit_scale,
                    )
                    extra["derivedDoorHeightM"] = declared_height or height
                    extra["doorHeightSource"] = "IfcDoor.OverallHeight" if declared_height is not None else "IFC door geometry"
                    dimensions_swapped = bool(
                        declared_width is not None
                        and declared_height is not None
                        and declared_width >= 1.80
                        and declared_height <= 1.50
                    )
                    if dimensions_swapped:
                        extra["inspectionDoorWidthM"] = declared_height
                        extra["inspectionDoorHeightM"] = declared_width
                        extra["inspectionDoorDimensionNote"] = "IfcDoor OverallWidth and OverallHeight are stored in reverse order."
                        extra["doorHeightSource"] = "swapped IfcDoor overall dimensions"
                        extra["doorWidthSource"] = "swapped IfcDoor overall dimensions"
                    else:
                        extra["inspectionDoorWidthM"] = extra["derivedDoorWidthM"]
                        extra["inspectionDoorHeightM"] = extra["derivedDoorHeightM"]
                if "doorWidthSource" not in extra:
                    extra["doorWidthSource"] = "IFC declared width" if declared_width is not None else "IFC door geometry"
                extra["routeDoorCenterPoint"] = ",".join(f"{v:.4f}" for v in center)
            if ifc_type in {"IfcRamp", "IfcRampFlight"}:
                run = max(width, depth)
                rise = height
                extra["rampRunLengthM"] = run
                extra["rampUsableWidthM"] = min(width, depth)
                extra["rampSlopePercent"] = (rise / run * 100) if run > 0 else None
                extra["rampPlatformLengthM"] = _length_property_number(obj, ["PlatformLength", "LandingLength"], unit_scale)
                inspection_size = geometry_inspection.get(obj.id(), {}).get("planSize")
                extra["inspectionRampRunLengthM"] = inspection_size[1] if inspection_size else run
                extra["inspectionRampRunSource"] = "minimum rotated IFC ramp footprint" if inspection_size else "axis-aligned IFC ramp bounds"
            if ifc_type == "IfcSpace":
                extra["derivedClearSpaceWidthM"] = min(width, depth)
                extra["movementAreaWidthM"] = min(width, depth)
                extra["movementAreaDepthM"] = max(width, depth)
                extra["turningSpaceM"] = min(width, depth)
                plan_size = inspection.get("planSize")
                if plan_size:
                    extra["derivedCorridorLengthM"] = plan_size[1]
                else:
                    extra["derivedCorridorLengthM"] = max(width, depth)
                extra["derivedCorridorSlopePercent"] = inspection.get("floorSlopePercent")
                extra["corridorSlopeSource"] = "IFC space floor faces" if inspection.get("floorSlopePercent") is not None else "unavailable"
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
