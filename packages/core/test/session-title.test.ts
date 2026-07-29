import { describe, expect } from "bun:test"
import { LLMClient, LLMEvent, Model, ProviderID, type LLMClientShape, type LLMRequest } from "@opencode-ai/llm"
import * as OpenAIChat from "@opencode-ai/llm/protocols/openai-chat"
import { AbsolutePath } from "@opencode-ai/core/schema"
import { AgentV2 } from "@opencode-ai/core/agent"
import { AppNodeBuilder } from "@opencode-ai/core/effect/app-node-builder"
import { LayerNodePlatform } from "@opencode-ai/core/effect/app-node-platform"
import { ModelV2 } from "@opencode-ai/core/model"
import { ProjectV2 } from "@opencode-ai/core/project"
import { ProviderV2 } from "@opencode-ai/core/provider"
import { SessionV2 } from "@opencode-ai/core/session"
import { SessionRunnerModel } from "@opencode-ai/core/session/runner/model"
import { SessionTitle } from "@opencode-ai/core/session/title"
import { DateTime, Effect, Layer, Stream } from "effect"
import { testEffect } from "./lib/effect"

const requests: LLMRequest[] = []
let response: LLMEvent[] = []
const model = Model.make({
  id: "profile",
  provider: "amazon-bedrock",
  route: OpenAIChat.route,
})
const client = Layer.succeed(
  LLMClient.Service,
  LLMClient.Service.of({
    prepare: () => Effect.die("unused"),
    stream: ((request: LLMRequest) => {
      requests.push(request)
      return Stream.fromIterable(response)
    }) as unknown as LLMClientShape["stream"],
    generate: () => Effect.die("unused"),
  }),
)
const it = testEffect(
  AppNodeBuilder.build(SessionTitle.node, [
    [SessionRunnerModel.node, SessionRunnerModel.layerWith(() => Effect.succeed(model))],
    [LayerNodePlatform.llmClient, client],
  ]),
)
const session = SessionV2.Info.make({
  id: SessionV2.ID.make("ses_title"),
  purpose: SessionV2.Purpose.make("terminal-chat"),
  projectID: ProjectV2.ID.global,
  title: "New session - now",
  agent: AgentV2.ID.make("chat"),
  model: {
    id: ModelV2.ID.make("opus"),
    providerID: ProviderV2.ID.make("amazon-bedrock"),
  },
  cost: 0,
  tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
  time: { created: DateTime.makeUnsafe(0), updated: DateTime.makeUnsafe(0) },
  location: { directory: AbsolutePath.make("/project") },
})

describe("SessionTitle", () => {
  it.effect("generates one bounded tool-free Bedrock title", () =>
    Effect.gen(function* () {
      requests.length = 0
      response = [
        LLMEvent.textStart({ id: "title" }),
        LLMEvent.textDelta({ id: "title", text: '<think>ignore</think>\n"Investigating API latency"' }),
        LLMEvent.textEnd({ id: "title" }),
      ]

      const title = yield* SessionTitle.Service.pipe(
        Effect.flatMap((service) =>
          service.generate({ session, user: "Why is this slow?", assistant: "The cache is cold." }),
        ),
      )

      expect(title).toBe("Investigating API latency")
      expect(requests).toHaveLength(1)
      expect(requests[0]?.tools).toEqual([])
      expect(requests[0]?.generation?.maxTokens).toBe(64)
      expect(requests[0]?.model.provider).toBe(ProviderID.make("amazon-bedrock"))
    }),
  )

  it.effect("uses a safe fallback after a definite provider failure", () =>
    Effect.gen(function* () {
      response = [LLMEvent.providerError({ message: "unavailable" })]

      const title = yield* SessionTitle.Service.pipe(
        Effect.flatMap((service) => service.generate({ session, user: "Hello", assistant: "Hi" })),
      )

      expect(title).toMatch(/^Chat \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
    }),
  )
})
