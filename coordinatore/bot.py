"""Telegram bot wiring.

Loads game configs at startup, builds the Application, registers handlers
(static + custom commands declared in configs), then runs polling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram.ext import Application, CommandHandler

from . import handlers
from .config import GameConfig, load_games_dir
from .storage import make_storage

log = logging.getLogger(__name__)


@dataclass
class BotContext:
    """Singleton dependencies passed to every handler via bot_data['ctx']."""

    games: dict[str, GameConfig]
    storage: Any  # SqliteStorage | LibSqlStorage — duck-typed


def build_application(
    token: str,
    games_dir: Path,
    storage_url: str,
    storage_auth_token: str | None = None,
) -> Application:
    games = load_games_dir(games_dir)
    log.info("loaded %d game configs: %s", len(games), ", ".join(games))

    storage = make_storage(storage_url, auth_token=storage_auth_token)
    ctx = BotContext(games=games, storage=storage)

    app = Application.builder().token(token).build()
    app.bot_data["ctx"] = ctx

    # Static handlers
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("games", handlers.cmd_games))
    app.add_handler(CommandHandler("open", handlers.cmd_open))
    app.add_handler(CommandHandler("join", handlers.cmd_join))
    app.add_handler(CommandHandler("begin", handlers.cmd_begin))
    app.add_handler(CommandHandler("score", handlers.cmd_score))
    app.add_handler(CommandHandler("status", handlers.cmd_status))
    app.add_handler(CommandHandler("moment", handlers.cmd_moment))
    app.add_handler(CommandHandler("end", handlers.cmd_end))

    # Custom commands aggregated across all configs.
    # Same command name across configs is fine: the handler resolves
    # against whichever game is loaded in the current session.
    custom_names: set[str] = set()
    for g in games.values():
        for cc in g.custom_commands:
            custom_names.add(cc.command.lower())
    for name in sorted(custom_names):
        app.add_handler(CommandHandler(name, handlers.cmd_custom))

    log.info(
        "registered %d custom commands: %s",
        len(custom_names),
        ", ".join(sorted(custom_names)),
    )
    return app
