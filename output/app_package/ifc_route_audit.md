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

- Route edges: 313
- Doors with route edges: 77
- Doors without route edges: 0
- Connected component sizes: [22, 21, 18, 16]
- Route status counts: {'pass': 313}
- Failure reason counts: {}

## SHACL Route Rule

The current SHACL route rule checks acc:routeStatus = 'fail'. The dependency facts are calculated before SHACL by the backend. This is useful for reporting but it is not a full route-planning proof inside SHACL.
