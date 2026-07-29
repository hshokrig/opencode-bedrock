import { Effect } from "effect"
import type { DatabaseMigration } from "../migration"

export default {
  id: "20260729184956_session_purpose",
  up(tx) {
    return Effect.gen(function* () {
      yield* tx.run(`ALTER TABLE \`session\` ADD \`purpose\` text;`)
      yield* tx.run(`CREATE INDEX \`session_purpose_idx\` ON \`session\` (\`purpose\`);`)
    })
  },
} satisfies DatabaseMigration.Migration
