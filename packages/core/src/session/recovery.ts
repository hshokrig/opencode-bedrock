export * as SessionRecovery from "./recovery"

import { and, desc, eq, gt, inArray, isNull, ne, sql } from "drizzle-orm"
import { Effect } from "effect"
import type { Database } from "../database/database"
import { EventV2 } from "../event"
import { EventTable } from "../event/sql"
import { SessionEvent } from "./event"
import { SessionMessage } from "./message"
import { SessionSchema } from "./schema"
import { SessionInputTable, SessionMessageTable } from "./sql"

type DatabaseService = Database.Interface["db"]

export const RequestedInputStatuses = ["not-requested", "absent", "unattempted", "attempted", "settled"] as const
export type RequestedInputStatus = (typeof RequestedInputStatuses)[number]

export type State = {
  readonly unfinishedProviderAttempt: boolean
  readonly unfinishedCompaction: boolean
  readonly unresolvedInput: boolean
  readonly attemptedUnsettledInput: boolean
  readonly requestedInputStatus: RequestedInputStatus
  readonly otherUnresolvedInput: boolean
}

export type RunnerState = State & {
  readonly overflowContinuation:
    | {
        readonly inputMessageIDs: readonly SessionMessage.ID[]
      }
    | undefined
}

const MAX_ATTEMPT_INPUTS = 128

const parseMessageIDs = (value: unknown) => {
  if (
    !Array.isArray(value) ||
    value.length > MAX_ATTEMPT_INPUTS ||
    !value.every((id): id is string => typeof id === "string" && id.startsWith("msg_"))
  )
    return
  return value.map((id) => SessionMessage.ID.make(id))
}

const parseContinuation = (value: unknown) => {
  if (value === null || typeof value !== "object" || !("attemptID" in value) || !("inputMessageIDs" in value)) return
  if (typeof value.attemptID !== "string") return
  const inputMessageIDs = parseMessageIDs(value.inputMessageIDs)
  if (!inputMessageIDs) return
  return { attemptID: value.attemptID, inputMessageIDs }
}

const inspectRunner = Effect.fn("SessionRecovery.inspectRunner")(function* (
  db: DatabaseService,
  input: {
    readonly sessionID: SessionSchema.ID
    readonly messageID?: SessionMessage.ID
  },
) {
  const providerStarted = EventV2.versionedType(SessionEvent.ProviderAttemptStarted.type, 1)
  const providerEnded = EventV2.versionedType(SessionEvent.ProviderAttemptEnded.type, 1)
  const compactionStarted = EventV2.versionedType(SessionEvent.Compaction.Started.type, 1)
  const compactionEnded = EventV2.versionedType(SessionEvent.Compaction.Ended.type, 1)
  const compactionFailed = EventV2.versionedType(SessionEvent.Compaction.Failed.type, 1)
  return yield* db
    .transaction((tx) =>
      Effect.gen(function* () {
        const [providerLifecycle, providerAttempt, compactionLifecycle, latestTerminal] = yield* Effect.all(
          [
            tx
              .select({ seq: EventTable.seq, type: EventTable.type, data: EventTable.data })
              .from(EventTable)
              .where(
                and(
                  eq(EventTable.aggregate_id, input.sessionID),
                  inArray(EventTable.type, [providerStarted, providerEnded]),
                ),
              )
              .orderBy(desc(EventTable.seq))
              .limit(1)
              .get(),
            tx
              .select({ seq: EventTable.seq, data: EventTable.data })
              .from(EventTable)
              .where(and(eq(EventTable.aggregate_id, input.sessionID), eq(EventTable.type, providerStarted)))
              .orderBy(desc(EventTable.seq))
              .limit(1)
              .get(),
            tx
              .select({ seq: EventTable.seq, type: EventTable.type, data: EventTable.data })
              .from(EventTable)
              .where(
                and(
                  eq(EventTable.aggregate_id, input.sessionID),
                  inArray(EventTable.type, [compactionStarted, compactionEnded, compactionFailed]),
                ),
              )
              .orderBy(desc(EventTable.seq))
              .limit(1)
              .get(),
            tx
              .select({ seq: SessionMessageTable.seq })
              .from(SessionMessageTable)
              .where(
                and(
                  eq(SessionMessageTable.session_id, input.sessionID),
                  eq(SessionMessageTable.type, "assistant"),
                  sql<boolean>`case
                    when json_valid(${SessionMessageTable.data}) then
                      coalesce(json_extract(${SessionMessageTable.data}, '$.finish'), '') <> 'tool-calls'
                      and (
                        json_type(${SessionMessageTable.data}, '$.time.completed') is not null
                        or json_type(${SessionMessageTable.data}, '$.finish') is not null
                        or json_type(${SessionMessageTable.data}, '$.error') is not null
                      )
                    else 0
                  end`,
                ),
              )
              .orderBy(desc(SessionMessageTable.seq))
              .limit(1)
              .get(),
          ],
          { concurrency: "unbounded" },
        )
        const rawAttemptID = providerAttempt?.data.attemptID
        const attemptInputMessageIDs = parseMessageIDs(providerAttempt?.data.inputMessageIDs)
        const attemptDataValid =
          providerAttempt === undefined || (typeof rawAttemptID === "string" && attemptInputMessageIDs !== undefined)
        const attempted = new Set(
          attemptDataValid && attemptInputMessageIDs !== undefined ? attemptInputMessageIDs : [],
        )
        const terminalSeq = latestTerminal?.seq ?? -1
        const terminalSettlesAttempt = providerAttempt !== undefined && terminalSeq > providerAttempt.seq
        const corruptUnsettledAttempt = !attemptDataValid && providerAttempt !== undefined && !terminalSettlesAttempt
        const continuation = parseContinuation(compactionLifecycle?.data.continuation)
        const continuationInputMessageIDs =
          continuation !== undefined &&
          attemptDataValid &&
          providerAttempt !== undefined &&
          attemptInputMessageIDs !== undefined &&
          providerLifecycle?.type === providerEnded &&
          compactionLifecycle?.type === compactionEnded &&
          compactionLifecycle.seq > providerAttempt.seq &&
          terminalSeq < compactionLifecycle.seq &&
          continuation.attemptID === rawAttemptID &&
          continuation.inputMessageIDs.length === attemptInputMessageIDs.length &&
          continuation.inputMessageIDs.every((id, index) => id === attemptInputMessageIDs[index])
            ? continuation.inputMessageIDs
            : undefined
        const [
          candidatePending,
          otherPending,
          candidateMessage,
          candidateInput,
          unresolvedMessage,
          otherUnresolvedMessage,
          attemptedUnsettledMessage,
        ] = yield* Effect.all(
          [
            tx
              .select({ id: SessionInputTable.id })
              .from(SessionInputTable)
              .where(
                and(
                  eq(SessionInputTable.session_id, input.sessionID),
                  isNull(SessionInputTable.promoted_seq),
                  input.messageID === undefined ? undefined : eq(SessionInputTable.id, input.messageID),
                ),
              )
              .limit(1)
              .get(),
            tx
              .select({ id: SessionInputTable.id })
              .from(SessionInputTable)
              .where(
                and(
                  eq(SessionInputTable.session_id, input.sessionID),
                  isNull(SessionInputTable.promoted_seq),
                  input.messageID === undefined ? undefined : ne(SessionInputTable.id, input.messageID),
                ),
              )
              .limit(1)
              .get(),
            input.messageID === undefined
              ? Effect.succeed(undefined)
              : tx
                  .select({
                    id: SessionMessageTable.id,
                    type: SessionMessageTable.type,
                    seq: SessionMessageTable.seq,
                  })
                  .from(SessionMessageTable)
                  .where(
                    and(
                      eq(SessionMessageTable.id, input.messageID),
                      eq(SessionMessageTable.session_id, input.sessionID),
                    ),
                  )
                  .limit(1)
                  .get(),
            input.messageID === undefined
              ? Effect.succeed(undefined)
              : tx
                  .select({ id: SessionInputTable.id })
                  .from(SessionInputTable)
                  .where(
                    and(eq(SessionInputTable.id, input.messageID), eq(SessionInputTable.session_id, input.sessionID)),
                  )
                  .limit(1)
                  .get(),
            tx
              .select({ id: SessionMessageTable.id })
              .from(SessionMessageTable)
              .where(
                and(
                  eq(SessionMessageTable.session_id, input.sessionID),
                  eq(SessionMessageTable.type, "user"),
                  gt(SessionMessageTable.seq, terminalSeq),
                ),
              )
              .limit(1)
              .get(),
            tx
              .select({ id: SessionMessageTable.id })
              .from(SessionMessageTable)
              .where(
                and(
                  eq(SessionMessageTable.session_id, input.sessionID),
                  eq(SessionMessageTable.type, "user"),
                  gt(SessionMessageTable.seq, terminalSeq),
                  input.messageID === undefined ? undefined : ne(SessionMessageTable.id, input.messageID),
                ),
              )
              .limit(1)
              .get(),
            attempted.size === 0
              ? Effect.succeed(undefined)
              : tx
                  .select({ id: SessionMessageTable.id })
                  .from(SessionMessageTable)
                  .where(
                    and(
                      eq(SessionMessageTable.session_id, input.sessionID),
                      eq(SessionMessageTable.type, "user"),
                      gt(SessionMessageTable.seq, terminalSeq),
                      inArray(SessionMessageTable.id, Array.from(attempted)),
                    ),
                  )
                  .limit(1)
                  .get(),
          ],
          { concurrency: "unbounded" },
        )
        const requestedUnresolved =
          candidatePending !== undefined || (candidateMessage?.type === "user" && candidateMessage.seq > terminalSeq)
        const requestedInputStatus: RequestedInputStatus =
          input.messageID === undefined
            ? "not-requested"
            : requestedUnresolved
              ? corruptUnsettledAttempt || attempted.has(input.messageID)
                ? "attempted"
                : "unattempted"
              : candidateMessage || candidateInput
                ? "settled"
                : "absent"
        return {
          unfinishedProviderAttempt: providerLifecycle?.type === providerStarted && !terminalSettlesAttempt,
          unfinishedCompaction: compactionLifecycle?.type === compactionStarted,
          unresolvedInput:
            unresolvedMessage !== undefined || candidatePending !== undefined || otherPending !== undefined,
          attemptedUnsettledInput: corruptUnsettledAttempt || attemptedUnsettledMessage !== undefined,
          requestedInputStatus,
          otherUnresolvedInput: otherPending !== undefined || otherUnresolvedMessage !== undefined,
          overflowContinuation:
            continuationInputMessageIDs === undefined ? undefined : { inputMessageIDs: continuationInputMessageIDs },
        } satisfies RunnerState
      }),
    )
    .pipe(Effect.orDie)
})

export { inspectRunner }

export const inspect = Effect.fn("SessionRecovery.inspect")(function* (
  db: DatabaseService,
  input: {
    readonly sessionID: SessionSchema.ID
    readonly messageID?: SessionMessage.ID
  },
) {
  const state = yield* inspectRunner(db, input)
  const requestedOverflowContinuation =
    input.messageID !== undefined &&
    state.overflowContinuation?.inputMessageIDs.some((messageID) => messageID === input.messageID) === true
  return {
    unfinishedProviderAttempt: state.unfinishedProviderAttempt,
    unfinishedCompaction: state.unfinishedCompaction,
    unresolvedInput: state.unresolvedInput,
    attemptedUnsettledInput: requestedOverflowContinuation ? false : state.attemptedUnsettledInput,
    requestedInputStatus: requestedOverflowContinuation ? "unattempted" : state.requestedInputStatus,
    otherUnresolvedInput: state.otherUnresolvedInput,
  } satisfies State
})
