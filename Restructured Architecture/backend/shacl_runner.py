from __future__ import annotations

from pathlib import Path


def run_shacl(data_graph: Path, shapes_graph: Path, report_ttl: Path) -> dict:
    try:
        from pyshacl import validate
    except Exception as exc:
        return {"available": False, "conforms": None, "source": "pySHACL", "message": str(exc)}
    conforms, report_graph, report_text = validate(
        str(data_graph),
        shacl_graph=str(shapes_graph),
        data_graph_format="turtle",
        shacl_graph_format="turtle",
        advanced=True,
        inference="rdfs",
        serialize_report_graph=True,
    )
    report_ttl.write_bytes(report_graph if isinstance(report_graph, bytes) else str(report_graph).encode("utf-8"))
    result_count = str(report_text).count("Constraint Violation")
    return {
        "available": True,
        "conforms": bool(conforms),
        "source": "SHACL SPARQL constraints through pySHACL",
        "resultCount": result_count,
        "message": f"Conforms: {bool(conforms)}. Constraint violations: {result_count}. Full report is in shacl_report.ttl.",
    }
