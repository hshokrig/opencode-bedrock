import { Deferred, Effect } from "effect"

export function makeSessionTitleCoordinator<A, E, R>() {
  const active = new Map<string, Deferred.Deferred<A, E>>()

  return (sessionID: string, firstMessageID: string, effect: Effect.Effect<A, E, R>): Effect.Effect<A, E, R> =>
    Effect.uninterruptibleMask((restore) => {
      const key = `${sessionID}\0${firstMessageID}`
      const current = active.get(key)
      if (current) return restore(Deferred.await(current))

      const deferred = Deferred.makeUnsafe<A, E>()
      active.set(key, deferred)
      return restore(effect).pipe(
        Effect.onExit((exit) =>
          Effect.sync(() => {
            if (active.get(key) === deferred) active.delete(key)
            Deferred.doneUnsafe(deferred, exit)
          }),
        ),
      )
    })
}
