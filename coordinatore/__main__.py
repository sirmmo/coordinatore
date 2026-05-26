"""Entry point: `python -m coordinatore` or the installed `coordinatore` script."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .bot import build_application


def main() -> None:
    ap = argparse.ArgumentParser(prog="coordinatore", description=__doc__)
    ap.add_argument(
        "--token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN"),
        help="Telegram bot token (or env TELEGRAM_BOT_TOKEN)",
    )
    ap.add_argument(
        "--games",
        type=Path,
        default=Path(os.environ.get("GAMES_DIR", "games")),
        help="Directory containing *.yaml game configs",
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=Path(os.environ.get("DB_PATH", "data/coordinatore.sqlite")),
        help="SQLite database path",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    if not args.token:
        ap.error("TELEGRAM_BOT_TOKEN is required (flag --token or env var)")

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = build_application(args.token, args.games, args.db)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
