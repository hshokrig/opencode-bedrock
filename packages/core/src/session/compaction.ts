export * as SessionCompaction from "./compaction"

import { LLM, LLMError, LLMEvent, Message, type LLMRequest, type Model } from "@opencode-ai/llm"
import { DateTime, Effect, Stream } from "effect"
import type { Config } from "../config"
import type { EventV2 } from "../event"
import { SessionEvent } from "./event"
import { SessionMessage } from "./message"
import { SessionSchema } from "./schema"
import { Token } from "../util/token"

const DEFAULT_BUFFER = 20_000
const DEFAULT_KEEP_TOKENS = 8_000
const DEFAULT_KEEP_TURNS = 2
const TOOL_OUTPUT_MAX_CHARS = 2_000
const SUMMARY_OUTPUT_TOKENS = 4_096
const SUMMARY_TEMPLATE = `Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers when known.
- Do not mention the summary process or that context was compacted.`

type Entry = {
  readonly seq: number
  readonly message: SessionMessage.Message
}

type Settings = {
  readonly auto: boolean
  readonly buffer: number
  readonly tokens: number
  readonly turns: number
}

type Dependencies = {
  readonly events: EventV2.Interface
  readonly llm: {
    readonly stream: (request: LLMRequest) => Stream.Stream<LLMEvent, LLMError>
  }
  readonly config: readonly Config.Entry[]
}

type Input = {
  readonly sessionID: SessionSchema.ID
  readonly entries: readonly Entry[]
  readonly model: Model
  readonly request: LLMRequest
  readonly reason?: "auto" | "manual"
}

export type Result = "not-needed" | "compacted" | "failed" | "cannot-fit"

const estimate = (value: unknown) => Token.estimate(JSON.stringify(value))

const truncate = (value: string) =>
  value.length <= TOOL_OUTPUT_MAX_CHARS ? value : `${value.slice(0, TOOL_OUTPUT_MAX_CHARS)}\n[truncated]`

export const serializeToolContent = (content: SessionMessage.ToolStateCompleted["content"]) =>
  content
    .map((item) =>
      item.type === "text" ? item.text : `[Attached ${item.mime}${item.name === undefined ? "" : `: ${item.name}`}]`,
    )
    .join("\n")

const serialize = (message: SessionMessage.Message) => {
  if (message.type === "user") {
    const files = message.files?.map((file) => `[Attached ${file.mime}: ${file.name ?? file.uri}]`) ?? []
    return [`[User]: ${message.text}`, ...files].join("\n")
  }
  if (message.type === "assistant") {
    return message.content
      .flatMap((part) => {
        if (part.type === "text") return [`[Assistant]: ${part.text}`]
        if (part.type === "reasoning") return part.text ? [`[Assistant reasoning]: ${part.text}`] : []
        const input = typeof part.state.input === "string" ? part.state.input : JSON.stringify(part.state.input)
        if (part.state.status === "completed")
          return [
            `[Assistant tool call]: ${part.name}(${input})`,
            `[Tool result]: ${truncate(serializeToolContent(part.state.content))}`,
          ]
        if (part.state.status === "error")
          return [`[Assistant tool call]: ${part.name}(${input})`, `[Tool error]: ${part.state.error.message}`]
        return [`[Assistant tool call]: ${part.name}(${input})`]
      })
      .join("\n")
  }
  if (message.type === "system") return `[System update]: ${message.text}`
  if (message.type === "synthetic") return `[Synthetic context]: ${message.text}`
  if (message.type === "shell") return `[Shell]: ${message.command}\n${truncate(message.output)}`
  return ""
}

const settings = (documents: readonly Config.Entry[]) => {
  const configured = documents
    .filter((entry): entry is Config.Document => entry.type === "document")
    .flatMap((entry) => (entry.info.compaction ? [entry.info.compaction] : []))
  return configured.reduce<Settings>(
    (result, current) => ({
      auto: current.auto ?? result.auto,
      buffer: current.buffer ?? result.buffer,
      tokens: current.keep?.tokens ?? result.tokens,
      turns: current.keep?.turns ?? result.turns,
    }),
    { auto: true, buffer: DEFAULT_BUFFER, tokens: DEFAULT_KEEP_TOKENS, turns: DEFAULT_KEEP_TURNS },
  )
}

const select = (
  entries: readonly Entry[],
  tokens: number,
  turns: number,
): { readonly head: string; readonly recent: string } | undefined => {
  const conversation = entries
    .filter((entry) => entry.message.type !== "compaction")
    .map((entry) => ({ message: entry.message, text: serialize(entry.message) }))
    .filter((entry) => entry.text.length > 0)
  if (conversation.length === 0) return

  const groups = conversation.reduce<
    Array<{ readonly text: string[]; hasUser: boolean; settled: boolean; complete: boolean }>
  >(
    (result, entry) => {
      const current = result.at(-1)
      if (entry.message.type === "user") {
        if (current && current.hasUser && !current.settled) {
          current.text.push(entry.text)
          return result
        }
        result.push({ text: [entry.text], hasUser: true, settled: false, complete: false })
        return result
      }
      if (current?.complete && entry.message.type === "assistant") {
        current.text.push(entry.text)
        return result
      }
      if (!current || current.settled) {
        result.push({ text: [entry.text], hasUser: false, settled: true, complete: false })
        return result
      }
      current.text.push(entry.text)
      if (
        entry.message.type === "assistant" &&
        (entry.message.time.completed !== undefined ||
          entry.message.finish !== undefined ||
          entry.message.error !== undefined)
      ) {
        current.settled = true
        current.complete =
          current.hasUser &&
          entry.message.error === undefined &&
          entry.message.finish !== "error" &&
          entry.message.time.completed !== undefined
      }
      return result
    },
    [],
  )
  const retained = new Set<number>()
  let total = 0
  let count = 0
  for (let index = groups.length - 1; index >= 0; index--) {
    const group = groups[index]
    if (!group.settled) {
      retained.add(index)
      total += Token.estimate(group.text.join("\n\n"))
      continue
    }
    if (!group.complete) continue
    if (count >= turns) continue
    const next = total + Token.estimate(group.text.join("\n\n"))
    if (next > tokens && (count > 0 || total > 0)) continue
    retained.add(index)
    total = next
    count += 1
  }
  return {
    head: groups
      .filter((_, index) => !retained.has(index))
      .flatMap((group) => group.text)
      .join("\n\n"),
    recent: groups
      .filter((_, index) => retained.has(index))
      .flatMap((group) => group.text)
      .join("\n\n"),
  }
}

export const buildPrompt = (input: { readonly previousSummary?: string; readonly context: readonly string[] }) =>
  [
    input.previousSummary
      ? `Update the anchored summary below using the conversation history above.\nPreserve still-true details, remove stale details, and merge in the new facts.\n<previous-summary>\n${input.previousSummary}\n</previous-summary>`
      : "Create a new anchored summary from the conversation history.",
    SUMMARY_TEMPLATE,
    ...input.context,
  ].join("\n\n")

export const make = (dependencies: Dependencies) => {
  const config = settings(dependencies.config)
  const compactAfterOverflow = Effect.fn("SessionCompaction.compactAfterOverflow")(function* (input: Input) {
    const context = input.model.route.defaults.limits?.context
    if (context === undefined || context <= 0) return "cannot-fit" as const
    const output = input.request.generation?.maxTokens ?? input.model.route.defaults.limits?.output ?? 0
    const selected = select(input.entries, config.tokens, config.turns)
    const previousSummary = input.entries.find((entry) => entry.message.type === "compaction")?.message
    if (!selected || (selected.head.length === 0 && previousSummary?.type !== "compaction"))
      return "cannot-fit" as const
    const summaryPrompt = buildPrompt({
      previousSummary: previousSummary?.type === "compaction" ? previousSummary.summary : undefined,
      context: [previousSummary?.type === "compaction" ? previousSummary.recent : "", selected.head].filter(Boolean),
    })
    const summaryOutput = Math.min(output || SUMMARY_OUTPUT_TOKENS, SUMMARY_OUTPUT_TOKENS)
    if (Token.estimate(summaryPrompt) > context - summaryOutput) return "cannot-fit" as const
    const messageID = SessionMessage.ID.create()
    yield* dependencies.events.publish(SessionEvent.Compaction.Started, {
      sessionID: input.sessionID,
      messageID,
      timestamp: yield* DateTime.now,
      reason: input.reason ?? "auto",
    })

    const chunks: string[] = []
    let failed = false
    const publishFailure = Effect.fnUntraced(function* (
      failure: "provider-error" | "empty-summary" | "interrupted",
    ) {
      yield* dependencies.events.publish(SessionEvent.Compaction.Failed, {
        sessionID: input.sessionID,
        messageID,
        timestamp: yield* DateTime.now,
        reason: input.reason ?? "auto",
        failure,
      })
    })
    const summarized = yield* dependencies.llm
      .stream(
        LLM.request({
          model: input.model,
          messages: [Message.user(summaryPrompt)],
          tools: [],
          generation: { maxTokens: summaryOutput },
        }),
      )
      .pipe(
        Stream.runForEach((event) => {
          if (LLMEvent.is.providerError(event)) failed = true
          if (LLMEvent.is.textDelta(event)) chunks.push(event.text)
          return Effect.void
        }),
        Effect.as(true),
        Effect.catchTag("LLM.Error", () => Effect.succeed(false)),
        Effect.onInterrupt(() => publishFailure("interrupted")),
      )
    const summary = chunks.join("")
    if (!summarized || failed) {
      yield* publishFailure("provider-error")
      return "failed" as const
    }
    if (!summary.trim()) {
      yield* publishFailure("empty-summary")
      return "failed" as const
    }
    yield* dependencies.events.publish(SessionEvent.Compaction.Ended, {
      sessionID: input.sessionID,
      messageID,
      timestamp: yield* DateTime.now,
      reason: input.reason ?? "auto",
      text: summary,
      recent: selected.recent,
    })
    return "compacted" as const
  })
  const compactIfNeeded = Effect.fn("SessionCompaction.compactIfNeeded")(function* (input: Input) {
    if (!config.auto) return "not-needed" as const
    const context = input.model.route.defaults.limits?.context
    if (context === undefined || context <= 0) return "not-needed" as const
    const output = input.request.generation?.maxTokens ?? input.model.route.defaults.limits?.output ?? 0
    if (
      estimate({ system: input.request.system, messages: input.request.messages, tools: input.request.tools }) <=
      context - Math.max(output, config.buffer)
    )
      return "not-needed" as const
    const latestCompaction = input.entries.findLastIndex((entry) => entry.message.type === "compaction")
    if (
      latestCompaction >= 0 &&
      !input.entries.slice(latestCompaction + 1).some(
        (entry) =>
          entry.message.type === "assistant" &&
          entry.message.error === undefined &&
          entry.message.finish !== "error" &&
          entry.message.time.completed !== undefined,
      )
    )
      return "not-needed" as const
    const selected = select(input.entries, config.tokens, config.turns)
    if (selected?.head.length === 0) return "not-needed" as const
    return yield* compactAfterOverflow(input)
  })
  return {
    compactIfNeeded,
    compactAfterOverflow,
  }
}
