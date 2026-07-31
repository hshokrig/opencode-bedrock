# Architecture

The specialization leaves OpenCode's agent loop, session database, terminal client, provider stack, and HTTP server in place. The added `opencode-bedrock` command handles the parts that are specific to persistent SageMaker use.

```text
opencode-bedrock CLI
  ├── project registry in XDG config
  ├── service and task state in XDG state
  ├── loopback HTTP client
  └── bubblewrap process boundary
        └── opencode serve
              ├── durable sessions and process-local pending permissions
              ├── plan, build, explore, implement, review, test
              ├── workspace tools
              └── Amazon Bedrock provider
                    └── Claude Opus inference profile
```

## Why the wrapper is separate

OpenCode already has `serve`, `attach`, async prompting, session persistence, permission requests, Bedrock streaming, tool calling, and context compaction. Replacing those systems would create a large fork. The wrapper registers repositories, starts one constrained server per workspace, keeps process state, rotates logs, and turns the existing HTTP endpoints into the requested task and approval commands.

Two upstream runtime paths changed for this specialization:

- The Bedrock provider now loads when it is explicitly configured, even when credentials are available only through the default AWS credential chain.
- Bedrock model and inference-profile ARNs bypass the provider's Region-prefix logic.
- External-path checks resolve symlinks and missing descendants before applying workspace permissions.

The imported session UI also has a semantics-preserving string-escape fix so the repository-wide lint command has no errors.

## State

Project registration is stored in `${XDG_CONFIG_HOME:-~/.config}/opencode-bedrock/projects.json`. Service records, generated passwords, logs, and task indexes live in `${XDG_STATE_HOME:-~/.local/state}/opencode-bedrock/services`.

OpenCode receives service-specific XDG directories inside that service state directory. Sessions therefore remain available after a client disconnect or service restart on the same persistent filesystem.

Task prompts use durable V2 admission with client-generated Session and message IDs. An uncertain
HTTP response is retried only with those exact IDs. If the result remains unknown, the task index
retains `delivery unknown` instead of treating the task as idle. Provider calls themselves are not
blindly replayed after an uncertain attempt.

Pending approval requests and active model/tool execution are process-local. Restarting a service
does not resume them automatically.

No service can survive destruction of its SageMaker application or compute instance. If the XDG state directory is on persistent storage, `start` recovers the stale PID record and OpenCode can reopen its session data after the application returns.
