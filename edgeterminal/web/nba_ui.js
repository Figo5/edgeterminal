/* Daily NBA research views. Rendering reads stored API responses only. */
(function(root){
"use strict";
const esc=v=>String(v??"—").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,n=1)=>v==null?'—':Number(v).toFixed(n);
const pct=v=>v==null?'—':`${(v*100).toFixed(1)}%`;
const time=v=>v?new Date(typeof v==='number'?v*1000:v).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZone:'America/New_York'})+' ET':'Not checked yet';
const market=v=>({points:'Points',rebounds:'Rebounds',assists:'Assists',threes:'3-pointers made',pra:'Points + rebounds + assists'}[v]||v||'—');
const availabilityText=v=>String(v||'Roster and availability were checked at capture. Absence from an injury report is not proof of health.').replaceAll('not_listed_on_roster_injury_feed','not listed with an injury in the roster feed');
const bookmaker=v=>({draftkings:'DraftKings',fanduel:'FanDuel',betmgm:'BetMGM'}[v]||v||'Unknown sportsbook');
const result=v=>({won:'Win',lost:'Loss',win:'Win',loss:'Loss',push:'Push',void:'Void',pending:'Pending'}[v]||'Pending');
const price=v=>v==null?'—':`${v>0?'+':''}${v}`;
const sign=v=>v==null?'—':`${v>0?'+':''}${num(v)}`;
const recordText=r=>(r?.won||r?.lost||r?.push)?`${r.won||0}–${r.lost||0}${r.push?`–${r.push}`:''}`:'No graded selections';
const note=r=>`${r?.won+r?.lost>0?pct(r.won/(r.won+r.lost))+' win rate · ':''}${r?.pending||0} pending · ${r?.void||0} void`;
const metric=(title,value,detail)=>`<div class="record-metric"><small>${esc(title)}</small><strong>${esc(value)}</strong><span>${esc(detail||'')}</span></div>`;
const probabilityNotes={
 historical_standardized_residual_cdf:'Estimated from historical forecast errors. Accuracy on current market lines is still being measured.',
 adaptive_residual_cdf:'Estimated from historical forecast errors. Accuracy on current market lines is still being measured.',
 exposure_gamma_poisson:'Includes uncertainty in the production rate at the projected minutes. Accuracy on current market lines is still being measured.',
 historical_validated_parametric:'Uses a count or normal distribution retained by historical validation. Accuracy on current market lines is still being measured.',
 actual_line_selected_policy_calibration:'Calibrated on earlier saved market lines. The original five selections and sides stay fixed; confidence can fall below 50%. Future accuracy is still being measured.',
 role_minutes_predictive_mixture:'Includes uncertainty in minutes and production, using prior observed workloads. The version passed saved-line validation; future accuracy is still being measured.'
};
const probabilityNote=r=>probabilityNotes[r.probability_method]||'Model probabilities are uncalibrated research estimates.';
const footer='<p class="source-note">Research estimates · PASS / RESEARCH_ONLY. Five distinct player/stat selections save before tipoff when supported source lines are available. Missing captures are never added after a game starts.</p>';

function props(game,detail=false){
 const rows=game.top_props||[];
 if(!rows.length)return `<p class="prop-empty">${esc(game.prop_message||'Waiting for five verified main player lines and sufficient history.')}</p>`;
 if(!detail)return `<ol class="brief-props">${rows.map(r=>`<li><span class="prop-rank">${r.rank}</span><span><strong>${esc(r.player)}</strong><small>${esc(market(r.market))}</small></span><b>${esc(r.side||r.stronger_side)} ${num(r.line)}</b><span class="prop-prob">${pct(r.confidence)}</span></li>`).join('')}</ol>`;
 return rows.map(r=>`<article class="selection-detail"><div class="selection-row"><div><strong>${r.rank}. ${esc(r.player)}</strong><small>${esc(r.team)} · ${esc(market(r.market))}</small></div><b>${esc(r.side||r.stronger_side)} ${num(r.line)}</b><span>Projection ${num(r.projection)}</span><strong>${pct(r.confidence)}<small>Model probability</small></strong><span class="result ${esc(r.result||'pending')}">${result(r.result)}${r.actual!=null?` · ${num(r.actual,0)}`:''}</span></div><div class="selection-context"><span>${esc(bookmaker(r.sportsbook))} ${price(r.price)} · quote ${time(r.observed_at||r.quote_observed_at)}</span><span>Saved ${time(r.selected_at)}</span></div><details><summary>Inputs and uncertainty</summary><p>${esc(availabilityText(r.availability_note))}</p><p>Expected minutes ${num(r.expected_minutes)} · history ${r.sample_size??r.n??'—'} games · spread of outcomes ${num(r.sd)}</p><p>${esc(probabilityNote(r))} These are statistical results, without sportsbook settlement rules, bankroll or stakes.</p>${r.model_version?`<p>Saved model: ${esc(r.model_version)}</p>`:""}</details></article>`).join('');
}

function gameCard(g){
 const p=g.projection||{},m=g.market||{};
 return `<article class="product-game"><div class="product-game-top"><span>${time(g.tipoff)}</span><span>${esc(g.status||'Scheduled')}</span></div><h3>${esc(g.away_team)} at ${esc(g.home_team)}</h3><div class="product-score"><div><span>${esc(g.away_team)}</span><strong>${num(p.away_score)}</strong><small>${pct(p.away_win_probability)} win</small></div><div><span>${esc(g.home_team)}</span><strong>${num(p.home_score)}</strong><small>${pct(p.home_win_probability)} win</small></div></div>${!g.projection?`<p class="source-note">${esc(g.forecast_message||'Waiting for sufficient sourced history.')}</p>`:''}<dl class="forecast-grid"><div><dt>Model home spread</dt><dd>${p.home_margin==null?'—':`${esc(g.home_team)} ${sign(-p.home_margin)}`}</dd></div><div><dt>Model total</dt><dd>${num(p.total)}</dd></div><div><dt>ESPN comparison spread</dt><dd>${sign(m.home_spread)}</dd></div><div><dt>ESPN comparison total</dt><dd>${num(m.total)}</dd></div></dl><p class="source-note">Spread selection: ${esc(spreadPick(g))}</p>${g.projection?`<p class="source-note">${forecastNote(g)}</p>`:""}<div class="top-five-heading"><h4>Top 5 player props</h4><span>Model probability</span></div>${props(g)}<div class="product-foot"><span>${g.top_props?.length?'Five selections saved before tipoff':'Awaiting supported inputs'}</span><button class="backlink" data-nba-game="${esc(g.id)}">View matchup →</button></div></article>`;
}

function board(d){
 const r=d.record||{};
 return `<div class="page-head board-heading"><div><h1>NBA projections</h1><p>${esc(d.season||'2026-27')} · Daily scores, game lines and player props.</p></div><div class="board-controls"><label for="nbaDate">NBA slate date</label><div class="nba-date-controls"><button class="btn" data-nba-date="previous" aria-label="Previous NBA date">←</button><input type="date" id="nbaDate" value="${esc(d.date)}"><button class="btn" data-nba-date="next" aria-label="Next NBA date">→</button><button class="backlink" data-nba-date="today">Today</button></div><span>Dates and times in Eastern Time</span></div></div><div class="research-strip"><span>Season player props</span><strong>${recordText(r)}</strong><span>${note(r)}</span><button class="backlink" data-section="performance">View record →</button></div>${d.notice?`<div class="notebox">${esc(d.notice)} <button class="backlink" data-section="research">About & data →</button></div>`:''}<section class="day-group"><h2>${esc(d.date)} <span>${(d.games||[]).length} games</span></h2><div class="product-board">${(d.games||[]).map(gameCard).join('')}</div>${!d.games?.length?`<div class="notebox">${d.schedule_checked_at?'No NBA regular season games are stored for this date.':'The schedule has not been checked for this date yet. The built-in refresh will load upcoming dates.'}</div>`:''}</section><p class="source-note">Schedule checked ${time(d.schedule_checked_at)} · Game prices from ESPN · Player lines from SportsGameOdds.</p>${footer}`;
}

function forecastNote(g){
 const r=g.forecast_refresh||{};
 return g.projection?`Forecast saved ${time(g.forecast_at)}. ${esc(r.message||'Original forecast and comparison lines stay fixed.')}${r.state==='stale'?' Inputs need a fresh check; the previous supported forecast remains visible.':''}`:esc(g.forecast_message||'Waiting for sufficient source history.');
}
function spreadPick(g){
 const margin=g.projection?.home_margin,line=g.market?.home_spread;
 if(margin==null||line==null)return 'Waiting for a sourced line';
 return `${margin+line>=0?g.home_team:g.away_team} ${sign(margin+line>=0?line:-line)}`;
}
function spreadStatus(d){
 if(!d.game_model)return '';
 const model=d.game_model,learning=d.game_learning||{};
 return `<section class="performance-block"><h2>Spread model</h2><p>Active game model ${esc(model.version)}.</p><p>${esc(learning.message||'Automatic source collection and validated model checks run with the background worker.')}</p><p>Every game with supported inputs and a sourced line keeps a spread selection. Pregame forecasts update automatically; the latest saved forecast before tipoff becomes its record. Past forecasts and grades remain available.</p><p class="source-note">Availability is recorded with each forecast. Numeric player-impact changes require separate validation; no improvement is assumed from collecting more data.</p></section>`;
}
function runtimeNote(d){
 return d.runtime?.requires_awake_mac===false?'Refreshes run on the always-on host independently of your Mac. Your phone reads the same stored forecasts and grades.':'This Mac must be awake and online for the local worker to refresh. An always-on host is needed for updates while it sleeps.';
}
function totalsStatus(d){
 const t=d.totals_learning;if(!t?.enabled)return '';
 const name=t.active?.version||'Current scoring baseline';
 return `<section class="performance-block"><h2>Totals model</h2><p>${esc(name)}.</p><p>${esc(t.message||'Waiting for verified future-game validation.')}</p><p>Last automatic check: ${time(t.checked_at)} · Next automatic check: ${time(t.next_due_at)}.</p><p>Every supported game keeps a totals pick when a sourced line is available. Pregame forecasts and final-result grading continue automatically. New totals versions require separate chronological validation.</p>${learningRunner(t.runner)}</section>`;
}
function matchup(d){
 const g=d.game,p=g.projection||{},m=g.market||{};
 return `<button class="backlink" data-section="nfl">← NBA board</button><div class="page-head"><div><h1>${esc(g.away_team)} at ${esc(g.home_team)}</h1><p>${time(g.tipoff)} · ${esc(g.venue||'Venue unavailable')}</p></div></div><div class="matchup-score"><div class="score-label">PROJECTED SCORE</div><div class="score-pair"><div><span>${esc(g.away_team)}</span><strong>${num(p.away_score)}</strong><small>${pct(p.away_win_probability)} win</small></div><div class="score-center">MODEL</div><div><span>${esc(g.home_team)}</span><strong>${num(p.home_score)}</strong><small>${pct(p.home_win_probability)} win</small></div></div></div><section class="performance-block"><h2>Game markets</h2><div class="record-grid nba-market-summary">${metric('Spread selection',spreadPick(g))}${metric('Model home spread',sign(p.home_margin==null?null:-p.home_margin))}${metric('Model total',num(p.total))}${metric('ESPN comparison spread',sign(m.home_spread),`Home ${price(m.home_price)} · Away ${price(m.away_price)}`)}${metric('ESPN comparison total',num(m.total),`Over ${price(m.over_price)} · Under ${price(m.under_price)}`)}</div><p class="source-note">${forecastNote(g)}</p><p class="source-note">Comparison lines observed ${time(m.comparison_observed_at||m.observed_at)} · ${esc(m.sportsbook||'No stored sportsbook quote')}</p></section><section class="prop-section"><h2>Top 5 player props</h2>${props(g,true)}</section><details class="research-disclosure"><summary>Latest observed ESPN lines</summary><p>Home spread ${sign(g.latest_market?.home_spread)} · total ${num(g.latest_market?.total)} · ${time(g.latest_market?.observed_at)}. These newer observations do not replace the comparison lines in the record.</p></details><details class="research-disclosure"><summary>Model and availability context</summary>${(p.notes||[]).map(n=>`<p>${esc(n)}</p>`).join('')}<p>Model ${esc(p.game_component_version||p.model_version)}. Versioned parameters; source history is available before this forecast. Rest and rotation changes can affect minutes.</p></details>${footer}`;
}

function versionHistory(versions,options={}){
 const props=versions?.props||[],games=versions?.games||[],totals=versions?.totals||[];
 if(!props.length&&!games.length&&!totals.length)return '';
 const saved=row=>row.first_saved_at==null?'Dates not recorded':`${time(row.first_saved_at)}${row.last_saved_at!=null&&row.last_saved_at!==row.first_saved_at?` – ${time(row.last_saved_at)}`:''}`;
 const model=row=>`<strong>${esc(row.model_version||'Unrecorded version')}</strong>${row.game_component_version?`<small>Game component: ${esc(row.game_component_version)}</small>`:''}`;
 const cell=summary=>{
  const r=summary?.record||{},details=[summary?.win_rate!=null?`${pct(summary.win_rate)} win rate`:'',
   ...['pending','void','pass','unavailable'].filter(key=>r[key]).map(key=>`${r[key]} ${key}`)].filter(Boolean);
  return `<td><strong>${recordText(r)}</strong><small>${esc(details.join(' · '))}</small></td>`;
 };
 const totalsHtml=totals.length?`<h3>Totals component</h3><p class="source-note">One saved total per game, attributed to its totals component. Legacy rows retain their original provenance.</p><div class="records-scroll"><table class="weekly-records"><thead><tr><th scope="col">Saved totals model</th><th scope="col">Saved record dates</th><th scope="col">Games</th><th scope="col">Record</th></tr></thead><tbody>${totals.map(row=>`<tr><th scope="row">${model(row)}</th><td>${esc(saved(row))}</td><td>${esc(row.game_count)}</td>${cell(row)}</tr>`).join('')}</tbody></table></div>`:'';
 return `<section class="performance-block model-version-records"><h2>Live records by model version</h2><p class="source-note">Each saved live selection counts once under its original model version. Game records use the selected pregame forecast and its saved lines.${options.historical?' Reconstructed rankings and replays are shown in provenance below.':''}</p>${props.length?`<h3>Player props</h3><div class="records-scroll"><table class="weekly-records"><thead><tr><th scope="col">Saved model</th><th scope="col">Saved record dates</th><th scope="col">Selections</th><th scope="col">Record</th></tr></thead><tbody>${props.map(row=>`<tr><th scope="row">${model(row)}</th><td>${esc(saved(row))}</td><td>${esc(row.count)}<small>${esc(row.game_count)} games</small></td>${cell(row)}</tr>`).join('')}</tbody></table></div>`:''}${games.length?`<h3>Game forecasts</h3><div class="records-scroll"><table class="weekly-records"><thead><tr><th scope="col">Saved model</th><th scope="col">Saved record dates</th><th scope="col">Games</th><th scope="col">Winners</th><th scope="col">Spread</th><th scope="col">Total</th></tr></thead><tbody>${games.map(row=>`<tr><th scope="row">${model(row)}</th><td>${esc(saved(row))}</td><td>${esc(row.game_count)}</td>${cell(row.winner)}${cell(row.spread)}${cell(row.total)}</tr>`).join('')}</tbody></table></div>`:''}${totalsHtml}</section>`;
}

function record(d){
 return `<div class="page-head"><div><h1>${esc(d.season||'2026-27')} NBA full season record</h1><p>Predictions across all saved live model versions, graded from verified final results.</p></div></div><div class="record-grid">${[['Winners',d.winners],['Against the spread',d.spread],['Over / under',d.total],['Player props',d.props]].map(([t,r])=>metric(t,recordText(r),note(r))).join('')}</div><p class="source-note">Historical evaluation is separate from this prospective record. Missing pregame lines or forecasts are excluded; pushes and voids do not count in win rate.</p>${versionHistory(d.model_versions)}<section class="performance-block"><h2>Daily results</h2>${(d.days||[]).map(day=>`<div class="coverage-row"><button class="backlink" data-nba-date="${esc(day.date)}">${esc(day.date)}</button><span>${recordText(day.props)} · ${note(day.props)}</span></div>`).join('')||'<p>No saved NBA selections yet. The record starts with the first supported pregame capture.</p>'}</section>${footer}`;
}

function learningRunner(r){
 if(!r)return '';
 const worker=r.worker||{},jobs=r.jobs||[];
 const states={queued:'Queued',running:'Running',completed:'Completed',failed:'Failed',interrupted:'Interrupted',deferred:'Deferred',idle:'Idle',stale:'Check overdue',not_checked:'Not checked yet'};
 const duration=value=>typeof value!=='number'||!Number.isFinite(value)||value<0?'Not recorded':`${Math.floor(value/60)}m ${Math.floor(value%60)}s`;
 const task=row=>`${row.component==='NBA_SPREAD'?'NBA game model':row.component==='NBA_TOTAL'?'NBA totals':row.component||'Model'} ${row.kind==='diagnostics'?'diagnostics':row.kind==='retuning'?'model check':row.kind||'check'}`;
 return `<div class="learning-runner"><h3>Separate learning worker</h3><p>Capture and grading continue on their own schedule while learning runs from archived inputs.</p><p>${r.needs_attention?'<strong>Learning needs review.</strong> ':''}Worker: ${esc(states[worker.state]||worker.state||'Not checked yet')} · Last checked ${time(worker.last_heartbeat_at)}.</p>${jobs.length?`<div class="records-scroll"><table class="weekly-records"><thead><tr><th scope="col">Task</th><th scope="col">State</th><th scope="col">Started</th><th scope="col">Elapsed</th></tr></thead><tbody>${jobs.slice(0,6).map(row=>`<tr><th scope="row">${esc(task(row))}</th><td>${esc(states[row.status]||row.status||'Not checked yet')}${row.message?`<small>${esc(row.message)}</small>`:''}</td><td>${time(row.started_at)}</td><td>${esc(duration(row.elapsed_seconds))}</td></tr>`).join('')}</tbody></table></div>`:'<p>No learning jobs have been recorded yet.</p>'}<p class="source-note">A queued or running retry does not resolve a failed learning check. Only a completed successful check does.</p></div>`;
}

function learningStatus(d){
 if(!d?.enabled)return '';
 const diagnostic=d.diagnostics||{},retuning=d.retuning||{},detail=retuning.detail||{};
 const names={not_checked:'First check due',waiting:'Waiting for sufficient verified data',rejected:'Current model retained',activated:'Validated update activated',retained:'Current model retained',error:'Check needs review',deferred:'Waiting until pregame capture is clear',ok:'Complete'};
 const reason=value=>{const [kind,m,score]=String(value).split(':');if(kind==='market_regression')return `${market(m).replaceAll('_',' ')}: ${score==='mae'?'prediction error increased':score==='brier'?'probability error increased':'probability fit worsened'}`;return ({insufficient_probability_improvement:'The improvement in probabilities was too small',uncertain_game_group_improvement:'The improvement was not reliable across separate games',insufficient_validation_rows:'More graded selections are needed',insufficient_validation_games:'More completed games are needed',no_fully_graded_locked_games:'Waiting for complete verified game results',active_baseline_overlaps_validation_window:'Waiting for games after the active model’s training cutoff',fewer_than_ten_new_whole_games:'Waiting for ten new completed games'})[kind]||String(value).replaceAll('_',' ')};
 const reasons=detail.reasons?.length?detail.reasons.slice(0,4).map(reason).join('; ')+(detail.reasons.length>4?'; additional checks also did not pass.':''):detail.reason?reason(detail.reason):'';
 return `<section class="performance-block"><h2>Automatic model learning</h2><p>Player inputs update from completed games. ${esc(d.schedule)}</p><p>Latest model check: <strong>${esc(names[retuning.status]||retuning.status)}</strong> · ${time(retuning.checked_at)}.</p>${reasons?`<p>${esc(reasons)}</p>`:''}<p>Next model check: ${time(retuning.next_due_at)} · Nightly diagnostics: ${esc(names[diagnostic.status]||diagnostic.status)} · ${time(diagnostic.checked_at)}.</p>${diagnostic.detail?.integrity_rollback?'<p>A model integrity check restored the prior verified version.</p>':''}${learningRunner(d.runner)}<p class="source-note">Updates require separate chronological fit, calibration and validation games, verified pregame lines, and better held-out probabilities without market-level regression. Every version keeps its cutoff; saved predictions and grades stay fixed. Future improvement is measured, never guaranteed.</p></section>`;
}

function about(d){
 const model=d.model||{},b=d.budget||{},e=d.evaluation||{};
 return `<div class="page-head"><div><h1>NBA · About & data</h1><p>Current source checks, model inputs and grading.</p></div></div><section class="performance-block"><h2>Source & grading checks</h2>${(d.checks||[]).map(c=>`<div class="coverage-row"><strong>${esc(c.label)}</strong><div><p>${esc(c.message||c.status)}</p><small>${time(c.checked_at)}${c.next_at?` · Next due ${time(c.next_at)}`:''}</small></div></div>`).join('')||'<p>The built-in refresh has not checked NBA yet.</p>'}<p>${runtimeNote(d)} Page visits request due work through the same lock and cadence guards.</p><p>On iPhone, open your hosted EdgeTerminal link in Safari and choose Share → Add to Home Screen.</p></section>${spreadStatus(d)}${totalsStatus(d)}${learningStatus(d.learning)}<section class="performance-block"><h2>Historical evaluation</h2><p>Model ${esc(model.version)} · version training cutoff ${esc(model.cutoff)}.</p><p>${esc(e.summary||'Historical inputs are loading. No validation result is claimed until the chronological evaluation is complete.')}</p>${e.markets?`<div class="records-scroll"><table class="weekly-records"><thead><tr><th>Market</th><th>Holdout forecasts</th><th>MAE</th></tr></thead><tbody>${Object.entries(e.markets).map(([m,r])=>`<tr><th>${esc(market(m))}</th><td>${r.n??r.count??0}</td><td>${num(r.mae,2)}</td></tr>`).join('')}</tbody></table></div>`:''}<p class="source-note">${esc(e.note||'This is a baseline research model. Prediction accuracy does not establish profitability or calibrated market probabilities.')}</p><details><summary>Model fingerprint</summary><p class="nba-fingerprint">${esc(model.fingerprint)}</p></details></section><section class="performance-block"><h2>Source coverage & limits</h2><p>${esc(d.source_note||'ESPN supplies schedules, historical game logs, current rosters and final box scores. NBA league endpoints were unavailable in this environment; ESPN is identified as the source.')}</p><p>Local shared capture budget: ${b.requests_30d??0}/${b.shared_month_limit??600} requests per 30 days. NBA: ${b.nba_requests_30d??0}/${b.nba_month_limit??48}, at most ${b.nba_day_limit??2} batches per rolling day.</p>${b.blocked_reason?`<p>Prop capture waiting: ${esc(b.blocked_reason.replaceAll('_',' '))}.</p>`:''}<p>Player markets: points, rebounds, assists, three-pointers made, and points + rebounds + assists. Missing lines wait; missing final statistics stay pending. Confirmed DNP/inactive results are statistical voids. Corrections retain earlier evidence.</p></section>${footer}`;
}

async function show(section,ctx){
 const path=section==='performance'?'/api/product/nba/performance':section==='research'?'/api/product/nba/status':`/api/product/nba${ctx.state.nbaDate?'?date='+ctx.state.nbaDate:''}`;
 const d=await ctx.api(path);ctx.state.nbaData=d;
 ctx.main.innerHTML=section==='performance'?record(d):section==='research'?about(d):board(d);
 if(d.date)ctx.state.nbaDate=d.date;
 return d;
}
root.EdgeNBA={board,matchup,record,about,show,learningStatus,versionHistory};
if(typeof module!=='undefined')module.exports=root.EdgeNBA;
})(typeof window!=='undefined'?window:globalThis);
