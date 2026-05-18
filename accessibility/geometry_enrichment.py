from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import ifcopenshell
import ifcopenshell.geom
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import XSD
from shapely.geometry import box


ACC = Namespace("http://example.org/accessibility#")
PROPS = Namespace("http://lbd.arch.rwth-aachen.de/props#")


def enrich_graph_with_geometry(uploaded_file, graph: Graph) -> tuple[int, list[str]]:
    with NamedTemporaryFile(delete=False, suffix=".ifc") as temp_file:
        temp_file.write(uploaded_file.getvalue())
        temp_path = temp_file.name

    model = ifcopenshell.open(temp_path)
    Path(temp_path).unlink(missing_ok=True)

    subjects = _subjects_by_global_id(graph)
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    messages: list[str] = []
    added = 0
    geometry_count = 0

    for element in _geometry_elements(model):
        subject = subjects.get(getattr(element, "GlobalId", ""))
        if subject is None:
            continue
        box_data = _box_data(settings, element)
        if box_data is None:
            continue

        width, depth, height, area, center_x, center_y, center_z = box_data
        graph.add((subject, ACC.geometryWidthM, Literal(width, datatype=XSD.double)))
        graph.add((subject, ACC.geometryDepthM, Literal(depth, datatype=XSD.double)))
        graph.add((subject, ACC.geometryHeightM, Literal(height, datatype=XSD.double)))
        graph.add((subject, ACC.footprintAreaM2, Literal(area, datatype=XSD.double)))
        graph.add((subject, ACC.centerX, Literal(center_x, datatype=XSD.double)))
        graph.add((subject, ACC.centerY, Literal(center_y, datatype=XSD.double)))
        graph.add((subject, ACC.centerZ, Literal(center_z, datatype=XSD.double)))
        added += 7
        geometry_count += 1

        if element.is_a("IfcSpace"):
            clear_width = min(width, depth)
            graph.add((subject, ACC.derivedClearWidthM, Literal(clear_width, datatype=XSD.double)))
            graph.add((subject, ACC.derivedTurningDiameterM, Literal(clear_width, datatype=XSD.double)))
            added += 2
        if element.is_a("IfcDoor"):
            graph.add((subject, ACC.derivedDoorWidthM, Literal(max(width, depth), datatype=XSD.double)))
            graph.add((subject, ACC.derivedDoorHeightM, Literal(height, datatype=XSD.double)))
            added += 2

    route_edges = _add_space_boundary_routes(model, graph, subjects)
    added += route_edges

    if geometry_count:
        messages.append(f"Added geometry triples for {geometry_count} IFC elements.")
    else:
        messages.append("No usable IFC geometry could be converted into accessibility triples.")

    if route_edges:
        messages.append("Added route relation triples from IFC space boundaries.")
    else:
        messages.append("No route relation triples were added. Space boundary data may be missing.")

    return added, messages


def _subjects_by_global_id(graph: Graph) -> dict[str, object]:
    mapping = {}
    for subject, value in graph.subject_objects(PROPS.globalIdIfcRoot_attribute_simple):
        mapping[str(value)] = subject
    return mapping


def _geometry_elements(model):
    classes = [
        "IfcDoor",
        "IfcSpace",
        "IfcStair",
        "IfcStairFlight",
        "IfcRamp",
        "IfcRampFlight",
        "IfcSlab",
    ]
    for class_name in classes:
        for element in model.by_type(class_name):
            yield element


def _box_data(settings, element):
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    verts = list(shape.geometry.verts)
    if len(verts) < 3:
        return None

    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]
    width = max(xs) - min(xs)
    depth = max(ys) - min(ys)
    height = max(zs) - min(zs)
    footprint = box(min(xs), min(ys), max(xs), max(ys))
    return (
        round(width, 4),
        round(depth, 4),
        round(height, 4),
        round(footprint.area, 4),
        round((max(xs) + min(xs)) / 2, 4),
        round((max(ys) + min(ys)) / 2, 4),
        round((max(zs) + min(zs)) / 2, 4),
    )


def _add_space_boundary_routes(model, graph: Graph, subjects: dict[str, object]) -> int:
    added = 0
    for relation in model.by_type("IfcRelSpaceBoundary"):
        space = getattr(relation, "RelatingSpace", None)
        element = getattr(relation, "RelatedBuildingElement", None)
        if space is None or element is None:
            continue
        space_subject = subjects.get(getattr(space, "GlobalId", ""))
        element_subject = subjects.get(getattr(element, "GlobalId", ""))
        if space_subject is None or element_subject is None:
            continue
        graph.add((space_subject, ACC.hasBoundaryElement, element_subject))
        added += 1
        if element.is_a("IfcDoor"):
            graph.add((space_subject, ACC.hasRouteDoor, element_subject))
            added += 1
    return added
