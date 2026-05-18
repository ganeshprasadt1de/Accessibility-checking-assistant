from __future__ import annotations

from rdflib import Graph


def run_local_geometry_queries(graph: Graph) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    rows.extend(_run_query(graph, "Route doors below 0.90 m", _NARROW_DOORS, "at least 0.90 m"))
    rows.extend(_run_query(graph, "Corridors below 1.20 m", _NARROW_ROUTE_SPACES, "at least 1.20 m"))
    rows.extend(_run_query(graph, "Accessible route spaces without a door boundary", _ROUTE_SPACE_WITHOUT_DOOR, "at least one route door boundary"))
    rows.extend(_run_query(graph, "Accessible route edges that fail", _FAILED_ROUTE_EDGES, "door at least 0.90 m and level change at most 0.02 m"))
    return rows


def _run_query(graph: Graph, check: str, query: str, required: str) -> list[dict[str, str]]:
    result_rows = []
    for row in graph.query(query):
        result_rows.append(
            {
                "Check": check,
                "Element": str(row.label or row.element),
                "Current value": str(row.value),
                "Required value": required,
                "Source": "IFCtoLBD plus IfcOpenShell/Shapely route geometry",
            }
        )
    return result_rows


_PREFIXES = """
PREFIX acc: <http://example.org/accessibility#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""


_NARROW_DOORS = _PREFIXES + """
SELECT ?element ?label ?value
WHERE {
  ?element acc:derivedDoorWidthM ?value .
  OPTIONAL { ?element rdfs:label ?label . }
  FILTER(xsd:decimal(?value) < 0.90)
}
ORDER BY ?value ?label
"""


_NARROW_ROUTE_SPACES = _PREFIXES + """
SELECT ?element ?label ?value
WHERE {
  ?element rdfs:label ?label ;
           acc:derivedClearWidthM ?value .
  FILTER(REGEX(LCASE(STR(?label)), "corridor|flur|gang|circulation|verkehr"))
  FILTER(xsd:decimal(?value) < 1.20)
}
ORDER BY ?value ?label
"""


_ROUTE_SPACE_WITHOUT_DOOR = _PREFIXES + """
SELECT ?element ?label ?value
WHERE {
  ?element rdfs:label ?label .
  FILTER(REGEX(LCASE(STR(?label)), "corridor|flur|gang|circulation|verkehr"))
  FILTER NOT EXISTS { ?element acc:hasRouteDoor ?door . }
  BIND("missing" AS ?value)
}
ORDER BY ?label
"""


_FAILED_ROUTE_EDGES = _PREFIXES + """
SELECT ?element ?label ?value
WHERE {
  ?element a acc:RouteEdge ;
           rdfs:label ?label ;
           acc:routePass ?pass .
  FILTER(?pass != true)
  BIND("failed" AS ?value)
}
ORDER BY ?label
"""


