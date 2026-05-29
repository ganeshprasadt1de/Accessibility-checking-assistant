import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

let appData;
let scene;
let camera;
let renderer;
let controls;
let routeGroup;
let selectedLineMaterial;
let doorMeshes = [];
let doorMarkerGroup;
let edgeOverlayGroup;
let loadedModel;
let graphViews = [];
let graphPaused = false;
let graphsReady = false;
let viewerReady = false;
let simulationReady = false;
let simScene;
let simCamera;
let simRenderer;
let simControls;
let simWorld;
let simChair;
let simPathLine;
let simClock;
let simFloorName = "";
let simProgress = 0;
let simPlaying = true;
let simSpeed = 0.85;
let simPath = [];
let simScenarioData;
let simRouteIndex = 0;

const pages = document.querySelectorAll(".page");
document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    button.classList.add("active");
    pages.forEach((page) => page.classList.toggle("active", page.id === button.dataset.page));
    if (button.dataset.page === "model") {
      if (!viewerReady) {
        setupViewer();
        viewerReady = true;
      }
      requestAnimationFrame(resizeRenderer);
    }
    if (button.dataset.page === "visualisation") {
      if (!graphsReady) {
        renderGraphs();
        graphsReady = true;
      }
      requestAnimationFrame(() => graphViews.forEach((view) => view.resize()));
    }
    if (button.dataset.page === "simulation") {
      if (!simulationReady) {
        setupSimulation();
        simulationReady = true;
      }
      requestAnimationFrame(resizeSimulationRenderer);
    }
  });
});

init();

async function init() {
  const response = await fetch("/api/data");
  if (!response.ok) {
    document.body.innerHTML = "<main><h1>Run preprocess.py first</h1><p>The app package is missing.</p></main>";
    return;
  }
  appData = await response.json();
  renderSummary();
  renderTables();
  setupAssistant();
}

function renderSummary() {
  const items = [
    ["Elements", appData.summary.elementCount],
    ["Doors", appData.summary.doorCount],
    ["Route edges", appData.summary.routeEdgeCount],
    ["Issues", appData.summary.issueCount],
    ["Missing geometry", appData.summary.missingGeometryCount],
    ["SHACL conforms", appData.summary.shacl.conforms === true ? "yes" : appData.summary.shacl.conforms === false ? "no" : "unknown"],
  ];
  document.querySelector("#summary").innerHTML = items
    .map(([label, value]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`)
    .join("");
}

function renderTables() {
  const elementRows = appData.elements
    .filter((e) => ["IfcDoor", "IfcSpace", "IfcRamp", "IfcStair"].includes(e.ifcType))
    .slice(0, 250)
    .map((e) => [
      e.ifcType,
      e.name || e.guid,
      round(e.width),
      round(e.depth),
      round(e.height),
      e.source,
    ]);
  fillTable("#elementTable", ["Type", "Name", "Width m", "Depth m", "Height m", "Source"], elementRows);
  const issueRows = appData.issues.map((i) => [
    i.element_type,
    i.element_label,
    i.short_text,
    valueWithUnit(i.measured, i.unit),
    valueWithUnit(i.required, i.unit),
    displaySource(i.source),
  ]);
  fillTable("#issueTable", ["Type", "Element", "Issue", "Measured", "Required", "Source"], issueRows);
}

function setupAssistant() {
  const input = document.querySelector("#assistantQuestion");
  const button = document.querySelector("#assistantAsk");
  const answer = document.querySelector("#assistantAnswer");
  if (!input || !button || !answer) return;

  const ask = async () => {
    const question = input.value.trim() || "Explain the checker result.";
    answer.textContent = "Preparing explanation...";
    button.disabled = true;
    try {
      const response = await fetch("/api/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Assistant request failed.");
      answer.textContent = `${data.answer || localAssistantAnswer()} Source: ${data.source || "prepared checker facts"}.`;
    } catch {
      answer.textContent = `${localAssistantAnswer()} Source: prepared checker facts.`;
    } finally {
      button.disabled = false;
    }
  };

  button.addEventListener("click", ask);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") ask();
  });
}

function localAssistantAnswer() {
  const floors = appData.floors
    .filter((floor) => floor.doorGuids?.length || floor.routeEdgeIds?.length)
    .map((floor) => {
      const failed = floor.routeStatusCounts?.fail || 0;
      return `${floor.name}: ${floor.doorGuids?.length || 0} doors, ${floor.routeEdgeIds?.length || 0} routes, ${failed} failed`;
    })
    .join("; ");
  const issues = appData.summary.issueCount || 0;
  const failedRoutes = appData.routeEdges.filter((edge) => edge.status === "fail").length;
  const result =
    issues === 0 && failedRoutes === 0
      ? "All generated indoor routes pass the current prototype checks."
      : `The checker found ${issues} issues and ${failedRoutes} failed route edges.`;
  return `${result} It checks door width, route width, turning space, stair blockers, ramp width, and ramp slope. By floor: ${floors}.`;
}

function fillTable(selector, headers, rows) {
  const table = document.querySelector(selector);
  table.innerHTML = `<thead><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell ?? "missing")}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;
}

function renderGraphs() {
  const raw = buildRawGraphData();
  const enriched = buildEnrichedGraphData();
  document.querySelector("#rawGraphStats").textContent =
    `Raw focused view: ${raw.nodes.length} nodes and ${raw.edges.length} links. Full file: ${appData.summary.elementCount} extracted elements.`;
  document.querySelector("#enrichedGraphStats").textContent =
    `Enriched focused view: ${enriched.nodes.length} nodes and ${enriched.edges.length} links. Routes: ${appData.summary.routeEdgeCount}, issues: ${appData.summary.issueCount}.`;
  graphViews = [
    createForceGraph(document.querySelector("#rawGraph"), raw, "Raw LBD Graph"),
    createForceGraph(document.querySelector("#enrichedGraph"), enriched, "Enriched Accessibility Graph"),
  ];
  document.querySelector("#graphSearch").addEventListener("input", (event) => {
    graphViews.forEach((view) => view.search(event.target.value));
  });
  document.querySelector("#graphFit").addEventListener("click", () => {
    graphViews.forEach((view) => view.fit());
  });
  document.querySelector("#graphPause").addEventListener("click", (event) => {
    graphPaused = !graphPaused;
    event.currentTarget.textContent = graphPaused ? "Resume" : "Pause";
  });
}

function buildRawGraphData() {
  const nodes = [];
  const edges = [];
  const addNode = nodeAdder(nodes);
  const focused = [
    ...appData.elements.filter((e) => e.ifcType === "IfcSpace").slice(0, 90),
    ...appData.elements.filter((e) => e.ifcType === "IfcDoor").slice(0, 110),
    ...appData.elements.filter((e) => ["IfcRamp", "IfcStair"].includes(e.ifcType)).slice(0, 50),
  ];
  for (const element of focused) {
    addNode(element.guid, element.name || element.label, groupForElement(element), detailsForElement(element));
    const typeId = `type:${element.ifcType}`;
    addNode(typeId, element.ifcType, "type", `IFC type: ${element.ifcType}`);
    edges.push({ from: element.guid, to: typeId, label: "type" });
    const guidId = `guid:${element.guid}`;
    addNode(guidId, element.guid, "value", "Original IFC GlobalId");
    edges.push({ from: element.guid, to: guidId, label: "globalId" });
  }
  return { nodes, edges };
}

function buildEnrichedGraphData() {
  const nodes = [];
  const edges = [];
  const addNode = nodeAdder(nodes);
  const issueByGuid = new Map();
  for (const issue of appData.issues) {
    if (!issueByGuid.has(issue.element_guid)) issueByGuid.set(issue.element_guid, []);
    issueByGuid.get(issue.element_guid).push(issue);
  }
  const failedEdges = appData.routeEdges.filter((edge) => edge.status === "fail").slice(0, 45);
  const passingEdges = appData.routeEdges.filter((edge) => edge.status === "pass").slice(0, 20);
  const visibleEdges = [...failedEdges, ...passingEdges];
  const elementGuids = new Set();
  for (const edge of visibleEdges) {
    elementGuids.add(edge.startGuid);
    elementGuids.add(edge.endGuid);
  }
  for (const issue of appData.issues.slice(0, 60)) elementGuids.add(issue.element_guid);
  const byGuid = new Map(appData.elements.map((element) => [element.guid, element]));
  for (const guid of elementGuids) {
    const element = byGuid.get(guid);
    if (!element) continue;
    const group = issueByGuid.has(guid) ? "issue" : groupForElement(element);
    addNode(guid, element.name || element.label, group, detailsForElement(element));
    for (const [key, value] of Object.entries(geometryFacts(element)).slice(0, 4)) {
      if (value === null || value === undefined || value === "missing") continue;
      const valueId = `value:${guid}:${key}`;
      addNode(valueId, `${key}: ${value}`, "value", `${key} from IfcOpenShell geometry`);
      edges.push({ from: guid, to: valueId, label: key });
    }
  }
  for (const edge of visibleEdges) {
    const routeId = `route:${edge.edgeId}`;
    addNode(routeId, `${edge.edgeId} ${edge.status}`, edge.status === "fail" ? "issue" : "route", routeDetails(edge));
    edges.push({ from: routeId, to: edge.startGuid, label: "start door" });
    edges.push({ from: routeId, to: edge.endGuid, label: "end door" });
    const reasonCodes = [...new Set(edge.reasons || [])];
    for (const code of reasonCodes) {
      const reasonId = `reason:${code}`;
      addNode(reasonId, reasonText(code), "issue", reasonText(code));
      edges.push({ from: routeId, to: reasonId, label: "fails because" });
    }
  }
  for (const issue of appData.issues.slice(0, 60)) {
    const issueId = `issue:${issue.issue_id}`;
    addNode(issueId, issue.short_text, "issue", issueDetails(issue));
    edges.push({ from: issue.element_guid, to: issueId, label: "has issue" });
    const requirementId = `requirement:${issue.rule_id}:${issue.required}`;
    addNode(requirementId, `${issue.rule_id}: ${valueWithUnit(issue.required, issue.unit)}`, "requirement", "prototype rule value, not measured data");
    edges.push({ from: issueId, to: requirementId, label: "must satisfy" });
  }
  return { nodes, edges };
}

function nodeAdder(nodes) {
  const seen = new Set();
  return (id, label, group, title) => {
    if (seen.has(id)) return;
    seen.add(id);
    nodes.push({ id, label: shortText(label || id, 42), group, title: title || String(label || id) });
  };
}

function createForceGraph(canvas, data, name) {
  const ctx = canvas.getContext("2d");
  const state = {
    nodes: data.nodes.map((node, index) => ({
      ...node,
      x: Math.cos(index * 2.399) * Math.sqrt(index + 1) * 22,
      y: Math.sin(index * 2.399) * Math.sqrt(index + 1) * 22,
      vx: 0,
      vy: 0,
      pinned: false,
    })),
    edges: [],
    selected: null,
    hover: null,
    dragged: null,
    panning: false,
    lastPointer: null,
    zoom: 1,
    panX: 0,
    panY: 0,
    search: "",
    frame: 0,
    needsFit: true,
  };
  const byId = new Map(state.nodes.map((node) => [node.id, node]));
  state.edges = data.edges
    .map((edge) => ({ ...edge, source: byId.get(edge.from), target: byId.get(edge.to) }))
    .filter((edge) => edge.source && edge.target);

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) {
      state.needsFit = true;
      return;
    }
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * ratio));
    canvas.height = Math.max(1, Math.floor(rect.height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (state.needsFit || state.frame < 5) {
      fit();
      state.needsFit = false;
    }
  }

  function fit() {
    const rect = canvas.getBoundingClientRect();
    if (!state.nodes.length) return;
    runWarmup(180);
    const minX = Math.min(...state.nodes.map((node) => node.x));
    const maxX = Math.max(...state.nodes.map((node) => node.x));
    const minY = Math.min(...state.nodes.map((node) => node.y));
    const maxY = Math.max(...state.nodes.map((node) => node.y));
    const graphW = Math.max(1, maxX - minX);
    const graphH = Math.max(1, maxY - minY);
    state.zoom = Math.max(0.18, Math.min(1.35, Math.min((rect.width - 80) / graphW, (rect.height - 80) / graphH)));
    state.panX = rect.width / 2 - ((minX + maxX) / 2) * state.zoom;
    state.panY = rect.height / 2 - ((minY + maxY) / 2) * state.zoom;
  }

  function screenToWorld(event) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left - state.panX) / state.zoom,
      y: (event.clientY - rect.top - state.panY) / state.zoom,
    };
  }

  function nearest(event) {
    const point = screenToWorld(event);
    let best = null;
    let bestDist = (18 / state.zoom) ** 2;
    for (const node of state.nodes) {
      const dist = (node.x - point.x) ** 2 + (node.y - point.y) ** 2;
      if (dist < bestDist) {
        best = node;
        bestDist = dist;
      }
    }
    return best;
  }

  function step() {
    const rect = canvas.getBoundingClientRect();
    for (const node of state.nodes) {
      node.vx *= 0.82;
      node.vy *= 0.82;
      node.vx += -node.x * 0.0008;
      node.vy += -node.y * 0.0008;
    }
    for (let i = 0; i < state.nodes.length; i++) {
      const a = state.nodes[i];
      const maxJ = Math.min(state.nodes.length, i + 80);
      for (let j = i + 1; j < maxJ; j++) {
        const b = state.nodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist2 = dx * dx + dy * dy + 0.01;
        if (dist2 > 90000) continue;
        const force = Math.min(340 / dist2, 0.06);
        a.vx -= dx * force;
        a.vy -= dy * force;
        b.vx += dx * force;
        b.vy += dy * force;
      }
    }
    for (const edge of state.edges) {
      const a = edge.source;
      const b = edge.target;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const desired = edge.label.includes("route") || edge.label.includes("door") ? 115 : 82;
      const force = (dist - desired) * 0.004;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    for (const node of state.nodes) {
      if (node !== state.dragged && !node.pinned) {
        node.x += node.vx;
        node.y += node.vy;
        node.x = Math.max(-rect.width * 2, Math.min(rect.width * 2, node.x));
        node.y = Math.max(-rect.height * 2, Math.min(rect.height * 2, node.y));
      }
    }
  }

  function runWarmup(count) {
    for (let i = 0; i < count; i++) step();
  }

  function render() {
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    ctx.save();
    ctx.translate(state.panX, state.panY);
    ctx.scale(state.zoom, state.zoom);
    ctx.lineWidth = 1 / state.zoom;
    for (const edge of state.edges) {
      const active = state.selected && (edge.source === state.selected || edge.target === state.selected);
      ctx.strokeStyle = active ? "#eef6f8" : edge.source.group === "issue" || edge.target.group === "issue" ? "rgba(179, 38, 30, 0.42)" : "rgba(145, 158, 163, 0.34)";
      ctx.beginPath();
      ctx.moveTo(edge.source.x, edge.source.y);
      ctx.lineTo(edge.target.x, edge.target.y);
      ctx.stroke();
      if (active || state.zoom > 1.35) {
        ctx.fillStyle = active ? "#ffffff" : "#b9c4c8";
        ctx.font = `${Math.max(9, 10 / Math.sqrt(state.zoom))}px Arial`;
        ctx.fillText(edge.label, (edge.source.x + edge.target.x) / 2 + 4, (edge.source.y + edge.target.y) / 2 - 4);
      }
    }
    const query = state.search.trim().toLowerCase();
    for (const node of state.nodes) {
      const match = query && `${node.id} ${node.label} ${node.title}`.toLowerCase().includes(query);
      const active = node === state.selected || node === state.hover || match;
      const baseRadius = node === state.selected || node === state.hover ? 8 : node.group === "issue" ? 4.2 : 3.6;
      const radius = baseRadius / Math.sqrt(state.zoom);
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = colorForGroup(node.group);
      ctx.fill();
      if (active) {
        ctx.strokeStyle = match ? "#ffd166" : "#ffffff";
        ctx.lineWidth = (match ? 2.4 : 1.8) / state.zoom;
        ctx.stroke();
      }
      if (node === state.selected || node === state.hover || (match && state.zoom > 0.65) || (node.group === "issue" && state.zoom > 1.05) || state.zoom > 1.35) {
        ctx.fillStyle = active ? "#ffffff" : "#dce8ea";
        ctx.font = `${(node === state.selected || node === state.hover ? 12 : 10) / Math.sqrt(state.zoom)}px Arial`;
        ctx.fillText(node.label, node.x + radius + 4, node.y - radius - 3);
      }
    }
    ctx.restore();
    ctx.fillStyle = "#dce8ea";
    ctx.font = "12px Arial";
    ctx.fillText(`${name}   zoom ${state.zoom.toFixed(2)}x`, 12, 22);
  }

  function tick() {
    state.frame += 1;
    const visible = document.querySelector("#visualisation")?.classList.contains("active");
    if (visible) {
      if (!graphPaused && state.frame < 420) {
        const steps = state.frame < 160 ? 3 : 1;
        for (let i = 0; i < steps; i++) step();
      }
      render();
    }
    if (state.frame < 420 || state.dragged || state.panning) {
      requestAnimationFrame(tick);
    } else {
      window.setTimeout(tick, visible ? 180 : 700);
    }
  }

  canvas.addEventListener("mousedown", (event) => {
    state.dragged = nearest(event);
    state.selected = state.dragged || state.selected;
    state.panning = !state.dragged;
    state.lastPointer = { x: event.clientX, y: event.clientY };
    updateGraphInspector(state.selected || state.hover);
  });
  canvas.addEventListener("mousemove", (event) => {
    if (state.dragged) {
      const point = screenToWorld(event);
      state.dragged.x = point.x;
      state.dragged.y = point.y;
      state.dragged.vx = 0;
      state.dragged.vy = 0;
      render();
    } else if (state.panning && state.lastPointer) {
      state.panX += event.clientX - state.lastPointer.x;
      state.panY += event.clientY - state.lastPointer.y;
      state.lastPointer = { x: event.clientX, y: event.clientY };
      render();
    } else {
      state.hover = nearest(event);
      if (state.hover) updateGraphInspector(state.hover);
    }
  });
  canvas.addEventListener("dblclick", (event) => {
    const node = nearest(event);
    if (node) node.pinned = !node.pinned;
  });
  canvas.addEventListener("click", (event) => {
    state.selected = nearest(event);
    updateGraphInspector(state.selected);
  });
  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const mouseX = event.clientX - rect.left;
      const mouseY = event.clientY - rect.top;
      const beforeX = (mouseX - state.panX) / state.zoom;
      const beforeY = (mouseY - state.panY) / state.zoom;
      const factor = event.deltaY < 0 ? 1.12 : 0.89;
      state.zoom = Math.max(0.18, Math.min(5, state.zoom * factor));
      state.panX = mouseX - beforeX * state.zoom;
      state.panY = mouseY - beforeY * state.zoom;
      render();
    },
    { passive: false },
  );
  window.addEventListener("mouseup", () => {
    state.dragged = null;
    state.panning = false;
    state.lastPointer = null;
  });
  window.addEventListener("resize", resize);
  resize();
  tick();
  return {
    fit,
    resize,
    search(value) {
      state.search = value;
      render();
    },
  };
}

function updateGraphInspector(node) {
  const inspector = document.querySelector("#graphInspector");
  if (!inspector) return;
  if (!node) {
    inspector.textContent = "Hover or click a node.";
    return;
  }
  inspector.innerHTML = `<strong>${escapeHtml(node.label)}</strong><br>${escapeHtml(node.title || node.id)}<br><span>Group: ${escapeHtml(node.group)}</span>`;
}

function groupForElement(element) {
  if (element.ifcType === "IfcSpace") return "space";
  if (["IfcDoor", "IfcRamp", "IfcStair", "IfcWall", "IfcColumn", "IfcSlab"].includes(element.ifcType)) return "element";
  return "resource";
}

function geometryFacts(element) {
  return {
    width: round(element.width),
    depth: round(element.depth),
    height: round(element.height),
    centerX: element.center ? round(element.center[0]) : "missing",
    centerY: element.center ? round(element.center[1]) : "missing",
    centerZ: element.center ? round(element.center[2]) : "missing",
    doorWidth: element.extra?.derivedDoorWidthM ? round(element.extra.derivedDoorWidthM) : undefined,
    clearWidth: element.extra?.derivedClearSpaceWidthM ? round(element.extra.derivedClearSpaceWidthM) : undefined,
  };
}

function detailsForElement(element) {
  const facts = geometryFacts(element);
  return [
    element.label,
    `GUID: ${element.guid}`,
    `Type: ${element.ifcType}`,
    `Width/depth/height: ${facts.width} m / ${facts.depth} m / ${facts.height} m`,
    `Source: ${element.source}`,
  ].join("\n");
}

function routeDetails(edge) {
  const reasons = [...new Set(edge.reasons || [])].map(reasonText).join(", ") || "clear";
  return [
    `Route edge ${edge.edgeId}`,
    `Status: ${edge.status}`,
    `Distance: ${round(edge.distanceM)} m`,
    `Reason: ${reasons}`,
    `Source: ${edge.source}`,
  ].join("\n");
}

function issueDetails(issue) {
  return [
    issue.short_text,
    `Element: ${issue.element_label}`,
    `Rule: ${issue.rule_id}`,
    `Measured: ${valueWithUnit(issue.measured, issue.unit)}`,
    `Required: ${valueWithUnit(issue.required, issue.unit)}`,
    `Source: ${issue.source}`,
  ].join("\n");
}

function colorForGroup(group) {
  return {
    element: "#1d7f9f",
    space: "#1f8a57",
    resource: "#7b6bb8",
    type: "#d6a400",
    value: "#aeb7ba",
    route: "#9a6b00",
    requirement: "#c47b00",
    issue: "#b3261e",
  }[group] || "#87928f";
}

function setupViewer() {
  const container = document.querySelector("#viewer");
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x101518);
  camera = new THREE.PerspectiveCamera(55, 1, 0.1, 10000);
  camera.position.set(25, -35, 24);
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2.5));
  container.appendChild(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.62;
  controls.panSpeed = 0.9;
  controls.zoomSpeed = 0.9;
  controls.screenSpacePanning = true;
  controls.minDistance = 2;
  controls.maxDistance = 650;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x263034, 2));
  const light = new THREE.DirectionalLight(0xffffff, 2);
  light.position.set(20, -20, 40);
  scene.add(light);
  routeGroup = new THREE.Group();
  doorMarkerGroup = new THREE.Group();
  edgeOverlayGroup = new THREE.Group();
  scene.add(routeGroup);
  scene.add(doorMarkerGroup);
  scene.add(edgeOverlayGroup);
  selectedLineMaterial = new THREE.LineBasicMaterial({ color: 0xb3261e, linewidth: 4 });
  new GLTFLoader().load("/files/route_model.glb", (gltf) => {
    loadedModel = gltf.scene;
    scene.add(gltf.scene);
    gltf.scene.updateMatrixWorld(true);
    doorMeshes = [];
    gltf.scene.traverse((obj) => {
      if (obj.isMesh && obj.parent?.userData?.ifcType === "IfcDoor") {
        doorMeshes.push(obj);
      }
      if (obj.isMesh && obj.parent?.name) {
        obj.userData.guid = obj.parent.name;
        obj.userData.ifcType = obj.parent.userData?.ifcType || obj.parent?.userData?.ifcType;
      }
      if (obj.isMesh) {
        obj.material = obj.material.clone();
        obj.material.transparent = true;
        if (obj.userData.ifcType === "IfcDoor") {
          obj.material.color.set(0x0d8fb8);
          obj.material.opacity = 0.95;
          obj.renderOrder = 2;
        } else if (obj.userData.ifcType === "IfcSpace") {
          obj.material.opacity = 0.12;
          obj.renderOrder = 0;
        } else {
          obj.material.opacity = 0.42;
          obj.renderOrder = 1;
        }
        addEdgeOverlay(obj);
      }
    });
    addDoorMarkers();
    frameScene(gltf.scene);
  });
  renderer.domElement.addEventListener("click", onModelClick);
  window.addEventListener("resize", resizeRenderer);
  setupViewerToolbar();
  resizeRenderer();
  animate();
}

function setupViewerToolbar() {
  const modeButtons = [
    ["modeOrbit", () => setControlMode("orbit")],
    ["modePan", () => setControlMode("pan")],
    ["modeSide", () => setControlMode("side")],
  ];
  for (const [id, handler] of modeButtons) {
    document.querySelector(`#${id}`)?.addEventListener("click", () => {
      document.querySelectorAll(".segmented button").forEach((button) => button.classList.remove("active"));
      document.querySelector(`#${id}`).classList.add("active");
      handler();
    });
  }
  document.querySelector("#viewFit")?.addEventListener("click", () => loadedModel && frameScene(loadedModel));
  document.querySelector("#viewTop")?.addEventListener("click", setTopView);
  document.querySelector("#viewDoors")?.addEventListener("click", () => {
    doorMarkerGroup.visible = !doorMarkerGroup.visible;
    document.querySelector("#viewDoors").textContent = doorMarkerGroup.visible ? "Hide Doors" : "Show Doors";
  });
  document.querySelector("#toggleRouteOnly")?.addEventListener("change", (event) => setRouteFocus(event.target.checked));
  setControlMode("orbit");
}

function setControlMode(mode) {
  if (!controls) return;
  controls.enableRotate = mode !== "pan";
  controls.enablePan = mode !== "side";
  if (mode === "pan") {
    controls.mouseButtons.LEFT = THREE.MOUSE.PAN;
    controls.mouseButtons.RIGHT = THREE.MOUSE.ROTATE;
  } else {
    controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
    controls.mouseButtons.RIGHT = THREE.MOUSE.PAN;
  }
  controls.minPolarAngle = 0;
  controls.maxPolarAngle = mode === "side" ? Math.PI / 2.05 : Math.PI;
  controls.update();
}

function setTopView() {
  if (!loadedModel) return;
  const box = new THREE.Box3().setFromObject(loadedModel);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  controls.target.copy(center);
  camera.position.set(center.x, center.y, center.z + Math.max(size.x, size.y) * 1.25);
  camera.up.set(0, 1, 0);
  camera.lookAt(center);
  controls.update();
}

function setRouteFocus(enabled) {
  if (!loadedModel) return;
  loadedModel.traverse((obj) => {
    if (!obj.isMesh || !obj.material) return;
    const type = obj.userData.ifcType;
    if (type === "IfcDoor") {
      obj.material.opacity = 1;
    } else if (enabled) {
      obj.material.opacity = type === "IfcSpace" ? 0.04 : 0.18;
    } else {
      obj.material.opacity = type === "IfcSpace" ? 0.12 : 0.42;
    }
  });
}

function addEdgeOverlay(mesh) {
  if (!mesh.geometry || edgeOverlayGroup.children.length > 900) return;
  const edges = new THREE.EdgesGeometry(mesh.geometry, 35);
  const material = new THREE.LineBasicMaterial({
    color: mesh.userData.ifcType === "IfcDoor" ? 0x5ce1ff : 0xc9d1cd,
    transparent: true,
    opacity: mesh.userData.ifcType === "IfcDoor" ? 0.85 : 0.16,
  });
  const line = new THREE.LineSegments(edges, material);
  line.matrix.copy(mesh.matrixWorld);
  line.matrixAutoUpdate = false;
  edgeOverlayGroup.add(line);
}

function addDoorMarkers() {
  doorMarkerGroup.clear();
  const doorElements = appData.elements.filter((element) => element.ifcType === "IfcDoor" && element.center);
  const markerGeometry = new THREE.SphereGeometry(0.28, 20, 12);
  const markerMaterial = new THREE.MeshStandardMaterial({
    color: 0x00b7d8,
    emissive: 0x00495a,
    roughness: 0.45,
    metalness: 0.05,
    depthTest: false,
  });
  const hitGeometry = new THREE.SphereGeometry(0.95, 12, 8);
  const hitMaterial = new THREE.MeshBasicMaterial({ color: 0x00b7d8, transparent: true, opacity: 0.02, depthWrite: false });
  for (const element of doorElements) {
    const [x, y, z] = element.center;
    const marker = new THREE.Mesh(markerGeometry, markerMaterial);
    marker.position.set(x, y, z + 0.45);
    marker.userData.guid = element.guid;
    marker.userData.ifcType = "IfcDoor";
    marker.userData.label = element.label;
    marker.renderOrder = 20;
    const hit = new THREE.Mesh(hitGeometry, hitMaterial);
    hit.position.copy(marker.position);
    hit.userData.guid = element.guid;
    hit.userData.ifcType = "IfcDoor";
    hit.renderOrder = 21;
    doorMarkerGroup.add(marker);
    doorMarkerGroup.add(hit);
  }
}

function onModelClick(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  const mouse = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects([doorMarkerGroup, loadedModel].filter(Boolean), true);
  const hit = hits.find((h) => {
    const guid = h.object.userData.guid || h.object.parent?.name;
    const element = appData.elements.find((e) => e.guid === guid);
    return element?.ifcType === "IfcDoor";
  });
  if (!hit) return;
  const guid = hit.object.userData.guid || hit.object.parent.name;
  showRoutes(guid);
}

async function showRoutes(guid) {
  const element = appData.elements.find((e) => e.guid === guid);
  document.querySelector("#selectedDoor").textContent = element ? cleanElementName(element.name || element.label) : guid;
  const response = await fetch(`/api/route/${encodeURIComponent(guid)}`);
  const data = await response.json();
  routeGroup.clear();
  const byEdge = new Map(appData.routeEdges.map((e) => [e.edgeId, e]));
  const visible = data.routes;
  if (visible[0]) addRoutePath(visible[0], byEdge);
  document.querySelector("#routeList").innerHTML = visible
    .map((route) => {
      const edges = route.edge_ids.map((id) => byEdge.get(id)).filter(Boolean);
      const failed = edges.some((e) => e.status === "fail");
      const reasonCodes = [...new Set(edges.flatMap((e) => e.reasons))];
      const reason = reasonCodes.map(reasonText).join(", ") || "clear";
      const target = appData.elements.find((e) => e.guid === route.target_guid);
      return `<div class="routeItem" data-target="${escapeHtml(route.target_guid)}"><strong>${escapeHtml(cleanElementName(target?.name || target?.label || route.target_guid))}</strong><br><span class="${failed ? "fail" : "pass"}">${failed ? "failed" : "passed"}</span> ${round(route.distance_m)} m<br>${escapeHtml(reason)}</div>`;
    })
    .join("");
  document.querySelectorAll("#routeList .routeItem").forEach((item) => {
    item.addEventListener("click", () => {
      const target = item.getAttribute("data-target");
      const route = visible.find((candidate) => candidate.target_guid === target);
      if (!route) return;
      document.querySelectorAll("#routeList .routeItem").forEach((row) => row.classList.remove("active"));
      item.classList.add("active");
      routeGroup.clear();
      addRoutePath(route, byEdge);
    });
  });
  document.querySelector("#routeList .routeItem")?.classList.add("active");
}

function addRoutePath(route, byEdge) {
  const points = [];
  let failed = false;
  for (const edgeId of route.edge_ids) {
    const edge = byEdge.get(edgeId);
    if (!edge) continue;
    failed = failed || edge.status === "fail";
    for (const point of edge.path) {
      const last = points[points.length - 1];
      if (!last || last[0] !== point[0] || last[1] !== point[1] || last[2] !== point[2]) points.push(point);
    }
  }
  if (points.length < 2) return;
  const geometry = new THREE.BufferGeometry().setFromPoints(points.map((p) => new THREE.Vector3(p[0], p[1], p[2] + 0.25)));
  const material = new THREE.LineBasicMaterial({ color: failed ? 0xb3261e : 0x2d7d46, transparent: true, opacity: failed ? 0.72 : 0.44 });
  routeGroup.add(new THREE.Line(geometry, material));
}

function addRouteLine(edge) {
  const points = edge.path.map((p) => new THREE.Vector3(p[0], p[1], p[2] + 0.25));
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({ color: edge.status === "fail" ? 0xb3261e : 0x2d7d46 });
  routeGroup.add(new THREE.Line(geometry, material));
}

function frameScene(object) {
  const box = new THREE.Box3().setFromObject(object);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  controls.target.copy(center);
  camera.position.set(center.x + size.x * 0.8, center.y - size.y * 0.9, center.z + Math.max(size.z * 2, 20));
  camera.near = 0.1;
  camera.far = Math.max(size.length() * 8, 1000);
  camera.updateProjectionMatrix();
}

function resizeRenderer() {
  if (!renderer) return;
  const container = document.querySelector("#viewer");
  const width = container.clientWidth || 800;
  const height = container.clientHeight || 540;
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function animate() {
  requestAnimationFrame(animate);
  controls?.update();
  renderer?.render(scene, camera);
}

function setupSimulation() {
  const container = document.querySelector("#simulationViewer");
  simScene = new THREE.Scene();
  simScene.background = new THREE.Color(0xd8f1f0);
  const aspect = Math.max(container.clientWidth, 1) / Math.max(container.clientHeight, 1);
  simCamera = new THREE.OrthographicCamera(-12 * aspect, 12 * aspect, 12, -12, 0.1, 200);
  simCamera.position.set(13, 12, 13);
  simCamera.lookAt(0, 0, 0);
  simRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: "high-performance" });
  simRenderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2.5));
  simRenderer.shadowMap.enabled = true;
  simRenderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(simRenderer.domElement);
  simControls = new OrbitControls(simCamera, simRenderer.domElement);
  simControls.enableDamping = true;
  simControls.enablePan = false;
  simControls.enableZoom = true;
  simControls.minZoom = 0.65;
  simControls.maxZoom = 2.4;
  simControls.target.set(0, 0, 0);
  simControls.maxPolarAngle = Math.PI / 2.05;
  simScene.add(new THREE.HemisphereLight(0xffffff, 0x769096, 1.9));
  const sun = new THREE.DirectionalLight(0xffffff, 2.3);
  sun.position.set(8, 14, 7);
  sun.castShadow = true;
  sun.shadow.camera.left = -18;
  sun.shadow.camera.right = 18;
  sun.shadow.camera.top = 18;
  sun.shadow.camera.bottom = -18;
  simScene.add(sun);
  simWorld = new THREE.Group();
  simScene.add(simWorld);
  simChair = createGrandpaWheelchair();
  simScene.add(simChair);
  simClock = new THREE.Clock();
  setupFloorSelect();
  document.querySelector("#simRun")?.addEventListener("click", () => loadSimulationScenario("floor"));
  document.querySelector("#simSpeed")?.addEventListener("input", (event) => {
    simSpeed = Number(event.target.value) || 0.85;
  });
  document.querySelector("#simPlayPause")?.addEventListener("click", (event) => {
    simPlaying = !simPlaying;
    event.currentTarget.textContent = simPlaying ? "Pause" : "Play";
  });
  document.querySelector("#simReset")?.addEventListener("click", () => {
    simProgress = 0;
    simPlaying = true;
    document.querySelector("#simPlayPause").textContent = "Pause";
    updateSimulationStatus(false);
  });
  window.addEventListener("resize", resizeSimulationRenderer);
  loadSimulationScenario("floor");
  resizeSimulationRenderer();
  animateSimulation();
}

function loadSimulationScenario(name) {
  simProgress = 0;
  simRouteIndex = 0;
  simPlaying = true;
  document.querySelector("#simPlayPause").textContent = "Pause";
  simWorld.clear();
  simScenarioData = buildFloorScenario();
  simPath = currentSimulationPath();
  addIsometricBase(simWorld);
  simScenarioData.build(simWorld);
  simPathLine = addSimulationPath(simWorld, simPath, simScenarioData.fail ? 0xb3261e : 0x2d7d46);
  updateSimulationPanel();
  updateChairPose(0, 0);
}

function setupFloorSelect() {
  const select = document.querySelector("#floorSelect");
  if (!select) return;
  const floors = appData.floors || [];
  select.innerHTML = floors
    .filter((floor) => floor.doorGuids?.length)
    .map((floor) => `<option value="${escapeHtml(floor.name)}">${escapeHtml(floor.name)} (${floor.doorGuids.length} doors)</option>`)
    .join("");
  const entranceFloor = guessEntranceFloor();
  simFloorName = entranceFloor || floors.find((floor) => floor.doorGuids?.length)?.name || "";
  select.value = simFloorName;
  select.addEventListener("change", () => {
    simFloorName = select.value;
    loadSimulationScenario("floor");
  });
}

function guessEntranceFloor() {
  const floors = appData.floors || [];
  const doorGuids = new Set(appData.elements.filter((element) => element.ifcType === "IfcDoor").map((element) => element.guid));
  const boundaryCounts = new Map();
  for (const edge of appData.routeEdges || []) {
    boundaryCounts.set(edge.startGuid, (boundaryCounts.get(edge.startGuid) || 0) + 1);
    boundaryCounts.set(edge.endGuid, (boundaryCounts.get(edge.endGuid) || 0) + 1);
  }
  const entrance = appData.elements
    .filter((element) => doorGuids.has(element.guid) && element.center)
    .sort((a, b) => Math.abs(a.center[2] - 1) - Math.abs(b.center[2] - 1) || (boundaryCounts.get(a.guid) || 0) - (boundaryCounts.get(b.guid) || 0))[0];
  return floors.find((floor) => floor.doorGuids?.includes(entrance?.guid))?.name;
}

function buildFloorScenario() {
  const floors = appData.floors || [];
  const floor = floors.find((item) => item.name === simFloorName) || floors.find((item) => item.doorGuids?.length) || { name: "Floor", doorGuids: [], routeEdgeIds: [] };
  simFloorName = floor.name;
  const select = document.querySelector("#floorSelect");
  if (select && select.value !== simFloorName) select.value = simFloorName;
  const elementsByGuid = new Map(appData.elements.map((element) => [element.guid, element]));
  const floorElements = (floor.elementGuids || []).map((guid) => elementsByGuid.get(guid)).filter(Boolean);
  const floorDoors = (floor.doorGuids || []).map((guid) => elementsByGuid.get(guid)).filter(Boolean);
  const floorEdges = (floor.routeEdgeIds || [])
    .map((edgeId) => appData.routeEdges.find((edge) => edge.edgeId === edgeId))
    .filter(Boolean);
  const passEdges = floorEdges.filter((edge) => edge.status === "pass");
  const failedEdges = floorEdges.filter((edge) => edge.status === "fail");
  const selectedEdges = [...passEdges.slice(0, 8), ...failedEdges.slice(0, 1)];
  const chosenEdge = selectedEdges[0] || floorEdges[0];
  const transform = createFloorTransform(floorElements);
  const routePaths = selectedEdges
    .filter((edge) => edge.path?.length)
    .map((edge) => ({
      edgeId: edge.edgeId,
      status: edge.status,
      reason: edge.reasons?.[0] ? reasonText(edge.reasons[0]) : "clear",
      path: edge.path.map((point) => transform.point(point)),
    }));
  const path = routePaths[0]?.path || [new THREE.Vector3(-5, 0.08, 0), new THREE.Vector3(5, 0.08, 0)];
  const reasonCounts = countReasons(floorEdges);
  const floorFailReason = topReasonText(reasonCounts);
  return {
    title: `${floor.name} indoor check`,
    status: chosenEdge
      ? failedEdges.length
        ? `Running passing routes first. It stops only when it reaches a failed route: ${floorFailReason}.`
        : "All generated routes on this floor pass the prototype checks."
      : "No door-to-door route edges were generated for this floor.",
    source: "Prototype indoor rules: door width, route width, turning space, stair blockers, ramp width and slope.",
    fail: Boolean(chosenEdge && chosenEdge.status === "fail"),
    blockAt: chosenEdge?.status === "fail" ? 0.72 : 1,
    routePaths,
    path,
    metrics: [
      ["Doors on floor", String(floorDoors.length), floorDoors.length ? "pass" : "fail"],
      ["Route edges", String(floorEdges.length), floorEdges.length ? "pass" : "fail"],
      ["Failed edges", String(failedEdges.length), failedEdges.length ? "fail" : "pass"],
      ["Main reason", topReasonText(reasonCounts), failedEdges.length ? "fail" : "pass"],
    ],
    build(group) {
      addFloorBase(group, floorElements, transform);
      addFloorSpaces(group, floorElements, transform);
      addFloorRouteLines(group, floorEdges, transform);
      addFloorDoors(group, floorDoors, floorEdges, transform);
      addFloorBlockers(group, floorElements, transform);
    },
  };
}

function createFloorTransform(elements) {
  const boxes = elements.filter((element) => element.bboxMin && element.bboxMax);
  if (!boxes.length) {
    return {
      point(point) {
        return new THREE.Vector3(point[0], 0.08, point[1]);
      },
      box(element) {
        return { center: new THREE.Vector3(0, 0, 0), size: new THREE.Vector3(1, 1, 1) };
      },
    };
  }
  const minX = Math.min(...boxes.map((element) => element.bboxMin[0]));
  const maxX = Math.max(...boxes.map((element) => element.bboxMax[0]));
  const minY = Math.min(...boxes.map((element) => element.bboxMin[1]));
  const maxY = Math.max(...boxes.map((element) => element.bboxMax[1]));
  const minZ = Math.min(...boxes.map((element) => element.bboxMin[2]));
  const span = Math.max(maxX - minX, maxY - minY, 1);
  const scale = Math.min(13.5 / span, 0.85);
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  return {
    point(point) {
      return new THREE.Vector3((point[0] - centerX) * scale, 0.08 + Math.max(0, point[2] - minZ) * scale, (point[1] - centerY) * scale);
    },
    box(element, flatHeight = false) {
      const mn = element.bboxMin;
      const mx = element.bboxMax;
      const center = this.point([(mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, minZ]);
      const sx = Math.max((mx[0] - mn[0]) * scale, 0.04);
      const sz = Math.max((mx[1] - mn[1]) * scale, 0.04);
      const sy = flatHeight ? 0.05 : Math.max(Math.min((mx[2] - mn[2]) * scale, 1.6), 0.12);
      center.y = sy / 2;
      return { center, size: new THREE.Vector3(sx, sy, sz) };
    },
  };
}

function addFloorBase(group, elements, transform) {
  const boxes = elements.filter((element) => element.bboxMin && element.bboxMax);
  if (!boxes.length) {
    addIsometricBase(group);
    return;
  }
  const slabElements = boxes.filter((element) => element.ifcType === "IfcSpace");
  const floorBox = slabElements.length ? combinedFloorBox(slabElements, transform, true) : combinedFloorBox(boxes, transform, true);
  addBox(group, [floorBox.center.x, -0.08, floorBox.center.z], [floorBox.size.x + 1.1, 0.16, floorBox.size.z + 1.1], 0xefc58e);
  addBox(group, [floorBox.center.x, -0.28, floorBox.center.z + floorBox.size.z / 2 + 0.68], [floorBox.size.x + 1.25, 0.4, 0.24], 0x0c6f68);
  addBox(group, [floorBox.center.x + floorBox.size.x / 2 + 0.68, -0.28, floorBox.center.z], [0.24, 0.4, floorBox.size.z + 1.25], 0x0c6f68);
  const grid = new THREE.GridHelper(Math.max(floorBox.size.x, floorBox.size.z) + 1.4, 12, 0xffffff, 0xd99e74);
  grid.position.set(floorBox.center.x, 0.012, floorBox.center.z);
  grid.material.opacity = 0.22;
  grid.material.transparent = true;
  group.add(grid);
}

function addFloorSpaces(group, elements, transform) {
  for (const element of elements.filter((item) => item.ifcType === "IfcSpace" && item.bboxMin && item.bboxMax)) {
    const box = transform.box(element, true);
    addBox(group, [box.center.x, 0.018, box.center.z], [box.size.x, 0.035, box.size.z], element.extra?.derivedClearSpaceWidthM < 1.5 ? 0xf0b35e : 0xf4d09a);
  }
  for (const element of elements.filter((item) => ["IfcWall", "IfcColumn"].includes(item.ifcType) && item.bboxMin && item.bboxMax)) {
    const box = transform.box(element);
    if (box.size.y < 0.25) continue;
    addBox(group, [box.center.x, Math.min(box.size.y, 1.35) / 2, box.center.z], [box.size.x, Math.min(box.size.y, 1.35), box.size.z], 0x20a48f);
  }
}

function addFloorDoors(group, doors, edges, transform) {
  for (const door of doors.filter((item) => item.center)) {
    const point = transform.point(door.center);
    const fail = door.extra?.derivedDoorWidthM < 0.9;
    addBox(group, [point.x, 0.18, point.z], [0.24, 0.36, 0.24], fail ? 0xb3261e : 0x2d7d46);
  }
}

function addFloorRouteLines(group, edges, transform) {
  for (const edge of edges) {
    if (!edge.path?.length) continue;
    const points = edge.path.map((point) => transform.point(point).add(new THREE.Vector3(0, 0.045, 0)));
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const material = new THREE.LineBasicMaterial({
      color: 0x7a9a93,
      transparent: true,
      opacity: 0.14,
    });
    group.add(new THREE.Line(geometry, material));
  }
}

function addFloorBlockers(group, elements, transform) {
  for (const element of elements.filter((item) => ["IfcStair", "IfcRamp"].includes(item.ifcType) && item.bboxMin && item.bboxMax)) {
    const box = transform.box(element);
    if (element.ifcType === "IfcStair") {
      addBox(group, [box.center.x, 0.11, box.center.z], [box.size.x, 0.22, box.size.z], 0xb3261e);
      addStairStripes(group, box);
      addLabelBoard(group, "stair blocker", [box.center.x, 1.05, box.center.z], 0xb3261e);
    } else {
      addBox(group, [box.center.x, Math.max(0.12, box.size.y / 2), box.center.z], [box.size.x, Math.max(0.24, box.size.y), box.size.z], 0xc47b00);
      addLabelBoard(group, "ramp check", [box.center.x, 2.2, box.center.z], 0xc47b00);
    }
  }
}

function addStairStripes(group, box) {
  const count = Math.max(3, Math.min(8, Math.floor(Math.max(box.size.x, box.size.z) / 0.28)));
  const alongX = box.size.x >= box.size.z;
  for (let i = 0; i < count; i++) {
    const offset = -0.42 + (i / Math.max(count - 1, 1)) * 0.84;
    const x = box.center.x + (alongX ? offset * box.size.x : 0);
    const z = box.center.z + (alongX ? 0 : offset * box.size.z);
    addBox(group, [x, 0.255, z], [alongX ? 0.035 : box.size.x * 0.92, 0.05, alongX ? box.size.z * 0.92 : 0.035], 0xffffff);
  }
}

function combinedFloorBox(elements, transform) {
  const boxes = elements.map((element) => transform.box(element, true));
  const minX = Math.min(...boxes.map((box) => box.center.x - box.size.x / 2));
  const maxX = Math.max(...boxes.map((box) => box.center.x + box.size.x / 2));
  const minZ = Math.min(...boxes.map((box) => box.center.z - box.size.z / 2));
  const maxZ = Math.max(...boxes.map((box) => box.center.z + box.size.z / 2));
  return {
    center: new THREE.Vector3((minX + maxX) / 2, 0, (minZ + maxZ) / 2),
    size: new THREE.Vector3(maxX - minX, 0.1, maxZ - minZ),
  };
}

function countReasons(edges) {
  const counts = {};
  for (const edge of edges) {
    for (const reason of edge.reasons || []) {
      counts[reason] = (counts[reason] || 0) + 1;
    }
  }
  return counts;
}

function topReasonText(counts) {
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  return top ? `${reasonText(top[0])} (${top[1]})` : "none";
}

function addIsometricBase(group) {
  addBox(group, [0, -0.08, 0], [16, 0.16, 10], 0xefc58e);
  addBox(group, [0, -0.28, 5.14], [16.3, 0.4, 0.28], 0x0c6f68);
  addBox(group, [8.14, -0.28, 0], [0.28, 0.4, 10.3], 0x0c6f68);
  const grid = new THREE.GridHelper(15.6, 12, 0xffffff, 0xd99e74);
  grid.position.y = 0.012;
  grid.material.opacity = 0.28;
  grid.material.transparent = true;
  group.add(grid);
}

function addSimulationPath(group, points, color) {
  const curvePoints = samplePolyline(points, 80);
  const geometry = new THREE.BufferGeometry().setFromPoints(curvePoints.map((point) => point.clone().add(new THREE.Vector3(0, 0.04, 0))));
  const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.86 }));
  group.add(line);
  for (const point of points) {
    addCylinder(group, [point.x, point.y + 0.02, point.z], 0.12, 0.06, color);
  }
  return line;
}

function createGrandpaWheelchair() {
  const group = new THREE.Group();
  const wheelMat = new THREE.MeshStandardMaterial({ color: 0x2c3437, roughness: 0.55 });
  const rimMat = new THREE.MeshStandardMaterial({ color: 0xdde6e4, metalness: 0.25, roughness: 0.35 });
  const chairMat = new THREE.MeshStandardMaterial({ color: 0x276c91, roughness: 0.45 });
  const skinMat = new THREE.MeshStandardMaterial({ color: 0xf1b487, roughness: 0.7 });
  const shirtMat = new THREE.MeshStandardMaterial({ color: 0x7f5bb0, roughness: 0.5 });
  const hairMat = new THREE.MeshStandardMaterial({ color: 0xf5f1df, roughness: 0.9 });

  const leftWheel = new THREE.Mesh(new THREE.TorusGeometry(0.54, 0.055, 12, 36), wheelMat);
  leftWheel.position.set(0, 0.58, -0.46);
  const rightWheel = leftWheel.clone();
  rightWheel.position.z = 0.46;
  group.add(leftWheel, rightWheel);
  group.userData.wheels = [leftWheel, rightWheel];
  const leftRim = new THREE.Mesh(new THREE.TorusGeometry(0.36, 0.025, 8, 28), rimMat);
  leftRim.position.copy(leftWheel.position);
  const rightRim = leftRim.clone();
  rightRim.position.copy(rightWheel.position);
  group.add(leftRim, rightRim);

  addPart(group, "box", [0.08, 0.88, 0], [0.9, 0.18, 0.78], chairMat);
  addPart(group, "box", [-0.28, 1.3, 0], [0.18, 0.82, 0.78], chairMat);
  addPart(group, "box", [0.62, 0.76, -0.34], [0.5, 0.08, 0.08], rimMat);
  addPart(group, "box", [0.62, 0.76, 0.34], [0.5, 0.08, 0.08], rimMat);
  addPart(group, "box", [0.82, 0.42, -0.25], [0.38, 0.07, 0.08], rimMat);
  addPart(group, "box", [0.82, 0.42, 0.25], [0.38, 0.07, 0.08], rimMat);
  addPart(group, "sphere", [1.03, 0.38, -0.25], [0.13, 0.13, 0.13], wheelMat);
  addPart(group, "sphere", [1.03, 0.38, 0.25], [0.13, 0.13, 0.13], wheelMat);

  addPart(group, "box", [0.12, 1.45, 0], [0.46, 0.62, 0.46], shirtMat);
  addPart(group, "sphere", [0.14, 2.0, 0], [0.28, 0.3, 0.28], skinMat);
  addPart(group, "sphere", [0.02, 2.2, 0], [0.26, 0.12, 0.24], hairMat);
  addPart(group, "box", [0.46, 1.48, -0.42], [0.12, 0.45, 0.1], skinMat).rotation.z = -0.45;
  addPart(group, "box", [0.46, 1.48, 0.42], [0.12, 0.45, 0.1], skinMat).rotation.z = -0.45;
  addPart(group, "sphere", [0.41, 2.02, -0.12], [0.035, 0.035, 0.035], wheelMat);
  addPart(group, "sphere", [0.41, 2.02, 0.12], [0.035, 0.035, 0.035], wheelMat);
  group.scale.setScalar(0.58);
  return group;
}

function addPart(group, kind, position, scale, material) {
  const geometry = kind === "sphere" ? new THREE.SphereGeometry(1, 20, 14) : new THREE.BoxGeometry(1, 1, 1);
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(position[0], position[1], position[2]);
  mesh.scale.set(scale[0], scale[1], scale[2]);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  return mesh;
}

function addBox(group, position, scale, color) {
  const wallColors = new Set([0x18a68f, 0x0f7f73, 0x25a18e, 0x51c8b3, 0x45c8b2, 0x1f9b86, 0x31a391]);
  const isWall = wallColors.has(color) && scale[1] > 1.5;
  const mesh = new THREE.Mesh(
    new THREE.BoxGeometry(scale[0], scale[1], scale[2]),
    new THREE.MeshStandardMaterial({ color, roughness: 0.62, metalness: 0.02, transparent: isWall, opacity: isWall ? 0.84 : 1 }),
  );
  mesh.position.set(position[0], position[1], position[2]);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  return mesh;
}

function addCylinder(group, position, radius, height, color) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, height, 24),
    new THREE.MeshStandardMaterial({ color, roughness: 0.55 }),
  );
  mesh.position.set(position[0], position[1], position[2]);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  group.add(mesh);
  return mesh;
}

function addLabelBoard(group, text, position, color) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 96;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = `#${color.toString(16).padStart(6, "0")}`;
  ctx.fillRect(0, 0, 12, canvas.height);
  ctx.fillStyle = "#1f2628";
  ctx.font = "bold 28px Arial";
  ctx.fillText(text, 28, 58);
  const texture = new THREE.CanvasTexture(canvas);
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(2.4, 0.9),
    new THREE.MeshBasicMaterial({ map: texture, transparent: true, side: THREE.DoubleSide }),
  );
  mesh.position.set(position[0], position[1], position[2]);
  mesh.rotation.y = -Math.PI / 4;
  group.add(mesh);
}

function animateSimulation() {
  requestAnimationFrame(animateSimulation);
  if (!simRenderer || !simClock) return;
  const delta = Math.min(simClock.getDelta(), 0.05);
  const route = currentSimulationRoute();
  const routeFails = route?.status === "fail";
  const blockAt = routeFails ? simScenarioData?.blockAt || 0.72 : 1;
  const blocked = routeFails && simProgress >= blockAt;
  if (simPlaying && !blocked) {
    simProgress += delta * simSpeed * 0.085;
    if (simProgress > 1) {
      advanceSimulationRoute();
    }
  }
  updateChairPose(simProgress, delta);
  updateSimulationStatus(blocked);
  simControls?.update();
  simRenderer.render(simScene, simCamera);
}

function currentSimulationRoute() {
  return simScenarioData?.routePaths?.[simRouteIndex] || null;
}

function currentSimulationPath() {
  return currentSimulationRoute()?.path || simScenarioData?.path || [];
}

function advanceSimulationRoute() {
  const routes = simScenarioData?.routePaths || [];
  if (!routes.length) {
    simProgress = 0;
    return;
  }
  simRouteIndex = (simRouteIndex + 1) % routes.length;
  simProgress = 0;
  simPath = currentSimulationPath();
  if (simPathLine) {
    simWorld.remove(simPathLine);
    simPathLine.geometry?.dispose?.();
    simPathLine.material?.dispose?.();
  }
  const route = currentSimulationRoute();
  simPathLine = addSimulationPath(simWorld, simPath, route?.status === "fail" ? 0xb3261e : 0x2d7d46);
}

function updateChairPose(progress, delta) {
  if (!simChair || !simPath.length) return;
  const eased = Math.min(progress, simScenarioData?.blockAt || 1);
  const point = pointOnPolyline(simPath, eased);
  const ahead = pointOnPolyline(simPath, Math.min(eased + 0.01, 1));
  simChair.position.copy(point);
  const dx = ahead.x - point.x;
  const dz = ahead.z - point.z;
  if (Math.abs(dx) + Math.abs(dz) > 0.001) {
    simChair.rotation.y = -Math.atan2(dz, dx);
  }
  const wheelTurn = (delta || 0.016) * simSpeed * 8;
  for (const wheel of simChair.userData.wheels || []) {
    wheel.rotation.z -= wheelTurn;
  }
}

function pointOnPolyline(points, progress) {
  if (points.length === 1) return points[0].clone();
  const segments = [];
  let total = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const length = points[i].distanceTo(points[i + 1]);
    segments.push(length);
    total += length;
  }
  let target = total * Math.max(0, Math.min(1, progress));
  for (let i = 0; i < segments.length; i++) {
    if (target <= segments[i]) {
      return points[i].clone().lerp(points[i + 1], target / Math.max(segments[i], 0.0001));
    }
    target -= segments[i];
  }
  return points[points.length - 1].clone();
}

function samplePolyline(points, count) {
  return Array.from({ length: count }, (_, index) => pointOnPolyline(points, index / Math.max(count - 1, 1)));
}

function updateSimulationPanel() {
  document.querySelector("#simTitle").textContent = simScenarioData.title;
  document.querySelector("#simStatus").textContent = simScenarioData.status;
  document.querySelector("#simSource").textContent = simScenarioData.source;
  document.querySelector("#simMetrics").innerHTML = simScenarioData.metrics
    .map(([label, value, status]) => `<div class="simMetric ${status}"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`)
    .join("");
}

function updateSimulationStatus(blocked) {
  if (!simScenarioData) return;
  const route = currentSimulationRoute();
  const prefix = route ? `Route ${route.edgeId}: ${route.reason}. ` : "";
  const status = blocked ? `${prefix}Blocked here. Reset or choose another floor to continue.` : `${prefix}${simScenarioData.status}`;
  document.querySelector("#simStatus").textContent = status;
}

function resizeSimulationRenderer() {
  if (!simRenderer || !simCamera) return;
  const container = document.querySelector("#simulationViewer");
  const width = container.clientWidth || 900;
  const height = container.clientHeight || 560;
  const aspect = width / height;
  simCamera.left = -12 * aspect;
  simCamera.right = 12 * aspect;
  simCamera.top = 12;
  simCamera.bottom = -12;
  simCamera.updateProjectionMatrix();
  simRenderer.setSize(width, height, false);
}

function round(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "missing";
}

function valueWithUnit(value, unit) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} ${unit}` : "missing";
}

function displaySource(value) {
  return {
    "IfcOpenShell geometry": "IFC model geometry",
    "IfcOpenShell": "IFC model data",
  }[value] || value;
}

function shortLabel(node) {
  const text = node.name || node.label || node.guid;
  return text.length > 22 ? `${text.slice(0, 20)}...` : text;
}

function shortText(value, limit) {
  const text = String(value ?? "");
  return text.length <= limit ? text : `${text.slice(0, limit - 3)}...`;
}

function cleanElementName(value) {
  let text = String(value || "");
  text = text.replace(/^IfcDoor\s+/i, "");
  const parts = text.split(":").map((part) => part.trim()).filter(Boolean);
  if (parts.length > 1) {
    const unique = [];
    for (const part of parts) {
      if (!unique.includes(part)) unique.push(part);
    }
    text = unique.join(" ");
  }
  text = text.replace(/\bM_/g, "").replace(/_/g, " ");
  return shortText(text, 58);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[ch]);
}

function reasonText(code) {
  return {
    door_width: "door too narrow",
    corridor_width: "corridor too narrow",
    route_width: "route too narrow",
    turning_space: "turning space too small",
    stair_block: "stair blocks route",
    ramp_slope: "ramp too steep",
    ramp_width: "ramp too narrow",
    missing: "data is missing",
    unreachable: "route not connected",
  }[code] || "access issue found";
}
