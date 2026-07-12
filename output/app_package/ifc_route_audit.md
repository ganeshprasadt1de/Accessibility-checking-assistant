# IFC Route Audit

## IFC Data

- Spaces: 64
- Doors: 70
- Space boundaries: 1123
- Door-space boundary relations: 131
- Doors with space boundary: 66
- Doors without space boundary: 4
- Door boundary space-count histogram: {2: 63, 3: 1, 1: 2}

## Route Graph

- Route edges: 574
- Doors with route edges: 62
- Doors without route edges: 8
- Connected component sizes: [31, 26, 15]
- Route status counts: {'pass': 406, 'fail': 168}
- Failure reason counts: {'route_width': 2, 'door_width': 156, 'stair_block': 10}

## SHACL Route Rule

Route geometry measurements are written to RDF first. SHACL then checks route door width, route clear width, turning space, stair intersection, and ramp measurements. The app route status is copied from the SHACL validation results.

## Doors Without Space Boundary

- TU Durchgang:DL - 1200 x 2100:2432935 (3LJODRGPbDdfXHXShFLBtQ)
- TU Durchgang:DL - 1200 x 2100:2432939 (3LJODRGPbDdfXHXShFLBtM)
- TU Fassade - 1-flg-Drehflügel:GG - Türe 10 mm:2543322 (0ehNcYPbH3JQicvZQLHOvk)
- TU Fassade - 1-flg-Drehflügel:GG - Türe 10 mm:2543424 (0ehNcYPbH3JQicvZQLHO$q)
