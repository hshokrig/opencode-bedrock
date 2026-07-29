# Offline installation

Build the artifact on Linux with public package access. The target SageMaker runtime does not need public access.

## Build

```bash
git checkout main
test "$(cat UPSTREAM_REVISION)" = "7565e03536d19e850f9996c407f9bf5e932b5f7a"

ALLOW_NETWORK_BOOTSTRAP=1 ./scripts/bootstrap.sh
export PATH="$HOME/.bun/bin:$PATH"
./scripts/build-offline.sh /tmp/opencode-bedrock-artifacts
```

The build uses `bun install --frozen-lockfile` and compiles one native OpenCode binary for the build host. Its version is derived from `UPSTREAM_REVISION`, and archive ownership, ordering, timestamps, and gzip metadata are normalized. Build x64 on Linux x64 and ARM64 on Linux ARM64. The artifact name includes the architecture.

For a repeatable build using an already compiled OpenCode binary:

```bash
OPENCODE_BIN=/absolute/path/to/opencode \
  ./scripts/build-offline.sh /tmp/opencode-bedrock-artifacts
```

## Transfer and install

Transfer the following four files through SageMaker upload or an approved S3 prefix:

- `opencode-bedrock-0.1.0-linux-x64.tar.gz`
- `opencode-bedrock-0.1.0-linux-x64.tar.gz.sha256`
- `install-opencode-bedrock.sh`
- `install-opencode-bedrock.sh.sha256`

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
sha256sum -c opencode-bedrock-0.1.0-linux-x64.tar.gz.sha256
./install-opencode-bedrock.sh opencode-bedrock-0.1.0-linux-x64.tar.gz
export PATH="$HOME/.local/bin:$PATH"
opencode-bedrock doctor
```

The installer requires and verifies the outer checksum, rejects traversal, links, devices, and duplicate archive paths, verifies every file against the inner `SHA256SUMS`, and installs into a versioned directory. It refuses to replace a regular file in `~/.local/bin`.

## Upgrade

Build a new artifact from a reviewed upstream merge. Install it beside the old version, run `doctor`, then restart each service. The installer updates the `opencode`, `opencode-bedrock`, and `opencode-bedrock-verify-aws` symlinks. Keep the previous version until the SageMaker checks pass.

Runtime auto-update, model-catalog downloads, repository plugins, external skills, and LSP downloads are disabled. A task can still run a package manager after explicit shell approval if the sandbox contains the command, but it will fail without an approved package source.
