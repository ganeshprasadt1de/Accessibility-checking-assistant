# Accessibility Compliance Checker

This folder contains an accessibility compliance checker.

It checks wheelchair accessibility, including routes, doors, ramps, lifts, corridors, and accessible toilets.

IFC means Industry Foundation Classes. It is the BIM file format used to exchange building models.

IFCtoLBD means IFC to Linked Building Data. It converts the IFC model into an RDF graph.

RDF means Resource Description Framework. It stores facts as triples, for example:

```text
Door A has width 0.86 m
```

SHACL means Shapes Constraint Language. It checks whether the graph follows the rule values.

SPARQL means the query language for RDF graphs. It is used here for route and geometry checks.

## What It Checks

The checker focuses on wheelchair accessibility according to DIN 18040-style measurable rules.

It checks:

- door clear width
- door clear height
- door approach space
- door reveal depth
- door threshold height
- door handle height
- ramp slope
- ramp usable width
- ramp run length
- ramp platform length
- ramp handrails
- ramp edge protection
- ramp cross slope
- ramp handrail dimensions
- ramp start and end movement areas
- lift door width
- lift cabin size
- corridor clear width
- passing space
- accessible toilet movement area
- accessible toilet turning space
- accessible toilet door direction
- accessible toilet washbasin
- accessible toilet side transfer space
- accessible toilet emergency call
- accessible route topology
- accessible route door width
- accessible route level change
- accessible route pass result

Route topology means how spaces and doors are connected. An accessible route needs connected spaces, doors, and space boundaries.

## Pipeline

```text
IFC file
-> IFCtoLBD RDF graph
-> IfcOpenShell and Shapely geometry enrichment
-> accessible route graph
-> SHACL accessibility rules
-> local SPARQL route checks
-> 2D Shapely route plan
-> 3D route and issue viewer
-> detailed 3D clearance model
-> voxel route simulation
-> RDF visualisation and Changes Impact view
-> local LLM explanation through Ollama
```

The LLM does not decide if something passes or fails. SHACL, SPARQL, IfcOpenShell, and Shapely do the checking. The LLM only explains the checked result in simple language.

## Pages

```text
Check Results     tables for accessibility elements, issues, route edges, SPARQL checks, RDF output, and assistant
Visualisation     raw IFCtoLBD graph, enriched accessibility-route graph, and RDF downloads
Changes Impact    door-widening impact simulation with footprint, volume, plot, room, and RDF change facts
Building Model    2D route plan, 3D route viewer, detailed 3D clearance model, and voxel route simulation
```

The visualisation page does not draw every triple from a large IFC file. It shows focused graph views so the browser stays usable. The full RDF data is still available through the Turtle downloads.

The Changes Impact page is a reasoning view tied to failed route doors. It shows what happens when a door is widened to satisfy the route width rule. The user can choose whether the building expands outward or the outer footprint stays fixed and a connected space gives up area. The app calculates footprint change, volume change, affected space area, percent change, and whether the entered plot limit can accept the change.

The selected change is also written into the RDF graph as a change option. That gives the assistant real numbers to explain instead of asking the LLM to guess.

## Route Viewer Checks

The 2D route plan uses Shapely. Shapely is a geometry library for plan-style checks. The app creates obstacle footprints and draws accessible route paths as right-angle lines through door points.

The 3D route viewer uses IfcOpenShell mesh geometry. It shows route lines, direction arrows, and route issue markers.

The detailed 3D clearance model is run separately because it is slower. It draws a wheelchair-sized clearance volume:

```text
clearance width 0.90 m
clearance height 2.05 m
```

The clearance volume is compared against 3D obstacle bounding boxes. A bounding box is a simple box around a 3D object. This adds height-based checking to the 2D plan check.

The voxel route simulation divides obstacle geometry into small 3D cells called voxels. A voxel is a cube in a 3D grid. The app moves a wheelchair-sized clearance volume along the route and checks whether that volume intersects occupied voxels. The visible wheelchair and person in the viewer explain the movement, but the pass or fail result comes from the clearance volume. The implementation uses the same voxel-grid logic directly in Python and detects Open3D if it is installed in a compatible Python environment.

## Needed Software

Install these once:

1. Python 3.12
2. Java 17 or newer
3. Ollama

Java is needed because IFCtoLBD runs as a Java tool.

Ollama is needed for the local LLM explanation.

Python 3.12 is recommended because Open3D currently does not install reliably on newer Python versions such as Python 3.14.

Open3D is used by the voxel route simulation setup. The app also keeps a direct Python voxel grid for the pass or fail calculation, so the route result stays clear and inspectable.

Create the local Python environment:

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\activate
python -m pip install -r requirements.txt
```

Install the local LLM model:

```powershell
ollama pull qwen3:8b
```

## IFCtoLBD Setup

The included `app_config.json` points to:

```text
IFCtoLBD-master.zip
```

This works when the ZIP file stays in this folder. If you move the folder to another computer, place `IFCtoLBD-master.zip` beside `app.py` or update `ifctolbd.zip_path` in `app_config.json`.

The app extracts the needed Java files into:

```text
.tools/ifctolbd/
```

## Run

Open PowerShell in this folder:

```powershell
cd "C:\Users\ganes\Desktop\Ubung - UniStuttgart\Ubung - UniStuttgart\LAB - Knowledge representations for Buildings\Mid Term 1"
.\.venv312\Scripts\activate
streamlit run app.py
```

Then upload an IFC file and press:

```text
Run check
```

## Main Files

```text
app.py                                  Streamlit app
app_config.json                         local tool settings
requirements.txt                        Python packages
shacl/accessibility_rules.shacl.ttl     accessibility SHACL rules
accessibility/lbd_converter.py          IFC to IFCtoLBD conversion
accessibility/geometry_enrichment.py    adds geometry and route triples
accessibility/route_graph.py            builds accessible route edges
accessibility/local_queries.py          local SPARQL route checks
accessibility/plan_viewer.py            2D Shapely route plan
accessibility/rdf_graph_viewer.py       RDF graph visualisation
accessibility/model_viewer.py           3D route and issue viewer
accessibility/clearance_3d.py           detailed 3D clearance model
accessibility/voxel_clearance.py        voxel route simulation with wheelchair/person marker
accessibility/change_impact.py          Changes Impact calculations and RDF change facts
accessibility/checker.py                SHACL validation
accessibility/explainer.py              local LLM explanations
```

Generated files:

```text
raw_lbd_graph.ttl
lbd_graph.ttl
```

These are created when the app runs.

## Important Limit

The result depends on the IFC export quality.

For strong accessible route checking, the IFC should contain:

- `IfcSpace`
- `IfcDoor`
- `IfcRelSpaceBoundary`
- usable door sizes
- usable route geometry
- lift data if lifts are part of the route
- ramp data if ramps are part of the route

If spaces or boundaries are missing, the app can still check single doors and ramps, but it cannot build a reliable room-door-room accessible route graph.

This checker is not a legal approval tool.



