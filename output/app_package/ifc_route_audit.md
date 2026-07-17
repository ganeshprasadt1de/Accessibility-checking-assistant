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

- Route edges: 582
- Doors with route edges: 68
- Doors without route edges: 2
- Connected component sizes: [59, 13, 3, 3]
- Route status counts: {'pass': 81, 'fail': 501}
- Failure reason counts: {'door_height': 476, 'unreachable': 19, 'route_width': 473, 'door_width': 156, 'stair_block': 77}
- Skipped door pairs: 0

## SHACL Route Rule

Route geometry measurements are written to RDF first. SHACL then checks door dimensions, corridor width, slope and passing areas, route geometry, stairs, and ramp measurements. The app route status is copied from the SHACL validation results.

## Doors Without Space Boundary

- TU Durchgang:DL - 1200 x 2100:2432935 (3LJODRGPbDdfXHXShFLBtQ)
- TU Durchgang:DL - 1200 x 2100:2432939 (3LJODRGPbDdfXHXShFLBtM)
- TU Fassade - 1-flg-Drehflügel:GG - Türe 10 mm:2543322 (0ehNcYPbH3JQicvZQLHOvk)
- TU Fassade - 1-flg-Drehflügel:GG - Türe 10 mm:2543424 (0ehNcYPbH3JQicvZQLHO$q)
