from __future__ import annotations

import json
import struct
from pathlib import Path

from .model import Element, RouteEdge


def _pad4(data: bytes, pad: bytes = b" ") -> bytes:
    while len(data) % 4:
        data += pad
    return data


def export_box_glb(elements: list[Element], edges: list[RouteEdge], output_path: Path) -> None:
    """Create a compact GLB with scaled boxes and route polylines."""
    positions = [
        -0.5,
        -0.5,
        -0.5,
        0.5,
        -0.5,
        -0.5,
        0.5,
        0.5,
        -0.5,
        -0.5,
        0.5,
        -0.5,
        -0.5,
        -0.5,
        0.5,
        0.5,
        -0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        -0.5,
        0.5,
        0.5,
    ]
    indices = [0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 4, 5, 0, 5, 1, 1, 5, 6, 1, 6, 2, 2, 6, 7, 2, 7, 3, 3, 7, 4, 3, 4, 0]
    bin_data = struct.pack("<" + "f" * len(positions), *positions)
    pos_offset = 0
    bin_data = _pad4(bin_data, b"\x00")
    idx_offset = len(bin_data)
    bin_data += struct.pack("<" + "H" * len(indices), *indices)
    bin_data = _pad4(bin_data, b"\x00")

    buffer_views = [
        {"buffer": 0, "byteOffset": pos_offset, "byteLength": len(positions) * 4, "target": 34962},
        {"buffer": 0, "byteOffset": idx_offset, "byteLength": len(indices) * 2, "target": 34963},
    ]
    accessors = [
        {
            "bufferView": 0,
            "componentType": 5126,
            "count": 8,
            "type": "VEC3",
            "min": [-0.5, -0.5, -0.5],
            "max": [0.5, 0.5, 0.5],
        },
        {"bufferView": 1, "componentType": 5123, "count": len(indices), "type": "SCALAR"},
    ]
    materials = [
        {"name": "space", "pbrMetallicRoughness": {"baseColorFactor": [0.80, 0.88, 0.95, 0.18], "metallicFactor": 0, "roughnessFactor": 0.8}, "alphaMode": "BLEND"},
        {"name": "door", "pbrMetallicRoughness": {"baseColorFactor": [0.08, 0.42, 0.62, 1], "metallicFactor": 0, "roughnessFactor": 0.7}},
        {"name": "obstacle", "pbrMetallicRoughness": {"baseColorFactor": [0.55, 0.55, 0.52, 1], "metallicFactor": 0, "roughnessFactor": 0.9}},
        {"name": "ramp", "pbrMetallicRoughness": {"baseColorFactor": [0.50, 0.38, 0.18, 1], "metallicFactor": 0, "roughnessFactor": 0.9}},
        {"name": "stair", "pbrMetallicRoughness": {"baseColorFactor": [0.70, 0.15, 0.12, 1], "metallicFactor": 0, "roughnessFactor": 0.85}},
    ]
    meshes = []
    nodes = []
    scene_nodes = []
    for element in elements:
        if not (element.center and element.width and element.depth and element.height):
            continue
        material_index = 2
        if element.ifc_type == "IfcSpace":
            material_index = 0
        elif element.ifc_type == "IfcDoor":
            material_index = 1
        elif element.ifc_type == "IfcRamp":
            material_index = 3
        elif element.ifc_type == "IfcStair":
            material_index = 4
        mesh_index = len(meshes)
        meshes.append({"name": element.label, "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": material_index}]})
        cx, cy, cz = element.center
        sx = max(element.width, 0.02)
        sy = max(element.depth, 0.02)
        sz = max(element.height, 0.02)
        node_index = len(nodes)
        nodes.append(
            {
                "name": element.guid,
                "mesh": mesh_index,
                "translation": [cx, cy, cz],
                "scale": [sx, sy, sz],
                "extras": {"guid": element.guid, "ifcType": element.ifc_type, "label": element.label},
            }
        )
        scene_nodes.append(node_index)

    gltf = {
        "asset": {"version": "2.0", "generator": "wheelchair-accessibility-preprocessor"},
        "scene": 0,
        "scenes": [{"nodes": scene_nodes}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "buffers": [{"byteLength": len(bin_data)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_chunk = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    bin_chunk = _pad4(bin_data, b"\x00")
    total_len = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        handle.write(struct.pack("<4sII", b"glTF", 2, total_len))
        handle.write(struct.pack("<I4s", len(json_chunk), b"JSON"))
        handle.write(json_chunk)
        handle.write(struct.pack("<I4s", len(bin_chunk), b"BIN\x00"))
        handle.write(bin_chunk)
