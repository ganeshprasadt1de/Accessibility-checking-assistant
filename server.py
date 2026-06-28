from __future__ import annotations

import argparse
import json
import shutil
import threading
import urllib.error
import urllib.request
from collections import Counter
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from backend.short_explainer import explain_question

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
PACKAGE = ROOT / "output" / "app_package"
OLLAMA_AVAILABLE = False
OLLAMA_HOST = "http://127.0.0.1:11434"
MAX_ASSISTANT_BODY_BYTES = 16_384
_APP_DATA_CACHE: dict[str, object] = {"mtime_ns": None, "size": None, "data": None}
_APP_DATA_LOCK = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean = unquote(parsed.path).lstrip("/")
        if parsed.path.startswith("/api/") or parsed.path.startswith("/files/"):
            return str(PACKAGE / clean.split("/", 1)[1])
        target = FRONTEND / (clean or "index.html")
        if target.is_dir():
            target = target / "index.html"
        return str(target)

    def do_GET(self) -> None:
        if self.path.startswith("/api/data"):
            self._send_file(PACKAGE / "app_data.json", "application/json")
            return
        if self.path.startswith("/api/route/"):
            guid = unquote(self.path.rsplit("/", 1)[-1])
            data_path = PACKAGE / "app_data.json"
            if not data_path.exists():
                self._json({"error": "Run preprocess.py first."}, 404)
                return
            data = load_app_data(data_path)
            self._json({"startGuid": guid, "routes": data.get("routesByDoor", {}).get(guid, [])})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/assistant"):
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_ASSISTANT_BODY_BYTES:
                self._json({"error": "Question is too large."}, 413)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "Question could not be read."}, 400)
                return

            data_path = PACKAGE / "app_data.json"
            if not data_path.exists():
                self._json({"error": "Run preprocess.py first."}, 404)
                return

            question = str(payload.get("question", "")).strip() or "Explain the checker result."
            data = load_app_data(data_path)
            if not OLLAMA_AVAILABLE:
                self._json(shacl_report_response(data))
                return
            try:
                self._json(explain_question(question, assistant_context(data)))
            except Exception as exc:
                self._json({"error": f"Ollama request failed: {exc}"}, 503)
            return
        self._json({"error": "Unknown endpoint."}, 404)

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
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def end_headers(self) -> None:
        self._send_no_cache_headers()
        super().end_headers()

    def _send_no_cache_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")


def load_app_data(data_path: Path) -> dict:
    stat = data_path.stat()
    with _APP_DATA_LOCK:
        if (
            _APP_DATA_CACHE["data"] is None
            or _APP_DATA_CACHE["mtime_ns"] != stat.st_mtime_ns
            or _APP_DATA_CACHE["size"] != stat.st_size
        ):
            _APP_DATA_CACHE["data"] = json.loads(data_path.read_text(encoding="utf-8"))
            _APP_DATA_CACHE["mtime_ns"] = stat.st_mtime_ns
            _APP_DATA_CACHE["size"] = stat.st_size
        return _APP_DATA_CACHE["data"]  # type: ignore[return-value]


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


def main() -> None:
    global OLLAMA_AVAILABLE
    parser = argparse.ArgumentParser(description="Start the wheelchair route checker website.")
    parser.add_argument("--yes", action="store_true", help="Continue without Ollama and use SHACL report output for assistant requests.")
    args = parser.parse_args()
    OLLAMA_AVAILABLE = ollama_available()
    if not OLLAMA_AVAILABLE:
        print(f"Ollama is not running at {OLLAMA_HOST}.")
        if not args.yes:
            print("Start Ollama, or rerun: python server.py --yes")
            raise SystemExit(2)
        print("Continuing because --yes was provided. Assistant requests will return SHACL report data.")
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Serving http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
