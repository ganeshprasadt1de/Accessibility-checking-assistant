from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "app_config.json"
SERVICE_STATE_PATH = PROJECT_ROOT / ".service_state.json"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def load_app_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def services_disabled_by_user() -> bool:
    if not SERVICE_STATE_PATH.exists():
        return False
    try:
        state = json.loads(SERVICE_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(state.get("services_disabled_by_user", False))


def set_services_disabled_by_user(disabled: bool) -> None:
    state = {"services_disabled_by_user": disabled}
    SERVICE_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def service_available(url: str, timeout: int = 3) -> bool:
    try:
        response = requests.get(url, timeout=timeout)
        return response.status_code < 500
    except requests.RequestException:
        return False


def start_local_services(config: dict[str, Any]) -> list[str]:
    set_services_disabled_by_user(False)
    messages = []
    ollama = config.get("ollama", {})

    if ollama.get("enabled", False):
        messages.append(_start_ollama(ollama))

    return messages


def service_status(config: dict[str, Any]) -> dict[str, bool]:
    ollama = config.get("ollama", {})
    return {
        "ollama": service_available(ollama.get("url", "")) if ollama.get("enabled", False) else False,
    }


def stop_local_services() -> list[str]:
    set_services_disabled_by_user(True)
    if os.name != "nt":
        return ["Automatic service stop is configured for Windows in this project."]

    stop_script = r"""
$messages = New-Object System.Collections.Generic.List[string]
$processes = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -in @('ollama.exe', 'ollama app.exe', 'ollama_llama_server.exe')
})

if ($processes.Count -eq 0) {
  $messages.Add('No Ollama process was found.')
} else {
  foreach ($process in $processes) {
    try {
      Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
      $messages.Add("Stopped $($process.Name) with PID $($process.ProcessId).")
    } catch {
      $messages.Add("Could not stop $($process.Name) with PID $($process.ProcessId).")
    }
  }
}

Start-Sleep -Seconds 3
$remaining = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -in @('ollama.exe', 'ollama app.exe', 'ollama_llama_server.exe')
})
if ($remaining.Count -eq 0) {
  $messages.Add('Ollama is not running now.')
} else {
  foreach ($process in $remaining) {
    $messages.Add("Still running: $($process.Name) with PID $($process.ProcessId).")
  }
}
$messages
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", stop_script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    messages = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.stderr.strip():
        messages.append(result.stderr.strip())
    return messages or ["No Ollama process was found."]


def _start_ollama(config: dict[str, Any]) -> str:
    if service_available(config.get("url", "")):
        return "Ollama is already running."

    command_path = _resolve_path(config.get("command", "ollama"))
    if not command_path:
        return "Ollama app was not found. Check ollama.command in app_config.json."

    command = [command_path, *config.get("args", ["serve"])]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)

    if _wait_for_url(config.get("url", ""), seconds=20):
        return "Ollama started."
    return "Ollama was started, but the API did not answer yet."


def _wait_for_url(url: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if service_available(url):
            return True
        time.sleep(1)
    return False


def _resolve_path(value: str) -> str | None:
    if not value:
        return None

    expanded = os.path.expanduser(os.path.expandvars(value))
    direct_path = Path(expanded)
    if direct_path.exists():
        return str(direct_path)

    found = shutil.which(expanded)
    if found:
        return found

    return None
