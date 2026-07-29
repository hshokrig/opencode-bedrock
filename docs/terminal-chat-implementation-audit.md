# Terminal chat implementation audit

This record covers five post-implementation review cycles of the Bedrock terminal chat added in
commit `958690804a`. Each cycle uses independent reviewers with different scopes. Reported
findings are reproduced against the exact source and classified before changes are made:

- **confirmed**: unintended behavior or a violated requirement; fixed in the cycle;
- **rejected**: not reproducible, already protected, or explicitly required by the design;
- **deferred**: real but outside the terminal-chat scope, with the reason recorded.

The target remains an offline SageMaker environment. Fixes must not add runtime dependencies,
public-network access, `sudo` requirements, non-loopback listeners, or live AWS calls to default
tests.

## Cycle 1

Three reviewers independently covered the durable V2 core, the Python terminal/security boundary,
and Protocol/migration/offline release integration. Duplicate reports were consolidated. Source
traces and focused reproductions confirmed every unique finding:

| Disposition | Finding |
| --- | --- |
| confirmed | A declined tool approval interrupted the runner before publishing the matching provider-attempt outcome, blocking every later drain. |
| confirmed | An admitted or promoted input with no provider-attempt boundary could be silently run or combined when a later prompt woke the Session. |
| confirmed | An interrupted compaction call could leave `Compaction.Started` unmatched and be called again after restart. |
| confirmed | An irreducibly oversized current input could trigger a paid summary call before being rejected. |
| confirmed | The loopback HTTP opener followed redirects, allowing a local redirect to escape the loopback-only network boundary with an authorization header. |
| confirmed | Prompt-admission uncertainty was memory-only and `Ctrl-C` during admission did not preserve the recovery identity. |
| confirmed | Stable client-generated Session creation IDs were not reconciled after a lost response. |
| confirmed | Definite HTTP responses and transport-unknown failures shared one exception, causing inappropriate exact retries and false uncertainty. |
| confirmed | Chat eligibility ignored explicit `workspaceID`, so directory equality alone could adopt a different Location identity. |
| confirmed | A failed atomic state write during Session switching leaked the new lock and left partially changed in-memory state. |
| confirmed | Normal JSON API responses were unbounded and malformed JSON escaped the wrapper's user-facing error boundary. |

No finding was rejected as intended design. The reviewers also confirmed that the purpose
migration and generated clients are consistent, the chat provider request has no tools or
workspace instructions, packaging adds no dependency, the runtime import set remains
standard-library-only, and AWS tests remain opt-in.

Fixes:

- provider and compaction attempts now close or block conservatively at every reviewed terminal
  boundary;
- irreducible input size is checked before a summary call;
- the local API refuses redirects, separates definite HTTP responses from transport uncertainty,
  bounds JSON responses at 8 MiB, and normalizes invalid JSON;
- creation and prompt admission use private atomic recovery journals and exact-ID retries;
- attachment resumes only durable, unsettled inputs proven never to have reached a provider call;
- Location validation includes the reserved workspace identity;
- Session switching commits state transactionally; and
- recovery scans process bounded pages without retaining full event or message bodies.

Verification:

- `packages/core`: 90 runner tests passed; package typecheck passed.
- Python terminal: 21 focused tests passed; compilation and `git diff --check` passed.
- The socket-backed Python API tests could not bind `127.0.0.1` under the active audit sandbox
  (`PermissionError: Operation not permitted`). Their new redirect, status, response-bound, JSON,
  proxy, and transport cases remain in the suite; this environment restriction is not classified
  as a product failure.

## Cycle 2

Three fresh reviewers attacked the revised crash state machine, context selection, and local
security/resource boundaries. The following unique findings were reproduced and confirmed:

| Disposition | Finding |
| --- | --- |
| confirmed | A successful empty provider stream ended the attempt without a terminal assistant failure, allowing the unanswered user input to flow into a later turn. |
| confirmed | A non-LLM publisher/protocol defect could escape without ending the provider attempt and permanently block the Session. |
| confirmed | An ended overflow attempt could still leave its input without terminal assistant settlement if the process died before the replacement attempt. |
| confirmed | The terminal did not surface an unmatched compaction, so it admitted inputs into a core drain that intentionally remained blocked. |
| confirmed | Pending-input tokens consumed the completed-turn retention budget and could summarize a newest complete turn that otherwise fit the real context budget. |
| confirmed | Invalid, oversized, truncated, or structurally wrong `2xx` responses were treated as definite rejection and could delete exact-retry identity. |
| confirmed | Legal 1 MiB prompt/event payloads could exceed the 8 MiB response cap under existing 50/100-item page sizes. |
| confirmed | The shared recovery-state file had no read bound and malformed recovery fields could be silently overwritten. |
| confirmed | The chat-state coordination lock followed symlinks and could operate on a non-regular target. |
| confirmed | A reconnect race could append a live stream after cleanup and leave a daemon/socket alive until the idle timeout. |

Rejected as intended or already protected:

- unmatched provider attempts must remain blocked to prevent unsafe replay;
- a duplicate title request after process death is explicitly accepted and title CAS prevents an
  overwrite;
- exact prompt retries reconcile durable admission and do not directly replay provider work;
- graceful compaction interruption already publishes a terminal failure;
- title CAS races, repeated-summary anchoring, generated clients, event manifests, and legacy
  compaction configuration were consistent.

Fixes:

- empty or defective provider streams now publish a terminal assistant failure and a matching
  failed provider-attempt outcome; overflow recovery closes the original attempt before any
  replacement call;
- compaction selection budgets pending input separately from completed-turn retention and rejects
  a newest turn that cannot fit the real request before calling the summarizer;
- a small recovery endpoint exposes only four durable uncertainty flags; the terminal refuses new
  input for unmatched provider, unmatched compaction, or attempted-but-unsettled states;
- mutation responses remain uncertain unless their successful payload shape and requested identity
  are confirmed;
- ordinary transcript reads use one-message pages so every legal prompt remains below the bounded
  response limit;
- prompt journals are bounded, mode-`0600`, and isolated by hashed Session ID; state and lock reads
  reject malformed, oversized, symlinked, or non-regular targets; and
- event-stream reconnect and cleanup now coordinate under one lock.

Verification:

- `packages/core`: 94 runner tests passed; package typecheck passed.
- Protocol, Server, and OpenCode package typechecks passed.
- The two focused HTTP recovery-route tests passed.
- Python terminal, response-validation, and private-I/O tests: 28 passed; compilation and
  `git diff --check` passed.
- Socket-backed Python API tests remain unavailable because the managed audit sandbox denies
  loopback socket creation. The no-socket response cases passed.

## Cycle 3

Three fresh reviewers covered the revised core crash boundaries and compaction math, the Python
recovery/security boundary, and complete Protocol/offline integration. Duplicate findings were
consolidated. The following were confirmed against the implementation and V2 design:

| Disposition | Finding |
| --- | --- |
| confirmed | Overflow recovery ended its provider attempt before `Compaction.Started`, leaving a power-loss gap in which direct core resume could replay attempted work. |
| confirmed | Compaction counted `finish: "tool-calls"` as a complete turn instead of an intermediate continuation boundary. |
| confirmed | Retained-context budgeting estimated raw text rather than its escaped checkpoint request, so an escape-heavy turn could cause a paid summary before final rejection. |
| confirmed | Provider/compaction recovery guards scanned every historical lifecycle event and large compaction payloads on every wake. |
| confirmed | Recovery context, lifecycle, and pending-input reads were not taken from one database snapshot. |
| confirmed | Aggregate recovery flags did not prove that an unresolved input matched the local journal; a stale journal could wake unrelated queued work. |
| confirmed | Journal rename and unlink operations did not synchronize their parent directory, so sudden power loss could lose the exact retry identity. |
| confirmed | Agent and model switches remained available for `terminal-chat` Sessions, leaving a race around the tool-free Bedrock-only invariant. |
| confirmed | A failed attachment recovery occurred after switch commit, silently changing the active chat even though `/use` reported an error. |
| confirmed | Switching checked the target Session but not the currently attached Session for active execution. |
| confirmed | Empty, extra-field, empty-ID, or oversized prompt journals were not rejected consistently. |
| confirmed | Successful session/message/history reads lacked endpoint-specific shape validation and could escape as raw Python exceptions. |
| confirmed | Lock and atomic-state I/O failures could bypass the wrapper's user-facing error boundary. |
| confirmed | A malformed private service record could inject a non-integer port into the nominally loopback-only URL authority. |

Rejected as intended or already protected:

- genuinely unmatched provider attempts, compactions, attempted-but-unsettled inputs, and
  journal-less unresolved inputs must remain blocked;
- exact durable admission retry does not itself replay a provider call;
- a graceful compaction interruption already publishes `Compaction.Failed`;
- one-message paging preserves cursor ordering and response bounds;
- stream reconnect cleanup, `Ctrl-C` journal preservation, generated clients, schema migration,
  no-tool agent configuration, and title/compaction tool suppression were consistent; and
- no runtime dependency, external Git source, public-network call, `sudo` requirement, or default
  live-AWS test was introduced.

Fixes:

- overflow recovery now publishes `Compaction.Started` before closing the original provider
  attempt; every process-death boundary therefore leaves either the attempt or compaction visibly
  unmatched, and newly queued input cannot wake an attempted-but-unsettled turn;
- the recovery service uses one read transaction and bounded latest-lifecycle queries, returns the
  requested journal message's status plus whether other unresolved input exists, and preserves
  safe tool continuation without permitting idle replay;
- compaction leaves `tool-calls` turns unsettled and measures retained context using the exact
  encoded checkpoint request before any summary call;
- `terminal-chat` agent and model switches are rejected server-side;
- attachment recovery is message-correlated, never wakes competing work, and Session switching
  does not commit until target recovery succeeds;
- prompt journals and selection state reject malformed data, synchronize their parent directory
  after replacement/removal, and translate filesystem failures into user-facing errors;
- successful API reads now receive endpoint-specific shape and requested-identity validation; and
- the loopback client rejects any service port that is not an integer in `1..65535`.

Verification:

- Core runner, compaction, and Session tests: 122 passed, including crash-transition, no-replay,
  tool-continuation, exact-budget, recovery-state, and immutable-selection regressions.
- Focused OpenCode HTTP tests: 5 passed, covering missing, pending, idle, corrupt, and immutable
  terminal-chat routes.
- Python chat, API response, and private-I/O tests: 44 passed; Python compilation passed.
- Core, Protocol, Server, Client, and OpenCode package typechecks passed.
- Public clients were regenerated; Prettier and `git diff --check` passed.
- The complete offline `linux-x64` artifact built successfully and its bundled OpenCode executable
  passed the build smoke test.
- Socket-backed Python tests remain unavailable because the managed audit sandbox denies loopback
  socket creation. All corresponding no-socket validation paths passed.

## Cycle 4

Three fresh reviewers independently audited the revised durable core, Python client, and complete
HTTP/offline integration. Reports were reproduced against source and deduplicated. The following
findings were confirmed:

| Disposition | Finding |
| --- | --- |
| confirmed | Compaction serialized the current promoted input into historical checkpoint text; a crash after `Compaction.Ended` could then make recovery classify that unanswered input as settled. |
| confirmed | A second compaction summarized all verbatim turns retained by the previous checkpoint, even when the configured newest-turn retention still fit. |
| confirmed | Summary preflight measured raw prompt text instead of the encoded provider request, so escape-heavy history could pass preflight and start an oversized paid call. |
| confirmed | Recovery loaded and decoded the entire active message context despite its bounded-query contract, making attachment and every wake scale with all post-checkpoint rows. |
| confirmed | The server allowed an arbitrary agent/model tuple to be created with `terminal-chat` purpose, and legacy execution routes could bypass the V2 tool-free runner and its active-drain serialization. |
| confirmed | A `5xx` response, or any HTTP error after an outcome-unknown first attempt, could delete the only stable Session-creation or prompt-admission recovery identity. |
| confirmed | Successful Session and message responses were validated too shallowly, allowing malformed nested data to escape the CLI's `BedrockError` boundary as `KeyError` or `TypeError`. |
| confirmed | Non-success event-stream setup and non-object SSE JSON had raw exception paths instead of falling back to durable reconciliation. |
| confirmed | Concurrent title-ensure requests could both call the paid title model before one compare-and-set lost, and the supplied message ID did not have to identify the first user message. |

Rejected as intended or already protected:

- unmatched provider or compaction work must remain blocked rather than replayed;
- a duplicate title request after process death remains acceptable, but concurrent live requests
  should coalesce;
- read-only legacy history access does not bypass model/tool invariants;
- one-message paging, exact successful mutation reconciliation, stored-location routing, Basic
  authentication, loopback/proxy/redirect confinement, private file durability, and generated
  recovery clients remained consistent; and
- no dependency, public repository, Internet path, `sudo` requirement, or default live-AWS test
  was introduced.

Fixes:

- compaction checkpoints now carry backward-compatible optional retained message IDs, load the
  exact retained rows after the checkpoint, keep the current input structurally model-facing, and
  preserve the newest configured complete-turn suffix through repeated compactions;
- summary preflight constructs and budgets the exact canonical summary request before publishing
  `Compaction.Started` or calling the provider;
- recovery uses bounded latest/existence queries and treats malformed or excessive attempt input
  metadata conservatively until a later terminal settlement;
- `terminal-chat` creation requires the exact `chat`/`amazon-bedrock`/`opus`/implicit-local tuple,
  including exact adoption of an existing ID, and all 18 legacy mutation or execution routes
  reject terminal chats while read routes remain available;
- title generation requires the actual first user message and coalesces concurrent live requests
  per Session;
- the Python client preserves stable creation/admission identity after every outcome-unknown
  response, serializes creation through successful attachment commit, validates nested Session and
  message data, and converts event-stream setup or shape failures into durable fallback; and
- public V2 clients and the legacy JavaScript SDK were regenerated from their schemas.

Verification:

- Core runner, compaction, and Session creation: 126 tests passed, 405 assertions.
- Schema event manifest: 2 tests passed; Schema typecheck passed.
- Title coordinator: 1 test passed.
- Focused OpenCode HTTP terminal creation/immutability/title/legacy-denial tests: 3 passed, 46
  assertions.
- Python chat, API, private-I/O, packaging, policy, project, sandbox, and task tests: 73 passed;
  Python compilation passed.
- Core, Protocol, Server, Client, and OpenCode typechecks passed.
- Client generation, legacy SDK generation, and `git diff --check` passed.

## Cycle 5

Three final fresh reviewers attacked the verified Cycle 4 state, including the new structured
checkpoint format, all nonstandard mutation routes, and Python failure boundaries. The following
unique findings were confirmed:

| Disposition | Finding |
| --- | --- |
| confirmed | A crash after durable terminal assistant settlement but before `ProviderAttemptEnded` left a stale unmatched start that blocked the already-settled Session forever. |
| confirmed | A crash after overflow `Compaction.Ended` lost the in-memory safe-continuation transition and recovery conservatively blocked the exact retained input forever. |
| confirmed | V2 revert stage/clear/commit remained available to terminal chats, allowing filesystem restoration and transcript truncation outside the chat contract. |
| confirmed | Control-plane move, workspace warp, and sync steal could change a terminal chat's immutable Location, and some paths could touch Git/files or remote synchronization first. |
| confirmed | Sync event replay could create or mutate `terminal-chat` state with an invalid agent, model, or workspace tuple, bypassing the V2 creation and switch guards. |
| confirmed | History accumulated as many as 50 separately bounded message responses before applying the 64 KiB render cap, allowing excessive retained memory. |
| confirmed | Failure of parent-directory synchronization after atomic replacement made a switch look failed in memory after its new selected Session was already visible on disk. |
| confirmed | Event-stream reconnection constructed an untracked replacement socket that final cleanup could not cancel while response headers were pending. |
| confirmed | Successful Session/message validators still accepted required-field omissions and malformed nested assistant/tool/compaction records outside the exact Protocol schema. |
| confirmed | Title singleflight was keyed only by Session even though concurrent effects could carry different first-message identities. |
| confirmed | Interrupt and Session-event-stream handlers did not honor their declared missing-Session errors. |

Rejected as intended, already protected, or outside this specialization:

- structured checkpoints remain compatible with old `recent`-only rows, and normal revert
  boundaries do not create dangling retained IDs;
- bounded recovery SQL, malformed-attempt conservatism, exact summary preflight, inline attachment
  lowering, V2 compact/wait behavior, and all 18 guarded legacy Session routes remained correct;
- generic non-chat move/revert serialization is a broader upstream concurrency concern; this fork
  will enforce the terminal-chat boundary without redesigning unrelated OpenCode workflows; and
- no runtime dependency, Internet/registry/Git requirement, `sudo` use, public listener, or default
  live-AWS test was found.

Fixes:

- recovery now treats a durable terminal assistant as settlement even if the matching
  provider-attempt end event was lost, while still blocking genuinely unresolved attempts;
- overflow compaction records the exact attempt and input correlation in `Compaction.Ended`, so a
  restart can authorize that one continuation exactly once without making generic compaction a
  replay signal;
- every terminal-chat revert and move entry point now fails before transcript, Git, filesystem,
  remote-sync, or Location effects, including control-plane move, workspace warp, and sync steal;
- the event projector and sync replay enforce the exact parentless, implicit-local,
  `chat`/`amazon-bedrock`/`opus` creation tuple and reject later immutable terminal configuration
  changes;
- missing interrupt and Session-event-stream requests now return their declared typed errors, and
  title-call singleflight includes both Session and first-message identity;
- history rendering has an 8 MiB aggregate retained-response bound in addition to its display
  bound;
- post-replacement directory-sync failure adopts the already-visible selection while reporting
  durability uncertainty, rather than pretending the old selection remains active;
- event-stream reconnect was removed, eliminating an untracked pending socket without weakening
  durable final-response reconciliation; and
- successful Python responses are checked against all Session/message variants and tool-state
  shapes before the terminal consumes them.

Verification:

- Core runner, compaction, and Session creation: 130 tests passed; the terminal move guard passed
  separately.
- Schema event manifest: 2 tests passed. Title coordinator: 3 tests passed.
- Focused Session HTTP tests: 4 passed, 66 assertions.
- Python chat, API, private-I/O, packaging, policy, project, sandbox, and task tests: 79 passed;
  Python compilation passed.
- Schema, Core, Protocol, Server, Client, and OpenCode typechecks passed.
- Client and legacy JavaScript SDK generation, `git diff --check`, the offline `linux-x64`
  artifact build, and its bundled executable smoke test passed.
- The control-plane, sync, and workspace HTTP fixtures could not open their ephemeral listener in
  this managed audit environment (`EADDRINUSE` for port `0`), so their new assertions did not run.
  The Session HTTP fixture did run successfully in the same pass.
- The complete MoveSession file also has two unrelated non-terminal Git-patch fixture failures
  (`git apply: No valid patches in input`). The new terminal guard passes, and its only production
  change is an early purpose check before the unaffected Git path.

## Final verification

All five review/fix cycles are complete. Every confirmed terminal-chat issue is fixed and covered
by a focused regression test; no terminal-chat finding is deferred. The remaining test limitations
above occur before the affected assertions or in an unrelated generic move fixture.

The final diff adds no runtime package, external repository, public-network dependency, `sudo`
requirement, non-loopback listener, default live-AWS call, or AWS identifier. Runtime model traffic
remains limited to the configured Amazon Bedrock Claude endpoint.
