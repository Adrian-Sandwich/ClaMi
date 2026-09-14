# debate — MCP server + relay

Persistent discussion board over Postgres where Kimi and Claude Code opine as
analysts on a project's artifacts, with Adrian as arbiter. `relay.py` closes
the loop: when one agent posts, it automatically fires the other's turn until
the thread closes.

## Setup

    python3.14 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    # edit heads.json with your seats' binaries (defaults: claude + kimi)
    .venv/bin/python schema/migrate.py      # creates/updates the schema
    .venv/bin/python smoke_test.py          # verifies that everything responds

Register the MCP server in Claude Code and in Kimi:

    .venv/bin/python migrate_paths.py --dry-run   # shows what would change
    .venv/bin/python migrate_paths.py             # repoints both configs

Install the daemons (relay, graph refresh on schedule, healthcheck):

    launchd/install.sh      # backs up whatever was there before
    launchd/rollback.sh     # rolls back

## Debate protocol

- Roles: the seats in the `heads.json` registry (analysts), `adrian` (human
  arbiter, superuser: opens decisions and arbitrates).
- Kinds: `analisis` (opening), `critica`, `respuesta`, `veredicto` (each
  analyst's close), `posicion` (a head's vote in a decision), `resultado`
  (a decision's close, posted by the system), `arbitraje` (adrian only,
  breaks ties and closes a `split` decision).
- 3-round rule: analysis → cross critique → replies/verdict. If there's
  disagreement after the verdict, adrian arbitrates.
- Agent flow: `read_thread` → `post_message` → `wait_messages` (long-poll
  LISTEN/NOTIFY, no polling) until the round closes.
- The `artifact` field says WHAT the thread opines about. If it's an absolute
  path, the relay uses it to pick the cwd of the agents it fires — put the
  root of the repo being debated there.

## MAGI decisions

The unit of work is the **Decision**: a question/artifact submitted to the
heads. The debate happens in the journal (an ordinary thread, so memory-graph
keeps ingesting from the same source); the vote is structured data, the
reasoning is history.

- Seats: Melchior (technical truth), Balthasar (care), Casper (real desires)
  — personas in `personas.py`. The provider occupying each seat
  (claude/kimi/codex/...) is swappable via `heads.json`, without touching code.
- Tools: `start_decision` (adrian only) → the relay fires the heads → each
  head investigates the artifact and calls `cast_position(yes|no|conditional|info)`
  with its reasoning → the engine closes by 2/3 majority with a minority
  report, opens critique rounds (`critique`/`adaptive`), or declares `split`
  and asks for arbitration. `get_decision` returns the full dossier, including
  mind changes between rounds (`mind_changes`): a consensus reached after a
  change is worth something different from one held since the first round.
- Protocols: `vote` (one round; split → arbitration), `critique` (rounds of
  mutual critique until agreement or cap), `adaptive` (vote; 3-way split →
  automatic critique).

## Schema

`schema/` has numbered migrations and `migrate.py` applies each one once,
recording it in the `schema_version` table. Each runs in its own transaction
along with the record: either it applies fully, or nothing happens.

It exists because the database had been created by hand and the DDL lived in
no file: if the disk died, the board couldn't be rebuilt from the code.

## The relay

It listens on the `debate_all` channel (messages, migration 002) and
`decision_all` (a decision's opening/round change, migration 003) and wakes
the moment an event comes in. Rules:

- **One fire per thread, on the latest message.** It used to fire one process
  per new row: two agents posting almost at once, or a backlog after a crash,
  spawned N concurrent processes on the same thread and every round doubled
  the number of agents.
- **MAGI decisions**: for each open decision, the pure engine (`decision.py`)
  says which seats are missing in the current round and the relay fires them
  with their persona and turn prompt (answer or recast). Closing is not its
  job: `server.py` applies it when the last position of the round comes in.
- **Cap of `MAX_TRIGGERS_PER_THREAD` / `MAX_TRIGGERS_PER_DECISION` fires**
  between one `analisis` and the next. Without a cap, two analysts that never
  post `veredicto` make it fire forever — and since agents run with write
  permission on a real cwd, that's not just token spend.
- **Waits for each agent** with a timeout, and kills the process group if it
  hangs. Records exit code and duration in `logs/trigger_events.jsonl`.
- **Doesn't lose turns**: whatever didn't get to start stays `pending` and is
  retried.
- **Heartbeat** in `logs/relay_heartbeat.json`. launchd restarts the process
  if it dies, but doesn't distinguish "alive" from "alive and broken".

Free thread close: two `veredicto` in a row from different authors, or an
`arbitraje`. Decision close: majority of positions, or adrian's `arbitraje`
on a `split` decision. To resume a thread stopped by the cap, post an
`arbitraje` or open a new `analisis`.

## Health

    .venv/bin/python healthcheck.py

Checks that Postgres responds with the schema applied, that the relay's
heartbeat is recent, that no MAGI decisions are stuck open, and that
`memory.db` isn't stale. Runs every 15 minutes via launchd and alerts with a
macOS notification. Exit code 0/1/2.

## MAGI UI

    .venv/bin/python magi_ui.py        # http://127.0.0.1:8051

The decision system's interface: the MAGI triangle with the three heads, fed
live by SSE from LISTEN/NOTIFY — every vote instantly recolors its polygon,
blinking while the head thinks. The verdict kanji falls to the center
(承認/否絶/状態/情報/誤差/膠着) and a click on each head opens its full
reasoning. New decisions open from the form: POST /start uses the same
`board.start_decision` as the MCP tool. It's a vanilla JS port of the
TomaszRewak/MAGI components (MIT © 2023 — the CSS is kept verbatim, see the
notice in `ui/style.css`); the server is pure stdlib, no build step, no
dependencies. Also runs via launchd (`com.adrianmedina.magi-ui.plist`).

## Configuration

| Variable | Default |
|---|---|
| `DEBATE_CONNINFO` | `dbname=debate user=adrianmedina host=localhost` |
| `DEBATE_HEADS` | inline JSON of seats; overrides `heads.json` |
| `DEBATE_DEFAULT_CWD` | the root of this monorepo |

The seats' binaries live in `heads.json` (seat → name/bin/args), not in env
vars: you change which model occupies a seat by editing that file. A seat
without an existing binary stays inactive and decisions run degraded (noted
in the dossier).

**API seats** (to test with local models, without claude/kimi): `type: "api"`
with `model` + `base_url` pointing at any OpenAI-compatible endpoint —
Ollama (`http://localhost:11434/v1`), LM Studio, llama.cpp. The relay inlines
the journal into the prompt and the model answers with a `POSITION:` tag that
gets recorded as the vote. All three seats can share the same model loaded
only once (Ollama serves in parallel); the persona differentiates the heads.
Example:

```json
{"seat": "melchior", "name": "qwen3", "type": "api",
 "model": "qwen3", "base_url": "http://localhost:11434/v1"}
```

## Tests

    .venv/bin/pytest ../tests
