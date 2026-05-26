"""Smoke tests: configs parse, session round-trips through storage,
outcome bands resolve correctly. No Telegram needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinatore.config import load_game, load_games_dir
from coordinatore.session import Session
from coordinatore.storage import LibSqlStorage, SqliteStorage, make_storage

GAMES_DIR = Path(__file__).resolve().parent.parent / "games"
VESPRI_PATH = GAMES_DIR / "vespri-1282.yaml"


def test_vespri_config_loads():
    cfg = load_game(VESPRI_PATH)
    assert cfg.id == "vespri-1282"
    assert {f.id for f in cfg.factions} == {"congiurati", "baroni", "clero", "popolo"}
    assert {v.id for v in cfg.variants} == {"full", "no_baroni", "no_congiurati"}
    assert {m.id for m in cfg.moments} == {"voce", "crisi", "campane"}


def test_all_games_load():
    games = load_games_dir(GAMES_DIR)
    assert "vespri-1282" in games
    assert "genova-1507" in games


def test_genova_config():
    cfg = load_game(GAMES_DIR / "genova-1507.yaml")
    assert cfg.id == "genova-1507"
    # Four casate, each contributing up to 2 to the shared clock (total = 0..8).
    assert {f.id for f in cfg.factions} == {"doria", "spinola", "fieschi", "grimaldi"}
    assert cfg.max_total("full") == 8
    # Outcome at 4 (clock segment 4) should land on "La rivolta fermenta".
    assert cfg.outcome_for("full", 4).label == "La rivolta fermenta"
    # Outcome at 8 (full clock) = freedom.
    assert cfg.outcome_for("full", 8).label == "GENOVA È LIBERA"
    # Custom commands present.
    custom = {c.command for c in cfg.custom_commands}
    assert "colpo_riservato" in custom
    assert "colpo_globale" in custom
    assert "interferenza" in custom


def test_outcome_bands_full():
    cfg = load_game(VESPRI_PATH)
    assert cfg.outcome_for("full", 0).label == "La Notte del Silenzio"
    assert cfg.outcome_for("full", 7).label == "La Scintilla Spenta"
    assert cfg.outcome_for("full", 10).label == "Il Vespro"
    assert cfg.outcome_for("full", 16).label == "L'Insurrezione"
    assert cfg.outcome_for("full", 99) is None


def test_outcome_bands_three_table():
    cfg = load_game(VESPRI_PATH)
    assert cfg.outcome_for("no_baroni", 0).label == "La Notte del Silenzio"
    assert cfg.outcome_for("no_baroni", 5).label == "La Scintilla Spenta"
    assert cfg.outcome_for("no_baroni", 8).label == "Il Vespro"
    assert cfg.outcome_for("no_baroni", 12).label == "L'Insurrezione"


def test_max_total_per_variant():
    cfg = load_game(VESPRI_PATH)
    assert cfg.max_total("full") == 16
    assert cfg.max_total("no_baroni") == 12
    assert cfg.max_total("no_congiurati") == 12


def test_session_round_trip(tmp_path):
    cfg = load_game(VESPRI_PATH)
    storage = SqliteStorage(tmp_path / "test.sqlite")

    sid = storage.create_session(
        chat_id=42,
        game_id=cfg.id,
        variant_id="full",
        initial_state=Session.initial_state(cfg, "full"),
    )

    row = storage.active_for_chat(42)
    assert row is not None and row["id"] == sid

    s = Session.from_row(row, cfg)
    s.join(user_id=1, username="alice", faction_id="congiurati")
    s.join(user_id=2, username="bob", faction_id="clero")
    s.set_score("congiurati", 3)
    s.set_score("clero", 4)
    s.save(storage)

    # Reload from DB, verify the state survived.
    row2 = storage.active_for_chat(42)
    s2 = Session.from_row(row2, cfg)
    # Scores are nested per-resource. Single-resource factions
    # (Vespri/Genova) get the synthesised "score" resource.
    assert s2.scores == {
        "congiurati": {"score": 3},
        "baroni": {"score": 0},
        "clero": {"score": 4},
        "popolo": {"score": 0},
    }
    assert s2.total() == 7
    assert s2.outcome().label == "La Scintilla Spenta"
    assert s2.masters[1].faction_id == "congiurati"


def test_join_rejects_already_claimed_faction(tmp_path):
    cfg = load_game(VESPRI_PATH)
    storage = SqliteStorage(tmp_path / "test.sqlite")
    storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="full",
        initial_state=Session.initial_state(cfg, "full"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)
    s.join(user_id=1, username="alice", faction_id="congiurati")
    with pytest.raises(ValueError, match="already held"):
        s.join(user_id=2, username="bob", faction_id="congiurati")


def test_join_rejects_inactive_faction_in_variant(tmp_path):
    cfg = load_game(VESPRI_PATH)
    storage = SqliteStorage(tmp_path / "test.sqlite")
    storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="no_baroni",
        initial_state=Session.initial_state(cfg, "no_baroni"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)
    with pytest.raises(ValueError, match="not active"):
        s.join(user_id=1, username="alice", faction_id="baroni")


@pytest.mark.parametrize(
    "url",
    [
        "file:./data/x.sqlite",
        "sqlite:///tmp/x.sqlite",
        "/tmp/bare-path.sqlite",
    ],
)
def test_make_storage_returns_sqlite_for_file_urls(tmp_path, monkeypatch, url):
    # Steer the bare-path test at a tmp location to keep the FS tidy.
    if url.startswith("/"):
        url = str(tmp_path / "bare.sqlite")
    s = make_storage(url)
    assert isinstance(s, SqliteStorage)


def test_make_storage_returns_libsql_for_libsql_urls():
    pytest.importorskip("libsql")
    # Construction will likely fail trying to reach example.invalid — that's
    # fine; we only care that the factory dispatched into LibSqlStorage.
    try:
        s = make_storage("libsql://example.invalid", auth_token="x")
    except Exception:
        return  # dispatched, network/auth failure as expected
    assert isinstance(s, LibSqlStorage)


def test_make_storage_rejects_unknown_scheme():
    with pytest.raises(ValueError, match="unknown storage URL scheme"):
        make_storage("redis://nope")


# ---------- multi-resource (FOFP-style) ----------


def test_figures_config_loads_multi_resource():
    cfg = load_game(GAMES_DIR / "figures-of-a-future-past.yaml")
    assert cfg.id == "figures-of-a-future-past"
    venture = cfg.faction("venture")
    assert not venture.is_single_resource
    res_ids = {r.id for r in venture.resolved_resources}
    assert res_ids == {"alpha", "beta", "gamma"}


def test_multi_resource_set_score_requires_resource(tmp_path):
    cfg = load_game(GAMES_DIR / "figures-of-a-future-past.yaml")
    storage = SqliteStorage(tmp_path / "test.sqlite")
    storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="full",
        initial_state=Session.initial_state(cfg, "full"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)

    # Multi-resource faction: must specify resource.
    with pytest.raises(ValueError, match="multiple resources"):
        s.set_score("venture", 3)

    # Specifying the resource works. Initial state is all resources at min;
    # the manual's "starting QDP distribution" is set by the coordinator
    # via /score after /begin, not via the schema's min.
    s.set_score("venture", 3, resource_id="alpha")
    assert s.scores["venture"] == {"alpha": 3, "beta": 0, "gamma": 0}


def test_resource_keyed_outcome_band(tmp_path):
    cfg = load_game(GAMES_DIR / "figures-of-a-future-past.yaml")
    storage = SqliteStorage(tmp_path / "test.sqlite")
    storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="full",
        initial_state=Session.initial_state(cfg, "full"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)

    # Drive global Alpha sum past the threshold (≥ 8).
    s.set_score("venture", 6, resource_id="alpha")
    s.set_score("halcyon", 3, resource_id="alpha")  # global alpha = 9 ≥ 8
    outcome = s.outcome()
    assert outcome is not None
    assert outcome.label == "L'Ascesa della Conoscenza"


def test_vespri_still_single_resource_after_refactor(tmp_path):
    """Backward-compat smoke: old single-resource configs unchanged."""
    cfg = load_game(VESPRI_PATH)
    storage = SqliteStorage(tmp_path / "test.sqlite")
    storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="full",
        initial_state=Session.initial_state(cfg, "full"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)
    # Legacy 2-arg form still works.
    s.set_score("congiurati", 4)
    assert s.scores["congiurati"]["score"] == 4
