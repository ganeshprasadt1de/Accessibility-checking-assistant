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
let simBlockedHold = 0;
let planFloorName = "";
let planRouteMode = "issues";
let planZoom = 1;
let planDrag = null;
let planSuppressClick = false;
let planClickTarget = null;
let planPick = null;

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
  setupFloorPlan();
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
    .filter((e) => ["IfcDoor", "IfcSpace", "IfcRamp", "IfcRampFlight", "IfcStair", "IfcStairFlight", "IfcTransportElement"].includes(e.ifcType))
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

function setupFloorPlan() {
  const select = document.querySelector("#planFloorSelect");
  if (!select) return;
  const routeMode = document.querySelector("#planRouteMode");
  const floors = (appData.floors || []).filter((floor) => floor.elementGuids?.length);
  select.innerHTML = floors
    .map((floor) => `<option value="${escapeHtml(floor.name)}">${escapeHtml(floor.name)} (${floor.spaceGuids?.length || 0} spaces)</option>`)
    .join("");
  planFloorName = guessEntranceFloor() || floors.find((floor) => floor.routeEdgeIds?.length)?.name || floors[0]?.name || "";
  select.value = planFloorName;
  select.addEventListener("change", () => {
    planFloorName = select.value;
    renderFloorPlan();
  });
  routeMode?.addEventListener("change", () => {
    planRouteMode = routeMode.value;
    renderFloorPlan();
  });
  document.querySelector("#planResetView")?.addEventListener("click", resetPlanView);
  setupPlanPanZoom();
  renderFloorPlan();
}

function renderFloorPlan() {
  const viewer = document.querySelector("#planViewer");
  const title = document.querySelector("#planTitle");
  const status = document.querySelector("#planStatus");
  const metrics = document.querySelector("#planMetrics");
  const details = document.querySelector("#planDetails");
  if (!viewer || !title || !status || !metrics || !details) return;
  const floors = appData.floors || [];
  const floor = floors.find((item) => item.name === planFloorName) || floors.find((item) => item.elementGuids?.length);
  if (!floor) {
    viewer.innerHTML = "<p class=\"emptyPlan\">No floor data was generated.</p>";
    title.textContent = "Floor plan";
    status.textContent = "No floor data was generated.";
    metrics.innerHTML = "";
    details.innerHTML = "";
    return;
  }
  planFloorName = floor.name;
  const select = document.querySelector("#planFloorSelect");
  if (select && select.value !== planFloorName) select.value = planFloorName;
  const elementsByGuid = new Map(appData.elements.map((element) => [element.guid, element]));
  const edgesById = new Map(appData.routeEdges.map((edge) => [edge.edgeId, edge]));
  const issueCounts = planIssueCounts();
  const elements = (floor.elementGuids || []).map((guid) => elementsByGuid.get(guid)).filter(Boolean);
  const edges = (floor.routeEdgeIds || []).map((edgeId) => edgesById.get(edgeId)).filter(Boolean);
  viewer.innerHTML = floorPlanSvg(floor, elements, edges, issueCounts);
  planPick = null;
  applyPlanZoom();
  title.textContent = `${floor.name} floor plan`;
  const failedRoutes = edges.filter((edge) => edge.status === "fail").length;
  status.textContent = `${elements.length} elements, ${edges.length} route edges, ${failedRoutes} blocked route edges.`;
  metrics.innerHTML = [
    ["Spaces", String(floor.spaceGuids?.length || 0), floor.spaceGuids?.length ? "pass" : "fail"],
    ["Doors", String(floor.doorGuids?.length || 0), floor.doorGuids?.length ? "pass" : "fail"],
    ["Route edges", String(edges.length), edges.length ? "pass" : "fail"],
    ["Issues", String(floor.issueCount || 0), floor.issueCount ? "fail" : "pass"],
  ]
    .map(([label, value, state]) => `<div class="simMetric ${state}"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`)
    .join("");
  details.innerHTML = planRouteMode === "issues" ? "<p>Click a room, door, stair, or ramp to inspect it. Use the Routes control to overlay route lines.</p>" : "<p>Click a room, door, stair, ramp, or route line to inspect it.</p>";
  viewer.onclick = (event) => {
    if (planSuppressClick) {
      planSuppressClick = false;
      planClickTarget = null;
      return;
    }
    const directTarget = event.target instanceof Element ? event.target.closest("[data-guid], [data-edge-id]") : null;
    const target = pickPlanTarget(event, directTarget || planClickTarget, elementsByGuid, edgesById, issueCounts);
    planClickTarget = null;
    if (!target) return;
    const guid = target.getAttribute("data-guid");
    const edgeId = target.getAttribute("data-edge-id");
    selectPlanTarget(target);
    if (guid) showPlanElement(guid, elementsByGuid, issueCounts);
    if (edgeId) showPlanRoute(edgeId, edgesById, elementsByGuid);
  };
}

function setPlanZoom(value) {
  planZoom = Math.max(0.7, Math.min(3.5, value));
  applyPlanZoom();
}

function applyPlanZoom() {
  const svg = document.querySelector("#planViewer svg");
  if (!svg) return;
  svg.style.width = `${100 * planZoom}%`;
  svg.style.height = `${100 * planZoom}%`;
  svg.style.minWidth = `${760 * planZoom}px`;
  svg.style.minHeight = `${560 * planZoom}px`;
}

function resetPlanView() {
  planZoom = 1;
  applyPlanZoom();
  const viewer = document.querySelector("#planViewer");
  if (!viewer) return;
  viewer.scrollLeft = 0;
  viewer.scrollTop = 0;
}

function setupPlanPanZoom() {
  const viewer = document.querySelector("#planViewer");
  if (!viewer) return;
  viewer.addEventListener("wheel", (event) => {
    if (!viewer.querySelector("svg")) return;
    event.preventDefault();
    const rect = viewer.getBoundingClientRect();
    const oldZoom = planZoom;
    const nextZoom = planZoom * (event.deltaY < 0 ? 1.12 : 0.89);
    setPlanZoom(nextZoom);
    const ratio = planZoom / oldZoom;
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    viewer.scrollLeft = (viewer.scrollLeft + x) * ratio - x;
    viewer.scrollTop = (viewer.scrollTop + y) * ratio - y;
  }, { passive: false });
  viewer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const target = event.target instanceof Element ? event.target.closest("[data-guid], [data-edge-id]") : null;
    planDrag = {
      x: event.clientX,
      y: event.clientY,
      left: viewer.scrollLeft,
      top: viewer.scrollTop,
      moved: false,
      target,
      pointerId: event.pointerId,
    };
    viewer.setPointerCapture(event.pointerId);
  });
  viewer.addEventListener("pointermove", (event) => {
    if (!planDrag) return;
    if ((event.buttons & 1) !== 1) {
      planDrag = null;
      return;
    }
    const dx = event.clientX - planDrag.x;
    const dy = event.clientY - planDrag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) {
      planDrag.moved = true;
    }
    viewer.scrollLeft = planDrag.left - (event.clientX - planDrag.x);
    viewer.scrollTop = planDrag.top - (event.clientY - planDrag.y);
  });
  viewer.addEventListener("pointerup", (event) => {
    if (planDrag?.moved) {
      planSuppressClick = true;
    } else {
      planClickTarget = planDrag?.target || null;
    }
    if (viewer.hasPointerCapture(event.pointerId)) {
      viewer.releasePointerCapture(event.pointerId);
    }
    planDrag = null;
  });
  viewer.addEventListener("pointercancel", (event) => {
    if (viewer.hasPointerCapture(event.pointerId)) {
      viewer.releasePointerCapture(event.pointerId);
    }
    planDrag = null;
  });
}

function selectPlanTarget(target) {
  document.querySelectorAll("#planViewer .selectedPlanElement").forEach((item) => item.classList.remove("selectedPlanElement"));
  document.querySelectorAll("#planViewer .selectedPlanRoute").forEach((item) => item.classList.remove("selectedPlanRoute"));
  if (target.hasAttribute("data-edge-id")) {
    target.classList.add("selectedPlanRoute");
    return;
  }
  target.classList.add("selectedPlanElement");
}

function pickPlanTarget(event, fallback, elementsByGuid, edgesById, issueCounts) {
  const candidates = planTargetCandidates(event, fallback, elementsByGuid, edgesById, issueCounts);
  if (!candidates.length) return null;
  const x = Math.round(event.clientX);
  const y = Math.round(event.clientY);
  const key = candidates.map(planTargetKey).join("|");
  const samePick = planPick && planPick.key === key && Math.abs(planPick.x - x) <= 4 && Math.abs(planPick.y - y) <= 4;
  const index = samePick ? (planPick.index + 1) % candidates.length : 0;
  planPick = { x, y, key, index };
  return candidates[index];
}

function planTargetCandidates(event, fallback, elementsByGuid, edgesById, issueCounts) {
  const viewer = document.querySelector("#planViewer");
  if (!viewer) return [];
  const candidates = [];
  for (const item of document.elementsFromPoint(event.clientX, event.clientY)) {
    const target = item instanceof Element ? item.closest("[data-guid], [data-edge-id]") : null;
    if (!target || !viewer.contains(target)) continue;
    if (!candidates.some((candidate) => planTargetKey(candidate) === planTargetKey(target))) {
      candidates.push(target);
    }
  }
  if (fallback && viewer.contains(fallback) && !candidates.some((candidate) => planTargetKey(candidate) === planTargetKey(fallback))) {
    candidates.push(fallback);
  }
  return candidates.sort((a, b) => planTargetPriority(a, elementsByGuid, edgesById, issueCounts) - planTargetPriority(b, elementsByGuid, edgesById, issueCounts));
}

function planTargetKey(target) {
  const edgeId = target.getAttribute("data-edge-id");
  if (edgeId) return `edge:${edgeId}`;
  return `guid:${target.getAttribute("data-guid") || ""}`;
}

function planTargetPriority(target, elementsByGuid, edgesById, issueCounts) {
  const edgeId = target.getAttribute("data-edge-id");
  if (edgeId) {
    const edge = edgesById.get(edgeId);
    if (planRouteMode === "issues") return 80;
    return edge?.status === "fail" ? 0 : 4;
  }
  const guid = target.getAttribute("data-guid");
  const element = elementsByGuid.get(guid);
  let priority = planElementPriority(element);
  if (planRouteMode === "issues" && issueCounts.get(guid)) priority -= 100;
  return priority;
}

function planElementPriority(element) {
  if (!element) return 70;
  if (element.ifcType === "IfcDoor") return 20;
  if (isStairType(element.ifcType) || isRampType(element.ifcType)) return 30;
  if (element.ifcType === "IfcSpace") return 40;
  if (element.ifcType === "IfcWall" || element.ifcType === "IfcColumn") return 50;
  return 60;
}

function floorPlanSvg(floor, elements, edges, issueCounts) {
  const bounds = floorPlanBounds(elements, edges);
  if (!bounds) return "<p class=\"emptyPlan\">No drawable geometry was generated for this floor.</p>";
  const view = floorPlanView(bounds);
  const spaces = elements.filter((element) => element.ifcType === "IfcSpace" && element.bboxMin && element.bboxMax);
  const walls = elements.filter((element) => ["IfcWall", "IfcColumn"].includes(element.ifcType) && element.bboxMin && element.bboxMax);
  const doors = elements.filter((element) => element.ifcType === "IfcDoor" && element.bboxMin && element.bboxMax);
  const blockers = elements.filter((element) => ["IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"].includes(element.ifcType) && element.bboxMin && element.bboxMax);
  const visibleEdges = planRouteMode === "all" ? edges : planRouteMode === "fail" ? edges.filter((edge) => edge.status === "fail") : [];
  const routeMarkup = visibleEdges
    .filter((edge) => edge.path?.length > 1)
    .sort((a, b) => (a.status === "fail" ? 1 : 0) - (b.status === "fail" ? 1 : 0))
    .map((edge) => floorPlanRoute(edge, bounds, view))
    .join("");
  const wallMarkup = walls.map((element) => floorPlanRect(element, bounds, view, "planWall", issueCounts)).join("");
  const spaceMarkup = spaces.map((element) => floorPlanRect(element, bounds, view, floorPlanSpaceClass(element, issueCounts), issueCounts)).join("");
  const blockerMarkup = blockers.map((element) => floorPlanRect(element, bounds, view, isStairType(element.ifcType) ? "planBlocker" : "planRamp", issueCounts)).join("");
  const doorMarkup = doors.map((element) => floorPlanRect(element, bounds, view, floorPlanDoorClass(element, issueCounts), issueCounts)).join("");
  const labelMarkup = spaces.map((element, index) => index < 90 ? floorPlanLabel(element, bounds, view) : "").join("");
  return `<svg class="floorSvg" viewBox="0 0 ${view.width} ${view.height}" role="img" aria-label="${escapeHtml(floor.name)} floor plan">
    <rect class="planCanvas" x="0" y="0" width="${view.width}" height="${view.height}"></rect>
    <g>${spaceMarkup}</g>
    <g>${wallMarkup}</g>
    <g>${routeMarkup}</g>
    <g>${blockerMarkup}</g>
    <g>${doorMarkup}</g>
    <g>${labelMarkup}</g>
  </svg>`;
}

function floorPlanBounds(elements, edges) {
  const xs = [];
  const ys = [];
  const baseElements = elements.some((element) => element.ifcType === "IfcSpace" && element.bboxMin && element.bboxMax)
    ? elements.filter((element) => element.ifcType === "IfcSpace")
    : elements;
  for (const element of baseElements) {
    if (!element.bboxMin || !element.bboxMax) continue;
    xs.push(element.bboxMin[0], element.bboxMax[0]);
    ys.push(element.bboxMin[1], element.bboxMax[1]);
  }
  if (!xs.length || !ys.length) {
    for (const edge of edges) {
      for (const point of edge.path || []) {
        xs.push(point[0]);
        ys.push(point[1]);
      }
    }
  }
  if (!xs.length || !ys.length) return null;
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const pad = Math.max(1, Math.min(4, Math.max(maxX - minX, maxY - minY) * 0.04));
  return { minX: minX - pad, maxX: maxX + pad, minY: minY - pad, maxY: maxY + pad };
}

function floorPlanView(bounds) {
  const width = 1000;
  const worldWidth = Math.max(bounds.maxX - bounds.minX, 1);
  const worldHeight = Math.max(bounds.maxY - bounds.minY, 1);
  return { width, height: Math.max(380, Math.min(760, Math.round(width * worldHeight / worldWidth))) };
}

function floorPlanPoint(point, bounds, view) {
  const x = ((point[0] - bounds.minX) / Math.max(bounds.maxX - bounds.minX, 0.001)) * view.width;
  const y = view.height - ((point[1] - bounds.minY) / Math.max(bounds.maxY - bounds.minY, 0.001)) * view.height;
  return [x, y];
}

function floorPlanRect(element, bounds, view, className, issueCounts) {
  const a = floorPlanPoint(element.bboxMin, bounds, view);
  const b = floorPlanPoint(element.bboxMax, bounds, view);
  const x = Math.min(a[0], b[0]);
  const y = Math.min(a[1], b[1]);
  const width = Math.max(Math.abs(a[0] - b[0]), element.ifcType === "IfcDoor" ? 4 : 1);
  const height = Math.max(Math.abs(a[1] - b[1]), element.ifcType === "IfcDoor" ? 4 : 1);
  const issueClass = issueCounts.get(element.guid) ? " hasIssue" : "";
  return `<g data-guid="${escapeHtml(element.guid)}">
    <rect class="${className}${issueClass}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${width.toFixed(2)}" height="${height.toFixed(2)}"></rect>
  </g>`;
}

function floorPlanLabel(element, bounds, view) {
  const a = floorPlanPoint(element.bboxMin, bounds, view);
  const b = floorPlanPoint(element.bboxMax, bounds, view);
  const x = Math.min(a[0], b[0]);
  const y = Math.min(a[1], b[1]);
  const width = Math.abs(a[0] - b[0]);
  const height = Math.abs(a[1] - b[1]);
  if (width <= 48 || height <= 18) return "";
  return `<text class="planLabel" x="${(x + width / 2).toFixed(2)}" y="${(y + height / 2).toFixed(2)}">${escapeHtml(cleanElementName(element.name || element.label))}</text>`;
}

function floorPlanRoute(edge, bounds, view) {
  const points = edge.path.map((point) => floorPlanPoint(point, bounds, view).map((value) => value.toFixed(2)).join(",")).join(" ");
  const className = edge.status === "fail" ? "planRoute failRoute" : "planRoute passRoute";
  return `<polyline class="${className}" points="${points}" data-edge-id="${escapeHtml(edge.edgeId)}"></polyline>`;
}

function floorPlanSpaceClass(element, issueCounts) {
  if (issueCounts.get(element.guid)) return "planSpace issueSpace";
  return element.extra?.isCorridorLike ? "planSpace corridorSpace" : "planSpace";
}

function floorPlanDoorClass(element, issueCounts) {
  if (issueCounts.get(element.guid) || Number(element.extra?.derivedDoorWidthM || 0) < 0.9) return "planDoor issueDoor";
  return "planDoor";
}

function planIssueCounts() {
  const counts = new Map();
  for (const issue of appData.issues || []) {
    counts.set(issue.element_guid, (counts.get(issue.element_guid) || 0) + 1);
  }
  return counts;
}

function showPlanElement(guid, elementsByGuid, issueCounts) {
  const element = elementsByGuid.get(guid);
  if (!element) return;
  const issues = (appData.issues || []).filter((issue) => issue.element_guid === guid);
  const rows = [
    ["Type", element.ifcType],
    ["Name", cleanElementName(element.name || element.label)],
    ["Width", valueWithUnit(element.width, "m")],
    ["Depth", valueWithUnit(element.depth, "m")],
    ["Height", valueWithUnit(element.height, "m")],
    ["Issues", String(issueCounts.get(guid) || 0)],
  ];
  document.querySelector("#planDetails").innerHTML = `<h3>${escapeHtml(cleanElementName(element.name || element.label))}</h3>${planRows(rows)}${planIssueList(issues)}`;
}

function showPlanRoute(edgeId, edgesById, elementsByGuid) {
  const edge = edgesById.get(edgeId);
  if (!edge) return;
  const start = elementsByGuid.get(edge.startGuid);
  const end = elementsByGuid.get(edge.endGuid);
  const rows = [
    ["Route", edge.edgeId],
    ["From", cleanElementName(start?.name || start?.label || edge.startGuid)],
    ["To", cleanElementName(end?.name || end?.label || edge.endGuid)],
    ["Distance", valueWithUnit(edge.distanceM, "m")],
    ["Status", edge.status],
    ["Reason", edge.reasons?.map(reasonText).join(", ") || "clear"],
  ];
  document.querySelector("#planDetails").innerHTML = `<h3>${escapeHtml(edge.edgeId)}</h3>${planRows(rows)}`;
}

function planRows(rows) {
  return `<dl class="planRows">${rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "missing")}</dd>`).join("")}</dl>`;
}

function planIssueList(issues) {
  if (!issues.length) return "";
  return `<div class="planIssues">${issues.map((issue) => `<p><strong>${escapeHtml(issue.rule_id)}</strong><br>${escapeHtml(issue.short_text || issue.details)}</p>`).join("")}</div>`;
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
      answer.textContent = `${data.answer || "No generated answer was returned."} Source: ${data.source || "backend response"}.`;
    } catch (error) {
      answer.textContent = error.message || "Assistant request failed.";
    } finally {
      button.disabled = false;
    }
  };

  button.addEventListener("click", ask);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") ask();
  });
}

function fillTable(selector, headers, rows) {
  const table = document.querySelector(selector);
  table.innerHTML = `<thead><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell ?? "missing")}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;
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
        } else if (isStairType(obj.userData.ifcType)) {
          obj.material.color.set(0xb3261e);
          obj.material.opacity = 0.9;
          obj.renderOrder = 3;
        } else if (isRampType(obj.userData.ifcType)) {
          obj.material.color.set(0x9c7a32);
          obj.material.opacity = 0.85;
          obj.renderOrder = 3;
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
    } else if (isStairType(type) || isRampType(type)) {
      obj.material.opacity = enabled ? 1 : 0.9;
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
    color: mesh.userData.ifcType === "IfcDoor" ? 0x5ce1ff : isStairType(mesh.userData.ifcType) ? 0xff7a70 : 0xc9d1cd,
    transparent: true,
    opacity: mesh.userData.ifcType === "IfcDoor" ? 0.85 : isStairType(mesh.userData.ifcType) ? 0.9 : 0.16,
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
      const failed = route.status === "fail" || edges.some((e) => e.status === "fail");
      const reasonCodes = [...new Set(edges.flatMap((e) => e.reasons))];
      const reason = route.reason || reasonCodes.map(reasonText).join(", ") || "clear";
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
  if (route.path?.length) {
    const geometry = new THREE.BufferGeometry().setFromPoints(route.path.map((p) => new THREE.Vector3(p[0], p[1], p[2] + 0.25)));
    const material = new THREE.LineBasicMaterial({ color: route.status === "fail" ? 0xb3261e : 0x2d7d46, transparent: true, opacity: route.status === "fail" ? 0.78 : 0.44 });
    routeGroup.add(new THREE.Line(geometry, material));
    return;
  }
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
  simBlockedHold = 0;
  simPlaying = true;
  document.querySelector("#simPlayPause").textContent = "Pause";
  simWorld.clear();
  simScenarioData = buildFloorScenario();
  simPath = currentSimulationPath();
  addIsometricBase(simWorld);
  simScenarioData.build(simWorld);
  simPathLine = addSimulationPath(simWorld, simPath, currentSimulationRoute()?.status === "fail" ? 0xb3261e : 0x2d7d46);
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
  const routeStartDoors = floorDoors.filter((door) => !isLiftDoor(door));
  const rawFloorEdges = (floor.routeEdgeIds || [])
    .map((edgeId) => appData.routeEdges.find((edge) => edge.edgeId === edgeId))
    .filter(Boolean);
  const routeBounds = floorRouteBounds(floorElements);
  const floorEdges = rawFloorEdges.filter((edge) => routeEdgeLooksDrawable(edge, routeBounds));
  const passEdges = rawFloorEdges.filter((edge) => edge.status === "pass");
  const failedEdges = rawFloorEdges.filter((edge) => edge.status === "fail");
  const transform = createFloorTransform(floorElements);
  const startDoors = chooseFloorStartDoors(floor, routeStartDoors.length ? routeStartDoors : floorDoors, floorEdges);
  const floorStart = chooseFloorStartPoint(floor, startDoors, floorElements, floorEdges);
  const routePaths = buildSimulationRoutesFromStarts(startDoors, floorEdges, transform, floorStart);
  const chosenRoute = routePaths[0];
  const path = routePaths[0]?.path || [new THREE.Vector3(-5, 0.08, 0), new THREE.Vector3(5, 0.08, 0)];
  const reasonCounts = countReasons(rawFloorEdges);
  const floorFailReason = topReasonText(reasonCounts);
  const startText = floorStart?.label || startDoors.map((door) => cleanElementName(door.name || door.label)).join(", ") || "no start point";
  const visualFailedRoutes = routePaths.filter((route) => route.status === "fail").length;
  const blockerText = failedEdges.length ? floorFailReason : visualFailedRoutes ? "stair blocks route" : "none";
  return {
    title: `${floor.name} indoor check`,
    status: chosenRoute
      ? visualFailedRoutes
        ? `Starting from ${startText}. The stair approach is shown as blocked, then the other door routes continue.`
        : `Starting from ${startText}. All generated routes on this floor pass the indoor checks.`
      : "No door-to-door route edges were generated for this floor.",
    source: "SHACL rules over IFCtoLBD RDF and IFC-derived route measurements.",
    fail: Boolean(chosenRoute && chosenRoute.status === "fail"),
    blockAt: chosenRoute?.status === "fail" ? 0.72 : 1,
    routePaths,
    path,
    metrics: [
      ["Start door", startText, startDoors.length ? "pass" : "fail"],
      ["Doors on floor", String(floorDoors.length), floorDoors.length ? "pass" : "fail"],
      ["Route edges", String(rawFloorEdges.length), rawFloorEdges.length ? "pass" : "fail"],
      ["Failed door routes", String(failedEdges.length), failedEdges.length ? "fail" : "pass"],
      ["Stair approach", visualFailedRoutes ? "blocked" : "clear", visualFailedRoutes ? "fail" : "pass"],
      ["Main reason", blockerText, visualFailedRoutes ? "fail" : "pass"],
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

function chooseFloorStartPoint(floor, startDoors, floorElements, floorEdges) {
  const entranceFloor = guessEntranceFloor();
  const stairs = floorElements.filter((element) => isStairType(element.ifcType) && element.center && element.bboxMin && element.bboxMax);
  if (floor.name !== entranceFloor && stairs.length) {
    const stair = stairs[0];
    return {
      label: "stair landing",
      point: stairLandingPoint(stair, floorEdges),
    };
  }
  const door = startDoors[0];
  return door?.center ? { label: cleanElementName(door.name || door.label), point: door.center } : null;
}

function stairLandingPoint(stair, floorEdges) {
  const candidates = [];
  for (const edge of floorEdges) {
    for (const point of edge.path || []) {
      const insideStairX = point[0] >= stair.bboxMin[0] - 0.2 && point[0] <= stair.bboxMax[0] + 0.2;
      const beforeStair = point[1] <= stair.bboxMin[1] - 0.25;
      if (insideStairX && beforeStair) {
        candidates.push(point);
      }
    }
  }
  if (candidates.length) {
    return candidates.sort((a, b) => Math.abs(a[1] - stair.bboxMin[1]) - Math.abs(b[1] - stair.bboxMin[1]) || Math.abs(a[0] - stair.center[0]) - Math.abs(b[0] - stair.center[0]))[0];
  }
  return [stair.center[0], stair.bboxMin[1] - 1.2, stair.center[2]];
}

function chooseFloorStartDoors(floor, floorDoors, floorEdges) {
  const edgeDegree = new Map();
  for (const edge of floorEdges) {
    edgeDegree.set(edge.startGuid, (edgeDegree.get(edge.startGuid) || 0) + 1);
    edgeDegree.set(edge.endGuid, (edgeDegree.get(edge.endGuid) || 0) + 1);
  }
  const doorByGuid = new Map(floorDoors.map((door) => [door.guid, door]));
  const stairStartGuids = new Set();
  for (const edge of floorEdges.filter((item) => item.reasons?.includes("stair_block"))) {
    const startRoutes = appData.accessibleRoutesByDoor?.[edge.startGuid] || [];
    const endRoutes = appData.accessibleRoutesByDoor?.[edge.endGuid] || [];
    if (startRoutes.length && doorByGuid.has(edge.startGuid)) stairStartGuids.add(edge.startGuid);
    if (endRoutes.length && doorByGuid.has(edge.endGuid)) stairStartGuids.add(edge.endGuid);
  }
  const stairStarts = [...stairStartGuids]
    .map((guid) => doorByGuid.get(guid))
    .filter(Boolean)
    .sort((a, b) => String(a.name || a.label).localeCompare(String(b.name || b.label)));
  if (stairStarts.length) return stairStarts;

  const entranceFloor = guessEntranceFloor();
  if (floor.name === entranceFloor) {
    const entrance = [...floorDoors].sort((a, b) => {
      const widthDiff = Number(b.extra?.derivedDoorWidthM || 0) - Number(a.extra?.derivedDoorWidthM || 0);
      if (Math.abs(widthDiff) > 0.001) return widthDiff;
      return (edgeDegree.get(a.guid) || 0) - (edgeDegree.get(b.guid) || 0);
    })[0];
    return entrance ? [entrance] : [];
  }

  const bestStart = [...floorDoors].sort((a, b) => {
    const reachDiff = (appData.accessibleRoutesByDoor?.[b.guid]?.length || 0) - (appData.accessibleRoutesByDoor?.[a.guid]?.length || 0);
    if (reachDiff) return reachDiff;
    return (edgeDegree.get(b.guid) || 0) - (edgeDegree.get(a.guid) || 0);
  })[0];
  return bestStart ? [bestStart] : [];
}

function buildSimulationRoutesFromStarts(startDoors, floorEdges, transform, floorStart) {
  const edgeById = new Map(floorEdges.map((edge) => [edge.edgeId, edge]));
  const routes = [];
  const seen = new Set();
  for (const startDoor of startDoors) {
    const failedEdges = floorEdges.filter((edge) => edge.status === "fail" && (edge.startGuid === startDoor.guid || edge.endGuid === startDoor.guid));
    for (const edge of failedEdges) {
      const startGuid = edge.startGuid === startDoor.guid ? edge.startGuid : edge.endGuid;
      const targetGuid = edge.startGuid === startDoor.guid ? edge.endGuid : edge.startGuid;
      const item = routePathFromEdgeIds(startGuid, [edge.edgeId], edgeById, transform, "fail", edge.reasons?.map(reasonText).join(", ") || "blocked", targetGuid);
      if (item && !seen.has(item.key)) {
        seen.add(item.key);
        routes.push(item);
      }
    }
    const passRoutes = appData.accessibleRoutesByDoor?.[startDoor.guid] || [];
    for (const route of passRoutes) {
      const item = routePathFromEdgeIds(startDoor.guid, route.edge_ids || [], edgeById, transform, "pass", "clear", route.target_guid);
      prependFloorStart(item, floorStart, transform);
      if (item && !seen.has(item.key)) {
        seen.add(item.key);
        routes.push(item);
      }
    }
  }
  return routes;
}

function floorRouteBounds(elements) {
  const boxes = elements.filter((element) => element.bboxMin && element.bboxMax && ["IfcSpace", "IfcWall", "IfcDoor", "IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"].includes(element.ifcType));
  if (!boxes.length) return null;
  return {
    minX: Math.min(...boxes.map((element) => element.bboxMin[0])) - 1.5,
    maxX: Math.max(...boxes.map((element) => element.bboxMax[0])) + 1.5,
    minY: Math.min(...boxes.map((element) => element.bboxMin[1])) - 1.5,
    maxY: Math.max(...boxes.map((element) => element.bboxMax[1])) + 1.5,
    minZ: Math.min(...boxes.map((element) => element.bboxMin[2])) - 1.2,
    maxZ: Math.max(...boxes.map((element) => element.bboxMax[2])) + 1.2,
  };
}

function routeEdgeLooksDrawable(edge, bounds) {
  if (!edge.path?.length) return false;
  if (!bounds) return true;
  for (const point of edge.path) {
    if (point[0] < bounds.minX || point[0] > bounds.maxX || point[1] < bounds.minY || point[1] > bounds.maxY || point[2] < bounds.minZ || point[2] > bounds.maxZ) {
      return false;
    }
  }
  for (let i = 1; i < edge.path.length; i++) {
    const a = edge.path[i - 1];
    const b = edge.path[i];
    const segmentLength = Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
    if (segmentLength > 9) return false;
  }
  return true;
}

function prependFloorStart(route, floorStart, transform) {
  if (!route?.path?.length || !floorStart?.point) return;
  const start = transform.point(floorStart.point);
  const gap = route.path[0].distanceTo(start);
  if (gap > 0.03 && gap <= 1.6) {
    route.path = [start, ...route.path];
  }
}

function routePathFromEdgeIds(startGuid, edgeIds, edgeById, transform, status, reason, targetGuid) {
  let currentGuid = startGuid;
  const points = [];
  const ids = [];
  for (const edgeId of edgeIds) {
    const edge = edgeById.get(edgeId);
    if (!edge?.path?.length) return null;
    const forward = edge.startGuid === currentGuid;
    const reverse = edge.endGuid === currentGuid;
    if (!forward && !reverse) return null;
    const rawPath = forward ? edge.path : [...edge.path].reverse();
    for (const point of rawPath) {
      const next = transform.point(point);
      if (!points.length || points[points.length - 1].distanceTo(next) > 0.03) points.push(next);
    }
    currentGuid = forward ? edge.endGuid : edge.startGuid;
    ids.push(edge.edgeId);
  }
  if (!points.length) return null;
  return {
    key: `${startGuid}:${ids.join("-")}:${targetGuid || currentGuid}`,
    edgeId: ids.join(" + "),
    status,
    reason,
    blockAt: status === "fail" ? 0.72 : 1,
    path: points,
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
  for (const element of elements.filter((item) => ["IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"].includes(item.ifcType) && item.bboxMin && item.bboxMax)) {
    const box = transform.box(element);
    if (isStairType(element.ifcType)) {
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
  const blockAt = routeFails ? route?.blockAt || 0.72 : 1;
  const blocked = routeFails && simProgress >= blockAt;
  if (simPlaying && blocked) {
    simBlockedHold += delta;
    if (simBlockedHold > 1.4) {
      advanceSimulationRoute();
    }
  } else if (simPlaying) {
    simBlockedHold = 0;
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
  simBlockedHold = 0;
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
  const route = currentSimulationRoute();
  const eased = Math.min(progress, route?.status === "fail" ? route.blockAt || 0.72 : 1);
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
  const status = blocked ? `${prefix}Blocked here. Moving to the next route in a moment.` : `${prefix}${simScenarioData.status}`;
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

function isStairType(type) {
  return type === "IfcStair" || type === "IfcStairFlight";
}

function isRampType(type) {
  return type === "IfcRamp" || type === "IfcRampFlight";
}

function isLiftDoor(door) {
  const text = `${door?.name || ""} ${door?.label || ""}`.toLowerCase();
  return text.includes("lift") || text.includes("elevator") || text.includes("hissi") || text.includes("aufzug");
}
