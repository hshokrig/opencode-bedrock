# Contributing

Thanks for helping improve OpenCode for Amazon Bedrock. This repository contains a full OpenCode source tree plus a Bedrock and SageMaker specialization. Check which side of that boundary your change belongs to before you start.

Use this repository for changes to:

- `opencode_bedrock`, `scripts`, `policies`, and the root `docs` directory
- Bedrock-specific behavior in the native OpenCode packages
- Offline packaging, SageMaker operation, workspace isolation, or terminal chat

If a bug or feature is independent of Bedrock and reproduces in unmodified OpenCode, open it in the [upstream repository](https://github.com/anomalyco/opencode) instead. Keeping general OpenCode work upstream makes future syncs safer.

## Development setup

The supported development environment is Linux. Install Python 3.10 or newer, Git, `bubblewrap`, and the pinned Bun version:

```bash
git clone https://github.com/hshokrig/opencode-bedrock.git
cd opencode-bedrock
ALLOW_NETWORK_BOOTSTRAP=1 ./scripts/bootstrap.sh
export PATH="$HOME/.bun/bin:$PATH"
```

The bootstrap script downloads dependencies. The finished offline artifact does not.

## Tests

Run the Python harness tests from the repository root:

```bash
python3 -m unittest discover -s tests/bedrock -t .
```

Run native tests from their package directories. Do not run the repository-root test command; it is intentionally blocked.

```bash
(cd packages/schema && bun test test/event.test.ts)
(cd packages/server && bun test test/session-title-coordinator.test.ts)
(cd packages/core && bun test --max-concurrency 1 test/database-migration.test.ts test/session-create.test.ts test/session-prompt.test.ts test/session-run-coordinator.test.ts)
(cd packages/opencode && bun test --timeout 10000 --max-concurrency 1 test/server/httpapi-session.test.ts test/server/httpapi-workspace.test.ts)
```

Typecheck the packages touched by the specialization:

```bash
(cd packages/schema && bun typecheck)
(cd packages/core && bun typecheck)
(cd packages/server && bun typecheck)
(cd packages/opencode && bun typecheck)
```

AWS smoke tests must remain opt-in. Do not add live Bedrock or SageMaker calls to the default test suite. Run `RUN_AWS_SMOKE=1 opencode-bedrock-verify-aws` only in an authorized AWS environment where charges are understood.

## Security rules

- Never commit AWS account IDs, role ARNs, inference-profile ARNs, bucket names, access keys, session tokens, or generated service records.
- Keep the service on `127.0.0.1`.
- Do not weaken the bubblewrap workspace boundary or detached approval policy without a security review.
- Use placeholders in tests and documentation.
- Do not post sensitive logs in an issue or pull request.

Report suspected vulnerabilities through the private process in [SECURITY.md](SECURITY.md).

## Pull requests

Use a short branch name with no type prefix, such as `session-recovery` or `offline-install`.

PR titles and commits use conventional commit form:

```text
fix(bedrock): preserve task identity during retry
docs: clarify offline installation
test(core): cover session recovery
```

Keep a pull request focused. Explain the behavior change, the risk, and the checks you ran. Update the relevant documentation when commands, configuration, permissions, or recovery behavior change.

When importing upstream changes, update [UPSTREAM_REVISION](UPSTREAM_REVISION) and follow [docs/upstream-sync.md](docs/upstream-sync.md).

By contributing, you agree that your contribution is released under the repository's [MIT License](LICENSE).
