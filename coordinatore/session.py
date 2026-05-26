"""Session state and the small bit of business logic that operates on it.

A Session bundles a loaded GameConfig with the mutable per-session state
(scores, masters, log). Handlers in `commands/` instantiate one from the
DB row, mutate it, then save it back.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import GameConfig


@dataclass
class Master:
    """A GM who joined the session and (optionally) owns a faction's table."""

    user_id: int
    username: str
    faction_id: str | None = None


@dataclass
class Session:
    id: str
    chat_id: int
    config: GameConfig
    variant_id: str
    status: str = "opening"  # opening | running | ended
    started_at: float | None = None
    scores: dict[str, int] = field(default_factory=dict)
    masters: dict[int, Master] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    # ---- factory ----

    @classmethod
    def initial_state(cls, config: GameConfig, variant_id: str) -> dict:
        variant = config.variant(variant_id)
        scores = {fid: config.faction(fid).score_min for fid in variant.active_factions}
        return {"scores": scores, "masters": {}, "log": []}

    @classmethod
    def from_row(cls, row: dict, config: GameConfig) -> Session:
        state = row["state"]
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            config=config,
            variant_id=row["variant_id"],
            status=row["status"],
            started_at=row.get("started_at"),
            scores=dict(state.get("scores", {})),
            masters={
                int(uid): Master(**m) for uid, m in state.get("masters", {}).items()
            },
            log=list(state.get("log", [])),
        )

    # ---- serialisation ----

    def to_state(self) -> dict:
        return {
            "scores": self.scores,
            "masters": {
                str(uid): {"user_id": m.user_id, "username": m.username, "faction_id": m.faction_id}
                for uid, m in self.masters.items()
            },
            "log": self.log,
        }

    def save(self, storage: Any) -> None:
        """Persist mutable state. ``storage`` is duck-typed: SqliteStorage or
        LibSqlStorage both implement the same ``update_state`` method."""
        storage.update_state(self.id, self.to_state())

    # ---- mutators ----

    def join(self, user_id: int, username: str, faction_id: str | None = None) -> Master:
        if faction_id is not None:
            self.config.faction(faction_id)  # raises if unknown
            variant = self.config.variant(self.variant_id)
            if faction_id not in variant.active_factions:
                raise ValueError(
                    f"faction {faction_id!r} not active in variant {self.variant_id!r}"
                )
            existing = self._master_for_faction(faction_id)
            if existing and existing.user_id != user_id:
                raise ValueError(
                    f"faction {faction_id!r} is already held by @{existing.username}"
                )
        master = self.masters.get(user_id) or Master(user_id=user_id, username=username)
        master.username = username
        if faction_id is not None:
            master.faction_id = faction_id
        self.masters[user_id] = master
        self._record("join", user_id=user_id, faction=faction_id)
        return master

    def set_score(self, faction_id: str, value: int) -> None:
        if faction_id not in self.scores:
            raise ValueError(f"faction {faction_id!r} not active in this variant")
        f = self.config.faction(faction_id)
        if not (f.score_min <= value <= f.score_max):
            raise ValueError(f"score must be {f.score_min}..{f.score_max}")
        self.scores[faction_id] = value
        self._record("score", faction=faction_id, value=value)

    def total(self) -> int:
        return sum(self.scores.values())

    def outcome(self):
        return self.config.outcome_for(self.variant_id, self.total())

    def fire_moment(self, moment_id: str) -> None:
        self.config.moment(moment_id)
        self._record("moment", moment=moment_id)

    def start(self) -> None:
        self.status = "running"
        self.started_at = time.time()
        self._record("start")

    def end(self) -> None:
        self.status = "ended"
        outcome = self.outcome()
        self._record("end", total=self.total(), outcome=outcome.label if outcome else None)

    # ---- helpers ----

    def _master_for_faction(self, faction_id: str) -> Master | None:
        for m in self.masters.values():
            if m.faction_id == faction_id:
                return m
        return None

    def _record(self, kind: str, **payload: Any) -> None:
        """Append an event to the session log.

        Each entry gets a monotonic ``seq`` (per-session) and a ``t``
        timestamp. The stream endpoints use ``seq`` for "give me everything
        since X" cursors.
        """
        self.log.append({"seq": len(self.log), "t": time.time(), "kind": kind, **payload})

    def status_text(self) -> str:
        """A human-friendly status block to post back to the chat."""
        variant = self.config.variant(self.variant_id)
        lines = [
            f"*{self.config.name}* — variante: _{variant.label}_",
            f"Stato: `{self.status}`",
            "",
            "*Punteggi:*",
        ]
        for fid in variant.active_factions:
            f = self.config.faction(fid)
            score = self.scores.get(fid, f.score_min)
            owner = self._master_for_faction(fid)
            owner_tag = f" — @{owner.username}" if owner else ""
            lines.append(f"  • {f.label}: *{score}/{f.score_max}*{owner_tag}")
        total = self.total()
        max_total = self.config.max_total(self.variant_id)
        outcome = self.outcome()
        outcome_str = f" → {outcome.label}" if outcome else ""
        lines.append("")
        lines.append(f"*Totale:* {total}/{max_total}{outcome_str}")
        return "\n".join(lines)
