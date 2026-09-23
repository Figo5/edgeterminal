"use strict";

// chatContext: this tab's conversation state, echoed back each turn (see sendChat).
const state = { sport: "NFL", section: "games", dashboard: null, config: null, eventId: null, history: [], chatContext: null, window: "all" };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "—").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const pct = (v) => v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`;
const odds = (v) => v == null ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(0)}`;
const num = (v, d=1) => v == null ? "—" : Number(v).toFixed(d);
const fmtPct = (v) => v == null ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(1)} pts`;
const when = (v) => { if (!v) return "—"; const d = typeof v === "number" ? new Date(v*1000) : new Date(v); return isNaN(d) ? "—" : `${d.toLocaleString("en-US", {month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:"America/New_York"})} ET`; };
const stateClass = (s="") => ({"OFFICIAL PICK":"pick","LEAN":"lean","WATCHLIST":"watch","WAIT FOR PRICE":"wait","NO EDGE":"noedge","PASS":"pass","STALE":"stale"}[s] || "pass");
const tag = (s) => `<span class="tag ${stateClass(s)}">${esc(s)}</span>`;

async function api(path, options={}) {
  const response = await fetch(path, {headers:{"Content-Type":"application/json"}, ...options});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.answer || `${response.status} ${response.statusText}`);
  return body;
}
function toast(message, bad=false) { const el=$("toast"); el.textContent=message; el.classList.remove("hidden"); el.style.borderColor=bad?"var(--bad)":"var(--line2)"; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.classList.add("hidden"),4500); }

function renderNavigation() {
  const sections=[["games","NFL Slate",state.dashboard?.upcoming?.length],["performance","Model record",null],["data","Data health",null]];
  $("sections").innerHTML=sections.map(([id,label,count])=>`<button data-section="${id}" class="${id===state.section?"on":""}"><span>${label}</span>${count==null?"":`<span class="count">${count}</span>`}</button>`).join("");
}
function providerPanel() {
  const rows=state.dashboard?.providers||[];
  $("providerList").innerHTML=rows.length?rows.map(p=>`<div class="prov" title="${esc(p.detail)}"><i class="dot ${p.is_demo?"demo":p.ok?"ok":"bad"}"></i>${esc(p.kind)} · ${esc(p.name)}</div>`).join(""):`<div class="prov"><i class="dot bad"></i>not checked</div>`;
  $("providerDots").innerHTML=rows.length?rows.filter(p=>["odds","stats","injuries","weather","news"].includes(p.kind)).map(p=>`<i class="dot ${p.is_demo?"demo":p.ok?"ok":"bad"}" title="${esc(p.kind)}: ${esc(p.detail)}"></i>`).join(""):`<i class="dot bad"></i>`;
}
function kickoffWindow(e) {
  const date = new Date(e.start_time);
  const weekday = new Intl.DateTimeFormat("en-US", {weekday:"short",timeZone:"America/New_York"}).format(date);
  const hour = Number(new Intl.DateTimeFormat("en-US", {hour:"numeric",hourCycle:"h23",timeZone:"America/New_York"}).format(date));
  if (weekday === "Thu") return "thu";
  if (weekday === "Sun" && hour < 16) return "early";
  if (weekday === "Sun" && hour < 19) return "late";
  if (weekday === "Sun") return "snf";
  if (weekday === "Mon") return "mnf";
  return "other";
}
function boardQuote(e, market, selection) {
  return (e.markets||[]).find(m=>m.market===market && m.selection===selection);
}
function gamesTable(rows) {
  const windows=[["all","All"],["thu","Thursday"],["early","Sunday early"],["late","Sunday late"],["snf","SNF"],["mnf","MNF"]];
  const shown=state.window==="all"?(rows||[]):(rows||[]).filter(e=>kickoffWindow(e)===state.window);
  const filters=`<div class="board-filters" role="group" aria-label="Kickoff window">${windows.map(([id,label])=>`<button type="button" data-window="${id}" class="${state.window===id?"on":""}">${label}</button>`).join("")}</div>`;
  const cards=shown.map(e=>{
    const status=String(e.status||"").toLowerCase();
    const final=["status_final","final","completed","post","closed"].includes(status);
    const started=final||status==="unverified past kickoff"||new Date(e.start_time).getTime()<=Date.now();
    const p=e.projection||{}, spread=boardQuote(e,"spread",e.home_team), total=boardQuote(e,"total","Over");
    const ml=boardQuote(e,"moneyline",e.home_team);
    const modelSide=p.margin==null?"—":Math.abs(p.margin)<0.05?"Pick'em":`${p.margin>=0?e.home_team:e.away_team} -${num(Math.abs(p.margin))}`;
    const gap=p.margin==null||!spread?"—":Math.abs(p.margin+Number(spread.line))<0.05?"0.0 pts":`${p.margin+Number(spread.line)>=0?e.home_team:e.away_team} ${num(Math.abs(p.margin+Number(spread.line)))} pts`;
    const quote=m=>m?`${m.line==null?"":`${num(m.line)} · `}${odds(m.price)} · ${esc(m.book)}`:"unavailable";
    const checked=Math.min(...(e.markets||[]).filter(m=>m.captured_at).map(m=>Number(m.captured_at)));
    const books=Math.max(0,...(e.markets||[]).map(m=>Number(m.books_quoted)||0));
    const marketHtml=started?`<div class="game-card-bottom">Game started · current prices hidden</div>`:
      `<div class="game-market"><div><small>HOME SPREAD</small><span>${quote(spread)}</span></div><div><small>TOTAL</small><span>${quote(total)}</span></div><div><small>HOME MONEYLINE</small><span>${quote(ml)}</span></div></div>
       <div class="game-card-bottom"><span>${books?`Spread gap toward ${esc(gap)}`:"No current verified price"}</span><span>${books?`${books} ${books===1?"book":"books"} · Checked ${Number.isFinite(checked)?when(checked):"unavailable"}`:"Refresh data to retry"}</span></div>`;
    return `<button type="button" class="game-card" data-event="${esc(e.id)}"><div class="game-card-top"><time>${when(e.start_time)}</time><span class="tag pass">${final?"FINAL":started?"STARTED":"PASS"}</span></div>
      <div class="game-score"><small>${started?"PREGAME PROJECTION":"PROJECTED SCORE"}</small><div><span>${esc(e.away_team)}</span><strong>${num(p.proj_away)}</strong></div><div><span>${esc(e.home_team)}</span><strong>${num(p.proj_home)}</strong></div></div>
      <div class="game-model"><div><small>MODEL SPREAD</small><b>${esc(modelSide)}</b></div><div><small>MODEL TOTAL</small><b>${num(p.total)}</b></div><div><small>HOME WIN</small><b>${pct(p.home_win_prob)}</b></div></div>
      ${marketHtml}
      ${e.projection_error?`<div class="warnbox">${esc(e.projection_error)}</div>`:""}</button>`;
  }).join("");
  const counts=(rows||[]).flatMap(e=>(e.markets||[]).map(m=>Number(m.books_quoted)||0));
  const oneBook=counts.length>0&&counts.every(count=>count<=1);
  const week=Number((rows||[])[0]?.id?.split("-")[3]);
  return `<div class="board-head"><div><h1>${Number.isInteger(week)&&week>0?`Week ${week}`:"Weekly"} NFL board</h1><p>${(rows||[]).length} games · quantitative model · observed prices</p></div><span class="badge">Research only</span></div>
    <div class="board-note">NFL pick thresholds are not validated. Upcoming games remain PASS. Forward shadow grading has not started.${oneBook?" Current prices come from one book per game; this is not a multi-book comparison.":""}</div>${filters}
    <div class="board-grid">${cards||`<div class="notebox">${(rows||[]).length?"No games in this kickoff window.":"No current week schedule. Refresh data to retry."}</div>`}</div>`;
}
function dataHealth(d) {
  const oddsSource=d.last_refresh?.sports?.NFL?.odds_source||"unavailable";
  return `<section class="panel"><div class="panelhead"><h2>Data health</h2></div><div class="panelbody"><p>Schedule: ESPN · ${(d.upcoming||[]).length} games</p><p>Market: ${esc(oddsSource)} · last refresh ${when(d.last_refresh?.at)}</p><p>Model: ${esc(state.config?.model_version)} · research only</p>${(d.providers||[]).map(p=>`<div class="srcrow"><b>${esc(p.kind)}</b><span>${esc(p.name)}</span><span>${p.ok?"available":"unavailable"}</span></div>`).join("")}</div></section>`;
}
async function performancePage() {
  $("main").innerHTML='<div class="loading">Loading model record…</div>';
  try {
    const report=await api("/api/model-report");
    const r=report.historical?.["2025"]||{},m=r.baselines?.market_close||{};
    const metric=(label,value,sub)=>`<div class="record-metric"><small>${label}</small><strong>${value}</strong><span>${sub}</span></div>`;
    const interval=(v)=>Array.isArray(v)?`${pct(v[0])}–${pct(v[1])}`:"—";
    const buckets=(rows)=>`<table><thead><tr><th>Gap bucket</th><th>N</th><th>W</th><th>L</th><th>Push</th><th>Rate</th><th>95% interval</th></tr></thead><tbody>${(rows||[]).map(b=>`<tr><td>${esc(b.bucket)} pts</td><td>${b.n}</td><td>${b.wins}</td><td>${b.losses}</td><td>${b.pushes}</td><td>${pct(b.hit_rate)}</td><td>${interval(b.hit_rate_ci95)}</td></tr>`).join("")}</tbody></table>`;
    const seasons=["2023","2024","2025"].map(year=>{const x=report.historical?.[year]||{},close=x.baselines?.market_close||{};return `<tr><td>${year}</td><td>${x.n??"—"}</td><td>${num(x.margin_mae)}</td><td>${num(close.margin_mae)}</td><td>${num(x.total_mae)}</td><td>${num(close.total_mae)}</td><td>${pct(x.ats_hit_rate)}</td><td>${pct(x.total_pick_hit_rate)}</td></tr>`}).join("");
    const marketBetter=["2023","2024","2025"].every(year=>{const x=report.historical?.[year],c=x?.baselines?.market_close;return x&&c&&c.margin_mae<x.margin_mae&&c.total_mae<x.total_mae});
    $("main").innerHTML=`<div class="board-head"><div><h1>Model record</h1><p>2023–2025 retrospective · 2025 detail below</p></div><span class="badge">Research only</span></div><div class="board-note">These are seen historical seasons, not independent validation. ${marketBetter?"The closing market beat this model on margin and total error in all three.":"Compare each model result with the closing market below."} Forward shadow grading has not started.</div>
      <div class="record-grid">${metric("GAMES",r.n??"—","2025 replay")}${metric("MARGIN MAE",num(r.margin_mae),`Market ${num(m.margin_mae)}`)}${metric("TOTAL MAE",num(r.total_mae),`Market ${num(m.total_mae)}`)}${metric("WIN BRIER",num(r.brier_score,3),`Market ${num(m.win_probability?.brier_score,3)} · N ${r.win_prob_n??0}`)}${metric("SPREAD ATS",pct(r.ats_hit_rate),`95% ${interval(r.ats_hit_rate_ci95)}`)}${metric("TOTAL PICKS",pct(r.total_pick_hit_rate),`95% ${interval(r.total_pick_hit_rate_ci95)}`)}</div>
      <section class="panel"><div class="panelhead"><h2>Historical seasons</h2></div><div class="panelbody table-scroll"><table><thead><tr><th>Season</th><th>Games</th><th>Margin MAE</th><th>Market</th><th>Total MAE</th><th>Market</th><th>Spread picks</th><th>Total picks</th></tr></thead><tbody>${seasons}</tbody></table></div></section>
      <div class="board-note">At -110, break-even is 52.38%. Recorded 2025 closing prices imply ${num(r.spread_closing_price_record?.roi_pct)}% spread ROI and ${num(r.total_closing_price_record?.roi_pct)}% total ROI. Price timing and attainability are unverified.</div>
      <section class="panel"><div class="panelhead"><h2>Forward shadow record</h2></div><div class="panelbody">Not started. Automatic NFL game prediction grading has not been implemented. Historical replay is reported separately.</div></section>
      <section class="panel"><div class="panelhead"><h2>Spread edge buckets</h2></div><div class="panelbody table-scroll">${buckets(r.spread_edge_buckets)}</div></section>
      <section class="panel"><div class="panelhead"><h2>Total edge buckets</h2></div><div class="panelbody table-scroll">${buckets(r.total_edge_buckets)}</div></section>`;
  } catch(e) { $("main").innerHTML=`<div class="warnbox">${esc(e.message)}</div>`; }
}
function renderDashboard() {
  renderNavigation(); providerPanel(); const d=state.dashboard;
  $("demoBadge").classList.toggle("hidden", !d.demo_mode && !(d.best_bets||[]).some(r=>r.is_demo));
  $("staleBadge").classList.toggle("hidden", !(d.odds_age_s > (state.config?.stale_odds_s||900)));
  $("lastUpdated").textContent=`last updated ${when(d.last_refresh?.at)}`;
  if(state.section==="performance") { performancePage(); return; }
  $("main").innerHTML=state.section==="data"?dataHealth(d):gamesTable(d.upcoming);
}

function teamComparison(detail) { const e=detail.event, a=detail.stats?.[e.away_team]||{}, h=detail.stats?.[e.home_team]||{}; const labels={epa_play:"EPA per play",success_rate:"Success rate",yards_per_play:"Yards per play",points_for:"Points for / game",points_against:"Points against / game",pressure_rate:"Pressure rate",neutral_pace:"Neutral pace",proe:"Pass rate over expected",games:"Games sampled"}; const keys=Object.keys(labels).filter(k=>a[k]!=null||h[k]!=null); const value=(k,v)=>v==null?"—":k==="games"?String(v):["success_rate","pressure_rate"].includes(k)?`${num(Number(v)*100)}%`:k==="proe"?`${num(v)} pts`:num(v,k==="epa_play"?3:1); const thin=Number(a.games)<8||Number(h.games)<8; return keys.length?(thin?`<div class="notebox">Scoring rates are regressed toward the league average while the season sample is thin.</div>`:"")+keys.map(k=>`<div class="cmp"><span class="l">${value(k,a[k])}</span><span class="m">${esc(labels[k])}</span><span class="r">${value(k,h[k])}</span></div>`).join(""):`<div class="notebox">Comparable team metrics are unavailable.</div>`; }
function sources(rows) { return `<div class="sources">${(rows||[]).map(s=>`<div class="srcrow"><span class="tag">${esc(s.trust)}</span>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>`:`<span>${esc(s.title)}</span>`}<span>${esc(s.publisher)}</span><span class="mono">${when(s.retrieved_at)}</span></div>`).join("")||"No research sources available."}</div>`; }
function marketTable(rows) {
  const games=(rows||[]).filter(r=>["spread","total","moneyline"].includes(r.market));
  return `<section class="panel"><div class="panelhead"><h2>Current game markets</h2><span class="sub">model and observed prices remain separate</span></div><div class="panelbody table-scroll">${games.length?`<table><thead><tr><th>Market</th><th>Side</th><th>Line</th><th>Book / price</th><th>Model</th><th>Market fair</th><th>Implied with margin</th><th>vs price</th><th>Status</th></tr></thead><tbody>${games.map(r=>`<tr><td>${esc(r.market)}</td><td>${esc(r.selection)}</td><td>${r.line==null?"—":num(r.line)}</td><td>${esc(r.book)} ${odds(r.price)}<div class="meta">Checked ${when(r.selected_quote_at)}</div><div class="meta">Book updated ${r.provider_quote_updated_at?when(r.provider_quote_updated_at):"unavailable"}</div></td><td>${pct(r.model_prob)}</td><td>${pct(r.market_fair_prob)}</td><td>${pct(r.implied_prob)}</td><td>${num(r.edge_pct)} pts</td><td>${tag(r.stale?"STALE":r.state)}</td></tr>`).join("")}</tbody></table>`:`<div class="notebox">MARKET DATA UNAVAILABLE. No fresh verified game-market quotes.</div>`}</div></section>`;
}
async function openEvent(id) {
  state.eventId=id;
  $("main").innerHTML='<div class="loading">Running deterministic game analysis…</div>';
  try {
    const d=await api(`/api/event/${encodeURIComponent(id)}`), e=d.event, p=d.projection||{};
    const injuries=(d.injuries||[]).filter(i=>!["active","probable"].includes(String(i.status||"").toLowerCase()));
    const weather=d.weather?`${esc(d.weather.conditions)} · ${num(d.weather.temp_f)}°F · wind ${num(d.weather.wind_mph)} mph · precip ${num(d.weather.precip_chance)}% · valid ${when(d.weather.valid_for)}`:"Forecast unavailable for this kickoff.";
    $("main").innerHTML=`<button class="backlink" id="backBtn">← Dashboard</button>
      <section class="panel"><div class="panelhead"><h2>${esc(e.sport)} Game Detail</h2><span class="sub">checked ${when(d.analyzed_at)}</span></div>
      <div class="panelbody"><div class="notebox">Projected score · research model, not a result</div>
      <div class="gamehead"><div class="teamcol"><span class="tn">${esc(e.away_team)}</span><span class="proj">${num(p.proj_away)}</span></div>
      <div class="teamcol"><span class="meta">at</span></div>
      <div class="teamcol"><span class="tn">${esc(e.home_team)}</span><span class="proj">${num(p.proj_home)}</span></div>
      <div class="teamcol"><span>${when(e.start_time)}</span><span class="meta">${esc(e.venue)}</span>
      <span class="mono">model total ${num(p.total)} · margin ${p.margin==null?"—":`${p.margin>0?"+":""}${num(p.margin)}`}</span></div></div>
      ${(d.warnings||[]).filter(w=>!String(w).includes("player props")).map(w=>`<div class="warnbox">${esc(w)}</div>`).join("")}</div></section>
      ${marketTable(d.recommendations)}
      <div class="grid g2"><section class="panel"><div class="panelhead"><h2>Team comparison</h2><span class="sub">away · metric · home</span></div><div class="panelbody">${teamComparison(d)}</div></section>
      <section class="panel"><div class="panelhead"><h2>Context</h2></div><div class="panelbody"><div class="notebox">Injuries and weather are sourced context. They do not change this NFL model's projected scores.</div>
      <h4>Weather</h4><div class="notebox">${weather}</div><h4>Injuries requiring review</h4>
      ${injuries.map(i=>`<div class="srcrow"><b>${esc(i.player)}</b><span>${esc(i.team)}</span><span>${esc(i.status)}</span><span>${esc(i.detail)}</span></div>`).join("")||"No unresolved injury rows available."}
      <h4>News</h4>${(d.news||[]).map(n=>`<div class="srcrow"><span class="tag">${esc(n.trust)}</span><b>${esc(n.title)}</b><span>${esc(n.summary)}</span></div>`).join("")||"No news rows available."}</div></section></div>
      <section class="panel"><div class="panelhead"><h2>Research sources</h2><span class="sub">timestamped provenance</span></div><div class="panelbody">${sources(d.sources)}</div></section>`;
    $("backBtn").onclick=()=>{state.eventId=null;renderDashboard()};
  } catch(e) {
    $("main").innerHTML=`<button class="backlink" onclick="location.reload()">← Dashboard</button><div class="warnbox">${esc(e.message)}</div>`;
  }
}

function md(text) { return esc(text).replace(/^### (.+)$/gm,"<h3>$1</h3>").replace(/^\*\*(.+?)\*\*/gm,"<b>$1</b>").replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/^[-•] (.+)$/gm,"<div>• $1</div>").replace(/\n\n/g,"</p><p>").replace(/\n/g,"<br>"); }
function addMessage(role,text,trace=[]) { const el=document.createElement("div"); el.className=`msg ${role}`; el.innerHTML=`<div class="who">${role==="user"?"You":"EdgeTerminal"}</div><div class="bubble"><p>${md(text)}</p>${trace.length?`<details class="trace"><summary>${trace.length} application tool call(s)</summary>${trace.map(t=>`<div>${esc(t.tool)} ${esc(JSON.stringify(t.arguments||{}))}</div>`).join("")}</details>`:""}</div>`; $("chatLog").appendChild(el); $("chatLog").scrollTop=$("chatLog").scrollHeight; return el; }
// The conversation's own state travels with it: the server returns `context`
// and the next turn sends it straight back. An answer names a matchup, not its
// id, so history text alone can never tell the next turn which game we are on.
// This lives in `state`, so two tabs are two conversations.
async function sendChat(message) { if(!message.trim())return; addMessage("user",message); state.history.push({role:"user",content:message}); const thinking=addMessage("bot","Analyzing from stored application data…"); thinking.querySelector(".bubble").classList.add("thinking"); try { const out=await api("/api/chat",{method:"POST",body:JSON.stringify({message,history:state.history.slice(-8),event_id:state.eventId,context:state.chatContext||null})}); thinking.remove(); if(out.context) state.chatContext=out.context; addMessage("bot",out.answer,out.tool_trace||[]); state.history.push({role:"assistant",content:out.answer}); } catch(e){thinking.remove();addMessage("bot",`Unavailable: ${e.message}`);} }
async function refreshOdds() { const b=$("refreshBtn"); b.disabled=true;b.textContent="Refreshing…"; try { const out=await api("/api/refresh",{method:"POST",body:JSON.stringify({sports:["NFL"]})}); const r=out.sports?.NFL||{}; await loadDashboard(true); toast(`NFL: stored ${r.odds_rows||0} odds rows via ${r.odds_source||"provider"}${r.odds_error?` · ${r.odds_error}`:""}`,!!r.odds_error); } catch(e){toast(`Data refresh failed: ${e.message}`,true);} finally{b.disabled=false;b.textContent="Refresh data";} }
async function loadDashboard(force=false) { $("main").innerHTML='<div class="loading">Loading NFL slate…</div>'; state.dashboard=await api(`/api/dashboard?sport=NFL${force?"&refresh_analysis=true":""}`); renderDashboard(); }
function bind() {
  document.addEventListener("click",e=>{ if(e.target.closest("#backBtn")){state.eventId=null;renderDashboard();return} const window=e.target.closest("[data-window]"); if(window){state.window=window.dataset.window;renderDashboard();return} const sec=e.target.closest("[data-section]"); if(sec){state.section=sec.dataset.section;state.eventId=null;renderDashboard();return} const ev=e.target.closest("[data-event]"); if(ev){openEvent(ev.dataset.event);return} });
  $("refreshBtn").onclick=refreshOdds; $("providerHealth").onclick=()=>$("providerPanel").classList.toggle("hidden"); $("chatToggle").onclick=()=>$("chat").classList.add("open"); $("chatClose").onclick=()=>$("chat").classList.remove("open");
  $("chatForm").onsubmit=e=>{e.preventDefault();const i=$("chatInput");const m=i.value;i.value="";sendChat(m)};
  $("suggest").innerHTML=["Why is this a pass?","Explain this matchup","What is the freshest market price?"].map(x=>`<button>${esc(x)}</button>`).join(""); $("suggest").onclick=e=>{if(e.target.tagName==="BUTTON")sendChat(e.target.textContent)};
}
async function init() { bind(); try { state.config=await api("/api/config"); $("modelVersion").textContent=`${state.config.model_version} · ${state.config.confidence_version}`; $("llmBadge").textContent=state.config.llm_enabled?state.config.llm_model:"DETERMINISTIC"; $("llmBadge").className=`badge ${state.config.llm_enabled?"live":""}`; await loadDashboard(); addMessage("bot",state.config.llm_enabled?"Connected. I use application tools for all odds, projections, and confidence values.":"No OpenAI key configured. Deterministic chat remains available and uses the same application tools."); } catch(e){ $("main").innerHTML=`<div class="warnbox">Startup failed: ${esc(e.message)}</div>`; } }
init();
