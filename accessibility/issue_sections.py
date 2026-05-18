from accessibility.model import Issue


SECTION_ORDER = ["Accessible route", "Accessible toilet", "Model data"]


def issue_section(issue: Issue) -> str:
    if issue.element_kind == "Element":
        return "Model data"
    if "toilet" in issue.rule.lower() or issue.element_kind == "Accessible toilet":
        return "Accessible toilet"
    return "Accessible route"


def section_summary(section: str) -> str:
    if section == "Accessible route":
        return "Checks that affect wheelchair movement through doors, ramps, corridors, lifts, thresholds, and route edges."
    if section == "Accessible toilet":
        return "Checks for movement area and reachable fixtures inside accessible toilets."
    return "Checks that depend on whether the IFC model contains usable spaces, doors, boundaries, and geometry."


def fix_text(issue: Issue) -> str:
    rule = issue.rule.lower()
    if "door clear width" in rule or "route door width" in rule:
        return "Use a wider door or change the opening so the clear accessible passage is at least 0.90 m."
    if "door clear height" in rule:
        return "Increase the clear door height to at least 2.05 m."
    if "approach space" in rule:
        return "Keep enough free side space near the door handle so a person using a wheelchair can reach and open the door."
    if "reveal depth" in rule:
        return "Reduce the door reveal depth or move the handle/control so it can be reached from a wheelchair."
    if "threshold" in rule or "level change" in rule:
        return "Remove the step, reduce the threshold, or provide a compliant ramp or lift route."
    if "handle" in rule:
        return "Place the handle or control in the reachable height range."
    if "ramp slope" in rule:
        return "Lengthen the ramp or add a landing so the slope is at most 6 percent."
    if "ramp usable width" in rule:
        return "Increase the usable ramp width to at least 1.20 m."
    if "platform" in rule:
        return "Increase the landing or platform length to at least 1.50 m."
    if "handrail" in rule:
        return "Adjust ramp handrails so they are present, reachable, and easy to grip."
    if "edge protection" in rule:
        return "Add wheel deflectors or edge protection so wheelchair wheels cannot slip off the ramp edge."
    if "cross slope" in rule:
        return "Remove the side slope from the ramp or keep it within the checked requirement."
    if "lift" in rule:
        return "Use a lift with a compliant door and cabin size."
    if "corridor" in rule:
        return "Increase the clear circulation width or remove obstacles from the route."
    if "passing" in rule:
        return "Add a wider passing area along the route."
    if "toilet movement" in rule or "toilet turning space" in rule:
        return "Increase the free floor area so a wheelchair can turn and position beside the toilet."
    if "toilet door direction" in rule:
        return "Change the door swing so it does not reduce the movement area."
    if "toilet washbasin" in rule:
        return "Add or move the washbasin so it can be reached from a wheelchair."
    if "side approach" in rule:
        return "Keep enough free transfer space next to the toilet."
    if "emergency call" in rule:
        return "Add an emergency call control that can be reached from the toilet area."
    if "topology" in rule:
        return "Export spaces, doors, and space boundaries so the route can be connected correctly."
    if "pass result" in rule:
        return "Fix the failed route edge by widening the door or removing the level change."
    return "Fix the route element so it satisfies the listed accessibility requirement."


