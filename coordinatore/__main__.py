"""Entry point for the long-running poller deployment.

Run with `python -m coordinatore` or the installed `coordinatore` script.
For the serverless (Vercel) deployment, see `api/telegram.py`.
"""

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
        "--storage-url",
        default=os.environ.get("STORAGE_URL", "file:./data/coordinatore.sqlite"),
        help=(
            "Storage URL. Examples: file:./data/x.sqlite, libsql://my-db.turso.io, "
            "https://my-db.turso.io (or env STORAGE_URL)."
        ),
    )
    ap.add_argument(
        "--storage-auth-token",
        default=os.environ.get("STORAGE_AUTH_TOKEN"),
        help=(
            "Auth token for remote storage (Turso); required for libsql:// "
            "(or env STORAGE_AUTH_TOKEN)"
        ),
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    if not args.token:
        ap.error("TELEGRAM_BOT_TOKEN is required (flag --token or env var)")

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = build_application(
        token=args.token,
        games_dir=args.games,
        storage_url=args.storage_url,
        storage_auth_token=args.storage_auth_token,
    )
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
