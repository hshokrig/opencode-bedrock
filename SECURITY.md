# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private [Report a vulnerability](https://github.com/hshokrig/opencode-bedrock/security/advisories/new) form and include:

- the affected commit or artifact version
- the component and deployment environment
- the security boundary that was crossed
- a minimal reproducer with all AWS identifiers and credentials removed
- the impact you observed

Maintainers will respond as capacity allows. This is a community-maintained project and does not offer a guaranteed response time or commercial support agreement.

If the issue also exists in unmodified OpenCode, report it to the [upstream OpenCode security process](https://github.com/anomalyco/opencode/security) and mention that this fork may also be affected.

## Supported version

Security fixes are applied to the current `main` branch. Older commits and locally modified artifacts are not maintained releases.

## Fork threat model

The Bedrock wrapper is intended to add a Linux workspace boundary around the native OpenCode service:

- the service listens only on `127.0.0.1`
- each service receives a generated password stored in a mode-0600 record
- bubblewrap mounts the selected workspace read-write and required runtime files read-only
- AWS and SSH configuration, unrelated home directories, and common credential files are not mounted
- external-directory tools, credential-like files, web tools, model-catalog downloads, automatic updates, external plugins, and LSP downloads are denied by policy
- file edits and non-read-only shell commands require approval by default

These controls have limits. The sandbox does not filter network traffic. The model receives repository content and can act through the tools allowed by the active policy. A hostile repository can contain misleading instructions. Bedrock data handling, configured Model Context Protocol servers, SageMaker administration, IAM policy, VPC routing, and the host operating system remain outside this repository's isolation boundary.

Read [docs/security-model.md](docs/security-model.md) before deployment or policy changes.

## In scope

- escaping the configured bubblewrap workspace boundary
- bypassing loopback binding or service authentication
- bypassing the configured tool or approval policy
- leaking credentials or files that the documented sandbox says are unavailable
- replaying a provider request after the durable attempt record says it must not be replayed
- installer or artifact verification failures

Configuration choices that deliberately disable a documented boundary are not vulnerabilities in the default configuration. Reports still need a reproducible path from the supported defaults.
