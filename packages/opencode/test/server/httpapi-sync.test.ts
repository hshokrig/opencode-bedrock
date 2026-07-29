import { afterEach, describe, expect, mock } from "bun:test"
import { LayerNode } from "@opencode-ai/core/effect/layer-node"
import { Context, Effect, Layer } from "effect"
import { Flag } from "@opencode-ai/core/flag/flag"
import { EventV2 } from "@opencode-ai/core/event"
import { SessionV1 } from "@opencode-ai/core/v1/session"
import { SessionV2 } from "@opencode-ai/core/session"
import { SessionEvent } from "@opencode-ai/schema/session-event"
import { SessionMessage } from "@opencode-ai/schema/session-message"
import { WorkspaceV2 } from "@opencode-ai/core/workspace"
import { SyncPaths } from "../../src/server/routes/instance/httpapi/groups/sync"
import { HttpApiApp } from "../../src/server/routes/instance/httpapi/server"
import { Session } from "@/session/session"
import { resetDatabase } from "../fixture/db"
import { disposeAllInstances, TestInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { httpApiLayer, requestInDirectory } from "./httpapi-layer"
import { withFixedWorkspaceID } from "../fixture/flag"

const originalWorkspaces = Flag.OPENCODE_EXPERIMENTAL_WORKSPACES
const context = Context.empty() as Context.Context<unknown>
const it = testEffect(Layer.mergeAll(LayerNode.compile(Session.node), httpApiLayer))

afterEach(async () => {
  mock.restore()
  Flag.OPENCODE_EXPERIMENTAL_WORKSPACES = originalWorkspaces
  await disposeAllInstances()
  await resetDatabase()
})

describe("sync HttpApi", () => {
  it.instance(
    "serves sync routes",
    () =>
      Effect.gen(function* () {
        Flag.OPENCODE_EXPERIMENTAL_WORKSPACES = true
        const tmp = yield* TestInstance
        const headers = { "x-opencode-directory": tmp.directory, "content-type": "application/json" }
        const session = yield* Session.use.create({ title: "sync" })

        const started = yield* requestInDirectory(SyncPaths.start, tmp.directory, { method: "POST", headers })
        expect(started.status).toBe(200)
        expect(yield* started.json).toBe(true)

        const history = yield* requestInDirectory(SyncPaths.history, tmp.directory, {
          method: "POST",
          headers,
          body: JSON.stringify({}),
        })
        expect(history.status).toBe(200)
        const rows = (yield* history.json) as Array<{
          id: string
          aggregate_id: string
          seq: number
          type: string
          data: Record<string, unknown>
        }>
        expect(rows.map((row) => row.aggregate_id)).toContain(session.id)

        const replayed = yield* requestInDirectory(SyncPaths.replay, tmp.directory, {
          method: "POST",
          headers,
          body: JSON.stringify({
            directory: tmp.directory,
            events: rows
              .filter((row) => row.aggregate_id === session.id)
              .map((row) => ({
                id: row.id,
                aggregateID: row.aggregate_id,
                seq: row.seq,
                type: row.type,
                data: row.data,
              })),
          }),
        })
        expect(replayed.status).toBe(200)
        expect(yield* replayed.json).toEqual({ sessionID: session.id })
      }),
    { git: true, config: { formatter: false, lsp: false } },
  )

  it.instance(
    "validates seq values",
    () =>
      Effect.gen(function* () {
        const tmp = yield* TestInstance
        const headers = { "x-opencode-directory": tmp.directory, "content-type": "application/json" }
        const cases = [
          {
            path: SyncPaths.history,
            body: { aggregate: -1 },
          },
          {
            path: SyncPaths.history,
            body: { aggregate: 1.5 },
          },
          {
            path: SyncPaths.replay,
            body: {
              directory: tmp.directory,
              events: [{ id: "event", aggregateID: "session", seq: -1, type: "session.created", data: {} }],
            },
          },
          {
            path: SyncPaths.replay,
            body: {
              directory: tmp.directory,
              events: [{ id: "event", aggregateID: "session", seq: 1.5, type: "session.created", data: {} }],
            },
          },
          {
            path: SyncPaths.replay,
            body: {
              directory: tmp.directory,
              events: [{ id: "event", aggregateID: "session", seq: 0, type: "session.created", data: {} }],
            },
          },
        ]

        for (const item of cases) {
          const response = yield* requestInDirectory(item.path, tmp.directory, {
            method: "POST",
            headers,
            body: JSON.stringify(item.body),
          })
          expect(response.status).toBe(400)
        }
      }),
    { git: true, config: { formatter: false, lsp: false } },
  )

  it.instance(
    "replays valid terminal histories and rejects events that violate terminal invariants",
    () =>
      Effect.gen(function* () {
        const tmp = yield* TestInstance
        const headers = { "x-opencode-directory": tmp.directory, "content-type": "application/json" }
        const created = yield* requestInDirectory("/api/session", tmp.directory, {
          method: "POST",
          headers,
          body: JSON.stringify({
            purpose: "terminal-chat",
            agent: "chat",
            model: { id: "opus", providerID: "amazon-bedrock" },
            location: { directory: tmp.directory },
          }),
        })
        const terminal = (yield* created.json) as { data: { id: string } }
        const history = yield* requestInDirectory(SyncPaths.history, tmp.directory, {
          method: "POST",
          headers,
          body: JSON.stringify({}),
        })
        const rows = (yield* history.json) as Array<{
          id: EventV2.ID
          aggregate_id: string
          seq: number
          type: string
          data: Record<string, unknown>
        }>
        const source = rows.find((row) => row.aggregate_id === terminal.data.id && row.seq === 0)
        expect(source).toBeDefined()
        if (!source) return
        const sourceInfo = source.data.info as Record<string, unknown>
        const replay = (events: EventV2.SerializedEvent[]) =>
          requestInDirectory(SyncPaths.replay, tmp.directory, {
            method: "POST",
            headers,
            body: JSON.stringify({ directory: tmp.directory, events }),
          })
        const creation = (sessionID: string, info: Record<string, unknown> = {}) => ({
          id: EventV2.ID.create(),
          aggregateID: sessionID,
          seq: 0,
          type: source.type,
          data: {
            ...source.data,
            sessionID,
            info: { ...sourceInfo, ...info, id: sessionID },
          },
        })

        const validID = SessionV2.ID.create()
        expect((yield* replay([creation(validID)])).status).toBe(200)

        const invalidID = SessionV2.ID.create()
        expect((yield* replay([creation(invalidID, { agent: "build" })])).status).toBe(400)

        const cases = [
          {
            type: EventV2.versionedType(SessionEvent.AgentSwitched.type, 1),
            data: {
              messageID: SessionMessage.ID.create(),
              timestamp: Date.now(),
              agent: "build",
            },
          },
          {
            type: EventV2.versionedType(SessionEvent.ModelSwitched.type, 1),
            data: {
              messageID: SessionMessage.ID.create(),
              timestamp: Date.now(),
              model: { id: "other", providerID: "amazon-bedrock" },
            },
          },
          {
            type: EventV2.versionedType(SessionEvent.Moved.type, 1),
            data: {
              timestamp: Date.now(),
              location: { directory: tmp.directory, workspaceID: "wrk_remote" },
            },
          },
          {
            type: EventV2.versionedType(SessionV1.Event.Updated.type, 1),
            data: {
              info: { ...sourceInfo, agent: "build" },
            },
          },
          {
            type: EventV2.versionedType(SessionV1.Event.Updated.type, 1),
            data: {
              info: { ...sourceInfo, directory: `${tmp.directory}/changed` },
            },
          },
          {
            type: EventV2.versionedType(SessionV1.Event.Updated.type, 1),
            data: {
              info: { ...sourceInfo, parentID: SessionV2.ID.create() },
            },
          },
        ]

        for (const item of cases) {
          const sessionID = SessionV2.ID.create()
          expect((yield* replay([creation(sessionID)])).status).toBe(200)
          const mutation = {
            id: EventV2.ID.create(),
            aggregateID: sessionID,
            seq: 1,
            type: item.type,
            data: { ...item.data, sessionID },
          }
          expect((yield* replay([mutation])).status).toBe(400)
        }

        const normalID = SessionV2.ID.create()
        expect(
          (yield* replay([
            creation(normalID, {
              purpose: undefined,
              agent: "build",
              model: undefined,
            }),
          ])).status,
        ).toBe(200)
        expect(
          (yield* replay([
            {
              id: EventV2.ID.create(),
              aggregateID: normalID,
              seq: 1,
              type: EventV2.versionedType(SessionV1.Event.Updated.type, 1),
              data: {
                sessionID: normalID,
                info: { ...sourceInfo, id: normalID },
              },
            },
          ])).status,
        ).toBe(400)
      }),
    { git: true, config: { formatter: false, lsp: false } },
  )

  it.instance(
    "rejects stealing a terminal session into a workspace",
    () =>
      Effect.gen(function* () {
        const tmp = yield* TestInstance
        const headers = { "x-opencode-directory": tmp.directory, "content-type": "application/json" }
        const created = yield* requestInDirectory("/api/session", tmp.directory, {
          method: "POST",
          headers,
          body: JSON.stringify({
            purpose: "terminal-chat",
            agent: "chat",
            model: { id: "opus", providerID: "amazon-bedrock" },
            location: { directory: tmp.directory },
          }),
        })
        const terminal = (yield* created.json) as { data: { id: string } }
        yield* withFixedWorkspaceID(WorkspaceV2.ID.ascending("wrk_terminal_steal"))

        const stolen = yield* requestInDirectory(SyncPaths.steal, tmp.directory, {
          method: "POST",
          headers,
          body: JSON.stringify({ sessionID: terminal.data.id }),
        })

        expect(stolen.status).toBe(503)
        expect(yield* stolen.json).toMatchObject({
          _tag: "ServiceUnavailableError",
          service: "session.move",
        })
      }),
    { git: true, config: { formatter: false, lsp: false } },
  )

  it.instance.skip(
    "returns structured validation errors",
    () =>
      Effect.gen(function* () {
        const tmp = yield* TestInstance
        const response = yield* Effect.promise(() =>
          HttpApiApp.webHandler().handler(
            new Request(`http://localhost${SyncPaths.history}`, {
              method: "POST",
              headers: { "x-opencode-directory": tmp.directory, "content-type": "application/json" },
              body: JSON.stringify({ aggregate: -1 }),
            }),
            context,
          ),
        )

        expect(response.status).toBe(400)
        expect(response.headers.get("content-type") ?? "").toContain("application/json")
        const body = (yield* Effect.promise(() => response.json())) as Record<string, unknown>
        expect(body.success).toBe(false)
        expect(Array.isArray(body.error) || Array.isArray(body.errors)).toBe(true)
      }),
    { git: true, config: { formatter: false, lsp: false } },
  )
})
