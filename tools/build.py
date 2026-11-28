#!/usr/bin/env python3
"""Build the roadmap page from the board snapshot and the chart's config.

Usage: build.py [--config config/roadmap.json] [--board data/board.json]
                [--out site/index.html] [--strict-releases]

The page is a status board: every label on it is also a control. Section
headings are the jump bar, the level rows filter the character roster, the
legend filters the lines, release tags filter the shipped work and character
tiles are picked into a team. This file holds the layout, the CSS and the
script that drives those controls, and no words: every word on the page comes
from the board snapshot (boxes, lines, page text, character levels and issue
types) or from the config's `legend`, `words` and size words, and the board's
`Words` card overrides the config's. Fails (exit 1, old page untouched) on
anything that would put wrong or internal information on the public page. An
unchanged snapshot gives a byte-identical page.
"""
import argparse
import html
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"\{([^{}]*)\}")
DATED_RE = re.compile(r"(\d{4}-\d\d-\d\d):\s+(.+)")
MAX_BYTES = 600_000


def fail(reason):
    sys.exit(f"build failed: {reason}")


def warn(msg):
    print(f"warn: {msg}", file=sys.stderr)


def esc(s):
    return html.escape(str(s), quote=True)


def put(template, **parts):
    """`template` escaped, with the already-escaped `parts` in its {placeholders}."""
    return re.sub(r"\{(\w+)\}", lambda m: parts.get(m.group(1), m.group(0)), esc(template))


def page_words(cfg, board):
    """The page's fixed phrases: the config's, unless the board's Words card says otherwise."""
    words = dict(cfg["words"])
    known = set(words) | {"legend_" + l["class"] for l in cfg["legend"]}
    for key, text in sorted(((board.get("page") or {}).get("words") or {}).items()):
        if key in known:
            words[key] = text
        else:
            warn(f"word '{key}' is not a phrase this page uses; ignored")
    return words


def _fallback(kind):
    return "now" if kind == "active" else "open"


def line_state(kind, line, releases, words):
    """Return (state, suffix) for a line; first matching rule wins."""
    cards = line.get("cards") or []
    if kind == "not_planned":
        return "no", ""
    if line.get("done") or (cards and all(c["done"] for c in cards)):
        return "done", ""
    if line.get("confirming"):
        return "verifying", ""
    if line.get("not_confirming"):
        return _fallback(kind), ""
    tag = line.get("release")
    if tag:
        if tag in releases:
            return "verifying", " " + words["in_release"].replace("{tag}", tag)
        warn(f"line {line['id']} names release {tag}, which is not a known release")
    if any(not c["done"] and c["needs_repro"] for c in cards):
        return "verifying", " " + words["fix_released"]
    return _fallback(kind), ""


# Which failure outranks which is a page rule, not board content; the level
# words, marks and captions come from the board snapshot.
SEVERITY = ("crash", "desync", "playable")


def character_level(char, types):
    """Return (level, open, fixed, awaiting) for one character."""
    open_keys, n_open, fixed, awaiting = set(), 0, 0, 0
    for issue in char["issues"]:
        if issue["done"]:
            fixed += 1
            continue
        if issue.get("needs_repro"):
            awaiting += 1
        key = types.get(issue.get("type"))
        if key is not None:
            n_open += 1
            open_keys.add(key)
    if char.get("ready"):
        level = "ready"
    else:
        level = next((k for k in SEVERITY if k in open_keys), "untested")
    return level, n_open, fixed, awaiting


def open_mix(char, types):
    """[(level, count)] of one character's open, counted issues, worst level first."""
    got = {}
    for issue in char["issues"]:
        key = None if issue["done"] else types.get(issue.get("type"))
        if key is not None:
            got[key] = got.get(key, 0) + 1
    return sorted(got.items(), key=lambda kv: (SEVERITY.index(kv[0]) if kv[0] in SEVERITY
                                               else len(SEVERITY), kv[0]))


def slug(name, used):
    """A URL-safe handle for a character name; card ids never reach the page."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "x"
    got, n = base, 1
    while got in used:
        n += 1
        got = f"{base}-{n}"
    used.add(got)
    return got


def version_key(tag):
    return tuple(int(p) for p in re.findall(r"\d+", tag)), tag


def fill(text, board):
    """The text with board counts filled in, or None when it holds an unknown token."""
    for tok in TOKEN_RE.findall(text):
        if tok != "verifying_open":
            warn(f"unknown token {{{tok}}}; line dropped")
            return None
    return text.replace("{verifying_open}", str(board["stats"]["verifying_open"]))


def fmt_time(ts):
    # "2026-11-25T05:30Z" -> "2026-11-25 05:30"; the stamp phrase names the zone
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)Z", ts)
    if not m:
        fail(f"bad exported_at '{ts}'")
    return f"{m.group(1)} {m.group(2)}"


CSS = r"""
:root{color-scheme:dark;--bg:#14161b;--panel:#1b1e25;--card:#262a33;--edge:#394050;--fg:#e8eaed;--mut:#9ea6b1;--acc:#4aa3ff;--on:#08121f;--tint:#1d2c40;--warn:#f0b429;--warnbg:#3a2f12;--ready:#4caf50;--playable:#8bc34a;--desync:#f0b429;--crash:#f0635a;--untested:#8d96a1;--c:var(--untested);--navh:2.9rem}
@media(prefers-color-scheme:light){:root{color-scheme:light;--bg:#f3f4f7;--panel:#e7eaef;--card:#fff;--edge:#c9ced8;--fg:#1b1d22;--mut:#56606c;--acc:#0b62c4;--on:#fff;--tint:#e3effc;--warn:#a86400;--warnbg:#fff1cf;--ready:#2e7d32;--playable:#4f7a16;--desync:#a86400;--crash:#c62828;--untested:#68717d}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{scroll-behavior:smooth;scroll-padding-top:calc(var(--navh) + .75rem);-webkit-text-size-adjust:100%}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
a{color:var(--acc)}
button,input{font:inherit;color:inherit}
button{cursor:pointer;margin:0}
html:not(.js) button{cursor:default}
html:not(.js) .jsonly{display:none}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
header,main,footer{max-width:1400px;margin:0 auto;padding:0 1rem}
header{padding-top:1.5rem}
header p{margin:.6rem 0 0}
.brand{display:flex;flex-wrap:wrap;gap:.8rem 2rem;align-items:center;justify-content:space-between}
h1{margin:0;font-size:clamp(1.9rem,6vw,3rem);line-height:1.05;font-style:italic;font-weight:800;letter-spacing:-.02em}
.brand p{margin:.3rem 0 0;color:var(--mut)}
.links{display:flex;flex-wrap:wrap;gap:.5rem}
.links a{padding:.45rem 1.1rem;border:1px solid var(--edge);border-radius:999px;background:var(--card);color:var(--fg);font-weight:600;text-decoration:none}
.links a:first-child{background:var(--acc);border-color:var(--acc);color:var(--on)}
.links a:hover{border-color:var(--acc)}
header .goal{margin-top:1.1rem;max-width:62rem;font-size:1.2rem;line-height:1.4}
.notice,.banner{max-width:62rem;padding:.5rem .8rem;border-left:4px solid var(--warn);border-radius:4px;background:var(--warnbg)}
header .stamp{color:var(--mut);font-size:.875rem}
.jump{position:sticky;top:0;z-index:5;display:flex;gap:.2rem;align-items:center;height:var(--navh);margin-top:1rem;padding:0 max(.6rem,calc(50% - 700px + .6rem));overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--edge);background:var(--bg)}
.jump::-webkit-scrollbar{display:none}
.jump a{flex:none;padding:.3rem .7rem;border-radius:999px;color:var(--mut);font-weight:600;text-decoration:none;white-space:nowrap}
.jump a:hover{color:var(--fg)}
.jump a[aria-current]{background:var(--card);color:var(--fg)}
.jump b,.chip b{font-weight:600;font-size:.85em;font-variant-numeric:tabular-nums}
.jump b{margin-left:.4rem;opacity:.75}
main>*{margin-top:2.2rem}
h2{margin:0 0 .7rem;font-size:1.5rem;line-height:1.2;font-style:italic;font-weight:800}
h2 small{font-size:.85rem;font-style:normal;font-weight:400;color:var(--mut)}
h3{margin:0 0 .35rem;font-size:1.05rem;line-height:1.3}
.l-ready{--c:var(--ready)}.l-playable{--c:var(--playable)}.l-desync{--c:var(--desync)}.l-crash{--c:var(--crash)}.l-untested{--c:var(--untested)}
.banner{margin:0 0 1rem}
.meter{display:flex;gap:2px;height:.6rem;margin-bottom:.6rem;border-radius:999px;overflow:hidden}
.meter i,.mix i{background:var(--c)}
.levels{display:grid;gap:.5rem;grid-template-columns:repeat(auto-fit,minmax(10.5rem,1fr))}
.lv{display:grid;grid-template-columns:auto 1fr auto;gap:0 .5rem;align-items:baseline;padding:.5rem .7rem;border:1px solid var(--edge);border-radius:8px;background:var(--card);text-align:left}
.m{color:var(--c)}
.n,.lv em{font-weight:700;font-style:normal}
.lv em{font-variant-numeric:tabular-nums}
.t{color:var(--mut);font-size:.8rem}
.lv .t{grid-column:1/-1}
.lv:hover,.chip:enabled:hover,button.tile:hover{border-color:var(--acc)}
.lv[aria-pressed=true],.chip[aria-pressed=true]{border-color:var(--acc);background:var(--tint);box-shadow:inset 0 0 0 1px var(--acc)}
.tools{position:sticky;top:var(--navh);z-index:4;display:flex;flex-wrap:wrap;gap:.5rem .8rem;align-items:center;margin:.3rem -.5rem;padding:.6rem .5rem;background:var(--bg)}
#find{flex:1 1 9rem;max-width:20rem;min-width:0;height:2.3rem;padding:0 .9rem;border:1px solid var(--edge);border-radius:999px;background:var(--card)}
#find.miss{border-color:var(--crash)}
.team{display:contents}
.verdict{flex:0 1 auto;display:flex;gap:0 .45rem;align-items:baseline;margin:0}
.verdict:empty{display:none}
.verdict .n{color:var(--c);white-space:nowrap}
.picks{flex:1 1 auto;display:flex;gap:.4rem;min-width:0;margin:0;padding:0;overflow-x:auto;scrollbar-width:none;list-style:none}
.picks::-webkit-scrollbar{display:none}
.picks li{flex:none}
.slot{width:3.2rem;height:2.3rem;border:1px dashed var(--edge);border-radius:999px}
.pick{display:block;height:2.3rem;padding:0 .8rem;border:1px solid var(--c);border-radius:999px;background:var(--card);white-space:nowrap}
.pick::after{content:"✕";margin-left:.5rem;color:var(--mut);font-size:.8em}
.pick i,.tile b i{font-style:normal;color:var(--c)}
.tiles{display:grid;gap:.5rem;grid-template-columns:repeat(auto-fill,minmax(10rem,1fr))}
.tile{display:flex;flex-direction:column;padding:.45rem .6rem .5rem;border:1px solid transparent;border-left:4px solid var(--c);border-radius:6px;background:var(--card);text-align:left}
.tile span{font-size:.85rem}
.tile small{color:var(--mut);font-size:.75rem}
.tile .mix{display:flex;gap:2px;height:4px;margin-top:auto;border-radius:2px;overflow:hidden}
.tile small{margin-bottom:.4rem}
.tile[aria-pressed=true]{border-color:var(--acc);border-left-color:var(--c);background:var(--tint);box-shadow:0 0 0 1px var(--acc)}
.legend,.rels{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.9rem}
.chip{display:inline-flex;gap:.45rem;align-items:baseline;padding:.3rem .75rem;border:1px solid var(--edge);border-radius:999px;background:var(--card);font-size:.9rem;text-align:left}
.chip:disabled{opacity:.5;cursor:default}
.now{--m:"▶";--mc:var(--acc)}.open{--m:"○";--mc:var(--mut)}.verifying{--m:"◷";--mc:var(--desync)}.done{--m:"✔";--mc:var(--ready)}.no{--m:"✕";--mc:var(--crash)}
.legend .chip::before,.lines li::before{content:var(--m,"○");color:var(--mc,var(--mut))}
.lanes{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(min(100%,17rem),1fr));align-items:start}
.lane{padding:.9rem;border-top:3px solid var(--edge);border-radius:10px;background:var(--panel)}
.lane.k-active{border-top-color:var(--acc)}
.lane.k-not_planned{border-top-color:var(--crash)}
.lane h2{margin:0}
.lane h2 small{display:block;margin-top:.15rem}
.box{padding:.8rem .9rem;border-radius:8px;background:var(--card)}
.lane .box{margin-top:.7rem}
.box>p{margin:.2rem 0 .5rem;color:var(--mut);font-size:.92rem}
.box h3{display:flex;gap:.6rem;align-items:baseline;justify-content:space-between}
.size{flex:none;padding:0 .4rem;border:1px solid var(--edge);border-radius:4px;color:var(--mut);font-size:.72rem;font-weight:400;white-space:nowrap}
.lines,.plain,.timeline{margin:0;padding:0;list-style:none}
.lines{font-size:.95rem}
.lines li{position:relative;margin:.35rem 0;padding-left:1.5rem}
.lines li::before{position:absolute;left:0}
.lane li.done{text-decoration:line-through;opacity:.6}
.lines a{white-space:nowrap}
.past .boxes{columns:19rem;column-gap:1rem}
.past .box{display:inline-block;width:100%;margin-bottom:1rem;vertical-align:top;break-inside:avoid}
.notes{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(min(100%,22rem),1fr));align-items:start}
.plain li{margin:.3rem 0 .3rem 1.2rem;list-style:disc}
.timeline li{position:relative;display:grid;grid-template-columns:auto 1fr;gap:.8rem;margin-left:.3rem;padding:.2rem 0 .2rem 1.1rem;border-left:2px solid var(--edge)}
.timeline li::before{content:"";position:absolute;left:-5px;top:.75rem;width:8px;height:8px;border-radius:50%;background:var(--acc)}
.timeline time{color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}
footer{margin-top:2.5rem;padding-bottom:2rem;color:var(--mut);font-size:.9rem}
@media(max-width:640px){.verdict .t{display:none}.tools{gap:.5rem}.picks{flex-basis:100%}}
""".strip()

# The script only shows, hides and copies what the page already holds: it has
# no words of its own, makes no requests, and without it every section still
# reads top to bottom. The picked team lives in the address (?team=) and in
# this browser's localStorage.
JS = r"""
(function(){
"use strict";
var d=document,f={level:"",state:"",rel:"",q:""};
d.documentElement.classList.add("js");
function all(q,e){return Array.prototype.slice.call((e||d).querySelectorAll(q))}
function shown(e){return !e.hidden}
var tiles=all(".tile"),chips=all("[data-f]"),lines=all(".lines li"),boxes=all(".lane .box,.past .box"),
groups=all(".lane,.past"),find=d.getElementById("find"),tray=d.getElementById("team"),team=[],by={};
function apply(){
chips.forEach(function(b){b.setAttribute("aria-pressed",String(f[b.getAttribute("data-f")]===b.value))});
var n=0;
tiles.forEach(function(t){
t.hidden=!!(f.level&&t.getAttribute("data-level")!==f.level)||!!(f.q&&t.getAttribute("data-name").indexOf(f.q)<0);
if(!t.hidden)n++});
if(find)find.classList.toggle("miss",!n);
lines.forEach(function(li){
li.hidden=!!(f.state&&!li.classList.contains(f.state))||!!(f.rel&&li.closest(".past")&&li.getAttribute("data-rel")!==f.rel)});
boxes.forEach(function(b){b.hidden=!all(".lines li",b).some(shown)});
groups.forEach(function(g){g.hidden=!all(".box",g).some(shown)});
}
chips.forEach(function(b){b.addEventListener("click",function(){
var k=b.getAttribute("data-f");
f[k]=f[k]===b.value?"":b.value;
if(k==="rel"&&f.rel&&f.state!=="done")f.state="";
if(k==="state"&&f.state&&f.state!=="done")f.rel="";
apply()})});
all('a[href^="#"]').forEach(function(a){a.addEventListener("click",function(){
var t=d.getElementById(a.getAttribute("href").slice(1)),rel=a.getAttribute("data-rel");
if(rel){f.rel=rel;if(f.state!=="done")f.state="";apply()}
else if(t&&t.hidden){f.state=f.rel="";apply()}})});
if(tray){
var size=+tray.getAttribute("data-size"),rank=tray.getAttribute("data-rank").split(" "),
verdict=tray.querySelector(".verdict"),picks=tray.querySelector(".picks");
tiles.forEach(function(t){by[t.value]=t;t.addEventListener("click",function(){toggle(t.value)})});
picks.addEventListener("click",function(e){
var b=e.target.closest("button");
if(b){toggle(b.value);by[b.value].focus({preventScroll:true})}});
var draw=function(){
var worst="",at=rank.length+1;
picks.replaceChildren();verdict.replaceChildren();
tiles.forEach(function(t){t.setAttribute("aria-pressed",String(team.indexOf(t.value)>=0))});
for(var i=0;i<size;i++){
var li=d.createElement("li"),t=by[team[i]];
if(t){
var b=d.createElement("button"),lv=t.getAttribute("data-level"),r=rank.indexOf(lv);
if(r<0)r=rank.length;
if(r<at){at=r;worst=lv}
b.type="button";b.value=t.value;b.className="pick l-"+lv;b.setAttribute("aria-pressed","true");
b.appendChild(t.querySelector("b").cloneNode(true));li.appendChild(b);
}else{li.className="slot";li.setAttribute("aria-hidden","true")}
picks.appendChild(li)}
verdict.className="verdict l-"+worst;
all(".lv").forEach(function(c){if(worst&&c.value===worst)all(".m,.n,.t",c).forEach(function(x){verdict.appendChild(x.cloneNode(true))})});
};
var link=function(){
var s=team.join(",");
try{history.replaceState(null,"",location.pathname+(s?"?team="+s:"")+location.hash)}catch(e){}
};
var toggle=function(v){
var i=team.indexOf(v);
if(i>=0)team.splice(i,1);else{team.push(v);if(team.length>size)team.shift()}
draw();link();
try{localStorage.setItem("team",team.join(","))}catch(e){}
};
var m=/[?&]team=([^&#]*)/.exec(location.search),s=null;
if(m)s=decodeURIComponent(m[1]);else try{s=localStorage.getItem("team")}catch(e){}
(s||"").split(",").forEach(function(v){if(by[v]&&team.indexOf(v)<0&&team.length<size)team.push(v)});
draw();link();
}
if(find){
find.addEventListener("input",function(){f.q=find.value.trim().toLowerCase();apply()});
find.addEventListener("keydown",function(e){
var t=tiles.filter(shown)[0];
if(e.key==="Enter"&&f.q&&t){
if(tray&&team.indexOf(t.value)<0)t.click();
find.value="";f.q="";apply()}});
}
var nav=d.querySelector(".jump"),links=all(".jump a"),seen={};
function mark(){
var end=window.innerHeight+window.scrollY>=d.documentElement.scrollHeight-2,
cur=end?links[links.length-1]:links.filter(function(a){return seen[a.getAttribute("href").slice(1)]})[0];
links.forEach(function(a){
if(a!==cur)a.removeAttribute("aria-current");
else if(!a.hasAttribute("aria-current")){a.setAttribute("aria-current","true");nav.scrollLeft=a.offsetLeft-(nav.clientWidth-a.offsetWidth)/2}});
}
if(nav&&"IntersectionObserver" in window){
var io=new IntersectionObserver(function(es){
es.forEach(function(e){seen[e.target.id]=e.isIntersecting});mark()},{rootMargin:"-20% 0px -70% 0px"});
links.forEach(function(a){var t=d.getElementById(a.getAttribute("href").slice(1));if(t)io.observe(t)});
window.addEventListener("scroll",mark,{passive:true});
}
})();
""".strip()

ROAD = ("active", "upcoming", "not_planned")


def release_mentioned(board, tag):
    """True when `tag` shipped a line or is named in the background section."""
    name = re.compile(r"(?<![\w.])" + re.escape(tag) + r"(?!\.?\w)")
    for s in board["sections"]:
        for box in s["boxes"]:
            for line in box["lines"]:
                if s["kind"] == "shipped" and line.get("release") == tag:
                    return True
                if s["kind"] == "background" and name.search(line["text"]):
                    return True
    return False


def characters(cfg, board, words):
    """Return (html, tile count) for the roster: level bar, level filters, team tray, tiles."""
    page = board["page"]
    levels = board.get("levels") or []
    chars = board.get("characters") or []
    types = board.get("types") or {}
    if not (levels and chars):
        return "", 0
    team = cfg.get("team") or {}
    size = int(team.get("size") or 0)
    by_key = {lv["key"]: lv for lv in levels}
    rows, used = [], set()
    for c in sorted(chars, key=lambda c: c["name"].lower()):
        level, n_open, fixed, awaiting = character_level(c, types)
        lv = by_key.get(level)
        if lv is None:
            warn(f"level '{level}' has no card; tile rendered without a mark")
            lv = {"key": level, "mark": "", "label": level, "text": ""}
        rows.append((c["name"], level, n_open, fixed, awaiting, lv, open_mix(c, types)))
    counts = {lv["key"]: sum(1 for r in rows if r[1] == lv["key"]) for lv in levels}
    most = max(r[2] for r in rows) or 1
    heading = esc(page["characters_heading"])
    out = []
    b = out.append
    b(f'<section id="characters">\n<h2>{heading}</h2>\n')
    if page.get("characters_banner"):
        b(f'<p class="banner">{esc(page["characters_banner"])}</p>\n')
    b('<div class="meter" aria-hidden="true">'
      + "".join(f'<i class="l-{esc(lv["key"])}" style="flex:{counts[lv["key"]]}"></i>'
                for lv in levels if counts[lv["key"]]) + "</div>\n")
    b('<div class="levels">\n')
    for lv in levels:
        key = esc(lv["key"])
        b(f'<button type="button" class="lv l-{key}" data-f="level" value="{key}" aria-pressed="false">'
          f'<b class="m">{esc(lv["mark"])}</b> <span class="n">{esc(lv["label"])}</span> '
          f'<em>{counts[lv["key"]]}</em> <small class="t">{esc(lv["text"])}</small></button>\n')
    b('</div>\n<div class="tools jsonly">\n'
      f'<input type="search" id="find" aria-label="{heading}" placeholder="{heading}" '
      'autocomplete="off" spellcheck="false">\n')
    if size > 0:
        rank = " ".join(esc(k) for k in team.get("worst_first") or [])
        b(f'<div class="team" id="team" data-size="{size}" data-rank="{rank}">'
          '<p class="verdict" aria-live="polite"></p><ol class="picks"></ol></div>\n')
    b('</div>\n<div class="tiles">\n')
    tag = "button" if size > 0 else "div"
    for name, level, n_open, fixed, awaiting, lv, mix in rows:
        tally = put(words["counts"], open=str(n_open), fixed=str(fixed))
        if awaiting:
            tally += " · " + put(words["awaiting"], n=str(awaiting))
        low = name.lower()
        attrs = (f'class="tile l-{esc(level)}" data-level="{esc(level)}" '
                 f'data-name="{esc(low + " " + re.sub(r"[^a-z0-9]+", "", low))}"')
        if size > 0:
            attrs = f'type="button" {attrs} value="{slug(name, used)}" aria-pressed="false"'
        bar = "".join(f'<i class="l-{esc(k)}" style="flex:{n}"></i>' for k, n in mix)
        b(f'<{tag} {attrs}><b><i>{esc(lv["mark"])}</i> {esc(name)}</b>'
          f'<span>{esc(lv["label"])}</span><small>{tally}</small>'
          f'<span class="mix" aria-hidden="true" style="width:{round(100 * n_open / most)}%">{bar}</span>'
          f'</{tag}>\n')
    b("</div>\n</section>\n")
    return "".join(out), len(rows)


def render(cfg, board, strict_releases=False):
    if board.get("schema") != 2:
        fail("snapshot schema is not 2")
    releases = set(board.get("releases") or [])
    repo = (cfg.get("releases") or {}).get("repo")
    page = board["page"]
    words = page_words(cfg, board)
    sections = board["sections"]

    lr = board.get("latest_release")
    if lr and not release_mentioned(board, lr["tag"]):
        if strict_releases:
            fail(f"latest release {lr['tag']} not mentioned in shipped")
        warn(f"latest release {lr['tag']} is not listed in shipped")

    def box_head(box):
        size = (f' <span class="size">{put(words["size"], size=esc(box["size"]))}</span>'
                if box.get("size") else "")
        got = f'<article class="box">\n<h3>{esc(box["title"])}{size}</h3>\n'
        if box.get("blurb"):
            got += f"<p>{esc(box['blurb'])}</p>\n"
        return got

    def heading(s):
        note = f' <small>{esc(s["note"])}</small>' if s.get("note") else ""
        return f'<h2>{esc(s["heading"])}{note}</h2>\n'

    jump = []      # (anchor, heading, count or None), in page order
    tally = {}     # line state -> lines on the page
    body = []
    b = body.append

    # The roster opens the page: it is the one block the board's column order
    # does not place, so it sits here on purpose.
    roster, n_tiles = characters(cfg, board, words)
    if roster:
        jump.append(("characters", page["characters_heading"], n_tiles))

    lanes = []
    for s in sections:
        if s["kind"] not in ROAD:
            continue
        lane, n = [], 0
        for box in s["boxes"]:
            lane.append(box_head(box) + '<ul class="lines">\n')
            for line in box["lines"]:
                state, suffix = line_state(s["kind"], line, releases, words)
                text = fill(line["text"], board)
                if text is None:
                    continue
                if state == "done":
                    warn(f"line {line['id']} is all done; move it to shipped")
                tally[state] = tally.get(state, 0) + 1
                n += 1
                lane.append(f'<li class="{state}">{esc(text + suffix)}</li>\n')
            lane.append("</ul>\n</article>\n")
        lanes.append(f'<section id="{esc(s["id"])}" class="lane k-{esc(s["kind"])}">\n'
                     + heading(s) + "".join(lane) + "</section>\n")
        jump.append((s["id"], s["heading"], n))

    past, shipped_tags = [], {}
    for s in sections:
        if s["kind"] != "shipped":
            continue
        cards, n = [], 0
        for box in s["boxes"]:
            cards.append(box_head(box) + '<ul class="lines">\n')
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is None:
                    continue
                tag, rel = line.get("release"), ""
                if tag:
                    shipped_tags[tag] = shipped_tags.get(tag, 0) + 1
                    rel = f' data-rel="{esc(tag)}"'
                    tail = (f' <a href="https://github.com/{esc(repo)}/releases/tag/{esc(tag)}">'
                            f"{esc(tag)}</a>") if repo else f" {esc(tag)}"
                elif line.get("date"):
                    tail = f" ({esc(line['date'])})"
                else:
                    tail = ""
                tally["done"] = tally.get("done", 0) + 1
                n += 1
                cards.append(f'<li class="done"{rel}>{esc(text)}{tail}</li>\n')
            cards.append("</ul>\n</article>\n")
        past.append((s, "".join(cards)))
        jump.append(("shipped", s["heading"], n))

    notes = []
    for s in sections:
        if s["kind"] != "background":
            continue
        cards = []
        for box in s["boxes"]:
            texts = [t for t in (fill(line["text"], board) for line in box["lines"]) if t is not None]
            dated = [DATED_RE.fullmatch(t) for t in texts]
            cards.append(f'<article class="box">\n<h3>{esc(box["title"])}</h3>\n')
            if box.get("blurb"):
                cards.append(f"<p>{esc(box['blurb'])}</p>\n")
            if texts and all(dated):
                cards.append('<ol class="timeline">\n' + "".join(
                    f'<li><time datetime="{m.group(1)}">{m.group(1)}</time> '
                    f"<span>{esc(m.group(2))}</span></li>\n" for m in dated) + "</ol>\n")
            else:
                cards.append('<ul class="plain">\n'
                             + "".join(f"<li>{esc(t)}</li>\n" for t in texts) + "</ul>\n")
            cards.append("</article>\n")
        notes.append((s, "".join(cards)))
        jump.append(("history", s["heading"], None))

    b(roster)
    b('<div class="road">\n<div class="legend">\n')
    for l in cfg["legend"]:
        cls, n = l["class"], tally.get(l["class"], 0)
        b(f'<button type="button" class="chip {esc(cls)}" data-f="state" value="{esc(cls)}" '
          f'aria-pressed="false"{"" if n else " disabled"}>'
          f'<span>{esc(words.get("legend_" + cls, l["text"]))}</span> <b>{n}</b></button>\n')
    b('</div>\n<div class="lanes">\n' + "".join(lanes) + "</div>\n</div>\n")
    for s, cards in past:
        b(f'<section id="shipped" class="past">\n{heading(s)}')
        if shipped_tags:
            b('<div class="rels">\n' + "".join(
                f'<button type="button" class="chip" data-f="rel" value="{esc(tag)}" '
                f'aria-pressed="false"><span>{esc(tag)}</span> <b>{shipped_tags[tag]}</b></button>\n'
                for tag in sorted(shipped_tags, key=version_key, reverse=True)) + "</div>\n")
        b(f'<div class="boxes">\n{cards}</div>\n</section>\n')
    for s, cards in notes:
        b(f'<section id="history">\n{heading(s)}<div class="notes">\n{cards}</div>\n</section>\n')

    out = []
    w = out.append
    w('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    w(f'<title>{esc(page["title"])}</title>\n<meta name="robots" content="noindex, nofollow">\n'
      f"<style>\n{CSS}\n</style>\n</head>\n<body>\n")
    w(f'<header>\n<div class="brand">\n<div>\n<h1>{esc(page["title"])}</h1>\n')
    if page.get("tagline"):
        w(f'<p>{esc(page["tagline"])}</p>\n')
    w("</div>\n")
    if page.get("links"):
        w('<nav class="links">' + "".join(f'<a href="{esc(l["href"])}">{esc(l["label"])}</a>'
                                          for l in page["links"]) + "</nav>\n")
    w("</div>\n")
    if page.get("goal"):
        w(f'<p class="goal"><strong>{esc(page["goal"])}</strong></p>\n')
    if page.get("notice"):
        w(f'<p class="notice">{esc(page["notice"])}</p>\n')
    stamp = put(words["stamp"], time=esc(fmt_time(board["exported_at"])))
    if lr:
        # Inside the page the latest tag is a shortcut to what that release shipped.
        tag = esc(lr["tag"])
        if lr["tag"] in shipped_tags:
            tag = f'<a href="#shipped" data-rel="{tag}">{tag}</a>'
        stamp += " · " + put(words["latest"], tag=tag, date=esc(lr["date"]))
    w(f'<p class="stamp">{stamp}</p>\n</header>\n')
    w('<nav class="jump">' + "".join(
        f'<a href="#{esc(anchor)}">{esc(text)}' + (f"<b>{n}</b>" if n is not None else "") + "</a>"
        for anchor, text, n in jump) + "</nav>\n")
    w("<main>\n")
    out.extend(body)
    w("</main>\n")
    if page.get("footer"):
        w(f'<footer><p>{esc(page["footer"])}</p></footer>\n')
    w(f"<script>\n{JS}\n</script>\n</body>\n</html>\n")
    got = "".join(out)

    m = re.compile(cfg["forbidden"]).search(got)
    if m:
        fail(f"forbidden text '{m.group(0)}' near '{got[max(0, m.start() - 40):m.end() + 40]}'")
    if len(got.encode()) >= MAX_BYTES:
        fail(f"page is {len(got.encode())} bytes")
    return got


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the roadmap page.")
    ap.add_argument("--config", default="config/roadmap.json")
    ap.add_argument("--board", default="data/board.json")
    ap.add_argument("--out", default="site/index.html")
    ap.add_argument("--strict-releases", action="store_true",
                    help="fail when the latest release is not listed in the shipped section")
    a = ap.parse_args(argv)
    try:
        with open(a.config) as f:
            cfg = json.load(f)
        with open(a.board) as f:
            board = json.load(f)
    except (OSError, ValueError) as e:
        fail(str(e))
    try:
        page = render(cfg, board, a.strict_releases)
        types = board.get("types") or {}
        levels = [character_level(c, types)[0] for c in (board.get("characters") or [])]
    except (KeyError, TypeError) as e:
        fail(f"malformed input: {e!r}")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, a.out)
    n_up = sum(len(box["lines"]) for s in board["sections"]
               if s["kind"] in ROAD for box in s["boxes"])
    n_sh = sum(len(box["lines"]) for s in board["sections"] if s["kind"] == "shipped"
               for box in s["boxes"])
    line = f"ok: {n_up} upcoming, {n_sh} shipped"
    if board.get("levels"):
        line += (f", characters tiles={len(board.get('characters') or [])} "
                 + " ".join(f"{k}={levels.count(k)}" for k in SEVERITY + ("untested", "ready")))
    print(line)


if __name__ == "__main__":
    main()
