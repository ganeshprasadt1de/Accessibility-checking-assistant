# Third-Party Software And Data

The licence in [LICENSE](LICENSE) covers Wheelchair Route Checker material only where its copyright holder has made that material available under those terms. The software and model data below keep their own terms.

## IFCtoLBD

This project uses **IFCtoLBD 2.43.4** to convert IFC STEP files into RDF triples. The unzipped Java runtime is stored in `tools/ifctolbd/java_libraries/` so preprocessing can run without Maven.

IFCtoLBD is maintained by [Jyrki Oraskari and the IFCtoLBD contributors](https://github.com/jyrkioraskari/IFCtoLBD). Its upstream README provides the author list, acknowledgements and citation format.

IFCtoLBD is distributed under the Apache License 2.0. A copy of that licence is stored in [`tools/ifctolbd/LICENSE-APACHE-2.0.txt`](tools/ifctolbd/LICENSE-APACHE-2.0.txt). The 170 JAR files in the bundled runtime include IFCtoLBD dependencies with their own upstream licences. Their file names and SHA-256 hashes are recorded in [`tools/ifctolbd/java_libraries/SHA256SUMS.txt`](tools/ifctolbd/java_libraries/SHA256SUMS.txt). Those licences remain in force; the project licence does not relicense the JAR files.

## Three.js

The browser views use **Three.js 0.165.0**, including `three.module.js`, `OrbitControls.js` and `GLTFLoader.js`. Three.js is copyright 2010-2024 the Three.js authors and is distributed under the MIT License. The included licence is at [`frontend/vendor/three/LICENSE`](frontend/vendor/three/LICENSE).

## Python Packages

`requirements.txt` installs these packages rather than copying their source into this repository. Each package remains under its own licence.

| Package | Pinned version | Licence reported by the package project |
| --- | ---: | --- |
| IfcOpenShell | 0.8.5 | LGPL-3.0-or-later |
| RDFLib | 7.6.0 | BSD-3-Clause |
| pySHACL | 0.40.0 | Apache-2.0 |
| Shapely | 2.1.2 | BSD-3-Clause |
| html5rdf | 1.2.1 | MIT |
| isodate | 0.7.2 | BSD-3-Clause |
| Lark | 1.3.1 | MIT |
| NumPy | 2.3.5 | BSD-3-Clause; its distribution contains additional notices |
| owlrl | 7.6.2 | W3C-20150513 |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| PrettyTable | 3.18.0 | BSD-3-Clause |
| pyparsing | 3.3.2 | MIT |
| python-dateutil | 2.9.0.post0 | dual-licensed under Apache-2.0 or BSD-3-Clause |
| six | 1.17.0 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| wcwidth | 0.8.2 | MIT |

The installed wheel or source distribution contains the authoritative copyright and licence text for each Python package.

## Ollama And Local Language Models

Ollama and its models are downloaded separately and are not included in this repository. The server prefers the model named by `OLLAMA_MODEL`; its documented default is `qwen3:8b`, and it can use another installed model when configured. Ollama's software terms and the selected model's terms apply. The language model explains facts supplied by the backend. It does not calculate routes or decide SHACL compliance.

## Demonstration IFC Models

### DigitalHub

`20201208DigitalHub_ARC.ifc` comes from the [RWTH-E3D DigitalHub model collection](https://github.com/RWTH-E3D/DigitalHub). That repository is published under the MIT License by RWTH Aachen University's E3D Institute of Energy Efficiency and Sustainable Building. A copy of its licence is included at [`licenses/DigitalHub-MIT.txt`](licenses/DigitalHub-MIT.txt).

The bundled filename is an earlier DigitalHub export and is not the filename of the current architecture model in the upstream repository. This notice identifies its model family and source; it does not claim that the two files are byte-for-byte identical.

### KIT IFC Example

`AC20-Institute-Var-2.ifc` is from the [KIT IFC Examples](https://www.ifcwiki.org/index.php?title=KIT_IFC_Examples) collection. The source page states that the examples are for unrestricted use and asks publications to credit the Institute for Automation and Applied Informatics (IAI), Karlsruhe Institute of Technology (KIT). The [file page](https://www.ifcwiki.org/index.php?title=File%3AAC20-Institute-Var-2.ifc) records the downloadable example.

The local file's content hash does not match the file currently served by IFC Wiki. It may be a different export or revision. For that reason, this repository records the source and requested credit but does not describe the local copy as an exact mirror of the current download.

The PolyForm licence for the Wheelchair Route Checker does not replace the terms attached to either model.

## Standards And Names

DIN 18040-1, IFC, RDF, SHACL, BOT, Three.js, IFCtoLBD, Ollama and Qwen are names of standards, projects or products owned by their respective organizations. Mentioning them describes interoperability and does not imply endorsement.
