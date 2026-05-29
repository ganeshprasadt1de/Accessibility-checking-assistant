from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
PACKAGE = ROOT / "output" / "app_package"


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
        if self.path.startswith("/api/ttl/raw"):
            self._send_file(PACKAGE / "raw_lbd_graph.ttl", "text/turtle")
            return
        if self.path.startswith("/api/ttl/enriched"):
            self._send_file(PACKAGE / "lbd_graph.ttl", "text/turtle")
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


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Serving http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
