# OpenCode for Amazon Bedrock

[![CI](https://github.com/hshokrig/opencode-bedrock/actions/workflows/ci.yml/badge.svg)](https://github.com/hshokrig/opencode-bedrock/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

OpenCode for Amazon Bedrock is a self-hosted OpenCode fork for running coding agents with Claude Opus through Amazon Bedrock. It is designed for Linux workspaces, including Amazon SageMaker environments that cannot reach the public internet.

The wrapper keeps one service per repository, survives terminal disconnects, accepts background tasks over a loopback-only API, and provides a focused terminal chat with durable history. A commit-specific offline archive contains the native OpenCode binary and all Python runtime files, so the target machine does not download packages during installation.

> [!IMPORTANT]
> This is an independent community fork. It is not an official OpenCode, Amazon Web Services, or Anthropic distribution. You provide and pay for the AWS account, SageMaker environment, Bedrock access, and Claude inference profile.

## Project status

The local harness, native session transport, offline installer, and Linux x64 release path are covered by automated tests. Live AWS checks are deliberately opt-in because they use your credentials and can incur Bedrock charges.

The repository does not currently publish supported prebuilt binaries on GitHub Releases. Build the offline archive from the commit you reviewed, or use source directly on a connected development machine.

The supported product is the Bedrock wrapper and the native packages included in its offline archive. The repository retains upstream web, desktop, console, statistics, enterprise, Slack, and SDK publishing code so upstream revisions can be imported cleanly, but this fork does not build or deploy those applications. Their dependency alerts remain visible for anyone who chooses to work on them.

## What it provides

- A persistent background service for each Git repository
- Basic agent tasks and a separate tool-free terminal chat
- Bedrock inference-profile routing for Claude Opus
- A bubblewrap workspace boundary on Linux
- Loopback-only HTTP with a generated service password
- An offline, checksum-verified installation archive
- Explicit approval for file edits and non-read-only shell commands
- Local tests that never call AWS unless `RUN_AWS_SMOKE=1` is set

## Requirements

- Linux x64 or ARM64
- Python 3.10 or newer
- Git
- `bubblewrap` (`bwrap`) with working user namespaces
- Bun 1.3.14 when building from source
- An AWS Region and an active Bedrock inference-profile ID or ARN whose destination models are Claude Opus
- AWS credentials from the SageMaker execution role or another AWS default-chain source

macOS and Windows are not native runtime targets. Use a Linux virtual machine or WSL with functional user namespaces.

## Build from source

Clone the fork on a connected Linux machine:

```bash
git clone https://github.com/hshokrig/opencode-bedrock.git
cd opencode-bedrock
git switch main

ALLOW_NETWORK_BOOTSTRAP=1 ./scripts/bootstrap.sh
export PATH="$HOME/.bun/bin:$PATH"
./scripts/build-offline.sh /tmp/opencode-bedrock-release
```

The build refuses a dirty worktree and records the source commit in the artifact manifest. Build on the same CPU architecture as the target.

Copy the archive, its `.sha256` file, `install-opencode-bedrock.sh`, and the installer's checksum to the target. Install them from the same directory:

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
sha256sum -c opencode-bedrock-*.tar.gz.sha256
./install-opencode-bedrock.sh opencode-bedrock-*.tar.gz

export PATH="$HOME/.local/bin:$PATH"
opencode-bedrock doctor
```

For an internet-isolated SageMaker target, follow the [deployment runbook](docs/deployment-runbook.md). Do not clone and build the source inside SageMaker unless the environment has an approved dependency mirror.

## Start a project

Set the AWS source Region and the Bedrock inference profile you intend to use:

```bash
export AWS_REGION='your-source-region'
export BEDROCK_INFERENCE_PROFILE='your-profile-id-or-arn'

opencode-bedrock project add \
  --name my-project \
  --path /absolute/path/to/repository

opencode-bedrock start --project my-project
opencode-bedrock status
```

Submit a task and follow it:

```bash
opencode-bedrock task --project my-project \
  "Inspect the failing tests and prepare the smallest safe fix."

opencode-bedrock tasks --project my-project
opencode-bedrock logs --project my-project --follow
```

File edits and non-read-only shell commands wait for approval by default:

```bash
opencode-bedrock approval list --project my-project
opencode-bedrock approval approve --project my-project REQUEST_ID
opencode-bedrock approval reject --project my-project REQUEST_ID \
  --message "Use the existing parser instead."
```

Open the native terminal client or use the focused chat:

```bash
opencode-bedrock attach --project my-project
opencode-bedrock chat --project my-project
```

Use `--workspace /absolute/path` instead of `--project` for an unregistered repository. Non-Git directories require `--allow-non-git` on `start`.

## Security boundaries

The selected workspace is mounted read-write inside a bubblewrap namespace. The OpenCode executable, operating-system commands, shared libraries, and certificate files are mounted read-only. Other home-directory content is absent, including AWS and SSH configuration, unrelated repositories, and common credential files.

The sandbox does not filter network traffic. Bedrock needs network access, so tool permissions remain part of the security boundary. Keep the service bound to `127.0.0.1`, do not expose its generated password, and read the [security model](docs/security-model.md) before changing the headless policy.

Never put AWS account IDs, role ARNs, inference-profile ARNs, bucket names, credentials, or service records in issues, logs, commits, or example configuration.

## Documentation

- [Deployment runbook](docs/deployment-runbook.md): connected build machine, laptop, and SageMaker transfer
- [Offline installation](docs/offline-installation.md): artifact contents, verification, upgrades, and rollback
- [SageMaker setup](docs/sagemaker-setup.md): target-image and storage prerequisites
- [AWS validation checklist](docs/aws-validation.md): paid live checks and acceptance tests
- [Architecture](docs/architecture.md): components and trust boundaries
- [Security model](docs/security-model.md): isolation guarantees and limits
- [Background service](docs/background-service.md): lifecycle, approvals, and recovery
- [Terminal chat](docs/terminal-chat.md): commands, persistence, and streaming
- [Project workspaces](docs/project-workspaces.md): registration and repository selection
- [Upstream sync](docs/upstream-sync.md): importing a new OpenCode revision

## Contributing and support

Start with [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Use the fork's issue tracker for Bedrock wrapper, SageMaker packaging, and fork-specific native changes. Report bugs that also occur in unmodified OpenCode to the [upstream OpenCode project](https://github.com/anomalyco/opencode/issues).

Read [SUPPORT.md](SUPPORT.md) for the information to include in a support request. Security reports belong in the private channel described in [SECURITY.md](SECURITY.md).

## Upstream and license

This fork is based on [OpenCode](https://github.com/anomalyco/opencode). The imported revision is recorded in [UPSTREAM_REVISION](UPSTREAM_REVISION), and upstream history is retained. Root-level localized `README.*.md` files are imported OpenCode documentation; they do not describe this Bedrock specialization.

OpenCode and this specialization are available under the [MIT License](LICENSE). See [NOTICE](NOTICE) for attribution.
