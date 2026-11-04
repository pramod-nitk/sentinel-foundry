DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Foundry Sentinel</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e4e3de;--crit:#c0392b;--warn:#b7791f;--info:#2b6cb0;--ok:#2f855a;
--b1:#3b6fd8;--b2:#8a63d2;--b3:#d2691e;--b4:#2a9d8f;--b5:#c94f7c;--b6:#7d8b99;--b7:#e0a800}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mute:#9b9a94;--line:#33332f;--crit:#ef6f5e;--warn:#e2a93b;--info:#6ea8f0;--ok:#5cc58a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;padding:16px 24px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}h2{font-size:15px;margin:0 0 10px}main{padding:20px 24px;max-width:1400px;margin:auto}
.controls{display:flex;gap:8px;flex-wrap:wrap}select,button,input{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
button{cursor:pointer}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .v{font-size:24px;font-weight:600}.kpi .l{color:var(--mute);font-size:12px}
.grid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px;margin-bottom:20px}@media(max-width:900px){.grid{grid-template-columns:1fr}}
.rec{border-left:3px solid var(--line);padding:8px 10px;margin-bottom:8px}.rec.critical{border-color:var(--crit)}.rec.warning{border-color:var(--warn)}.rec.info{border-color:var(--info)}
.rec .t{font-weight:600}.rec .s{color:var(--mute);font-size:13px;margin-top:2px}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;color:#fff}
.pill.critical{background:var(--crit)}.pill.warning{background:var(--warn)}.pill.info{background:var(--info)}.pill.ok{background:var(--ok)}
.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:500;font-size:12px}tbody tr{cursor:pointer}tbody tr:hover{background:color-mix(in srgb,var(--info) 8%,transparent)}
.mute{color:var(--mute)}.num{text-align:right;font-variant-numeric:tabular-nums}
#drawer{position:fixed;top:0;right:0;width:min(720px,100%);height:100%;background:var(--card);border-left:1px solid var(--line);overflow:auto;padding:20px;box-shadow:-8px 0 24px rgba(0,0,0,.15)}
.finding{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}.finding .fix{margin-top:6px;padding:8px;border-radius:8px;background:color-mix(in srgb,var(--ok) 10%,transparent)}
.bar{display:flex;height:14px;border-radius:7px;overflow:hidden;margin:8px 0}.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);padding:10px;border-radius:8px;max-height:360px;overflow:auto;font-size:12px}
.empty{color:var(--mute);padding:20px;text-align:center}
</style></head><body>
<header><h1>Foundry Sentinel</h1>
<div class="controls">
<select id="hours"><option value="1">Last hour</option><option value="24" selected>Last 24h</option><option value="168">Last 7 days</option><option value="720">Last 30 days</option></select>
<select id="app"><option value="">All apps</option></select>
<select id="sev"><option value="">All requests</option><option value="info">With findings</option><option value="warning">Warning+</option><option value="critical">Critical</option></select>
<button id="refresh">Refresh</button></div></header>
<main>
<div class="kpis" id="kpis"></div>
<div class="grid">
<div class="card"><h2>What to fix first</h2><div id="recs"></div></div>
<div class="card"><h2>Apps / agents</h2><div class="tablewrap"><table><thead><tr><th>App</th><th class="num">Requests</th><th class="num">Avg health</th><th class="num">Avg input tok</th><th class="num">Crit</th><th class="num">Warn</th></tr></thead><tbody id="apps"></tbody></table></div></div>
</div>
<div class="card"><h2>Recent requests</h2><div class="tablewrap"><table><thead><tr><th>Time</th><th>App</th><th>API</th><th>Model</th><th class="num">Status</th><th class="num">Input tok</th><th class="num">Context</th><th class="num">Cached</th><th class="num">Latency</th><th class="num">Health</th><th>Top issue</th></tr></thead><tbody id="reqs"></tbody></table></div></div>
</main>
<aside id="drawer" hidden></aside>
<script>
const tok=new URLSearchParams(location.search).get('token');
const q=(p)=>fetch(p+(p.includes('?')?'&':'?')+(tok?'token='+encodeURIComponent(tok):''),{headers:tok?{'x-sentinel-admin-token':tok}:{}}).then(r=>r.json());
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=n=>n==null?'–':Number(n).toLocaleString();
const pill=(sev,txt)=>`<span class="pill ${sev}">${esc(txt||sev)}</span>`;
const hp=h=>h==null?'–':pill(h>=85?'ok':h>=60?'warning':'critical',h);
const COLORS={system:'var(--b1)',instructions:'var(--b2)',history:'var(--b6)',latest_user:'var(--b4)',tool_outputs:'var(--b3)',tool_calls:'var(--b7)',tool_schemas:'var(--b5)'};
async function load(){
 const h=document.getElementById('hours').value,app=document.getElementById('app').value,sev=document.getElementById('sev').value;
 const [sum,rows]=await Promise.all([q(`/sentinel/api/summary?hours=${h}`),q(`/sentinel/api/requests?hours=${h}&limit=300&app=${encodeURIComponent(app)}&severity=${sev}`)]);
 const sc=sum.requests_by_severity||{};
 document.getElementById('kpis').innerHTML=[
  ['Requests',fmt(sum.requests)],['Avg health',sum.avg_health??'–'],['Critical requests',fmt(sc.critical||0)],['Warning requests',fmt(sc.warning||0)],
  ['Error rate',((sum.error_rate||0)*100).toFixed(1)+'%'],['Prompt tokens',fmt(sum.tokens.prompt)],['Cache hit',((sum.tokens.cache_hit_ratio||0)*100).toFixed(0)+'%']
 ].map(([l,v])=>`<div class="card kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
 document.getElementById('recs').innerHTML=sum.recommendations.length?sum.recommendations.slice(0,8).map(r=>`<div class="rec ${r.severity}"><div class="t">${pill(r.severity)} ${esc(r.title)} <span class="mute">· ${r.count}× (${(r.share*100).toFixed(0)}% of requests)${r.wasted_tokens?' · ~'+fmt(r.wasted_tokens)+' tokens wasted':''}</span></div><div class="s">${esc(r.suggestion)}</div><div class="s">Apps: ${esc(r.apps.join(', '))}</div></div>`).join(''):'<div class="empty">No issues found in this window.</div>';
 const sel=document.getElementById('app');const cur=sel.value;
 sel.innerHTML='<option value="">All apps</option>'+sum.apps.map(a=>`<option ${a.app===cur?'selected':''}>${esc(a.app)}</option>`).join('');
 document.getElementById('apps').innerHTML=sum.apps.map(a=>`<tr><td>${esc(a.app)}</td><td class="num">${fmt(a.requests)}</td><td class="num">${hp(a.avg_health)}</td><td class="num">${fmt(a.avg_input_tokens)}</td><td class="num">${a.critical}</td><td class="num">${a.warning}</td></tr>`).join('')||'<tr><td colspan="6" class="empty">No traffic yet</td></tr>';
 document.getElementById('reqs').innerHTML=rows.map(r=>{const inp=r.prompt_tokens??r.est_input_tokens;const pct=r.context_window&&inp?Math.round(inp/r.context_window*100):null;const top=r.findings[0];
  return `<tr data-id="${r.id}"><td>${new Date(r.ts*1000).toLocaleTimeString()}</td><td>${esc(r.app)}</td><td>${esc(r.api)}</td><td>${esc(r.model)}</td><td class="num">${r.status}</td><td class="num">${fmt(inp)}</td><td class="num">${pct==null?'–':pct+'%'}</td><td class="num">${fmt(r.cached_tokens)}</td><td class="num">${fmt(r.latency_ms)} ms</td><td class="num">${hp(r.health_score)}</td><td>${top?pill(top.severity,top.rule)+' '+esc(top.title):'<span class="mute">—</span>'}</td></tr>`}).join('')||'<tr><td colspan="11" class="empty">No requests. Point your SDK endpoint at this service.</td></tr>';
 document.querySelectorAll('#reqs tr[data-id]').forEach(tr=>tr.onclick=()=>openReq(tr.dataset.id));
}
async function openReq(id){
 const r=await q('/sentinel/api/requests/'+id);const d=document.getElementById('drawer');
 const total=Object.values(r.breakdown).reduce((a,b)=>a+b,0)||1;
 const bar=Object.entries(r.breakdown).map(([k,v])=>`<div title="${k}: ${v}" style="width:${v/total*100}%;background:${COLORS[k]||'var(--b6)'}"></div>`).join('');
 const legend=Object.entries(r.breakdown).map(([k,v])=>`<span><i style="background:${COLORS[k]||'var(--b6)'}"></i>${k} ${fmt(v)}</span>`).join('');
 const j=r.judge;
 const judgeHtml=!j?'':j.error?`<h2>LLM review</h2><div class="mute">${esc(j.error)}</div>`:`<h2>LLM review · ${esc(j.verdict)}</h2><p>${esc(j.summary)}</p>`+(j.issues||[]).map(i=>`<div class="finding">${pill(i.severity)} <b>${esc(i.category)}</b><div>${esc(i.explanation)}</div><div class="fix"><b>Fix:</b> ${esc(i.fix)}</div></div>`).join('')+(j.improved_system_prompt?`<h2>Suggested system prompt</h2><pre>${esc(j.improved_system_prompt)}</pre>`:'');
 let reqBody=r.request_body;try{reqBody=JSON.stringify(JSON.parse(reqBody),null,2)}catch(e){}
 d.innerHTML=`<button onclick="document.getElementById('drawer').hidden=true" style="float:right">Close</button>
 <h2>${esc(r.api)} · ${esc(r.model)} · ${hp(r.health_score)}</h2>
 <div class="mute">${esc(r.method)} ${esc(r.path)} · HTTP ${r.status} · ${fmt(r.latency_ms)} ms · app ${esc(r.app)} · id ${r.id}</div>
 <p>Input ${fmt(r.prompt_tokens)} tok (est. ${fmt(r.est_input_tokens)}) of ${fmt(r.context_window)} · output ${fmt(r.completion_tokens)} · cached ${fmt(r.cached_tokens)} · finish ${esc(r.finish_reason)}</p>
 <h2>Where the input tokens go (estimate)</h2><div class="bar">${bar}</div><div class="legend">${legend}</div>
 <h2 style="margin-top:16px">Findings (${r.findings.length})</h2>
 ${r.findings.map(f=>`<div class="finding">${pill(f.severity)} <b>${esc(f.rule)} · ${esc(f.title)}</b><div>${esc(f.detail)}</div><div class="fix"><b>Fix:</b> ${esc(f.suggestion)}</div></div>`).join('')||'<div class="mute">No issues detected.</div>'}
 ${judgeHtml}
 <h2>Request body</h2><pre>${esc(reqBody||'(not stored)')}</pre>`;
 d.hidden=false;
}
['hours','app','sev'].forEach(i=>document.getElementById(i).onchange=load);document.getElementById('refresh').onclick=load;
load();setInterval(load,15000);
</script></body></html>"""
