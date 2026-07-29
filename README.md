# OpenCode for Amazon Bedrock

This repository packages OpenCode as a private, Bedrock-first coding service for Amazon SageMaker. A service is tied to one repository. It keeps running after the terminal disconnects, accepts tasks over a loopback-only API, and uses an Amazon Bedrock inference profile for Claude Opus.

The imported OpenCode revision is recorded in [UPSTREAM_REVISION](UPSTREAM_REVISION). OpenCode's MIT license and history are preserved.

## What you need

- Linux x64 or ARM64
- Python 3.10 or newer
- `bubblewrap` (`bwrap`) for filesystem isolation
- An OpenCode binary from this source tree or the offline artifact
- An AWS Region and a Bedrock inference-profile ID or ARN
- AWS credentials from the SageMaker execution role or another AWS default-chain source

The local test suite does not call AWS. Real Bedrock checks require `RUN_AWS_SMOKE=1`.

## Start a project

```bash
export AWS_REGION=eu-north-1
export BEDROCK_INFERENCE_PROFILE='your-profile-id-or-arn'

opencode-bedrock project add \
  --name my-project \
  --path /absolute/path/to/repository

opencode-bedrock start --project my-project
opencode-bedrock status
```

All agents use the primary profile by default. Override one agent when needed:

```bash
opencode-bedrock start --project my-project \
  --agent-model review='another-profile-id-or-arn'
```

Submit a task and follow it:

```bash
opencode-bedrock task --project my-project \
  "Inspect the failing tests, explain the cause, and prepare the smallest safe fix."

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

Attach OpenCode's terminal client or stop the service:

```bash
opencode-bedrock attach --project my-project
opencode-bedrock stop --project my-project
```

For a focused, tool-free Claude conversation with durable history:

```bash
opencode-bedrock chat --project my-project
```

The terminal chat resumes its last selected Session, streams best-effort text, retains the full
transcript, and compacts active context near the 200,000-token policy ceiling. See
[Terminal chat](docs/terminal-chat.md) for commands, history behavior, call counts, offline
dependencies, and power-loss handling.

Use `--workspace /absolute/path` instead of `--project` for an unregistered repository. Non-Git directories require `--allow-non-git` on `start`.

## Safety boundaries

Each service runs inside a bubblewrap mount namespace. The selected workspace is mounted read-write. The OpenCode binary, operating-system commands, shared libraries, and certificate files are mounted read-only. Other home-directory content is absent, including `~/.aws`, `~/.ssh`, unrelated repositories, and credential files.

OpenCode also denies external-directory tools, reads of `.env` files, key files, and credential-like names, as well as web tools, auto-update, model-catalog downloads, external plugins, and LSP downloads. The server listens on `127.0.0.1` and uses a generated password stored in a mode-0600 service record.

The sandbox does not filter network traffic. Bedrock needs network access, so shell and web permissions are the control against model-directed network calls. Read [docs/security-model.md](docs/security-model.md) before enabling `--headless-policy workspace-write`.

## Build and install offline

Build on a networked Linux machine with the pinned Bun version:

```bash
ALLOW_NETWORK_BOOTSTRAP=1 ./scripts/bootstrap.sh
export PATH="$HOME/.bun/bin:$PATH"
./scripts/build-offline.sh
```

Copy the archive, its checksum, `install-opencode-bedrock.sh`, and the installer's checksum to SageMaker. Then run:

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
./install-opencode-bedrock.sh \
  opencode-bedrock-0.1.0-linux-x64.tar.gz

export PATH="$HOME/.local/bin:$PATH"
opencode-bedrock doctor
```

The artifact includes the OpenCode executable, this wrapper, the opt-in AWS verifier, the IAM template, documentation, license notices, a version manifest, and checksums. It does not download packages at runtime.

## Documentation

- [Architecture](docs/architecture.md)
- [Background service](docs/background-service.md)
- [Project and workspace handling](docs/project-workspaces.md)
- [Security model](docs/security-model.md)
- [Offline installation](docs/offline-installation.md)
- [SageMaker setup](docs/sagemaker-setup.md)
- [AWS validation checklist](docs/aws-validation.md)
- [Upstream sync procedure](docs/upstream-sync.md)

AWS has the final word on inference-profile permissions and Region routing. The IAM template follows the current Amazon Bedrock guidance for profile and destination-model resources:

- [Inference profile prerequisites](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)
- [Using an inference profile](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-use.html)
- [SageMaker execution roles](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-roles.html)
