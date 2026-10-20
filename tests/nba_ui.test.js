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
  assert.doesNotMatch(html,/Not recorded|Week/);
});
