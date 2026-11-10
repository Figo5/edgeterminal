const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../edgeterminal/web/nba_ui.js');

test('Recovered research is separate from the original NBA headline and escapes provenance',()=>{
 const data={season:'2026-27',spread:{won:4,lost:3},historical_line_audit:{rows:[{
  game_id:'game<unsafe>',home:'GSW',away:'NOP',market:'spread',side:'home',line:-9.5,
  projection:6.45,result:'won',forecast_at:1794260000,model_version:'model<unsafe>',
  source:{sportsbook:'book<unsafe>'}}],excluded_from_live_record:true}};
 const html=ui.record(data);
 assert.match(html,/<small>Against the spread<\/small><strong>4–3<\/strong>/);
 assert.match(html,/Recovered line research/);
 assert.match(html,/Excluded from the live season record and model learning/);
 assert.match(html,/model&lt;unsafe&gt;/);
 assert.doesNotMatch(html,/<unsafe>|undefined|NaN/);
});

test('Retained lines identify the original quote time without calling it a fresh price',()=>{
 const note=ui.retainedLineNote({spread:{basis:'retained',book:'draftkings',captured_at:100}});
 assert.match(note,/Last observed comparison/);
 assert.match(note,/not current prices/);
 assert.doesNotMatch(note,/Not checked yet|undefined|NaN/);
 assert.equal(ui.retainedLineNote({spread:{basis:'fresh'}}),'');
 assert.equal(ui.lineAudit({rows:[]}),'');
});

test('Historical away spread displays the opposite of the saved home handicap',()=>{
 const html=ui.lineAudit({rows:[{home:'GSW',away:'NOP',market:'spread',side:'away',line:-9.5,
  projection:6.45,result:'lost',forecast_at:1794260000,model_version:'model'}]});
 assert.match(html,/NOP \+9\.5/);
 assert.doesNotMatch(html,/NOP -9\.5/);
});

test('Audit integrity failure stays visible without pretending research is empty or valid',()=>{
 const html=ui.record({spread:{won:4,lost:3},historical_line_audit:{status:'needs_review',rows:[]}});
 assert.match(html,/<small>Against the spread<\/small><strong>4–3<\/strong>/);
 assert.match(html,/Recovered line research needs review/);
 assert.match(html,/Original live season totals remain visible/);
 assert.doesNotMatch(html,/undefined|NaN/);
});
