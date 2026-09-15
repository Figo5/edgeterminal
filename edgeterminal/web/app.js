"use strict";

const state = { sport: "NFL", section: "overview", dashboard: null, config: null, eventId: null, history: [] };
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "—").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
const pct = (v) => v == null ? "—" : `${(Number(v) * 100).toFixed(1)}%`;
const odds = (v) => v == null ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(0)}`;
const num = (v, d=1) => v == null ? "—" : Number(v).toFixed(d);
const fmtPct = (v) => v == null ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(1)} pts`;
const when = (v) => { if (!v) return "—"; const d = typeof v === "number" ? new Date(v*1000) : new Date(v); return isNaN(d) ? "—" : d.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}); };
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
  const counts=state.dashboard?.counts||{};
  const sections=[["overview","Overview",counts.total],["bets","Best Bets",counts.official],["watch","Watchlist",counts.watch],["games","NFL Slate",state.dashboard?.upcoming?.length]];
  $("sections").innerHTML=sections.map(([id,label,count])=>`<button data-section="${id}" class="${id===state.section?"on":""}"><span>${label}</span><span class="count">${count??0}</span></button>`).join("");
}
function providerPanel() {
  const rows=state.dashboard?.providers||[];
  $("providerList").innerHTML=rows.length?rows.map(p=>`<div class="prov" title="${esc(p.detail)}"><i class="dot ${p.is_demo?"demo":p.ok?"ok":"bad"}"></i>${esc(p.kind)} · ${esc(p.name)}</div>`).join(""):`<div class="prov"><i class="dot bad"></i>not checked</div>`;
  $("providerDots").innerHTML=rows.length?rows.filter(p=>["odds","stats","injuries","weather","news"].includes(p.kind)).map(p=>`<i class="dot ${p.is_demo?"demo":p.ok?"ok":"bad"}" title="${esc(p.kind)}: ${esc(p.detail)}"></i>`).join(""):`<i class="dot bad"></i>`;
}
function recCard(label,r) {
  if (!r) return `<div class="card"><div class="label">${label}</div><div class="empty">No qualifying market</div></div>`;
  return `<div class="card" data-event="${esc(r.event_id)}"><div class="label">${label}</div><div class="headline">${esc(r.selection)} ${r.line==null?"":num(r.line)}</div><div class="sub">${esc(r.event_name)} · ${esc(r.market_label)}</div><div class="nums"><span>${odds(r.price)} ${esc(r.book)}</span><span class="pos">${num(r.edge_pct)} edge</span><span>${num(r.confidence,0)} conf</span></div></div>`;
}
function parlayCard(p) {
  if (!p) return `<div class="card" id="parlayCard"><div class="label">Best Parlay</div><div class="empty">No combination clears the quality bar</div></div>`;
  return `<div class="card" id="parlayCard"><div class="label">Best Parlay · ${p.legs.length} legs</div><div class="headline">${p.legs.map(l=>esc(l.selection)).join(" + ")}</div><div class="sub">correlation-adjusted ${pct(p.correlation_adjusted_prob)} · ${esc(p.state)}</div><div class="nums"><span>${odds(p.combined_price)}</span><span class="pos">${num(p.edge_pct)} edge</span><span>${num(p.confidence,0)} conf</span></div></div>`;
}
function filterRecs(rows) {
  const conf=Number($("confFilter").value), market=$("marketFilter").value, plus=$("plusOnly").checked, dogs=$("dogsOnly").checked, sort=$("sortBy").value;
  return [...(rows||[])].filter(r=>r.confidence>=conf && (!market||r.market===market) && (!plus||r.price>0) && (!dogs||r.price>0)).sort((a,b)=>sort==="start_time"?String(a.start_time).localeCompare(String(b.start_time)):Number(b[sort]??-Infinity)-Number(a[sort]??-Infinity));
}
function gauge(v) { const c=v>=80?"hi":v>=65?"mid":"lo"; return `<div class="gauge"><span class="bar"><i class="fill ${c}" style="width:${Math.max(0,Math.min(100,v))}%"></i></span><span class="v">${num(v,0)}</span></div>`; }
function movement(r) { const m=r.line_movement||{}; if (!m.snapshots || m.snapshots<2) return "1 snapshot"; const open=m.open_line??m.open_price; return `${esc(open)} → ${esc(r.line??r.price)} (${Number(m.delta)>0?"+":""}${esc(m.delta)})`; }
function recTable(rows, title="Best Bets", sub="") {
  const data=filterRecs(rows);
  const empty=title==="Best Bets"?`<div class="empty-state"><b>NO OFFICIAL PICKS RIGHT NOW</b><span>No available NFL market currently satisfies the required price, confidence, data-quality, and market-coverage thresholds.</span></div>`:`<div class="notebox">No markets meet the active filters.</div>`;
  return `<section class="panel priority"><div class="panelhead"><h2>${esc(title)}</h2><span class="sub">${esc(sub||`${data.length} markets`)}</span></div><div class="panelbody" style="padding:0;overflow:auto"><table><thead><tr><th>Matchup / bet</th><th>Best price</th><th class="num">Edge</th><th class="num">Confidence</th><th class="num">Units</th><th>Status</th></tr></thead><tbody>${data.length?data.map(recRow).join(""):`<tr><td colspan="6">${empty}</td></tr>`}</tbody></table></div></section>`;
}
function recRow(r) { return `<tr class="row" data-rec="${esc(r.id)}" data-event="${esc(r.event_id)}"><td><b>${esc(r.event_name)}</b><div class="evname">${esc(r.selection)} ${r.line==null?"":num(r.line)} · ${esc(r.market_label||r.market)}</div><details onclick="event.stopPropagation()"><summary>Research details</summary><div class="detail-inline">Model ${pct(r.model_prob)} · implied ${pct(r.implied_prob)} · fair ${odds(r.fair_price)} · playable to ${odds(r.breakeven_price)} · data ${esc(r.data_quality)}<br>${esc((r.reasoning||[]).slice(0,3).join(" · "))}<br><span class="neg">${esc((r.risks||[]).slice(0,2).join(" · "))}</span></div></details></td><td><span class="book ${["fanduel","bet365"].includes(String(r.book).toLowerCase())?"pref":""}">${esc(r.book)} ${odds(r.price)}</span></td><td class="num ${r.edge_pct>0?"pos":"neg"}">${r.edge_pct>0?"+":""}${num(r.edge_pct)}</td><td class="num">${gauge(r.confidence)}</td><td class="num">${num(r.units,2)}u</td><td>${tag(r.state)}</td></tr>`; }
function watchTable(rows) { const data=(rows||[]).slice(0,5); return `<section class="panel"><div class="panelhead"><h2>Watchlist</h2><span class="sub">strongest non-bets · exact blockers</span></div><div class="panelbody" style="padding:0"><table><thead><tr><th>Market</th><th>Current</th><th class="num">Confidence</th><th>Why not a bet?</th></tr></thead><tbody>${data.map(r=>`<tr class="row" data-event="${esc(r.event_id)}"><td><b>${esc(r.selection)} ${r.line==null?"":num(r.line)}</b><div class="evname">${esc(r.event_name)} · ${esc(r.market_label)}</div></td><td class="mono">${esc(r.book)} ${odds(r.price)}</td><td class="num">${num(r.confidence,0)} / ${state.config?.official_pick_confidence||80}</td><td><b>${esc(r.trigger_note||`${r.state}: deterministic gates not cleared`)}</b></td></tr>`).join("")||`<tr><td colspan="4">Nothing is close enough to track.</td></tr>`}</tbody></table></div></section>`; }
function movesTable(rows) { return `<section class="panel"><div class="panelhead"><h2>Biggest Line Moves</h2><span class="sub">observed snapshots only · no cause inferred</span></div><div class="panelbody">${(rows||[]).length?rows.map(m=>`<div class="srcrow"><span class="tag sport">${esc(m.sport)}</span><b>${esc(m.selection)}</b><span>${esc(m.event_name)}</span><span class="mono">${esc(m.open_line??m.open_price)} → ${esc(m.current_line??m.current_price)} (${Number(m.delta)>0?"+":""}${esc(m.delta)} ${esc(m.unit)})</span><span>${m.snapshots} snapshots</span></div>`).join(""):`<div class="notebox">Movement needs at least two distinct snapshots. Refresh Odds again later; no movement is inferred from one observation.</div>`}</div></section>`; }
function gamesTable(rows) { return `<section class="panel"><div class="panelhead"><h2>NFL Slate</h2><span class="sub">kickoff and best available prices · click for research</span></div><div class="panelbody" style="padding:0;overflow:auto"><table><thead><tr><th>Kickoff</th><th>Matchup</th><th>Spread / moneyline / total</th><th>Status</th></tr></thead><tbody>${(rows||[]).map(e=>`<tr class="row" data-event="${esc(e.id)}"><td class="mono">${when(e.start_time)}</td><td><b>${esc(e.away_team||e.name)}</b><div class="evname">at ${esc(e.home_team||"")} · ${esc(e.venue)}</div></td><td>${(e.markets||[]).slice(0,6).map(m=>`<span class="quote">${esc(m.selection)} ${m.line==null?"":num(m.line)} ${odds(m.price)} ${esc(m.book)}</span>`).join(" ")||"Prices unavailable"}</td><td>${esc(e.status)} · ${e.odds_count||0} quotes</td></tr>`).join("")||`<tr><td colspan="4">No current NFL games available.</td></tr>`}</tbody></table></div></section>`; }
function renderDashboard() {
  renderNavigation(); providerPanel(); const d=state.dashboard, h=d.headline||{};
  $("demoBadge").classList.toggle("hidden", !d.demo_mode && !(d.best_bets||[]).some(r=>r.is_demo));
  $("staleBadge").classList.toggle("hidden", !(d.odds_age_s > (state.config?.stale_odds_s||900)));
  $("lastUpdated").textContent=`last updated ${when(d.last_refresh?.at)}`;
  let html="";
  if(state.section==="overview") html=`${recTable(d.best_bets,"Best Bets",`Official Picks · confidence ≥ ${state.config?.official_pick_confidence||80}`)}${watchTable(d.watchlist)}${gamesTable(d.upcoming)}`;
  if(state.section==="bets") html=recTable(d.best_bets,"Best Bets",`official picks · confidence ≥ ${state.config?.official_pick_confidence||80}`);
  if(state.section==="watch") html=watchTable(d.watchlist);

  if(state.section==="games") html=gamesTable(d.upcoming);
  $("main").innerHTML=html;
}

function teamComparison(detail) { const e=detail.event, a=detail.stats?.[e.away_team]||{}, h=detail.stats?.[e.home_team]||{}; const keys=["epa_play","success_rate","yards_per_play","points_for","points_against","pressure_rate","neutral_pace","proe","games"].filter(k=>a[k]!=null||h[k]!=null); return keys.length?keys.map(k=>`<div class="cmp"><span class="l">${esc(a[k])}</span><span class="m">${esc(k.replaceAll("_"," "))}</span><span class="r">${esc(h[k])}</span></div>`).join(""):`<div class="notebox">Comparable team metrics are unavailable.</div>`; }
function sources(rows) { return `<div class="sources">${(rows||[]).map(s=>`<div class="srcrow"><span class="tag">${esc(s.trust)}</span>${s.url?`<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>`:`<span>${esc(s.title)}</span>`}<span>${esc(s.publisher)}</span><span class="mono">${when(s.retrieved_at)}</span></div>`).join("")||"No research sources available."}</div>`; }
async function openEvent(id) { state.eventId=id; $("main").innerHTML='<div class="loading">Running deterministic game analysis…</div>'; try { const d=await api(`/api/event/${encodeURIComponent(id)}`); const e=d.event,p=d.projection||{}; $("main").innerHTML=`<button class="backlink" id="backBtn">← Dashboard</button><section class="panel"><div class="panelhead"><h2>${esc(e.sport)} Game Detail</h2><span class="sub">analyzed ${when(d.analyzed_at)} · data ${esc(d.data_quality_tier)}</span></div><div class="panelbody"><div class="gamehead"><div class="teamcol"><span class="tn">${esc(e.away_team)}</span><span class="proj">${esc(p.proj_away??"—")}</span></div><div class="teamcol"><span class="meta">at</span></div><div class="teamcol"><span class="tn">${esc(e.home_team)}</span><span class="proj">${esc(p.proj_home??"—")}</span></div><div class="teamcol"><span>${when(e.start_time)}</span><span class="meta">${esc(e.venue)}</span><span class="mono">model total ${num(p.total)} · margin ${p.margin==null?"—":`${p.margin>0?"+":""}${num(p.margin)}`}</span></div></div>${(d.warnings||[]).map(w=>`<div class="warnbox">${esc(w)}</div>`).join("")}</div></section>${propsPanel()}${recTable(d.recommendations,"Market Board",`${d.recommendations.length} deterministically priced markets`)}<div class="grid g2"><section class="panel"><div class="panelhead"><h2>Team Comparison</h2><span class="sub">away · metric · home</span></div><div class="panelbody">${teamComparison(d)}</div></section><section class="panel"><div class="panelhead"><h2>Context</h2></div><div class="panelbody"><h4>Weather</h4><div class="notebox">${d.weather?`${esc(d.weather.conditions)} · ${num(d.weather.temp_f)}°F · wind ${num(d.weather.wind_mph)} mph · precip ${num(d.weather.precip_chance)}%`:"Unavailable; confidence reduced where relevant."}</div><h4>Injuries</h4>${(d.injuries||[]).map(i=>`<div class="srcrow"><b>${esc(i.player)}</b><span>${esc(i.team)}</span><span>${esc(i.status)}</span><span>${esc(i.detail)}</span></div>`).join("")||"No injury rows available."}<h4>News</h4>${(d.news||[]).map(n=>`<div class="srcrow"><span class="tag">${esc(n.trust)}</span><b>${esc(n.title)}</b><span>${esc(n.summary)}</span></div>`).join("")||"No news rows available."}</div></section></div><section class="panel"><div class="panelhead"><h2>Research Sources</h2><span class="sub">timestamped provenance</span></div><div class="panelbody">${sources(d.sources)}</div></section>`; $("backBtn").onclick=()=>{state.eventId=null;renderDashboard()}; } catch(e){ $("main").innerHTML=`<button class="backlink" onclick="location.reload()">← Dashboard</button><div class="warnbox">${esc(e.message)}</div>`; }
}

function propsPanel() {
  const cats=[["best","Best"],["td","TD"],["passing","Passing"],["rushing","Rushing"],["receiving","Receiving"]];
  return `<section class="panel"><div class="panelhead"><h2>Player Props</h2><span class="sub">on demand · odds API · research-grounded</span><span class="filters">${cats.map(([k,l])=>`<button class="chip" data-propcat="${k}">${l}</button>`).join("")}</span></div><div class="panelbody" id="propsHost"><div class="notebox">Props are fetched on demand to protect Odds API quota. Choose a category.</div></div></section>`;
}
function propRow(r) {
  const line=r.line==null?"":` ${num(r.line)}`;
  const side=r.side?` ${r.side[0].toUpperCase()+r.side.slice(1)}`:"";
  return `<tr class="row" data-event="${esc(r.event_id)}"><td><b>${esc(r.player)}</b>${side}${line}<div class="evname">${esc(r.market_label)} · ${esc(r.book||"—")}</div></td><td class="num">${odds(r.price)}</td><td class="num">${pct(r.model_prob)}</td><td class="num">${odds(r.fair_price)}</td><td class="num ${r.edge_pct>0?"pos":"neg"}">${fmtPct(r.edge_pct)}</td><td class="num">${num(r.confidence)}/${esc(r.confidence_cap??"")}</td><td class="num">${num(r.units)}u</td><td>${tag(r.state)}${r.usage_available===false?'<span class="tag warn">no usage</span>':""}${(r.books||0)<2?'<span class="tag warn">thin book</span>':""}</td></tr>`;
}
function propRec(r) {
  const usage=r.usage_available?`<div><b>Usage</b> ${esc(JSON.stringify(r.usage_summary||{}))}</div>`:`<div class="warn">No structured usage for this player — the projection is not trustworthy.</div>`;
  const ctx=(r.research_context||[]).map(c=>`<div><b>${esc(c.reliability)}</b> ${esc(c.claim||"")} <span class="meta">${esc(c.source||"")}</span></div>`).join("")||"<div class='meta'>No web research was needed for this player.</div>";
  const cites=(r.citations||[]).map(c=>`<div class="srcrow"><span class="tag">${esc(c.reliability||"")}</span>${c.url?`<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.title||c.url)}</a>`:`<span>${esc(c.title||"")}</span>`}<span>${esc(c.publisher||"")}</span><span class="mono">${when(c.retrieved_at)}</span></div>`).join("");
  return `${r.reasons.map(x=>`<div>• ${esc(x)}</div>`).join("")}${(r.risks||[]).map(x=>`<div class="warn">• ${esc(x)}</div>`).join("")}<div>${usage}${ctx}</div><div>${cites}</div>`;
}
function propsTable(d) {
  const html=d.map(r=>`<tr class="row"><td colspan="8" style="padding:0"><table style="margin:0"><tbody>${propRow(r)}</tbody></table><details class="detail-inline" style="padding:0 12px 8px"><summary>Usage · current role · research · risks · citations</summary>${propRec(r)}</details></td></tr>`).join("");
  return `<table><thead><tr><th>Player</th><th class="num">Best</th><th class="num">Model</th><th class="num">Fair</th><th class="num">Edge</th><th class="num">Conf</th><th class="num">Units</th><th>Status</th></tr></thead><tbody>${html}</tbody></table>`;
}
async function loadProps(category) {
  state.propsCategory=category; const host=$("propsHost"); if(!host) return;
  document.querySelectorAll("[data-propcat]").forEach(b=>b.classList.toggle("on",b.dataset.propcat===category));
  host.innerHTML='<div class="loading">Fetching on-demand prop markets…</div>';
  try {
    const d=await api(`/api/event/${encodeURIComponent(state.eventId)}/props?category=${encodeURIComponent(category)}`);
    const q=d.quota||{};
    const head=`<div class="meta">source ${esc(d.price_source)} · ${(d.market||{}).books||0} book(s) · coverage ${esc((d.market||{}).coverage||"—")} · overround ${esc((d.market||{}).overround??"—")}${q.remaining?` · odds-api remaining ${esc(q.remaining)}`:""} · ${d.recommendations.length} priced prop(s)</div>`;
    if(!d.recommendations.length){ host.innerHTML=head+`<div class="empty-state"><b>NO PRICED PROPS</b><div>${esc((d.warnings||[])[0]||d.state||"No prop market returned for this game.")}</div></div>`; return; }
    const ml=d.most_likely?`<div class="notebox"><b>Most likely TD scorer</b> — ${esc(d.most_likely.player)} (${pct(d.most_likely.final_prob)}, ${odds(d.most_likely.price)} at ${esc(d.most_likely.book||"")})${d.best_value?` · <b>Best value</b> — ${esc(d.best_value.player)} (${fmtPct(d.best_value.edge_pct)} edge, ${odds(d.best_value.price)} at ${esc(d.best_value.book||"")})`:" · no positive-value TD price"}</div>`:"";
    const gaps=(d.gaps||[]).length?`<div class="meta">research gaps closed: ${(d.gaps||[]).map(g=>esc(g.gap)).join(", ")} · queries used ${(d.research||{}).queries_used||0}</div>`:"";
    host.innerHTML=head+ml+propsTable(d.recommendations)+gaps;
  } catch(e) { host.innerHTML=`<div class="warnbox">${esc(e.message)}</div>`; }
}

function md(text) { return esc(text).replace(/^### (.+)$/gm,"<h3>$1</h3>").replace(/^\*\*(.+?)\*\*/gm,"<b>$1</b>").replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/^[-•] (.+)$/gm,"<div>• $1</div>").replace(/\n\n/g,"</p><p>").replace(/\n/g,"<br>"); }
function addMessage(role,text,trace=[]) { const el=document.createElement("div"); el.className=`msg ${role}`; el.innerHTML=`<div class="who">${role==="user"?"You":"EdgeTerminal"}</div><div class="bubble"><p>${md(text)}</p>${trace.length?`<details class="trace"><summary>${trace.length} application tool call(s)</summary>${trace.map(t=>`<div>${esc(t.tool)} ${esc(JSON.stringify(t.arguments||{}))}</div>`).join("")}</details>`:""}</div>`; $("chatLog").appendChild(el); $("chatLog").scrollTop=$("chatLog").scrollHeight; return el; }
async function sendChat(message) { if(!message.trim())return; addMessage("user",message); state.history.push({role:"user",content:message}); const thinking=addMessage("bot","Analyzing from stored application data…"); thinking.querySelector(".bubble").classList.add("thinking"); try { const out=await api("/api/chat",{method:"POST",body:JSON.stringify({message,history:state.history.slice(-8),event_id:state.eventId})}); thinking.remove(); addMessage("bot",out.answer,out.tool_trace||[]); state.history.push({role:"assistant",content:out.answer}); } catch(e){thinking.remove();addMessage("bot",`Unavailable: ${e.message}`);} }
async function refreshOdds() { const b=$("refreshBtn"); b.disabled=true;b.textContent="Refreshing…"; try { const out=await api("/api/refresh",{method:"POST",body:JSON.stringify({sports:["NFL"]})}); const r=out.sports?.NFL||{}; await loadDashboard(true); toast(`NFL: stored ${r.odds_rows||0} odds rows via ${r.odds_source||"provider"}${r.odds_error?` · ${r.odds_error}`:""}`,!!r.odds_error); } catch(e){toast(`Odds refresh failed: ${e.message}`,true);} finally{b.disabled=false;b.textContent="Refresh Odds";} }
async function loadDashboard(force=false) { $("main").innerHTML='<div class="loading">Loading NFL slate…</div>'; state.dashboard=await api(`/api/dashboard?sport=NFL${force?"&refresh_analysis=true":""}`); renderDashboard(); }
async function loadParlay() { try { const p=await api(`/api/parlay?max_legs=5&sport=NFL`); if(!p.parlay) toast(p.note); else { state.dashboard.headline.best_parlay=p.parlay; renderDashboard(); } } catch(e){toast(e.message,true);} }
function bind() {
  document.addEventListener("click",e=>{ if(e.target.closest("#backBtn")){state.eventId=null;renderDashboard();return} const pc=e.target.closest("[data-propcat]"); if(pc){loadProps(pc.dataset.propcat);return} const sec=e.target.closest("[data-section]"); if(sec){state.section=sec.dataset.section;renderDashboard();return} const ev=e.target.closest("[data-event]"); if(ev){openEvent(ev.dataset.event);return} if(e.target.closest("#parlayCard")) loadParlay(); });
  $("refreshBtn").onclick=refreshOdds; $("providerHealth").onclick=()=>$("providerPanel").classList.toggle("hidden"); $("chatToggle").onclick=()=>$("chat").classList.add("open"); $("chatClose").onclick=()=>$("chat").classList.remove("open");
  $("confFilter").oninput=()=>{$("confOut").textContent=$("confFilter").value;if(state.dashboard)renderDashboard()}; ["marketFilter","plusOnly","dogsOnly","sortBy"].forEach(id=>$(id).onchange=()=>state.dashboard&&renderDashboard());
  $("chatForm").onsubmit=e=>{e.preventDefault();const i=$("chatInput");const m=i.value;i.value="";sendChat(m)};
  $("suggest").innerHTML=["Best NFL bet right now","Analyze this game","What price makes this playable?","Biggest NFL line move"].map(x=>`<button>${esc(x)}</button>`).join(""); $("suggest").onclick=e=>{if(e.target.tagName==="BUTTON")sendChat(e.target.textContent)};
}
async function init() { bind(); try { state.config=await api("/api/config"); $("modelVersion").textContent=`${state.config.model_version} · ${state.config.confidence_version}`; $("llmBadge").textContent=state.config.llm_enabled?state.config.llm_model:"DETERMINISTIC"; $("llmBadge").className=`badge ${state.config.llm_enabled?"live":""}`; await loadDashboard(); addMessage("bot",state.config.llm_enabled?"Connected. I use application tools for all odds, projections, and confidence values.":"No OpenAI key configured. Deterministic chat remains available and uses the same application tools."); } catch(e){ $("main").innerHTML=`<div class="warnbox">Startup failed: ${esc(e.message)}</div>`; } }
init();
