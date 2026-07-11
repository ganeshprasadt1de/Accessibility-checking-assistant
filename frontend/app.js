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
let planControlsReady = false;
let modelState = { models: [], activeModelId: null, defaultPackageAvailable: false };
let pendingUploadFiles = [];
let modelPollTimer;
let viewerAnimationStarted = false;
let simulationAnimationStarted = false;
let viewerResizeReady = false;
let simulationResizeReady = false;

const pages = document.querySelectorAll(".page");
document.querySelectorAll("nav button").forEach((button) => {
  button.addEventListener("click", () => goToPage(button.dataset.page));
});
window.addEventListener("hashchange", () => showPage(currentPage()));

init();

async function init() {
  setupModelLibrary();
  await loadModels();
  await loadAppData();
  showPage(currentPage());
}

function currentPage() {
  const pageId = location.hash ? location.hash.slice(1) : "models";
  return document.getElementById(pageId) ? pageId : "models";
}

function goToPage(pageId) {
  if (!document.getElementById(pageId)) return;
  if (pageId !== "models" && !appData) {
    setModelStatus("Generate and open a completed model before viewing results.");
    pageId = "models";
  }
  if (currentPage() === pageId) {
    showPage(pageId);
    return;
  }
  location.hash = pageId;
}

function showPage(pageId) {
  if (pageId !== "models" && !appData) {
    setModelStatus("Generate and open a completed model before viewing results.");
    pageId = "models";
  }
  document.body.classList.toggle("homePage", pageId === "models");
  document.querySelectorAll("nav button").forEach((button) => button.classList.toggle("active", button.dataset.page === pageId));
  pages.forEach((page) => page.classList.toggle("active", page.id === pageId));
  if (pageId === "model" && appData) {
    if (!viewerReady) {
      setupViewer();
      viewerReady = true;
    }
    requestAnimationFrame(resizeRenderer);
  }
  if (pageId === "simulation" && appData) {
    if (!simulationReady) {
      setupSimulation();
      simulationReady = true;
    }
    requestAnimationFrame(resizeSimulationRenderer);
  }
}

async function loadAppData() {
  const response = await fetch("/api/data");
  if (!response.ok) {
    appData = null;
    return false;
  }
  appData = await response.json();
  renderSummary();
  renderTables();
  setupFloorPlan();
  setupAssistant();
  return true;
}

function setupModelLibrary() {
  const input = document.querySelector("#modelFile");
  const dropZone = document.querySelector(".modelDropZone");
  document.querySelector("#modelUploadButton")?.addEventListener("click", uploadSelectedModels);
  document.querySelector("#modelRefresh")?.addEventListener("click", loadModels);
  input?.addEventListener("change", () => {
    pendingUploadFiles = Array.from(input.files || []);
    setUploadSummary(pendingUploadFiles);
  });
  dropZone?.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
  dropZone?.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
  dropZone?.addEventListener("drop", async (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
    pendingUploadFiles = Array.from(event.dataTransfer?.files || []);
    setUploadSummary(pendingUploadFiles);
    await uploadFiles(pendingUploadFiles);
  });
  document.querySelector("#modelTable")?.addEventListener("click", async (event) => {
    const button = event.target instanceof Element ? event.target.closest("button[data-action]") : null;
    if (!button) return;
    const id = button.getAttribute("data-id");
    const action = button.getAttribute("data-action");
    if (!id || !action) return;
    if (action === "generate") await generateModel(id);
    if (action === "open") await openModel(id);
    if (action === "rename") await renameModel(id);
    if (action === "delete") await deleteModel(id);
  });
}

async function loadModels() {
  try {
    const response = await fetch("/api/models");
    if (!response.ok) return;
    modelState = await response.json();
    renderModelTable();
    renderCurrentModel();
    scheduleModelPolling();
  } catch {
    setModelStatus("Model library could not be loaded.");
  }
}

function renderModelTable() {
  const table = document.querySelector("#modelTable");
  if (!table) return;
  const models = sortedModels();
  if (!models.length) {
    table.innerHTML = "<tbody><tr><td>No uploaded models yet.</td></tr></tbody>";
    return;
  }
  table.innerHTML = `<thead><tr>
    <th>Model</th>
    <th>Status</th>
    <th>Progress</th>
    <th>Package</th>
    <th>Updated</th>
    <th>Actions</th>
  </tr></thead><tbody>${models.map(modelRow).join("")}</tbody>`;
}

function sortedModels() {
  return [...(modelState.models || [])].sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0));
}

function modelRow(model) {
  const active = model.id === modelState.activeModelId ? " active" : "";
  const summary = model.summary || {};
  const fallback = String(summary.ifctolbd || "").toLowerCase().includes("ifctolbd failed");
  const packageText = model.status === "complete"
    ? `${summary.elementCount ?? 0} elements, ${summary.issueCount ?? 0} issues${fallback ? "; IFCtoLBD failed" : ""}`
    : model.message || "";
  return `<tr class="${active}">
    <td class="modelNameCell"><strong>${escapeHtml(model.name || model.fileName || model.id)}</strong><span>${escapeHtml(model.fileName || "")}</span></td>
    <td><span class="statusBadge ${escapeHtml(model.status || "")}">${escapeHtml(model.status || "uploaded")}</span></td>
    <td>${progressMarkup(model)}</td>
    <td>${escapeHtml(packageText)}</td>
    <td>${escapeHtml(formatDate(model.updatedAt))}</td>
    <td><div class="modelActions">${modelActions(model)}</div></td>
  </tr>`;
}

function modelActions(model) {
  const buttons = [];
  if (model.status === "complete") buttons.push(modelButton(model.id, "open", "Open"));
  if (model.status !== "running") buttons.push(modelButton(model.id, "generate", model.status === "complete" ? "Regenerate" : "Generate"));
  buttons.push(modelButton(model.id, "rename", "Rename"));
  if (model.status !== "running") buttons.push(modelButton(model.id, "delete", "Delete"));
  return buttons.join("");
}

function modelButton(id, action, label) {
  return `<button data-id="${escapeHtml(id)}" data-action="${escapeHtml(action)}">${escapeHtml(label)}</button>`;
}

function progressMarkup(model) {
  const value = Math.max(0, Math.min(100, Number(model.progress || 0)));
  const stage = model.stage || "Uploaded";
  const message = model.message || "";
  return `<div class="progressBar"><span style="width:${value}%"></span></div><div class="modelStage">${escapeHtml(stage)}${message ? `<br>${escapeHtml(shortText(message, 70))}` : ""}</div>`;
}

async function uploadSelectedModels() {
  const input = document.querySelector("#modelFile");
  const files = pendingUploadFiles.length ? pendingUploadFiles : Array.from(input?.files || []);
  await uploadFiles(files);
}

async function uploadFiles(files) {
  const input = document.querySelector("#modelFile");
  const ifcFiles = files.filter((file) => file.name.toLowerCase().endsWith(".ifc"));
  if (!ifcFiles.length) {
    setModelStatus("Choose an IFC file first.");
    return;
  }
  for (let index = 0; index < ifcFiles.length; index++) {
    const file = ifcFiles[index];
    setModelStatus(`Uploading ${index + 1}/${ifcFiles.length}: ${file.name}.`);
    const response = await fetch("/api/models/upload", {
      method: "POST",
      headers: { "X-File-Name": encodeURIComponent(file.name) },
      body: file,
    });
    const data = await response.json();
    if (data.error) {
      setModelStatus(data.error);
      return;
    }
  }
  if (input) input.value = "";
  pendingUploadFiles = [];
  setUploadSummary([]);
  setModelStatus(`${ifcFiles.length} model(s) uploaded.`);
  await loadModels();
}

async function generateModel(id) {
  const model = modelState.models.find((item) => item.id === id);
  setModelStatus(`Generating ${model?.name || "model"}.`);
  const response = await fetch(`/api/models/${encodeURIComponent(id)}/generate`, { method: "POST" });
  const data = await response.json();
  if (data.error) {
    setModelStatus(data.error);
    return;
  }
  await loadModels();
}

async function openModel(id) {
  const response = await fetch(`/api/models/${encodeURIComponent(id)}/select`, { method: "POST" });
  const data = await response.json();
  if (data.error) {
    setModelStatus(data.error);
    return;
  }
  await loadModels();
  resetLoadedViews();
  const loaded = await loadAppData();
  if (!loaded) {
    setModelStatus("Selected package could not be loaded.");
    return;
  }
  goToPage("results");
}

function resetLoadedViews() {
  renderer?.dispose();
  simRenderer?.dispose();
  viewerReady = false;
  simulationReady = false;
  scene = null;
  camera = null;
  renderer = null;
  controls = null;
  loadedModel = null;
  routeGroup = null;
  doorMarkerGroup = null;
  edgeOverlayGroup = null;
  doorMeshes = [];
  simScene = null;
  simCamera = null;
  simRenderer = null;
  simControls = null;
  simWorld = null;
  simChair = null;
  simScenarioData = null;
  document.querySelector("#viewer").innerHTML = "";
  document.querySelector("#simulationViewer").innerHTML = "";
  document.querySelector("#selectedDoor").textContent = "Click a door box in the model.";
  document.querySelector("#routeList").innerHTML = "";
}

async function renameModel(id) {
  const model = modelState.models.find((item) => item.id === id);
  const name = prompt("Model name", model?.name || "");
  if (!name) return;
  const response = await fetch(`/api/models/${encodeURIComponent(id)}/rename`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  const data = await response.json();
  if (data.error) {
    setModelStatus(data.error);
    return;
  }
  await loadModels();
}

async function deleteModel(id) {
  const model = modelState.models.find((item) => item.id === id);
  if (!confirm(`Delete ${model?.name || "this model"}?`)) return;
  const response = await fetch(`/api/models/${encodeURIComponent(id)}`, { method: "DELETE" });
  const data = await response.json();
  if (data.error) {
    setModelStatus(data.error);
    return;
  }
  await loadModels();
}

function scheduleModelPolling() {
  const running = (modelState.models || []).some((model) => model.status === "running");
  if (running && !modelPollTimer) {
    modelPollTimer = setInterval(loadModels, 1600);
  } else if (!running && modelPollTimer) {
    clearInterval(modelPollTimer);
    modelPollTimer = null;
  }
}

function setModelStatus(text) {
  const status = document.querySelector("#modelStatus");
  if (status) status.textContent = text;
}

function setUploadSummary(files) {
  const summary = document.querySelector("#modelFileSummary");
  if (!summary) return;
  const ifcCount = files.filter((file) => file.name.toLowerCase().endsWith(".ifc")).length;
  summary.textContent = ifcCount ? `${ifcCount} IFC file(s) selected.` : "No files selected.";
}

function renderSummary() {
  renderCurrentModel();
  const items = [
    ["Elements", appData.summary.elementCount],
    ["Doors", appData.summary.doorCount],
    ["Building issues", buildingIssues().length],
    ["Missing geometry", appData.summary.missingGeometryCount],
    ["IFCtoLBD", String(appData.summary.ifctolbd || "").toLowerCase().includes("ifctolbd failed") ? "failed" : "ok"],
    ["SHACL conforms", appData.summary.shacl.conforms === true ? "yes" : appData.summary.shacl.conforms === false ? "no" : "unknown"],
  ];
  document.querySelector("#summary").innerHTML = items
    .map(([label, value]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`)
    .join("");
}

function renderCurrentModel() {
  const bar = document.querySelector("#currentModelBar");
  if (!bar) return;
  const model = activeModel();
  if (!model) {
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  bar.hidden = false;
  bar.innerHTML = `Current model: <strong>${escapeHtml(model.name || model.fileName || model.id)}</strong>`;
}

function activeModel() {
  return (modelState.models || []).find((model) => model.id === modelState.activeModelId) || null;
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
  const issueRows = buildingIssues().map((i) => [
    i.element_type,
    i.element_label,
    i.short_text,
    issueComparisonText(i),
    displaySource(i.source),
  ]);
  fillTable("#issueTable", ["Type", "Element", "Issue", "Check", "Source"], issueRows);
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
  if (!planControlsReady) {
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
    planControlsReady = true;
  }
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
  document.querySelector(".planShell")?.setAttribute("data-route-mode", planRouteMode);
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
  viewer.innerHTML = floorPlanSvg(floor, elements, edges, issueCounts, elementsByGuid);
  planPick = null;
  applyPlanZoom();
  title.textContent = `${floor.name} floor plan`;
  const floorIssueCount = elements.reduce((sum, element) => sum + (issueCounts.get(element.guid) || 0), 0);
  const routeIssueCount = routeIssueGroupCount(edges, elementsByGuid);
  status.textContent = planRouteMode === "accessible"
    ? `${floor.spaceGuids?.length || 0} rooms and ${floor.doorGuids?.length || 0} doors on this floor.`
    : planRouteMode === "issues"
      ? `${floorIssueCount} building issue${floorIssueCount === 1 ? "" : "s"} on this floor.`
      : `${routeIssueCount} route issue${routeIssueCount === 1 ? "" : "s"} on this floor.`;
  const visibleEdges = planVisibleEdges(edges, elementsByGuid);
  const networkStats = planAccessibleNetworkStats(visibleEdges, elementsByGuid);
  metrics.innerHTML = planMetricRows(floor, networkStats, floorIssueCount, routeIssueCount)
    .map(([label, value, state]) => `<div class="simMetric ${state}"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`)
    .join("");
  details.innerHTML = floorPlanModeHelp();
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
    const regionId = target.getAttribute("data-region-id");
    const areaId = target.getAttribute("data-region-area-id");
    selectPlanTarget(target);
    if (guid) showPlanElement(guid, elementsByGuid, regionId, areaId);
    if (edgeId) {
      const edgeIds = planTargetEdgeIds(target);
      selectPlanRouteRegions(edgeIds, edgesById);
      if (edgeIds.length > 1 && target.classList.contains("routeIssueMarker")) {
        showPlanRouteGroup(edgeIds, edgesById, elementsByGuid);
      } else {
        showPlanRoute(edgeIds[0] || edgeId, edgesById, elementsByGuid);
      }
    }
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
  document.querySelectorAll("#planViewer .selectedIssueRegion").forEach((item) => item.classList.remove("selectedIssueRegion"));
  if (target.hasAttribute("data-edge-id")) {
    for (const edgeId of planTargetEdgeIds(target)) {
      document.querySelectorAll(`#planViewer [data-edge-id="${cssEscape(edgeId)}"]`).forEach((item) => item.classList.add("selectedPlanRoute"));
    }
    target.classList.add("selectedPlanRoute");
    return;
  }
  const regionId = target.getAttribute("data-region-id");
  if (regionId) {
    const areaId = target.getAttribute("data-region-area-id");
    const areaSelector = areaId ? `[data-issue-area-id="${cssEscape(areaId)}"]` : "";
    document.querySelectorAll(`#planViewer [data-issue-region-id="${cssEscape(regionId)}"]${areaSelector}`).forEach((item) => item.classList.add("selectedIssueRegion"));
    target.classList.add("selectedPlanElement");
    return;
  }
  const guid = target.getAttribute("data-guid");
  if (guid) {
    document.querySelectorAll(`#planViewer [data-guid="${cssEscape(guid)}"]`).forEach((item) => item.classList.add("selectedPlanElement"));
    return;
  }
  target.classList.add("selectedPlanElement");
}

function selectPlanRouteRegions(edgeIds, edgesById) {
  const spaceGuids = new Set(
    edgeIds
      .map((edgeId) => edgesById.get(edgeId))
      .filter((edge) => edge?.reasons?.includes("route_width") && edge.viaSpaceGuid)
      .map((edge) => edge.viaSpaceGuid),
  );
  for (const region of appData.issueRegions || []) {
    if (!spaceGuids.has(region.element_guid) || region.rule_id !== "corridor_width") continue;
    document.querySelectorAll(`#planViewer .planIssueRegion[data-issue-region-id="${cssEscape(region.region_id)}"]`).forEach((item) => item.classList.add("selectedIssueRegion"));
  }
}

function planTargetEdgeIds(target) {
  const edgeIds = target.getAttribute("data-edge-ids");
  if (edgeIds) return edgeIds.split(",").filter(Boolean);
  const edgeId = target.getAttribute("data-edge-id");
  return edgeId ? [edgeId] : [];
}

function pickPlanTarget(event, fallback, elementsByGuid, edgesById, issueCounts) {
  const candidates = planTargetCandidates(event, fallback, elementsByGuid, edgesById, issueCounts);
  if (!candidates.length) return null;
  const x = Math.round(event.clientX);
  const y = Math.round(event.clientY);
  const samePick = planPick && Math.abs(planPick.x - x) <= 8 && Math.abs(planPick.y - y) <= 8;
  let target = candidates[0];
  if (samePick && planPick.key) {
    target = nextPlanTarget(candidates, planPick.key, planPick.kind, elementsByGuid, edgesById);
  }
  planPick = { x, y, key: planTargetKey(target), kind: planTargetKind(target, elementsByGuid, edgesById) };
  return target;
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
  const regionId = target.getAttribute("data-region-id");
  if (regionId) return `region:${regionId}:${target.getAttribute("data-region-area-id") || ""}`;
  return `guid:${target.getAttribute("data-guid") || ""}`;
}

function nextPlanTarget(candidates, key, kind, elementsByGuid, edgesById) {
  const index = candidates.findIndex((candidate) => planTargetKey(candidate) === key);
  if (index < 0) return candidates[0];
  for (let offset = 1; offset < candidates.length; offset++) {
    const candidate = candidates[(index + offset) % candidates.length];
    if (planTargetKind(candidate, elementsByGuid, edgesById) !== kind) return candidate;
  }
  return candidates[(index + 1) % candidates.length];
}

function planTargetKind(target, elementsByGuid, edgesById) {
  const edgeId = target.getAttribute("data-edge-id");
  if (edgeId && edgesById.has(edgeId)) return "route";
  const element = elementsByGuid.get(target.getAttribute("data-guid"));
  if (!element) return "element";
  if (element.ifcType === "IfcDoor") return "door";
  if (isStairType(element.ifcType) || isRampType(element.ifcType)) return "blocker";
  if (element.ifcType === "IfcSpace") return "space";
  if (element.ifcType === "IfcWall" || element.ifcType === "IfcColumn") return "wall";
  return "element";
}

function planTargetPriority(target, elementsByGuid, edgesById, issueCounts) {
  const edgeId = target.getAttribute("data-edge-id");
  if (edgeId) {
    if (target.classList.contains("routeIssueMarker")) return 4;
    if (target.classList.contains("routeDotItem")) return 24;
    if (target.classList.contains("routeDoorMarkerItem")) return 24;
    if (planRouteMode === "issues") return 80;
    return 35;
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

function floorPlanSvg(floor, elements, edges, issueCounts, elementsByGuid) {
  const spaces = elements.filter((element) => element.ifcType === "IfcSpace" && element.bboxMin && element.bboxMax);
  const walls = elements.filter((element) => ["IfcWall", "IfcColumn"].includes(element.ifcType) && element.bboxMin && element.bboxMax);
  const doors = elements.filter((element) => element.ifcType === "IfcDoor" && element.bboxMin && element.bboxMax);
  const blockers = elements.filter((element) => ["IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"].includes(element.ifcType) && element.bboxMin && element.bboxMax);
  if (!spaces.length && !doors.length && !edges.some((edge) => edge.path?.length > 1)) {
    return "<p class=\"emptyPlan\">No routed floor geometry was generated for this floor.</p>";
  }
  const bounds = floorPlanBounds(elements, edges);
  if (!bounds) return "<p class=\"emptyPlan\">No drawable geometry was generated for this floor.</p>";
  const view = floorPlanView(bounds);
  const elementGuids = new Set(elements.map((element) => element.guid));
  const issueRegions = (appData.issueRegions || []).filter((region) => elementGuids.has(region.element_guid));
  const visibleEdges = planVisibleEdges(edges, elementsByGuid);
  const showRouteIssues = planRouteMode === "candidate";
  const showElementIssues = planRouteMode === "issues";
  const issueEdges = showRouteIssues ? edges.filter((edge) => !planRouteVisible(edge, elementsByGuid) && pathBlockingRoute(edge)) : [];
  const hiddenPassEdges = planRouteMode === "candidate"
    ? edges.filter((edge) => !planRouteVisible(edge, elementsByGuid) && edge.status === "pass")
    : [];
  const routeIssueEdges = showRouteIssues
    ? [...new Map(visibleEdges.filter(pathBlockingRoute).concat(issueEdges).map((edge) => [edge.edgeId, edge])).values()]
    : [];
  const issueReserved = floorPlanIssueReservedAreas(elements, visibleEdges, bounds, view);
  const doorOpenings = doors.map((door) => floorPlanDoorOpening(door, bounds, view, walls)).filter(Boolean);
  const routeMarkup = floorPlanRouteMarkup(visibleEdges, bounds, view, elementsByGuid);
  const routeIssueMarkup = floorPlanRouteIssueMarkup(routeIssueEdges, bounds, view, elementsByGuid, issueReserved);
  const regionMarkup = floorPlanIssueRegionMarkup(issueRegions, bounds, view);
  const markerEdges = (planRouteMode === "candidate" ? visibleEdges : visibleEdges.filter((edge) => edge.status === "pass")).concat(hiddenPassEdges);
  const connectedDoorMarkup = floorPlanConnectedDoorMarkers(markerEdges, bounds, view, elementsByGuid, walls);
  const wallMarkup = floorPlanWallMarkup(walls, doorOpenings, bounds, view, issueCounts);
  const spaceFillMarkup = floorPlanSpaceFillMarkup(spaces, bounds, view);
  const doorFootprintMarkup = doors.map((element) => floorPlanDoorFootprint(element, bounds, view, floorPlanDoorClass(element, issueCounts), issueCounts, walls)).join("");
  const spaceBorderMarkup = floorPlanSpaceBorderMarkup(spaces, doorOpenings, bounds, view, issueCounts);
  const blockerMarkup = blockers.map((element) => floorPlanRect(element, bounds, view, isStairType(element.ifcType) ? "planBlocker" : "planRamp", issueCounts)).join("");
  const doorMarkup = doors.map((element) => floorPlanDoor(element, bounds, view, floorPlanDoorClass(element, issueCounts), issueCounts, walls)).join("");
  const labelMarkup = spaces.map((element, index) => index < 90 ? floorPlanLabel(element, bounds, view) : "").join("");
  const issueMarkup = showElementIssues ? floorPlanElementIssueMarkup(elements, issueCounts, bounds, view, issueReserved, issueRegions) : "";
  return `<svg class="floorSvg routeMode-${escapeHtml(planRouteMode)}" viewBox="0 0 ${view.width} ${view.height}" role="img" aria-label="${escapeHtml(floor.name)} floor plan">
    <defs><pattern id="planIssueRegionHatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="7" height="7" fill="#f8e3df" fill-opacity="0.72"></rect><line x1="0" y1="0" x2="0" y2="7" stroke="#a84d45" stroke-opacity="0.48" stroke-width="2"></line></pattern></defs>
    <rect class="planCanvas" x="0" y="0" width="${view.width}" height="${view.height}"></rect>
    <g>${spaceFillMarkup}</g>
    <g class="planIssueRegionLayer">${regionMarkup.fill}</g>
    <g>${doorFootprintMarkup}</g>
    <g>${spaceBorderMarkup}</g>
    <g>${wallMarkup}</g>
    <g>${routeMarkup}</g>
    <g>${blockerMarkup}</g>
    <g>${doorMarkup}</g>
    <g>${connectedDoorMarkup}</g>
    <g>${labelMarkup}</g>
    <g class="planIssueLayer">${regionMarkup.measurement}${routeIssueMarkup}${issueMarkup}</g>
  </svg>`;
}

function planVisibleEdges(edges, elementsByGuid) {
  if (planRouteMode === "accessible") {
    const passed = edges.filter((edge) => edge.status === "pass");
    return planAccessibleSpanningForest(passed, elementsByGuid);
  }
  const visible = edges.filter((edge) => planRouteVisible(edge, elementsByGuid));
  if (planRouteMode === "candidate") return visible;
  return [];
}

function distance3d(a, b) {
  return Math.hypot((a?.[0] || 0) - (b?.[0] || 0), (a?.[1] || 0) - (b?.[1] || 0), (a?.[2] || 0) - (b?.[2] || 0));
}

function floorPlanModeHelp() {
  if (planRouteMode === "issues") return "<p>No issue selected.</p>";
  if (planRouteMode === "accessible") return "<p>Accessible network shows the pass-only network without redundant cycles. Separate groups are not mutually reachable.</p>";
  return "<p>No route selected.</p>";
}

function planMetricRows(floor, networkStats, issueCount, routeIssueCount) {
  const rows = [
    ["Rooms", String(floor.spaceGuids?.length || 0), floor.spaceGuids?.length ? "pass" : "fail"],
    ["Doors", String(floor.doorGuids?.length || 0), floor.doorGuids?.length ? "pass" : "fail"],
  ];
  if (planRouteMode === "accessible") {
    rows.push(["Doors on network", String(networkStats.doorCount), networkStats.doorCount === (floor.doorGuids?.length || 0) ? "pass" : "fail"]);
    rows.push(["Accessible groups", String(networkStats.groupCount), networkStats.groupCount === 1 ? "pass" : "fail"]);
  } else {
    const count = planRouteMode === "issues" ? issueCount : routeIssueCount;
    rows.push(["Issues", String(count), count ? "fail" : "pass"]);
  }
  return rows;
}

function planAccessibleNetworkStats(edges, elementsByGuid) {
  const groups = planNetworkComponents(edges, elementsByGuid).filter((group) => group.doorCount >= 2);
  return {
    doorCount: groups.reduce((sum, group) => sum + group.doorCount, 0),
    groupCount: groups.length,
  };
}

function planAccessibleSpanningForest(edges, elementsByGuid) {
  const included = new Set(
    planNetworkComponents(edges, elementsByGuid)
      .filter((group) => group.doorCount >= 2)
      .flatMap((group) => [...group.nodes]),
  );
  const candidates = edges
    .filter((edge) => included.has(edge.startGuid) && included.has(edge.endGuid))
    .sort((a, b) => {
      const role = Number(a.measurements?.routeNetworkRole === "candidate") - Number(b.measurements?.routeNetworkRole === "candidate");
      if (role) return role;
      const length = Number(a.distanceM || 0) - Number(b.distanceM || 0);
      if (Math.abs(length) > 0.001) return length;
      const points = (a.path?.length || 0) - (b.path?.length || 0);
      return points || String(a.edgeId).localeCompare(String(b.edgeId));
    });
  const parent = new Map();
  const find = (node) => {
    if (!parent.has(node)) parent.set(node, node);
    if (parent.get(node) !== node) parent.set(node, find(parent.get(node)));
    return parent.get(node);
  };
  const selected = [];
  for (const edge of candidates) {
    const start = find(edge.startGuid);
    const end = find(edge.endGuid);
    if (start === end) continue;
    parent.set(end, start);
    selected.push(edge);
  }
  const incident = new Map();
  selected.forEach((edge, index) => {
    for (const node of [edge.startGuid, edge.endGuid]) {
      if (!incident.has(node)) incident.set(node, new Set());
      incident.get(node).add(index);
    }
  });
  const active = new Set(selected.map((_edge, index) => index));
  const queue = [...incident]
    .filter(([node, edgeIds]) => elementsByGuid.get(node)?.ifcType !== "IfcDoor" && edgeIds.size <= 1)
    .map(([node]) => node);
  while (queue.length) {
    const node = queue.shift();
    const edgeIndex = [...(incident.get(node) || [])].find((index) => active.has(index));
    if (edgeIndex == null) continue;
    active.delete(edgeIndex);
    const edge = selected[edgeIndex];
    for (const endpoint of [edge.startGuid, edge.endGuid]) {
      incident.get(endpoint)?.delete(edgeIndex);
      if (elementsByGuid.get(endpoint)?.ifcType !== "IfcDoor" && incident.get(endpoint)?.size <= 1) queue.push(endpoint);
    }
  }
  return selected.filter((_edge, index) => active.has(index));
}

function planNetworkComponents(edges, elementsByGuid) {
  const graph = new Map();
  for (const edge of edges) {
    if (!edge.startGuid || !edge.endGuid || edge.startGuid === edge.endGuid) continue;
    if (!graph.has(edge.startGuid)) graph.set(edge.startGuid, new Set());
    if (!graph.has(edge.endGuid)) graph.set(edge.endGuid, new Set());
    graph.get(edge.startGuid).add(edge.endGuid);
    graph.get(edge.endGuid).add(edge.startGuid);
  }
  const seen = new Set();
  const groups = [];
  for (const start of graph.keys()) {
    if (seen.has(start)) continue;
    const pending = [start];
    const nodes = new Set();
    let doorCount = 0;
    while (pending.length) {
      const guid = pending.pop();
      if (seen.has(guid)) continue;
      seen.add(guid);
      nodes.add(guid);
      if (elementsByGuid.get(guid)?.ifcType === "IfcDoor") doorCount += 1;
      for (const next of graph.get(guid) || []) {
        if (!seen.has(next)) pending.push(next);
      }
    }
    groups.push({ nodes, doorCount });
  }
  return groups;
}

function planRouteVisible(edge, elementsByGuid) {
  if (!isLocalDoorToDoorRoute(edge, elementsByGuid)) return true;
  return pathBlockingRoute(edge);
}

function isLocalDoorToDoorRoute(edge, elementsByGuid) {
  if (edge.source !== "IFC space boundaries and floor geometry") return false;
  const space = elementsByGuid.get(edge.viaSpaceGuid);
  if (space?.extra?.isCorridorLike) return false;
  return elementsByGuid.get(edge.startGuid)?.ifcType === "IfcDoor" && elementsByGuid.get(edge.endGuid)?.ifcType === "IfcDoor";
}

function floorPlanBounds(elements, edges) {
  const xs = [];
  const ys = [];
  const baseElements = elements.some((element) => element.ifcType === "IfcSpace" && element.bboxMin && element.bboxMax)
    ? elements.filter((element) => element.ifcType === "IfcSpace" || element.ifcType === "IfcDoor")
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
  if (element.ifcType === "IfcDoor") return floorPlanDoor(element, bounds, view, className, issueCounts);
  const a = floorPlanPoint(element.bboxMin, bounds, view);
  const b = floorPlanPoint(element.bboxMax, bounds, view);
  const x = Math.min(a[0], b[0]);
  const y = Math.min(a[1], b[1]);
  const width = Math.max(Math.abs(a[0] - b[0]), 1);
  const height = Math.max(Math.abs(a[1] - b[1]), 1);
  return `<g data-guid="${escapeHtml(element.guid)}">
    <rect class="${className}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${width.toFixed(2)}" height="${height.toFixed(2)}"></rect>
  </g>`;
}

function floorPlanDoor(element, bounds, view, className, issueCounts, walls = []) {
  const box = floorPlanDoorHitBox(element, bounds, view, walls);
  return `<g data-guid="${escapeHtml(element.guid)}">
    <rect class="planDoorHit" x="${box.x.toFixed(2)}" y="${box.y.toFixed(2)}" width="${box.width.toFixed(2)}" height="${box.height.toFixed(2)}"></rect>
  </g>`;
}

function floorPlanDoorFootprint(element, bounds, view, className, issueCounts, walls = []) {
  const box = floorPlanDoorOpening(element, bounds, view, walls) || floorPlanBox(element, bounds, view);
  return `<g data-guid="${escapeHtml(element.guid)}">
    <rect class="${className}" x="${box.x.toFixed(2)}" y="${box.y.toFixed(2)}" width="${box.width.toFixed(2)}" height="${box.height.toFixed(2)}"></rect>
  </g>`;
}

function floorPlanDoorHitBox(element, bounds, view, walls = []) {
  const box = floorPlanDoorOpening(element, bounds, view, walls) || floorPlanBox(element, bounds, view);
  const pad = 4;
  return { x: box.x - pad, y: box.y - pad, width: box.width + pad * 2, height: box.height + pad * 2 };
}

function floorPlanWallMarkup(walls, openings, bounds, view, issueCounts) {
  return walls.map((wall) => floorPlanWall(wall, openings, bounds, view, issueCounts)).join("");
}

function floorPlanSpaceFillMarkup(spaces, bounds, view) {
  return spaces.map((space) => {
    const box = floorPlanBox(space, bounds, view);
    return `<g data-guid="${escapeHtml(space.guid)}">
      <rect class="${floorPlanSpaceFillClass(space)}" x="${box.x.toFixed(2)}" y="${box.y.toFixed(2)}" width="${box.width.toFixed(2)}" height="${box.height.toFixed(2)}"></rect>
    </g>`;
  }).join("");
}

function floorPlanSpaceBorderMarkup(spaces, openings, bounds, view, issueCounts) {
  return spaces.map((space) => {
    const box = floorPlanBox(space, bounds, view);
    const borderClass = floorPlanSpaceBorderClass(space, issueCounts);
    return `<g data-guid="${escapeHtml(space.guid)}">${floorPlanSpaceBorder(box, openings, borderClass)}</g>`;
  }).join("");
}

function floorPlanSpaceBorder(box, openings, className) {
  return [
    ...floorPlanBorderSegments(box.x, box.x + box.width, box.y, "h", openings),
    ...floorPlanBorderSegments(box.x, box.x + box.width, box.y + box.height, "h", openings),
    ...floorPlanBorderSegments(box.y, box.y + box.height, box.x, "v", openings),
    ...floorPlanBorderSegments(box.y, box.y + box.height, box.x + box.width, "v", openings),
  ].map((segment) => floorPlanBorderLine(segment, className)).join("");
}

function floorPlanBorderSegments(start, end, coord, axis, openings) {
  const cuts = openings
    .filter((opening) => axis === "h" ? coord >= opening.y - 0.8 && coord <= opening.y + opening.height + 0.8 : coord >= opening.x - 0.8 && coord <= opening.x + opening.width + 0.8)
    .map((opening) => axis === "h" ? [Math.max(start, opening.x), Math.min(end, opening.x + opening.width)] : [Math.max(start, opening.y), Math.min(end, opening.y + opening.height)])
    .filter(([cutStart, cutEnd]) => cutEnd - cutStart > 0.8)
    .sort((a, b) => a[0] - b[0]);
  const segments = [];
  let cursor = start;
  for (const [cutStart, cutEnd] of cuts) {
    if (cutStart - cursor > 0.8) segments.push({ axis, coord, start: cursor, end: cutStart });
    cursor = Math.max(cursor, cutEnd);
  }
  if (end - cursor > 0.8) segments.push({ axis, coord, start: cursor, end });
  return segments;
}

function floorPlanBorderLine(segment, className) {
  if (segment.axis === "h") {
    return `<line class="${className}" x1="${segment.start.toFixed(2)}" y1="${segment.coord.toFixed(2)}" x2="${segment.end.toFixed(2)}" y2="${segment.coord.toFixed(2)}"></line>`;
  }
  return `<line class="${className}" x1="${segment.coord.toFixed(2)}" y1="${segment.start.toFixed(2)}" x2="${segment.coord.toFixed(2)}" y2="${segment.end.toFixed(2)}"></line>`;
}

function floorPlanWall(element, openings, bounds, view, issueCounts) {
  const box = floorPlanBox(element, bounds, view);
  const horizontal = box.width >= box.height;
  const cuts = openings
    .filter((opening) => floorPlanBoxesOverlap(box, opening))
    .map((opening) => horizontal ? [Math.max(box.x, opening.x), Math.min(box.x + box.width, opening.x + opening.width)] : [Math.max(box.y, opening.y), Math.min(box.y + box.height, opening.y + opening.height)])
    .filter(([start, end]) => end - start > 1)
    .sort((a, b) => a[0] - b[0]);
  if (!cuts.length) return floorPlanWallSegmentGroup(element, [box], issueCounts);
  const segments = [];
  let cursor = horizontal ? box.x : box.y;
  for (const [start, end] of cuts) {
    if (start - cursor > 0.8) segments.push(horizontal ? { x: cursor, y: box.y, width: start - cursor, height: box.height } : { x: box.x, y: cursor, width: box.width, height: start - cursor });
    cursor = Math.max(cursor, end);
  }
  const limit = horizontal ? box.x + box.width : box.y + box.height;
  if (limit - cursor > 0.8) segments.push(horizontal ? { x: cursor, y: box.y, width: limit - cursor, height: box.height } : { x: box.x, y: cursor, width: box.width, height: limit - cursor });
  return floorPlanWallSegmentGroup(element, segments, issueCounts);
}

function floorPlanWallSegmentGroup(element, segments, issueCounts) {
  const rects = segments
    .map((segment) => `<rect class="planWall" x="${segment.x.toFixed(2)}" y="${segment.y.toFixed(2)}" width="${segment.width.toFixed(2)}" height="${segment.height.toFixed(2)}"></rect>`)
    .join("");
  return `<g data-guid="${escapeHtml(element.guid)}">${rects}</g>`;
}

function floorPlanBoxesOverlap(a, b) {
  return Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x) > 0.6 &&
    Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y) > 0.6;
}

function floorPlanBox(element, bounds, view) {
  const a = floorPlanPoint(element.bboxMin, bounds, view);
  const b = floorPlanPoint(element.bboxMax, bounds, view);
  const x = Math.min(a[0], b[0]);
  const y = Math.min(a[1], b[1]);
  const width = Math.max(Math.abs(a[0] - b[0]), 1);
  const height = Math.max(Math.abs(a[1] - b[1]), 1);
  return { x, y, width, height };
}

function floorPlanDoorOpening(element, bounds, view, walls = []) {
  const axis = floorPlanDoorAxis(element, bounds, view, walls);
  if (!axis) return null;
  const thickness = Math.max(axis.thickness, Math.min(8, axis.length * 0.45));
  if (axis.horizontal) {
    return { x: axis.center[0] - axis.length / 2, y: axis.center[1] - thickness / 2, width: axis.length, height: thickness };
  }
  return { x: axis.center[0] - thickness / 2, y: axis.center[1] - axis.length / 2, width: thickness, height: axis.length };
}

function floorPlanDoorAxis(element, bounds, view, walls = []) {
  if (!element?.bboxMin || !element?.bboxMax) return null;
  const a = floorPlanPoint(element.bboxMin, bounds, view);
  const b = floorPlanPoint(element.bboxMax, bounds, view);
  const width = Math.abs(a[0] - b[0]);
  const height = Math.abs(a[1] - b[1]);
  const scale = floorPlanScale(bounds, view);
  const wall = floorPlanDoorWall(element, walls);
  const center = element.center ? floorPlanPoint(element.center, bounds, view) : [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const axisX = Number(element.extra?.doorWidthAxisX);
  const axisY = Number(element.extra?.doorWidthAxisY);
  const horizontal = Number.isFinite(axisX) && Number.isFinite(axisY) ? Math.abs(axisX) >= Math.abs(axisY) : wall ? wall.horizontal : width >= height;
  const assessedWidth = doorAssessedWidth(element);
  const worldLength = assessedWidth || (horizontal ? Math.abs(element.bboxMax[0] - element.bboxMin[0]) : Math.abs(element.bboxMax[1] - element.bboxMin[1]));
  const length = Math.max(worldLength * (horizontal ? scale.x : scale.y), 5);
  const wallThickness = wall ? wall.thickness * (horizontal ? scale.y : scale.x) + 3 : 0;
  const rawThickness = horizontal ? height : width;
  const fallbackThickness = Math.max(3, Math.min(rawThickness, Math.min(8, length * 0.18)));
  const thickness = Math.max(wallThickness, fallbackThickness);
  return { center, horizontal, length, thickness };
}

function floorPlanScale(bounds, view) {
  return {
    x: view.width / Math.max(bounds.maxX - bounds.minX, 0.001),
    y: view.height / Math.max(bounds.maxY - bounds.minY, 0.001),
  };
}

function floorPlanDoorWall(door, walls) {
  const point = elementPoint(door);
  if (!point) return null;
  const candidates = walls
    .filter((wall) => wall.bboxMin && wall.bboxMax)
    .map((wall) => {
      const distance = floorPlanBoxDistance(point, wall);
      const horizontal = Math.abs(wall.bboxMax[0] - wall.bboxMin[0]) >= Math.abs(wall.bboxMax[1] - wall.bboxMin[1]);
      const thickness = horizontal ? Math.abs(wall.bboxMax[1] - wall.bboxMin[1]) : Math.abs(wall.bboxMax[0] - wall.bboxMin[0]);
      return { distance, horizontal, thickness };
    })
    .filter((item) => item.distance <= 0.75)
    .sort((a, b) => a.distance - b.distance || a.thickness - b.thickness);
  return candidates[0] || null;
}

function floorPlanBoxDistance(point, element) {
  const dx = point[0] < element.bboxMin[0] ? element.bboxMin[0] - point[0] : point[0] > element.bboxMax[0] ? point[0] - element.bboxMax[0] : 0;
  const dy = point[1] < element.bboxMin[1] ? element.bboxMin[1] - point[1] : point[1] > element.bboxMax[1] ? point[1] - element.bboxMax[1] : 0;
  return Math.hypot(dx, dy);
}

function floorPlanLabel(element, bounds, view) {
  const a = floorPlanPoint(element.bboxMin, bounds, view);
  const b = floorPlanPoint(element.bboxMax, bounds, view);
  const x = Math.min(a[0], b[0]);
  const y = Math.min(a[1], b[1]);
  const width = Math.abs(a[0] - b[0]);
  const height = Math.abs(a[1] - b[1]);
  if (width <= 48 || height <= 18) return "";
  return `<text class="planLabel" x="${(x + width / 2).toFixed(2)}" y="${(y + height / 2).toFixed(2)}">${escapeHtml(planElementName(element.name || element.label))}</text>`;
}

function floorPlanRoute(edge, bounds, view, elementsByGuid) {
  const screenPoints = edge.path.map((point) => floorPlanPoint(point, bounds, view));
  if (routeAsConnectionDot(edge, screenPoints)) {
    if (routeDoorForEdge(edge, elementsByGuid)) return "";
    return floorPlanRouteDotSegment({ point: screenPoints[0], edgeId: edge.edgeId });
  }
  const points = screenPoints.map((point) => point.map((value) => value.toFixed(2)).join(",")).join(" ");
  const className = pathBlockingRoute(edge) ? "planRoute passRoute blockedRoute" : "planRoute passRoute";
  return `<g class="planRouteItem${pathBlockingRoute(edge) ? " blockedRouteItem" : ""}" data-edge-id="${escapeHtml(edge.edgeId)}">
    <polyline class="planRouteHit" points="${points}"></polyline>
    <polyline class="planRouteCasing" points="${points}"></polyline>
    <polyline class="${className}" points="${points}"></polyline>
  </g>`;
}

function floorPlanRouteMarkup(edges, bounds, view, elementsByGuid) {
  const drawable = edges
    .filter((edge) => edge.path?.length > 1)
    .sort((a, b) => (pathBlockingRoute(a) ? 1 : 0) - (pathBlockingRoute(b) ? 1 : 0));
  if (planRouteMode === "accessible") {
    return drawable.map((edge) => floorPlanRoute(edge, bounds, view, elementsByGuid)).join("");
  }
  if (planRouteMode === "candidate") {
    return floorPlanMergedRoutes(drawable, bounds, view, elementsByGuid);
  }
  if (drawable.length <= 30) {
    return drawable.map((edge) => floorPlanRoute(edge, bounds, view, elementsByGuid)).join("");
  }
  return floorPlanMergedRoutes(drawable, bounds, view, elementsByGuid);
}

function _clampNumber(value, low, high) {
  if (low > high) return (low + high) / 2;
  return Math.max(low, Math.min(high, value));
}

function floorPlanMergedRoutes(edges, bounds, view, elementsByGuid) {
  const groups = new Map();
  const diagonal = [];
  const dots = [];
  for (const edge of edges) {
    const points = edge.path.map((point) => floorPlanPoint(point, bounds, view));
    if (routeAsConnectionDot(edge, points)) {
      if (!routeDoorForEdge(edge, elementsByGuid)) dots.push({ point: points[0], edgeId: edge.edgeId });
      continue;
    }
    let drawable = false;
    for (let index = 1; index < points.length; index++) {
      const a = points[index - 1];
      const b = points[index];
      const dx = b[0] - a[0];
      const dy = b[1] - a[1];
      if (Math.hypot(dx, dy) < 4) continue;
      drawable = true;
      if (Math.abs(dy) <= 2) {
        addRouteInterval(groups, `h:${Math.round((a[1] + b[1]) / 2)}`, "h", (a[1] + b[1]) / 2, Math.min(a[0], b[0]), Math.max(a[0], b[0]), edge.edgeId);
      } else if (Math.abs(dx) <= 2) {
        addRouteInterval(groups, `v:${Math.round((a[0] + b[0]) / 2)}`, "v", (a[0] + b[0]) / 2, Math.min(a[1], b[1]), Math.max(a[1], b[1]), edge.edgeId);
      } else {
        diagonal.push({ points: `${a[0].toFixed(2)},${a[1].toFixed(2)} ${b[0].toFixed(2)},${b[1].toFixed(2)}`, edgeId: edge.edgeId, count: 1 });
      }
    }
    if (!drawable && points.length) {
      if (!routeDoorForEdge(edge, elementsByGuid)) dots.push({ point: points[0], edgeId: edge.edgeId });
    }
  }
  const merged = [];
  for (const group of groups.values()) {
    const intervals = group.intervals.sort((a, b) => a.start - b.start);
    let current = null;
    for (const interval of intervals) {
      if (!current || interval.start > current.end + 3) {
        if (current) merged.push(routeSegmentFromInterval(group, current));
        current = { ...interval, edgeIds: [...interval.edgeIds] };
      } else {
        current.end = Math.max(current.end, interval.end);
        current.edgeIds.push(...interval.edgeIds);
      }
    }
    if (current) merged.push(routeSegmentFromInterval(group, current));
  }
  return [...merged, ...diagonal]
    .map(floorPlanRouteSegment)
    .join("") + dots.map(floorPlanRouteDotSegment).join("");
}

function routeAsConnectionDot(edge, points) {
  const source = String(edge.source || "");
  if (!source.includes("space boundary to corridor spine")) return routeScreenLength(points) < 4;
  return routeScreenLength(points) < 18;
}

function addRouteInterval(groups, key, axis, coord, start, end, edgeId) {
  if (!groups.has(key)) groups.set(key, { axis, coord, intervals: [] });
  groups.get(key).intervals.push({ start, end, edgeIds: [edgeId] });
}

function routeSegmentFromInterval(group, interval) {
  const points = group.axis === "h"
    ? `${interval.start.toFixed(2)},${group.coord.toFixed(2)} ${interval.end.toFixed(2)},${group.coord.toFixed(2)}`
    : `${group.coord.toFixed(2)},${interval.start.toFixed(2)} ${group.coord.toFixed(2)},${interval.end.toFixed(2)}`;
  return { points, edgeId: interval.edgeIds[0], count: interval.edgeIds.length };
}

function floorPlanRouteSegment(segment) {
  const edgeIds = segment.edgeIds || [segment.edgeId];
  return `<g class="planRouteItem routeBundle" data-edge-id="${escapeHtml(segment.edgeId)}" data-edge-ids="${escapeHtml(edgeIds.join(","))}" data-edge-count="${segment.count}">
    <polyline class="planRouteHit" points="${segment.points}"></polyline>
    <polyline class="planRouteCasing" points="${segment.points}"></polyline>
    <polyline class="planRoute passRoute" points="${segment.points}"></polyline>
  </g>`;
}

function floorPlanRouteDotSegment(segment) {
  return `<g class="planRouteItem routeDotItem" data-edge-id="${escapeHtml(segment.edgeId)}">
    <circle class="planRouteDotHit" cx="${segment.point[0].toFixed(2)}" cy="${segment.point[1].toFixed(2)}" r="8"></circle>
    ${floorPlanRouteDot(segment.point)}
  </g>`;
}

function floorPlanRouteDot(point) {
  if (!point) return "";
  return `<circle class="planRouteDot" cx="${point[0].toFixed(2)}" cy="${point[1].toFixed(2)}" r="3.8"></circle>`;
}

function floorPlanConnectedDoorMarkers(edges, bounds, view, elementsByGuid, walls = []) {
  const markers = new Map();
  for (const edge of edges) {
    for (const guid of [edge.startGuid, edge.endGuid]) {
      const door = elementsByGuid.get(guid);
      if (door?.ifcType !== "IfcDoor" || !door.center || !door.bboxMin || !door.bboxMax) continue;
      const axis = floorPlanDoorAxis(door, bounds, view, walls);
      if (!markers.has(guid)) markers.set(guid, { axis, edgeIds: [] });
      if (axis?.projected && !markers.get(guid).axis?.projected) markers.get(guid).axis = axis;
      markers.get(guid).edgeIds.push(edge.edgeId);
    }
  }
  return [...markers.values()].filter((item) => item.axis).map(floorPlanRouteDoorMarker).join("");
}

function floorPlanRouteDoorMarker(item) {
  const edgeIds = uniqueText(item.edgeIds);
  const axis = item.axis;
  const radius = Math.max(5.2, Math.min(7.2, axis.thickness * 0.85));
  return `<g class="planRouteItem routeDoorMarkerItem" data-edge-id="${escapeHtml(edgeIds[0])}" data-edge-ids="${escapeHtml(edgeIds.join(","))}">
    <circle class="planRouteDoorMarkerHit" cx="${axis.center[0].toFixed(2)}" cy="${axis.center[1].toFixed(2)}" r="${(radius + 5).toFixed(2)}"></circle>
    <circle class="planRouteDoorMarker" cx="${axis.center[0].toFixed(2)}" cy="${axis.center[1].toFixed(2)}" r="${radius.toFixed(2)}"></circle>
  </g>`;
}

function floorPlanIssueRegionMarkup(regions, bounds, view) {
  const fill = [];
  const measurement = [];
  for (const region of regions) {
    for (const area of issueRegionAreas(region)) {
      const areaAttribute = ` data-issue-area-id="${escapeHtml(area.area_id)}"`;
      const path = floorPlanIssueRegionPath(area.geometry, bounds, view);
      if (path) {
        fill.push(`<g class="planIssueRegion" data-issue-region-id="${escapeHtml(region.region_id)}"${areaAttribute}><path class="planIssueRegionPath" d="${path}" fill-rule="evenodd"></path></g>`);
      }
      const line = area.measurement_line || [];
      if (line.length !== 2) continue;
      const [start, end] = floorPlanOrthogonalMeasurement(
        floorPlanPoint(line[0], bounds, view),
        floorPlanPoint(line[1], bounds, view),
      );
      measurement.push(`<g class="planIssueMeasurement" data-issue-region-id="${escapeHtml(region.region_id)}"${areaAttribute}>
        <line class="planIssueMeasureHalo" x1="${start[0].toFixed(2)}" y1="${start[1].toFixed(2)}" x2="${end[0].toFixed(2)}" y2="${end[1].toFixed(2)}"></line>
        <line class="planIssueMeasureLine" x1="${start[0].toFixed(2)}" y1="${start[1].toFixed(2)}" x2="${end[0].toFixed(2)}" y2="${end[1].toFixed(2)}"></line>
        <circle class="planIssueMeasureEnd" cx="${start[0].toFixed(2)}" cy="${start[1].toFixed(2)}" r="2"></circle>
        <circle class="planIssueMeasureEnd" cx="${end[0].toFixed(2)}" cy="${end[1].toFixed(2)}" r="2"></circle>
      </g>`);
    }
  }
  return { fill: fill.join(""), measurement: measurement.join("") };
}

function floorPlanOrthogonalMeasurement(start, end) {
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const length = Math.hypot(dx, dy);
  if (!length) return [start, end];
  const middle = [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2];
  if (Math.abs(dx) <= Math.abs(dy) * 0.22) {
    return [[middle[0], middle[1] - length / 2], [middle[0], middle[1] + length / 2]];
  }
  if (Math.abs(dy) <= Math.abs(dx) * 0.22) {
    return [[middle[0] - length / 2, middle[1]], [middle[0] + length / 2, middle[1]]];
  }
  return [start, end];
}

function issueRegionAreas(region) {
  if (region?.areas?.length) {
    return region.areas.map((area, index) => ({ ...area, position: index + 1 }));
  }
  return region ? [{
    area_id: region.region_id,
    measured: region.measured,
    geometry: region.geometry,
    anchor: region.anchor,
    measurement_line: region.measurement_line,
    position: 1,
  }] : [];
}

function floorPlanIssueRegionPath(geometry, bounds, view) {
  if (!geometry?.coordinates) return "";
  const polygons = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.type === "MultiPolygon" ? geometry.coordinates : [];
  return polygons.flatMap((polygon) => polygon.map((ring) => {
    const points = ring.map((point) => floorPlanPoint([point[0], point[1], 0], bounds, view));
    return points.length ? `M ${points.map((point) => `${point[0].toFixed(2)} ${point[1].toFixed(2)}`).join(" L ")} Z` : "";
  })).filter(Boolean).join(" ");
}

function floorPlanElementIssueMarkup(elements, issueCounts, bounds, view, reserved = [], issueRegions = []) {
  const groups = new Map();
  const regionsByIssue = new Map(issueRegions.map((region) => [region.issue_id, region]));
  for (const element of elements.filter((item) => issueCounts.get(item.guid) && (item.center || item.bboxMin))) {
    const key = elementIssueGroupKey(element);
    if (!groups.has(key)) groups.set(key, { elements: [], issues: [] });
    groups.get(key).elements.push(element);
  }
  for (const issue of buildingIssues()) {
    const element = elements.find((item) => item.guid === issue.element_guid);
    if (!element) continue;
    const group = groups.get(elementIssueGroupKey(element));
    if (group) {
      group.issues.push(issue);
      if (regionsByIssue.has(issue.issue_id)) group.region = regionsByIssue.get(issue.issue_id);
    }
  }
  const markers = [...groups.values()].flatMap((group) => {
    const count = issueGroupCount(group);
    const areas = issueRegionAreas(group.region);
    if (!areas.length) return [{
      group,
      count,
      anchor: averageScreenPoint(group.elements.map(elementPoint).filter(Boolean).map((item) => floorPlanPoint(item, bounds, view))),
      radius: floorPlanIssueRadius(count, 6.6),
    }];
    return areas.map((area) => ({
      group,
      count,
      area,
      anchor: floorPlanPoint(area.anchor, bounds, view),
      radius: floorPlanIssueRadius(count, 5.8),
    }));
  });
  return floorPlanIssueLayout(markers, reserved, view).map(floorPlanElementIssueMarker).join("");
}

function floorPlanElementIssueMarker(marker) {
  const { group, count, area, anchor, point, radius } = marker;
  const element = group.elements[0];
  const checks = uniqueText(group.issues.map(issueCheckText)).join("; ");
  const areas = Number(group.region?.area_count || 0);
  const areaText = area && areas > 1 ? `; affected area ${area.position} of ${areas}` : areas > 1 ? `; ${areas} affected areas` : "";
  const title = checks ? `${checks}${areaText}` : `${count} issue${count > 1 ? "s" : ""}: ${elementName(element)}`;
  const regionId = group.region ? ` data-region-id="${escapeHtml(group.region.region_id)}"` : "";
  const areaId = area ? ` data-region-area-id="${escapeHtml(area.area_id)}"` : "";
  return `<g class="planElementIssueMarker" data-guid="${escapeHtml(element.guid)}"${regionId}${areaId}>
    <title>${escapeHtml(title)}</title>
    ${floorPlanIssueLeaderMarkup(anchor, point, radius)}
    <circle class="planIssueAnchorHalo" cx="${anchor[0].toFixed(2)}" cy="${anchor[1].toFixed(2)}" r="3.2"></circle>
    <circle class="planIssueAnchor" cx="${anchor[0].toFixed(2)}" cy="${anchor[1].toFixed(2)}" r="2"></circle>
    <circle class="planElementIssueHit" cx="${point[0].toFixed(2)}" cy="${point[1].toFixed(2)}" r="${(radius + 5).toFixed(2)}"></circle>
    <circle class="planElementIssueHalo" cx="${point[0].toFixed(2)}" cy="${point[1].toFixed(2)}" r="${(radius + 2.8).toFixed(2)}"></circle>
    <circle class="planElementIssueDot" cx="${point[0].toFixed(2)}" cy="${point[1].toFixed(2)}" r="${radius.toFixed(2)}"></circle>
    ${count > 1 ? `<text class="planElementIssueCount" x="${point[0].toFixed(2)}" y="${point[1].toFixed(2)}">${count}</text>` : planElementIssueTickMarkup(point[0], point[1])}
  </g>`;
}

function planElementIssueTickMarkup(x, y) {
  return `<line class="planElementIssueTick" x1="${x.toFixed(2)}" y1="${(y - 3.4).toFixed(2)}" x2="${x.toFixed(2)}" y2="${(y + 1.4).toFixed(2)}"></line>
      <circle class="planElementIssueTickDot" cx="${x.toFixed(2)}" cy="${(y + 4).toFixed(2)}" r="0.9"></circle>`;
}

function issueGroupCount(group) {
  if (group.elements.some((element) => isStairType(element.ifcType))) {
    return uniqueText(group.issues.map((issue) => issue.rule_id)).length || group.elements.length;
  }
  return group.issues.length || group.elements.length;
}

function elementIssueGroupKey(element) {
  if (isStairType(element.ifcType)) return stairGroupKey(element);
  return element.guid;
}

function averageScreenPoint(points) {
  if (!points.length) return [0, 0];
  return [
    points.reduce((sum, point) => sum + point[0], 0) / points.length,
    points.reduce((sum, point) => sum + point[1], 0) / points.length,
  ];
}

function floorPlanIssueReservedAreas(elements, edges, bounds, view) {
  const reserved = [];
  for (const element of elements) {
    if (!element.bboxMin || !element.bboxMax) continue;
    const box = floorPlanBox(element, bounds, view);
    if (["IfcWall", "IfcColumn"].includes(element.ifcType)) {
      reserved.push({ ...box, pad: 2 });
    } else if (element.ifcType === "IfcDoor") {
      reserved.push({ ...box, pad: 6 });
    } else if (["IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"].includes(element.ifcType)) {
      reserved.push({ ...box, pad: 3 });
    } else if (element.ifcType === "IfcSpace" && box.width > 48 && box.height > 18) {
      const labelWidth = Math.min(box.width - 6, Math.max(18, planElementName(element.name || element.label).length * 6));
      reserved.push({ x: box.x + (box.width - labelWidth) / 2, y: box.y + box.height / 2 - 7, width: labelWidth, height: 14, pad: 2 });
    }
  }
  for (const edge of edges.filter((item) => item.path?.length > 1)) {
    const points = edge.path.map((point) => floorPlanPoint(point, bounds, view));
    for (let index = 1; index < points.length; index++) {
      const start = points[index - 1];
      const end = points[index];
      const steps = Math.max(1, Math.ceil(Math.hypot(end[0] - start[0], end[1] - start[1]) / 14));
      for (let step = 0; step <= steps; step++) {
        const t = step / steps;
        reserved.push({
          x: start[0] + (end[0] - start[0]) * t,
          y: start[1] + (end[1] - start[1]) * t,
          radius: 3,
        });
      }
    }
  }
  return reserved;
}

function floorPlanIssueRadius(count, singleRadius) {
  if (count > 99) return 10;
  if (count > 9) return 8.4;
  if (count > 1) return 7.2;
  return singleRadius;
}

function floorPlanIssueLayout(markers, reserved, view) {
  const anchors = markers.map((marker) => ({ x: marker.anchor[0], y: marker.anchor[1], radius: 3 }));
  const placed = [];
  const ordered = markers.map((marker, index) => ({ ...marker, index }))
    .sort((a, b) => b.count - a.count || a.anchor[1] - b.anchor[1] || a.anchor[0] - b.anchor[0]);
  for (const marker of ordered) {
    const candidates = floorPlanIssueCandidates(marker, view);
    const markerObstacles = placed.map((item) => ({ x: item.point[0], y: item.point[1], radius: item.radius + 9 }));
    const openCandidates = candidates.filter((point) => markerObstacles.every((obstacle) => floorPlanIssueOverlap(point, marker.radius, obstacle) <= 0));
    const available = openCandidates.length ? openCandidates : candidates;
    const obstacles = reserved.concat(anchors, markerObstacles);
    let best = available[0];
    let bestScore = Number.POSITIVE_INFINITY;
    for (let index = 0; index < available.length; index++) {
      const point = available[index];
      const score = floorPlanIssuePositionScore(point, marker.radius, marker.anchor, obstacles) + index * 0.01;
      if (score < bestScore) {
        best = point;
        bestScore = score;
      }
    }
    placed.push({ ...marker, point: best });
  }
  return placed.sort((a, b) => a.index - b.index);
}

function floorPlanIssueCandidates(marker, view) {
  const directions = [[0, -1], [1, 0], [0, 1], [-1, 0], [0.71, -0.71], [0.71, 0.71], [-0.71, 0.71], [-0.71, -0.71]];
  const distances = [marker.radius + 11, marker.radius + 23, marker.radius + 35, marker.radius + 49];
  const margin = marker.radius + 4;
  return distances.flatMap((distance) => directions.map(([dx, dy]) => [
    _clampNumber(marker.anchor[0] + dx * distance, margin, view.width - margin),
    _clampNumber(marker.anchor[1] + dy * distance, margin, view.height - margin),
  ]));
}

function floorPlanIssuePositionScore(point, radius, anchor, obstacles) {
  let score = Math.hypot(point[0] - anchor[0], point[1] - anchor[1]) * 0.3;
  for (const obstacle of obstacles) {
    const overlap = floorPlanIssueOverlap(point, radius, obstacle);
    if (overlap > 0) score += overlap * overlap * 30;
  }
  return score;
}

function floorPlanIssueOverlap(point, radius, obstacle) {
  if (Number.isFinite(obstacle.width) && Number.isFinite(obstacle.height)) {
    const dx = Math.max(obstacle.x - point[0], 0, point[0] - obstacle.x - obstacle.width);
    const dy = Math.max(obstacle.y - point[1], 0, point[1] - obstacle.y - obstacle.height);
    return radius + (obstacle.pad || 0) - Math.hypot(dx, dy);
  }
  return radius + (obstacle.radius || 0) + 3 - Math.hypot(point[0] - obstacle.x, point[1] - obstacle.y);
}

function floorPlanIssueLeaderMarkup(anchor, point, radius) {
  const dx = point[0] - anchor[0];
  const dy = point[1] - anchor[1];
  const length = Math.hypot(dx, dy) || 1;
  const startX = anchor[0] + dx / length * 3;
  const startY = anchor[1] + dy / length * 3;
  const endX = point[0] - dx / length * (radius + 1);
  const endY = point[1] - dy / length * (radius + 1);
  return `<line class="planIssueLeaderHalo" x1="${startX.toFixed(2)}" y1="${startY.toFixed(2)}" x2="${endX.toFixed(2)}" y2="${endY.toFixed(2)}"></line>
    <line class="planIssueLeader" x1="${startX.toFixed(2)}" y1="${startY.toFixed(2)}" x2="${endX.toFixed(2)}" y2="${endY.toFixed(2)}"></line>`;
}

function routeDoorForEdge(edge, elementsByGuid) {
  return [elementsByGuid.get(edge.startGuid), elementsByGuid.get(edge.endGuid)].find((element) => element?.ifcType === "IfcDoor") || null;
}

function routeScreenLength(points) {
  let total = 0;
  for (let index = 1; index < points.length; index++) {
    total += Math.hypot(points[index][0] - points[index - 1][0], points[index][1] - points[index - 1][1]);
  }
  return total;
}

function floorPlanRouteIssueMarkup(edges, bounds, view, elementsByGuid, reserved = []) {
  const markers = routeIssueMarkers(edges, bounds, view, elementsByGuid);
  const clusters = routeIssueClusters(markers).map((cluster) => {
    const count = routeIssueClusterCount(cluster.items, elementsByGuid);
    return { cluster, count, anchor: cluster.screenPoint, radius: floorPlanIssueRadius(count, 5.6) };
  });
  return floorPlanIssueLayout(clusters, reserved, view).map((marker) => {
    const { cluster, count, anchor, point, radius } = marker;
    const [x, y] = point;
    const edgeIds = uniqueText(cluster.items.map((item) => item.edge.edgeId));
    const checks = routeCheckSummary(cluster.items.map((item) => item.edge));
    const title = count > 1 ? `${count} issues: ${checks}` : checks;
    return `<g class="routeIssueMarker" data-edge-id="${escapeHtml(edgeIds[0])}" data-edge-ids="${escapeHtml(edgeIds.join(","))}">
      <title>${escapeHtml(title)}</title>
      ${floorPlanIssueLeaderMarkup(anchor, point, radius)}
      <circle class="planIssueAnchorHalo" cx="${anchor[0].toFixed(2)}" cy="${anchor[1].toFixed(2)}" r="3.2"></circle>
      <circle class="planIssueAnchor" cx="${anchor[0].toFixed(2)}" cy="${anchor[1].toFixed(2)}" r="2"></circle>
      <circle class="routeIssueHit" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${(radius + 5).toFixed(2)}"></circle>
      <circle class="routeIssueHalo" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${(radius + 2.8).toFixed(2)}"></circle>
      <circle class="routeIssueDot" cx="${x.toFixed(2)}" cy="${y.toFixed(2)}" r="${radius.toFixed(2)}"></circle>
      ${count > 1 ? `<text class="routeIssueCount" x="${x.toFixed(2)}" y="${y.toFixed(2)}">${count}</text>` : routeIssueTickMarkup(x, y)}
    </g>`;
  }).join("");
}

function routeIssueClusters(markers) {
  const clusters = [];
  for (const marker of markers) {
    const cluster = clusters.find((item) => {
      const distance = Math.hypot(item.screenPoint[0] - marker.screenPoint[0], item.screenPoint[1] - marker.screenPoint[1]);
      return distance < (item.groupKeys.has(marker.groupKey) ? 18 : 7);
    });
    if (cluster) {
      cluster.items.push(marker);
      cluster.groupKeys.add(marker.groupKey);
      cluster.screenPoint = [
        cluster.items.reduce((sum, item) => sum + item.screenPoint[0], 0) / cluster.items.length,
        cluster.items.reduce((sum, item) => sum + item.screenPoint[1], 0) / cluster.items.length,
      ];
    } else {
      clusters.push({ screenPoint: marker.screenPoint, items: [marker], groupKeys: new Set([marker.groupKey]) });
    }
  }
  return clusters;
}

function routeIssueClusterCount(items, elementsByGuid) {
  return uniqueText(items.map((item) => routeIssueGroupKey(item.edge, elementsByGuid))).length;
}

function routeIssueGroupCount(edges, elementsByGuid) {
  return uniqueText(edges.filter(pathBlockingRoute).map((edge) => routeIssueGroupKey(edge, elementsByGuid))).length;
}

function routeIssueTickMarkup(x, y) {
  return `<line class="routeIssueTick" x1="${(x - 3.2).toFixed(2)}" y1="${(y - 3.2).toFixed(2)}" x2="${(x + 3.2).toFixed(2)}" y2="${(y + 3.2).toFixed(2)}"></line>
      <line class="routeIssueTick" x1="${(x + 3.2).toFixed(2)}" y1="${(y - 3.2).toFixed(2)}" x2="${(x - 3.2).toFixed(2)}" y2="${(y + 3.2).toFixed(2)}"></line>`;
}

function routeIssueMarkers(edges, bounds, view, elementsByGuid) {
  const groups = new Map();
  for (const edge of edges.filter(pathBlockingRoute)) {
    for (const location of routeIssueLocations(edge, elementsByGuid)) {
      if (!groups.has(location.key)) groups.set(location.key, { ...location, edges: [] });
      const group = groups.get(location.key);
      group.edges.push(edge);
      if (location.value != null && (group.value == null || location.value < group.value)) {
        group.value = location.value;
        group.point = location.point;
      }
    }
  }
  return [...groups.values()].flatMap((group) => group.edges.map((edge) => ({
    edge,
    point: group.point,
    screenPoint: floorPlanPoint(group.point, bounds, view),
    groupKey: group.key,
  })));
}

function routeIssueLocations(edge, elementsByGuid) {
  const reasons = edge.reasons || [];
  const locations = [];
  if (reasons.includes("unreachable")) {
    const door = routeEndpointElements(edge, elementsByGuid).find((element) => element.ifcType === "IfcDoor");
    if (door) locations.push({ key: `unreachable:${door.guid}`, point: elementPoint(door) });
  }
  if (reasons.includes("door_width")) {
    for (const door of narrowRouteDoors(edge, elementsByGuid)) {
      locations.push({ key: `door_width:${door.guid}`, point: elementPoint(door), value: doorAssessedWidth(door) });
    }
  }
  if (reasons.includes("wall_block")) {
    for (const wall of routeObstacleElements(edge, elementsByGuid, isWallType)) {
      locations.push({ key: `wall_block:${wall.guid}`, point: routeIssuePointNearElement(edge, wall) });
    }
  }
  if (reasons.includes("stair_block")) {
    for (const stair of routeObstacleElements(edge, elementsByGuid, isStairType)) {
      locations.push({ key: `stair_block:${stairGroupKey(stair)}`, point: routeIssuePointNearElement(edge, stair) });
    }
  }
  if (reasons.includes("ramp_slope") || reasons.includes("ramp_width")) {
    for (const ramp of routeObstacleElements(edge, elementsByGuid, isRampType)) {
      locations.push({ key: `ramp:${ramp.guid}`, point: routeIssuePointNearElement(edge, ramp) });
    }
  }
  if (reasons.includes("turning_space")) {
    const point = routeMeasurementPoint(edge, "routeTurningPoint") || routeTurnPoint(edge) || routeMidpoint(edge);
    if (point) {
      locations.push({
        key: `turning_space:${edge.viaSpaceGuid || edge.edgeId}:${Math.round(point[0] * 2)}:${Math.round(point[1] * 2)}`,
        point,
        value: Number(edge.measurements?.routeTurningSpaceM),
      });
    }
  }
  if (reasons.includes("route_width")) {
    const point = routeMeasurementPoint(edge, "routeClearWidthPoint") || routeMidpoint(edge);
    if (point) {
      locations.push({
        key: `route_width:${edge.viaSpaceGuid || edge.edgeId}`,
        point,
        value: Number(edge.measurements?.routeClearWidthM),
      });
    }
  }
  if (!locations.length) {
    locations.push({ key: `route:${edge.edgeId}`, point: routeMidpoint(edge) });
  }
  return locations.filter((location) => location.point);
}

function routeIssuePointNearElement(edge, element) {
  const samples = routePathSamples(edge.path || []);
  const point = samples.find((item) => pointInElement(item, element, 0.75));
  return point || elementPoint(element) || routeMidpoint(edge);
}

function pointInElement(point, element, pad = 0) {
  return point &&
    element?.bboxMin &&
    element?.bboxMax &&
    point[0] >= element.bboxMin[0] - pad &&
    point[0] <= element.bboxMax[0] + pad &&
    point[1] >= element.bboxMin[1] - pad &&
    point[1] <= element.bboxMax[1] + pad;
}

function elementPoint(element) {
  if (element?.center) return element.center;
  if (element?.bboxMin && element?.bboxMax) {
    return [
      (element.bboxMin[0] + element.bboxMax[0]) / 2,
      (element.bboxMin[1] + element.bboxMax[1]) / 2,
      (element.bboxMin[2] + element.bboxMax[2]) / 2,
    ];
  }
  return null;
}

function routeTurnPoint(edge) {
  const path = edge.path || [];
  if (path.length < 3) return null;
  for (let index = 1; index < path.length - 1; index++) {
    const prev = path[index - 1];
    const point = path[index];
    const next = path[index + 1];
    const a = [point[0] - prev[0], point[1] - prev[1]];
    const b = [next[0] - point[0], next[1] - point[1]];
    if (Math.abs(a[0] * b[1] - a[1] * b[0]) > 0.05) return point;
  }
  return null;
}

function routeMidpoint(edge) {
  const path = edge.path || [];
  if (!path.length) return null;
  return path[Math.floor(path.length / 2)];
}

function routeMeasurementPoint(edge, key) {
  const x = Number(edge.measurements?.[`${key}X`]);
  const y = Number(edge.measurements?.[`${key}Y`]);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const zValue = edge.measurements?.[`${key}Z`];
  const z = zValue == null ? Number(routeMidpoint(edge)?.[2] || 0) : Number(zValue);
  return [x, y, Number.isFinite(z) ? z : 0];
}

function uniqueRoutePoints(points) {
  const result = [];
  for (const point of points) {
    if (!result.some((item) => Math.hypot(item[0] - point[0], item[1] - point[1]) < 0.35)) {
      result.push(point);
    }
  }
  return result;
}

function pathBlockingRoute(edge) {
  const pathReasons = new Set(["door_width", "route_width", "turning_space", "stair_block", "ramp_slope", "ramp_width", "unreachable"]);
  return edge.status === "fail" && (edge.reasons || []).some((reason) => pathReasons.has(reason));
}

function floorPlanSpaceFillClass(element) {
  return element.extra?.isCorridorLike ? "planSpace corridorSpace" : "planSpace";
}

function floorPlanSpaceBorderClass(element, issueCounts) {
  return element.extra?.isCorridorLike ? "planSpaceBorder corridorSpaceBorder" : "planSpaceBorder";
}

function floorPlanDoorClass(element, issueCounts) {
  return "planDoor";
}

function planIssueCounts() {
  const counts = new Map();
  for (const issue of buildingIssues()) {
    counts.set(issue.element_guid, (counts.get(issue.element_guid) || 0) + 1);
  }
  return counts;
}

function buildingIssues() {
  const routeRules = new Set([
    "route_door_width",
    "route_width",
    "route_turning_space",
    "route_wall_block",
    "route_unreachable",
    "stair_block",
    "route_ramp_slope",
    "route_ramp_width",
  ]);
  return (appData.issues || []).filter((issue) => !routeRules.has(issue.rule_id));
}

function showPlanElement(guid, elementsByGuid, regionId = null, areaId = null) {
  const element = elementsByGuid.get(guid);
  if (!element) return;
  const region = (appData.issueRegions || []).find((item) => item.region_id === regionId) || null;
  const area = issueRegionAreas(region).find((item) => item.area_id === areaId) || null;
  const selectedRegion = area ? { ...region, ...area } : region;
  const issues = planRouteMode === "issues"
    ? buildingIssues().filter((issue) => issue.element_guid === guid && (!region || issue.issue_id === region.issue_id))
    : [];
  const affectedRoutes = planRouteMode === "issues" ? planElementAffectedRoutes(element, issues, elementsByGuid) : [];
  if (!issues.length) {
    const rows = [["Type", planElementType(element)]];
    const width = element.ifcType === "IfcDoor" ? doorAssessedWidth(element) : 0;
    if (width) rows.push(["Clear width", valueWithUnit(width, "m")]);
    document.querySelector("#planDetails").innerHTML = `<h3>${escapeHtml(elementName(element))}</h3>${planRows(rows)}`;
    return;
  }
  const checks = issues.map((issue) => ({
    label: sentenceText(planReasonText(issue.rule_id)),
    comparison: selectedRegion && issue.issue_id === selectedRegion.issue_id
      ? comparisonText(selectedRegion.rule_id, selectedRegion.measured, selectedRegion.required, selectedRegion.unit)
      : issueComparisonText(issue),
  }));
  const rows = planIssueDetailRows([elementName(element)], checks);
  if (region?.area_count > 1) rows.push(["Affected areas", String(region.area_count)]);
  if (affectedRoutes.length) rows.push(["Affected routes", String(affectedRoutes.length)]);
  document.querySelector("#planDetails").innerHTML = `<h3>${checks.length === 1 ? "Accessibility issue" : "Accessibility issues"}</h3>${planRows(rows)}`;
}

function planElementType(element) {
  if (element.ifcType === "IfcSpace") return element.extra?.isCorridorLike ? "Corridor" : "Room";
  return {
    IfcDoor: "Door",
    IfcWall: "Wall",
    IfcColumn: "Column",
    IfcStair: "Stair",
    IfcStairFlight: "Stair",
    IfcRamp: "Ramp",
    IfcRampFlight: "Ramp",
  }[element.ifcType] || String(element.ifcType || "Element").replace(/^Ifc/, "");
}

function planElementAffectedRoutes(element, issues, elementsByGuid) {
  const reasonByRule = {
    door_width: "door_width",
    corridor_width: "route_width",
    ramp_slope: "ramp_slope",
    ramp_width: "ramp_width",
  };
  const reasons = new Set(issues.map((issue) => reasonByRule[issue.rule_id]).filter(Boolean));
  if (!reasons.size) return [];
  return (appData.routeEdges || []).filter((edge) => {
    if (edge.status !== "fail" || !(edge.reasons || []).some((reason) => reasons.has(reason))) return false;
    if (element.ifcType === "IfcDoor") return edge.startGuid === element.guid || edge.endGuid === element.guid;
    if (element.ifcType === "IfcSpace") return edge.viaSpaceGuid === element.guid;
    if (isRampType(element.ifcType)) return routeIntersectsElement(edge, element);
    return edge.startGuid === element.guid || edge.endGuid === element.guid || edge.viaSpaceGuid === element.guid;
  });
}

function showPlanRoute(edgeId, edgesById, elementsByGuid) {
  const edge = edgesById.get(edgeId);
  if (!edge) return;
  const start = elementsByGuid.get(edge.startGuid);
  const end = elementsByGuid.get(edge.endGuid);
  const connection = `${routeEndpointName(edge.startGuid, start)} to ${routeEndpointName(edge.endGuid, end)}`;
  if (edge.status === "pass") {
    const rows = [["Connection", connection]];
    if (edge.viaSpaceLabel) rows.push(["Through", planElementName(edge.viaSpaceLabel)]);
    rows.push(["Distance", valueWithUnit(edge.distanceM, "m")]);
    document.querySelector("#planDetails").innerHTML = `<h3>Accessible route</h3>${planRows(rows)}`;
    return;
  }
  const checks = routeIssueChecks([edge]);
  const rows = planIssueDetailRows(routeIssueLocationNames(edge, elementsByGuid), checks);
  rows.push(["Connection", connection], ["Alternative", routeAlternativeText(edge)]);
  document.querySelector("#planDetails").innerHTML = `<h3>${checks.length === 1 ? "Accessibility issue" : "Accessibility issues"}</h3>${planRows(rows)}`;
}

function showPlanRouteGroup(edgeIds, edgesById, elementsByGuid) {
  const edges = edgeIds.map((edgeId) => edgesById.get(edgeId)).filter(Boolean);
  if (!edges.length) return;
  const checks = routeIssueChecks(edges);
  const rows = planIssueDetailRows(uniqueText(edges.flatMap((edge) => routeIssueLocationNames(edge, elementsByGuid))), checks);
  rows.push(["Affected routes", String(edges.length)]);
  document.querySelector("#planDetails").innerHTML = `<h3>${checks.length === 1 ? "Accessibility issue" : "Accessibility issues"}</h3>${planRows(rows)}`;
}

function planIssueDetailRows(locations, checks) {
  return [
    ["Location", uniqueText(locations).join("; ") || "Not identified"],
    ["Issue", uniqueText(checks.map((check) => check.label)).join("; ")],
    ["Check", uniqueText(checks.map((check) => check.comparison)).join("; ")],
  ];
}

function routeIssueGroupKey(edge, elementsByGuid) {
  const reasons = edge.reasons || [];
  if (reasons.includes("unreachable")) {
    const door = routeEndpointElements(edge, elementsByGuid).find((element) => element.ifcType === "IfcDoor");
    if (door) return `unreachable:${door.guid}`;
  }
  if (reasons.includes("stair_block")) {
    const stair = routeEndpointElements(edge, elementsByGuid).find((element) => isStairType(element.ifcType)) || routeObstacleElements(edge, elementsByGuid, isStairType)[0];
    if (stair) return `stair:${stairGroupKey(stair)}`;
  }
  if (reasons.includes("ramp_slope") || reasons.includes("ramp_width")) {
    const ramp = routeEndpointElements(edge, elementsByGuid).find((element) => isRampType(element.ifcType)) || routeObstacleElements(edge, elementsByGuid, isRampType)[0];
    if (ramp) return `ramp:${ramp.guid}`;
  }
  if (reasons.includes("wall_block")) {
    const wall = routeObstacleElements(edge, elementsByGuid, isWallType)[0];
    if (wall) return `wall:${wall.guid}`;
  }
  if (reasons.includes("door_width")) {
    const doors = narrowRouteDoors(edge, elementsByGuid).map((door) => door.guid).join(",");
    if (doors) return `door:${doors}`;
  }
  if (reasons.includes("route_width") || reasons.includes("turning_space")) {
    return `space:${edge.viaSpaceGuid || edge.edgeId}`;
  }
  return edge.edgeId;
}

function routeEndpointElements(edge, elementsByGuid) {
  return [elementsByGuid.get(edge.startGuid), elementsByGuid.get(edge.endGuid)].filter(Boolean);
}

function routeIssueLocationNames(edge, elementsByGuid) {
  const reasons = edge.reasons || [];
  const locations = [];
  if (reasons.includes("unreachable")) {
    const door = routeEndpointElements(edge, elementsByGuid).find((element) => element.ifcType === "IfcDoor");
    if (door) locations.push(elementName(door));
  }
  if (reasons.includes("door_width")) {
    const doors = narrowRouteDoors(edge, elementsByGuid);
    locations.push(...(doors.length ? doors : routeEndpointElements(edge, elementsByGuid).filter((element) => element.ifcType === "IfcDoor")).map(elementName));
  }
  if (reasons.includes("route_width") || reasons.includes("turning_space")) {
    if (edge.viaSpaceLabel) locations.push(planElementName(edge.viaSpaceLabel));
  }
  if (reasons.includes("wall_block")) {
    const walls = routeObstacleElements(edge, elementsByGuid, isWallType);
    locations.push(...walls.map(elementName));
  }
  if (reasons.includes("stair_block")) {
    const stairs = routeObstacleElements(edge, elementsByGuid, isStairType);
    locations.push(...stairs.map(elementName));
  }
  if (reasons.includes("ramp_slope") || reasons.includes("ramp_width")) {
    const ramps = routeObstacleElements(edge, elementsByGuid, isRampType);
    locations.push(...ramps.map(elementName));
  }
  if (!locations.length && edge.viaSpaceLabel) locations.push(planElementName(edge.viaSpaceLabel));
  return uniqueText(locations);
}

function narrowRouteDoors(edge, elementsByGuid) {
  const limit = routeDoorWidthLimit();
  return [elementsByGuid.get(edge.startGuid), elementsByGuid.get(edge.endGuid)]
    .filter((element) => element?.ifcType === "IfcDoor" && doorAssessedWidth(element) && doorAssessedWidth(element) < limit);
}

function routeAlternativeText(edge) {
  const route = accessibleRouteBetween(edge.startGuid, edge.endGuid) || accessibleRouteBetween(edge.endGuid, edge.startGuid);
  if (route) return `Available (${valueWithUnit(route.distance_m, "m")})`;
  return "None found";
}

function accessibleRouteBetween(startGuid, endGuid) {
  return (appData.accessibleRoutesByDoor?.[startGuid] || []).find((route) => route.target_guid === endGuid) || null;
}

function routeObstacleElements(edge, elementsByGuid, predicate) {
  return (appData.elements || [])
    .filter((element) => predicate(element.ifcType) && element.bboxMin && element.bboxMax)
    .filter((element) => routeIntersectsElement(edge, element))
    .sort((a, b) => elementName(a).localeCompare(elementName(b)));
}

function isWallType(type) {
  return type === "IfcWall" || type === "IfcColumn";
}

function routeIntersectsElement(edge, element) {
  const pad = 0.75;
  for (const point of routePathSamples(edge.path || [])) {
    if (
      point[0] >= element.bboxMin[0] - pad &&
      point[0] <= element.bboxMax[0] + pad &&
      point[1] >= element.bboxMin[1] - pad &&
      point[1] <= element.bboxMax[1] + pad
    ) {
      return true;
    }
  }
  return false;
}

function routePathSamples(path) {
  if (path.length < 2) return path;
  const points = [];
  for (let index = 1; index < path.length; index++) {
    const start = path[index - 1];
    const end = path[index];
    const count = Math.max(1, Math.ceil(Math.hypot(end[0] - start[0], end[1] - start[1]) / 0.5));
    for (let step = 0; step <= count; step++) {
      const t = step / count;
      points.push([
        start[0] + (end[0] - start[0]) * t,
        start[1] + (end[1] - start[1]) * t,
        start[2] + (end[2] - start[2]) * t,
      ]);
    }
  }
  return points;
}

function doorAssessedWidth(door) {
  return Number(door?.extra?.derivedDoorWidthM || 0);
}

function routeDoorWidthLimit() {
  return Number(appData.rules?.route_door_width_m || appData.rules?.door_width_m || 0.9);
}

function elementName(element) {
  const text = planElementName(element?.name || element?.label || element?.guid || "unknown");
  if (isStairType(element?.ifcType)) return text.replace(/\s+Run\s+\d+\b/i, "");
  return text;
}

function stairGroupKey(element) {
  return elementName(element).toLowerCase();
}

function routeEndpointName(guid, element) {
  if (element) return elementName(element);
  if (String(guid || "").startsWith("route-node:")) return "Route network";
  return planElementName(guid || "unknown");
}

function uniqueText(items) {
  return [...new Set(items.filter(Boolean))];
}

function planRows(rows) {
  return `<dl class="planRows">${rows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "missing")}</dd>`).join("")}</dl>`;
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

  button.onclick = ask;
  input.onkeydown = (event) => {
    if (event.key === "Enter") ask();
  };
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
  if (!viewerResizeReady) {
    window.addEventListener("resize", resizeRenderer);
    viewerResizeReady = true;
  }
  setupViewerToolbar();
  resizeRenderer();
  if (!viewerAnimationStarted) {
    viewerAnimationStarted = true;
    animate();
  }
}

function setupViewerToolbar() {
  const modeButtons = [
    ["modeOrbit", () => setControlMode("orbit")],
    ["modePan", () => setControlMode("pan")],
    ["modeSide", () => setControlMode("side")],
  ];
  for (const [id, handler] of modeButtons) {
    const button = document.querySelector(`#${id}`);
    if (!button) continue;
    button.onclick = () => {
      document.querySelectorAll(".segmented button").forEach((button) => button.classList.remove("active"));
      document.querySelector(`#${id}`).classList.add("active");
      handler();
    };
  }
  const fit = document.querySelector("#viewFit");
  const top = document.querySelector("#viewTop");
  const doors = document.querySelector("#viewDoors");
  const routeOnly = document.querySelector("#toggleRouteOnly");
  if (fit) fit.onclick = () => loadedModel && frameScene(loadedModel);
  if (top) top.onclick = setTopView;
  if (doors) {
    doors.onclick = () => {
      doorMarkerGroup.visible = !doorMarkerGroup.visible;
      doors.textContent = doorMarkerGroup.visible ? "Hide Doors" : "Show Doors";
    };
  }
  if (routeOnly) routeOnly.onchange = (event) => setRouteFocus(event.target.checked);
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
  const runButton = document.querySelector("#simRun");
  const speedInput = document.querySelector("#simSpeed");
  const playButton = document.querySelector("#simPlayPause");
  const resetButton = document.querySelector("#simReset");
  if (runButton) runButton.onclick = () => loadSimulationScenario("floor");
  if (speedInput) {
    speedInput.oninput = (event) => {
      simSpeed = Number(event.target.value) || 0.85;
    };
  }
  if (playButton) {
    playButton.onclick = (event) => {
      simPlaying = !simPlaying;
      event.currentTarget.textContent = simPlaying ? "Pause" : "Play";
    };
  }
  if (resetButton) {
    resetButton.onclick = () => {
      simProgress = 0;
      simPlaying = true;
      document.querySelector("#simPlayPause").textContent = "Pause";
      updateSimulationStatus(false);
    };
  }
  if (!simulationResizeReady) {
    window.addEventListener("resize", resizeSimulationRenderer);
    simulationResizeReady = true;
  }
  loadSimulationScenario("floor");
  resizeSimulationRenderer();
  if (!simulationAnimationStarted) {
    simulationAnimationStarted = true;
    animateSimulation();
  }
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
  return value !== null && value !== "" && Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} ${unit}` : "missing";
}

function issueCheckText(issue) {
  return `${issue.short_text || issue.details || planReasonText(issue.rule_id)}: ${issueComparisonText(issue)}`;
}

function issueComparisonText(issue) {
  return comparisonText(issue.rule_id, issue.measured, issue.required, issue.unit);
}

function routeCheckSummary(edges) {
  return routeIssueChecks(edges).map((check) => `${check.label}: ${check.comparison}`).join("; ") || "none";
}

function routeIssueChecks(edges) {
  const reasons = uniqueText(edges.flatMap((edge) => edge.reasons || []));
  return reasons.map((reason) => {
    const values = edges
      .filter((edge) => edge.reasons?.includes(reason))
      .map((edge) => routeCheckValues(edge, reason));
    const measured = values.filter((value) => value[0] !== null && value[0] !== undefined && value[0] !== "" && Number.isFinite(Number(value[0])));
    const selected = measured.reduce((best, value) => {
      if (!best) return value;
      if (reason === "ramp_slope") return Number(value[0]) > Number(best[0]) ? value : best;
      return Number(value[0]) < Number(best[0]) ? value : best;
    }, null) || values[0] || [null, null, ""];
    return {
      label: sentenceText(planReasonText(reason)),
      comparison: comparisonText(reason, selected[0], selected[1], selected[2]),
    };
  });
}

function routeCheckValues(edge, reason) {
  const rules = appData.rules || {};
  return {
    door_width: [edge.measurements?.routeDoorWidthMinM, rules.route_door_width_m ?? rules.door_width_m, "m"],
    route_width: [edge.measurements?.routeClearWidthM, rules.corridor_width_m, "m"],
    turning_space: [edge.measurements?.routeTurningSpaceM, rules.turning_space_m, "m"],
    wall_block: [edge.measurements?.routeHitsWall ?? true, false, "bool"],
    stair_block: [edge.measurements?.routeHitsStair ?? true, false, "bool"],
    ramp_slope: [edge.measurements?.routeRampSlopePercent, rules.ramp_slope_percent, "%"],
    ramp_width: [edge.measurements?.routeRampUsableWidthM, rules.ramp_width_m, "m"],
    unreachable: [edge.measurements?.routeReachable ?? false, true, "bool"],
  }[reason] || [null, null, ""];
}

function comparisonText(ruleId, measured, required, unit) {
  if (measured === null || measured === "" || measured === undefined) {
    if (required === null || required === "" || required === undefined) return "measurement missing";
    const operator = String(ruleId).includes("ramp_slope") ? "<=" : ">=";
    return `missing; required ${operator} ${valueWithUnit(required, unit)}`;
  }
  if (unit === "bool") {
    const measuredText = measured === true || Number(measured) === 1 ? "yes" : "no";
    const requiredText = required === true || Number(required) === 1 ? "yes" : "no";
    return `${measuredText} != ${requiredText}`;
  }
  if (required === null || required === "" || required === undefined) return valueWithUnit(measured, unit);
  const operator = String(ruleId).includes("ramp_slope") ? ">" : "<";
  return `${valueWithUnit(measured, unit)} ${operator} ${valueWithUnit(required, unit)}`;
}

function displaySource(value) {
  return {
    "IfcOpenShell geometry": "IFC model geometry",
    "IfcOpenShell": "IFC model data",
  }[value] || value;
}

function formatDate(value) {
  const date = new Date(Number(value || 0) * 1000);
  return Number.isFinite(date.getTime()) && Number(value) ? date.toLocaleString() : "";
}

function shortText(value, limit) {
  const text = String(value ?? "");
  return text.length <= limit ? text : `${text.slice(0, limit - 3)}...`;
}

function planElementName(value) {
  let text = String(value || "");
  text = text.replace(/^Ifc[A-Za-z0-9]+\s+/i, "");
  const parts = text.split(":").map((part) => part.trim()).filter(Boolean);
  if (parts.length > 1) {
    const unique = [];
    for (const part of parts) {
      if (!unique.includes(part)) unique.push(part);
    }
    text = unique.join(" ");
  }
  text = text.replace(/\bM_/g, "").replace(/_/g, " ");
  return text;
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

function sentenceText(value) {
  const text = String(value || "");
  return text ? `${text[0].toUpperCase()}${text.slice(1)}` : "";
}

function planReasonText(code) {
  return code === "wall_block" ? "wall blocks route" : reasonText(code);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[ch]);
}

function cssEscape(value) {
  if (window.CSS?.escape) return CSS.escape(String(value));
  return String(value).replace(/["\\]/g, "\\$&");
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
