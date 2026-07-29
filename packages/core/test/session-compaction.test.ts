import { expect, test } from "bun:test"
import { LLM, LLMEvent, Model, type LLMRequest } from "@opencode-ai/llm"
import * as OpenAIChat from "@opencode-ai/llm/protocols/openai-chat"
import { AgentV2 } from "@opencode-ai/core/agent"
import { Config } from "@opencode-ai/core/config"
import { ConfigCompaction } from "@opencode-ai/core/config/compaction"
import { EventV2 } from "@opencode-ai/core/event"
import { ModelV2 } from "@opencode-ai/core/model"
import { ProviderV2 } from "@opencode-ai/core/provider"
import { SessionV2 } from "@opencode-ai/core/session"
import { SessionCompaction } from "@opencode-ai/core/session/compaction"
import { SessionMessage } from "@opencode-ai/core/session/message"
import { toLLMMessages } from "@opencode-ai/core/session/runner/to-llm-message"
import { DateTime, Effect, Stream } from "effect"

test("compaction prompt preserves detailed work state and relevant files", () => {
  const prompt = SessionCompaction.buildPrompt({ context: ["conversation history"] })

  expect(prompt).toContain("## Work State\n### Completed")
  expect(prompt).toContain("### Active")
  expect(prompt).toContain("### Blocked")
  expect(prompt).toContain("## Relevant Files")
})

test("compaction describes tool media without embedding base64", () => {
  const base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
  const serialized = SessionCompaction.serializeToolContent([
    { type: "text", text: "Image read successfully" },
    {
      type: "file",
      uri: `data:image/png;base64,${base64}`,
      mime: "image/png",
      name: "pixel.png",
    },
  ])

  expect(serialized).toBe("Image read successfully\n[Attached image/png: pixel.png]")
  expect(serialized).not.toContain(base64)
})

const created = DateTime.makeUnsafe(0)
const sessionID = SessionV2.ID.make("ses_compaction_unit")
const model = Model.make({
  id: "compact",
  provider: "fake",
  route: OpenAIChat.route.with({ limits: { context: 4_000, output: 50 } }),
})
const entries = [
  {
    seq: 0,
    message: SessionMessage.User.make({
      id: SessionMessage.ID.make("msg_earlier"),
      type: "user",
      text: "Earlier question",
      time: { created },
    }),
  },
  {
    seq: 1,
    message: SessionMessage.Assistant.make({
      id: SessionMessage.ID.make("msg_earlier_answer"),
      type: "assistant",
      agent: AgentV2.ID.make("build"),
      model: { id: ModelV2.ID.make(model.id), providerID: ProviderV2.ID.make(model.provider) },
      content: [SessionMessage.AssistantText.make({ type: "text", id: "text-earlier", text: "Earlier answer" })],
      finish: "stop",
      time: { created, completed: created },
    }),
  },
  {
    seq: 2,
    message: SessionMessage.User.make({
      id: SessionMessage.ID.make("msg_current"),
      type: "user",
      text: "Current tool turn",
      time: { created },
    }),
  },
  {
    seq: 3,
    message: SessionMessage.Assistant.make({
      id: SessionMessage.ID.make("msg_tool_calls"),
      type: "assistant",
      agent: AgentV2.ID.make("build"),
      model: { id: ModelV2.ID.make(model.id), providerID: ProviderV2.ID.make(model.provider) },
      content: [],
      finish: "tool-calls",
      time: { created, completed: created },
    }),
  },
] as const

const makeCompaction = () => {
  const published: Array<{ readonly type: string; readonly data: unknown }> = []
  const requests: LLMRequest[] = []
  const events = EventV2.Service.of({
    publish: (definition, data) =>
      Effect.sync(() => {
        published.push({ type: definition.type, data })
        return { id: EventV2.ID.create(), type: definition.type, data } as EventV2.Payload<typeof definition>
      }),
    subscribe: () => Stream.empty,
    all: () => Stream.empty,
    durable: () => Stream.empty,
    listen: () => Effect.succeed(Effect.void),
    project: () => Effect.void,
    replay: () => Effect.void,
    replayAll: () => Effect.succeed(undefined),
    remove: () => Effect.void,
    claim: () => Effect.void,
  })
  const compaction = SessionCompaction.make({
    events,
    llm: {
      stream: (request) => {
        requests.push(request)
        return Stream.fromIterable([LLMEvent.textDelta({ id: "summary", text: "summary" })])
      },
    },
    config: [
      new Config.Document({
        type: "document",
        info: new Config.Info({
          compaction: new ConfigCompaction.Info({ keep: new ConfigCompaction.Keep({ turns: 0 }) }),
        }),
      }),
    ],
  })
  return { published, requests, compaction }
}

const request = LLM.request({
  model,
  messages: toLLMMessages(
    entries.map((entry) => entry.message),
    model,
  ),
  tools: [],
  generation: { maxTokens: 50 },
})

test("tool-call turns remain in retained compaction context", async () => {
  const fixture = makeCompaction()

  expect(await Effect.runPromise(fixture.compaction.compactAfterOverflow({ sessionID, entries, model, request }))).toBe(
    "compacted",
  )
  expect(fixture.requests).toHaveLength(1)
  expect(
    fixture.requests[0]?.messages.some(
      (message) =>
        message.role === "user" &&
        message.content.some((part) => part.type === "text" && part.text.includes("Current tool turn")),
    ),
  ).toBe(false)
  expect(fixture.published.at(-1)).toMatchObject({
    type: "session.next.compaction.ended",
    data: {
      recent: expect.stringContaining("[User]: Current tool turn"),
      retainedMessageIDs: [entries[2].message.id, entries[3].message.id],
    },
  })
})

test("does not call the summary provider when the exact summary request exceeds the context budget", async () => {
  const fixture = makeCompaction()
  const constrainedModel = Model.make({
    id: "compact-constrained",
    provider: "fake",
    route: OpenAIChat.route.with({ limits: { context: 1_000, output: 50 } }),
  })
  const constrainedEntries = [
    {
      ...entries[0],
      message: SessionMessage.User.make({
        ...entries[0].message,
        text: "Earlier oversized history ".repeat(400),
      }),
    },
    ...entries.slice(1),
  ]
  const constrainedRequest = LLM.request({
    model: constrainedModel,
    messages: toLLMMessages(
      constrainedEntries.map((entry) => entry.message),
      constrainedModel,
    ),
    tools: [],
    generation: { maxTokens: 50 },
  })

  expect(
    await Effect.runPromise(
      fixture.compaction.compactAfterOverflow({
        sessionID,
        entries: constrainedEntries,
        model: constrainedModel,
        request: constrainedRequest,
      }),
    ),
  ).toBe("cannot-fit")
  expect(fixture.requests).toHaveLength(0)
  expect(fixture.published).toHaveLength(0)
})

test("compaction start is durable before the overflow attempt transition", async () => {
  const fixture = makeCompaction()
  const exited = await Effect.runPromise(
    fixture.compaction
      .compactAfterOverflow({
        sessionID,
        entries,
        model,
        request,
        onStarted: Effect.die("simulated crash"),
      })
      .pipe(Effect.exit),
  )

  expect(exited._tag).toBe("Failure")
  expect(fixture.requests).toHaveLength(0)
  expect(fixture.published.map((event) => event.type)).toEqual(["session.next.compaction.started"])
})
