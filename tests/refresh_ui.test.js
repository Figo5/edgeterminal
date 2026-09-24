const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../edgeterminal/web/app.js'), 'utf8')
  .replace(/init\(\);\s*$/, '');

function harness(fetch) {
  const elements = {
    refreshButton: {disabled: false, textContent: '↻ Refresh', attrs: {},
      setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; }},
    lastUpdated: {textContent: ''},
    mobileUpdated: {textContent: ''},
    toast: {textContent: '', classList: {add() {}, remove() {}}},
    main: {innerHTML: '', scrollTop: 0},
  };
  const scope = {document: {getElementById: id => elements[id]}, fetch,
    setTimeout: () => 1, clearTimeout: () => {}, console, Date};
  vm.runInNewContext(source + '\nglobalThis.__test = {refreshProduct, state};', scope);
  return {elements, ...scope.__test};
}

const response = body => ({ok: true, json: async () => body});
const board = name => ({games: [{id: 'nfl-2026-reg-03-atl-gb',
  away_team: name, home_team: 'Green Bay Packers', kickoff_utc: '2026-09-25T00:15Z',
  designation: 'PASS', projection: null, market: {}}]});

test('Refresh disables immediately, ignores a second click, then reloads stored board', async () => {
  let finishPost;
  const calls = [];
  const {elements, refreshProduct} = harness((url, options) => {
    calls.push([url, options?.method || 'GET']);
    if (url === '/api/research/refresh') return new Promise(resolve => { finishPost = resolve; });
    if (url === '/api/product/nfl') return Promise.resolve(response(board('New stored team')));
    throw Error(`unexpected request: ${url}`);
  });
  const first = refreshProduct();
  await refreshProduct();
  assert.equal(elements.refreshButton.disabled, true);
  assert.equal(elements.refreshButton.textContent, '↻ Refreshing…');
  assert.equal(elements.refreshButton.attrs['aria-busy'], 'true');
  assert.equal(calls.filter(([url]) => url === '/api/research/refresh').length, 1);
  finishPost(response({ok: true, did_work: false, steps: {}, updated_at: '2026-09-24T15:00:00+00:00'}));
  await first;
  assert.equal(elements.refreshButton.disabled, false);
  assert.equal(elements.lastUpdated.textContent, 'Updated just now');
  assert.equal(elements.mobileUpdated.textContent, 'Updated just now');
  assert.match(elements.main.innerHTML, /New stored team/);
  assert.deepEqual(calls.map(([url]) => url), ['/api/research/refresh', '/api/product/nfl']);
});

test('Refresh failure shows a clean message and still reloads stored data', async () => {
  const {elements, refreshProduct, state} = harness(url => {
    if (url === '/api/research/refresh') return Promise.resolve({ok: false, status: 503,
      statusText: 'Unavailable', json: async () => ({detail: 'secret provider response'})});
    if (url === '/api/product/nfl') return Promise.resolve(response(board('Still stored team')));
    throw Error(`unexpected request: ${url}`);
  });
  state.lastSuccessfulRefresh = 1790250000;
  await refreshProduct();
  assert.equal(elements.refreshButton.disabled, false);
  assert.match(elements.lastUpdated.textContent, /^Refresh failed · Last successful update:/);
  assert.equal(elements.mobileUpdated.textContent, elements.lastUpdated.textContent);
  assert.doesNotMatch(elements.lastUpdated.textContent, /secret/);
  assert.match(elements.main.innerHTML, /Still stored team/);
});
