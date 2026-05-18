from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from rdflib import Graph

from accessibility.checker import check_graph
from accessibility.change_impact import add_change_option_to_graph
from accessibility.change_impact import calculate_change_option
from accessibility.change_impact import change_context
from accessibility.change_impact import failed_door_options
from accessibility.change_impact import make_change_impact_viewer
from accessibility.change_impact import option_rows
from accessibility.clearance_3d import make_3d_clearance_viewer
from accessibility.explainer import answer_question
from accessibility.explainer import explain_issue
from accessibility.geometry_enrichment import enrich_graph_with_geometry
from accessibility.geometry_route import analyze_ifc_routes
from accessibility.issue_sections import SECTION_ORDER, fix_text, issue_section, section_summary
from accessibility.lbd_accessibility import extract_lbd_elements
from accessibility.lbd_converter import convert_ifc_to_lbd
from accessibility.local_queries import run_local_geometry_queries
from accessibility.local_services import (
    load_app_config,
    service_status,
    services_disabled_by_user,
    set_services_disabled_by_user,
    start_local_services,
    stop_local_services,
)
from accessibility.model_viewer import make_interactive_model_viewer
from accessibility.pipeline import save_graph
from accessibility.plan_viewer import make_2d_route_plan
from accessibility.rdf_graph_viewer import make_rdf_graph_viewers
from accessibility.route_graph import build_accessible_route_graph
from accessibility.voxel_clearance import make_voxel_clearance_viewer


ACCESSIBILITY_KINDS = {"Door", "Ramp", "Lift", "Corridor", "Accessible toilet", "Route edge"}


class StoredUpload:
    def __init__(self, data: bytes):
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


def copy_graph(graph: Graph) -> Graph:
    copied = Graph()
    for prefix, namespace in graph.namespaces():
        copied.bind(prefix, namespace)
    for triple in graph:
        copied.add(triple)
    return copied


def route_elements(elements):
    return [item for item in elements if item.kind in ACCESSIBILITY_KINDS]


def issue_rows(issues):
    return [
        {
            "Section": issue_section(issue),
            "Element": issue.element_name,
            "Type": issue.element_kind,
            "Rule": issue.rule,
            "Current value": issue.value,
            "Required value": issue.required,
            "How to fix": fix_text(issue),
        }
        for issue in issues
    ]


def build_assistant_context(result: dict) -> str:
    lines = []
    elements = route_elements(result.get("elements", []))
    issues = result.get("issues", [])
    geometry_findings = result.get("geometry_findings", [])
    route_graph_findings = result.get("route_graph_findings", [])
    clearance_3d_findings = result.get("clearance_3d_findings", [])
    voxel_findings = result.get("voxel_findings", [])
    route_edge_rows = result.get("route_edge_rows", [])
    local_query_rows = result.get("local_query_rows", [])
    rdf_graph_stats = result.get("rdf_graph_stats", {})

    lines.append(f"Accessibility elements: {len(elements)}.")
    for element in elements[:25]:
        values = []
        if element.clear_width_m is not None:
            values.append(f"clear width {element.clear_width_m} m")
        if element.clear_height_m is not None:
            values.append(f"clear height {element.clear_height_m} m")
        if element.threshold_height_m is not None:
            values.append(f"threshold {element.threshold_height_m} m")
        if element.slope_percent is not None:
            values.append(f"slope {element.slope_percent} percent")
        detail = ", ".join(values) if values else "no checked numeric value"
        lines.append(f"{element.name} ({element.kind}): {detail}.")

    lines.append(f"SHACL accessibility issues: {len(issues)}.")
    for issue in issues[:40]:
        lines.append(f"{issue.element_name}: {issue.rule}. Current {issue.value}. Required {issue.required}. Fix: {fix_text(issue)}")

    lines.append(f"Accessible route edges: {len(route_edge_rows)}.")
    failed_edges = [row for row in route_edge_rows if str(row.get("Pass", "")).lower() == "false"]
    lines.append(f"Failed accessible route edges: {len(failed_edges)}.")
    for row in route_edge_rows[:35]:
        lines.append(
            f"Route edge from {row.get('From space', '')} to {row.get('To space', '')} through {row.get('Door', '')}: "
            f"width {row.get('Door width m', '')}, level change {row.get('Level change m', '')}, pass {row.get('Pass', '')}."
        )

    for item in geometry_findings[:20]:
        lines.append(f"Model geometry: {item.check}, {item.result}. {item.reason}")
    for item in route_graph_findings[:20]:
        lines.append(f"Route graph: {item.check}, {item.result}. {item.reason}")
    for item in clearance_3d_findings[:20]:
        lines.append(f"3D clearance: {item.element}, {item.result}. {item.reason}")
    for item in voxel_findings[:20]:
        lines.append(f"Voxel route simulation: {item.element}, {item.result}. {item.reason}")
    for row in local_query_rows[:20]:
        lines.append("SPARQL row: " + ", ".join(f"{key}: {value}" for key, value in row.items()) + ".")
    if rdf_graph_stats:
        lines.append("RDF visualisation stats: " + ", ".join(f"{key}: {value}" for key, value in rdf_graph_stats.items()) + ".")
    impact_context = change_context(result.get("change_option"))
    if impact_context:
        lines.append(impact_context)

    return "\n".join(lines)[:16000]


def render_changes_impact_page(result: dict) -> None:
    st.subheader("Changes Impact")
    st.write(
        "This page simulates what happens when a failed route door is widened. "
        "It stores the selected change as RDF facts, shows the area and percent impact, and gives the assistant concrete numbers to explain."
    )
    options = failed_door_options(result.get("route_edge_rows", []))
    if not options:
        st.info("No failed route door below 0.90 m was found in the current route data.")
        return

    labels = [item["label"] for item in options]
    selected = st.selectbox("Failed route door", labels)
    selected_info = next(item for item in options if item["label"] == selected)
    current_width = float(selected_info["current_width"])
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        target_width = st.slider("Target clear door width m", current_width, 1.50, max(0.90, current_width), 0.01)
    with col_b:
        strategy = st.selectbox("Change strategy", ["expand building outward", "keep building fixed and reduce connected space"])
    with col_c:
        plot_limit = st.number_input("Plot footprint limit m2", min_value=0.0, value=0.0, step=5.0)

    option = calculate_change_option(result["lbd_graph"], result.get("route_edge_rows", []), selected, target_width, strategy, plot_limit)
    if option is None:
        st.warning("The selected door could not be linked to the route graph.")
        return
    add_change_option_to_graph(result["lbd_graph"], option)
    result["change_option"] = option
    result["change_impact_html"] = make_change_impact_viewer(option)
    st.session_state["check_result"] = result

    st.write(option.explanation)
    st.dataframe(pd.DataFrame(option_rows(option)), use_container_width=True)
    if option.fits_plot:
        st.write("The selected plot limit can accept this option.")
    else:
        st.write("The selected plot limit cannot accept this outward expansion. Use a fixed-building strategy or choose another accessible route.")
    if result.get("change_impact_html"):
        components.html(result["change_impact_html"], height=660, scrolling=True)


def render_rdf_visualisation_page(result: dict) -> None:
    st.subheader("Visualisation")
    st.write("This page shows the raw IFCtoLBD RDF graph and the enriched accessibility-route RDF graph.")
    st.download_button(
        "Download raw IFCtoLBD RDF",
        result["raw_lbd_graph"].serialize(format="turtle"),
        "raw_lbd_graph.ttl",
        "text/turtle",
    )
    st.download_button(
        "Download enriched accessibility-route RDF",
        result["lbd_graph"].serialize(format="turtle"),
        "lbd_graph.ttl",
        "text/turtle",
    )
    if result.get("rdf_graph_html"):
        stats = result.get("rdf_graph_stats", {})
        st.caption(
            f"Raw RDF triples: {stats.get('raw_triples', 0)} | "
            f"raw visible nodes: {stats.get('raw_visible_nodes', 0)} | "
            f"raw visible links: {stats.get('raw_visible_edges', 0)} | "
            f"enriched RDF triples: {stats.get('enriched_triples', 0)} | "
            f"enriched visible nodes: {stats.get('enriched_visible_nodes', 0)} | "
            f"enriched visible links: {stats.get('enriched_visible_edges', 0)}"
        )
        components.html(result["rdf_graph_html"], height=1420, scrolling=True)


def render_building_model(result: dict) -> None:
    st.subheader("Route Viewer Checks")

    st.markdown("### 2D Shapely Route Plan")
    st.write("This plan uses Shapely obstacle footprints and a 0.90 m clearance strip along right-angle route paths through door points. Wall intersections at the door opening are allowed.")
    if not result.get("plan_viewer_html"):
        if st.button("Build 2D route plan"):
            if not result.get("ifc_bytes"):
                st.error("Run the main check again so the IFC data is available.")
            else:
                with st.spinner("Building 2D route plan..."):
                    stored_upload = StoredUpload(result["ifc_bytes"])
                    plan_viewer_html, plan_viewer_stats = make_2d_route_plan(stored_upload, result["lbd_graph"])
                    result["plan_viewer_html"] = plan_viewer_html
                    result["plan_viewer_stats"] = plan_viewer_stats
                    st.session_state["check_result"] = result
                st.rerun()
    if result.get("plan_viewer_html"):
        stats = result.get("plan_viewer_stats", {})
        st.caption(
            f"route edges: {stats.get('route_edges', 0)} | "
            f"failed 2D route edges: {stats.get('failed_2d_route_edges', 0)} | "
            f"obstacle footprints: {stats.get('obstacle_footprints', 0)} | "
            f"clearance width: {stats.get('wheelchair_clear_width_m', '0.90')} m"
        )
        components.html(result["plan_viewer_html"], height=860, scrolling=True)
    else:
        st.info("Build the 2D route plan when you need the Shapely route view.")

    st.markdown("### 3D Route And Issue Viewer")
    st.write("This model shows IFC geometry, accessible route lines, arrows, and elements with route issues.")
    if not result.get("viewer_html"):
        if st.button("Build 3D route viewer"):
            if not result.get("ifc_bytes"):
                st.error("Run the main check again so the IFC data is available.")
            else:
                with st.spinner("Building 3D route viewer..."):
                    stored_upload = StoredUpload(result["ifc_bytes"])
                    viewer_html, viewer_stats = make_interactive_model_viewer(stored_upload, result["lbd_graph"], result["issues"])
                    result["viewer_html"] = viewer_html
                    result["viewer_stats"] = viewer_stats
                    st.session_state["check_result"] = result
                st.rerun()
    if result.get("viewer_html"):
        stats = result.get("viewer_stats", {})
        st.caption(
            f"model elements: {stats.get('model_elements', 0)} | "
            f"route edges: {stats.get('route_edges', 0)} | "
            f"failed route edges: {stats.get('failed_route_edges', 0)}"
        )
        components.html(result["viewer_html"], height=1080, scrolling=True)
    else:
        st.info("Build the 3D route viewer when you need the interactive IFC model.")

    st.markdown("### Detailed 3D Clearance")
    st.write("This slower check draws wheelchair-sized 3D clearance volumes and compares them with obstacle boxes.")
    if not result.get("clearance_3d_html"):
        if st.button("Run detailed 3D clearance check"):
            if not result.get("ifc_bytes"):
                st.error("Run the main check again so the IFC data is available.")
            else:
                with st.spinner("Building detailed 3D clearance model..."):
                    stored_upload = StoredUpload(result["ifc_bytes"])
                    html_3d, stats_3d, findings_3d = make_3d_clearance_viewer(stored_upload, result["lbd_graph"])
                    result["clearance_3d_html"] = html_3d
                    result["clearance_3d_stats"] = stats_3d
                    result["clearance_3d_findings"] = findings_3d
                    st.session_state["check_result"] = result
                st.rerun()
        st.info("Run this only when a deeper 3D clearance check is needed.")

    if result.get("clearance_3d_html"):
        stats = result.get("clearance_3d_stats", {})
        st.caption(
            f"route segments: {stats.get('route_segments', 0)} | "
            f"failed clearance segments: {stats.get('failed_clearance_segments', 0)} | "
            f"clearance width: {stats.get('clearance_width_m', 0)} m | "
            f"clearance height: {stats.get('clearance_height_m', 0)} m"
        )
        components.html(result["clearance_3d_html"], height=980, scrolling=True)

    st.markdown("### Voxel Route Simulation")
    st.write(
        "This check divides obstacle geometry into small 3D voxels and moves a wheelchair-sized clearance volume along the route. "
        "The visible wheelchair/person marker explains the movement; the collision result comes from the clearance volume."
    )
    if not result.get("voxel_html"):
        if st.button("Run voxel route simulation"):
            if not result.get("ifc_bytes"):
                st.error("Run the main check again so the IFC data is available.")
            else:
                with st.spinner("Building voxel route simulation..."):
                    stored_upload = StoredUpload(result["ifc_bytes"])
                    voxel_html, voxel_stats, voxel_findings = make_voxel_clearance_viewer(stored_upload, result["lbd_graph"])
                    result["voxel_html"] = voxel_html
                    result["voxel_stats"] = voxel_stats
                    result["voxel_findings"] = voxel_findings
                    st.session_state["check_result"] = result
                st.rerun()
    if result.get("voxel_html"):
        stats = result.get("voxel_stats", {})
        st.caption(
            f"voxel size: {stats.get('voxel_size_m', 0)} m | "
            f"occupied voxels: {stats.get('occupied_voxels', 0)} | "
            f"checked route segments: {stats.get('checked_route_segments', 0)} | "
            f"failed route segments: {stats.get('failed_route_segments', 0)} | "
            f"Open3D voxel cells: {stats.get('open3d_voxel_cells', 0)} | "
            f"voxel engine: {stats.get('voxel_engine', 'internal Python grid')}"
        )
        components.html(result["voxel_html"], height=1040, scrolling=True)


st.set_page_config(page_title="Accessibility Compliance Checker", layout="wide")
app_config = load_app_config()

if (
    app_config.get("auto_start_services", False)
    and "services_started_once" not in st.session_state
    and not st.session_state.get("services_disabled_by_user", False)
    and not services_disabled_by_user()
):
    st.session_state["service_messages"] = start_local_services(app_config)
    st.session_state["services_started_once"] = True

st.title("Accessibility Compliance Checker")
st.write(
    "Upload an IFC model. The app converts it to IFCtoLBD, adds route geometry, "
    "checks DIN 18040-style wheelchair accessibility rules with SHACL and SPARQL, and explains the result."
)

page = st.radio("Page", ["Check Results", "Visualisation", "Changes Impact", "Building Model"], horizontal=True, label_visibility="collapsed")

with st.sidebar:
    st.header("Input")
    uploaded_file = st.file_uploader("IFC file", type=["ifc"])
    run_button = st.button("Run check")
    st.markdown("---")
    st.markdown("**Local Services**")
    current_status = service_status(app_config)
    st.write(f"Ollama: {'running' if current_status['ollama'] else 'not running'}")
    if st.button("Start services"):
        st.session_state["services_disabled_by_user"] = False
        set_services_disabled_by_user(False)
        st.session_state["service_messages"] = start_local_services(app_config)
        st.rerun()
    if st.button("Kill services"):
        st.session_state["services_disabled_by_user"] = True
        st.session_state["service_messages"] = stop_local_services()
        st.rerun()
    for message in st.session_state.get("service_messages", []):
        st.caption(message)
    st.markdown("---")
    st.markdown("**Team**")
    st.caption("Ganesh Prasad Tamminedi")
    st.caption("Simon Knorr")
    st.caption("Yang Yu")
    st.caption("Yaoqiao Sha")

if run_button:
    if uploaded_file is None:
        st.warning("Upload an IFC file before running the check.")
        st.stop()

    progress = st.progress(0)
    status = st.empty()

    status.write("Creating IFCtoLBD graph...")
    lbd_graph, lbd_message = convert_ifc_to_lbd(uploaded_file, app_config.get("ifctolbd", {}))
    progress.progress(25)
    if lbd_graph is None:
        st.error(lbd_message)
        st.stop()
    raw_lbd_graph = copy_graph(lbd_graph)

    status.write("Adding accessible route geometry...")
    geometry_triples, geometry_messages = enrich_graph_with_geometry(uploaded_file, lbd_graph)
    geometry_messages.insert(0, f"Added {geometry_triples} geometry and route triples.")
    progress.progress(40)

    status.write("Reading accessibility elements...")
    elements = extract_lbd_elements(lbd_graph)
    progress.progress(52)

    status.write("Checking IFC route data quality...")
    geometry_findings = analyze_ifc_routes(uploaded_file)
    progress.progress(60)

    status.write("Building accessible route graph...")
    route_edge_rows, route_graph_findings = build_accessible_route_graph(uploaded_file, lbd_graph)
    progress.progress(68)

    status.write("Running SPARQL route checks...")
    local_query_rows = run_local_geometry_queries(lbd_graph)
    progress.progress(76)

    status.write("Running SHACL accessibility checks...")
    conforms, issues, result_text = check_graph(lbd_graph)
    for issue in issues:
        issue.explanation = explain_issue(issue, use_llm=False)
    progress.progress(88)

    status.write("Saving RDF output...")
    save_graph(raw_lbd_graph, Path("raw_lbd_graph.ttl"))
    save_graph(lbd_graph, Path("lbd_graph.ttl"))
    progress.progress(100)
    status.write("Accessibility check finished.")

    result = {
        "ifc_bytes": uploaded_file.getvalue(),
        "ifc_name": uploaded_file.name,
        "elements": elements,
        "lbd_graph": lbd_graph,
        "raw_lbd_graph": raw_lbd_graph,
        "conforms": conforms,
        "issues": issues,
        "geometry_findings": geometry_findings,
        "route_edge_rows": route_edge_rows,
        "route_graph_findings": route_graph_findings,
        "local_query_rows": local_query_rows,
        "plan_viewer_html": None,
        "plan_viewer_stats": {},
        "viewer_html": None,
        "viewer_stats": {},
        "clearance_3d_html": None,
        "clearance_3d_stats": {},
        "clearance_3d_findings": [],
        "voxel_html": None,
        "voxel_stats": {},
        "voxel_findings": [],
        "change_option": None,
        "change_impact_html": None,
        "result_text": result_text,
        "lbd_message": lbd_message,
        "geometry_messages": geometry_messages,
    }
    st.session_state["check_result"] = result

if "check_result" not in st.session_state:
    if page in {"Visualisation", "Changes Impact", "Building Model"}:
        st.info("Run a check first, then open this page.")
    else:
        st.info("Upload an IFC file, then run the check.")
    st.stop()

result = st.session_state["check_result"]

if page == "Changes Impact":
    render_changes_impact_page(result)
    save_graph(result["lbd_graph"], Path("lbd_graph.ttl"))
    st.stop()

if page == "Building Model":
    render_building_model(result)
    st.stop()

if page == "Visualisation":
    if "rdf_graph_html" not in result or not result.get("rdf_graph_html"):
        with st.spinner("Building RDF graph views..."):
            rdf_graph_html, rdf_graph_stats = make_rdf_graph_viewers(result["raw_lbd_graph"], result["lbd_graph"], result["issues"])
            result["rdf_graph_html"] = rdf_graph_html
            result["rdf_graph_stats"] = rdf_graph_stats
            st.session_state["check_result"] = result
    render_rdf_visualisation_page(result)
    st.stop()

elements = route_elements(result["elements"])
issues = result["issues"]

st.subheader("Accessibility Elements")
st.dataframe(
    pd.DataFrame(
        [
            {
                "Element": item.name,
                "Type": item.kind,
                "Clear width m": item.clear_width_m,
                "Clear height m": item.clear_height_m,
                "Approach space m": item.approach_space_m,
                "Reveal depth m": item.reveal_depth_m,
                "Threshold height m": item.threshold_height_m,
                "Handle height m": item.handle_height_m,
                "Slope percent": item.slope_percent,
                "Usable width m": item.usable_width_m,
                "Length m": item.length_m,
                "Platform length m": item.platform_length_m,
                "Has handrails": item.has_handrails,
                "Has edge protection": item.has_edge_protection,
                "Has cross slope": item.has_cross_slope,
                "Handrail height m": item.handrail_height_m,
                "Handrail diameter m": item.handrail_diameter_m,
                "Handrail extension m": item.handrail_extension_m,
                "Start area width m": item.start_area_width_m,
                "Start area depth m": item.start_area_depth_m,
                "End area width m": item.end_area_width_m,
                "End area depth m": item.end_area_depth_m,
                "Passing space m": item.passing_space_m,
                "Lift door width m": item.door_width_m,
                "Cabin width m": item.cabin_width_m,
                "Cabin depth m": item.cabin_depth_m,
                "Movement area width m": item.movement_area_width_m,
                "Movement area depth m": item.movement_area_depth_m,
                "Turning diameter m": item.turning_diameter_m,
                "Opens inward": item.opens_inward,
                "Has washbasin": item.has_washbasin,
                "Side approach width m": item.side_approach_width_m,
                "Side approach depth m": item.side_approach_depth_m,
                "Has emergency call": item.has_emergency_call,
                "Source": item.source,
            }
            for item in elements
        ]
    ),
    use_container_width=True,
)

st.subheader("Accessibility Compliance Issues")
if issues:
    rows = issue_rows(issues)
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    for section in SECTION_ORDER:
        section_rows = [row for row in rows if row["Section"] == section]
        with st.expander(f"{section} ({len(section_rows)})", expanded=section == "Accessible route"):
            st.write(section_summary(section))
            if section_rows:
                st.dataframe(pd.DataFrame(section_rows), use_container_width=True)
            else:
                st.write("No issue was found in this section.")
else:
    st.write("No accessible route issue was found for the selected checks.")

if result.get("route_edge_rows"):
    st.subheader("Route Edges")
    st.dataframe(pd.DataFrame(result["route_edge_rows"]), use_container_width=True)

if result.get("geometry_findings"):
    st.subheader("IFC Route Data Quality")
    st.dataframe(pd.DataFrame([item.__dict__ for item in result["geometry_findings"]]), use_container_width=True)

if result.get("route_graph_findings"):
    st.subheader("Route Graph Findings")
    st.dataframe(pd.DataFrame([item.__dict__ for item in result["route_graph_findings"]]), use_container_width=True)

if result.get("voxel_findings"):
    st.subheader("Voxel Route Findings")
    st.dataframe(pd.DataFrame([item.__dict__ for item in result["voxel_findings"]]), use_container_width=True)

if result.get("local_query_rows"):
    st.subheader("SPARQL Route Checks")
    st.dataframe(pd.DataFrame(result["local_query_rows"]), use_container_width=True)

st.subheader("RDF Output")
st.write("The raw IFCtoLBD graph is saved as `raw_lbd_graph.ttl`.")
st.write("The enriched accessibility-route graph is saved as `lbd_graph.ttl`.")
st.download_button("Download raw IFCtoLBD RDF", result["raw_lbd_graph"].serialize(format="turtle"), "raw_lbd_graph.ttl", "text/turtle")
st.download_button("Download enriched accessibility-route RDF", result["lbd_graph"].serialize(format="turtle"), "lbd_graph.ttl", "text/turtle")
if result.get("lbd_message"):
    st.write(result["lbd_message"])
for message in result.get("geometry_messages", []):
    st.write(message)

st.subheader("Ask The Assistant")
question = st.text_input("Question about accessibility compliance", placeholder="Ask which elements fail or how to fix wheelchair accessibility.")
if question:
    with st.spinner("Assistant is reading the accessibility checks..."):
        answer = answer_question(issues, question, True, extra_context=build_assistant_context(result))
    st.write(answer)




