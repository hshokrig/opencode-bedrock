# Laptop and SageMaker release runbook

Use one reviewed Git commit as the release identity. Build the offline archive on a connected Linux
machine with the same CPU architecture as the target. SageMaker installs that archive; it does not
clone or build the repository.

## 1. Connected build machine

```bash
git switch main
git pull --ff-only origin main
git status --short
python3 -m unittest discover -s tests/bedrock -t .

(cd packages/schema && bun typecheck)
(cd packages/core && bun typecheck)
(cd packages/server && bun typecheck)
(cd packages/opencode && bun typecheck)

ALLOW_NETWORK_BOOTSTRAP=1 ./scripts/bootstrap.sh
export PATH="$HOME/.bun/bin:$PATH"
./scripts/build-offline.sh /tmp/opencode-bedrock-release
```

The build refuses a dirty tree. Record `git rev-parse HEAD`, `uname -m`, and the two SHA-256 values
printed in `/tmp/opencode-bedrock-release`. Keep the expected digests in a trusted channel separate
from the files being transferred.

## 2. New Linux laptop

The supported targets are Linux x64 and Linux ARM64. A macOS or Windows laptop needs a Linux VM or
WSL environment with functional user namespaces and `bubblewrap`.

Either clone the same commit on a connected laptop and repeat the local tests, or transfer and
install the four release files exactly as SageMaker will:

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
sha256sum -c opencode-bedrock-*.tar.gz.sha256
./install-opencode-bedrock.sh opencode-bedrock-*.tar.gz
export PATH="$HOME/.local/bin:$PATH"
opencode-bedrock doctor
```

Set temporary AWS credentials only if this laptop is authorized for the selected Bedrock profile.
Then complete steps 1–10 of [the AWS validation checklist](aws-validation.md), including one basic
agent task and both terminal-chat output modes. Do not put credentials, profile ARNs, endpoints, or
account identifiers in Git.

## 3. Internet-isolated SageMaker

Before transfer, the SageMaker image or administrator-managed layer must provide Python 3.10+,
Git, `bwrap`, and a persistent local/block-backed path compatible with SQLite WAL. Point
`XDG_STATE_HOME` at that path before the first start. Do not use an unverified EFS/NFS path for the
live Session database.

Transfer these four files through the approved upload flow or S3 VPC endpoint:

- `opencode-bedrock-<release>-linux-<architecture>.tar.gz`
- its `.sha256` file
- `install-opencode-bedrock.sh`
- its `.sha256` file

Verify the out-of-band digests, then install:

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
sha256sum -c opencode-bedrock-*.tar.gz.sha256
./install-opencode-bedrock.sh opencode-bedrock-*.tar.gz
export PATH="$HOME/.local/bin:$PATH"

export AWS_REGION='SOURCE_REGION'
export BEDROCK_INFERENCE_PROFILE='PROFILE_ID_OR_ARN'
# Set BEDROCK_RUNTIME_ENDPOINT and BEDROCK_CONTROL_ENDPOINT only for approved AWS endpoints.

opencode-bedrock doctor
RUN_AWS_SMOKE=1 opencode-bedrock-verify-aws
```

Apply the reviewed IAM policy before the paid smoke test. The inference profile must be active and
must list the intended Claude Opus model in every required destination Region.

Create a small disposable Git repository on the persistent workspace volume and complete every
step in [the AWS validation checklist](aws-validation.md). In particular, verify the basic agent
read/command/approval flow, the out-of-workspace denial, terminal chat in final and streaming modes,
terminal disconnect persistence, and a full SageMaker application restart.

## Release decision

Promote the artifact only when all local tests pass, the manifest `source_revision` equals the
reviewed Git commit with `source_dirty: false`, `doctor` passes on the exact target image, both
Bedrock invocation modes pass, the basic agent and chat flows pass, and the Session database
survives an application restart on the chosen storage path.
