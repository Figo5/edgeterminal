"""Actual stored-source boundaries for prospective spread evidence."""
from datetime import datetime, timezone
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from edgeterminal import db
from edgeterminal.config import settings
from test_nfl_spread_component import isolated, NOW

GAME='nfl-2026-reg-10-atl-gb'
HOME='Green Bay Packers'; AWAY='Atlanta Falcons'


def saved(monkeypatch, when=NOW):
    from edgeterminal.analytics import football
    source=SimpleNamespace(verify_input_map=lambda *args,**kw: {'stats':{t:dict(epa_play=.1,success_rate=.45) for t in [HOME,AWAY]}})
    monkeypatch.setitem(sys.modules,'edgeterminal.nfl_game_sources',source)
    import edgeterminal
    monkeypatch.setattr(edgeterminal,'nfl_game_sources',source,raising=False)
    start=datetime.fromtimestamp(when+3600,timezone.utc).isoformat()
    event=dict(id=GAME,sport='NFL',home_team=HOME,away_team=AWAY,start_time=start,
        status='STATUS_SCHEDULED',meta=json.dumps(dict(season=2026,week=10,season_type=2)))
    db.ex('INSERT INTO events(id,sport,home_team,away_team,start_time,status,meta) VALUES(?,?,?,?,?,?,?)',tuple(event[k] for k in ['id','sport','home_team','away_team','start_time','status','meta']))
    monkeypatch.setattr(db,'now',lambda:when)
    db.store_odds([dict(event_id=GAME,sport='NFL',book='ESPN BET',market='spread',selection=HOME,
        line=-3.5,price=-110,captured_at=when-1,source_url='https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard')],source='espn-odds')
    quotes=db.latest_odds(event_id=GAME)
    stats={t:dict(points_for=25.,points_against=22.,games=8,epa_play=.1,success_rate=.45,retrieved_at=when-2) for t in [HOME,AWAY]}
    projection=football.project_game('NFL',HOME,AWAY,stats)
    game=dict(event,kickoff_utc=start)
    payload=dict(game=game,projection=projection,team_form=stats,market=quotes,created_at=when,
        model_version='et-model-0.4.0',forecast_policy_id='nfl-game-source-revisions-v1',game_input_map=dict(schema=1,policy_id='nfl-game-current-inputs-v1',
        as_of=when,original_received_at=when-10,adopted_at=when-5,sha256='a'*64,
        teams={t:dict(games=8,plays=500) for t in [HOME,AWAY]}))
    sid=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()[:24]
    db.save_nfl_snapshot(sid,GAME,payload,'et-model-0.4.0','nfl-features-1')
    return sid,payload


def test_exact_saved_quote_and_source_are_required(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch)
    row=data.observation(sid,now=NOW)
    assert row['home_spread']==-3.5
    assert row['input_policy_id']=='nfl-game-current-inputs-v1'
    assert row['feature_received_at']==NOW-2
    with pytest.raises(ValueError,match='snapshot|missing'):
        data.observation('absent',now=NOW)
    fake=dict(payload['market'][0],line=-7.5)
    with pytest.raises(ValueError,match='quote'):
        data.quote_context([fake],dict(id=GAME,home_team=HOME,away_team=AWAY),NOW,NOW+3600)


def test_source_verifier_failure_cannot_be_hidden_by_complete_feature_flags(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,_=saved(monkeypatch)
    import edgeterminal.nfl_game_sources as source
    monkeypatch.setattr(source,'verify_input_map',lambda *args,**kw:(_ for _ in ()).throw(ValueError('corrupt raw hash')))
    with pytest.raises(ValueError,match='corrupt raw hash'):
        data.observation(sid,now=NOW)


def test_finished_game_cannot_be_enrolled_with_a_replayed_clock(monkeypatch):
    from edgeterminal import nfl_spread_learning as learning
    sid,_=saved(monkeypatch)
    monkeypatch.setattr(learning.time,'time',lambda:NOW+4000)
    result=learning.capture(sid,now=NOW)
    assert result['status']=='skipped'
    assert db.q1('SELECT COUNT(*) n FROM nfl_spread_observations')['n']==0


def test_final_sources_are_exact_hashed_and_prior_corrections_are_not_mutated(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch)
    def body(score):
        return json.dumps(dict(season=dict(year=2026,type=2),week=dict(number=10),events=[dict(id='1234567',
            date=payload['game']['start_time'],status=dict(type=dict(name='STATUS_FINAL',completed=True)),
            competitions=[dict(competitors=[dict(homeAway='home',score=str(score),team=dict(displayName=HOME,abbreviation='GB')),
                dict(homeAway='away',score='20',team=dict(displayName=AWAY,abbreviation='ATL'))])])])).encode()
    for score,stamp in [(25,NOW+18000),(26,NOW+19000)]:
        raw=body(score); digest=hashlib.sha256(raw).hexdigest()
        db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',(digest,
            'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2026&seasontype=2&week=10&limit=100',stamp,raw))
    labels=data.labels([data.observation(sid,now=NOW)],now=NOW+20000)
    assert labels[GAME]['actual_margin']==6
    assert labels[GAME]['label_received_at']==NOW+19000
    assert db.q1('SELECT COUNT(*) n FROM nfl_prop_grade_source_artifacts')['n']==2


def test_final_bad_hash_and_equal_clock_conflicts_remain_errors(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch)
    raw=b'{"events":[]}'
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',('f'*64,
        'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2026&seasontype=2&week=10',NOW+18000,raw))
    with pytest.raises(ValueError,match='hash'):
        data.labels([data.observation(sid,now=NOW)],now=NOW+20000)
    with pytest.raises(ValueError,match='duplicate'):
        data.decode('{"events":[],"events":[]}')


def test_source_map_cannot_authenticate_changed_advanced_fields(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,_=saved(monkeypatch)
    import edgeterminal.nfl_game_sources as sources
    monkeypatch.setattr(sources,'verify_input_map',lambda *args,**kw:{'stats':{t:dict(epa_play=.9,success_rate=.45) for t in [HOME,AWAY]}})
    with pytest.raises(ValueError,match='advanced fields'):
        data.observation(sid,now=NOW)


def test_cached_final_pending_is_not_zero_and_unresolved_week_is_not_complete(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,_=saved(monkeypatch)
    item=data.observation(sid,now=NOW)
    assert data.labels([item],now=NOW+20000)=={}
    assert not data.complete_week(2026,10,NOW+20000)
    assert data.week_universe(2026,10)==[]  # no authenticated full-week schedule proof


def test_capture_with_legacy_inputs_is_waiting_not_an_integrity_error(monkeypatch):
    from edgeterminal import nfl_spread_learning as learning
    sid,payload=saved(monkeypatch)
    original=learning.data.decode
    def decode(raw):
        result=original(raw)
        result['game_input_map']=dict(status='legacy_fallback')
        return result
    monkeypatch.setattr(learning.data,'decode',decode)
    monkeypatch.setattr(learning.time,'time',lambda:NOW)
    assert learning.capture(sid,now=NOW)['status']=='waiting'
    assert learning.project_component(payload['projection'],payload['game'],payload['market'],{},NOW) is None


def _status_receipt(payload, status, completed, stamp, score=26):
    raw=json.dumps(dict(lastUpdated=stamp,season=dict(year=2026,type=2),week=dict(number=10),events=[dict(id='1234567',
        date=payload['game']['start_time'],status=dict(type=dict(name=status,completed=completed)),
        competitions=[dict(competitors=[dict(homeAway='home',score=str(score),team=dict(displayName=HOME,abbreviation='GB')),
            dict(homeAway='away',score='20',team=dict(displayName=AWAY,abbreviation='ATL'))])])])).encode()
    digest=hashlib.sha256(raw).hexdigest()
    db.ex('INSERT INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',(digest,
        'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2026&seasontype=2&week=10&limit=100',stamp,raw))
    return digest


def test_new_policy_snapshot_seal_precedes_formula_replay(monkeypatch):
    from edgeterminal import nfl_spread_data as data,nfl_game_forecasts as forecasts
    from edgeterminal.analytics import football
    sid,payload=saved(monkeypatch)
    payload['forecast_policy_id']=forecasts.POLICY_ID
    encoded=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)
    authentic=hashlib.sha256(encoded.encode()).hexdigest()[:24]
    assert authentic==sid  # exactly the compact content-addressed serving policy
    assert data.observation(authentic,now=NOW)['baseline_margin']==payload['projection']['margin']
    # A damaged-storage copy carries a mathematically coherent changed score,
    # but retains the content address of the original authenticated snapshot.
    payload['team_form'][HOME]['points_for']+=14.
    payload['projection']=football.project_game('NFL',HOME,AWAY,payload['team_form'])
    changed=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False)
    db.ex('DROP TRIGGER nfl_snapshot_no_update')
    db.ex('UPDATE nfl_game_snapshots SET payload=? WHERE id=?',(changed,authentic))
    with pytest.raises(ValueError,match='snapshot.*(hash|seal)|snapshot.*identity'):
        data.observation(authentic,now=NOW)


def test_final_nonfinal_final_receipts_reset_label_availability(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch);item=data.observation(sid,now=NOW)
    first=_status_receipt(payload,'STATUS_FINAL',True,NOW+18000)
    assert data.labels([item],now=NOW+18000)[GAME]['label_sha256']==first
    _status_receipt(payload,'STATUS_IN_PROGRESS',False,NOW+18060)
    assert data.labels([item],now=NOW+18060)=={}
    # A new final with the original score is a new availability event after
    # withdrawal, never resurrection of the earlier final's receipt clock.
    latest=_status_receipt(payload,'STATUS_FINAL',True,NOW+18120,score=26)
    result=data.labels([item],now=NOW+18120)[GAME]
    assert result['label_sha256']==latest and result['label_received_at']==NOW+18120
    assert result['actual_margin']==6
    assert db.q1('SELECT count(*) n FROM nfl_prop_grade_source_artifacts')['n']==3


def test_equal_clock_final_nonfinal_conflict_fails_closed(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch);item=data.observation(sid,now=NOW)
    _status_receipt(payload,'STATUS_FINAL',True,NOW+18000)
    _status_receipt(payload,'STATUS_IN_PROGRESS',False,NOW+18000)
    with pytest.raises(ValueError,match='equal-clock|status conflict'):
        data.labels([item],now=NOW+18000)


@pytest.mark.parametrize('name,completed',[('STATUS_FINAL',False),('STATUS_IN_PROGRESS',True),('STATUS_FINAL',None)])
def test_contradictory_or_missing_official_status_never_reuses_an_old_final(monkeypatch,name,completed):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch);item=data.observation(sid,now=NOW)
    _status_receipt(payload,'STATUS_FINAL',True,NOW+18000)
    _status_receipt(payload,name,completed,NOW+18060)
    with pytest.raises(ValueError,match='status'):
        data.labels([item],now=NOW+18060)


def test_old_unsealed_snapshot_is_ineligible_without_changing_legacy_readers(monkeypatch):
    from edgeterminal import nfl_spread_data as data
    sid,payload=saved(monkeypatch)
    payload.pop('forecast_policy_id')
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'))
    legacy='legacy-original-id'
    db.ex('INSERT INTO nfl_game_snapshots VALUES(?,?,?,?,?,?)',(legacy,GAME,raw,'et-model-0.4.0','legacy',NOW))
    assert data.observation(legacy,now=NOW) is None
    eligible,unknown=data.observations(now=NOW)
    assert unknown==1 and [r['snapshot_id'] for r in eligible]==[sid]
    assert db.q1('SELECT payload FROM nfl_game_snapshots WHERE id=?',(legacy,))['payload']==raw
