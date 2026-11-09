const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../edgeterminal/web/nba_ui.js');

test('stage progress distinguishes collected evidence from a live replacement',()=>{
 const html=ui.about({totals_learning:{enabled:true,status:'waiting',active:null,
  detail:{stage:'development',coverage:{fit:660,development:32,calibration:0},
   required:{fit:660,development:120,calibration:120}},
  message:'Collecting verified games <not activated>'}});
 assert.match(html,/32\s*\/\s*120/);
 assert.match(html,/660\s*\/\s*660/);
 assert.match(html,/Current scoring baseline/);
 assert.match(html,/&lt;not activated&gt;/);
 assert.doesNotMatch(html,/<not activated>|undefined|NaN/);
});

test('NFL learning status reports insufficient season without claiming activation',()=>{
 const html=ui.gameLearningStatus({enabled:true,status:'waiting',
  detail:{stage:'validation',reason:'insufficient_season_remaining',coverage:{validation:12},
   required:{validation:60}},message:'Insufficient season remaining; baseline stays live.'});
 assert.match(html,/NFL spread/);
 assert.match(html,/Insufficient season remaining/);
 assert.match(html,/12\s*\/\s*60/);
 assert.doesNotMatch(html,/undefined|NaN/);
});
