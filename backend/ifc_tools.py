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


def try_ifctolbd(ifc_path: Path, ifctolbd_zip: Path, output_ttl: Path, work_dir: Path) -> tuple[bool, str]:
    """Run IFCtoLBD when Java/Maven can execute it. Returns status and note."""
    java = shutil.which("java")
    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")
    if not java or not mvn:
        return False, "Java or Maven not found, used IFC-derived fallback LBD graph"
    tool_dir = work_dir / "ifctolbd"
    if not tool_dir.exists():
        with zipfile.ZipFile(ifctolbd_zip) as archive:
            archive.extractall(tool_dir)
    project = tool_dir / "IFCtoLBD-master" / "IFCtoLBD"
    if not project.exists():
        return False, "IFCtoLBD project folder not found, used IFC-derived fallback LBD graph"
    try:
        subprocess.run([mvn, "-q", "-DskipTests", "package"], cwd=project, check=True, timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        return False, f"IFCtoLBD build did not finish, used IFC-derived fallback LBD graph: {exc}"
    jars = sorted((project / "target").glob("*jar-with-dependencies*.jar")) or sorted(
        (project / "target").glob("*.jar")
    )
    if not jars:
        return False, "IFCtoLBD jar not found after build, used IFC-derived fallback LBD graph"
    commands = [
        [java, "-jar", str(jars[0]), str(ifc_path), str(output_ttl)],
        [java, "-cp", str(jars[0]), "org.linkedbuildingdata.ifc2lbd.IFCtoLBDConverter_CLI", str(ifc_path), str(output_ttl)],
    ]
    for command in commands:
        try:
            subprocess.run(command, cwd=project, check=True, timeout=180)
            if output_ttl.exists() and output_ttl.stat().st_size > 0:
                return True, "raw graph created by IFCtoLBD"
        except (subprocess.SubprocessError, OSError):
            continue
    return False, "IFCtoLBD CLI entry point was not callable, used IFC-derived fallback LBD graph"


def bind_graph(g: Graph) -> None:
    for prefix, ns in NS.items():
        g.bind(prefix, ns)


def element_uri(guid: str) -> URIRef:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in guid)
    return ACC[f"element/{safe}"]


def create_raw_lbd_fallback(elements: list[Element], output_ttl: Path) -> None:
    g = Graph()
    bind_graph(g)
    for element in elements:
        uri = element_uri(element.guid)
        g.add((uri, RDF.type, ACC[element.ifc_type]))
        if element.ifc_type == "IfcSpace":
            g.add((uri, RDF.type, BOT.Space))
        elif element.ifc_type in {"IfcDoor", "IfcRamp", "IfcStair", "IfcWall", "IfcSlab", "IfcColumn"}:
            g.add((uri, RDF.type, BOT.Element))
        g.add((uri, RDFS.label, Literal(element.label)))
        g.add((uri, PROPS.globalId, Literal(element.guid)))
        g.add((uri, PROPS.ifcType, Literal(element.ifc_type)))
        if element.name:
            g.add((uri, PROPS.name, Literal(element.name)))
    output_ttl.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=output_ttl, format="turtle")


def load_raw_graph(path: Path) -> Graph:
    g = Graph()
    bind_graph(g)
    if path.exists() and path.stat().st_size:
        try:
            g.parse(path, format="turtle")
        except Exception:
            g = Graph()
            bind_graph(g)
    return g


def add_geometry_to_graph(g: Graph, elements: list[Element]) -> None:
    for element in elements:
        uri = element_uri(element.guid)
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
