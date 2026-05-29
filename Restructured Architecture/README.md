# Wheelchair Route Checker

This folder contains a focused indoor wheelchair route checker for IFC building models.

IFC means Industry Foundation Classes. It is the building model file format.

IFCtoLBD means IFC to Linked Building Data. It converts IFC content into RDF.

RDF means Resource Description Framework. It stores facts as triples.

SHACL means Shapes Constraint Language. It checks rule targets on RDF data.

SPARQL is the query language used inside the SHACL rules and route checks.

## Scope

The app checks only indoor wheelchair movement and related building elements:

- doors
- indoor route spaces
- ramps
- stairs close to movement routes
- route continuity between doors on each floor
- floor-by-floor isometric route simulation

It does not check toilets, lifts, outdoor approach paths, fire safety, furniture comfort, or full legal approval.

## Prototype Rules

The current prototype uses five simple wheelchair rules:

- door or opening clear width must be at least 0.90 m
- main indoor route width should be at least 1.50 m
- turning space should be at least 1.50 m by 1.50 m
- stairs block wheelchair routes when the route path intersects the stair area
- ramps, when present, must be at least 1.20 m wide and at most 6 percent slope

## Honest Result Rules

The app does not invent building data.

Rule values are used only as requirements. They are not used as fake IFC measurements.

If a required width, ramp value, space value, or geometry value is missing, the app reports it as missing.

## Pipeline

```text
IFC input from command line
-> IFCtoLBD raw RDF
-> IfcOpenShell geometry extraction
-> RDF geometry enrichment
-> floor-based door route graph
-> prototype indoor route checks
-> static frontend package
```

Generated files are written to:

```text
output/app_package/
```

Main generated files:

```text
raw_lbd_graph.ttl       raw IFCtoLBD graph, or clear IFC-derived fallback if IFCtoLBD cannot run
lbd_graph.ttl           raw graph plus geometry, route, and issue facts
route_graph.bin         pickled route lookup graph when --save-bin is used
app_data.json           fast frontend data
route_model.glb         simple 3D model made from IFC bounding boxes
shacl_report.ttl        SHACL validation report
```

## Geometry Calculations

IfcOpenShell reads the IFC model and creates geometry for each relevant element.

For each element the app calculates:

- bounding box width from max X minus min X
- bounding box depth from max Y minus min Y
- bounding box height from max Z minus min Z
- center point from the middle of the bounding box
- door width from IFC OverallWidth when available
- ramp run length from the larger horizontal side
- ramp usable width from the smaller horizontal side
- ramp slope from height divided by run length
- space clear width from the smaller horizontal side

These values are calculated from the IFC geometry. They are added to `lbd_graph.ttl` on the same element GUID.

## Floor Split

The app groups doors, spaces, stairs, ramps, walls, and columns by `IfcBuildingStorey`.

Each floor is checked as a separate indoor route graph.

The app does not connect floors through stairs. Stairs are blockers for wheelchair movement.

The app does not check lifts because lift checks are outside the current prototype scope.

## Route Graph

Doors become route nodes.

The preprocessor connects doors that share an IFC space boundary. The route edge uses a simple path through the shared space:

```text
start door center
-> clamped entry point inside shared space
-> center lane inside shared space
-> end door center
```

Each route edge stores:

- start door GUID
- end door GUID
- distance
- pass or fail status
- failure reasons
- path points

The frontend only reads this prepared route data. IFC geometry is not recalculated in the browser.

## Stair Check

The app samples points along each route path.

At each point it checks whether the wheelchair route footprint intersects an `IfcStair` bounding box.

If the route intersects a stair, the route fails with:

```text
stair_block
```

This avoids failing a route only because a stair is somewhere else inside the same room or hall.

## SHACL SPARQL Rules

Rules are stored in:

```text
rules/accessibility_rules.shacl.ttl
```

The rules use SPARQL constraints for checks such as:

- door width below 0.90 m
- ramp slope above 6 percent
- ramp width below 1.20 m
- failed route edge

The Python checks also create clean issue rows because SHACL reports are too verbose for the model viewer.

## Run

Open PowerShell in this folder:

```powershell
cd "C:\Users\ganes\Desktop\Ubung - UniStuttgart\Ubung - UniStuttgart\LAB - Knowledge representations for Buildings\Mid Term 1\Restructured Architecture"
..\.venv312\Scripts\activate
```

Preprocess the included AC20 IFC file:

```powershell
python preprocess.py --ifc ".\AC20-Institute-Var-2.ifc" --save-bin
```

Or pass one IFC file directly:

```powershell
python preprocess.py --ifc "C:\path\to\model.ifc" --save-bin
```

Start the local server:

```powershell
python server.py
```

Open:

```text
http://127.0.0.1:8765
```

## Frontend Pages

Check Results:

- extracted building data table
- issues table

Visualisation:

- raw LBD graph preview
- enriched accessibility graph preview
- Turtle download buttons

Building Model:

- rotatable and zoomable 3D model
- door click route lookup
- route failure reasons

Wheelchair Simulation:

- floor dropdown
- isometric floor view
- cartoon wheelchair user animation
- route status summary for the selected floor
- active route shown in green when it passes and red when it fails

## Needed Software

Use the Python environment already present in `Mid Term 1`:

```powershell
..\.venv312\Scripts\activate
```

Install missing Python packages only if needed:

```powershell
python -m pip install -r requirements.txt
```

Java and Maven are needed only when running the bundled IFCtoLBD converter. If they are missing or the converter cannot be called, the app writes a clear note and uses an IFC-derived RDF fallback so the rest of the pipeline can still be inspected.

## Main Files

```text
preprocess.py                    command-line preprocessing pipeline
server.py                        local web server
backend/geometry.py              IfcOpenShell geometry extraction
backend/routes.py                floor route graph and route checks
backend/rules.py                 focused wheelchair issue checks
backend/shacl_runner.py          SHACL SPARQL validation
backend/glb_export.py            GLB box model export
backend/short_explainer.py       short deterministic issue text
frontend/index.html              app shell
frontend/app.js                  tables, graph view, model view, and floor simulation
frontend/styles.css              app styling
rules/accessibility_rules.shacl.ttl
```
