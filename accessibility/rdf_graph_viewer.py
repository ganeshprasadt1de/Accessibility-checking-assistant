from __future__ import annotations

import html
import json
from rdflib import Graph, Literal, Namespace, RDF, RDFS

from accessibility.model import Issue


ACC = Namespace("http://example.org/accessibility#")
BOT = Namespace("https://w3id.org/bot#")
PROPS = Namespace("http://lbd.arch.rwth-aachen.de/props#")

MAX_RAW_EDGES = 420
MAX_ENRICHED_EDGES = 520

IMPORTANT_RAW_TYPES = {
    "Element",
    "Space",
    "Zone",
    "IfcDoor",
    "IfcSpace",
    "IfcRamp",
    "IfcTransportElement",
    "IfcBuildingStorey",
}

ENRICHED_PREDICATES = {
    "centerX",
    "centerY",
    "centerZ",
    "geometryWidthM",
    "geometryDepthM",
    "geometryHeightM",
    "footprintAreaM2",
    "derivedClearWidthM",
    "derivedTurningDiameterM",
    "derivedDoorWidthM",
    "derivedDoorHeightM",
    "hasRouteDoor",
    "hasBoundaryElement",
    "fromSpace",
    "toSpace",
    "routeDoor",
    "routeDoorWidthM",
    "levelChangeM",
    "stepFree",
    "routePass",
    "doorCenterX",
    "doorCenterY",
    "doorCenterZ",
}


def make_rdf_graph_viewers(raw_graph: Graph, enriched_graph: Graph, issues: list[Issue]) -> tuple[str, dict[str, int]]:
    raw_data = _raw_graph_data(raw_graph)
    enriched_data = _enriched_graph_data(enriched_graph, issues)
    stats = {
        "raw_triples": len(raw_graph),
        "raw_visible_edges": len(raw_data["edges"]),
        "raw_visible_nodes": len(raw_data["nodes"]),
        "enriched_triples": len(enriched_graph),
        "enriched_visible_edges": len(enriched_data["edges"]),
        "enriched_visible_nodes": len(enriched_data["nodes"]),
    }
    return _html(raw_data, enriched_data, stats), stats


def _raw_graph_data(graph: Graph) -> dict[str, list[dict[str, object]]]:
    builder = _GraphBuilder()
    typed_subjects = _typed_subjects(graph)
    labels = _labels(graph)

    for subject in _ordered_subjects(graph, typed_subjects, labels):
        if builder.edge_count >= MAX_RAW_EDGES:
            break
        subject_label = labels.get(subject, _short(subject))
        builder.add_node(subject, subject_label, _node_group(subject, typed_subjects))
        for predicate, obj in graph.predicate_objects(subject):
            if predicate == RDF.type:
                builder.add_node(obj, _short(obj), "type")
                builder.add_edge(subject, obj, "type")
            elif predicate == RDFS.label:
                continue
            elif _keep_raw_predicate(predicate, obj):
                obj_label = _literal_label(obj) if isinstance(obj, Literal) else labels.get(obj, _short(obj))
                builder.add_node(obj, obj_label, _node_group(obj, typed_subjects))
                builder.add_edge(subject, obj, _short(predicate))
            if builder.edge_count >= MAX_RAW_EDGES:
                break

    return builder.data()


def _enriched_graph_data(graph: Graph, issues: list[Issue]) -> dict[str, list[dict[str, object]]]:
    builder = _GraphBuilder()
    labels = _labels(graph)
    subject_by_short = {_short(subject): subject for subject in graph.subjects()}
    issue_subjects = {subject_by_short.get(issue.element_key) for issue in issues}
    issue_subjects.discard(None)

    for subject, predicate, obj in graph:
        pred_name = _short(predicate)
        if pred_name not in ENRICHED_PREDICATES:
            continue
        if builder.edge_count >= MAX_ENRICHED_EDGES:
            break
        group = "issue-element" if subject in issue_subjects else "geometry"
        builder.add_node(subject, labels.get(subject, _short(subject)), group)
        obj_group = "value" if isinstance(obj, Literal) else "route"
        builder.add_node(obj, _literal_label(obj) if isinstance(obj, Literal) else labels.get(obj, _short(obj)), obj_group)
        builder.add_edge(subject, obj, pred_name)

    for issue in issues[:350]:
        subject = subject_by_short.get(issue.element_key)
        if subject is None:
            continue
        rule_id = f"requirement:{issue.rule}:{issue.required}"
        missing_id = f"current:{issue.element_key}:{issue.rule}:{issue.value}"
        builder.add_node(subject, issue.element_name, "issue-element")
        builder.add_node(rule_id, f"{issue.rule}: {issue.required}", "requirement")
        builder.add_node(missing_id, f"current: {issue.value}", "missing" if issue.value == "missing" else "value")
        builder.add_edge(subject, missing_id, "has current value")
        builder.add_edge(subject, rule_id, "must satisfy")

    return builder.data()


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, object]] = {}
        self.edges: list[dict[str, object]] = []
        self.edge_count = 0

    def add_node(self, node, label: str, group: str) -> str:
        node_id = _node_id(node)
        if node_id not in self.nodes:
            self.nodes[node_id] = {"id": node_id, "label": _trim(label, 44), "title": html.escape(str(label)), "group": group}
        return node_id

    def add_edge(self, source, target, label: str) -> None:
        self.edges.append({"from": _node_id(source), "to": _node_id(target), "label": _trim(label, 28)})
        self.edge_count += 1

    def data(self) -> dict[str, list[dict[str, object]]]:
        return {"nodes": list(self.nodes.values()), "edges": self.edges}


def _typed_subjects(graph: Graph) -> dict[object, set[str]]:
    typed: dict[object, set[str]] = {}
    for subject, obj in graph.subject_objects(RDF.type):
        typed.setdefault(subject, set()).add(_short(obj))
    return typed


def _labels(graph: Graph) -> dict[object, str]:
    labels = {}
    for subject, label in graph.subject_objects(RDFS.label):
        labels[subject] = str(label)
    return labels


def _ordered_subjects(graph: Graph, typed_subjects: dict[object, set[str]], labels: dict[object, str]) -> list[object]:
    subjects = set(graph.subjects())

    def score(subject) -> tuple[int, str]:
        types = typed_subjects.get(subject, set())
        label = labels.get(subject, _short(subject)).lower()
        important_type = bool(types.intersection(IMPORTANT_RAW_TYPES))
        important_label = any(word in label for word in ["door", "space", "room", "corridor", "ramp", "lift", "route"])
        return (0 if important_type or important_label else 1, label)

    return sorted(subjects, key=score)


def _keep_raw_predicate(predicate, obj) -> bool:
    pred = _short(predicate)
    if pred in {"hasElement", "hasSpace", "containsElement", "adjacentElement", "hasProperty", "hasSimpleProperty"}:
        return True
    if pred.startswith("globalId") or pred.startswith("name"):
        return True
    if isinstance(obj, Literal):
        return pred in {"value", "unit", "hasValue", "nominalValue"}
    return False


def _node_group(node, typed_subjects: dict[object, set[str]]) -> str:
    if isinstance(node, Literal):
        return "value"
    types = typed_subjects.get(node, set())
    if "Element" in types or any(text.startswith("Ifc") for text in types):
        return "element"
    if "Space" in types:
        return "space"
    return "resource"


def _literal_label(value: Literal) -> str:
    text = str(value)
    if len(text) > 60:
        return text[:57] + "..."
    return text


def _node_id(node) -> str:
    return str(node)


def _short(value) -> str:
    text = str(value)
    if "#" in text:
        return text.rsplit("#", 1)[1]
    if "/" in text:
        return text.rstrip("/").rsplit("/", 1)[1]
    return text


def _trim(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _html(raw_data: dict[str, object], enriched_data: dict[str, object], stats: dict[str, int]) -> str:
    payload = json.dumps({"raw": raw_data, "enriched": enriched_data}, ensure_ascii=False)
    stats_json = json.dumps(stats)
    return f"""
<div class="rdf-panel">
  <div class="rdf-note">
    The complete RDF data stays in the Turtle file. The force graphs below show focused views, because drawing every triple from a large IFC file would make the browser very slow.
    Use the mouse wheel to zoom. Drag empty space to pan. Drag a node to move it.
  </div>
  <h3>IFCtoLBD RDF Graph Before Geometry Enrichment</h3>
  <div id="raw-stats" class="rdf-stats"></div>
  <canvas id="raw-graph" class="rdf-canvas"></canvas>
  <h3>IFCtoLBD RDF Graph After Geometry Enrichment</h3>
  <div id="enriched-stats" class="rdf-stats"></div>
  <canvas id="enriched-graph" class="rdf-canvas"></canvas>
</div>
<style>
.rdf-panel {{
  background: #0b0f17;
  color: #edf2f7;
  font-family: Arial, sans-serif;
  padding: 14px;
}}
.rdf-note {{
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 12px;
  background: #111827;
  line-height: 1.45;
  margin-bottom: 16px;
}}
.rdf-stats {{
  color: #a8b3c7;
  margin: 0 0 8px 0;
}}
.rdf-canvas {{
  width: 100%;
  height: 620px;
  border: 1px solid #334155;
  border-radius: 8px;
  background: #080c13;
  margin-bottom: 26px;
  cursor: grab;
}}
.rdf-canvas:active {{
  cursor: grabbing;
}}
</style>
<script>
const graphPayload = {payload};
const graphStats = {stats_json};

function nodeColor(group) {{
  return {{
    element: '#67e8f9',
    space: '#a7f3d0',
    resource: '#c4b5fd',
    type: '#fcd34d',
    value: '#d1d5db',
    geometry: '#38bdf8',
    route: '#34d399',
    requirement: '#fbbf24',
    missing: '#fb7185',
    'issue-element': '#f87171'
  }}[group] || '#94a3b8';
}}

function drawGraph(canvasId, data, statText) {{
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  const stat = document.getElementById(canvasId.replace('-graph', '-stats'));
  stat.textContent = statText;
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  canvas.width = rect.width * scale;
  canvas.height = rect.height * scale;
  ctx.scale(scale, scale);
  const width = rect.width;
  const height = rect.height;
  const spread = Math.max(width, height) * 0.82;
  const nodes = data.nodes.map((node, index) => ({{
    ...node,
    x: width / 2 + Math.cos(index * 2.399) * Math.sqrt(index + 1) * spread / Math.sqrt(Math.max(data.nodes.length, 1)),
    y: height / 2 + Math.sin(index * 2.399) * Math.sqrt(index + 1) * spread / Math.sqrt(Math.max(data.nodes.length, 1)),
    vx: 0,
    vy: 0
  }}));
  const byId = new Map(nodes.map(node => [node.id, node]));
  const edges = data.edges.map(edge => ({{...edge, source: byId.get(edge.from), target: byId.get(edge.to)}})).filter(edge => edge.source && edge.target);
  let dragged = null;
  let panning = false;
  let selected = null;
  let hover = null;
  let zoom = 1;
  let panX = 0;
  let panY = 0;
  let lastPointer = null;
  let frame = 0;

  function screenToWorld(event) {{
    const box = canvas.getBoundingClientRect();
    return {{
      x: (event.clientX - box.left - panX) / zoom,
      y: (event.clientY - box.top - panY) / zoom
    }};
  }}

  function step() {{
    for (const node of nodes) {{
      node.vx *= 0.84;
      node.vy *= 0.84;
      node.vx += (width / 2 - node.x) * 0.00016;
      node.vy += (height / 2 - node.y) * 0.00016;
    }}
    for (let i = 0; i < nodes.length; i++) {{
      for (let j = i + 1; j < Math.min(nodes.length, i + 70); j++) {{
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let dist2 = dx * dx + dy * dy + 0.01;
        if (dist2 > 65000) continue;
        const force = Math.min(260 / dist2, 0.055);
        a.vx -= dx * force;
        a.vy -= dy * force;
        b.vx += dx * force;
        b.vy += dy * force;
      }}
    }}
    for (const edge of edges) {{
      const a = edge.source, b = edge.target;
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const desired = 185;
      const force = (dist - desired) * 0.0028;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }}
    for (const node of nodes) {{
      if (node !== dragged) {{
        node.x += node.vx;
        node.y += node.vy;
        node.x = Math.max(-width * 1.5, Math.min(width * 2.5, node.x));
        node.y = Math.max(-height * 1.5, Math.min(height * 2.5, node.y));
      }}
    }}
  }}

  function render() {{
    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.translate(panX, panY);
    ctx.scale(zoom, zoom);
    ctx.lineWidth = 1;
    ctx.strokeStyle = '#334155';
    ctx.fillStyle = '#94a3b8';
    ctx.font = '10px Arial';
    for (const edge of edges) {{
      ctx.beginPath();
      ctx.moveTo(edge.source.x, edge.source.y);
      ctx.lineTo(edge.target.x, edge.target.y);
      ctx.stroke();
      const mx = (edge.source.x + edge.target.x) / 2;
      const my = (edge.source.y + edge.target.y) / 2;
      const nearSelected = selected === edge.source || selected === edge.target;
      if (nearSelected || zoom >= 1.45) {{
        ctx.fillStyle = nearSelected ? '#f8fafc' : '#93a4ba';
        ctx.font = nearSelected ? '11px Arial' : '9px Arial';
        ctx.fillText(edge.label, mx + 4, my - 4);
      }}
    }}
    for (const node of nodes) {{
      const radius = node === selected ? 9 : node === hover ? 8 : 5;
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = nodeColor(node.group);
      ctx.fill();
      const important = node.group === 'issue-element' || node.group === 'requirement' || node.group === 'missing';
      if (node === selected || node === hover || important || zoom >= 1.25) {{
        ctx.fillStyle = node === selected || node === hover ? '#ffffff' : '#dbeafe';
        ctx.font = node === selected || node === hover ? '12px Arial' : '10px Arial';
        ctx.fillText(node.label, node.x + 8, node.y - 8);
      }}
      if (node === selected || node === hover) {{
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();
      }}
    }}
    ctx.restore();
    ctx.fillStyle = '#94a3b8';
    ctx.font = '12px Arial';
    ctx.fillText(`zoom: ${{zoom.toFixed(2)}}x`, 12, 22);
  }}

  function tick() {{
    frame += 1;
    if (frame < 900) {{
      for (let i = 0; i < 4; i++) step();
    }} else {{
      for (let i = 0; i < 1; i++) step();
    }}
    render();
    requestAnimationFrame(tick);
  }}

  function nearest(event) {{
    const point = screenToWorld(event);
    const x = point.x;
    const y = point.y;
    let best = null;
    let bestDist = (16 / zoom) * (16 / zoom);
    for (const node of nodes) {{
      const dx = node.x - x;
      const dy = node.y - y;
      const dist = dx * dx + dy * dy;
      if (dist < bestDist) {{
        best = node;
        bestDist = dist;
      }}
    }}
    return best;
  }}

  canvas.addEventListener('mousedown', event => {{
    dragged = nearest(event);
    selected = dragged || selected;
    panning = !dragged;
    lastPointer = {{x: event.clientX, y: event.clientY}};
  }});
  canvas.addEventListener('mousemove', event => {{
    if (dragged) {{
      const point = screenToWorld(event);
      dragged.x = point.x;
      dragged.y = point.y;
      dragged.vx = 0;
      dragged.vy = 0;
    }} else if (panning && lastPointer) {{
      panX += event.clientX - lastPointer.x;
      panY += event.clientY - lastPointer.y;
      lastPointer = {{x: event.clientX, y: event.clientY}};
    }} else {{
      hover = nearest(event);
    }}
  }});
  window.addEventListener('mouseup', () => {{
    dragged = null;
    panning = false;
    lastPointer = null;
  }});
  canvas.addEventListener('click', event => {{
    selected = nearest(event);
  }});
  canvas.addEventListener('wheel', event => {{
    event.preventDefault();
    const box = canvas.getBoundingClientRect();
    const mouseX = event.clientX - box.left;
    const mouseY = event.clientY - box.top;
    const beforeX = (mouseX - panX) / zoom;
    const beforeY = (mouseY - panY) / zoom;
    const factor = event.deltaY < 0 ? 1.12 : 0.89;
    zoom = Math.max(0.25, Math.min(4, zoom * factor));
    panX = mouseX - beforeX * zoom;
    panY = mouseY - beforeY * zoom;
  }}, {{passive: false}});
  tick();
}}

drawGraph(
  'raw-graph',
  graphPayload.raw,
  `Complete raw RDF graph: ${{graphStats.raw_triples}} triples. Visible focused view: ${{graphStats.raw_visible_nodes}} nodes and ${{graphStats.raw_visible_edges}} links.`
);
drawGraph(
  'enriched-graph',
  graphPayload.enriched,
  `Enriched RDF graph: ${{graphStats.enriched_triples}} triples. Visible geometry and rule view: ${{graphStats.enriched_visible_nodes}} nodes and ${{graphStats.enriched_visible_edges}} links.`
);
</script>
"""
