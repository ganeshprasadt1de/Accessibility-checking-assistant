# Contributing To Wheelchair Route Checker

Contributions should solve a clearly described problem and stay within the scope of wheelchair-route checking from IFC data.

## Describe The Problem

Before writing a larger change, open an issue with the affected commit and enough information to reproduce the problem. For a routing problem, include the model, floor and relevant coordinates or route identifier. For a display problem, include a screenshot and the browser name. Never upload a confidential building model to a public issue.

## Project Boundaries

- Accessibility status comes from extracted IFC measurements, route geometry and SHACL. Ollama may explain a recorded result, but it must not create or replace that result.
- Routing code must work from IFC geometry and configuration values. Do not place sample-model coordinates, GUIDs or route identifiers in production logic.
- The 2D plan and 2.5D simulation must present the same coordinates and route status as the generated package.
- A change to preprocessing or routing requires new packages for both demonstration models. Check the reported problem again after regeneration.
- Do not remove copyright, licence or attribution text from third-party files.

## Before Opening A Pull Request

Create the Python 3.12 environment described in the README, then run:

```powershell
python -m compileall backend preprocess.py server.py
```

Regenerate both included IFC models and test the affected page in a browser. The pull request should state what was tested and provide the IFC or rule evidence for any result that changed. Recheck earlier routing cases when the same navigation function affects more than one floor or view.

## Contribution Licence

Only submit work you wrote or have permission to contribute. By submitting a contribution for inclusion, you agree that your contribution is available under the repository's [PolyForm Noncommercial License 1.0.0](LICENSE), including its required notice. Third-party material must retain its original licence and be recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The repository licence does not grant commercial use. A separate written agreement from every relevant copyright holder is needed before a contribution can be offered under different commercial terms.
