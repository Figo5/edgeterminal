const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../edgeterminal/web/nba_ui.js');

test('NBA totals status shows automatic validation and a retained live baseline',()=>{
 const html=ui.about({totals_learning:{enabled:true,status:'waiting',active:null,
  message:'Waiting for fresh whole-game validation <unsafe>',checked_at:1793884000,next_due_at:1793884300}});
 assert.match(html,/Totals model/);
 assert.match(html,/Current scoring baseline/);
 assert.match(html,/Next automatic check/);
 assert.match(html,/Every supported game keeps a totals pick/);
 assert.match(html,/&lt;unsafe&gt;/);
 assert.doesNotMatch(html,/<unsafe>|undefined|NaN|Invalid Date/);
});

test('Totals version history counts one saved total per game with legacy provenance',()=>{
 const html=ui.versionHistory({totals:[{model_version:'legacy-total:game-v1',game_count:2,count:2,
  record:{won:1,lost:1},win_rate:.5,first_saved_at:1792500000,last_saved_at:1792600000},
  {model_version:'totals-v2<unsafe>',game_count:1,count:1,record:{pending:1},win_rate:null,
   first_saved_at:1793884000,last_saved_at:1793884000}]});
 assert.match(html,/Totals component/);
 assert.match(html,/legacy-total:game-v1/);
 assert.match(html,/totals-v2&lt;unsafe&gt;/);
 assert.match(html,/1 pending/);
 assert.doesNotMatch(html,/<unsafe>|undefined|NaN/);
});

test('Totals learning jobs identify diagnostics and retuning separately',()=>{
 const html=ui.learningStatus({enabled:true,runner:{worker:{state:'running'},jobs:[{component:'NBA_TOTAL',kind:'diagnostics',
  status:'running',message:'Verified totals diagnostics',elapsed_seconds:5}]}});
 assert.match(html,/NBA totals diagnostics/);
 assert.doesNotMatch(html,/NBA_TOTAL diagnostics|undefined|NaN/);
});
