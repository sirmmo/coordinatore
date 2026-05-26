"""Telegram command handlers.

Each handler is a thin shim that:
  1. fetches (or rejects-if-missing) the active session for this chat,
  2. mutates it through a `Session` method,
  3. persists, and
  4. replies.

All the rule-specific logic lives in the GameConfig + Session methods.
The bot itself is rule-agnostic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .session import Session

if TYPE_CHECKING:
    from .bot import BotContext

log = logging.getLogger(__name__)


# ---------- helpers ----------

def _bot_ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    """Pull the BotContext singleton out of the application's bot_data dict."""
    return context.application.bot_data["ctx"]


def _user_handle(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "(unknown)"
    return u.username or u.full_name or str(u.id)


async def _load_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Session | None:
    chat = update.effective_chat
    if chat is None:
        return None
    bctx = _bot_ctx(context)
    row = bctx.storage.active_for_chat(chat.id)
    if not row:
        return None
    config = bctx.games.get(row["game_id"])
    if not config:
        await update.effective_message.reply_text(
            f"⚠️ Sessione attiva ma il gioco `{row['game_id']}` non è più caricato.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return None
    return Session.from_row(row, config)


async def _require_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    require_status: str | tuple[str, ...] | None = None,
) -> Session | None:
    s = await _load_session(update, context)
    if s is None:
        await update.effective_message.reply_text(
            "❌ Nessuna sessione attiva in questa chat. Usa `/open <gioco>` per crearne una.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return None
    if require_status:
        wanted = (require_status,) if isinstance(require_status, str) else require_status
        if s.status not in wanted:
            await update.effective_message.reply_text(
                f"❌ Comando non disponibile in stato `{s.status}` (richiede: {', '.join(wanted)})",
                parse_mode=ParseMode.MARKDOWN,
            )
            return None
    return s


# ---------- commands ----------

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram-side /start — orienting message, not the session start."""
    await update.effective_message.reply_text(
        "👋 *Coordinatore* — bot per partite multi-tavolo.\n\n"
        "Comandi principali:\n"
        "`/games` — lista dei giochi caricati\n"
        "`/open <gioco> [variante]` — apri una sessione in questa chat\n"
        "`/join [fazione]` — entra come master\n"
        "`/begin` — avvia la sessione (parte il cronometro)\n"
        "`/score <fazione> <n>` — aggiorna un punteggio\n"
        "`/status` — punteggi e totale\n"
        "`/moment <id>` — pubblica un Momento di Congiunzione\n"
        "`/end` — chiudi la sessione\n"
        "`/help` — questo aiuto",
        parse_mode=ParseMode.MARKDOWN,
    )


cmd_help = cmd_start


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    games = _bot_ctx(context).games
    if not games:
        await update.effective_message.reply_text("Nessun gioco caricato.")
        return
    lines = ["*Giochi disponibili:*"]
    for g in games.values():
        lines.append(f"  • `{g.id}` — {g.name} ({g.language})")
        for v in g.variants:
            lines.append(f"      ↳ variante `{v.id}`: {v.label}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Uso: `/open <gioco-id> [variante-id]`", parse_mode=ParseMode.MARKDOWN
        )
        return
    bctx = _bot_ctx(context)
    game_id = args[0]
    config = bctx.games.get(game_id)
    if not config:
        await update.effective_message.reply_text(
            f"❌ Gioco `{game_id}` non trovato. Usa `/games`.", parse_mode=ParseMode.MARKDOWN
        )
        return
    variant_id = args[1] if len(args) > 1 else config.variants[0].id
    try:
        config.variant(variant_id)
    except KeyError:
        await update.effective_message.reply_text(
            f"❌ Variante `{variant_id}` non valida per `{game_id}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    chat = update.effective_chat
    if bctx.storage.active_for_chat(chat.id):
        await update.effective_message.reply_text(
            "⚠️ Una sessione è già attiva. Chiudila con `/end` prima di aprirne un'altra.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    bctx.storage.create_session(
        chat_id=chat.id,
        game_id=game_id,
        variant_id=variant_id,
        initial_state=Session.initial_state(config, variant_id),
    )
    variant = config.variant(variant_id)
    faction_labels = ", ".join(
        config.faction(fid).label for fid in variant.active_factions
    )
    msg = (
        f"📜 Sessione aperta: *{config.name}* — _{variant.label}_\n\n"
        f"{variant.description or ''}\n\n"
        f"Fazioni attive: {faction_labels}\n\n"
        f"I master entrino con `/join <fazione>`. Quando siete tutti, `/begin`."
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _require_session(update, context, require_status="opening")
    if not s:
        return
    args = context.args or []
    user = update.effective_user
    handle = _user_handle(update)
    faction_id = args[0] if args else None
    try:
        master = s.join(user.id, handle, faction_id)
    except (KeyError, ValueError) as exc:
        await update.effective_message.reply_text(f"❌ {exc}", parse_mode=ParseMode.MARKDOWN)
        return
    s.save(_bot_ctx(context).storage)
    if master.faction_id:
        f = s.config.faction(master.faction_id)
        await update.effective_message.reply_text(
            f"✅ @{handle} è il GM di *{f.label}*.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.effective_message.reply_text(
            f"✅ @{handle} è entrato. Usa `/join <fazione>` per claimarne una.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _require_session(update, context, require_status="opening")
    if not s:
        return
    if not s.masters:
        await update.effective_message.reply_text(
            "❌ Nessun master ha fatto `/join`. Almeno uno è necessario."
        )
        return
    s.start()
    s.save(_bot_ctx(context).storage)
    _bot_ctx(context).storage.mark_started(s.id)
    msg = [
        f"🔔 *La sessione inizia.* — _{s.config.name}_",
        "",
        f"Master ai tavoli ({len(s.masters)}):",
    ]
    for m in s.masters.values():
        if m.faction_id:
            f = s.config.faction(m.faction_id)
            msg.append(f"  • @{m.username} → {f.label}")
        else:
            msg.append(f"  • @{m.username} (senza fazione)")
    msg.append("")
    moments_with_time = [m for m in s.config.moments if m.suggested_minute is not None]
    if moments_with_time:
        msg.append("Momenti suggeriti:")
        for m in moments_with_time:
            msg.append(f"  ⏱  +{m.suggested_minute} min — {m.label}")
    await update.effective_message.reply_text("\n".join(msg), parse_mode=ParseMode.MARKDOWN)


async def cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _require_session(update, context, require_status=("opening", "running"))
    if not s:
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Uso: `/score <fazione> <n>`", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        s.set_score(args[0], int(args[1]))
    except (KeyError, ValueError) as exc:
        await update.effective_message.reply_text(f"❌ {exc}", parse_mode=ParseMode.MARKDOWN)
        return
    s.save(_bot_ctx(context).storage)
    await update.effective_message.reply_text(s.status_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _require_session(update, context)
    if not s:
        return
    await update.effective_message.reply_text(s.status_text(), parse_mode=ParseMode.MARKDOWN)


async def cmd_moment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _require_session(update, context, require_status="running")
    if not s:
        return
    args = context.args or []
    if not args:
        moment_list = ", ".join(f"`{m.id}`" for m in s.config.moments)
        await update.effective_message.reply_text(
            f"Uso: `/moment <id>` — disponibili: {moment_list}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    moment_id = args[0]
    try:
        moment = s.config.moment(moment_id)
    except KeyError:
        await update.effective_message.reply_text(
            f"❌ Momento `{moment_id}` sconosciuto.", parse_mode=ParseMode.MARKDOWN
        )
        return
    s.fire_moment(moment_id)
    s.save(_bot_ctx(context).storage)
    variant = s.config.variant(s.variant_id)
    lines = [
        f"🔔 *{moment.label}*",
        "",
        f"_{moment.description}_",
    ]
    if moment.per_faction_text:
        lines.append("")
        for fid in variant.active_factions:
            text = moment.per_faction_text.get(fid)
            if text:
                f = s.config.faction(fid)
                lines.append(f"*{f.label}* — {text}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = await _require_session(update, context)
    if not s:
        return
    s.end()
    s.save(_bot_ctx(context).storage)
    _bot_ctx(context).storage.mark_ended(s.id)
    outcome = s.outcome()
    lines = [
        f"📕 *Sessione chiusa* — _{s.config.name}_",
        "",
        s.status_text(),
    ]
    if outcome and outcome.description:
        lines.append("")
        lines.append(f"_{outcome.description}_")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------- custom (config-defined) commands ----------

async def cmd_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for commands declared in `custom_commands` of the loaded config.

    Pure documentation — echoes the label + description so everyone in the
    chat sees what just happened at a table. Does not mutate state.
    """
    s = await _require_session(update, context, require_status="running")
    if not s:
        return
    cmd_text = (update.effective_message.text or "").lstrip("/").split()[0].split("@")[0].lower()
    for cc in s.config.custom_commands:
        if cc.command.lower() == cmd_text:
            await update.effective_message.reply_text(
                f"⚙️ *{cc.label}* — {_user_handle(update)}\n\n_{cc.description}_",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    await update.effective_message.reply_text(
        f"❌ Comando custom `{cmd_text}` non definito per `{s.config.id}`.",
        parse_mode=ParseMode.MARKDOWN,
    )
