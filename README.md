# Wheelchair Route Checker

The Wheelchair Route Checker reads an IFC building model, calculates indoor door-to-door wheelchair routes, checks accessibility measurements with SHACL and shows the results in a browser.

The application contains:

- IFC geometry extraction with IfcOpenShell
- IFC-to-RDF conversion with the included IFCtoLBD 2.43.4 Java runtime
- four-direction A* routing on precomputed 0.01 m floor tiles for door routes and point-to-point checks
- Shapely-based plan-route generation for 2D floor routing and visual route topology
- shortest door-graph routes calculated with Dijkstra's algorithm
- SHACL validation with pySHACL
- matching 2D floor plans and a 2.5D wheelchair simulation
- a local Ollama assistant that explains checked facts without deciding compliance

## Before You Start

The instructions below are for Windows 10 or Windows 11 and PowerShell.

You need:

- Git
- Python 3.12
- Java 17
- Ollama if you want generated explanations

The IFCtoLBD JAR files and Three.js 0.165.0 browser modules are already included in the repository. Maven, Node.js and npm are not required to run the project.

Maven is not part of the runtime. The server does not call `mvn` and the repository does not need to resolve a `pom.xml`. Java executes the included IFCtoLBD classpath directly.

## 1. Install Git

Open PowerShell as a normal user and run:

```powershell
winget install --id Git.Git --version 2.55.0.2 -e --accept-source-agreements --accept-package-agreements
```

Close PowerShell, open it again and verify the installation:

```powershell
git --version
```

## 2. Install Python 3.12

```powershell
winget install --id Python.Python.3.12 --version 3.12.10 -e --accept-source-agreements --accept-package-agreements
```

Close PowerShell, open it again and verify that the Python 3.12 launcher is available:

```powershell
py -3.12 --version
```

The documented installer provides Python 3.12.10. The application is also tested with Python 3.12.13. Use Python 3.12 because the pinned IfcOpenShell wheel is available for this Python series.

## 3. Install Java 17

```powershell
winget install --id Microsoft.OpenJDK.17 --version 17.0.19.10 -e --accept-source-agreements --accept-package-agreements
```

Close PowerShell, open it again and verify Java:

```powershell
java -version
```

The tested Java runtime is Microsoft OpenJDK 17.0.19. A Java 17 runtime must be on `PATH` before preprocessing an IFC file.

## 4. Install Ollama

Ollama is needed only for generated explanations. Route calculation, RDF conversion, SHACL checks, 2D plans and the wheelchair simulation work without it.

```powershell
winget install --id Ollama.Ollama --version 0.31.2 -e --accept-source-agreements --accept-package-agreements
```

Close PowerShell, open it again and verify Ollama:

```powershell
ollama --version
```

Install the expected local model once:

```powershell
ollama pull qwen3:8b
```

The model download is several gigabytes and can take time. It is stored by Ollama, not inside this repository.

## 5. Clone The Repository

Choose a folder where the project should be stored:

```powershell
cd "$HOME\Desktop"
git clone https://github.com/ganeshprasadt1de/Accessibility-checking-assistant.git
cd Accessibility-checking-assistant
```

Confirm that you are on the main branch:

```powershell
git branch --show-current
```

The result should be `main`.

## 6. Create An Isolated Python Environment

A virtual environment prevents this project's packages from changing other Python projects on the computer.

```powershell
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

After activation, the PowerShell prompt begins with `(.venv)`.

Install the exact tested package versions:

```powershell
python -m pip install --upgrade "pip==25.2"
python -m pip install -r requirements.txt
```

Verify the four direct libraries:

```powershell
python -c "import ifcopenshell, rdflib, pyshacl, shapely; print('Python dependencies are ready.')"
```

The versions in `requirements.txt` are fixed with `==`. Direct and transitive packages are pinned so reinstalling the project does not silently select newer library behaviour.

The browser uses the included Three.js 0.165.0 files from `frontend/vendor/three`. It does not download JavaScript from a CDN when the application starts.

## 7. Verify The Included IFCtoLBD Runtime

The unzipped Java runtime is stored in:

```text
tools/ifctolbd/java_libraries/
```

It contains IFCtoLBD 2.43.4, Apache Jena 4.10.0 and their matching Java libraries. Do not download or unzip another IFCtoLBD package over this folder.

Verify the two main JAR files:

```powershell
Test-Path ".\tools\ifctolbd\java_libraries\ifc-to-lbd-2.43.4.jar"
Test-Path ".\tools\ifctolbd\java_libraries\jena-arq-4.10.0.jar"
```

Both commands must return `True`.

Verify every included JAR against the repository checksum manifest:

```powershell
$root = Resolve-Path ".\tools\ifctolbd\java_libraries"
$expected = @{}
Get-Content "$root\SHA256SUMS.txt" | ForEach-Object {
    if ($_ -match '^([0-9a-f]{64})  (.+)$') {
        $expected[$matches[2]] = $matches[1]
    }
}
$failed = @()
Get-ChildItem $root -Filter "*.jar" | ForEach-Object {
    $actual = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $expected.ContainsKey($_.Name) -or $expected[$_.Name] -ne $actual) {
        $failed += $_.Name
    }
}
if ($failed.Count -eq 0 -and $expected.Count -eq 170) {
    "IFCtoLBD runtime verified: 170 JAR files."
} else {
    throw "IFCtoLBD verification failed: $($failed -join ', ')"
}
```

## 8. Prepare An IFC Model

The repository includes both project IFC files:

- `AC20-Institute-Var-2.ifc`
- `20201208DigitalHub_ARC.ifc`

The repository also includes a ready-to-run DigitalHub package in `output/app_package`. A new user can skip the two preprocessing commands below and continue with step 9. Run preprocessing before reviewing changes to the IFC model, geometry, rules or routing code.

Generate the AC20 browser package with:

```powershell
python preprocess.py --ifc ".\AC20-Institute-Var-2.ifc" --save-bin
```

Generate the DigitalHub browser package with:

```powershell
python preprocess.py --ifc ".\20201208DigitalHub_ARC.ifc" --save-bin
```

For weaker laptops, add `--low-end`:

```powershell
python preprocess.py --ifc ".\20201208DigitalHub_ARC.ifc" --save-bin --low-end
```

Low-end mode uses the same IFC extraction, RDF conversion, 0.01 m navigation tiles, route audits and SHACL checks. It does not loosen the route rules and should produce the same accessibility results as the normal run. It only changes how the computer spends CPU time: the preprocessing process gets lower priority, native math and Java worker threads are limited, and short pauses are added inside heavy tile and route loops. The laptop should stay more responsive, but preprocessing can take longer.

`output/app_package` contains one active package at a time. Running either command replaces that generated package with the selected model's results. The original IFC files are not modified.

Preprocessing also builds the point-to-point navigation data. Each floor is divided into 5 m tiles at 0.01 m resolution. Walkable cells are packed as bits and compressed, so the browser and server load only the tiles needed for the selected floor and route. Walls, columns, stairs, inaccessible ramps and narrow doors are blocked during this build. Corridor-width issue regions are blocked locally instead of removing the complete IFC space from the navigation grid.

To process another IFC model, use its full path:

```powershell
python preprocess.py --ifc "C:\path\to\building.ifc" --save-bin
```

Preprocessing can take several minutes. A successful run ends with lines similar to:

```text
Extracted elements: ...
raw graph created by pinned IFCtoLBD 2.43.4 runtime
Wrote package: ...\output\app_package
Routes: ..., plan routes: ..., issues: ..., missing geometry: ..., skipped route pairs: ...
```

In the browser, open **Floor Plan 2D** or **Wheelchair Simulation**, change **Mode** to **Point-to-point**, then select a start and destination on the current floor. Both tabs send the same IFC coordinates to the same backend route checker. A successful result must start and end at the selected coordinates, use only 0, 90 or 180 degree segments, and pass the final collision audit before it is drawn.

If preprocessing reports that Java is missing, close PowerShell, open it again and rerun `java -version`.

## 9. Start Ollama

Check whether Ollama is already running:

```powershell
Invoke-RestMethod "http://127.0.0.1:11434/api/tags"
```

If the command cannot connect, start Ollama in a separate PowerShell window:

```powershell
ollama serve
```

Keep that window open.

## 10. Start The Website

Return to the project PowerShell window. Make sure the virtual environment is active, then run:

```powershell
python server.py --port 8771
```

Open this address in a browser:

```text
http://127.0.0.1:8771
```

Keep the server window open. Stop the server with `Ctrl+C`.

If port 8771 is already occupied, stop the old local server:

```powershell
Get-NetTCPConnection -LocalPort 8771 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

Then run `python server.py --port 8771` again.

## Running Without Ollama

If you do not want generated explanations, start the website with:

```powershell
python server.py --yes --port 8771
```

The assistant area then returns SHACL report data. All building extraction, route generation, validation and visualisation features remain available.

## Using The Website

The website contains:

- `Home`: upload IFC files and manage generated model packages
- `Check Results`: inspect extracted elements, SHACL results and grounded explanations
- `Floor Plan 2D`: inspect floor geometry and route overlays
- `Building Model`: inspect the compact GLB model
- `Wheelchair Simulation`: play clear and blocked routes on a 2.5D floor slice

In the Model Library, `Generate` uses the normal full-speed preprocessing run. `Generate low-end` runs the same checks with reduced CPU pressure. Use it when a laptop becomes noisy, hot or slow during IFC preprocessing. The result package still uses the same 0.01 m routing data and the same SHACL rules.

The Check Results page includes `Restart Ollama`. It stops verified Ollama processes, starts one clean Ollama service, waits for the API and warms the selected model. It refuses to stop an unrelated program if another executable owns port 11434.

The header also includes `Stop Project Services`. It stops verified Wheelchair Route Checker servers on ports 8765, 8766, 8767 and 8771, then stops verified Ollama processes on port 11434. It checks the executable, command line and project API response before stopping a server. Other localhost listeners are reported and left running. Because the current website server is stopped last, the page disconnects after showing the result.

The server prefers `qwen3:8b`. To select another installed model before starting the server:

```powershell
$env:OLLAMA_MODEL = "qwen3:8b"
python server.py --port 8771
```

## Accessibility Checks

The supplied SHACL rules check:

- route-relevant door or opening clear width of at least 0.90 m and clear height of at least 2.05 m
- corridor and route clear width of at least 1.50 m
- corridor slope of at most 3 percent, or 4 percent for sections no longer than 10.00 m
- 1.80 m by 1.80 m passing areas at intervals no greater than 15.00 m
- turning space of at least 1.50 m by 1.50 m
- stair, wall or column obstruction along a wheelchair route
- disconnected route doors
- ramp usable width of at least 1.20 m
- ramp slope of at most 6 percent
- ramp flight length of at most 6.00 m

These checks support model review. They do not replace professional accessibility approval, fire-safety review or requirements from the responsible local authority.

## Route Construction

Routes use `IfcRelSpaceBoundary` door-to-space relationships to decide which doors may form an edge in the door graph. This relationship creates only the candidate pair. It does not prove that the space between the doors is accessible.

Preprocessing divides each floor into compressed 5 m tiles containing 0.01 m cells. Walls, columns and stairs are included when their IFC bounding boxes enter the floor's 2.05 m vertical clearance volume. In plan view, solid obstacles are expanded by 0.445 m around the route centre: a 0.45 m wheelchair radius minus a 0.005 m geometry tolerance. Accessible door rectangles create controlled portals through wall openings.

Every candidate door pair is then checked by four-direction A* on these tiles. The strict result replaces the provisional candidate before RDF measurements, SHACL validation, Dijkstra indexes, audit files or browser data are written. The same tiled geometry and clearance rules are therefore used by the door graph, the floor-check simulation and interactive point-to-point mode.

The generated route has these properties:

- every plan segment uses 0, 90 or 180 degrees
- route endpoints match the selected door centres
- a route is rejected when no collision-free grid connection exists
- a route intersecting a blocked wall cell is never accepted
- every accepted path is audited again for exact endpoints, orthogonal segments and collision-free cells

Dijkstra's algorithm joins passing door-to-door edges into shortest routes across the door graph. Stair approaches are stored as blocked edges so the simulation can stop before the stair.

`backend/plan_routes.py` builds a separate 2D route network from Shapely walkable-space geometry. It removes wall, column and stair obstacles, restores controlled openings at valid doors and checks the buffered route footprint against the walkable area. A 0.20 m grid search is used when a direct candidate is not suitable.

The automatic 2.5D floor check uses `planRouteEdges` as its visual route topology and audits the displayed paths against the strict precomputed 0.01 m navigation tiles. Passing routes must be collision-free, orthogonal and consistent with the strict grid. Failed physical edges remain available as blocked evidence. A blocked red route follows its collision-free prefix to the exact door or stair obstacle where the failure occurs. Cross-floor edges and disagreements between the plan network and the strict navigation grid are recorded instead of being drawn as valid floor routes.

Interactive point-to-point routing reads the shared floor package. A request loads only the required tiles, runs four-direction A*, restores the exact selected endpoints and performs a final geometry audit. Tiles from other floors remain on disk, and the in-memory tile cache has a fixed limit. A complete audited route is green. If the destination cannot be reached, the red candidate ends at the last collision-free cell and is never labelled accessible.

## RDF, SHACL And Ollama

IFCtoLBD converts IFC meaning into RDF Turtle. The application adds geometry measurements and route measurements to the RDF graph. pySHACL then runs the constraints in:

```text
rules/accessibility_rules.shacl.ttl
```

SHACL produces the pass or fail result. Ollama does not decide compliance. It selects only recommendations supplied by the backend and linked to detected SHACL issue types. Unsupported or incomplete model output is rejected and replaced with SHACL-grounded text.

## Matching 2D And 2.5D Views

The 2D floor plan renders `planRouteEdges`. The wheelchair simulation uses the same plan network as its floor-route topology and checks its displayed paths against the strict navigation grid. The Building Model continues to use the base `routeEdges` door graph.

The simulation uses one uniform metres-to-scene scale. Routes are placed on one selected floor-slice surface. The wheelchair is grounded from its calculated mesh bounds so the tyre bottom touches that surface. Orbit, pan and zoom change the camera only; they do not change route coordinates.

## Generated Package Files

```text
output/app_package/app_data.json         browser data, elements, issues and routes
output/app_package/raw_lbd_graph.ttl     IFCtoLBD RDF graph
output/app_package/lbd_graph.ttl         RDF with derived measurements and routes
output/app_package/shacl_report.ttl      complete SHACL report
output/app_package/route_model.glb       compact browser model
output/app_package/route_graph.bin       saved route graph when --save-bin is used
output/app_package/ifc_route_audit.json  machine-readable route audit
output/app_package/ifc_route_audit.md    readable route audit
output/app_package/navigation/index.json floor extents, tile metadata and hashes
output/app_package/navigation/tiles/*/*.nav compressed 0.01 m walkability tiles
```

Uploaded Model Library entries have separate generated packages. After changing preprocessing or route code, select `Regenerate`, wait for `Complete`, then select `Open`.

## Main Project Files

```text
preprocess.py                              preprocessing entry point
server.py                                  local server, Model Library and Ollama control
requirements.txt                           exact Python dependency versions
backend/geometry.py                        IFC geometry and bounding-box extraction
backend/routes.py                          IFC door graph candidates and route measurements
backend/plan_routes.py                     Shapely-based 2D route network generation
backend/navigation.py                      compressed 0.01 m navigation tiles, A* and final audit
backend/simulation_routes.py               strict automatic floor-check route geometry
backend/ifc_tools.py                       pinned IFCtoLBD execution
backend/shacl_runner.py                    SHACL validation and issue extraction
backend/package_writer.py                  browser package generation
backend/glb_export.py                      compact GLB export
backend/short_explainer.py                 grounded Ollama recommendation selection
frontend/index.html                        website structure
frontend/app.js                            2D, 3D and simulation behaviour
frontend/styles.css                        website styling
frontend/vendor/three/                     included Three.js 0.165.0 browser modules
rules/accessibility_rules.shacl.ttl        accessibility constraints
tools/ifctolbd/java_libraries/             complete unzipped IFCtoLBD Java runtime
```

## Troubleshooting

### PowerShell cannot run Activate.ps1

Run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

This changes the policy only for the current PowerShell window.

### `py -3.12` is not found

Close PowerShell and open it again. If it is still missing, reinstall Python:

```powershell
winget install --id Python.Python.3.12 --version 3.12.10 -e --accept-source-agreements --accept-package-agreements
```

### `java` is not found

Close PowerShell and open it again. Verify:

```powershell
java -version
```

If necessary, reinstall Java 17:

```powershell
winget install --id Microsoft.OpenJDK.17 --version 17.0.19.10 -e --accept-source-agreements --accept-package-agreements
```

### Ollama does not answer

Verify the service and installed model:

```powershell
Invoke-RestMethod "http://127.0.0.1:11434/api/tags"
ollama list
```

If `qwen3:8b` is missing:

```powershell
ollama pull qwen3:8b
```

Open Check Results and select `Restart Ollama`. Wait until the page reports that the model is warm before asking a question.
