# Terminal chat

`opencode-bedrock chat` opens a small, persistent Claude chat in the terminal. It uses the same
loopback-only OpenCode service and private Session database as the task workflow. It does not add a
second server, transcript store, Python package, or terminal UI dependency.

## Start or resume

For a registered project:

```bash
opencode-bedrock chat --project my-project
```

For an unregistered repository:

```bash
opencode-bedrock chat --workspace /absolute/path/to/repository
```

The command reuses a running workspace service. If none is running, it starts one when
`AWS_REGION` and `BEDROCK_INFERENCE_PROFILE` (or the corresponding command options) are present.
The default launch resumes the last chat selected by this terminal client.

Use a fresh or explicit Session when needed:

```bash
opencode-bedrock chat --project my-project --new
opencode-bedrock chat --project my-project --session ses_...
opencode-bedrock chat --project my-project --no-stream
```

`--no-stream` waits for the durable response and prints it once. Redirected standard output also
uses final-response mode. Standard input must be an interactive terminal.

## Commands

The prompt supports:

```text
/new                 create and switch to a new chat
/sessions            list recent chats for this workspace
/use SESSION_ID      switch by a full ID or unique displayed prefix
/history             show the newest bounded transcript page
/history more        show the next older page
/help                show the command list
/quit                exit without deleting the Session
```

Start a message with `//` to send one literal leading slash. Empty lines are ignored.

While Claude is responding, `Ctrl-C` requests interruption and waits up to ten seconds. A second
`Ctrl-C` exits with status 130. `Ctrl-C` while idle also exits with status 130.

## Sessions, history, and context

Every chat has an immutable `terminal-chat` purpose, a client-generated and server-confirmed
Session ID, and an exact Bedrock model selection. A kernel lock prevents two local terminal
clients from attaching to the same chat. Build, plan, child, other-workspace, and non-Bedrock
Sessions cannot be adopted as chats. The server rejects agent, model, revert, move, workspace-warp,
sync-steal, and invalid replay mutations for `terminal-chat` Sessions, so the tool-free Bedrock
selection and implicit-local Location cannot change between wrapper validation and execution.

The first completed exchange causes a second, tool-free Bedrock request that creates a concise
title. A normal first exchange therefore makes one visible response call and one title call.
If title generation definitely fails, the server stores a timestamp title; if its outcome is
unknown after a power loss, the next attachment safely retries while the default title remains.

The full public transcript stays in OpenCode's private Session database. The terminal displays a
bounded recent window and pages older messages without creating a second transcript. It retains at
most 8 MiB of validated message responses while assembling that window, independently of the
smaller rendered-text limit. Compaction changes only the active model context:

- policy ceiling: 200,000 estimated tokens;
- response reserve: 20,000 tokens;
- automatic compaction target: approximately 180,000 tokens;
- recent context: up to ten complete turns and approximately 40,000 tokens;
- summary output: at most 4,096 tokens.

Compaction makes one additional tool-free call through the same Bedrock profile. After a lost
admission HTTP response, the terminal may exact-retry the same message ID and body once; it never
creates a replacement user row or directly retries a provider call. Durable provider-attempt start
and end events make an unfinished power-loss outcome visible and prevent automatic replay when a
provider call may have started. Overflow compaction durably records its exact attempted input, so a
power loss after the completed checkpoint resumes only that proven continuation. Ordinary or
manual compaction never grants replay permission.

Before a creation or prompt-admission request, the wrapper writes a bounded mode-`0600` recovery
journal to its private service-state directory. Prompt journals are isolated by hashed Session ID
and may temporarily contain one pending message so the exact ID and body survive `Ctrl-C`, a lost
response, or a laptop power loss. A journal remains until the Session reports that its input has a
terminal settlement. File and containing-directory synchronization makes journal creation,
replacement, and removal durable across sudden power loss. On attachment, the wrapper asks the
service for the exact journaled message's bounded recovery status and whether any other unresolved
input exists. It wakes only that matching, never-attempted input; unrelated queued work and any
input whose provider call may have started are never replayed.

## Isolation and dependencies

The chat agent receives no tool definitions, project instructions, skills, references, shell,
filesystem, web, MCP, or subagent access. Visible answers, compaction summaries, and titles use
only the configured Amazon Bedrock model. Loopback API calls ignore ambient HTTP proxies.

The deployed runtime requirements remain Python 3.10+, the bundled OpenCode executable, existing
`bwrap`, AWS role credentials, and ordinary system libraries. No `sudo`, pip install, npm install,
Git access, registry access, or public internet access is required after installing the offline
artifact.

The native build still uses the repository's pinned Bun dependency graph on the networked build
machine. The chat implementation adds no Python, npm, or Bun dependency.

## Troubleshooting

- If startup asks for a Region or profile, set `AWS_REGION` and
  `BEDROCK_INFERENCE_PROFILE`, then retry.
- Run `opencode-bedrock doctor` if `bwrap`, the OpenCode binary, or the local sandbox is missing.
- Run `opencode-bedrock status --project my-project` to inspect the workspace service.
- A “currently generating” message means the Session must settle or be interrupted before it can
  be selected.
- An “already open locally” message means another terminal process holds that Session's kernel
  lock. Close that client; a dead process releases the lock automatically.
- A warning about an unfinished provider attempt means the service lost power after a Bedrock call
  may have started. The call is not replayed automatically, and that chat rejects new messages so
  an uncertain response cannot be absorbed into a later turn. The durable transcript is preserved;
  use `/new` to continue safely.
- Warnings about unfinished compaction or attempted-but-unsettled input are handled the same way:
  the affected chat remains readable but refuses new input because replay would be unsafe.

The Session database and chat selection state live under the wrapper's private XDG state
directory. Service state directories use mode `0700`; credential-bearing records, chat selection,
and lock files use mode `0600`.
