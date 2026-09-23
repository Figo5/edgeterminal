"""Point-in-time controls for the offline NFL model lab."""
from pathlib import Path

import pytest

from edgeterminal import nfl_research as research
from edgeterminal.nfl_forward_research import freeze


def _game(game_id, week, home, away, home_score, away_score):
    return dict(game_id=game_id, season="2025", week=str(week), game_type="REG",
                home_team=home, away_team=away, home_score=str(home_score),
                away_score=str(away_score), home_rest="7", away_rest="7",
                location="Home", spread_line="1.5", total_line="44.5")


def test_same_week_scores_and_plays_cannot_enter_features(monkeypatch):
    games=[_game("g1",1,"KC","BUF",24,17),_game("g2",1,"BAL","PIT",21,14),
           _game("g3",2,"KC","BAL",28,24),_game("g4",2,"BUF","PIT",20,17)]
    metrics={}
    for game in games:
        for team in (game["home_team"],game["away_team"]):
            metrics[(game["game_id"],team)]={"off_epa":.2,"def_epa":-.1,
                "off_success":.5,"def_success":.4,"off_early_epa":.1,
                "off_explosive":.15,"qb_id":team+"QB","qb_epa":.2}
    monkeypatch.setattr(research,"load_schedules",lambda cache:games)
    monkeypatch.setattr(research,"_game_play_metrics",lambda cache,seasons:metrics)
    first=research.build_dataset(Path("unused"),seasons=[2025])
    first_features=[row["features"] for row in first if row["week"]==2]
    games[2]["home_score"]="0"
    games[2]["away_score"]="50"
    metrics[("g3","KC")]["off_epa"]=8.0
    second=research.build_dataset(Path("unused"),seasons=[2025])
    assert [row["features"] for row in second if row["week"]==2]==first_features
    assert second[2]["actual_margin"]!=first[2]["actual_margin"]
    assert first[2]["features"]["away_tz_shift"] == 1


def test_betting_scores_include_counts_and_bootstrap_intervals():
    games=[dict(season=2025,week=1,actual_margin=7,actual_total=50,
                spread_line=3,total_line=45),
           dict(season=2025,week=1,actual_margin=-7,actual_total=40,
                spread_line=3,total_line=45)]
    predictions=[dict(game=g,models={"linear":dict(margin=5,total=48,prob=.6)})
                 for g in games]
    result=research.score(predictions)["linear"]
    assert result["ats"]=={"n":2,"wins":1,"hit_rate":.5,
                            "bootstrap_ci95":[0.0,1.0]}
    assert result["totals"]["n"]==2
    assert result["totals"]["bootstrap_ci95"] is not None


def test_market_probability_removes_moneyline_vig():
    prob=research._market_probability(dict(home_moneyline=-120,
                                           away_moneyline=110,spread_line=2.5))
    assert .53 < prob < .54


def test_qb_history_uses_current_incumbents_prior_dropbacks():
    past=[dict(home="Kansas City Chiefs",away="Buffalo Bills",
               metrics={"Kansas City Chiefs":dict(qb_id="old",qb_epa=1.0,qb_dropbacks=30)}),
          dict(home="Kansas City Chiefs",away="Buffalo Bills",
               metrics={"Kansas City Chiefs":dict(qb_id="new",qb_epa=-.2,qb_dropbacks=20)}),
          dict(home="Kansas City Chiefs",away="Buffalo Bills",
               metrics={"Kansas City Chiefs":dict(qb_id="new",qb_epa=.1,qb_dropbacks=40)})]
    snapshot=research._team_snapshot(past,"Kansas City Chiefs")
    assert abs(snapshot["qb_epa"]-0.0)<1e-9
    assert snapshot["qb_change"]==0.0


def test_forward_research_freeze_refuses_overwrite(tmp_path):
    output=tmp_path/"week3.json"
    output.write_text("frozen")
    with pytest.raises(FileExistsError):
        freeze(tmp_path,output,2026,3,"linear")
    assert output.read_text()=="frozen"
