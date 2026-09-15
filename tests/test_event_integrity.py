"""Event and player integrity.

Every test here is an adversarial case: a wrong event, a finished event, two
meetings between the same teams, a cache that could bleed between events, and
players who cannot be shown to belong to the matchup.
"""
import time

import pytest

from edgeterminal import db
from edgeterminal import integrity as I

NOW = 1_800_000_000.0                    # 2027-01-15T08:00Z
FUTURE = "2027-01-16T18:00:00Z"          # tomorrow
FUTURE_LATER = "2027-01-21T18:00:00Z"    # same teams, a week later
PAST = "2026-09-15T00:15:00Z"            # finished


def _stored(event_id="evt-1", home="Kansas City Chiefs", away="Denver Broncos",
            start=FUTURE, status="scheduled", sport="NFL"):
    return dict(id=event_id, sport=sport, home_team=home, away_team=away,
                start_time=start, status=status, name=f"{away} @ {home}")


def _provider(event_id="prov-abc", home="Kansas City Chiefs", away="Denver Broncos",
              commence=FUTURE):
    return dict(id=event_id, sport_key="americanfootball_nfl", sport_title="NFL",
                home_team=home, away_team=away, commence_time=commence)


# ------------------------------------------------------------------ event match
def test_identity_accepts_a_matching_provider_payload():
    verdict = I.validate_provider_event(_provider(), _stored(), now=NOW)
    assert verdict["state"] == I.IDENTITY_OK
    assert verdict["provider_id"] == "prov-abc"
    assert verdict["reasons"] == []


def test_identity_rejects_a_different_matchup():
    wrong = _provider(home="Buffalo Bills", away="Miami Dolphins")
    verdict = I.validate_provider_event(wrong, _stored(), now=NOW)
    assert verdict["state"] == I.IDENTITY_MISMATCH
    assert any("team" in r.lower() for r in verdict["reasons"])


def test_identity_rejects_a_home_away_reversal():
    flipped = _provider(home="Denver Broncos", away="Kansas City Chiefs")
    verdict = I.validate_provider_event(flipped, _stored(), now=NOW)
    assert verdict["state"] == I.IDENTITY_MISMATCH


def test_identity_rejects_a_wrong_kickoff_for_the_same_teams():
    other_date = _provider(commence=FUTURE_LATER)
    verdict = I.validate_provider_event(other_date, _stored(), now=NOW)
    assert verdict["state"] == I.IDENTITY_MISMATCH
    assert any("kickoff" in r.lower() for r in verdict["reasons"])


def test_identity_flags_a_finished_event_as_expired():
    past = _stored(start=PAST)
    verdict = I.validate_provider_event(_provider(commence=PAST), past, now=NOW)
    assert verdict["state"] == I.IDENTITY_EXPIRED


def test_identity_rejects_a_non_nfl_payload():
    verdict = I.validate_provider_event(_provider(event_id="prov-x", home="Boston Celtics",
                                                  away="New York Knicks"), _stored(), now=NOW)
    assert verdict["state"] in (I.IDENTITY_MISMATCH, I.IDENTITY_EXPIRED)


def test_matching_never_uses_a_team_name_substring():
    candidates = [_provider(event_id="p1", home="Kansas City Chiefs", away="Denver Broncos",
                            commence=FUTURE),
                  _provider(event_id="p2", home="Los Angeles Rams", away="Denver Broncos",
                            commence=FUTURE)]
    verdict = I.match_provider_event(candidates, _stored(), now=NOW)
    assert verdict["state"] == I.IDENTITY_OK
    assert verdict["provider_id"] == "p1"


def test_repeated_matchups_resolve_to_the_nearest_kickoff():
    stored = _stored(start=FUTURE_LATER)
    candidates = [_provider(event_id="last-month", commence=PAST),
                  _provider(event_id="this-week", commence=FUTURE_LATER)]
    verdict = I.match_provider_event(candidates, stored, now=NOW)
    assert verdict["state"] == I.IDENTITY_OK
    assert verdict["provider_id"] == "this-week"


def test_ambiguous_kickoffs_are_refused_rather_than_guessed():
    stored = _stored(start=FUTURE)
    candidates = [_provider(event_id="a", commence=FUTURE),
                  _provider(event_id="b", commence="2027-01-16T18:55:00Z")]
    verdict = I.match_provider_event(candidates, stored, now=NOW, tolerance_s=7200)
    assert verdict["state"] == I.IDENTITY_AMBIGUOUS
    assert verdict["provider_id"] is None
    assert len(verdict["alternatives"]) == 2


def test_no_candidate_is_unavailable_not_a_substitute():
    verdict = I.match_provider_event([_provider(event_id="other",
                                                home="Dallas Cowboys", away="New York Giants")],
                                     _stored(), now=NOW)
    assert verdict["state"] == I.IDENTITY_UNAVAILABLE
    assert verdict["provider_id"] is None


def test_an_expired_event_is_never_substituted_with_the_next_meeting():
    stored = _stored(start=PAST)                            # finished
    candidates = [_provider(event_id="next", commence=FUTURE)]
    verdict = I.match_provider_event(candidates, stored, now=NOW)
    assert verdict["state"] == I.IDENTITY_EXPIRED
    assert verdict["provider_id"] is None


# ------------------------------------------------------------------ cache keys
def test_prop_cache_keys_are_event_scoped_and_versioned():
    a = I.prop_cache_key("prov-1", _stored(event_id="evt-a"), ["player_anytime_td"])
    b = I.prop_cache_key("prov-1", _stored(event_id="evt-b"), ["player_anytime_td"])
    assert a != b
    assert f"v{I.CACHE_VERSION}" in a
    assert a.startswith("props:")
    # market order must not create duplicate entries
    assert I.prop_cache_key("prov-1", _stored(event_id="evt-a"),
                            ["player_1st_td", "player_anytime_td"]) == \
        I.prop_cache_key("prov-1", _stored(event_id="evt-a"),
                         ["player_anytime_td", "player_1st_td"])
    # a different provider event id can never share a cache entry
    assert I.prop_cache_key("prov-2", _stored(event_id="evt-a"), ["player_anytime_td"]) != a


def test_a_reassigned_provider_id_does_not_reuse_another_events_payload():
    stored_a = _stored(event_id="evt-a", start=FUTURE)
    stored_b = _stored(event_id="evt-b", home="Detroit Lions", away="Buffalo Bills",
                       start=FUTURE)
    assert I.prop_cache_key("prov-1", stored_a, ["player_anytime_td"]) != \
        I.prop_cache_key("prov-1", stored_b, ["player_anytime_td"])


# ------------------------------------------------------------------ players
def _usage(team):
    return dict(name="X", team=team, games=10, touches=100.0)


def test_participant_mismatch_and_unresolved_are_distinguished():
    event = _stored()
    ok = I.classify_player("Bo Nix", [dict(name="B.Nix", team="Denver Broncos", games=17)],
                           event)
    other = I.classify_player("Jaylen Waddle", [dict(name="J.Waddle", team="Miami Dolphins",
                                                     games=16)], event)
    unknown = I.classify_player("Franklin Gore Jr.", [], event)
    assert ok["state"] == I.PARTICIPANT
    assert other["state"] == I.MISMATCH
    assert other["team"] == "Miami Dolphins"
    assert unknown["state"] == I.UNRESOLVED


def test_an_ambiguous_name_with_two_candidates_is_unresolved():
    rows = [dict(name="J.Williams", team="Los Angeles Rams", games=12, touches=60.0),
            dict(name="J.Williams", team="Seattle Seahawks", games=3, touches=5.0)]
    verdict = I.classify_player("Jameson Williams", rows,
                                _stored(home="Detroit Lions", away="Buffalo Bills"))
    assert verdict["state"] == I.UNRESOLVED
    assert verdict["alternatives"] == 2


def test_event_team_evidence_decides_identity_but_a_bigger_elsewhere_match_refuses():
    event = _stored(home="Detroit Lions", away="Buffalo Bills")
    on_team = [dict(name="J.Williams", team="Detroit Lions", games=12, touches=60.0),
               dict(name="J.Williams", team="Los Angeles Rams", games=3, touches=5.0)]
    verdict = I.classify_player("Jameson Williams", on_team, event)
    assert verdict["state"] == I.PARTICIPANT and verdict["team"] == "Detroit Lions"

    elsewhere_bigger = [dict(name="J.Williams", team="Detroit Lions", games=3, touches=5.0),
                        dict(name="J.Williams", team="Los Angeles Rams", games=16, touches=200.0)]
    verdict = I.classify_player("Jameson Williams", elsewhere_bigger, event)
    assert verdict["state"] == I.UNRESOLVED


def test_team_units_are_participants_not_players():
    verdict = I.classify_player("Buffalo Bills D/ST", [], _stored(home="Buffalo Bills",
                                                                 away="Detroit Lions"))
    assert verdict["state"] == I.PARTICIPANT
    assert verdict["kind"] == "team_unit"


def test_the_same_player_in_two_seasons_is_not_an_ambiguity():
    """One person appearing in both seasons' usage is still one person."""
    event = _stored(home="Kansas City Chiefs", away="Denver Broncos")
    rows = [dict(name="B.Nix", key="00-0039123", team="Denver Broncos", games=17, touches=83.0),
            dict(name="B.Nix", key="00-0039123", team="Denver Broncos", games=6, touches=20.0)]
    verdict = I.classify_player("Bo Nix", rows, event)
    assert verdict["state"] == I.PARTICIPANT and verdict["team"] == "Denver Broncos"
    assert verdict["alternatives"] == 1


def test_unresolved_players_are_excluded_from_modeling():
    event = _stored()
    verdict = I.classify_player("Kenneth Walker III",
                                [dict(name="K.Walker", team="Seattle Seahawks", games=17)],
                                event)
    assert not I.is_model_eligible(verdict)
    assert I.is_model_eligible(I.classify_player(
        "Bo Nix", [dict(name="B.Nix", team="Denver Broncos", games=17)], event))


# ------------------------------------------------------------------ quarantine
@pytest.fixture()
def scoped_db(tmp_path, monkeypatch):
    from edgeterminal.config import settings
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'q.db'}")
    db.init()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def test_quarantined_odds_are_excluded_from_analysis_reads(scoped_db):
    db.ex("INSERT OR REPLACE INTO events(id,sport,league,name,home_team,away_team,start_time,"
          "venue,status,meta,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         ("evt-q", "NFL", "NFL", "Denver Broncos @ Kansas City Chiefs",
          "Kansas City Chiefs", "Denver Broncos", "2026-09-15T00:15:00Z", None, "scheduled",
          "{}", NOW))
    db.store_odds([dict(event_id="evt-q", sport="NFL", book="fanduel", market="spread",
                        selection="Kansas City Chiefs", line=-3.0, price=-110,
                        captured_at=NOW, source="test")], source="test")
    before = db.latest_odds(event_id="evt-q")
    assert len(before) == 1
    db.quarantine("odds", str(before[0]["id"]), "evt-q", "wrong matchup", detail={"why": "test"})
    assert db.latest_odds(event_id="evt-q") == []
    raw = db.latest_odds(event_id="evt-q", exclude_quarantined=False)
    assert len(raw) == 1
    assert db.is_quarantined("odds", str(before[0]["id"])) is True


def test_quarantine_is_idempotent_and_records_reasons(scoped_db):
    db.quarantine("odds", "42", "evt-q", "mismatch")
    db.quarantine("odds", "42", "evt-q", "mismatch")
    rows = db.quarantined("odds")
    assert len(rows) == 1 and rows[0]["reason"] == "mismatch"


def test_quarantined_predictions_are_excluded_from_performance(scoped_db):
    from edgeterminal import calibration
    db.save_prediction(dict(id="p1", event_id="evt-q", sport="NFL", market="spread",
                            selection="Kansas City Chiefs", state="OFFICIAL PICK",
                            confidence=85, units=1.0, price=-110, line=-3.0, book="fanduel",
                            model_prob=0.55, implied_prob=0.52, edge_pct=3.0,
                            fair_price=-120, created_at=NOW, is_demo=0))
    db.ex("INSERT OR REPLACE INTO results(prediction_id,event_id,outcome,won,push,graded_at) "
          "VALUES(?,?,?,?,?,?)", ("p1", "evt-q", "win", 1, 0, NOW))
    assert calibration.performance()["graded"] == 1
    db.quarantine("prediction", "p1", "evt-q", "wrong matchup")
    assert calibration.performance()["graded"] == 0