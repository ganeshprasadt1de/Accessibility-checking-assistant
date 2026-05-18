from pathlib import Path


def save_graph(graph, path: str | Path) -> None:
    graph.serialize(destination=str(path), format="turtle")
