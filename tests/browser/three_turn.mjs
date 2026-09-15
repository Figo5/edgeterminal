// Rendered-browser test of the three-turn conversation.
//
// Drives the real UI in Chromium: types into the chat box, clicks send, and
// reads what is painted. /api/chat is intercepted and answered by a mock that
// records exactly what the browser sent, so no model or provider is called.
// Playwright is not a project dependency. Point NODE_PATH at an install that
// has Chromium, e.g.:
//   NODE_PATH=$HOME/.hermes/hermes-agent/node_modules \
//     node tests/browser/three_turn.mjs
// Requires EdgeTerminal running on 8799 (./tools/edgeterminal.sh start).
import { createRequire } from 'node:module';
// createRequire honours NODE_PATH; bare ESM `import` does not.
const { chromium } = createRequire(import.meta.url)('playwright');

const BASE = process.env.ET_URL || 'http://127.0.0.1:8799';
const THU = 'thu-lions-bills';
const SUN = 'sun-vikings-bears';

const sent = [];
let turn = 0;

// Server-side behaviour is already covered by the pytest suite; here the mock
// stands in for it so the test isolates the BROWSER half of the turn boundary:
// does the page carry `context` forward, and does it render what comes back?
function reply(body) {
  turn += 1;
  const prior = body.context || null;
  // Turn 1 resolves Thursday. Later turns must arrive carrying it.
  // A server that ignored `context` would fall back to the dashboard selection,
  // which is exactly the reported bug.
  const event_id = turn === 1 ? THU : (prior && prior.event_id) || body.event_id;
  const market = turn === 1 ? 'player_anytime_td'
    : (/receiving/i.test(body.message) ? 'player_reception_yards'
       : (prior && prior.market) || null);
  void SUN;
  const name = event_id === THU ? 'Detroit Lions @ Buffalo Bills'
                                : 'Minnesota Vikings @ Chicago Bears';
  return {
    answer: `**${name}** — market: ${market}`,
    tool_trace: [{ tool: 'analyze_td_scorers', arguments: { event_id }, ok: true }],
    context: { conversation_id: 'c1', turn, event_id, event_name: name, market,
               selected_event_id: body.event_id || null, validated: true },
  };
}

const browser = await chromium.launch();
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

await page.route('**/api/chat', async (route) => {
  const body = route.request().postDataJSON();
  sent.push(body);
  await route.fulfill({ status: 200, contentType: 'application/json',
                        body: JSON.stringify(reply(body)) });
});

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);

// Select a game the way a user does: click a card. From here the frontend
// re-attaches that id to every chat payload, touched again or not.
const card = page.locator('[data-event]').first();
await card.click();
await page.waitForTimeout(800);

await page.click('#chatToggle');
await page.waitForTimeout(200);
const turns = [
  'Who might score a touchdown in the Thursday night game',
  'What about receiving yards in that game',
  'Show me the available prices and when they were updated, even if nothing qualifies',
];
for (const t of turns) {
  await page.fill('#chatInput', t);
  // requestSubmit() fires the real submit handler, exactly as the send button does.
  await page.locator('#chatForm').evaluate((f) => f.requestSubmit());
  await page.waitForTimeout(700);
}

const painted = await page.$$eval('#chatLog .bubble, .bubble',
  (els) => els.map((e) => e.textContent.trim()).filter(Boolean));

await browser.close();

const fail = [];
if (sent.length !== 3) fail.push(`expected 3 requests, got ${sent.length}`);
if (sent[1] && !sent[1].context) fail.push('turn 2 did not send context back');
if (sent[1]?.context?.event_id !== THU) fail.push(`turn 2 sent event ${sent[1]?.context?.event_id}`);
if (sent[2]?.context?.event_id !== THU) fail.push(`turn 3 sent event ${sent[2]?.context?.event_id}`);
if (sent[2]?.context?.market !== 'player_reception_yards')
  fail.push(`turn 3 lost the market: ${sent[2]?.context?.market}`);
// Every turn still carries the sticky dashboard selection; that is the point.
if (!sent[1]?.event_id || sent[1].event_id !== sent[0].event_id)
  fail.push('turn 2 should still attach the same dashboard selection');
const bot = painted.filter((t) => t.includes('—') || t.includes('market:'));
if (!bot.some((t) => t.includes('Detroit Lions @ Buffalo Bills')))
  fail.push('the page never rendered the Thursday game');
if (bot.some((t) => t.includes('Minnesota Vikings')))
  fail.push('the page rendered the Vikings game');
if (errors.length) fail.push(`page errors: ${errors.slice(0, 3).join(' | ')}`);

console.log('--- requests the browser actually sent ---');
sent.forEach((s, i) => console.log(
  `T${i + 1} msg=${JSON.stringify(s.message.slice(0, 44))} selection=${s.event_id} ` +
  `ctx.event=${s.context?.event_id ?? 'none'} ctx.market=${s.context?.market ?? 'none'}`));
console.log('--- rendered bot messages ---');
bot.forEach((t) => console.log('  ', t.slice(0, 90)));
if (fail.length) { console.log('\nFAIL:'); fail.forEach((f) => console.log(' -', f)); process.exit(1); }
console.log('\nPASS: rendered browser carried event and market across all three turns');
