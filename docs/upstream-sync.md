# Syncing OpenCode

The `upstream` remote points to `https://github.com/anomalyco/opencode`. The specialization starts from the commit in `UPSTREAM_REVISION`.

Use a short-lived branch:

```bash
git fetch upstream dev --tags
git switch -c upstream-sync
git merge --no-ff upstream/dev
```

Resolve conflicts without dropping the service wrapper, offline scripts, license notice, or security tests. Pay particular attention to:

- `packages/opencode/src/provider/provider.ts`
- `packages/opencode/src/tool/external-directory.ts`
- session, permission, server, and attach APIs used by `opencode_bedrock/api.py`
- OpenCode build output paths

After the merge, replace `UPSTREAM_REVISION` with `git rev-parse upstream/dev`. Run the Python suite, the two focused OpenCode test files, the OpenCode type check, the offline build, checksum verification, license review, and secret scan.

Record the upstream commit in the sync commit message. Merge the sync branch into `main` only after the offline artifact and mock lifecycle test pass.
