const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../edgeterminal/web/nba_ui.js');

test('NBA daily slate shows date controls and honest waiting states', () => {
  const html = ui.board({date:'2026-10-20',today:'2026-10-20',season:'2026-27',games:[{
    id:'nba-2026-1',home_team:'BOS',away_team:'NYK',tipoff:'2026-10-20T23:00Z',
    top_props:[],prop_message:'Waiting for five supported main lines.',projection:null,
    forecast_message:'Historical inputs are still loading.'}],record:{won:0,lost:0,pending:0}});
  assert.match(html,/NBA projections/);
  assert.match(html,/NBA slate date/);
  assert.match(html,/Historical inputs are still loading/);
  assert.match(html,/Waiting for five supported main lines/);
  assert.doesNotMatch(html,/Week|NaN|undefined/);
});

test('NBA zero actuals render and sources are escaped', () => {
  const html=ui.matchup({game:{id:'nba-2026-1',home_team:'BOS',away_team:'NYK',
    tipoff:'2026-10-20T23:00Z',status:'FINAL',top_props:[{rank:1,player:'<unsafe>',
    market:'points',side:'over',line:0.5,result:'lost',actual:0,confidence:0.6,price:-110}]}});
  assert.match(html,/Loss · 0/);
  assert.match(html,/&lt;unsafe&gt;/);
  assert.doesNotMatch(html,/<unsafe>/);
});

test('NBA record separates historical evaluation from saved predictions', () => {
  const html=ui.record({season:'2026-27',props:{won:0,lost:0,push:0,void:0,pending:5},
    winners:{won:0,lost:0,pending:1},spread:{},total:{},days:[]});
  assert.match(html,/No graded selections/);
  assert.match(html,/5 pending/);
  assert.match(html,/Historical evaluation/);
  assert.match(html,/2026-27 NBA full season record/);
  assert.match(html,/all saved live model versions/);
  assert.match(html,/<div class="record-metric"><small>Player props<\/small><strong>No graded selections<\/strong><span>5 pending · 0 void<\/span><\/div>/);
  assert.doesNotMatch(html,/Not recorded|Week/);
});

test('Full NBA record attributes older and newer saved versions without changing headline totals', () => {
  const html=ui.record({season:'2026-27',props:{won:3,lost:2,pending:1},winners:{},spread:{},total:{},days:[],
    model_versions:{props:[
      {model_version:'original<unsafe>',count:3,game_count:1,record:{won:1,lost:2},win_rate:1/3,first_saved_at:1792500000,last_saved_at:1792500000},
      {model_version:'new-v2',count:3,game_count:1,record:{won:2,pending:1},win_rate:1,first_saved_at:1792600000,last_saved_at:1792600000}],games:[
      {model_version:'wrapper-v2',game_component_version:'spread<component>',game_count:1,first_saved_at:1792600000,last_saved_at:1792600000,
        winner:{count:1,record:{won:1},win_rate:1},spread:{count:1,record:{lost:1},win_rate:0},total:{count:0,record:{},win_rate:null}}]}});
  const [headline,versions]=html.split('<section class="performance-block model-version-records">');
  assert.match(headline,/<div class="record-metric"><small>Player props<\/small><strong>3–2<\/strong><span>/);
  assert.ok(versions);
  assert.match(versions,/Live records by model version/);
  assert.match(versions,/original&lt;unsafe&gt;/);
  assert.match(versions,/new-v2/);
  assert.match(versions,/spread&lt;component&gt;/);
  assert.match(versions,/1 pending/);
  assert.match(versions,/33.3% win rate/);
  assert.match(versions,/Saved record dates/);
  assert.doesNotMatch(html,/<unsafe>|<component>|NaN|undefined/);
});

test('Version history keeps unknown identities and empty or void-only records honest', () => {
  assert.equal(ui.versionHistory(), '');
  const html=ui.versionHistory({props:[{model_version:null,count:1,game_count:1,
    record:{void:1},win_rate:null,first_saved_at:null,last_saved_at:null}],games:[]});
  assert.match(html,/Unrecorded version/);
  assert.match(html,/1 void/);
  assert.match(html,/No graded selections/);
  assert.match(html,/Dates not recorded/);
  assert.match(html,/Saved record dates/);
  assert.doesNotMatch(html,/0.0% win rate|NaN|undefined/);
});

test('NBA probability disclosures follow the saved prediction method', () => {
  const game={id:'nba-2026-1',home_team:'BOS',away_team:'NYK',top_props:[{
    rank:1,player:'Player',market:'points',side:'over',line:15.5,confidence:0.6,
    model_version:'nba-research-2.0.0',probability_method:'historical_standardized_residual_cdf'}]};
  const upgraded=ui.matchup({game});
  assert.match(upgraded,/historical forecast errors/);
  assert.match(upgraded,/nba-research-2.0.0/);
  assert.doesNotMatch(upgraded,/uncalibrated research estimates/);
  const legacy=ui.matchup({game:{...game,top_props:[{...game.top_props[0],probability_method:undefined}]}});
  assert.match(legacy,/uncalibrated research estimates/);
  assert.doesNotMatch(legacy,/historical forecast errors/);
});

test('Learning status shows a retained model and escapes recorded reasons', () => {
  const html=ui.about({model:{version:'nba-research-2.0.1',cutoff:'2026-10-01'},learning:{
    enabled:true,schedule:'Weekly retuning',diagnostics:{status:'ok',checked_at:1793188800},
    retuning:{status:'rejected',checked_at:1793188800,next_due_at:1793793600,
      detail:{reasons:['market_regression:<unsafe>']}}}});
  assert.match(html,/Automatic model learning/);
  assert.match(html,/Current model retained/);
  assert.match(html,/&lt;unsafe&gt;/);
  assert.match(html,/saved predictions and grades stay fixed/);
  assert.doesNotMatch(html,/<unsafe>|undefined|fixed input\/training cutoff/);
});

test('Live NBA game revisions explain the pretipoff record and stale inputs', () => {
  const html=ui.matchup({game:{id:'nba-2026-1',home_team:'BOS',away_team:'NYK',
    forecast_refresh:{enabled:true,state:'stale',message:'Pregame updates continue automatically; the latest saved forecast before tipoff becomes the record.'},
    projection:{game_component_version:'nba-game-live-1.2.0'},top_props:[]}});
  assert.match(html,/latest saved forecast before tipoff/);
  assert.match(html,/Inputs need a fresh check/);
  assert.match(html,/nba-game-live-1.2.0/);
});

test('Phone data page distinguishes the local worker from an always-on host', () => {
  const local=ui.about({runtime:{mode:'local',requires_awake_mac:true},game_model:{version:'game-v2'},
    game_learning:{message:'More verified games needed <unsafe>'}});
  assert.match(local,/Mac must be awake/);
  assert.match(local,/Spread model/);
  assert.match(local,/&lt;unsafe&gt;/);
  assert.doesNotMatch(local,/<unsafe>/);
  const hosted=ui.about({runtime:{mode:'hosted',requires_awake_mac:false}});
  assert.match(hosted,/independently of your Mac/);
  assert.doesNotMatch(hosted,/while this Mac is awake/);
});

test('Validated calibration and workload forecasts show their saved method honestly', () => {
  for (const [method, phrase] of [
    ['actual_line_selected_policy_calibration', /Calibrated on earlier saved market lines/],
    ['role_minutes_predictive_mixture', /Includes uncertainty in minutes and production/]
  ]) {
    const html=ui.matchup({game:{home_team:'BOS',away_team:'NYK',top_props:[{
      rank:1,player:'Player',market:'points',side:'over',line:15.5,confidence:.49,
      probability_method:method}]}});
    assert.match(html,phrase);
    assert.match(html,/49\.0%/);
    assert.doesNotMatch(html,/uncalibrated research estimates/);
    assert.match(html,/over 15\.5/);
  }
});
