from pathlib import Path

from rdflib import Graph


def rules_path() -> Path:
    return Path(__file__).resolve().parents[1] / "shacl" / "accessibility_rules.shacl.ttl"


def load_shapes_graph() -> Graph:
    graph = Graph()
    graph.parse(rules_path(), format="turtle")
    return graph
