"""Game configuration schema.

A game config is a YAML file that declares everything a session needs:
factions, scoring ranges, outcome bands, conjunction moments, and any
custom commands the game ships. The bot is rule-agnostic — it loads
configs at startup and routes per-session state against the loaded
config, so one bot can host many concurrent sessions of different games.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class Faction(BaseModel):
    id: str
    label: str
    score_min: int = 0
    score_max: int = 4


class Variant(BaseModel):
    """A way to play a smaller-than-canonical configuration of the same game."""

    id: str
    label: str
    active_factions: list[str]
    description: str | None = None


class OutcomeBand(BaseModel):
    min: int
    max: int
    label: str
    description: str | None = None


class Moment(BaseModel):
    """A conjunction moment that the coordinator pushes to all tables.

    The bot can fire one on command (`/moment voce`) or — if `suggested_minute`
    is set — emit a reminder at that offset from session start.
    """

    id: str
    label: str
    description: str
    suggested_minute: int | None = None
    per_faction_text: dict[str, str] = Field(default_factory=dict)


class CustomCommand(BaseModel):
    """A game-specific extra command. The bot only echoes the description;
    the game still happens at the table — this is documentation that travels
    with the session."""

    command: str
    label: str
    description: str


class GameConfig(BaseModel):
    id: str
    name: str
    language: str = "en"
    description: str | None = None
    factions: list[Faction]
    variants: list[Variant]
    outcomes: dict[str, list[OutcomeBand]]
    moments: list[Moment]
    custom_commands: list[CustomCommand] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_cross_refs(self) -> GameConfig:
        faction_ids = {f.id for f in self.factions}
        for v in self.variants:
            missing = set(v.active_factions) - faction_ids
            if missing:
                raise ValueError(f"variant {v.id!r} references unknown factions: {missing}")
        for variant_id in self.outcomes:
            if variant_id not in {v.id for v in self.variants}:
                raise ValueError(f"outcomes block {variant_id!r} has no matching variant")
        for variant in self.variants:
            if variant.id not in self.outcomes:
                raise ValueError(f"variant {variant.id!r} has no outcomes block")
        for moment in self.moments:
            unknown = set(moment.per_faction_text) - faction_ids
            if unknown:
                raise ValueError(
                    f"moment {moment.id!r} per_faction_text references unknown: {unknown}"
                )
        return self

    def faction(self, faction_id: str) -> Faction:
        for f in self.factions:
            if f.id == faction_id:
                return f
        raise KeyError(faction_id)

    def variant(self, variant_id: str) -> Variant:
        for v in self.variants:
            if v.id == variant_id:
                return v
        raise KeyError(variant_id)

    def moment(self, moment_id: str) -> Moment:
        for m in self.moments:
            if m.id == moment_id:
                return m
        raise KeyError(moment_id)

    def outcome_for(self, variant_id: str, total: int) -> OutcomeBand | None:
        for band in self.outcomes.get(variant_id, []):
            if band.min <= total <= band.max:
                return band
        return None

    def max_total(self, variant_id: str) -> int:
        v = self.variant(variant_id)
        return sum(self.faction(fid).score_max for fid in v.active_factions)


def load_game(path: Path) -> GameConfig:
    """Parse a single game YAML."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return GameConfig.model_validate(data)


def load_games_dir(games_dir: Path) -> dict[str, GameConfig]:
    """Load every *.yaml file under games_dir, keyed by config id."""
    out: dict[str, GameConfig] = {}
    for p in sorted(games_dir.glob("*.yaml")):
        cfg = load_game(p)
        if cfg.id in out:
            raise ValueError(f"duplicate game id {cfg.id!r} in {p}")
        out[cfg.id] = cfg
    return out
