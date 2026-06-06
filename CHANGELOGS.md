# Changelogs

## v0.01

- Required IFCtoLBD for raw RDF graph generation.
- Removed the Python-created LBD graph fallback from preprocessing.
- Made missing Java, Maven, IFCtoLBD ZIP, converter build errors, and pySHACL import errors stop the pipeline with clear messages.
- Moved compliance issue creation to SHACL validation results.
- Kept Python only for IFC geometry extraction, route geometry preparation, and RDF measurement enrichment.
- Removed nearest-door route fallback when IFC space-boundary routes are unavailable.
- Added SHACL checks for door width, corridor width, route width, turning space, stair blockers, and ramp measurements.
- Changed the website assistant so missing Ollama requires `python server.py --yes` and returns SHACL report data instead of local generated text.
