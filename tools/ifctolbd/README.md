# Included IFCtoLBD Runtime

`java_libraries` contains the complete unzipped Java classpath used by the application.

The main components are:

- IFCtoLBD 2.43.4
- Apache Jena 4.10.0
- the IFC geometry and IFC parsing libraries required by IFCtoLBD
- matching Java runtime dependencies

The application runs this classpath directly from `backend/ifc_tools.py`. Maven and an IFCtoLBD ZIP archive are not required.

Do not replace individual JAR files with newer releases. The files are tested as one set. Mixing IFCtoLBD, Jena or geometry-library versions can change RDF output or prevent Turtle parsing.

`java_libraries/SHA256SUMS.txt` records the SHA-256 checksum of every included JAR. The verification command is documented in the project README.

Java 17 is not included in this repository and must be installed separately.
