# Wheelchair Route Checker

This repo contains an indoor wheelchair route checker for IFC building models.

IFC is a building model file format. It stores things like doors, rooms, floors, stairs, ramps, and walls.

The app reads an IFC file, splits the building by floor, checks wheelchair routes between doors, and shows the result in a browser.

The final compliance decision is made by SHACL rules over RDF. Python prepares IFC geometry measurements and route geometry, but it does not create the pass or fail issue list.

## What The App Checks

The app checks these indoor wheelchair route rules:

- door or opening clear width must be at least 0.90 m
- main indoor route width should be at least 1.50 m
- turning space should be at least 1.50 m by 1.50 m
- stairs block a wheelchair route when the route path enters the stair area
- ramps must be at least 1.20 m wide and at most 6 percent slope

The app is only for indoor route checks. It does not check toilets, lifts, outdoor paths, fire safety, or full legal approval.

Stairs are shown separately from door-to-door routes:

- door and corridor routes stay green when their path does not enter the stair area
- stair approaches are shown as blocked because stairs are not wheelchair routes
- on upper floors and basement, the simulation starts from the stair landing before checking routes to doors

## What You Can See In The Website

Check Results:

- building data found in the IFC file
- route issues found by the checker
- assistant box that explains the result in simple words

Building Model:

- 3D building view
- stairs shown in red
- clickable doors
- routes connected to the selected door
- blocked stair approach path for the selected door

Wheelchair Simulation:

- floor dropdown
- isometric floor view
- small wheelchair user
- stair landing start on basement and upper floors
- green route when it passes
- red route when it fails

## How The Checker Works

The app uses these steps:

```text
IFC file
-> building elements and geometry
-> floor-by-floor route graph
-> wheelchair rule checks
-> stair approach issue checks
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

- Ollama is only needed if you want the assistant to use a local LLM.

Required tools:

- Java is required for IFCtoLBD.
- Maven is required to build IFCtoLBD from `IFCtoLBD-master.zip`.
- `IFCtoLBD-master.zip` must be present in the repo folder.

The preprocessing step stops with an error if Java, Maven, IFCtoLBD, or pySHACL is not available. It does not create a Python replacement LBD graph.

The website checks Ollama when it starts. If Ollama is not running, start the website with `--yes` to continue and show SHACL report data instead of generated assistant text.

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

If Ollama is not running and you still want to open the website:

```powershell
python server.py --yes
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

If Ollama is not running, `python server.py` stops and prints a message. Use `python server.py --yes` to continue. In that mode, assistant requests return SHACL report data instead of generated explanation text.

The assistant is only for explanation. The pass or fail result comes from SHACL validation.

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
frontend/app.js                  website logic, assistant, model view, and simulation
frontend/styles.css              website styling
rules/accessibility_rules.shacl.ttl
```
