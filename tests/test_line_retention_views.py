"""Product views consume saved comparison bindings without presenting fresh prices."""
from edgeterminal import product_views, nfl_game_lines


def test_nfl_board_uses_the_sealed_retained_comparison_not_the_empty_fresh_market(monkeypatch):
    game={'id':'nfl-2026-reg-10-atl-gb','home_team':'Green Bay Packers',
          'away_team':'Atlanta Falcons','start_time':'2026-11-15T18:00Z','markets':[]}
    snapshot={'forecast_policy_id':'nfl-game-source-revisions-v1','game':game,
              'market':[],'line_retention':{'groups':{}}}
    monkeypatch.setattr(product_views,'_prediction',lambda *a,**k: (
        {'margin':4,'total':45,'proj_home':24.5,'proj_away':20.5,'home_win_prob':.6},100,snapshot))
    monkeypatch.setattr(product_views.db,'q',lambda *a,**k:[])
    monkeypatch.setattr(nfl_game_lines,'effective_quotes',lambda *a,**k:[
        {'market':'spread','selection':game['home_team'],'book':'draftkings','line':-3.5,
         'price':-110,'captured_at':90}])
    monkeypatch.setattr(nfl_game_lines,'line_refs',lambda *a,**k:{'spread':{
        'basis':'retained','book':'draftkings','captured_at':90}})
    public=product_views._game(game)
    assert public['market']['spread']['home']['line']==-3.5
    assert public['saved_line_refs']['spread']['captured_at']==90
    assert public['market_is_stale']


def test_nba_record_api_exposes_audit_separately_without_changing_headline(monkeypatch):
    from edgeterminal import api, game_line_audit
    headline={'sport':'NBA','spread':{'won':4,'lost':3}}
    audit={'rows':[{'result':'won'}],'excluded_from_live_record':True}
    monkeypatch.setattr(api.nba_ops,'performance',lambda:headline.copy())
    monkeypatch.setattr(game_line_audit,'view',lambda sport:audit)
    public=api.product_nba_performance()
    assert public['spread']==headline['spread']
    assert public['historical_line_audit']==dict(audit,status='ok')


def test_bad_historical_audit_does_not_hide_original_season_record(monkeypatch):
    from edgeterminal import api, game_line_audit
    headline={'sport':'NBA','spread':{'won':4,'lost':3}}
    monkeypatch.setattr(api.nba_ops,'performance',lambda:headline.copy())
    def damaged(sport):raise ValueError('Historical final source hash conflict')
    monkeypatch.setattr(game_line_audit,'view',damaged)
    public=api.product_nba_performance()
    assert public['spread']==headline['spread']
    assert public['historical_line_audit']['status']=='needs_review'
    assert public['historical_line_audit']['rows']==[]
    assert public['historical_line_audit']['excluded_from_live_record']
