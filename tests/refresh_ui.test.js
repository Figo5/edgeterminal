const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../edgeterminal/web/app.js'), 'utf8')
  .replace(/init\(\);\s*$/, '');

function harness(fetch) {
  const elements = {
    refreshButton: {disabled: false, textContent: '↻ Refresh odds', attrs: {},
      setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }},
    lastUpdated: {textContent: ''},
    mobileUpdated: {textContent: ''},
    toast: {textContent: '', classList: {add() {}, remove() {}}},
    main: {innerHTML: '', scrollTop: 0},
    sections: {innerHTML: ''},
  };
  const scope = {document: {getElementById: id => elements[id]}, fetch,
    setTimeout: () => 1, clearTimeout: () => {}, console, Date,
    EdgeNBA: require('../edgeterminal/web/nba_ui.js')};
  vm.runInNewContext(source + '\nglobalThis.__test = {refreshProduct, pollStoredBoard, state, paper, performance, updateChecks, captureHistory};', scope);
  return {elements, ...scope.__test};
}

const response = body => ({ok: true, json: async () => body});
const board = name => ({games: [{id: 'nfl-2026-reg-03-atl-gb',
  away_team: name, home_team: 'Green Bay Packers', kickoff_utc: '2026-09-25T00:15Z',
  designation: 'PASS', projection: null, market: {}}]});

test('About data distinguishes a pending source retry from an expired capture window', () => {
  const {state, updateChecks} = harness(() => { throw Error('status display must not fetch'); });
  state.service = {update_checks: [
    {label: 'Player-prop grading', message: 'Source unavailable <private>', checked_at: 900,
      next_retry_at: Date.now()/1000 + 3600, state: 'retry_pending'},
    {label: 'Near-kickoff game quotes', message: 'The failed capture window has ended.',
      checked_at: 100, state: 'past_window'},
  ]};
  const html = updateChecks();
  assert.match(html, /Next scheduled retry/);
  assert.match(html, /Last checked/);
  assert.match(html, /window has ended/);
  assert.match(html, /&lt;private&gt;/);
  assert.doesNotMatch(html, /<private>/);
});

test('Past capture gaps are labeled as history and preserve the no-backfill explanation', () => {
  const {captureHistory} = harness(() => { throw Error('history must not fetch'); });
  assert.equal(captureHistory({}), '');
  const html = captureHistory({historical_warnings: [{message: 'A past capture was missed.',
    game_ids: ['nfl-2026-reg-06-buf-lv']}]});
  assert.match(html, /Past capture gaps/);
  assert.match(html, /BUF at LV/);
  assert.match(html, /not added after kickoff/);
});

test('Refresh disables immediately, ignores a second click, then reloads stored board', async () => {
  let finishPost;
  const calls = [];
  const {elements, refreshProduct} = harness((url, options) => {
    calls.push([url, options?.method || 'GET']);
    if (url === '/api/product/nfl/check') return new Promise(resolve => { finishPost = resolve; });
    if (url === '/api/product/status') return Promise.resolve(response({state:'healthy'}));
    if (url === '/api/account') return Promise.resolve(response({signed_in:false}));
    if (url === '/api/product/nfl') return Promise.resolve(response(board('New stored team')));
    throw Error(`unexpected request: ${url}`);
  });
  const first = refreshProduct();
  await refreshProduct();
  assert.equal(elements.refreshButton.disabled, true);
  assert.equal(elements.refreshButton.textContent, '↻ Checking…');
  assert.equal(elements.refreshButton.attrs['aria-busy'], 'true');
  assert.equal(calls.filter(([url]) => url === '/api/product/nfl/check').length, 1);
  finishPost(response({ok: true, status: 'checked', did_work: true}));
  await first;
  assert.equal(elements.refreshButton.disabled, false);
  assert.equal(elements.lastUpdated.textContent, 'Week 3 · No observed prices');
  assert.equal(elements.mobileUpdated.textContent, 'Week 3 · No observed prices');
  assert.match(elements.main.innerHTML, /New stored team/);
  assert.deepEqual(calls.map(([url]) => url), ['/api/product/nfl/check', '/api/product/status', '/api/account', '/api/product/nfl']);
});

test('Refresh failure shows a clean message and still reloads stored data', async () => {
  const {elements, refreshProduct, state} = harness(url => {
    if (url === '/api/product/nfl/check') return Promise.resolve({ok: false, status: 503,
      statusText: 'Unavailable', json: async () => ({detail: 'secret provider response'})});
    if (url === '/api/product/nfl') return Promise.resolve(response(board('Still stored team')));
    throw Error(`unexpected request: ${url}`);
  });
  state.board = board('Stored team');
  await refreshProduct();
  assert.equal(elements.refreshButton.disabled, false);
  assert.equal(elements.lastUpdated.textContent, 'Update check failed · Showing stored Week 3');
  assert.equal(elements.mobileUpdated.textContent, elements.lastUpdated.textContent);
  assert.doesNotMatch(elements.lastUpdated.textContent, /secret/);
  assert.match(elements.main.innerHTML, /Still stored team/);
});

test('A blocked weekly snapshot is identified without declaring all saved data stale', async () => {
  const {elements, refreshProduct} = harness(url => {
    if (url === '/api/product/nfl/check') return Promise.resolve(response({
      ok:false,status:'needs_attention',attention:'weekly_forecast'}));
    if (url === '/api/product/status') return Promise.resolve(response({state:'attention'}));
    if (url === '/api/account') return Promise.resolve(response({signed_in:false}));
    if (url === '/api/product/nfl') return Promise.resolve(response(board('Current team')));
    throw Error(`unexpected request: ${url}`);
  });
  await refreshProduct();
  assert.equal(elements.lastUpdated.textContent, 'Week 3 · Weekly forecast snapshot needs review');
  assert.equal(elements.mobileUpdated.textContent, elements.lastUpdated.textContent);
  assert.match(elements.toast.textContent, /Prices and saved prop selections remain available/);
  assert.match(elements.main.innerHTML, /Current team/);
});

test('Past paper position with a verified stat appears as awaiting settlement', () => {
  const {elements, paper} = harness(() => { throw Error('no request expected'); });
  paper({current_bankroll: 1000, available_bankroll: 990, starting_bankroll: 1000,
    profit_loss: 0, open_positions: 0, awaiting_settlement_positions: 1,
    settled_positions: 0, verified_statistical_record: {won: 1}, record: {},
    awaiting: [{player_name: 'Example Player', market: 'receptions', game_id: 'game',
      side: 'over', line: 1.5, sportsbook: 'espnbet', odds: 120, stake: 10,
      settlement: {result: 'pending', research_statistical_result: 'won'}}]});
  assert.match(elements.main.innerHTML, /AWAITING SETTLEMENT/);
  assert.match(elements.main.innerHTML, /Verified stat: won · book settlement pending/);
  assert.match(elements.main.innerHTML, /No open mock positions/);
});


test('Visible page polling requests guarded work rather than a forced refresh', async () => {
  const calls = [];
  const {pollStoredBoard} = harness(url => {
    calls.push(url);
    if (url === '/api/product/nfl/check') return Promise.resolve(response({ok:true,status:'cached',did_work:false}));
    if (url === '/api/product/status') return Promise.resolve(response({state:'healthy'}));
    if (url === '/api/account') return Promise.resolve(response({signed_in:false}));
    if (url === '/api/product/nfl') return Promise.resolve(response(board('Updated on visit')));
    throw Error(`unexpected request: ${url}`);
  });
  await pollStoredBoard();
  assert.equal(calls[0], '/api/product/nfl/check');
  assert.equal(calls.filter(x => x === '/api/product/nfl/check').length, 1);
  assert.ok(!calls.some(x => x.includes('oddsapi') || x === '/api/product/nfl/refresh'));
});


test('Combined season record includes replayed wins while preserving provenance labels', () => {
  const {elements, performance} = harness(() => {throw Error('no request expected');});
  const historical={count:5,record:{won:4,lost:1},win_rate:.8};
  const empty={count:0,record:{},win_rate:null};
  performance({season_record:{season:2026,props:{...historical,historical_count:5,
    locked:empty,archived:empty,replayed:historical,games:[{game_id:'nfl-2026-reg-01-den-kc',
      week:1,basis:'reconstructed_replay',generated_at:1791297000,training_cutoff:[2026,1],
      rows:[{player:'Example',market:'receptions',side:'over',line:3.5,sportsbook:'draftkings',result:'won',confidence:.8}]}]},
    games:{rows:[],winner:empty,spread:empty,total:empty},weekly:[{week:1,props:historical,
      games:{winner:empty,spread:empty,total:empty}}]}});
  assert.match(elements.main.innerHTML,/Player props/);
  assert.match(elements.main.innerHTML,/80.0% win rate/);
  assert.match(elements.main.innerHTML,/5 reconstructed and 0 locked/);
  assert.match(elements.main.innerHTML,/Reconstructed/);
  assert.match(elements.main.innerHTML,/training before Week 1/);
  assert.doesNotMatch(elements.main.innerHTML,/Live top-five props|Excluded from the live/);
});

test('Linear research results stay in collapsed history outside the full season headline', () => {
  const {elements, performance} = harness(() => {throw Error('no request expected');});
  const empty={count:0,record:{},win_rate:null};
  performance({season_record:{season:2026,props:{...empty,games:[]},
    games:{rows:[],winner:empty,spread:empty,total:empty},weekly:[],
    linear_research:{weekly:[{week:5,game_count:15,winner:empty,
      spread:{count:15,record:{won:9,lost:6},win_rate:.6},
      total:{count:15,record:{won:5,lost:10},win_rate:1/3}}]}}});
  assert.match(elements.main.innerHTML,/Linear research model · weekly results/);
  assert.match(elements.main.innerHTML,/separate from the main forecast record/);
  assert.match(elements.main.innerHTML,/60.0% win rate/);
  assert.match(elements.main.innerHTML,/33.3% win rate/);
  assert.match(elements.main.innerHTML,/<details class="research-disclosure" data-disclosure="model-history"><summary>Model history and research<\/summary>[\s\S]*Linear research model · weekly results[\s\S]*<\/details>/);
});

test('Archived comparison stays inspectable without adding overlapping outcomes to the full record', () => {
  const {elements,performance}=harness(()=>{throw Error('no fetch');});
  const empty={count:0,record:{},win_rate:null};
  performance({season_record:{season:2026,props:{count:2,record:{won:1,lost:1},win_rate:.5,games:[]},games:{rows:[],winner:empty,spread:empty,total:empty},weekly:[],
    challenger:{record:{won:38,lost:23,void:5,pending:20},selections:86,comparison:{n:5,baseline_mae:2,challenger_mae:1.5},brier:.2}}});
  assert.match(elements.main.innerHTML,/Archived comparison model/);
  assert.match(elements.main.innerHTML,/New selections use the active model/);
  assert.match(elements.main.innerHTML,/<small>Challenger record<\/small><strong>38–23<\/strong>/);
  assert.match(elements.main.innerHTML,/86 saved · 20 pending · 5 void/);
  assert.match(elements.main.innerHTML,/Brier score/);
  assert.match(elements.main.innerHTML,/2026 NFL full season record/);
  const [headline,history]=elements.main.innerHTML.split('<details class="research-disclosure" data-disclosure="model-history">');
  assert.ok(history,'alternate model history should be collapsed by default');
  assert.match(headline,/<small>Player props<\/small><strong>1–1<\/strong>/);
  assert.doesNotMatch(headline,/38–23|Challenger record/);
  assert.match(history,/Overlapping alternative forecasts are excluded from the season totals/);
});

test('NFL version history shows live decisions while retaining reconstructed provenance', () => {
  const {elements,performance}=harness(()=>{throw Error('no fetch');});
  const empty={count:0,record:{},win_rate:null};
  const live={count:2,game_count:1,record:{won:1,pending:1},win_rate:1,
    model_version:'nfl-original<unsafe>',first_saved_at:1792500000,last_saved_at:1792500000};
  performance({season_record:{season:2026,props:{...live,historical_count:5,locked:live,archived:empty,replayed:empty,games:[]},
    games:{rows:[],winner:empty,spread:empty,total:empty},weekly:[],model_versions:{props:[live],games:[]}}});
  assert.match(elements.main.innerHTML,/Live records by model version/);
  assert.match(elements.main.innerHTML,/nfl-original&lt;unsafe&gt;/);
  assert.match(elements.main.innerHTML,/1 pending/);
  assert.match(elements.main.innerHTML,/Reconstructed rankings and replays are shown in provenance/);
  assert.match(elements.main.innerHTML,/5 reconstructed and 2 locked/);
  assert.doesNotMatch(elements.main.innerHTML,/<unsafe>|NaN|undefined/);
});
