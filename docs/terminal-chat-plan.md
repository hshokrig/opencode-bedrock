# Terminal chatbot implementation plan

## Status

Planning document. No chatbot implementation exists yet. Revised after three independent review cycles.

## Purpose

Add a small terminal chatbot to the Bedrock specialization without creating a second chat backend or adding runtime dependencies. The command should feel like a focused chat surface: it opens in one terminal, streams Claude responses, keeps durable session history, resumes prior chats, and creates independently named chat sessions.

The primary user is a SageMaker user working without `sudo` or public internet access. Amazon Bedrock, AWS role credentials, Python, and the AWS tooling are available. The installed offline artifact remains the deployment unit.

## Goals

- Add `opencode-bedrock chat --project NAME` and `--workspace PATH`.
- Start or reuse the selected workspace service and open a simple terminal prompt.
- Store every user and assistant message in OpenCode's existing session database.
- Resume the last chat selected through this terminal surface unless the user requests a new or specific session.
- Generate a stable session ID in the client, submit it during V2 creation, require the server to confirm the same ID, and display that confirmed ID.
- Let Claude assign a concise title after the first user message.
- Stream assistant text while it is generated.
- Preserve the full transcript for display and audit.
- Compact the active model context near a 200,000-token policy ceiling.
- Keep the ten most recent conversational turns verbatim after compaction.
- Build on durable V2 Session admission, projection, and replay rather than adding new legacy SessionPrompt usage.
- Use only approved Claude inference profiles through Amazon Bedrock for visible replies, summaries, and title generation, with no provider or model fallback.
- Add no pip, npm, terminal UI, or database dependency.
- Add no deployed network destination beyond authenticated loopback traffic and the existing AWS credential and Bedrock paths. Public-internet denial remains infrastructure-enforced.

## Delivery contract

Keep the work independently releasable:

| Milestone | Required behavior |
| --- | --- |
| Internal skeleton | Create, select, and resume a V2 chat session; admit queued prompts durably; print a completed response; preserve and page the transcript; enforce a tool-free and project-instruction-free chat agent. Placeholder titles are allowed. This milestone is not published. |
| Production Release 1 | Add the exact 200,000-token and ten-turn compaction policy, conservative crash classification, best-effort streaming with durable reconciliation, interruption, one Claude-generated title after the first completed exchange, offline packaging, and all release gates. |

No release artifact is published until all implementation phases and the Release 1 acceptance checklist pass.

## Non-goals

- A browser or desktop interface.
- Markdown widgets, mouse support, panels, themes, or a plugin system.
- A second message store alongside OpenCode.
- Cross-workspace chat sessions.
- Multi-user access to one terminal session.
- Attachments in the first version.
- Multiline editing, persistent arrow-key input history, autocomplete, Markdown rendering, spinners, and animation.
- Editing, shell, filesystem, web, Model Context Protocol (MCP), or subagent tools in chatbot mode.
- Replacing the existing `attach`, `task`, or approval workflows.
- Deleting old transcript rows during context compaction.

## Product behavior

### Launch

```bash
opencode-bedrock chat --project my-project
```

The command resolves the workspace exactly as the other wrapper commands do. If its service is already running, the command connects to it. If it is stopped and the required region and inference-profile settings are available, the command starts it. Otherwise it reports the missing setting and exits without creating partial state.

By default, the command resumes the last session selected by this terminal surface. Store that session ID in the existing private per-service state and validate it against the server on every launch. If it is absent or invalid, create a new chat rather than silently adopting an older conversation. V2 currently orders sessions by creation time rather than update time, so the plan must not claim last-updated resume semantics without a generic list-order extension.

Explicit launch modes:

```bash
opencode-bedrock chat --project my-project --new
opencode-bedrock chat --project my-project --session SESSION_ID
opencode-bedrock chat --project my-project --no-stream
```

On exit, the background service and session remain available.

### Terminal layout

Use a line-oriented interface built with the Python standard library:

```text
Chat: Investigating API latency
Session: ses_...
Model: Claude Opus through Amazon Bedrock

you> Why does this request take so long?

claude> The delay appears to come from...

you>
```

Render basic ANSI color only when standard output is a terminal and `NO_COLOR` is unset. Plain text must remain fully usable when output is redirected.

Treat user text, assistant text, titles, history, and server error details as untrusted terminal input. Pass all of them through one rendering-boundary sanitizer that preserves normal newline and tab characters while escaping or removing escape sequences, C0/C1 controls, carriage return, backspace, operating-system commands such as OSC 52, and bidirectional formatting controls. Apply trusted ANSI styling only after sanitization.

### Commands

Keep the command language deliberately small:

```text
/new                 create and switch to a new chat
/sessions            list recent chatbot sessions
/use SESSION_ID      switch to an existing chatbot session
/history             show the latest bounded history page
/history more        show the next older history page
/help                show these commands
/quit                leave without deleting the chat
```

Local slash commands are never stored or sent to Claude. `//text` sends the literal message `/text`; unknown slash commands are rejected locally with a short hint.

Interactive mode requires terminal standard input. Redirected standard output uses plain final-response mode automatically; `--no-stream` provides the same stable output for screen readers and copying. State changes must always be conveyed in text rather than color alone.

An empty line does not submit a message. End-of-file exits successfully. During generation, the first `Ctrl-C` sends one interrupt request and waits for a bounded acknowledgement; a second exits with status 130 without deleting state. While idle, `Ctrl-C` exits with status 130. These semantics must be covered by subprocess-level signal tests.

The first version accepts one submitted line at a time. Multiline editing, attachments, message deletion, session deletion, renaming, and transcript export can be considered separately.

Precise context usage may later be exposed through `/info`; it is not part of the minimum command surface.

## Architecture

```text
opencode-bedrock chat
        |
        | authenticated loopback HTTP and server-sent events
        v
existing per-workspace OpenCode service
        |
        +-- durable V2 Session admission and projection
        +-- replayable per-session durable event stream
        +-- durable history and active context
        +-- dedicated tool-free chat agent
        `-- Amazon Bedrock inference profile
```

OpenCode remains the source of truth for sessions, messages, titles, token usage, and summaries. The Python client durably admits one new prompt with a session ID; it does not rebuild and resend historical messages itself. Use the V2 `/api/session` surface exclusively for chatbot work. Do not bridge chat orchestration through legacy `SessionPrompt.loop(...)`.

The client must continue using the generated Basic authentication password and `127.0.0.1`. It must ignore ambient HTTP proxy settings for loopback calls.

V2 model resolution does not currently provide an Amazon Bedrock route. Before building the terminal loop, add a generic V2 Bedrock model route that supports the SageMaker/default AWS credential chain, region and endpoint configuration, credential refresh, Converse streaming, and the configured inference-profile ID or ARN. Preserve one explicit `llm.stream(request)` call per provider turn.

The configured `opus` alias must publish reviewed model metadata rather than falling back to zero limits:

```text
context limit       200,000
input limit         200,000
output maximum       20,000
provider/model       approved Claude inference profile through amazon-bedrock
```

Validate the actual inference profile during opt-in AWS verification. The application has no provider or model fallback if that profile is not Claude or cannot be invoked.

## Chat agent

Add a primary `chat` agent to the generated OpenCode configuration:

- model: the exact configured Amazon Bedrock inference profile, with Opus as the intended default;
- short conversational system prompt;
- no shell or command execution;
- no read, edit, search, web, question, task, or external-directory tools;
- no subagents;
- no approval workflow;
- no project instructions injected into chat context unless explicitly enabled in a later design.

The current shared service injects explicitly configured project instructions into every provider turn, so agent policy alone cannot yet satisfy the last requirement. Add a generic per-agent configuration switch that excludes configured and discovered workspace instructions, and assert the exact provider system prompt in an integration test. A separate chat service is the fallback only if a generic per-agent exclusion cannot be implemented safely.

Define the final chat permission set as deny-all, including `external_directory`, and send no tool definitions in the provider request. Test this through native OpenCode with a deterministic local provider, not only by inspecting the generated Python dictionary.

Chatbot sessions must be distinguishable immutably from build and plan sessions. Current agent selection is mutable, so it cannot establish provenance. Add a generic immutable Session purpose or creation-surface field, set it to `terminal-chat`, and pair it with root-session and exact Location/workspace checks. Existing sessions have no chat purpose and can never be adopted merely because their current agent was switched to `chat`. Project the field for indexed listing; account for its schema migration and client regeneration.

## Session lifecycle

### Creation

Generate a stable V2 session ID in the terminal client, then create the session with that ID, immutable `terminal-chat` purpose, the `chat` agent, exact approved Claude-through-Bedrock model/profile reference, and exact workspace Location. Reject any non-Claude or non-Bedrock model override. Require the server response to confirm the same ID; the confirmed ID is shown to the user and reused for exact creation reconciliation.

Do not reuse the task wrapper's current behavior of assigning the first 80 prompt characters as a title.

### Automatic title

V2 does not currently expose durable title update behavior. Add a generic compare-and-set title update API and a server-owned `ensureTitle(sessionID, firstTurnID)` operation triggered after the first exchange settles. It makes one tool-free title request through the Session's configured Bedrock provider/model using that completed exchange, then replaces the title only if it is still the default. The stable idempotency key is the Session ID plus first-turn ID.

The title is derived by Claude from the first completed exchange and becomes visible immediately after the first answer. It is a second hidden Bedrock call, not a field extracted from the visible answer.

Requirements:

- normally one title-generation request per untitled root session;
- title length at most 100 characters;
- trim reasoning tags, newlines, and surrounding whitespace;
- refresh the terminal header after the title update is observed;
- do not block or alter the visible response;
- on a definite request failure, compare-and-set a `Chat YYYY-MM-DD HH:MM` fallback;
- if the process dies while the title call outcome is unknown, leave the default title and retry on the next attachment;
- accept that a crash may cause one duplicate title call, while compare-and-set prevents conflicting persisted titles;
- never send title requests to a non-Bedrock provider.

The extra title request and its token cost must be documented. A normal first exchange uses one visible Bedrock response call plus one title call. Each later compaction adds one summary call before the next visible response. There are no other model-service calls in chatbot mode.

### Listing and switching

List eligible root sessions for the exact workspace Location with immutable purpose `terminal-chat` and expected current `chat` agent. Until V2 gains an updated-time sort, display the API's creation-time ordering and use the private last-selected pointer for default resume. Show the full session ID, title, and available timestamp. `/use` accepts a full ID or an unambiguous displayed prefix and rejects ambiguity.

Switching is allowed only while the current session is idle. If a response is running, the user must interrupt it or wait.

Hold a nonblocking kernel `flock` on an open per-session lock descriptor for the lifetime of a terminal attachment. Store the regular lock file in the private service-state directory, reject symlink or non-regular lock paths, and rely on kernel release after process death rather than deleting stale PID files. Refuse a second local terminal client for the same session with recovery guidance. Revalidate the session agent, root status, Location, model/provider, lock ownership, and execution status before display, adoption, or prompt admission.

## History model

### Durable transcript

Retain all projected public user and assistant messages in OpenCode's session database. Compaction is additive and must not delete or overwrite the original transcript. `/history` paginates the projected-message API in chronological display order; the durable event-history endpoint is for replay and audit, not transcript rendering.

On resume, render only the latest ten complete turns, subject to a defined byte and line display cap with an omission marker. `/history` shows one bounded page and `/history more` streams the next older page without accumulating the whole conversation in memory. History remains interruptible. `/sessions` shows at most 20 records per page, one record per line. Exclude compaction, system, synthetic, reasoning-only, and internal records from the normal transcript; render durable assistant failures as explicit sanitized error markers.

Do not store a second transcript in `tasks.json` or another wrapper JSON file. The only chat-specific wrapper state is the validated last-selected session pointer and the kernel-held attachment lock.

### Active model context

Before the first compaction, the next model request includes the complete active conversation.

After compaction, the request contains:

1. the chat agent's system instructions;
2. the cumulative anchored summary of older conversation;
3. the ten most recent complete conversational turns verbatim;
4. the current user message.

A completed turn is one promoted input batch plus the assistant steps through its durable terminal settlement. Add a durable turn/correlation identity because current assistant events do not identify the promoted user input batch and generic V2 can combine steering inputs. Terminal chat always admits with `delivery: "queue"` and a client-generated stable message ID, but generic compaction must not assume every caller is single-input. The currently admitted user input is not counted as a completed turn. Interrupted or failed turns remain visible in durable history but do not displace a newer completed turn from the retention budget. Tool events should not exist in chatbot sessions. Preserve complete exchange groups instead of splitting a serialized message.

Interpret the requested “last 10 messages” as ten complete user/assistant exchanges, potentially twenty visible messages. This avoids retaining one side of a conversation pair. Confirm this interpretation before implementation if literal ten individual messages are required.

The durable transcript and active model context are intentionally different after compaction.

## Token accounting and compaction

### Policy

Treat 200,000 tokens as the maximum chatbot context policy even when the selected Claude model supports a larger context window.

User-facing wording: the policy ceiling is 200,000 total context tokens; compaction starts near 180,000 so Claude retains approximately 20,000 tokens of response capacity. The system does not intentionally send a request above the 200,000-token policy ceiling.

Initial operating values:

```text
policy ceiling                 200,000 tokens
reserved generation space      20,000 tokens
preemptive compaction target   180,000 tokens
recent turns to retain                 10
recent-context token ceiling    40,000 tokens
summary output ceiling           4,096 tokens
```

The final values must be server-side configuration in one location and exposed through a read-only session/context information contract for `/info`. Do not duplicate context estimates or threshold constants in Python.

### Trigger

Perform the threshold decision inside the serialized V2 runner, where the exact system text, projected messages, tools, output allowance, and model limits are available. Add a generic configurable `context_ceiling`; the effective ceiling is the smaller of the model's real limit and the configured 200,000-token policy. Reserve 20,000 tokens for generation, making the normal compaction boundary 180,000 tokens.

Finish the generic V2 explicit compaction and wait/idle contracts, which currently return service-unavailable responses. The V2 runner owns the single overflow-recovery attempt. The terminal client must never resubmit an admitted prompt, because that risks duplicate user messages or unintended steering. If recovery still fails, project the provider error against the same durable message and render it.

Apply only a transport-level byte cap before durable admission. Perform model-context validation inside the serialized runner after admission and before any Bedrock call. If one input cannot fit an otherwise empty post-compaction request, publish a correlated durable terminal rejection, keep the admitted input for exact retry reconciliation, and do not invoke Bedrock. Do not truncate user text silently.

### Summary

Use the existing anchored V2 compaction summary format. Each later compaction updates the previous summary using the newly aged-out turns. The summary should preserve:

- user preferences and constraints;
- decisions and their reasons;
- names and definitions;
- unresolved questions;
- important exact identifiers;
- the current conversational objective.

The summarizer uses the same Bedrock inference profile by default. A future lower-cost Bedrock profile can be allowed through an explicit agent-model override, but no non-Bedrock fallback is permitted.

### Retention invariant

V2 currently retains a token suffix and may split a serialized message. Add a generic `compaction.keep.turns` option and make selection aware of completed user-to-assistant turn boundaries.

After successful compaction:

- durable history still returns every original message;
- active context contains exactly one current summary;
- the most recent ten complete turns are present verbatim when they fit the 40,000-token recent-context target;
- the currently admitted user message is represented separately and is never summarized out of the turn being executed;
- if ten turns exceed the target, retain as many newest complete turns as fit;
- the 40,000-token recent-context value is soft for one atomic newest completed turn: retain that turn when it exceeds 40,000 tokens but still fits the effective input budget;
- reject or durably fail the pending input when the newest completed turn plus required system/summary context cannot fit the effective input budget.

Manual and automatic compaction share one Session-owned serialized operation. Concurrent compaction requests join, coalesce, or return a typed busy result; they never start two summary calls for the same context epoch. Add a durable compaction failure outcome so a started compaction cannot remain indefinitely ambiguous. A prompt admitted during compaction remains pending and is not summarized into the context being compacted.

## Crash and recovery semantics

Durable admission does not by itself make an in-flight provider call safely replayable after service or laptop power loss. During the contract spike, define and test recovery at these boundaries:

1. admitted but not promoted;
2. promoted but provider execution not started;
3. provider execution started with no terminal durable outcome;
4. provider output completed but final projection not durably settled.

Never resubmit automatically from the terminal. The server may explicitly resume an input only when durable evidence proves provider execution never started. Once a provider call may have started, surface the turn as interrupted or outcome-unknown unless a separate generic recovery design can prove replay is safe. Reconnection classifies state from durable admission, promotion, step, text, and terminal events and never creates a duplicate user row or automatic second Bedrock call.

Current Step events begin only after provider output is observed, so they cannot prove whether a silent provider call started. Publish a durable provider-attempt boundary immediately before the one explicit `llm.stream(request)` call, linked to the turn and promoted input IDs. Recovery may resume only a turn with no durable attempt boundary. A started attempt without a terminal settlement becomes outcome-unknown and is never called automatically again. Add deterministic test-only failpoints at admission, promotion, attempt start, provider completion, and terminal settlement.

## API work

Extend the Python `Client` without changing authentication or proxy behavior. Required operations:

- list sessions for the workspace;
- create a session with immutable `terminal-chat` purpose, the `chat` agent, Bedrock model, exact Location, and stable client-generated ID;
- get one session;
- compare-and-set the default title;
- request server-owned title generation for the first settled turn;
- list durable messages with pagination;
- submit a chat prompt with `delivery: "queue"` and a stable message ID;
- wait for or observe session status;
- subscribe to durable per-session V2 events from an aggregate sequence;
- optionally subscribe to live best-effort text deltas;
- interrupt active generation;
- optionally retrieve server-derived context usage and policy limits for a later `/info` enhancement.

Treat every session, message, turn, and event identifier as an opaque untrusted value. Percent-encode it as one HTTP path segment, reject delimiters and control characters that violate the server ID schema, and never build a route by interpolation without segment encoding.

Use V2 `/api/session` endpoints consistently. Do not create a second HTTP server and do not use legacy `/session` prompt orchestration for chat. The contract spike is expected to demonstrate missing generic contracts rather than choose between API generations.

Required generic V2 core and API additions:

- implement explicit compaction;
- add passive process-local `awaitIdle(sessionID)` behavior without forcing or resuming execution;
- add generic compare-and-set title update behavior;
- add a server-side context ceiling and read-only context-usage information;
- add complete-turn compaction retention;
- add durable turn correlation between promoted inputs and terminal assistant work;
- add a durable provider-attempt boundary before `llm.stream(request)`;
- add immutable Session purpose/creation-surface projection and filtering;
- add per-agent workspace-instruction exclusion;
- add post-admission, pre-provider single-input context rejection;
- add explicit crash-state classification for unfinished inputs;
- add a durable compaction failure outcome.

Durable correctness and live presentation are separate:

- `/api/session/:sessionID/event?after=SEQ` replays durable boundaries and terminal outcomes;
- the global live V2 `/api/event` stream provides non-durable text deltas for best-effort presentation and must be filtered by exact session, turn, assistant, text, and Location identities;
- live deltas are never treated as replayable or as proof of completion;
- after disconnect, resume durable events, discard assumptions about missing deltas, and reconcile rendered content against durable `Text.Ended` records and the final projected assistant message.

Bound UTF-8 and SSE buffers, support CRLF, multiline `data`, comments, and heartbeats, impose response and idle timeouts, and reconnect once. Correlate by selected session, admitted message, assistant message, turn, and text-block IDs. Advance durable cursors only after a complete validated event.

Extract one pure Location-scoped request-preparation/context-information service shared by the runner and `/info`. It reports estimated input tokens, model limit, configured ceiling, effective ceiling, reserved output, compaction target, and estimator version. It must not mutate the context epoch. Describe the count as an estimate rather than exact tokenizer output.

Any public Protocol or Server `HttpApi` change requires `bun run generate` from `packages/client`; generated files must not be edited manually. New durable events must be added to the durable event definitions and manifest. Backward-compatible event fields need replay tests. New projected SQL fields require a migration and registration in the generated migration list; title changes can reuse existing title and update-time columns unless outcome state is projected.

### Client resource bounds

Use named constants, validated during the contract spike:

```text
prompt transport cap             1 MiB UTF-8
SSE event/buffer cap             1 MiB
HTTP error body shown            8 KiB
history API page                    50 messages
history terminal page           200 lines or 64 KiB
sessions page                       20 sessions
title                            100 characters and 512 UTF-8 bytes
SSE header timeout                 15 seconds
SSE idle timeout                  120 seconds
durable reconnect attempts          1
```

An endless heartbeat stream does not reset the model-progress timeout. Abrupt EOF, an unterminated event, an oversized length, or an invalid UTF-8 sequence must fail boundedly and fall back to durable reconciliation.

## Contract and migration matrix

Phase 1 must turn this provisional matrix into a checked-in exact contract before implementation:

| Concern | Durable contract | Projection/storage | Compatibility work |
| --- | --- | --- | --- |
| Session purpose | Immutable purpose on Session creation and info | New indexed Session column | SQL migration, old rows unset, Protocol/client regeneration |
| Turn identity | Turn started/promoted identity links input batch to assistant work | Event-derived unless query performance requires projection | Backward-compatible new durable events/optional fields and replay tests |
| Provider attempt | Durable attempt-started event immediately before provider call | Event-derived recovery state or explicit input state if proven necessary | Durable manifest and crash failpoint tests |
| Terminal settlement | Durable completed, failed, rejected, interrupted, or outcome-unknown result linked to turn | Projected message/session input status | Event schema, projector, and possible migration |
| Title | Compare-and-set title plus title-changed event | Reuse existing title/update-time columns | API/event/client regeneration, no SQL migration expected |
| Compaction | Started, ended, and failed outcomes with reason and epoch | Existing compaction projection plus failure state if needed | Durable event manifest and replay tests |
| Context policy | Server configuration and shared request estimate | No persistence required | Config schema and optional read-only API regeneration |
| Instruction sources | Per-agent source-selection policy | Configuration only | Agent/config schema and context-epoch tests |

Do not add required fields to historical event schemas without an explicit version or backward-compatible optional-field strategy.

## Proposed file changes

Expected specialization changes:

- `opencode_bedrock/chat.py`: terminal state machine, rendering, commands, live/durable stream consumption, and reconciliation;
- `opencode_bedrock/api.py`: session, message, event, interruption, and compaction operations;
- `opencode_bedrock/cli.py`: `chat` parser and dispatch;
- `opencode_bedrock/policy.py`: tool-free chat agent and centralized compaction configuration;
- private service-state support for the last-selected session and per-session terminal lock;
- `tests/bedrock/test_chat.py`: terminal behavior with a fake OpenCode server;
- `tests/bedrock/test_api.py`: API shapes, pagination, streaming, and reconnect behavior;
- `tests/bedrock/test_policy.py`: Bedrock-only chat agent and compaction values;
- `README.md` and one task-oriented chat usage document after implementation.

Expected generic OpenCode changes:

- V2 Amazon Bedrock route with default credential-chain refresh and exact inference-profile support;
- immutable Session purpose projection, migration, and filters;
- V2 compare-and-set title HTTP contract using existing title projection fields;
- V2 explicit compaction, passive idle wait, and crash-state classification;
- V2 compaction context ceiling and complete-turn retention;
- V2 durable turn identity, provider-attempt boundary, terminal outcomes, and compaction failure outcome;
- per-agent instruction-source exclusion;
- shared non-mutating request preparation and context-usage projection needed by `/info`;
- Protocol/client regeneration for changed public APIs.

Keep every core change generic enough to propose upstream and add its tests beside the affected module. Do not route around missing V2 behavior through legacy SessionPrompt code.

## Implementation ownership

Freeze interfaces before downstream work:

| Area | Ownership |
| --- | --- |
| V2 core, store, runner, events, projection, migrations | Durable behavior, turn/attempt identity, recovery, context policy, compaction, title operation |
| Protocol and Server `HttpApi` | Public schemas, endpoint behavior, error types, replay contracts |
| Generated clients | One regeneration after the API batch freezes; never hand-edited |
| Python API client | Authentication, bounded HTTP/SSE parsing, correlation, pagination |
| Terminal state machine | Commands, rendering, locks, signals, session selection |
| Packaging and release | Offline artifact, dependency checks, upgrade test, AWS smoke |

Do not start Python endpoint integration until the core and public API contract batch is frozen and generated clients are clean.

## Implementation phases

### Phase 0: baseline and hard contract gate

1. Build a deterministic local provider and fault-injection harness.
2. Exercise current V2 admission, queue delivery, projection, history, events, interruption, Bedrock routing, and compaction behavior.
3. Add deterministic failpoints around admission, promotion, provider-attempt start, provider completion, and terminal settlement.
4. Check in the exact contract/migration matrix: endpoint or event, schema, durability, owner, migration impact, and consuming phase.
5. Write a focused failing test for every core addition. Do not add a contract merely because it appears in this plan.
6. Stop if queue settlement, turn correlation, or safe crash classification would require a legacy bridge or unsafe automatic provider replay.

Required gate:

```bash
cd packages/core
bun test test/session-runner.test.ts

cd ../opencode
bun test test/server/httpapi-session.test.ts
```

Exit criterion: all reusable behavior is proven, every missing user-required contract has one failing test, and the implementation can remain V2-only.

### Phase 1: V2 durability, context, and naming

1. Add and test the V2 Amazon Bedrock route with the SageMaker/default credential chain, refresh, region, optional endpoint, exact inference profile, and explicit model limits.
2. Add immutable Session purpose, turn identity, provider-attempt boundary, terminal outcomes, and conservative recovery classification.
3. Add passive idle observation without forcing execution.
4. Add per-agent instruction-source selection and prove a zero-tool, chat-only provider request.
5. Add the effective 200,000-token ceiling, post-admission input rejection, complete-turn retention, and serialized compaction with failure outcome.
6. Add server-owned first-exchange title generation and compare-and-set title persistence.
7. Add migrations only where the frozen matrix requires them.
8. Test existing build/plan Sessions and task/approval behavior against a copied pre-change state directory.

Required gate:

```bash
cd packages/core
bun test test/session-runner.test.ts
bun typecheck

cd ../opencode
bun test test/server/httpapi-session.test.ts
bun typecheck
```

Exit criterion: all first-release durability, naming, tool isolation, and context guarantees pass without a terminal client.

### Phase 2: Protocol freeze and client generation

1. Implement the frozen Protocol and Server `HttpApi` batch.
2. Add durable definitions, event manifest updates, projectors, and registered migrations.
3. Regenerate clients once from the frozen API.
4. Review generated diffs and backward replay fixtures; do not edit generated files directly.

Required gate:

```bash
cd packages/client
bun run generate
bun typecheck

cd ../protocol
bun typecheck

cd ../server
bun typecheck

cd ../opencode
git diff --check
```

Exit criterion: public schemas are frozen, generated clients match them, and replay/migration compatibility passes.

### Phase 3: non-streaming terminal chatbot

1. Extend the Python client for V2 create, list, get, queued prompt, completion, projected messages, durable events, interruption, and title observation.
2. Implement the line-oriented terminal, immutable chat validation, last-selected pointer, kernel-held lock, sanitizer, and minimal commands.
3. Print the correlated completed response; do not depend on live deltas.
4. Run fake-server tests for client parsing and native OpenCode tests for actual provider requests and persistence.
5. Confirm the Claude-generated title appears immediately after the first completed answer.

Required gate:

```bash
python3 -m unittest discover -s tests/bedrock -t .
```

Exit criterion: the first usable chatbot meets naming, durable history, tool isolation, ten-exchange retention, and 200k compaction requirements without new runtime dependencies.

### Phase 4: live presentation and interruption

1. Add best-effort live delta rendering.
2. Reconcile against durable text boundaries and final projection.
3. Add bounded reconnect, `Ctrl-C`, `--no-stream`, and non-TTY behavior.
4. Run chunk-boundary, control-sequence, disconnect, signal, and correlation tests.

Exit criterion: presentation failures never change durable settlement or cause prompt resubmission.

### Phase 5: offline packaging and upgrade compatibility

1. Include the new Python module in wheel, source distribution, and offline archive checks.
2. Run package tamper, installer, no-network help, and installed native lifecycle tests.
3. Verify a copied state directory from the current artifact: old Sessions remain readable, transcripts survive, migrations are registered, and existing task/approval workflows are unchanged.
4. Document that migrations are one-way and state rollback limitations before accepting a schema migration.
5. Build twice from the same reviewed binary and compare the release archives byte-for-byte.

Exit criterion: the mandatory offline release gate passes and produces a checksummed reproducible artifact.

### Phase 6: optional AWS environment validation

Behind `RUN_AWS_SMOKE=1`, create an isolated chat, receive a response, verify its Claude-generated title, resume it in a second client process, and exercise streaming. Record the check as passed or not run. Unavailable AWS access does not block an otherwise verified offline release; an actual AWS test failure blocks promotion to that environment.

## Test matrix

### Python client and terminal tests

Use a bounded fake HTTP/SSE server only for wrapper behavior:

- start a service automatically when configuration is present;
- refuse partial startup when required configuration is absent;
- create and resume a chatbot session;
- validate the private last-selected pointer on launch;
- never select build, plan, child, or unrelated Location sessions;
- refuse a second terminal attachment and recover immediately after the lock-holding process dies;
- list and switch sessions only while idle;
- paginate history incrementally without unbounded buffering;
- parse UTF-8-split, CRLF, multiline, comment, and heartbeat SSE input;
- enforce event-size and timeout limits;
- treat live deltas as best-effort, reconnect durable events from the last aggregate sequence, and reconcile final text exactly once;
- sanitize CSI, OSC 52, carriage return, backspace, C0/C1, and bidi controls in every untrusted field;
- preserve Basic authentication and loopback proxy bypass;
- support plain output when no terminal is attached;
- implement deterministic end-of-file and `Ctrl-C` behavior;
- redact and bound HTTP error details.

### Native OpenCode integration tests

Use native OpenCode with a deterministic local provider to verify server and provider-request invariants:

- durable V2 prompt admission and exact retry/conflict behavior;
- tool-free provider requests with zero tool definitions;
- absence of workspace, skill, MCP, and project instructions from the chat system prompt;
- title success, failure fallback, process death, duplicate retry, and compare-and-set races;
- projected transcript versus durable event history versus active context;
- 9, 10, and 11 completed turns at compaction;
- an incomplete, interrupted, or errored current turn;
- one completed turn larger than the 40,000-token retention budget;
- repeated compaction with one cumulative current summary;
- service restart after compaction;
- every original public message remains projected after compaction;
- post-admission, pre-provider durable rejection of one oversized input without silent truncation;
- one server-owned overflow-recovery attempt with no terminal resubmission;
- process kill after admission, promotion, provider start, provider completion, and before terminal settlement;
- queue delivery prevents a concurrent prompt from steering the active provider turn;
- automatic and explicit compaction serialize and never duplicate summary calls for one epoch;
- interrupt acknowledgement and later prompt admission;
- unique secret prompts, summaries, credentials, inference-profile identifiers, and raw provider payloads do not appear in service logs or client errors.

### Packaging and dependency tests

- package the module and run it from an installed no-network offline artifact;
- assert `pyproject.toml` still has no runtime dependencies;
- assert Bun dependency manifests and lockfile are unchanged by the chatbot specialization;
- scan new Python imports against the standard library;
- retain only Python, bundled OpenCode, existing `bwrap`, and ordinary operating-system runtime requirements.
- assert `chat.py` is present and `opencode-bedrock chat --help` works with network disabled;
- install the checksummed artifact on SageMaker without running pip, `bun install`, Git, or a registry request.

Building a modified native OpenCode binary still uses the repository's existing Bun/npm dependency graph unless a reviewed prebuilt binary or local package cache is supplied. This is a build-time property, not a deployed runtime dependency.

### Opt-in AWS tests

Behind `RUN_AWS_SMOKE=1`:

- create an isolated chat session through the SageMaker execution role;
- receive streamed text from the configured Claude inference profile;
- verify that the generated title is nonempty;
- resume the same session in a second terminal-client process;
- verify Converse and streaming remain limited to the configured Bedrock provider.

Do not force a 200,000-token live test. Compaction thresholds belong in deterministic fake-provider tests.

### Release 1 acceptance checklist

- Phases 1 through 4 and all default test layers pass.
- Visible replies, compaction summaries, and titles select only an approved Claude inference profile through Amazon Bedrock.
- New sessions receive a client-generated, server-confirmed ID and an automatic Claude title after the first completed exchange.
- Durable transcript, ten-turn retention, 200,000-token ceiling, interruption, and conservative crash behavior pass native integration tests.
- Session and message identifiers are validated and encoded as opaque URL segments.
- Service and state directories are mode `0700`; transcript database/state, last-selected pointer, lock, and credential-bearing records are mode `0600` or stricter.
- Last-selected state is written atomically without following symlinks.
- Unique user messages, summaries, Claude titles, fallback titles, credentials, profile identifiers, and raw provider payloads do not appear in service logs or client errors.
- `pyproject.toml` has no new runtime dependency; package manifests and the Bun lockfile add no chatbot dependency.
- The installed artifact requires only Python 3.10+, bundled `opencode`, existing `bwrap`, CA/system libraries, and the installer utilities already documented.
- `opencode-bedrock chat --help` and a native fake-provider chat run with network disabled and without pip, `bun install`, Git, GitHub, or registry access.
- The opt-in AWS smoke passes in its isolated test-owned workspace and state root.
- The checksummed archive and standalone installer verification pass.

## Security and privacy checks

- Bind and connect only to `127.0.0.1`.
- Keep generated service credentials out of terminal output and logs.
- Do not place message text in process arguments, task indexes, or service records.
- Do not log complete prompts or summaries by default.
- Ignore ambient proxies for loopback requests.
- Keep full transcripts inside the service's private XDG state.
- Continue using the SageMaker execution role; do not add credential files.
- Ensure the chat agent exposes no tool path that could read the workspace or contact another service.
- Ensure session listing is scoped to the selected canonical workspace.
- Hold private session locks with mode `0600` inside the existing service-state boundary.
- Use a stateful stream sanitizer so escape, OSC, UTF-8, and bidirectional sequences split across chunks cannot bypass the rendering boundary.
- Sanitize terminal control data before applying any trusted ANSI presentation. Preserve ordinary Unicode and visibly escape dangerous control and bidirectional formatting characters.
- Redact and truncate server error bodies before showing or logging them.
- Redact at persistence and log origins, including runner failure causes and durable provider error/retry events, not only at terminal rendering.
- Treat public-internet denial as an infrastructure property. Application tests prove Bedrock-only provider selection, zero tools, and disabled external features; they do not claim to prove VPC routing policy.

## Failure behavior

- Service unavailable: print the project and recovery command.
- Authentication failure: stop; never retry with missing authentication.
- Title failure: compare-and-set the timestamp fallback and continue; an unknown crash outcome leaves the default title for a later retry.
- Stream interruption: discard live-delta assumptions, replay once from the last durable aggregate sequence, then poll boundedly for the correlated projected final message.
- Bedrock throttling or transient failure: rely on the existing bounded provider retry policy and show the terminal error.
- Context overflow: let the V2 runner compact and recover once; the terminal never resubmits the admitted prompt.
- Compaction failure: publish a durable failed outcome, preserve the original transcript, and durably reject the admitted turn before a known-oversized Bedrock call.
- Unfinished turn after service restart: resume only when durable evidence proves provider execution never began; otherwise render interrupted or outcome-unknown without an automatic Bedrock retry.
- Corrupt or unknown session ID: report it without creating a replacement implicitly.
- Terminal closure: do not delete or archive the session.

## Documentation changes after implementation

Keep this file as the implementation and design record. Add one concise task guide for users:

- how to launch chat;
- command reference;
- how sessions, titles, history, and compaction behave;
- where history is stored;
- how to resume a session;
- how to diagnose `bwrap`, Bedrock, or service failures.

Link that task guide from the README. Do not duplicate the full design or test matrix in the README.

## Decisions to confirm during the contract spike

1. What is the smallest generic V2 compare-and-set title contract using existing title projection fields?
2. Should completion use a finished `wait` endpoint, terminal durable events, or both?
3. How should the effective 200,000-token context ceiling appear in generic V2 configuration and public context information?
4. What exact event marks a completed conversational turn when the assistant is interrupted or fails?
5. Can the existing service start path be reused directly by `chat` without duplicating CLI argument defaults?
6. Can per-agent instruction exclusion cover configured instructions, discovered instructions, skills, and MCP text without affecting coding agents?
7. What backward-compatible turn identity links promoted input batches, assistant work, and terminal settlement?
8. Which crash states can be resumed without risking a duplicate provider call?

These are implementation checks, not reasons to create a parallel chat backend.

## Review history

### Cycle 1

Three independent reviewers audited API feasibility, context/session semantics, and security/testing.

Consolidated decisions:

- Use durable V2 Session APIs even though the legacy surface currently implements more of the desired behavior. This follows the repository's session architecture and avoids adding new legacy orchestration.
- Treat missing V2 title, explicit compaction, completion, exact turn retention, context ceiling, and per-agent instruction isolation as known generic core work.
- Keep token decisions and overflow recovery server-side; the terminal never reconstructs provider context or resubmits an admitted prompt.
- Use the replayable per-session event stream with aggregate sequence cursors.
- Initially classify chats by the dedicated `chat` agent and exact Location; later review replaced this with an immutable generic Session purpose because agent selection is mutable.
- Add terminal sanitization, a one-client session lock, bounded history and SSE processing, compare-and-set title fallback, and native OpenCode integration tests.

One reviewer recommended the legacy Session API because it is closer to feature-complete today. That recommendation was not adopted because it conflicts with the repository's durable V2 Session direction. Its useful implementation observations—explicit model limits, title races, history filtering, and generated-client boundaries—were incorporated where they also apply to V2.

### Cycle 2

Three new reviewers audited V2 core feasibility, minimal product scope, and adversarial reliability.

Consolidated decisions:

- Separate an internal non-streaming skeleton from streaming and naming work; Cycle 3 later made every user requirement mandatory for Release 1.
- Treat live text deltas as best-effort presentation and durable per-session events plus final projection as the correctness boundary.
- Add generic durable turn correlation because current assistant work cannot be mapped reliably to one promoted input batch.
- Use `delivery: "queue"` and stable message IDs; the terminal never steers or resubmits an admitted prompt.
- Replace pre-admission token rejection with a transport byte cap plus durable post-admission, pre-provider context rejection.
- Define conservative power-loss recovery and never replay a provider call whose start may have occurred.
- Serialize manual and automatic compaction and add a durable failure outcome.
- Simplify title generation to an idempotent compare-and-set flow that may repeat one title call after a crash rather than introducing a durable job scheduler.
- Use a kernel-held `flock`, stateful terminal sanitization, bounded display/SSE/error resources, and explicit non-TTY behavior.
- State runtime and build-time dependency guarantees separately and document the exact normal Bedrock call count.

### Cycle 3

Three final reviewers checked requirement traceability, implementation sequencing, and release/security acceptance.

Consolidated decisions:

- Make V2 Amazon Bedrock routing and explicit model limits the first implementation prerequisite.
- Define session IDs as client-generated and server-confirmed, and resume the last terminal-selected chat rather than claiming updated-time behavior.
- Require approved Claude-through-Bedrock profiles for visible replies, summaries, and titles, with no fallback.
- Treat the non-streaming loop as an unpublished internal skeleton; Release 1 requires compaction, recovery, streaming reconciliation, Claude naming, packaging, and all acceptance gates.
- Use the global live event stream for best-effort deltas and the per-session durable stream for truth and recovery.
- Add explicit Release 1 checks for filesystem modes, atomic state, identifier encoding, log privacy, exact local runtime prerequisites, network-disabled artifact execution, and safe test-owned AWS smoke cleanup.
- Sequence implementation vertically: Bedrock route and limits; isolated durable chat; context and recovery; streaming and naming; packaging and release.
