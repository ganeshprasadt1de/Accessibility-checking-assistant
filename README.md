# Wheelchair Route Checker

This project checks indoor wheelchair routes in IFC building models and presents the results in a browser.

The checker reads rooms, doors, walls, columns, stairs, ramps and storeys from an IFC file. It builds a floor-by-floor door graph, creates collision-checked routes, runs SHACL rules over RDF data and prepares matching 2D and 3D views.

## Checks

The supplied rules check:

- door or opening clear width of at least 0.90 m
- route clear width of at least 1.50 m
- turning space of at least 1.50 m by 1.50 m
- stair intersection along a wheelchair route
- ramp usable width of at least 1.20 m
- ramp slope of at most 6 percent

These checks support indoor route review. They are not a replacement for full accessibility approval, fire-safety review or local authority requirements.

## Route Construction

Routes are based on `IfcRelSpaceBoundary` door-to-space relationships.

For each usable space, the checker creates a 0.10 m occupancy grid from wall, column and stair bounding boxes. Obstacles are expanded by 0.38 m around the wheelchair centre. Door rectangles create local openings in wall cells. A four-direction A* search then connects door centres using only horizontal and vertical movement.

The resulting route has these properties:

- every plan segment is 0, 90 or 180 degrees
- consecutive route points are no more than 0.12 m apart
- start and end coordinates match the requested doors
- routes are rejected when the grid has no collision-free connection
- no collision-minimising route is returned through a blocked wall

Stair approaches are stored separately as blocked routes.

## RDF And SHACL

The project includes a pinned IFCtoLBD 2.43.4 Java runtime in:

```text
tools/ifctolbd/java_libraries/
```

Preprocessing requires Java on `PATH`. The IFCtoLBD result must be a valid Turtle graph; preprocessing stops if conversion or parsing fails. IFC geometry measurements and route measurements are added to the RDF graph before pySHACL runs the rules in:

```text
rules/accessibility_rules.shacl.ttl
```

SHACL produces the final pass and fail issues. The optional assistant explains those results but does not decide compliance.

## Browser Views

The website contains:

- Home and Model Library for IFC uploads and generated packages
- Check Results for extracted data, SHACL issues and explanations
- Floor Plan 2D for floor geometry, doors, wall openings, blockers and routes
- Building Model for the compact GLB overview
- Wheelchair Simulation for floor-by-floor route playback

The 2D floor plan and wheelchair simulation use the same:

- floor elements and route edges
- wall segmentation at door openings
- door bounding boxes
- stair and ramp bounding boxes
- route coordinates and status colours
- uniform X/Y proportions

The simulation uses one metres-to-scene scale for the floor, route, wheelchair, doors, markers and labels. Routes are flattened to the selected floor-slice surface. The wheelchair is grounded from its calculated mesh bounds so its tire bottom remains on that surface. Drag with the right mouse button to pan the simulation view. Orbit and zoom continue to use the standard Three.js controls.

## Requirements

- Python 3.12
- Java 17 or another compatible Java runtime available on `PATH`
- Python packages from `requirements.txt`

Install the Python packages:

```powershell
python -m pip install -r requirements.txt
```

Ollama is optional. When it is unavailable, start the server with `--yes`; assistant requests then return the SHACL-backed report data.

The Check Results page includes a `Restart Ollama` button. It first refuses to continue if a non-Ollama process owns port 11434, then stops every Windows process whose executable is the installed `ollama.exe`, including detached model runners. It starts one clean `ollama serve` process, waits for the API and warms the assistant model before reporting that it is ready. Cross-site and simultaneous restart requests are rejected. The server uses `qwen3:8b` when it is installed, otherwise it uses another installed model. Set `OLLAMA_MODEL` before starting the server to select a specific installed model:

```powershell
$env:OLLAMA_MODEL = "qwen3:8b"
python server.py --yes --port 8767
```

## Prepare A Model

Run preprocessing from the project directory:

```powershell
python preprocess.py --ifc ".\AC20-Institute-Var-2.ifc" --save-bin
```

For another IFC file:

```powershell
python preprocess.py --ifc "C:\path\to\building.ifc" --save-bin
```

Prepared files are written to:

```text
output/app_package/
```

## Start The Website

```powershell
python server.py --yes --port 8767
```

Open:

```text
http://127.0.0.1:8767
```

Keep the terminal open while using the website. Stop the server with `Ctrl+C`.

To use the assistant, install the expected model once:

```powershell
ollama pull qwen3:8b
```

Open `Check Results` and use `Restart Ollama` if the Ollama service is stopped or did not load the model correctly. The button waits for startup and warmup; do not send an assistant question until its status reports that Ollama is ready.

Uploaded Model Library entries have their own generated packages. Use `Regenerate` after changing preprocessing or route code, wait for `Complete`, and then use `Open`.

## Output Files

```text
app_data.json           browser data, floors, elements, issues and routes
raw_lbd_graph.ttl       IFCtoLBD Turtle graph
lbd_graph.ttl           RDF graph with derived measurements and routes
shacl_report.ttl        SHACL validation report
route_model.glb         compact browser model
route_graph.bin         saved route graph when --save-bin is used
ifc_route_audit.json    machine-readable route audit
ifc_route_audit.md      readable route audit
```

## Main Files

```text
preprocess.py                    preprocessing entry point
server.py                        local web server and Model Library API
backend/geometry.py              IFC element and bounding-box extraction
backend/routes.py                occupancy grid and route graph
backend/ifc_tools.py             pinned IFCtoLBD execution
backend/shacl_runner.py          SHACL validation and issue extraction
backend/package_writer.py        browser package generation
backend/glb_export.py            compact GLB export
frontend/index.html              website structure
frontend/app.js                  2D, 3D and simulation logic
frontend/styles.css              website styling
tests/test_routes.py             route geometry regression tests
```
