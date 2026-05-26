"""Smoke tests: configs parse, session round-trips through storage,
outcome bands resolve correctly. No Telegram needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinatore.config import load_game, load_games_dir
from coordinatore.session import Session
from coordinatore.storage import Storage


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
    storage = Storage(tmp_path / "test.sqlite")

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
    assert s2.scores == {"congiurati": 3, "baroni": 0, "clero": 4, "popolo": 0}
    assert s2.total() == 7
    assert s2.outcome().label == "La Scintilla Spenta"
    assert s2.masters[1].faction_id == "congiurati"


def test_join_rejects_already_claimed_faction(tmp_path):
    cfg = load_game(VESPRI_PATH)
    storage = Storage(tmp_path / "test.sqlite")
    sid = storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="full",
        initial_state=Session.initial_state(cfg, "full"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)
    s.join(user_id=1, username="alice", faction_id="congiurati")
    with pytest.raises(ValueError, match="already held"):
        s.join(user_id=2, username="bob", faction_id="congiurati")


def test_join_rejects_inactive_faction_in_variant(tmp_path):
    cfg = load_game(VESPRI_PATH)
    storage = Storage(tmp_path / "test.sqlite")
    storage.create_session(
        chat_id=1, game_id=cfg.id, variant_id="no_baroni",
        initial_state=Session.initial_state(cfg, "no_baroni"),
    )
    s = Session.from_row(storage.active_for_chat(1), cfg)
    with pytest.raises(ValueError, match="not active"):
        s.join(user_id=1, username="alice", faction_id="baroni")
