"""Session state and the small bit of business logic that operates on it.

A Session bundles a loaded GameConfig with the mutable per-session state
(scores, masters, log). Handlers in `handlers.py` instantiate one from the
DB row, mutate it, then save it back.

Scores are stored nested per ``(faction_id, resource_id) -> value``:
single-resource factions (Vespri / Genova) still have one entry per faction
keyed under the synthesised resource name ``"score"``; multi-resource
factions (FOFP) have an entry per declared resource.
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
    # Nested: {faction_id: {resource_id: value}}.
    scores: dict[str, dict[str, int]] = field(default_factory=dict)
    masters: dict[int, Master] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)

    # ---- factory ----

    @classmethod
    def initial_state(cls, config: GameConfig, variant_id: str) -> dict:
        variant = config.variant(variant_id)
        scores: dict[str, dict[str, int]] = {}
        for fid in variant.active_factions:
            f = config.faction(fid)
            scores[fid] = {r.id: r.min for r in f.resolved_resources}
        return {"scores": scores, "masters": {}, "log": []}

    @classmethod
    def from_row(cls, row: dict, config: GameConfig) -> Session:
        state = row["state"]
        raw_scores = state.get("scores", {})
        # Tolerate legacy flat-dict shape from earlier sessions:
        #   {faction: int}  ->  {faction: {"score": int}}
        scores: dict[str, dict[str, int]] = {}
        for fid, val in raw_scores.items():
            if isinstance(val, dict):
                scores[fid] = dict(val)
            else:
                scores[fid] = {"score": int(val)}
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            config=config,
            variant_id=row["variant_id"],
            status=row["status"],
            started_at=row.get("started_at"),
            scores=scores,
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

    def set_score(
        self, faction_id: str, value: int, resource_id: str | None = None
    ) -> None:
        """Set a faction's score.

        For a single-resource faction (Vespri/Genova) ``resource_id`` can be
        omitted; it defaults to the only resource. For multi-resource factions
        (FOFP), pass the resource explicitly.
        """
        if faction_id not in self.scores:
            raise ValueError(f"faction {faction_id!r} not active in this variant")
        f = self.config.faction(faction_id)
        if resource_id is None:
            if not f.is_single_resource:
                raise ValueError(
                    f"faction {faction_id!r} has multiple resources "
                    f"({', '.join(r.id for r in f.resolved_resources)}); "
                    f"specify which one to set"
                )
            resource_id = f.resolved_resources[0].id
        r = f.resource(resource_id)  # raises if unknown
        if not (r.min <= value <= r.max):
            raise ValueError(
                f"resource {resource_id!r} of {faction_id!r} must be {r.min}..{r.max}"
            )
        self.scores[faction_id][resource_id] = value
        self._record("score", faction=faction_id, resource=resource_id, value=value)

    def total(self, resource_id: str | None = None) -> int:
        """Sum of scores across factions. With ``resource_id`` set, sum only
        that resource's values (factions without it contribute 0)."""
        total = 0
        for per_resource in self.scores.values():
            if resource_id is None:
                total += sum(per_resource.values())
            else:
                total += per_resource.get(resource_id, 0)
        return total

    def outcome(self):
        return self.config.outcome_for(
            self.variant_id, self.total(), scores=self.scores
        )

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
            owner = self._master_for_faction(fid)
            owner_tag = f" — @{owner.username}" if owner else ""
            if f.is_single_resource:
                r = f.resolved_resources[0]
                score = self.scores.get(fid, {}).get(r.id, r.min)
                lines.append(f"  • {f.label}: *{score}/{r.max}*{owner_tag}")
            else:
                lines.append(f"  • {f.label}{owner_tag}")
                for r in f.resolved_resources:
                    score = self.scores.get(fid, {}).get(r.id, r.min)
                    lines.append(f"      ↳ {r.label}: *{score}/{r.max}*")
        total = self.total()
        max_total = self.config.max_total(self.variant_id)
        outcome = self.outcome()
        outcome_str = f" → {outcome.label}" if outcome else ""
        lines.append("")
        lines.append(f"*Totale:* {total}/{max_total}{outcome_str}")
        return "\n".join(lines)
