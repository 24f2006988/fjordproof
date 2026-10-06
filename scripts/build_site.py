#!/usr/bin/env python3
"""Build one self-contained, static profile viewer (HTML) from contract-shaped envelopes.

python scripts/build_site.py --envelopes out/envelopes.jsonl --output out/site/index.html

No network, no server, no extra dependencies. Every fact shown carries its source link, retrieval date and
reporting period; missing facts are listed with their state instead of being hidden or zero-filled.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

KEEP_ENV = ("id", "source_url", "source_class", "retrieved_at", "content_sha256")


def slim(envelope: dict) -> dict:
    profile = envelope.get("profile") or {}
    website_ev = (profile.get("evidence") or {}).get("website") or {}
    return {
        "org": envelope.get("organisation_number"),
        "name": profile.get("name") or "",
        "state": envelope.get("state"),
        "modules": {k: (v or {}).get("state") for k, v in (envelope.get("modules") or {}).items()},
        "claims": [
            {"f": c.get("field"), "v": c.get("value"), "s": c.get("availability"), "c": c.get("confidence"), "e": c.get("evidence_ids") or []}
            for c in envelope.get("claims") or []
        ],
        "evidence": [{k: e.get(k) for k in KEEP_ENV} for e in envelope.get("evidence") or []],
        "changes": envelope.get("changes") or [],
        "errors": envelope.get("errors") or [],
        "website_note": website_ev.get("note"),
        "run": envelope.get("run") or {},
        "ops": envelope.get("operations") or {},
    }


def build(envelopes: list[dict]) -> str:
    data = [slim(e) for e in envelopes]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return TEMPLATE.replace("__DATA__", payload)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--envelopes", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    lines = Path(a.envelopes).read_text(encoding="utf-8").splitlines()
    envelopes = [json.loads(line) for line in lines if line.strip()]
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(envelopes), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(envelopes)} companies)")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FjordProof Company Profiles</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#16202a;--muted:#5a6672;--line:#dde2e8;--accent:#1d4ed8;--accent-ink:#fff;--ok:#16794a;--ok-bg:#e3f4ea;--warn:#8a5a00;--warn-bg:#fff1d6;--bad:#a12626;--bad-bg:#fde6e6;--none:#4b5563;--none-bg:#eceff3;--radius:10px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f141a;--panel:#171e26;--ink:#e6ebf0;--muted:#9aa7b4;--line:#2a343f;--accent:#7aa2ff;--accent-ink:#0b1220;--ok:#6fd49a;--ok-bg:#12301f;--warn:#f0c26a;--warn-bg:#352a0e;--bad:#ff8f8f;--bad-bg:#3a1717;--none:#b4bfca;--none-bg:#222b35}}
:root[data-theme="dark"]{--bg:#0f141a;--panel:#171e26;--ink:#e6ebf0;--muted:#9aa7b4;--line:#2a343f;--accent:#7aa2ff;--accent-ink:#0b1220;--ok:#6fd49a;--ok-bg:#12301f;--warn:#f0c26a;--warn-bg:#352a0e;--bad:#ff8f8f;--bad-bg:#3a1717;--none:#b4bfca;--none-bg:#222b35}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px 80px}
header.top{padding:20px 0 12px}
header.top h1{margin:0;font-size:1.35rem}
header.top p{margin:4px 0 0;color:var(--muted);font-size:.92rem}
.theme{float:right;background:none;border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:6px 10px;font:inherit;font-size:.85rem;cursor:pointer}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;margin:12px 0}
.panel h2{margin:0 0 8px;font-size:1.02rem}
.cov{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px 18px}
.cov div{font-size:.86rem}
.bar{height:6px;background:var(--none-bg);border-radius:4px;margin-top:3px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}
.tools input[type=search],.tools select{font:inherit;padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);min-height:44px}
.tools input[type=search]{flex:1 1 220px}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;padding:8px 14px;min-height:44px;font:inherit;font-size:.88rem;cursor:pointer}
.chip[aria-pressed=true]{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.count{color:var(--muted);font-size:.88rem;margin:4px 0 8px}
.list{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));padding:0;margin:0;list-style:none}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.card h3{margin:0;font-size:1rem;line-height:1.3}
.card h3 a{color:var(--ink);text-decoration:none}
.card h3 a:hover{text-decoration:underline}
.meta{color:var(--muted);font-size:.85rem}
.tags{display:flex;flex-wrap:wrap;gap:5px}
.tag{font-size:.75rem;padding:2px 8px;border-radius:999px;background:var(--none-bg);color:var(--none)}
.tag.ok{background:var(--ok-bg);color:var(--ok)}
.tag.warn{background:var(--warn-bg);color:var(--warn)}
.tag.bad{background:var(--bad-bg);color:var(--bad)}
.cmp{display:flex;align-items:center;gap:8px;font-size:.85rem;color:var(--muted);min-height:44px;margin-top:auto}
.cmp input{width:20px;height:20px}
.back{display:inline-flex;align-items:center;min-height:44px;margin:8px 0}
.profile h2.name{margin:4px 0 2px;font-size:1.5rem;line-height:1.25}
.summary p{margin:.4rem 0}
sup.ref a{text-decoration:none;font-size:.75rem;padding:0 2px}
.grid2{display:grid;gap:12px;grid-template-columns:minmax(0,1fr)}
.grid2>*,.panel,.card,.wrap{min-width:0}
body{overflow-x:hidden}
td:first-child,th:first-child{white-space:nowrap}
@media(min-width:800px){.grid2{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.state{font-size:.75rem;padding:2px 8px;border-radius:999px;white-space:nowrap}
.state.available{background:var(--ok-bg);color:var(--ok)}
.state.not_available,.state.not_applicable{background:var(--none-bg);color:var(--none)}
.state.blocked,.state.ambiguous{background:var(--warn-bg);color:var(--warn)}
.state.failed{background:var(--bad-bg);color:var(--bad)}
.hash{font-family:ui-monospace,Consolas,monospace;font-size:.78rem;word-break:break-all}
.url{word-break:break-all}
.cmpbar{position:fixed;left:0;right:0;bottom:0;background:var(--panel);border-top:1px solid var(--line);padding:10px 16px;display:flex;gap:10px;align-items:center;justify-content:center;z-index:5}
.btn{font:inherit;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);border-radius:8px;padding:9px 16px;min-height:44px;cursor:pointer}
.btn.sec{background:transparent;color:var(--accent)}
details{margin:6px 0}
summary{cursor:pointer;min-height:32px}
.sr{position:absolute;left:-9999px}
ul.plain{margin:0;padding-left:18px}
.empty{color:var(--muted);font-style:italic}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
<button class="theme" id="theme" type="button" aria-label="Toggle dark mode">Theme</button>
<h1>FjordProof company profiles</h1>
<p id="sub"></p>
</header>
<main id="app" aria-live="polite"></main>
</div>
<div id="cmpbar" class="cmpbar" hidden></div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const BY = new Map(DATA.map(d => [d.org, d]));
const app = document.getElementById("app");
const nok = new Intl.NumberFormat("nb-NO", {maximumFractionDigits: 0});
const FIELDS = ["name","legal_form","industry","employees","municipality","insolvency_status","annual_accounts","roles","group_structure","locations","official_website","external_footprint"];
const LABEL = {name:"Name",legal_form:"Legal form",industry:"Industry",employees:"Employees (registry)",municipality:"Municipality",insolvency_status:"Bankruptcy / liquidation",annual_accounts:"Annual accounts",roles:"Roles and officers",group_structure:"Group structure",locations:"Sub-units / locations",official_website:"Website",external_footprint:"Public activity and contacts"};
const STATE_TEXT = {available:"Found",not_available:"Not found",blocked:"Blocked by source",not_applicable:"Not applicable",ambiguous:"Could not confirm company",failed:"Failed"};
const NOT_FOUND_WHY = {
  name:"The registry returned no name.", legal_form:"The registry returned no legal form.", industry:"The registry lists no industry code.",
  employees:"The registry has no employee count for this company; none is estimated.", municipality:"No registered business municipality.",
  insolvency_status:"No registry status was returned.", annual_accounts:"No annual accounts are filed in the public accounts register.",
  roles:"The registry lists no roles.", group_structure:"The company is not part of a registered group structure.", locations:"No registered sub-units.",
  official_website:"No website is registered, and none could be proven to belong to this company (exact-company proof is required).",
  external_footprint:"No dated activity, careers pages or contacts were found on a verified company site."};

function el(tag, attrs, ...kids){
  const n = document.createElement(tag);
  for (const [k,v] of Object.entries(attrs||{})){
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
}
function safeUrl(u){ try { const x = new URL(u); return (x.protocol === "https:" || x.protocol === "http:") ? x.href : null; } catch(e){ return null; } }
function link(u, text){ const s = safeUrl(u); return s ? el("a", {href:s, target:"_blank", rel:"noopener noreferrer", class:"url", text:text || s}) : el("span", {class:"url", text:u || ""}); }
const claim = (d, f) => d.claims.find(c => c.f === f);
const val = (d, f) => { const c = claim(d, f); return c && c.s === "available" ? c.v : null; };
const money = n => (n == null ? "–" : nok.format(n));
function accounts(d){ const v = val(d, "annual_accounts"); return v && v.records ? [...v.records].sort((a,b) => (a.period?.tilDato||"").localeCompare(b.period?.tilDato||"")) : []; }
function latestAcc(d){ const a = accounts(d); return a.length ? a[a.length-1] : null; }
function activeRoles(d){ const v = val(d, "roles"); return v && v.roles ? v.roles.filter(r => !r.inactive) : []; }
function activity(d){ const v = val(d, "external_footprint"); return v && v.dated_activity ? v.dated_activity : []; }
function status(d){ const i = val(d, "insolvency_status"); if (!i) return ["unknown","none"]; if (i.bankrupt) return ["Bankrupt","bad"]; if (i.liquidating) return ["Under liquidation","warn"]; return ["Active in registry","ok"]; }
function found(d){ return FIELDS.filter(f => claim(d, f)?.s === "available").length; }
function evNo(d, id){ return d.evidence.findIndex(e => e.id === id) + 1; }
function refs(d, ids){ return el("sup", {class:"ref"}, (ids||[]).map(id => { const n = evNo(d, id); return n ? el("a", {href:"#/" + d.org + "/ev-" + id, "aria-label":"Source " + n, text:"[" + n + "]", onclick: ev => { ev.preventDefault(); const t = document.getElementById("ev-" + id); if (t){ t.closest("details").open = true; t.scrollIntoView({block:"center"}); } }}) : null; }));
}

/* ---------- list view ---------- */
const st = {q:"", filters:new Set(), sort:"name", cmp:new Set()};
const FILTERS = [["website","Has website",d=>claim(d,"official_website")?.s==="available"],["accounts","Has accounts",d=>accounts(d).length>0],["activity","Has dated activity",d=>activity(d).length>0],["group","In a group",d=>claim(d,"group_structure")?.s==="available"],["employees","Employee count",d=>claim(d,"employees")?.s==="available"]];
function coverage(){
  const n = DATA.length; const rows = [["Registry identity", d=>claim(d,"name")?.s==="available"],["Industry", d=>claim(d,"industry")?.s==="available"],["Annual accounts", d=>accounts(d).length>0],["Roles", d=>activeRoles(d).length>0],["Employee count", d=>claim(d,"employees")?.s==="available"],["Sub-units", d=>claim(d,"locations")?.s==="available"],["Group structure", d=>claim(d,"group_structure")?.s==="available"],["Verified website", d=>claim(d,"official_website")?.s==="available"],["Dated activity", d=>activity(d).length>0]];
  return el("div", {class:"panel"}, el("h2", {text:"What this batch contains"}), el("p", {class:"meta", text:n + " companies, one result each. Every fact links to its source and retrieval date. Where nothing was found the profile says so; nothing is estimated or filled in."}),
    el("div", {class:"cov"}, rows.map(([label, fn]) => { const c = DATA.filter(fn).length; return el("div", {}, el("div", {text:label + ": " + c + " of " + n}), el("div", {class:"bar"}, el("i", {style:"width:" + Math.round(100*c/n) + "%"}))); })));
}
function listView(){
  const items = DATA.filter(d => { const q = st.q.trim().toLowerCase(); const m = claim(d,"municipality")?.v || ""; const okq = !q || d.name.toLowerCase().includes(q) || d.org.includes(q.replace(/\s/g,"")) || String(m).toLowerCase().includes(q); return okq && [...st.filters].every(k => FILTERS.find(f => f[0] === k)[2](d)); });
  const key = {name:(a,b)=>a.name.localeCompare(b.name,"nb"), revenue:(a,b)=>(latestAcc(b)?.revenue ?? -Infinity)-(latestAcc(a)?.revenue ?? -Infinity), found:(a,b)=>found(b)-found(a)}[st.sort];
  items.sort(key);
  const root = el("div", {});
  root.append(coverage());
  const search = el("input", {type:"search", id:"q", placeholder:"Search name, organisation number or municipality", "aria-label":"Search companies", value:st.q});
  const sort = el("select", {"aria-label":"Sort by", onchange: e => { st.sort = e.target.value; render(); }}, [["name","Sort: name"],["revenue","Sort: latest revenue"],["found","Sort: most facts found"]].map(([v,t]) => el("option", {value:v, text:t, selected:st.sort===v})));
  const count = el("p", {class:"count", role:"status", text:items.length + " of " + DATA.length + " companies"});
  const list = el("ul", {class:"list"});
  const draw = rows => { list.replaceChildren(...rows.map(card)); count.textContent = rows.length + " of " + DATA.length + " companies"; };
  search.addEventListener("input", e => { st.q = e.target.value; const pos = e.target.selectionStart; render(); const s = document.getElementById("q"); s.focus(); s.setSelectionRange(pos,pos); });
  root.append(el("div", {class:"tools"}, search, sort), el("div", {class:"tools"}, FILTERS.map(([k,t]) => el("button", {class:"chip", type:"button", "aria-pressed":st.filters.has(k), text:t, onclick: () => { st.filters.has(k) ? st.filters.delete(k) : st.filters.add(k); render(); }}))), count, list);
  draw(items);
  if (!items.length) list.replaceWith(el("p", {class:"empty", text:"No companies match. Clear the search or filters."}));
  return root;
}
function card(d){
  const [stText, stCls] = status(d); const acc = latestAcc(d); const ind = val(d,"industry"); const mun = val(d,"municipality"); const legal = val(d,"legal_form");
  const site = claim(d,"official_website")?.s === "available";
  return el("li", {class:"card"},
    el("h3", {}, el("a", {href:"#/" + d.org, text:d.name || d.org})),
    el("div", {class:"meta", text:[legal, mun, d.org].filter(Boolean).join(" · ")}),
    el("div", {class:"meta", text:ind ? ind.label + " (" + ind.code + ")" : "Industry not found"}),
    el("div", {class:"meta", text:acc ? "Revenue " + (acc.period?.tilDato||"").slice(0,4) + ": " + (acc.revenue == null ? "not reported" : money(acc.revenue) + " " + (acc.currency||"")) : "No annual accounts found"}),
    el("div", {class:"tags"}, el("span", {class:"tag " + stCls, text:stText}), el("span", {class:"tag" + (site && !(claim(d,"official_website").c < 0.9) ? " ok" : ""), text:site ? (claim(d,"official_website").c < 0.9 ? "Website listed, unconfirmed" : "Website verified") : "No verified website"}), el("span", {class:"tag", text:found(d) + "/" + FIELDS.length + " fields found"})),
    el("label", {class:"cmp"}, el("input", {type:"checkbox", checked:st.cmp.has(d.org), "aria-label":"Compare " + d.name, onchange: e => { if (e.target.checked){ if (st.cmp.size >= 4){ e.target.checked = false; return; } st.cmp.add(d.org); } else st.cmp.delete(d.org); cmpBar(); }}), "Compare"));
}
function cmpBar(){
  const b = document.getElementById("cmpbar"); b.replaceChildren();
  if (!st.cmp.size){ b.hidden = true; return; }
  b.hidden = false;
  b.append(el("span", {text:st.cmp.size + " selected (max 4)"}), el("button", {class:"btn", type:"button", disabled:st.cmp.size < 2, text:"Compare", onclick:() => { location.hash = "#/compare"; }}), el("button", {class:"btn sec", type:"button", text:"Clear", onclick:() => { st.cmp.clear(); render(); }}));
}

/* ---------- compare ---------- */
function compareView(){
  const ds = [...st.cmp].map(o => BY.get(o)).filter(Boolean);
  const rows = [["Organisation number", d=>d.org],["Legal form", d=>val(d,"legal_form") ?? "Not found"],["Municipality", d=>val(d,"municipality") ?? "Not found"],["Industry", d=>{const i=val(d,"industry");return i? i.label + " (" + i.code + ")" : "Not found";}],["Status", d=>status(d)[0]],["Employees (registry)", d=>val(d,"employees") ?? "Not reported"],
    ["Latest accounts year", d=>{const a=latestAcc(d);return a? (a.period?.tilDato||"").slice(0,4) : "None filed";}],["Revenue (NOK)", d=>{const a=latestAcc(d);return a? money(a.revenue) : "–";}],["Operating result (NOK)", d=>{const a=latestAcc(d);return a? money(a.operating_result) : "–";}],["Annual result (NOK)", d=>{const a=latestAcc(d);return a? money(a.annual_result) : "–";}],["Equity (NOK)", d=>{const a=latestAcc(d);return a? money(a.equity) : "–";}],["Total assets (NOK)", d=>{const a=latestAcc(d);return a? money(a.assets) : "–";}],
    ["Current roles", d=>activeRoles(d).length || "None listed"],["Sub-units", d=>(val(d,"locations")?.locations||[]).length || "None"],["Website", d=>val(d,"official_website") ?? "Not verified"],["Dated activity items", d=>activity(d).length || "None found"],["Fields found", d=>found(d) + " / " + FIELDS.length]];
  return el("div", {}, el("a", {class:"back", href:"#/", text:"← All companies"}), el("div", {class:"panel"}, el("h2", {text:"Side-by-side comparison"}), ds.length < 2 ? el("p", {class:"empty", text:"Pick at least two companies on the list."}) :
    el("div", {class:"tablewrap"}, el("table", {}, el("thead", {}, el("tr", {}, el("th", {text:""}), ds.map(d => el("th", {}, el("a", {href:"#/" + d.org, text:d.name}))))), el("tbody", {}, rows.map(([l,fn]) => el("tr", {}, el("th", {scope:"row", text:l}), ds.map(d => el("td", {class:typeof fn(d) === "string" && /^[\d\s\u00a0−-]+$/.test(fn(d)) ? "num" : "", text:String(fn(d))})))))))),
    el("p", {class:"meta", text:"Figures are taken from each company's profile; open a profile to see the source and date for every value."}));
}

/* ---------- profile ---------- */
function summary(d){
  const p = el("div", {class:"panel summary"}, el("h2", {text:"Summary"}));
  const add = (...parts) => p.append(el("p", {}, parts));
  const legal = val(d,"legal_form"), mun = val(d,"municipality"), ind = val(d,"industry");
  const idRef = claim(d,"name")?.e;
  add(d.name, legal ? " is registered as " + legal : " is registered", mun ? " in " + mun : "", " (organisation number " + d.org + ")", refs(d, idRef), ". ", status(d)[0] === "Active in registry" ? "The registry shows no bankruptcy or liquidation." : status(d)[0] + " according to the registry.", refs(d, claim(d,"insolvency_status")?.e));
  add(ind ? "Registered activity: " + ind.label + " (industry code " + ind.code + ")." : "No industry code is registered.", refs(d, claim(d,"industry")?.e));
  const a = accounts(d), l = a[a.length-1];
  if (l) add("Latest filed accounts (" + (l.period?.fraDato||"?") + " to " + (l.period?.tilDato||"?") + "): revenue " + money(l.revenue) + " " + l.currency + ", operating result " + money(l.operating_result) + ", annual result " + money(l.annual_result) + ", equity " + money(l.equity) + ".", refs(d, claim(d,"annual_accounts")?.e), a.length > 1 ? " " + a.length + " years of accounts are listed below." : "");
  else add("No annual accounts were found in the public accounts register.", refs(d, claim(d,"annual_accounts")?.e));
  const emp = val(d,"employees");
  add(emp != null ? "The registry reports " + emp + " employees." : "The registry reports no employee count, so none is stated.", refs(d, claim(d,"employees")?.e));
  const r = activeRoles(d); if (r.length) add(r.length + " current roles are registered, for example " + r.slice(0,2).map(x => (Array.isArray(x.name) ? x.name.join(" ") : x.name) + " (" + x.role + ")").join(" and ") + ".", refs(d, claim(d,"roles")?.e));
  const site = val(d,"official_website");
  const wc = claim(d,"official_website"); const weak = site && wc.c != null && wc.c < 0.9;
  add(site ? (weak ? "The registry lists this website, but the page does not confirm it belongs to this exact company (it may be a brand, franchise or shared site): " : "A website was matched to this company: ") : "No website could be verified for this company. ", site ? link(site) : "", refs(d, wc?.e));
  const act = activity(d); if (act.length){ const latest = act.map(x => x.date).filter(Boolean).sort().pop(); add(act.length + " dated public items were found on the company site; the latest is dated " + latest + ".", refs(d, claim(d,"external_footprint")?.e)); }
  const ch = d.changes; add(ch.length ? ch.length + " changes since the previous run (listed below)." : "No earlier snapshot to compare with, so this run is the baseline. Later runs list any changes here and keep the earlier evidence.");
  return p;
}
function unknowns(d){
  const rows = FIELDS.filter(f => claim(d,f) && claim(d,f).s !== "available");
  const box = el("div", {class:"panel"}, el("h2", {text:"What is unknown"}));
  if (!rows.length){ box.append(el("p", {class:"empty", text:"Every tracked field was found."})); return box; }
  box.append(el("ul", {class:"plain"}, rows.map(f => { const c = claim(d,f); return el("li", {}, el("strong", {text:LABEL[f] + ": "}), STATE_TEXT[c.s] || c.s, ". ", c.s === "blocked" ? (d.website_note || "The source refused the request.") : NOT_FOUND_WHY[f], refs(d, c.e)); })));
  return box;
}
function factsTable(d){
  return el("div", {class:"panel"}, el("h2", {text:"Facts, sources and dates"}), el("div", {class:"tablewrap"}, el("table", {}, el("thead", {}, el("tr", {}, ["Field","Value","State","Source","Retrieved"].map(h => el("th", {text:h})))),
    el("tbody", {}, FIELDS.map(f => { const c = claim(d,f); if (!c) return null; const ev = d.evidence.find(e => e.id === c.e[0]);
      let shown = "–";
      if (c.s === "available"){ const v = c.v; shown = f === "industry" ? v.label + " (" + v.code + ")" : f === "insolvency_status" ? (v.bankrupt ? "Bankrupt" : v.liquidating ? "Under liquidation" : "Neither bankrupt nor under liquidation") : f === "annual_accounts" ? v.records.length + " filing(s), see below" : f === "roles" ? v.roles.length + " role entries, see below" : f === "locations" ? v.locations.length + " sub-unit(s), see below" : f === "group_structure" ? "Group data, see below" : f === "external_footprint" ? activity(d).length + " dated item(s), see below" : String(v); }
      return el("tr", {}, el("td", {text:LABEL[f]}), el("td", {}, f === "official_website" && c.s === "available" ? link(c.v) : shown), el("td", {}, el("span", {class:"state " + c.s, text:STATE_TEXT[c.s] || c.s})), el("td", {}, ev ? link(ev.source_url, "Source " + evNo(d, ev.id)) : "–"), el("td", {text: ev ? (ev.retrieved_at||"").slice(0,10) : "–"})); })))));
}
function finTable(d){
  const a = accounts(d);
  const box = el("div", {class:"panel"}, el("h2", {text:"Annual accounts (NOK)"}));
  if (!a.length){ box.append(el("p", {class:"empty", text:"No filed accounts found in the public accounts register."})); return box; }
  const cols = [["Period", r => (r.period?.fraDato||"?") + " – " + (r.period?.tilDato||"?")],["Revenue", r=>money(r.revenue)],["Operating result", r=>money(r.operating_result)],["Profit before tax", r=>money(r.profit_before_tax)],["Annual result", r=>money(r.annual_result)],["Assets", r=>money(r.assets)],["Equity", r=>money(r.equity)],["Debt", r=>money(r.debt)]];
  box.append(el("div", {class:"tablewrap"}, el("table", {}, el("thead", {}, el("tr", {}, cols.map(([h],i) => el("th", {class:i?"num":"", text:h})))), el("tbody", {}, [...a].reverse().map(r => el("tr", {}, cols.map(([,fn],i) => el("td", {class:i?"num":"", text:fn(r)}))))))), el("p", {class:"meta"}, "Source: ", link(d.evidence.find(e => e.id === claim(d,"annual_accounts")?.e[0])?.source_url || ""), refs(d, claim(d,"annual_accounts")?.e)));
  return box;
}
function rolesPanel(d){
  const v = val(d,"roles"); const box = el("div", {class:"panel"}, el("h2", {text:"Roles and officers"}));
  if (!v || !v.roles.length){ box.append(el("p", {class:"empty", text:"The registry lists no roles."})); return box; }
  const name = r => Array.isArray(r.name) ? r.name.join(" ") : r.name;
  box.append(el("div", {class:"tablewrap"}, el("table", {}, el("thead", {}, el("tr", {}, ["Name","Role","Last changed","Status"].map(h => el("th", {text:h})))), el("tbody", {}, v.roles.map(r => el("tr", {}, el("td", {text:name(r) + (r.organisation_number ? " (" + r.organisation_number + ")" : "")}), el("td", {text:r.role}), el("td", {text:r.last_changed || "–"}), el("td", {text:r.inactive ? "Inactive" : "Current"})))))), refs(d, claim(d,"roles")?.e));
  return box;
}
function locPanel(d){
  const v = val(d,"locations"); const box = el("div", {class:"panel"}, el("h2", {text:"Sub-units and locations"}));
  if (!v || !v.locations.length){ box.append(el("p", {class:"empty", text:"No registered sub-units."})); return box; }
  box.append(el("div", {class:"tablewrap"}, el("table", {}, el("thead", {}, el("tr", {}, ["Unit","Address","Industry"].map(h => el("th", {text:h})))), el("tbody", {}, v.locations.slice(0,50).map(u => { const a = u.address || {}; return el("tr", {}, el("td", {text:u.name + " (" + u.organisation_number + ")"}), el("td", {text:[(a.adresse||[]).join(", "), [a.postnummer, a.poststed].filter(Boolean).join(" "), a.land].filter(Boolean).join(", ")}), el("td", {text:u.industry ? u.industry.beskrivelse : "–"})); })))), el("p", {class:"meta", text:v.locations.length > 50 ? "Showing 50 of " + v.locations.length + "." : ""}), refs(d, claim(d,"locations")?.e));
  return box;
}
function groupPanel(d){
  const v = val(d,"group_structure"); const box = el("div", {class:"panel"}, el("h2", {text:"Group structure"}));
  if (!v){ box.append(el("p", {class:"empty", text:NOT_FOUND_WHY.group_structure}), refs(d, claim(d,"group_structure")?.e)); return box; }
  const kids = v.children || [];
  box.append(el("p", {text:"Group root: " + v.navn + " (" + v.organisasjonsnummer + ")."}), kids.length ? el("ul", {class:"plain"}, kids.slice(0,40).map(k => el("li", {text:k.navn + " (" + k.organisasjonsnummer + ") – " + (k.knytningsform?.beskrivelse || "member") + (k.grunnlag ? ", " + k.grunnlag : "") + (k.dato ? ", since " + k.dato : "")}))) : null, refs(d, claim(d,"group_structure")?.e));
  return box;
}
function activityPanel(d){
  const v = val(d,"external_footprint"); const box = el("div", {class:"panel"}, el("h2", {text:"Website and public activity"}));
  const site = val(d,"official_website");
  const wcl = claim(d,"official_website"); const weak = site && wcl.c != null && wcl.c < 0.9;
  box.append(el("p", {}, site ? [weak ? "Website listed in the registry (identity not confirmed by the page): " : "Verified website: ", link(site), refs(d, wcl?.e)] : "No website could be verified for this company."), d.website_note && site ? el("p", {class:"meta", text:"How it was matched: " + d.website_note}) : null);
  if (!v){ box.append(el("p", {class:"empty", text:NOT_FOUND_WHY.external_footprint})); return box; }
  const act = [...activity(d)].sort((a,b) => (b.date||"").localeCompare(a.date||""));
  if (act.length) box.append(el("h3", {text:"Dated items"}), el("ul", {class:"plain"}, act.slice(0,30).map(x => el("li", {}, (x.date || "undated") + " – ", link(x.url, x.title || x.url)))));
  if (v.contact && Object.keys(v.contact).length) box.append(el("p", {text:"Contact on the site: " + Object.entries(v.contact).map(([k,x]) => k + " " + x).join(", ")}));
  if (v.social_links && v.social_links.length) box.append(el("p", {}, "Social links listed on the company site: ", v.social_links.map((u,i) => [i ? " · " : "", link(typeof u === "string" ? u : u.url)])));
  if ((v.careers_urls||[]).length) box.append(el("p", {}, "Careers pages: ", v.careers_urls.map((u,i) => [i ? " · " : "", link(u)])));
  box.append(refs(d, claim(d,"external_footprint")?.e));
  return box;
}
function changesPanel(d){
  const box = el("div", {class:"panel"}, el("h2", {text:"What changed"}));
  if (!d.changes.length) box.append(el("p", {text:"No earlier snapshot was supplied, so this run is the baseline. On later runs, changed fields are listed here and the earlier evidence is kept."}));
  else box.append(el("ul", {class:"plain"}, d.changes.map(c => el("li", {text:typeof c === "string" ? c : JSON.stringify(c)}))));
  return box;
}
function evidencePanel(d){
  const det = el("details", {id:"evidence"}, el("summary", {text:"Evidence (" + d.evidence.length + " sources)"}));
  det.append(el("div", {class:"tablewrap"}, el("table", {}, el("thead", {}, el("tr", {}, ["#","Source","Type","Retrieved","SHA-256"].map(h => el("th", {text:h})))), el("tbody", {}, d.evidence.map((e,i) => el("tr", {id:"ev-" + e.id}, el("td", {text:i+1}), el("td", {}, link(e.source_url)), el("td", {text:e.source_class}), el("td", {text:e.retrieved_at}), el("td", {class:"hash", text:e.content_sha256 || "–"})))))));
  return el("div", {class:"panel"}, det);
}
function profileView(d){
  const [stText, stCls] = status(d);
  return el("div", {class:"profile"}, el("a", {class:"back", href:"#/", text:"← All companies"}),
    el("h2", {class:"name", text:d.name || d.org}), el("div", {class:"tags"}, el("span", {class:"tag " + stCls, text:stText}), el("span", {class:"tag", text:"Org. no. " + d.org}), el("span", {class:"tag", text:"Retrieved " + (d.run.completed_at || "").slice(0,10)})),
    summary(d), unknowns(d), changesPanel(d), el("div", {class:"grid2"}, finTable(d), rolesPanel(d)), el("div", {class:"grid2"}, locPanel(d), groupPanel(d)), activityPanel(d), factsTable(d), evidencePanel(d),
    el("p", {class:"meta", text:"Registry data: Brønnøysundregistrene (NLOD 2.0). Website facts are shown only when the page proves it belongs to this company."}));
}

/* ---------- router ---------- */
function render(){
  const h = location.hash.replace(/^#\/?/, "").split("/")[0];
  app.replaceChildren();
  if (h === "compare") app.append(compareView());
  else if (/^\d{9}$/.test(h) && BY.has(h)){ app.append(profileView(BY.get(h))); document.title = BY.get(h).name + " – FjordProof"; cmpBar(); return; }
  else app.append(listView());
  document.title = "FjordProof Company Profiles"; cmpBar();
}
window.addEventListener("hashchange", () => { render(); window.scrollTo(0,0); });
document.getElementById("sub").textContent = DATA.length + " Norwegian companies · run " + (DATA[0]?.run.run_id || "") + " · every fact linked to its source and date";
document.getElementById("theme").addEventListener("click", () => { const r = document.documentElement; const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches; r.dataset.theme = dark ? "light" : "dark"; try { localStorage.setItem("theme", r.dataset.theme); } catch(e){} });
try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch(e){}
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
