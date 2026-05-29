# Wheelchair Route Checker

This repo contains an indoor wheelchair route checker for IFC building models.

IFC is a building model file format. It stores things like doors, rooms, floors, stairs, ramps, and walls.

The app reads an IFC file, splits the building by floor, checks wheelchair routes between doors, and shows the result in a browser.

## What The App Checks

The first prototype checks these rules:

- door or opening clear width must be at least 0.90 m
- main indoor route width should be at least 1.50 m
- turning space should be at least 1.50 m by 1.50 m
- stairs block a wheelchair route when the route path enters the stair area
- ramps must be at least 1.20 m wide and at most 6 percent slope

The app is only for indoor route checks. It does not check toilets, lifts, outdoor paths, fire safety, or full legal approval.

## What You Can See In The Website

Check Results:

- building data found in the IFC file
- route issues found by the checker
- assistant box that explains the result in simple words

Visualisation:

- raw building graph
- enriched accessibility graph
- Turtle file downloads

Building Model:

- 3D building view
- clickable doors
- routes connected to the selected door

Wheelchair Simulation:

- floor dropdown
- isometric floor view
- small cartoon wheelchair user
- green route when it passes
- red route when it fails

## How The Checker Works

The app uses these steps:

```text
IFC file
-> building elements and geometry
-> floor-by-floor route graph
-> wheelchair rule checks
-> website data
-> browser view
```

The browser does not recalculate the IFC model. It only shows the data prepared by the Python scripts.

## Install

Install Python 3.12 first.

Then open a terminal in the repo folder and install the Python packages:

```powershell
python -m pip install -r requirements.txt
```

Optional tools:

- Java and Maven are only needed if you want to run the bundled IFCtoLBD converter.
- Ollama is only needed if you want the assistant to use a local LLM.

The app still runs without Ollama. In that case, the assistant uses a built-in explanation based on the checker result.

## Run With The Included IFC File

From the repo folder, prepare the data:

```powershell
python preprocess.py --ifc ".\AC20-Institute-Var-2.ifc" --save-bin
```

Start the website:

```powershell
python server.py
```

Open this link in your browser:

```text
http://127.0.0.1:8765
```

## Run With Your Own IFC File

Use the same command, but replace the file path:

```powershell
python preprocess.py --ifc "path\to\your\model.ifc" --save-bin
python server.py
```

Then open:

```text
http://127.0.0.1:8765
```

## Assistant Explanation

The assistant explains the checker result in normal language.

If Ollama is running with `qwen3:8b`, the app asks Ollama to write the answer.

If Ollama is not running, the app gives a shorter built-in answer from the same result data.

The assistant is only for explanation. The pass or fail result comes from the route checker.

## Output Files

Prepared website files are written here:

```text
output/app_package/
```

Important output files:

```text
app_data.json           data used by the website
route_model.glb         simple 3D model for the browser
route_graph.bin         saved route graph when --save-bin is used
ifc_route_audit.json    route audit data
ifc_route_audit.md      readable route audit summary
shacl_report.ttl        SHACL validation report
```

## Main Files

```text
preprocess.py                    prepares checker data from the IFC file
server.py                        starts the local website
backend/geometry.py              reads IFC geometry
backend/routes.py                builds floor routes and checks them
backend/rules.py                 creates issue rows
backend/shacl_runner.py          runs SHACL checks
backend/glb_export.py            creates the browser 3D model
backend/short_explainer.py       assistant and short issue text
frontend/index.html              website layout
frontend/app.js                  website logic, graphs, model view, and simulation
frontend/styles.css              website styling
rules/accessibility_rules.shacl.ttl
```
