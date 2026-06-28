from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


@dataclass
class OllamaLLMProvider:
    model: str = "qwen3.5:9b"
    host: str = "http://127.0.0.1:11434"

    @property
    def name(self) -> str:
        return f"ollama_{self.model}"

    def generate_json(self, prompt: str, timeout: int = 180) -> dict:
        payload = {
            "model": self.model,
            "prompt": f"/no_think\n{prompt}",
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"temperature": 0.0},
        }
        request = urllib.request.Request(
            f"{self.host.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = str(data.get("response", "{}")).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model returned invalid JSON: {text[:500]}") from exc
