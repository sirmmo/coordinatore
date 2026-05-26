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

## Running it locally

```bash
# 1. Install
pip install -e .

# 2. Get a bot token from @BotFather on Telegram, export it
export TELEGRAM_BOT_TOKEN=123456:abc...

# 3. Run
coordinatore --games ./games --db ./data/coordinatore.sqlite

# 4. In Telegram: add the bot to a group, /open vespri-1282 full
```

## Running it in Docker

```bash
docker build -t coordinatore .
docker run --rm -it \
  -e TELEGRAM_BOT_TOKEN=123456:abc... \
  -v $(pwd)/data:/data \
  coordinatore
```

Mount a host volume on `/data` so the SQLite database survives restarts.
Drop your own `*.yaml` files into a directory and bind-mount it on
`/app/games` to add games without rebuilding the image.

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
       handlers       BotContext      Storage (sqlite)
       (per cmd)   (games, storage)     ▲
          │               │             │
          └──> Session ◄──┘             │
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
