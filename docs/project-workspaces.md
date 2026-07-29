# Projects and workspaces

Registration gives an absolute repository path a stable alias:

```bash
opencode-bedrock project add --name api --path /home/sagemaker-user/src/api
opencode-bedrock project list
```

The registry is outside the target repository. Registration does not create `AGENTS.md`, `.opencode`, or any other repository file.

Paths must be absolute, must exist, and are resolved to their canonical form. Git repositories are the default. Use `--allow-non-git` only when the directory is intentionally not a repository.

The wrapper rejects a second alias for the same path and refuses to run two services for one canonical workspace. Every task, permission request, log, and session call includes that workspace explicitly.

Workspaces may not overlap protected Linux runtime trees such as `/etc`, `/usr`, `/var`, `/proc`, or `/dev`. User credential roots such as `~/.aws`, `~/.ssh`, and `~/.config` are also rejected. This prevents a broad non-Git mount from making system files writable or exposing host credentials.

## Filesystem boundary

The sandbox mounts one workspace read-write. Other registered repositories and the rest of the home directory are not mounted. A symlink that points outside the workspace has no reachable target inside the sandbox. OpenCode's own external-directory check also resolves symlinks before evaluating permission rules.

The service exposes a small operating-system view so tools such as Git, ripgrep, compilers, and test runners can start. System paths are read-only. `/tmp` is private to the service.

Repository-local OpenCode configuration is disabled in service mode. This prevents a target repository from loosening the service policy or loading a plugin that needs a public package registry. A root `AGENTS.md` is loaded as an instruction file, and OpenCode loads nested instruction files when it reads files below them.
