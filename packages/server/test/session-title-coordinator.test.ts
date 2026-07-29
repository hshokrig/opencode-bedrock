import { describe, expect, it } from "bun:test"
import { Deferred, Effect, Fiber } from "effect"
import { makeSessionTitleCoordinator } from "../src/session-title-coordinator"

describe("makeSessionTitleCoordinator", () => {
  it("coalesces concurrent requests per session and permits a later request", async () => {
    let calls = 0
    const result = await Effect.runPromise(
      Effect.gen(function* () {
        const run = makeSessionTitleCoordinator<string, Error, never>()
        const started = yield* Deferred.make<void>()
        const release = yield* Deferred.make<void>()
        const operation = Effect.sync(() => {
          calls += 1
        }).pipe(
          Effect.andThen(Deferred.succeed(started, undefined)),
          Effect.andThen(Deferred.await(release)),
          Effect.as("Generated title"),
        )
        const first = yield* Effect.forkChild(run("session", "first", operation))
        yield* Deferred.await(started)
        const second = yield* Effect.forkChild(run("session", "first", operation))
        yield* Effect.yieldNow
        expect(calls).toBe(1)
        yield* Deferred.succeed(release, undefined)
        const titles = yield* Effect.all([Fiber.join(first), Fiber.join(second)])
        const later = yield* run(
          "session",
          "first",
          Effect.sync(() => {
            calls += 1
            return "Stored title"
          }),
        )
        return { titles, later }
      }).pipe(Effect.scoped),
    )

    expect(result.titles).toEqual(["Generated title", "Generated title"])
    expect(result.later).toBe("Stored title")
    expect(calls).toBe(2)
  })

  for (const order of [
    ["first", "second"],
    ["second", "first"],
  ] as const) {
    it(`does not coalesce differing first messages in ${order.join("-then-")} order`, async () => {
      const calls: string[] = []
      const result = await Effect.runPromise(
        Effect.gen(function* () {
          const run = makeSessionTitleCoordinator<string, never, never>()
          const started = yield* Deferred.make<void>()
          const release = yield* Deferred.make<void>()
          const active = run(
            "session",
            order[0],
            Effect.sync(() => {
              calls.push(order[0])
            }).pipe(
              Effect.andThen(Deferred.succeed(started, undefined)),
              Effect.andThen(Deferred.await(release)),
              Effect.as(order[0]),
            ),
          )
          const first = yield* Effect.forkChild(active)
          yield* Deferred.await(started)
          const second = yield* Effect.forkChild(
            run(
              "session",
              order[1],
              Effect.sync(() => {
                calls.push(order[1])
                return order[1]
              }),
            ),
          )
          expect(yield* Fiber.join(second)).toBe(order[1])
          yield* Deferred.succeed(release, undefined)
          expect(yield* Fiber.join(first)).toBe(order[0])
        }).pipe(Effect.scoped),
      )

      expect(result).toBeUndefined()
      expect(calls).toEqual([...order])
    })
  }
})
