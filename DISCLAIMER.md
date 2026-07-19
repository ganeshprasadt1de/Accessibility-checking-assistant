# Scope And Limitations

Wheelchair Route Checker is a research and design-review tool. It is not a certified building-control system, a site survey, an accessibility audit by a qualified professional or approval from the responsible authority.

Results depend on the content and quality of the uploaded IFC model. Missing spaces, incorrect door dimensions, overlapping storeys, incomplete boundaries or simplified bounding boxes can change a route result. The software currently focuses on selected wheelchair-mobility checks; it does not cover every disability, building type, exception or legal requirement.

DIN standards are copyrighted publications. This repository contains implemented measurements and SHACL constraints, not the complete text of DIN 18040-1. Users must consult the official standard, the applicable edition, local building law and project-specific requirements before relying on a result.

A green route means that the route passed the checks implemented by this software using the available IFC data. It does not prove that the finished building is accessible. A red route identifies a measured failure or blocked candidate under those checks; the model and design still need professional review.

The software is provided without warranty under the terms in [LICENSE](LICENSE). Third-party software and sample IFC files remain subject to the separate terms listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
