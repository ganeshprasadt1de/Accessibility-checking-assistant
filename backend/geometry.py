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


def _placement_frame(obj, unit_scale: float = 1.0):
    if not getattr(obj, "ObjectPlacement", None):
        return None
    try:
        from ifcopenshell.util.placement import get_local_placement

        matrix = get_local_placement(obj.ObjectPlacement)
        origin = tuple(float(matrix[row][3]) * unit_scale for row in range(3))
        axes = []
        for column in range(3):
            axis = tuple(float(matrix[row][column]) for row in range(3))
            length = math.sqrt(sum(value * value for value in axis))
            if length <= 1e-6:
                return None
            axes.append(tuple(value / length for value in axis))
    except Exception:
        return None
    return origin, tuple(axes)


def _shape_local_size(shape, axes) -> tuple[float, float, float] | None:
    verts = getattr(shape.geometry, "verts", None)
    if not verts:
        return None
    values = [[], [], []]
    for index in range(0, len(verts), 3):
        point = tuple(float(verts[index + offset]) for offset in range(3))
        for axis_index, axis in enumerate(axes):
            values[axis_index].append(sum(point[offset] * axis[offset] for offset in range(3)))
    return tuple(max(axis_values) - min(axis_values) for axis_values in values)


def _shape_plan_axis(shape) -> tuple[float, float] | None:
    from shapely.geometry import MultiPoint

    verts = getattr(shape.geometry, "verts", None)
    if not verts:
        return None
    rectangle = MultiPoint([(verts[index], verts[index + 1]) for index in range(0, len(verts), 3)]).minimum_rotated_rectangle
    if rectangle.is_empty or not hasattr(rectangle, "exterior"):
        return None
    coordinates = list(rectangle.exterior.coords)
    edges = []
    for start, end in zip(coordinates, coordinates[1:]):
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length > 1e-6:
            edges.append((length, dx / length, dy / length))
    if len(edges) < 2:
        return None
    longest = max(edges)
    shortest = min(edges)
    if longest[0] < shortest[0] * 1.5:
        return None
    return longest[1], longest[2]


def _host_plan_axis(host, settings, cache: dict[int, tuple[float, float] | None]):
    key = host.id()
    if key in cache:
        return cache[key]
    try:
        import ifcopenshell.geom

        axis = _shape_plan_axis(ifcopenshell.geom.create_shape(settings, host))
    except Exception:
        axis = None
    cache[key] = axis
    return axis


def _door_opening(obj):
    try:
        for relation in getattr(obj, "FillsVoids", []) or []:
            opening = getattr(relation, "RelatingOpeningElement", None)
            if opening is not None:
                return opening
    except Exception:
        return None
    return None


def _door_host(opening):
    if opening is None:
        return None
    try:
        for relation in getattr(opening, "VoidsElements", []) or []:
            host = getattr(relation, "RelatingBuildingElement", None)
            if host is not None:
                return host
    except Exception:
        return None
    return None


def _door_dimensions(obj, shape, settings, unit_scale: float, host_axis_cache):
    frame = _placement_frame(obj, unit_scale)
    axes = frame[1] if frame else None
    declared_width = _length_attribute_number(obj, "OverallWidth", unit_scale) or _length_property_number(
        obj,
        ["OverallWidth"],
        unit_scale,
    )
    declared_height = _length_attribute_number(obj, "OverallHeight", unit_scale) or _length_property_number(
        obj,
        ["OverallHeight"],
        unit_scale,
    )
    clear_width = _length_property_number(obj, ["ClearWidth"], unit_scale)
    opening = _door_opening(obj)
    host = _door_host(opening)
    host_axis = _host_plan_axis(host, settings, host_axis_cache) if host is not None else None
    if axes is None and host_axis is not None:
        axes = (
            (host_axis[0], host_axis[1], 0.0),
            (-host_axis[1], host_axis[0], 0.0),
            (0.0, 0.0, 1.0),
        )
    body_size = _shape_local_size(shape, axes) if shape is not None and axes else None
    opening_shape = None
    if opening is not None:
        try:
            import ifcopenshell.geom

            opening_shape = ifcopenshell.geom.create_shape(settings, opening)
        except Exception:
            pass
    opening_size = _shape_local_size(opening_shape, axes) if opening_shape is not None and axes else None
    opening_bbox = _bbox_from_shape(opening_shape) if opening_shape is not None else None

    extra: dict[str, float | str | bool | None] = {}
    if axes:
        for name, axis in zip(["doorWidthAxis", "doorDepthAxis", "doorHeightAxis"], axes):
            for suffix, value in zip(["X", "Y", "Z"], axis):
                extra[f"{name}{suffix}"] = value
        extra["doorAxisSource"] = "IfcDoor.ObjectPlacement" if frame else "host wall geometry"
        if host_axis is not None:
            width_axis = axes[0]
            width_length = math.hypot(width_axis[0], width_axis[1])
            if width_length > 1e-6:
                alignment = abs((width_axis[0] * host_axis[0] + width_axis[1] * host_axis[1]) / width_length)
                extra["doorAxisMatchesHost"] = alignment >= 0.95
    if body_size:
        extra["doorLocalWidthM"] = body_size[0]
        extra["doorLocalDepthM"] = body_size[1]
        extra["doorLocalHeightM"] = body_size[2]
    if opening_size:
        extra["doorOpeningWidthM"] = opening_size[0]
        extra["doorOpeningDepthM"] = opening_size[1]
        extra["doorOpeningHeightM"] = opening_size[2]
    if opening is not None:
        extra["doorOpeningGuid"] = getattr(opening, "GlobalId", None)
    if host is not None:
        extra["doorHostGuid"] = getattr(host, "GlobalId", None)
    if declared_width is not None:
        extra["doorDeclaredWidthM"] = declared_width
    if declared_height is not None:
        extra["doorDeclaredHeightM"] = declared_height
    if clear_width is not None:
        extra["doorClearWidthM"] = clear_width

    opening_width = opening_size[0] if opening_size else None
    opening_height = opening_size[2] if opening_size else None
    dimensions_swapped = False
    width_conflict = False
    height_conflict = False
    if declared_width is not None and opening_width is not None:
        width_conflict = abs(declared_width - opening_width) > max(0.03, opening_width * 0.03)
    if declared_height is not None and opening_height is not None:
        height_conflict = abs(declared_height - opening_height) > max(0.03, opening_height * 0.03)
    if (declared_width is not None and opening_width is not None) or (declared_height is not None and opening_height is not None):
        extra["doorDimensionConflict"] = width_conflict or height_conflict
    if declared_width is not None and declared_height is not None and opening_width is not None and opening_height is not None:
        direct_error = abs(declared_width - opening_width) + abs(declared_height - opening_height)
        swapped_error = abs(declared_width - opening_height) + abs(declared_height - opening_width)
        dimensions_swapped = swapped_error < 0.25 and swapped_error < direct_error * 0.35
        if dimensions_swapped:
            extra["doorDimensionsSwapped"] = True

    if clear_width is not None:
        width = clear_width
        source = "ClearWidth property"
        confidence = "reported clear width"
    elif opening_width is not None and declared_width is not None:
        width = opening_width if dimensions_swapped else min(opening_width, declared_width)
        source = "IfcOpeningElement geometry and IfcDoor.OverallWidth"
        confidence = "nominal opening"
    elif opening_width is not None:
        width = opening_width
        source = "IfcOpeningElement geometry"
        confidence = "nominal opening"
    elif declared_width is not None:
        width = declared_width
        source = "IfcDoor.OverallWidth"
        confidence = "nominal opening"
    elif body_size:
        width = body_size[0]
        source = "IfcDoor geometry"
        confidence = "estimated"
    else:
        width = None
        source = "unavailable"
        confidence = "unknown"
    extra["derivedDoorWidthM"] = width
    extra["doorWidthSource"] = source
    extra["doorWidthConfidence"] = confidence
    return extra, opening_bbox


def _door_placement_bbox(obj, width: float | None, depth: float | None, height: float | None, unit_scale: float):
    frame = _placement_frame(obj, unit_scale)
    if width is None or height is None or frame is None:
        return None
    origin, axes = frame
    x_axis, y_axis, z_axis = axes
    depth = depth or 0.12
    points = [
        (
            origin[0] + x_axis[0] * x + y_axis[0] * y + z_axis[0] * z,
            origin[1] + x_axis[1] * x + y_axis[1] * y + z_axis[1] * z,
            origin[2] + x_axis[2] * x + y_axis[2] * y + z_axis[2] * z,
        )
        for x in [0.0, width]
        for y in [0.0, depth]
        for z in [0.0, height]
    ]
    return (
        (min(point[0] for point in points), min(point[1] for point in points), min(point[2] for point in points)),
        (max(point[0] for point in points), max(point[1] for point in points), max(point[2] for point in points)),
    )


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
    host_axis_cache = {}
    for ifc_type in wanted:
        for obj in model.by_type(ifc_type):
            guid = getattr(obj, "GlobalId", None) or str(obj.id())
            name = _safe_name(obj)
            shape = None
            bbox = None
            try:
                shape = ifcopenshell.geom.create_shape(settings, obj)
                bbox = _bbox_from_shape(shape)
            except Exception:
                pass
            door_extra = {}
            opening_bbox = None
            if ifc_type == "IfcDoor":
                door_extra, opening_bbox = _door_dimensions(obj, shape, settings, unit_scale, host_axis_cache)
            if not bbox:
                missing_geometry.append(guid)
                fallback_bbox = opening_bbox
                if ifc_type == "IfcDoor" and fallback_bbox is None:
                    fallback_bbox = _door_placement_bbox(
                        obj,
                        door_extra.get("derivedDoorWidthM"),
                        door_extra.get("doorOpeningDepthM") or door_extra.get("doorLocalDepthM"),
                        door_extra.get("doorOpeningHeightM") or door_extra.get("doorDeclaredHeightM") or door_extra.get("doorLocalHeightM"),
                        unit_scale,
                    )
                if fallback_bbox:
                    mn, mx = fallback_bbox
                    width = abs(mx[0] - mn[0])
                    depth = abs(mx[1] - mn[1])
                    height = abs(mx[2] - mn[2])
                    center = ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2)
                    door_height = door_extra.get("doorOpeningHeightM") or door_extra.get("doorDeclaredHeightM") or door_extra.get("doorLocalHeightM")
                    extra = _semantic_extra(obj, ifc_type, door_height)
                    extra.update(door_extra)
                    extra["routeDoorCenterPoint"] = ",".join(f"{v:.4f}" for v in center)
                    extra["routeGeometrySource"] = "IfcOpeningElement geometry" if opening_bbox else "IFC placement and door dimensions"
                    elements.append(
                        Element(
                            guid=guid,
                            ifc_type=ifc_type,
                            name=name,
                            label=_element_label(ifc_type, name, extra),
                            source=extra["routeGeometrySource"],
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
                    continue
                extra = _semantic_extra(obj, ifc_type, door_extra.get("doorDeclaredHeightM"))
                extra.update(door_extra)
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
            door_height = door_extra.get("doorOpeningHeightM") or door_extra.get("doorDeclaredHeightM") or door_extra.get("doorLocalHeightM")
            extra = _semantic_extra(obj, ifc_type, door_height or height)
            if ifc_type == "IfcDoor":
                extra.update(door_extra)
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


def obstacle_elements(elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.ifc_type in {"IfcWall", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"} and e.bbox_min and e.bbox_max]


def intersects_box(a_min, a_max, b_min, b_max) -> bool:
    return all(a_min[i] <= b_max[i] and a_max[i] >= b_min[i] for i in range(3))


def distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))
