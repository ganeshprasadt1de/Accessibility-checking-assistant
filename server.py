from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from backend.short_explainer import explain_question

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
PACKAGE = ROOT / "output" / "app_package"
MODEL_HOME = Path(os.environ.get("WHEELCHAIR_MODEL_HOME", ROOT / "output" / "model_library"))
MODEL_FILE = MODEL_HOME / "models.json"
ACTIVE_MODEL_FILE = MODEL_HOME / "active_model.txt"
OLLAMA_AVAILABLE = False
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_PORT = 11434
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "").strip()
OLLAMA_LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean = unquote(parsed.path).lstrip("/")
        if parsed.path.startswith("/files/"):
            return str(package_file(clean.split("/", 1)[1]))
        if parsed.path.startswith("/api/"):
            return str(FRONTEND / "index.html")
        target = FRONTEND / (clean or "index.html")
        if target.is_dir():
            target = target / "index.html"
        return str(target)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/models":
            self._json(models_response())
            return
        if parsed.path.startswith("/api/data"):
            self._send_file(current_package() / "app_data.json", "application/json")
            return
        if parsed.path.startswith("/api/route/"):
            guid = unquote(parsed.path.rsplit("/", 1)[-1])
            data_path = current_package() / "app_data.json"
            if not data_path.exists():
                self._json({"error": "Run preprocess.py first."}, 404)
                return
            data = json.loads(data_path.read_text(encoding="utf-8"))
            self._json({"startGuid": guid, "routes": data.get("routesByDoor", {}).get(guid, [])})
            return
        if parsed.path.startswith("/files/"):
            relative = unquote(parsed.path.split("/", 2)[2])
            path = package_file(relative)
            content_type = "model/gltf-binary" if path.suffix.lower() == ".glb" else "application/octet-stream"
            self._send_file(path, content_type)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/ollama/restart":
            if not local_maintenance_request(self):
                self._json({"error": "Ollama maintenance requires a same-origin request from this computer."}, 403)
                return
            result, status = restart_ollama()
            self._json(result, status)
            return
        if parsed.path == "/api/models/upload":
            self._json(upload_model(self))
            return
        model_action = model_path_action(parsed.path)
        if model_action:
            model_id, action = model_action
            if action == "rename":
                self._json(rename_model(model_id, self._body_json()))
                return
            if action == "generate":
                self._json(start_model_generation(model_id))
                return
            if action == "select":
                self._json(select_model(model_id))
                return
        if parsed.path.startswith("/api/assistant"):
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "Question could not be read."}, 400)
                return

            data_path = current_package() / "app_data.json"
            if not data_path.exists():
                self._json({"error": "Run preprocess.py first."}, 404)
                return

            question = str(payload.get("question", "")).strip() or "Explain the checker result."
            data = json.loads(data_path.read_text(encoding="utf-8"))
            if not OLLAMA_AVAILABLE:
                self._json(shacl_report_response(data))
                return
            try:
                self._json(explain_question(question, assistant_context(data), model=OLLAMA_MODEL, host=OLLAMA_HOST))
            except Exception as exc:
                self._json({"error": f"Ollama request failed: {exc}"}, 503)
            return
        self._json({"error": "Unknown endpoint."}, 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "models"]:
            self._json(delete_model(parts[2]))
            return
        self._json({"error": "Unknown endpoint."}, 404)

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._json({"error": f"Missing file: {path.name}. Run preprocess.py first."}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        self._send_no_cache_headers()
        super().end_headers()

    def _send_no_cache_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")


def model_path_action(path: str) -> tuple[str, str] | None:
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[:2] == ["api", "models"]:
        return parts[2], parts[3]
    return None


def local_maintenance_request(handler: Handler) -> bool:
    if handler.client_address[0] not in {"127.0.0.1", "::1"}:
        return False
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    origin = handler.headers.get("Origin", "").strip()
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == handler.server.server_port


def current_package() -> Path:
    model = active_model()
    if model and model.get("status") == "complete":
        package = Path(model.get("packagePath", ""))
        if (package / "app_data.json").exists():
            return package
    return PACKAGE


def package_file(relative: str) -> Path:
    root = current_package().resolve()
    target = (root / relative).resolve()
    if root not in target.parents and target != root:
        return root / "__invalid__"
    return target


def load_model_state() -> dict:
    if not MODEL_FILE.exists():
        return {"models": []}
    try:
        data = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"models": []}
    if not isinstance(data.get("models"), list):
        data["models"] = []
    return data


def save_model_state(data: dict) -> None:
    MODEL_HOME.mkdir(parents=True, exist_ok=True)
    temp = MODEL_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(MODEL_FILE)


def active_model_id() -> str | None:
    if not ACTIVE_MODEL_FILE.exists():
        return None
    text = ACTIVE_MODEL_FILE.read_text(encoding="utf-8").strip()
    return text or None


def active_model() -> dict | None:
    active_id = active_model_id()
    if not active_id:
        return None
    with MODEL_LOCK:
        data = load_model_state()
        return next((model for model in data["models"] if model.get("id") == active_id), None)


def models_response() -> dict:
    with MODEL_LOCK:
        data = load_model_state()
        return {
            "models": sorted(data["models"], key=lambda item: int(item.get("order", 0))),
            "activeModelId": active_model_id(),
            "defaultPackageAvailable": (PACKAGE / "app_data.json").exists(),
        }


def upload_model(handler: Handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw_name = unquote(handler.headers.get("X-File-Name", "")).strip()
    if not raw_name:
        return {"error": "Missing file name."}
    if not raw_name.lower().endswith(".ifc"):
        return {"error": "Only IFC files can be uploaded."}
    body = handler.rfile.read(length)
    if not body:
        return {"error": "Uploaded file is empty."}
    now = int(time.time())
    stem = safe_name(Path(raw_name).stem) or "model"
    model_id = f"{now}-{uuid.uuid4().hex[:8]}-{stem}"
    ifc_dir = MODEL_HOME / "ifc"
    ifc_dir.mkdir(parents=True, exist_ok=True)
    ifc_path = ifc_dir / f"{model_id}.ifc"
    ifc_path.write_bytes(body)
    with MODEL_LOCK:
        data = load_model_state()
        order = max([int(model.get("order", 0)) for model in data["models"]] or [0]) + 1
        model = {
            "id": model_id,
            "name": Path(raw_name).stem,
            "fileName": raw_name,
            "ifcPath": str(ifc_path),
            "packagePath": str(MODEL_HOME / "packages" / model_id),
            "status": "uploaded",
            "progress": 0,
            "stage": "Uploaded",
            "message": "Ready to generate package.",
            "order": order,
            "createdAt": now,
            "updatedAt": now,
            "size": len(body),
            "summary": {},
            "log": [],
        }
        data["models"].append(model)
        save_model_state(data)
    return {"model": model}


def rename_model(model_id: str, payload: dict) -> dict:
    name = str(payload.get("name", "")).strip()
    if not name:
        return {"error": "Name cannot be empty."}
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return {"error": "Model was not found."}
        model["name"] = name
        model["updatedAt"] = int(time.time())
        save_model_state(data)
        return {"model": model}


def delete_model(model_id: str) -> dict:
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return {"error": "Model was not found."}
        if model.get("status") == "running":
            return {"error": "Model is still generating."}
        data["models"] = [item for item in data["models"] if item.get("id") != model_id]
        save_model_state(data)
        if active_model_id() == model_id and ACTIVE_MODEL_FILE.exists():
            ACTIVE_MODEL_FILE.unlink()
    path = Path(model.get("ifcPath", ""))
    if path.exists():
        path.unlink()
    package = Path(model.get("packagePath", ""))
    if package.exists():
        shutil.rmtree(package)
    return {"deleted": model_id}


def select_model(model_id: str) -> dict:
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return {"error": "Model was not found."}
        if model.get("status") != "complete":
            return {"error": "Only completed models can be opened."}
        if not (Path(model.get("packagePath", "")) / "app_data.json").exists():
            return {"error": "Package data is missing. Regenerate this model."}
        MODEL_HOME.mkdir(parents=True, exist_ok=True)
        ACTIVE_MODEL_FILE.write_text(model_id, encoding="utf-8")
        return {"activeModelId": model_id}


def start_model_generation(model_id: str) -> dict:
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return {"error": "Model was not found."}
        if model.get("status") == "running":
            return {"error": "Model is already generating."}
        model["status"] = "running"
        model["progress"] = 3
        model["stage"] = "Queued"
        model["message"] = "Starting preprocessing."
        model["log"] = []
        model["updatedAt"] = int(time.time())
        save_model_state(data)
    thread = threading.Thread(target=run_model_generation, args=(model_id,), daemon=True)
    thread.start()
    return {"modelId": model_id, "status": "running"}


def run_model_generation(model_id: str) -> None:
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return
        ifc_path = Path(model["ifcPath"])
        package = Path(model["packagePath"])
    if package.exists():
        shutil.rmtree(package)
    update_model_progress(model_id, 8, "Reading IFC", "Reading model geometry.")
    command = [
        sys.executable,
        str(ROOT / "preprocess.py"),
        "--ifc",
        str(ifc_path),
        "--output",
        str(package),
        "--save-bin",
    ]
    zip_path = ifctolbd_zip()
    exe_path = ifctolbd_exe()
    if exe_path:
        command.extend(["--ifctolbd-exe", str(exe_path)])
    if zip_path:
        command.extend(["--ifctolbd-zip", str(zip_path)])
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            update_model_from_log(model_id, line.rstrip())
        code = process.wait()
    except Exception as exc:
        cleanup_package_work(package)
        fail_model_generation(model_id, str(exc))
        return
    cleanup_package_work(package)
    if code != 0:
        fail_model_generation(model_id, f"preprocess.py exited with code {code}")
        return
    data_path = package / "app_data.json"
    if not data_path.exists():
        fail_model_generation(model_id, "Package data was not created.")
        return
    try:
        summary = json.loads(data_path.read_text(encoding="utf-8")).get("summary", {})
    except json.JSONDecodeError:
        summary = {}
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if model:
            ifctolbd_note = str(summary.get("ifctolbd", ""))
            used_fallback = "ifctolbd failed" in ifctolbd_note.lower()
            model["status"] = "complete"
            model["progress"] = 100
            model["stage"] = "Complete"
            model["message"] = "Package is ready. IFCtoLBD failed; geometry-only data was used." if used_fallback else "Package is ready."
            model["summary"] = summary
            model["updatedAt"] = int(time.time())
            save_model_state(data)


def cleanup_package_work(package: Path) -> None:
    root = package.resolve()
    work = (root / "_work").resolve()
    if not work.exists():
        return
    if root not in work.parents:
        return
    shutil.rmtree(work, ignore_errors=True)


def update_model_from_log(model_id: str, line: str) -> None:
    if not line:
        return
    progress, stage = progress_from_line(line)
    update_model_progress(model_id, progress, stage, line)


def update_model_progress(model_id: str, progress: int, stage: str, message: str) -> None:
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return
        model["progress"] = max(int(model.get("progress", 0)), progress)
        model["stage"] = stage
        model["message"] = message
        model["updatedAt"] = int(time.time())
        log = list(model.get("log", []))
        log.append(message)
        model["log"] = log[-80:]
        save_model_state(data)


def fail_model_generation(model_id: str, message: str) -> None:
    with MODEL_LOCK:
        data = load_model_state()
        model = find_model(data, model_id)
        if not model:
            return
        model["status"] = "failed"
        model["stage"] = "Failed"
        model["message"] = message
        model["updatedAt"] = int(time.time())
        log = list(model.get("log", []))
        log.append(message)
        model["log"] = log[-80:]
        save_model_state(data)


def progress_from_line(line: str) -> tuple[int, str]:
    lower = line.lower()
    if lower.startswith("reading ifc"):
        return 12, "Reading IFC"
    if lower.startswith("extracted elements"):
        return 24, "Extracting geometry"
    if "conversion" in lower or "ifctolbd" in lower or "target is" in lower:
        return 46, "Converting IFC to RDF"
    if "raw graph created" in lower:
        return 62, "Preparing rule graph"
    if "routes:" in lower:
        return 94, "Writing package"
    if "wrote package" in lower:
        return 98, "Writing package"
    return 36, "Running preprocessing"


def find_model(data: dict, model_id: str) -> dict | None:
    return next((model for model in data["models"] if model.get("id") == model_id), None)


def safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip().lower())
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-_")


def ifctolbd_zip() -> Path | None:
    explicit = os.environ.get("IFCTOLBD_ZIP")
    candidates = [Path(explicit)] if explicit else []
    candidates.extend([ROOT / "IFCtoLBD-master.zip", ROOT.parent / "IFCtoLBD-master.zip"])
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate.resolve()
    return None


def ifctolbd_exe() -> Path | None:
    explicit = os.environ.get("IFCTOLBD_EXE")
    candidates = [Path(explicit)] if explicit else []
    candidates.extend([ROOT / "IFCtoLBDConverter_CLI.exe", ROOT.parent / "IFCtoLBDConverter_CLI.exe"])
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate.resolve()
    return None


def assistant_context(data: dict) -> dict:
    elements_by_guid = {element.get("guid"): element for element in data.get("elements", [])}
    issues = data.get("issues", [])[:20]
    detected_rules = sorted({issue.get("rule_id") for issue in issues if issue.get("rule_id")})
    issue_counts = Counter(issue.get("rule_id") for issue in issues if issue.get("rule_id"))
    failed_routes = []
    for edge in data.get("routeEdges", []):
        if edge.get("status") != "fail":
            continue
        start = elements_by_guid.get(edge.get("startGuid"), {})
        end = elements_by_guid.get(edge.get("endGuid"), {})
        failed_routes.append(
            {
                "edgeId": edge.get("edgeId"),
                "from": start.get("name") or start.get("label") or edge.get("startGuid"),
                "to": end.get("name") or end.get("label") or edge.get("endGuid"),
                "targetType": end.get("ifcType"),
                "floor": start.get("storey") or end.get("storey"),
                "distanceM": round(float(edge.get("distanceM") or 0), 2),
                "reasons": edge.get("reasons", []),
            }
        )
    floors = []
    for floor in data.get("floors", []):
        door_count = len(floor.get("doorGuids", []))
        route_count = len(floor.get("routeEdgeIds", []))
        failed_route_count = int(floor.get("routeStatusCounts", {}).get("fail", 0) or 0)
        if not door_count and not route_count:
            continue
        if not failed_route_count and not floor.get("failureReasonCounts"):
            continue
        floors.append(
            {
                "name": floor.get("name") or "Unnamed floor",
                "doors": door_count,
                "routeEdges": route_count,
                "failedRouteEdges": failed_route_count,
                "failureReasons": floor.get("failureReasonCounts", {}),
            }
        )
    return {
        "summary": data.get("summary", {}),
        "detectedIssueTypes": detected_rules,
        "issueCountsByType": dict(issue_counts),
        "affectedElements": [
            {
                "name": issue.get("element_label"),
                "type": issue.get("element_type"),
                "rule": issue.get("rule_id"),
                "details": issue.get("details"),
            }
            for issue in issues
        ],
        "failedRoutes": failed_routes[:20],
        "floorsWithFailures": floors,
    }


def shacl_report_response(data: dict) -> dict:
    summary = data.get("summary", {})
    shacl = summary.get("shacl", {})
    return {
        "answer": "Ollama is not running. The app is showing the SHACL validation result instead of a generated explanation.",
        "source": "SHACL validation report",
        "shacl": shacl,
        "issueCount": summary.get("issueCount"),
        "issues": data.get("issues", [])[:20],
    }


def ollama_available(host: str = OLLAMA_HOST) -> bool:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def ollama_models(host: str = OLLAMA_HOST) -> list[str]:
    with urllib.request.urlopen(f"{host}/api/tags", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [str(item.get("name", "")).strip() for item in payload.get("models", []) if item.get("name")]


def select_ollama_model(models: list[str]) -> str:
    if not models:
        raise RuntimeError("Ollama is running but no local model is installed.")
    if OLLAMA_MODEL and OLLAMA_MODEL in models:
        return OLLAMA_MODEL
    for preferred in ("qwen3:8b", "qwen3.5:9b"):
        if preferred in models:
            return preferred
    return models[0]


def ollama_listener_processes() -> list[dict]:
    command = (
        "$owners = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess -Unique; "
        "foreach ($ownerId in $owners) { "
        "$process = Get-Process -Id $ownerId -ErrorAction SilentlyContinue; "
        "if ($process) { Write-Output ($process.Id.ToString() + '|' + $process.ProcessName + '|' + $process.Path) } }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    processes = []
    for line in completed.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        processes.append({"pid": int(parts[0]), "name": parts[1], "path": parts[2]})
    return processes


def stop_ollama_listener() -> None:
    listeners = ollama_listener_processes()
    unsafe = [item for item in listeners if "ollama" not in f"{item['name']} {item['path']}".lower()]
    if unsafe:
        details = ", ".join(f"{item['name']} (PID {item['pid']})" for item in unsafe)
        raise RuntimeError(f"Port {OLLAMA_PORT} is owned by a non-Ollama process: {details}. Nothing was stopped.")
    for item in listeners:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {item['pid']} -Force -ErrorAction Stop"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )


def ollama_executable() -> Path:
    located = shutil.which("ollama")
    candidates = [Path(located)] if located else []
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe")
    if program_files:
        candidates.append(Path(program_files) / "Ollama" / "ollama.exe")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise RuntimeError("ollama.exe was not found. Install Ollama or add it to PATH.")


def wait_for_ollama(timeout_seconds: float = 45) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if ollama_available():
            return
        time.sleep(0.4)
    raise RuntimeError(f"Ollama did not start within {timeout_seconds:.0f} seconds.")


def warm_ollama_model(model: str) -> None:
    payload = json.dumps(
        {
            "model": model,
            "prompt": "Reply with OK.",
            "stream": False,
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": 1},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        if response.status != 200:
            raise RuntimeError(f"Ollama warmup returned HTTP {response.status}.")
        json.loads(response.read().decode("utf-8"))


def restart_ollama() -> tuple[dict, int]:
    global OLLAMA_AVAILABLE, OLLAMA_MODEL
    if not OLLAMA_LOCK.acquire(blocking=False):
        return {"error": "Ollama is already restarting and warming up."}, 409
    started_at = time.monotonic()
    try:
        stop_ollama_listener()
        deadline = time.monotonic() + 12
        while ollama_available() and time.monotonic() < deadline:
            time.sleep(0.3)
        if not ollama_available():
            executable = ollama_executable()
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [str(executable), "serve"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        wait_for_ollama()
        model = select_ollama_model(ollama_models())
        warm_ollama_model(model)
        OLLAMA_MODEL = model
        OLLAMA_AVAILABLE = True
        return {
            "status": "ready",
            "model": model,
            "elapsedSeconds": round(time.monotonic() - started_at, 1),
            "message": "Ollama restarted and the assistant model is warm.",
        }, 200
    except Exception as exc:
        OLLAMA_AVAILABLE = ollama_available()
        return {"error": str(exc), "status": "available" if OLLAMA_AVAILABLE else "offline"}, 503
    finally:
        OLLAMA_LOCK.release()


def main() -> None:
    global MODEL_HOME, MODEL_FILE, ACTIVE_MODEL_FILE, OLLAMA_AVAILABLE, OLLAMA_MODEL
    parser = argparse.ArgumentParser(description="Start the wheelchair route checker website.")
    parser.add_argument("--yes", action="store_true", help="Continue without Ollama and use SHACL report output for assistant requests.")
    parser.add_argument("--port", type=int, default=8765, help="Local port for the website.")
    parser.add_argument("--model-home", type=Path, default=MODEL_HOME, help="Folder for uploaded IFC files and generated model packages.")
    args = parser.parse_args()
    MODEL_HOME = args.model_home.resolve()
    MODEL_FILE = MODEL_HOME / "models.json"
    ACTIVE_MODEL_FILE = MODEL_HOME / "active_model.txt"
    OLLAMA_AVAILABLE = ollama_available()
    if OLLAMA_AVAILABLE:
        try:
            OLLAMA_MODEL = select_ollama_model(ollama_models())
            print(f"Using Ollama model {OLLAMA_MODEL}.")
        except Exception as exc:
            OLLAMA_AVAILABLE = False
            print(f"Ollama is not ready: {exc}")
    if not OLLAMA_AVAILABLE:
        print("Ollama is not running at http://localhost:11434.")
        if not args.yes:
            print("Start Ollama, or rerun: python server.py --yes")
            raise SystemExit(2)
        print("Continuing because --yes was provided. Assistant requests will return SHACL report data.")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Serving http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
