from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import ifcopenshell

from accessibility.model import GeometryFinding


def analyze_ifc_routes(uploaded_file) -> list[GeometryFinding]:
    with NamedTemporaryFile(delete=False, suffix=".ifc") as temp_file:
        temp_file.write(uploaded_file.getvalue())
        temp_path = temp_file.name

    model = ifcopenshell.open(temp_path)
    Path(temp_path).unlink(missing_ok=True)
    findings: list[GeometryFinding] = []

    spaces = model.by_type("IfcSpace")
    doors = model.by_type("IfcDoor")
    ramps = model.by_type("IfcRamp") + model.by_type("IfcRampFlight")

    if not spaces:
        findings.append(
            GeometryFinding(
                category="Model data",
                element="IFC model",
                check="Route topology",
                result="not checked",
                reason="The IFC file has no IfcSpace entities. A route graph needs spaces or rooms to know where a person can move.",
                fix="Export rooms or spaces from the BIM model, then run the check again.",
            )
        )
    else:
        relations = model.by_type("IfcRelSpaceBoundary")
        if not relations:
            findings.append(
                GeometryFinding(
                    category="Model data",
                    element="IFC model",
                    check="Space boundaries",
                    result="not checked",
                    reason="The IFC file has spaces but no IfcRelSpaceBoundary entities. Door-to-room route links cannot be built.",
                    fix="Export space boundaries from the BIM model.",
                )
            )
        else:
            findings.append(
                GeometryFinding(
                    category="Mobility",
                    element="IFC model",
                    check="Route topology",
                    result="available",
                    reason=f"The IFC contains {len(spaces)} spaces and {len(relations)} space boundary relations.",
                    fix="Use these relations to build a room-door-room route graph for deeper checks.",
                )
            )

    if not doors:
        findings.append(
            GeometryFinding(
                category="Model data",
                element="IFC model",
                check="Door route access",
                result="not checked",
                reason="No IfcDoor entities were found.",
                fix="Export doors as IfcDoor elements.",
            )
        )

    if not ramps:
        findings.append(
            GeometryFinding(
                category="Mobility",
                element="IFC model",
                check="Ramp route access",
                result="not present",
                reason="No IfcRamp or IfcRampFlight entities were found in this IFC file.",
                fix="If the design has ramps, export them as ramp elements with slope, width, landing, and handrail properties.",
            )
        )

    findings.append(
        GeometryFinding(
            category="Model data",
            element="IFC model",
            check="Geometric enrichment",
            result="limited",
            reason="The accessible route checks use IFCtoLBD classes, space boundaries, door positions, and obstacle geometry.",
            fix="Export IfcSpace, IfcRelSpaceBoundary, door positions, ramp data, lift data, and obstacle geometry for stronger accessible route checking.",
        )
    )

    return findings


