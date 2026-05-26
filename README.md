# coordinatore

Telegram bot that runs the **shared/coordination layer for multi-table
tabletop sessions**. Built first to coordinate [Vespri 1282][vespri], but
rule-agnostic by design: a game is a YAML file in `games/`, and one bot
deployment can host many concurrent sessions of different games at once.

[vespri]: https://github.com/sirmmo/palermo_1282

## What it does

A session lives in one Telegram chat (typically a group with all the GMs).
Inside that chat the bot is the **single point of contact for the
coordinated layer**: who's in, what faction each master runs, what the
scores are, when the conjunction moments fire, and what the final outcome
band is.

The bot's binary is generic — it knows nothing about Sicily, the Vespers,
or any specific game. Per-session state lives in SQLite; game rules live
in YAML. That's the "stateless about rules" part.

## Commands

| Command | When | What |
|---|---|---|
| `/games` | any | list loaded game configs and their variants |
| `/open <game-id> [variant-id]` | no active session in chat | open a new session |
| `/join [faction-id]` | session opening | a GM enters and (optionally) claims a faction's table |
| `/begin` | session opening, ≥1 master | start the clock |
| `/score <faction-id> <n>` | session opening or running | set a faction's score |
| `/status` | any with session | current scores, total, current outcome band |
| `/moment <moment-id>` | session running | fire a Conjunction Moment — posts the configured text, including per-faction framing if defined |
| `/end` | any with session | close the session, post the final outcome |
| custom (`/legare`, `/sciogliere`, …) | session running | game-specific commands declared in the YAML — echo the configured label + description |

`/start` and `/help` show a short orienting message.

## Game configs

Each `*.yaml` in `games/` declares a game. The schema is in
[`coordinatore/config.py`](coordinatore/config.py); a worked example
shipped with the repo is [`games/vespri-1282.yaml`](games/vespri-1282.yaml).

Shape:

```yaml
id: my-game
name: "My Game"
language: en
factions:
  - { id: red,  label: "The Reds",  score_min: 0, score_max: 4 }
  - { id: blue, label: "The Blues", score_min: 0, score_max: 4 }
variants:
  - id: full
    label: "Two-table canonical"
    active_factions: [red, blue]
outcomes:
  full:
    - { min: 0, max: 3, label: "Catastrophe" }
    - { min: 4, max: 5, label: "Stalemate" }
    - { min: 6, max: 8, label: "Decisive win" }
moments:
  - id: act_one
    label: "End of Act One"
    suggested_minute: 60
    description: "Time to commit."
    per_faction_text:
      red: "Your scouts return with news."
      blue: "Your messenger never came back."
custom_commands:
  - command: parley
    label: "Parley"
    description: "Suspend hostilities for one scene."
```

Anything beyond the schema is ignored — extend the schema (and the bot
behaviour) by editing `coordinatore/config.py`.

## HTTP endpoints

Beyond the Telegram side, the bot exposes a small HTTP surface for an
auxiliary **panel** — a session monitor an organiser can keep on a screen
beside the GMs.

| Endpoint | Verb | What |
|---|---|---|
| `/api/telegram` | POST | Telegram webhook (in serverless mode) |
| `/api/sessions/{chat_id}` | GET | one-shot snapshot: scores, masters, log |
| `/api/stream/{chat_id}?since=N` | GET | **SSE** stream of new log events (`text/event-stream`); auto-reconnects via `EventSource` |
| `/api/ws/{chat_id}` | WS | **WebSocket** stream — same payloads as SSE; *does not work on Vercel* (no persistent connections), provided for self-hosted deploys |
| `/` | GET | static HTML panel (`public/panel.html`) — connects to the SSE feed and renders scores + live event list |

Each log entry carries a monotonic per-session `seq` so the panel can ask
"give me everything after N" on reconnect.

## Deploying — three paths

### A. Local development / quick test

```bash
pip install -e .
export TELEGRAM_BOT_TOKEN=123456:abc...
coordinatore --games ./games --storage-url file:./data/coordinatore.sqlite
# Then in Telegram: add the bot to a group, /open vespri-1282 full
```

This uses **long polling** — the bot keeps a connection open to Telegram.
No HTTP server runs, so the panel endpoints are not available in this mode.
Use it for quick checks; for actual play with the panel, use B or C.

### B. Self-hosted (Docker on a VPS / Fly.io / Raspberry Pi)

```bash
docker build -t coordinatore .
docker run -d --restart unless-stopped \
  -e TELEGRAM_BOT_TOKEN=123456:abc... \
  -v $(pwd)/data:/data \
  coordinatore
```

Mount a host volume on `/data` so the SQLite database survives restarts.
Drop your own `*.yaml` files into a directory and bind-mount it on
`/app/games` to add games without rebuilding the image.

To expose the **HTTP panel + WebSocket** alongside the polling bot, run
the FastAPI app separately with uvicorn:

```bash
pip install '.[serverless]'
uvicorn api.telegram:app --host 0.0.0.0 --port 8000
```

This serves the panel at `/`, SSE at `/api/stream/{chat_id}`, and a
true WebSocket at `/api/ws/{chat_id}`.

### C. Serverless (Vercel + Turso)

The hybrid deploy: Vercel hosts the webhook + panel, Turso hosts the
session database.

1. **Create a Turso database** and grab its libsql URL + an auth token.
2. **Push to Vercel**: `vercel --prod` (or hook the GitHub repo into
   Vercel). The `vercel.json` here routes `/api/*` and `/` to
   `api/telegram.py`.
3. **Configure env vars** in the Vercel project settings:
   - `TELEGRAM_BOT_TOKEN`
   - `STORAGE_URL` — e.g. `libsql://my-db-org.turso.io`
   - `STORAGE_AUTH_TOKEN`
   - `WEBHOOK_SECRET` (optional, recommended)
4. **Register the webhook** with Telegram:
   ```
   curl "https://api.telegram.org/bot$TOKEN/setWebhook?url=https://your.vercel.app/api/telegram&secret_token=$WEBHOOK_SECRET"
   ```
5. **Open the panel** at `https://your.vercel.app/?chat_id=-100...`.

Note: Vercel does **not** support persistent WebSockets on the standard
Python runtime. The panel uses SSE; the WebSocket endpoint will not
function on Vercel deployments (a `wss://` connection attempt times out).

## Setup on the Telegram side

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, get a token.
2. With BotFather: `/setprivacy` → **Disable**. Without this the bot only
   sees messages addressed to it (via `@botname` or replies), which breaks
   in-group coordination.
3. Add the bot to the group where the GMs are. Optionally `/setcommands`
   in BotFather and paste the command list from `commands.txt` so they
   autocomplete.

## Architecture

```
                       Telegram
                          │
                ┌─────────▼─────────┐
                │ python-telegram-  │
                │   bot Application │
                └─────────┬─────────┘
                          │
          ┌───────────────┼───────────────┐
          │               │               │
       handlers       BotContext      Storage (pluggable)
       (per cmd)   (games, storage)     ▲
          │               │             │  ┌─ SqliteStorage  (local file)
          └──> Session ◄──┘             ├──┤
                  │                     │  └─ LibSqlStorage  (Turso, remote)
                  │                     │
                  └─── load/save ───────┘
                  
            ┌─── GameConfig (YAML, pydantic)
            │      ↳ Faction, Variant, OutcomeBand,
            │        Moment, CustomCommand
```

One row in SQLite = one session. State (scores, masters, log) is stored
as JSON in that row. The bot loads it on demand per command, mutates,
saves. No locks: Telegram delivers messages serially per chat so concurrent
mutations on the same session are not a concern in practice.

## Status

Pre-1.0. Built for Vespri 1282's first run (Mensa Italia Games 2026), but
designed for whatever you want to drop into `games/`.

## Licence

AGPL-3.0-or-later. The bot itself is small; the value is in the game
configs. Share them.
