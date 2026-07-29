export * as SessionTitle from "./title"

import { LLM, LLMClient, LLMEvent, Message } from "@opencode-ai/llm"
import { Context, Effect, Layer, Schema, Stream } from "effect"
import { makeLocationNode } from "../effect/app-node"
import { llmClient } from "../effect/app-node-platform"
import { SessionSchema } from "./schema"
import { SessionRunnerModel } from "./runner/model"

export class UnavailableError extends Schema.TaggedErrorClass<UnavailableError>()("SessionTitle.UnavailableError", {
  reason: Schema.String,
}) {}

export interface Interface {
  readonly generate: (input: {
    readonly session: SessionSchema.Info
    readonly user: string
    readonly assistant: string
  }) => Effect.Effect<string, UnavailableError>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/v2/SessionTitle") {}

const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const models = yield* SessionRunnerModel.Service
    const llm = yield* LLMClient.Service
    return Service.of({
      generate: Effect.fn("SessionTitle.generate")(function* (input) {
        if (input.session.model?.providerID !== "amazon-bedrock")
          return yield* new UnavailableError({ reason: "Session model is not Amazon Bedrock" })
        const model = yield* models
          .resolve(input.session)
          .pipe(Effect.catch(() => Effect.succeed(undefined)))
        if (!model || model.provider !== "amazon-bedrock")
          return `Chat ${new Date().toISOString().slice(0, 16).replace("T", " ")}`

        const chunks: string[] = []
        let providerFailed = false
        const generated = yield* llm
          .stream(
            LLM.request({
              model,
              messages: [
                Message.user(
                  [
                    "Create a concise title for this chat in at most 8 words.",
                    "Return only the title, without quotes, Markdown, or explanation.",
                    `<user>${input.user.slice(0, 8_000)}</user>`,
                    `<assistant>${input.assistant.slice(0, 8_000)}</assistant>`,
                  ].join("\n"),
                ),
              ],
              tools: [],
              generation: { maxTokens: 64 },
            }),
          )
          .pipe(
            Stream.runForEach((event) => {
              if (LLMEvent.is.providerError(event)) providerFailed = true
              if (LLMEvent.is.textDelta(event)) chunks.push(event.text)
              return Effect.void
            }),
            Effect.as(true),
            Effect.catch(() => Effect.succeed(false)),
          )
        const candidate = chunks
          .join("")
          .replace(/<think>[\s\S]*?<\/think>/gi, "")
          .trim()
          .split(/\r?\n/, 1)[0]
          .trim()
          .replace(/^["'`]+|["'`]+$/g, "")
          .slice(0, 100)
        if (generated && !providerFailed && candidate) return candidate
        return `Chat ${new Date().toISOString().slice(0, 16).replace("T", " ")}`
      }),
    })
  }),
)

export const node = makeLocationNode({
  service: Service,
  layer,
  deps: [SessionRunnerModel.node, llmClient],
})
