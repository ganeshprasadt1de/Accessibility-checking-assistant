from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any
from zipfile import ZipFile

from rdflib import Graph


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / ".tools" / "ifctolbd"
LIB_DIR = TOOLS_DIR / "java_libraries"
CLI_CLASS = "org.linkedbuildingdata.ifc2lbd.IFCtoLBDConverter_CLI"
_CACHED_LIBRARIES: list[Path] | None = None


def convert_ifc_to_lbd(uploaded_file, config: dict[str, Any]) -> tuple[Graph | None, str]:
    if not config.get("enabled", False):
        return None, "IFCtoLBD is required. Set ifctolbd.enabled to true in app_config.json."

    java_command = _resolve_command(config.get("java_command", "java"))
    if not java_command:
        return None, "Java was not found. Install Java 17 or set ifctolbd.java_command in app_config.json."

    libraries = _prepare_libraries(config)
    if not libraries:
        return None, "IFCtoLBD libraries were not found. Check ifctolbd.zip_path in app_config.json."

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        with NamedTemporaryFile(delete=False, suffix=".ifc", dir=temp_path) as ifc_file:
            ifc_file.write(uploaded_file.getvalue())
            ifc_path = Path(ifc_file.name)

        target_path = temp_path / "lbd_graph.ttl"
        command = [
            java_command,
            "-cp",
            os.pathsep.join(str(path) for path in libraries),
            CLI_CLASS,
            str(ifc_path),
            "--target_file",
            str(target_path),
            "--url",
            config.get("uri_base", "http://example.org/building/"),
            "--level",
            str(config.get("property_level", 1)),
        ]
        if config.get("building_elements", True):
            command.append("--hasBuildingElements")
        if config.get("properties", True):
            command.append("--hasBuildingElementProperties")
        if config.get("geometry", False):
            command.append("--hasGeometry")
        if config.get("units", True):
            command.append("--hasUnits")

        result = subprocess.run(command, capture_output=True, text=True, timeout=int(config.get("timeout_seconds", 180)))
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            return None, f"IFCtoLBD conversion failed: {details}"

        if not target_path.exists():
            return None, "IFCtoLBD finished, but no Turtle file was created."

        graph = Graph()
        graph.parse(target_path, format="turtle")
        return graph, f"Created IFCtoLBD graph with {len(graph)} triples."


def _prepare_libraries(config: dict[str, Any]) -> list[Path]:
    global _CACHED_LIBRARIES
    if _CACHED_LIBRARIES:
        return _CACHED_LIBRARIES

    jar_paths = _configured_jars(config)
    if jar_paths:
        _CACHED_LIBRARIES = jar_paths
        return jar_paths

    if LIB_DIR.exists():
        jars = sorted(LIB_DIR.glob("*.jar"))
        if jars:
            _CACHED_LIBRARIES = jars
            return jars

    zip_path = _resolve_path(config.get("zip_path", "IFCtoLBD-master.zip"))
    if not zip_path:
        return []

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path) as archive:
        members = [
            member
            for member in archive.namelist()
            if member.startswith("IFCtoLBD-master/IFCtoLBD_NodeJS/java_libraries/") and member.endswith(".jar")
        ]
        for member in members:
            target = LIB_DIR / Path(member).name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)

    _CACHED_LIBRARIES = sorted(LIB_DIR.glob("*.jar"))
    return _CACHED_LIBRARIES


def _configured_jars(config: dict[str, Any]) -> list[Path]:
    library_dir = _resolve_path(config.get("library_dir", ""))
    if library_dir:
        jars = sorted(Path(library_dir).glob("*.jar"))
        if jars:
            return jars
    return []


def _resolve_command(value: str) -> str | None:
    path = _resolve_path(value)
    if path:
        return path
    return shutil.which(value)


def _resolve_path(value: str) -> str | None:
    if not value:
        return None
    expanded = os.path.expanduser(os.path.expandvars(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.exists():
        return str(path)
    return None
