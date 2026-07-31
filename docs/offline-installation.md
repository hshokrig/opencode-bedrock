# Offline installation

Build the artifact on Linux with public package access. The target SageMaker runtime does not need public access.
The source build requires the repository-pinned Bun 1.3.14 toolchain, npm registry access for the
locked dependency graph, and GitHub access for the locked `ghostty-web` source dependency. The
build embeds the repository's empty external model-catalog snapshot because this service configures
its Bedrock profile explicitly; it does not fetch live `models.dev` data. An approved, already
compiled `OPENCODE_BIN` avoids all package and source downloads during packaging.

## Build

```bash
git checkout main
test "$(cat UPSTREAM_REVISION)" = "7565e03536d19e850f9996c407f9bf5e932b5f7a"
test -z "$(git status --porcelain --untracked-files=normal)"

ALLOW_NETWORK_BOOTSTRAP=1 ./scripts/bootstrap.sh
export PATH="$HOME/.bun/bin:$PATH"
./scripts/build-offline.sh /tmp/opencode-bedrock-artifacts
```

The build uses `bun install --frozen-lockfile` and compiles one native OpenCode binary for the build host. Its release ID and archive name include the specialization Git commit; the manifest records that commit, the imported upstream commit, and whether the source tree was dirty. Release builds refuse dirty trees. Archive ownership, ordering, timestamps, and gzip metadata are normalized. Build x64 on Linux x64 and ARM64 on Linux ARM64. The artifact name includes the architecture.

For a repeatable build using an already compiled OpenCode binary:

```bash
OPENCODE_BIN=/absolute/path/to/opencode \
  ALLOW_UNVERIFIED_OPENCODE_BIN=1 \
  ./scripts/build-offline.sh /tmp/opencode-bedrock-artifacts
```

`OPENCODE_BIN` bypasses the native source build and is therefore rejected unless the explicit
override is present. Use it only for packaging tests or when that exact binary has separate
provenance and protocol-compatibility evidence. Do not use it for the SageMaker release described
here.

## Transfer and install

Transfer the following four files through SageMaker upload or an approved S3 prefix:

- `opencode-bedrock-0.1.0+SOURCE_COMMIT-linux-x64.tar.gz`
- `opencode-bedrock-0.1.0+SOURCE_COMMIT-linux-x64.tar.gz.sha256`
- `install-opencode-bedrock.sh`
- `install-opencode-bedrock.sh.sha256`

```bash
sha256sum -c install-opencode-bedrock.sh.sha256
sha256sum -c opencode-bedrock-0.1.0+SOURCE_COMMIT-linux-x64.tar.gz.sha256
./install-opencode-bedrock.sh opencode-bedrock-0.1.0+SOURCE_COMMIT-linux-x64.tar.gz
export PATH="$HOME/.local/bin:$PATH"
opencode-bedrock doctor
```

The installer requires and verifies the outer checksum, rejects traversal, links, devices, duplicate
archive paths, unsafe release IDs, dirty-source release artifacts, and platform/architecture/Python
mismatches. It verifies every file against the inner `SHA256SUMS` and installs into a
commit-versioned directory. It refuses to replace a regular file in `~/.local/bin`. Obtain the
expected archive digest through a trusted channel separate from the four transferred files;
checksums copied beside an archive detect corruption but do not authenticate its publisher.

The target needs no `sudo`. Its local utilities are `bash`, Python 3.10+, `sha256sum`, `tar`,
`gzip`, `realpath`, `awk`, `find`, `ln`, `mv`, `uname`, `git`, and `bwrap`, plus ordinary system
libraries and AWS role credentials. AWS CLI and Boto3 are needed only for the documented validation
commands, not for normal wrapper execution. The artifact supplies OpenCode and the Python wrapper.
No pip, npm, Bun, or public registry is used on the target.

## Upgrade

Build a new artifact from a reviewed upstream merge. Before first start, back up the private XDG
state directory. Install it beside the old version and run `doctor`. The installer atomically
updates a shared `current` release link used by `opencode`, `opencode-bedrock`, and
`opencode-bedrock-verify-aws`. `opencode-bedrock restart` resolves that current release by default;
use its `--opencode-bin` option only for an intentional override.
Keep the previous version until the SageMaker checks pass.

Database and durable-event upgrades are one-way unless a release explicitly documents otherwise.
After a new binary has opened and migrated a state directory, switching only the executable back
is not a supported rollback. Restore the matching pre-upgrade state backup together with the old
binary.

Runtime auto-update, model-catalog downloads, repository plugins, external skills, and LSP
downloads are disabled. In terminal-chat mode no tools are exposed, so the only non-loopback
network calls are AWS credential resolution and the configured Amazon Bedrock endpoint.

The Session database uses SQLite WAL with full synchronous durability. Put `XDG_STATE_HOME` on a
persistent local or block-backed filesystem whose locking and shared-memory behavior supports
SQLite WAL. Do not place the live database on EFS, NFS, or another network filesystem without an
explicit compatibility test and security review. Stop the service before copying its state for a
backup; restore the complete matching state directory and release together.
