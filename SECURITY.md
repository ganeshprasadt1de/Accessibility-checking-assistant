# Security

## Supported Version

Security reports are assessed against the latest published commit on `stable-version`. Old commits, forks and locally modified copies may no longer behave in the same way.

## Report A Vulnerability

Use the repository's [private vulnerability reporting page](https://github.com/ganeshprasadt1de/Accessibility-checking-assistant/security/advisories/new). Include the affected commit, Windows version, Python version, Java version, a minimal reproduction and the possible impact. Do not attach a confidential building model. If the failure needs an IFC file, make the smallest model that still reproduces it or describe how the maintainer can create one.

If private reporting is unavailable, contact the repository owner through the GitHub profile before publishing technical details.

## Safe Local Use

- Keep the server bound to `127.0.0.1`. Do not expose it directly to a public or office network.
- Treat uploaded IFC files as untrusted input. Parsing and RDF conversion can consume substantial CPU, memory and disk space.
- Run the project as a normal Windows user, not as Administrator.
- Use the Python and Java versions documented in the README. Test compatible security updates before changing the pinned environment.
- Before using a downloaded release, run the README's SHA-256 check for all 170 bundled IFCtoLBD JAR files. The application itself checks that its main JARs exist; it does not automatically verify the complete checksum manifest at startup.
- Review generated reports before using them in a design decision. This project is not a certified building-control or accessibility-approval service.

`Stop Project Services` only stops processes after checking their executable, command line or project API response. It is a cleanup control for the local server and Ollama, not a security boundary for other software on the computer.
