# IFC Route Audit

## IFC Data

- Spaces: 82
- Doors: 77
- Space boundaries: 1000
- Door-space boundary relations: 153
- Doors with space boundary: 77
- Doors without space boundary: 0
- Door boundary space-count histogram: {2: 76, 1: 1}

## Route Graph

- Route edges: 317
- Doors with route edges: 81
- Doors without route edges: -4
- Connected component sizes: [23, 22, 19, 17]
- Route status counts: {'pass': 313, 'fail': 4}
- Failure reason counts: {'stair_block': 4}

## SHACL Route Rule

Route geometry measurements are written to RDF first. SHACL then checks route door width, route clear width, turning space, stair intersection, and ramp measurements. The app route status is copied from the SHACL validation results.
