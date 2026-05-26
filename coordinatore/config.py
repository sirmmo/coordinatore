"""Game configuration schema.

A game config is a YAML file that declares everything a session needs:
factions, scoring ranges, outcome bands, conjunction moments, and any
custom commands the game ships. The bot is rule-agnostic — it loads
configs at startup and routes per-session state against the loaded
config, so one bot can host many concurrent sessions of different games.

Scoring model
-------------

Each faction owns one or more **resources** (named score tracks):

* Vespri 1282 and Genova 1507 each have one resource per faction (the
  per-faction "score"). The legacy ``score_min`` / ``score_max`` fields
  on ``Faction`` synthesise a single resource called ``score``.
* Figures of a Future Past has three resources per faction (Alpha /
  Beta / Gamma QDP). Declared explicitly under ``resources``.

Outcome bands can either key on the **overall total** (sum across all
factions × all resources, the original behaviour) or on a **specific
resource's global sum** (e.g. "global Alpha ≥ 8"). The lookup returns
the first matching band — order them strategically.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class Resource(BaseModel):
    """A score track owned by a faction (e.g. "score", "alpha", "beta")."""

    id: str
    label: str
    min: int = 0
    max: int = 4


class Faction(BaseModel):
    id: str
    label: str

    # Legacy single-resource shorthand. If `resources` is not set, a
    # single resource called "score" is synthesised from these.
    score_min: int = 0
    score_max: int = 4

    # New explicit form for multi-resource factions.
    resources: list[Resource] | None = None

    @property
    def resolved_resources(self) -> list[Resource]:
        if self.resources is not None:
            return self.resources
        return [
            Resource(id="score", label="score", min=self.score_min, max=self.score_max)
        ]

    def resource(self, resource_id: str) -> Resource:
        for r in self.resolved_resources:
            if r.id == resource_id:
                return r
        raise KeyError(resource_id)

    @property
    def is_single_resource(self) -> bool:
        return self.resources is None or len(self.resources) == 1


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

    # If set, this band's threshold is checked against the GLOBAL sum of
    # the named resource only (summed across all factions). If unset, the
    # threshold is checked against the overall total (sum across all
    # factions × all resources) — the legacy behaviour.
    resource: str | None = None


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
        # Build the global set of resource ids across all factions, for
        # outcome.resource validation.
        all_resource_ids = {
            r.id for f in self.factions for r in f.resolved_resources
        }
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
        for variant_id, bands in self.outcomes.items():
            for band in bands:
                if band.resource is not None and band.resource not in all_resource_ids:
                    raise ValueError(
                        f"outcome band in {variant_id!r} references unknown "
                        f"resource {band.resource!r}"
                    )
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

    def max_total(self, variant_id: str, resource_id: str | None = None) -> int:
        """Maximum possible total for a variant.

        ``resource_id=None`` → sum every resource of every active faction.
        ``resource_id="alpha"`` → sum the "alpha" resource of every active
        faction (factions without that resource contribute 0).
        """
        v = self.variant(variant_id)
        total = 0
        for fid in v.active_factions:
            f = self.faction(fid)
            for r in f.resolved_resources:
                if resource_id is None or r.id == resource_id:
                    total += r.max
        return total

    def outcome_for(
        self,
        variant_id: str,
        total: int,
        *,
        scores: dict[str, dict[str, int]] | None = None,
    ) -> OutcomeBand | None:
        """Find the matching outcome band for the given totals.

        If a band has ``resource: X`` set, the lookup compares its
        threshold against the global sum of X (requires ``scores``).
        Bands without ``resource`` compare against the overall ``total``.

        Returns the first matching band; order them strategically.
        """
        for band in self.outcomes.get(variant_id, []):
            if band.resource is None:
                value = total
            else:
                if scores is None:
                    continue
                value = sum(per_f.get(band.resource, 0) for per_f in scores.values())
            if band.min <= value <= band.max:
                return band
        return None


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
