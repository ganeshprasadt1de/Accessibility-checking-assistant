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
    """Run the pinned IFCtoLBD runtime and require a valid Turtle result.

    The upstream 2.45 shaded JAR in IFCtoLBD-master.zip has incompatible Jena
    service metadata and fails with ``NoReaderForLangException: TTL``.  The
    project therefore ships the verified 2.43.4 runtime and its matching Jena
    libraries as one inseparable classpath.
    """
    java = shutil.which("java")
    if not java:
        raise RuntimeError("Java is not installed or is not available on PATH. IFCtoLBD cannot run.")
    runtime = Path(__file__).resolve().parents[1] / "tools" / "ifctolbd" / "java_libraries"
    main_jar = runtime / "ifc-to-lbd-2.43.4.jar"
    jena_jar = runtime / "jena-arq-4.10.0.jar"
    if not main_jar.exists() or not jena_jar.exists():
        raise FileNotFoundError(
            f"Pinned IFCtoLBD runtime is incomplete: {runtime}. "
            "Expected ifc-to-lbd-2.43.4.jar and jena-arq-4.10.0.jar."
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "ifctolbd.log"
    trig_sidecar = output_ttl.with_suffix(".trig")
    if output_ttl.exists():
        output_ttl.unlink()
    if trig_sidecar.exists():
        trig_sidecar.unlink()
    base_uri = "https://example.org/building/"
    command = [
        java,
        "-cp",
        str(runtime / "*"),
        "org.linkedbuildingdata.ifc2lbd.IFCtoLBDConverter_CLI",
        "-u",
        base_uri,
        "-t",
        str(output_ttl),
        str(ifc_path),
    ]
    try:
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                command,
                cwd=runtime,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                text=True,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"Pinned IFCtoLBD runtime failed: {exc}. Log: {log_path}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Pinned IFCtoLBD runtime exited with code {result.returncode}. Log: {log_path}")
    if not output_ttl.exists() or not output_ttl.stat().st_size:
        raise RuntimeError(f"Pinned IFCtoLBD runtime produced no Turtle graph. Log: {log_path}")
    try:
        Graph().parse(output_ttl, format="turtle")
    except Exception as exc:
        output_ttl.unlink(missing_ok=True)
        raise RuntimeError(f"IFCtoLBD produced invalid Turtle: {exc}. Log: {log_path}") from exc
    # IFCtoLBD also writes a TriG geometry sidecar. The application uses its
    # own IfcOpenShell geometry, so retaining this duplicate file is misleading.
    trig_sidecar.unlink(missing_ok=True)
    return "raw graph created by pinned IFCtoLBD 2.43.4 runtime"


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
        subprocess.run(command, check=True, timeout=600)
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


def passing_area_gap_uri(evidence_id: str) -> URIRef:
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in evidence_id)
    return ACC[f"passing-area-gap/{safe}"]


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
        for gap in element.passing_area_gaps:
            gap_uri = passing_area_gap_uri(gap["evidence_id"])
            g.add((gap_uri, RDF.type, ACC.CorridorPassingAreaGap))
            g.add((gap_uri, ACC.inCorridor, uri))
            g.add((gap_uri, ACC.passingAreaGapLengthM, Literal(gap["measured"], datatype=XSD.decimal)))
            g.add((gap_uri, ACC.passingAreaTestWidthM, Literal(gap["movement_space_m"], datatype=XSD.decimal)))
            g.add((gap_uri, ACC.passingAreaTestDepthM, Literal(gap["movement_space_m"], datatype=XSD.decimal)))
            g.add((gap_uri, ACC.issueRegionId, Literal(gap["region_id"])))
