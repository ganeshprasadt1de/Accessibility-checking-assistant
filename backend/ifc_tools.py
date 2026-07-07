from __future__ import annotations

import json
import math
import shutil
import subprocess
import zipfile
from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef
from rdflib.namespace import XSD

from .config import NS
from .model import Element


ACC = Namespace(NS["acc"])
BOT = Namespace(NS["bot"])
PROPS = Namespace(NS["props"])


def extract_first_ifc(zip_path: Path, output_dir: Path, preferred_name: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        ifc_entries = [e for e in archive.infolist() if e.filename.lower().endswith(".ifc")]
        if not ifc_entries:
            raise FileNotFoundError(f"No IFC file found in {zip_path}")
        if preferred_name:
            lowered = preferred_name.lower()
            selected = next((e for e in ifc_entries if lowered in Path(e.filename).name.lower()), None)
        else:
            selected = next((e for e in ifc_entries if "arch-optimized" in e.filename.lower()), None)
        selected = selected or ifc_entries[0]
        target = output_dir / Path(selected.filename).name
        with archive.open(selected) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return target


def run_ifctolbd(ifc_path: Path, ifctolbd_zip: Path, output_ttl: Path, work_dir: Path) -> str:
    """Run IFCtoLBD and fail loudly when the converter cannot produce RDF."""
    java = shutil.which("java")
    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")
    if not java:
        raise RuntimeError("Java is not installed or is not available on PATH. IFCtoLBD cannot run.")
    if not mvn:
        raise RuntimeError("Maven is not installed or is not available on PATH. IFCtoLBD cannot be built.")
    if not ifctolbd_zip.exists():
        raise FileNotFoundError(f"IFCtoLBD ZIP was not found: {ifctolbd_zip}")
    tool_dir = work_dir / "ifctolbd"
    if not tool_dir.exists():
        with zipfile.ZipFile(ifctolbd_zip) as archive:
            archive.extractall(tool_dir)
    project = tool_dir / "IFCtoLBD-master" / "IFCtoLBD"
    if not project.exists():
        raise FileNotFoundError(f"IFCtoLBD project folder was not found after extraction: {project}")
    for module_name in ("IFCtoRDF", "IFCtoLBD_Geometry"):
        module = tool_dir / "IFCtoLBD-master" / module_name
        if not module.exists():
            raise FileNotFoundError(f"Required IFCtoLBD module was not found: {module}")
        try:
            subprocess.run([mvn, "-q", "-DskipTests", "install"], cwd=module, check=True, timeout=300)
        except (subprocess.SubprocessError, OSError) as exc:
            raise RuntimeError(f"IFCtoLBD dependency module build failed for {module_name}: {exc}") from exc
    try:
        subprocess.run([mvn, "-q", "-DskipTests", "package"], cwd=project, check=True, timeout=300)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"IFCtoLBD Maven build failed: {exc}") from exc
    jars = sorted((project / "target").glob("*jar-with-dependencies*.jar")) or sorted(
        (project / "target").glob("*.jar")
    )
    if not jars:
        raise FileNotFoundError(f"IFCtoLBD jar was not found after Maven build: {project / 'target'}")
    base_uri = "https://example.org/building/"
    commands = [
        [
            java,
            "-cp",
            str(jars[0]),
            "org.linkedbuildingdata.ifc2lbd.IFCtoLBDConverter_CLI",
            "-u",
            base_uri,
            "-t",
            str(output_ttl),
            str(ifc_path),
        ],
    ]
    errors: list[str] = []
    for command in commands:
        try:
            subprocess.run(command, cwd=project, check=True, timeout=180)
            if output_ttl.exists() and output_ttl.stat().st_size > 0:
                return "raw graph created by IFCtoLBD"
        except (subprocess.SubprocessError, OSError) as exc:
            errors.append(f"{' '.join(command)} -> {exc}")
            continue
    raise RuntimeError("IFCtoLBD converter did not produce a Turtle graph. " + " | ".join(errors))


def run_ifctolbd_exe(ifc_path: Path, executable: Path, output_ttl: Path) -> str:
    if not executable.exists():
        raise FileNotFoundError(f"IFCtoLBD executable was not found: {executable}")
    base_uri = "https://example.org/building/"
    command = [
        str(executable),
        "-u",
        base_uri,
        "-t",
        str(output_ttl),
        str(ifc_path),
    ]
    try:
        subprocess.run(command, check=True, timeout=240)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"IFCtoLBD executable failed: {exc}") from exc
    if output_ttl.exists() and output_ttl.stat().st_size > 0:
        return "raw graph created by IFCtoLBD"
    raise RuntimeError("IFCtoLBD executable did not produce a Turtle graph.")


def bind_graph(g: Graph) -> None:
    for prefix, ns in NS.items():
        g.bind(prefix, ns)


def element_uri(guid: str) -> URIRef:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in guid)
    return ACC[f"element/{safe}"]


def load_raw_graph(path: Path) -> Graph:
    g = Graph()
    bind_graph(g)
    if not path.exists() or not path.stat().st_size:
        raise FileNotFoundError(f"Raw IFCtoLBD graph is missing or empty: {path}")
    g.parse(path, format="turtle")
    return g


def add_geometry_to_graph(g: Graph, elements: list[Element]) -> None:
    for element in elements:
        uri = element_uri(element.guid)
        g.add((uri, RDF.type, ACC[element.ifc_type]))
        if element.ifc_type == "IfcSpace":
            g.add((uri, RDF.type, BOT.Space))
        elif element.ifc_type in {
            "IfcDoor",
            "IfcRamp",
            "IfcRampFlight",
            "IfcStair",
            "IfcStairFlight",
            "IfcWall",
            "IfcSlab",
            "IfcColumn",
        }:
            g.add((uri, RDF.type, BOT.Element))
        g.add((uri, PROPS.globalId, Literal(element.guid)))
        g.add((uri, PROPS.ifcType, Literal(element.ifc_type)))
        g.add((uri, RDFS.label, Literal(element.label)))
        for prop, value in {
            "bboxWidthM": element.width,
            "bboxDepthM": element.depth,
            "bboxHeightM": element.height,
        }.items():
            if value is not None:
                g.add((uri, ACC[prop], Literal(round(value, 4), datatype=XSD.decimal)))
        if element.center:
            for prop, value in zip(("centerX", "centerY", "centerZ"), element.center):
                g.add((uri, ACC[prop], Literal(round(value, 4), datatype=XSD.decimal)))
        for key, value in element.extra.items():
            if isinstance(value, bool):
                g.add((uri, ACC[key], Literal(value, datatype=XSD.boolean)))
            elif isinstance(value, (int, float)) and not math.isnan(float(value)):
                g.add((uri, ACC[key], Literal(round(float(value), 4), datatype=XSD.decimal)))
            elif value is not None:
                g.add((uri, ACC[key], Literal(str(value))))
