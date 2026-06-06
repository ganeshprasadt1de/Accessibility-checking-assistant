from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from backend.short_explainer import explain_question

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
PACKAGE = ROOT / "output" / "app_package"
OLLAMA_AVAILABLE = False


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
            data = json.loads(data_path.read_text(encoding="utf-8"))
            self._json({"startGuid": guid, "routes": data.get("routesByDoor", {}).get(guid, [])})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path.startswith("/api/assistant"):
            length = int(self.headers.get("Content-Length", "0") or 0)
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
            data = json.loads(data_path.read_text(encoding="utf-8"))
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


def assistant_context(data: dict) -> dict:
    rule_keys = [
        "door_width_m",
        "corridor_width_m",
        "turning_space_m",
        "ramp_width_m",
        "ramp_slope_percent",
    ]
    floors = []
    for floor in data.get("floors", []):
        door_count = len(floor.get("doorGuids", []))
        route_count = len(floor.get("routeEdgeIds", []))
        if not door_count and not route_count:
            continue
        floors.append(
            {
                "name": floor.get("name") or "Unnamed floor",
                "doors": door_count,
                "routeEdges": route_count,
                "failedRouteEdges": int(floor.get("routeStatusCounts", {}).get("fail", 0) or 0),
                "failureReasons": floor.get("failureReasonCounts", {}),
            }
        )
    return {
        "summary": data.get("summary", {}),
        "rules": {key: data.get("rules", {}).get(key) for key in rule_keys},
        "floors": floors,
        "issues": data.get("issues", [])[:20],
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


def ollama_available(host: str = "http://localhost:11434") -> bool:
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
        print("Ollama is not running at http://localhost:11434.")
        if not args.yes:
            print("Start Ollama, or rerun: python server.py --yes")
            raise SystemExit(2)
        print("Continuing because --yes was provided. Assistant requests will return SHACL report data.")
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Serving http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
