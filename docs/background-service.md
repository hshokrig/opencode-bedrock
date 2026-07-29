# Background service

`opencode-bedrock start` launches `opencode serve` in a new session with standard input closed. Output goes to a project-specific log. The CLI waits for `/global/health` before reporting success.

The service record contains the PID, Linux process start time, port, workspace, policy, model settings, and generated server password. PID checks compare both the number and the kernel start time, so a reused PID is not signaled by mistake. A stale record is replaced on the next start.

## Commands

```bash
opencode-bedrock start --project NAME
opencode-bedrock start --project NAME --foreground
opencode-bedrock status
opencode-bedrock restart --project NAME
opencode-bedrock logs --project NAME --follow
opencode-bedrock attach --project NAME
opencode-bedrock stop --project NAME
```

The server always binds to `127.0.0.1`. This wrapper has no remote-listen option. Use SageMaker's existing terminal or an authenticated tunnel if remote access is approved.

`stop` sends SIGTERM to the service process group and waits up to 15 seconds. It uses SIGKILL only when the group does not exit. OpenCode closes service resources on SIGTERM.

Logs rotate on start after reaching 5 MiB. Three old files are retained. A workspace banner is the first line of each log.

## Detached approvals

OpenCode keeps a permission request pending while the task waits. The wrapper reports that task as `awaiting approval`.

```bash
opencode-bedrock tasks --project NAME
opencode-bedrock approval list --project NAME
opencode-bedrock approval approve --project NAME REQUEST_ID
opencode-bedrock approval always --project NAME REQUEST_ID
opencode-bedrock approval reject --project NAME REQUEST_ID --message "Try a read-only check."
```

`approve` applies once. `always` applies for the current OpenCode session. A rejection message is returned to the agent as corrective feedback.

The default `approval` policy asks before edits and non-read-only shell commands. `workspace-write` allows file edits without a client but still asks for shell commands outside the small read-only allowlist.

Approval replies sent through the wrapper are appended to the service log with the request ID and action. OpenCode keeps the corresponding permission request and tool result in the session.

All agents use the primary inference profile unless `start` receives an override such as `--agent-model review=PROFILE`. Valid agent names are `plan`, `build`, `explore`, `implement`, `review`, and `test`. Overrides are stored in the private service record and are redacted from `status`.
