# Security model

The main boundary is a Linux mount namespace created by bubblewrap. OpenCode and every command it starts see the selected workspace, a private service home, a private `/tmp`, and a read-only operating-system runtime. They do not see the caller's home directory or unrelated workspaces.

The service refuses to start without a usable `bwrap`. This is deliberate. OpenCode's shell path scanner can warn about common external paths, but a shell is expressive enough to bypass string-based checks.

## Default permissions

The service allows file listing, search, normal reads, planning, task tracking, and selected read-only Git commands. It asks before edits and other shell commands. It denies:

- external-directory access
- `.env`, private-key, secret-like, and credential-like files
- web search and web fetch
- public model-catalog requests, updates, plugin installs, and LSP downloads
- repository-local OpenCode configuration

Unattended diff commands include `--no-ext-diff --no-textconv`, and Git filesystem monitors are disabled. This prevents repository configuration from turning a status or diff check into an external command.

The subprocess environment is rebuilt from an allowlist. Execution-role and other non-profile-file AWS credential-chain variables, proxy settings, locale, XDG paths, and OpenCode service settings pass through. `AWS_PROFILE`, `AWS_CONFIG_FILE`, and `AWS_SHARED_CREDENTIALS_FILE` are not passed or mounted. Unrelated host environment variables do not.

Standard input is closed for the service and for OpenCode shell tools. A background command cannot wait for hidden terminal input. OpenCode applies its own command timeout and output limits.

## Remaining risks

Network access is not namespaced because OpenCode must reach Bedrock and may need an AWS credential endpoint. A user-approved shell command can use the network. Keep shell approvals narrow and use VPC egress controls where the workload requires stronger enforcement.

The mount boundary hides host credential files, but it cannot hide a secret stored inside the selected workspace. File tools deny common secret names. An approved shell command can still read any file mounted in that workspace and can inherit AWS environment credentials. Review commands with that in mind.

`workspace-write` permits model-directed file changes without an attached client. Git history remains the practical recovery path. Do not enable it for an untrusted repository or a workspace containing data that cannot be restored.

The service password protects the loopback API from other local processes that do not have access to the mode-0600 state file. It is not a substitute for multi-user host isolation.

Cross-Region inference can move prompt data to every destination Region in the chosen profile. Check the profile's current Region list, organization SCPs, and data-residency requirements before use.
