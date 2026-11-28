#!/usr/bin/env python3
"""Build the roadmap page from the board snapshot and the chart's config.

Usage: build.py [--config config/roadmap.json] [--board data/board.json]
                [--out site/index.html] [--strict-releases]

The page is one static file: a status header (with the latest release and the
roster bar), a sticky section index with a client-side find, the board's
horizons, the character roster grouped by level, the shipped log newest first,
and the background notes. Every word a visitor reads comes from the board
snapshot (page text, headings and notes, box titles and blurbs, line text,
character names, level words). Page furniture -- the legend, the state marks,
the size words and the short labels around counts and the find box -- lives in
config/roadmap.json under "legend" and "ui", never here.

Fails (exit 1, old page untouched) on anything that would put wrong or
internal information on the public page. An unchanged snapshot gives a
byte-identical page.
"""
import argparse
import html
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"\{([^{}]*)\}")
MAX_BYTES = 600_000

FAVICON = (
    "data:image/svg+xml,%3Csvg%20xmlns='http%3A//www.w3.org/2000/svg'%20"
    "viewBox='0%200%2032%2032'%3E%3Crect%20width='32'%20height='32'%20rx='7'"
    "%20fill='%230a0c11'/%3E%3Cpath%20d='M8,8h16v5h-5v11h-6V13H8z'%20"
    "fill='%23f5b53d'/%3E%3C/svg%3E"
)


def fail(reason):
    sys.exit(f"build failed: {reason}")


def esc(s):
    return html.escape(str(s), quote=True)


def find_text(*parts):
    """Lowercased, whitespace-collapsed text for the client-side find."""
    words = [" ".join(str(p).split()) for p in parts if p]
    return " ".join(words).lower()


def _fallback(kind):
    return "now" if kind == "active" else "open"


def line_state(kind, line, releases):
    """Return (state, release tag or None, awaiting confirmation) for a line.

    The state is the mark class; the tag names the release the fix is in; the
    flag asks for the "fix released, confirming" wording. First matching rule
    wins, exactly as on the board.
    """
    cards = line.get("cards") or []
    if kind == "not_planned":
        return "no", None, False
    if line.get("done") or (cards and all(c["done"] for c in cards)):
        return "done", None, False
    if line.get("confirming"):
        return "verifying", None, False
    if line.get("not_confirming"):
        return _fallback(kind), None, False
    tag = line.get("release")
    if tag:
        if tag in releases:
            return "verifying", tag, False
        print(f"warn: line {line['id']} names release {tag}, which is not a known release",
              file=sys.stderr)
    if any(not c["done"] and c["needs_repro"] for c in cards):
        return "verifying", None, True
    return _fallback(kind), None, False


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


def character_groups(board):
    """The roster as [(level card, rows)] in the board's level order.

    A row is (name, level, open, fixed, awaiting). A character whose level has
    no card still gets a group of its own at the end, so it never disappears;
    the group is bare because the board holds no words for it.
    """
    levels = board.get("levels") or []
    types = board.get("types") or {}
    by_key = {}
    groups = []
    for level in levels:
        group = {"level": level, "rows": []}
        by_key[level["key"]] = group
        groups.append(group)
    extra = {}
    for char in sorted(board.get("characters") or [], key=lambda c: c["name"].lower()):
        level, n_open, fixed, awaiting = character_level(char, types)
        group = by_key.get(level)
        if group is None:
            print(f"warn: level '{level}' has no card; character {char['name']!r} kept in a bare group",
                  file=sys.stderr)
            group = extra.get(level)
            if group is None:
                group = {"level": {"key": level, "mark": "", "label": level, "text": ""},
                         "rows": []}
                extra[level] = group
                groups.append(group)
        group["rows"].append((char["name"], level, n_open, fixed, awaiting))
    return groups


def _digits(text):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else 0


def _ship_key(line, order):
    """Sort key for shipped work: newest release first, then newest date, rest last."""
    tag = line.get("release")
    if tag in order:
        return (0, order[tag])
    date = line.get("date") or ""
    if date:
        return (1, -_digits(date))
    return (2, 0)


def shipped_boxes(board):
    """[(box, lines)] newest area first, its lines newest first.

    Release order is the board snapshot's `releases` list, which the exporter
    takes from the GitHub API newest first. Within one release the board's own
    order is kept.
    """
    order = {tag: i for i, tag in enumerate(board.get("releases") or [])}
    boxes = []
    for section in board["sections"]:
        if section["kind"] != "shipped":
            continue
        for box in section["boxes"]:
            rows = sorted(((_ship_key(line, order), i, line)
                           for i, line in enumerate(box["lines"])),
                          key=lambda row: (row[0], row[1]))
            newest = min((row[0] for row in rows), default=(3, 0))
            boxes.append((newest, box, [row[2] for row in rows]))
    boxes.sort(key=lambda item: item[0])
    return [(box, lines) for _, box, lines in boxes]


def shipped_log(board):
    """Every shipped line in one list, newest first."""
    order = {tag: i for i, tag in enumerate(board.get("releases") or [])}
    rows = []
    for section in board["sections"]:
        if section["kind"] != "shipped":
            continue
        for box in section["boxes"]:
            for line in box["lines"]:
                rows.append((_ship_key(line, order), len(rows), line))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [line for _, _, line in rows]


def fill(text, board):
    """The text with board counts filled in, or None when it holds an unknown token."""
    for tok in TOKEN_RE.findall(text):
        if tok != "verifying_open":
            print(f"warn: unknown token {{{tok}}}; line dropped", file=sys.stderr)
            return None
    return text.replace("{verifying_open}", str(board["stats"]["verifying_open"]))


def fmt_time(ts):
    # "2026-11-25T05:30Z" -> "2026-11-25 05:30 UTC"
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)Z", ts)
    if not m:
        fail(f"bad exported_at '{ts}'")
    return f"{m.group(1)} {m.group(2)} UTC"


def size_badge(box):
    size = box.get("size")
    if not size:
        return ""
    return (f'<span class="size" data-size="{esc(size)}">'
            '<span class="meter" aria-hidden="true"><i></i><i></i><i></i></span>'
            f"{esc(size)}</span>")


CSS = """
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
:root{
  color-scheme:dark;
  --bg:#0a0c11;--panel:#131721;--panel-2:#171c27;
  --line:#232a37;--line-2:#333d4f;
  --ink:#eef1f7;--ink-2:#b6bfcf;--ink-3:#8892a4;
  --amber:#f5b53d;--blue:#6aa9ff;--green:#46c97e;--red:#ff6b6b;--lime:#8fc93a;
  --on-amber:#1a1204;
  --radius:14px;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --wrap:1280px;
}
@media (prefers-color-scheme:light){
  :root{
    color-scheme:light;
    --bg:#f7f8fb;--panel:#fff;--panel-2:#f3f5f9;
    --line:#e1e6ef;--line-2:#c6cedd;
    --ink:#10141c;--ink-2:#3a4454;--ink-3:#5c697d;
    --amber:#9a6400;--blue:#0b5bd3;--green:#0f7a43;--red:#c0272d;--lime:#4f7a00;
    --on-amber:#fff;
  }
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 var(--sans)}
a{color:var(--blue);text-underline-offset:2px}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.wrap{max-width:var(--wrap);margin:0 auto;padding:0 clamp(14px,3vw,28px)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.skip{position:absolute;left:-9999px;top:0;z-index:50;background:var(--amber);color:var(--on-amber);padding:.55rem .9rem;border-radius:0 0 10px 0;font-size:.85rem;font-weight:600;text-decoration:none}
.skip:focus{left:0}
[id]{scroll-margin-top:80px}

/* marks: one box per state, filled by the legend's class */
.now{--mk:var(--blue)}
.open{--mk:var(--ink-3)}
.verifying{--mk:var(--amber)}
.done{--mk:var(--green)}
.no{--mk:var(--red)}
.lv-ready{--mk:var(--green)}
.lv-playable{--mk:var(--lime)}
.lv-desync{--mk:var(--amber)}
.lv-crash{--mk:var(--red)}
.lv-untested{--mk:var(--ink-3)}
.mark{display:inline-grid;place-items:center;flex:none;width:1.5rem;height:1.5rem;border-radius:7px;font-size:.88rem;line-height:1;color:var(--mk,var(--ink-3));background:color-mix(in srgb,var(--mk,var(--ink-3)) 17%,transparent)}

/* header */
.hero{display:grid;gap:clamp(20px,3vw,40px);grid-template-columns:1fr;padding-top:clamp(26px,5vw,54px);padding-bottom:clamp(18px,3vw,32px)}
@media(min-width:900px){.hero{grid-template-columns:minmax(0,1.55fr) minmax(280px,.85fr);align-items:start}}
.hero h1{margin:0;font-size:clamp(2rem,5vw,3.2rem);line-height:1.02;letter-spacing:-.025em;font-weight:800}
.tagline{margin:.55rem 0 0;color:var(--ink-2);font-size:clamp(1rem,1.6vw,1.15rem);max-width:62ch}
.goal{margin:1rem 0 0;padding-left:.85rem;border-left:2px solid var(--amber);font-size:1rem;color:var(--ink);max-width:60ch}
.notice{margin:1.05rem 0 0;padding:.6rem .85rem;border:1px solid color-mix(in srgb,var(--amber) 34%,var(--line));border-radius:10px;background:color-mix(in srgb,var(--amber) 8%,transparent);color:var(--ink-2);font-size:.9rem;max-width:64ch}
.links{display:flex;flex-wrap:wrap;gap:8px;margin:1.15rem 0 0}
.links a{padding:.5rem .95rem;border-radius:999px;border:1px solid var(--line-2);background:var(--panel);color:var(--ink);font-size:.88rem;font-weight:500;text-decoration:none}
.links a:hover{border-color:var(--ink-3)}
.links a:first-child{background:var(--amber);border-color:var(--amber);color:var(--on-amber)}
.facts{padding:16px 16px 4px;border:1px solid var(--line);border-radius:var(--radius);background:radial-gradient(120% 100% at 100% 0,color-mix(in srgb,var(--amber) 12%,transparent),transparent 62%),linear-gradient(180deg,var(--panel),var(--panel-2))}
.fact+.fact{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.eyebrow{margin:0;font:600 .68rem/1.2 var(--mono);text-transform:uppercase;letter-spacing:.14em;color:var(--ink-3)}
.eyebrow a{color:inherit;text-decoration:none}
.eyebrow a:hover{color:var(--ink)}
.rel{margin:.45rem 0 0;font:700 clamp(1.5rem,3vw,2rem)/1 var(--mono);letter-spacing:-.02em}
.rel a{color:var(--ink);text-decoration:none}
.rel a:hover{color:var(--amber)}
.reldate{margin:.4rem 0 0;font:400 .78rem/1 var(--mono);color:var(--ink-3)}
.rosterbar{display:flex;height:7px;margin:.75rem 0 .55rem;border:1px solid var(--line);border-radius:999px;overflow:hidden;background:var(--panel-2)}
.seg{display:block;height:100%;background:var(--mk,var(--ink-3))}
.rosterkeys{display:flex;flex-wrap:wrap;gap:.35rem .8rem;list-style:none;margin:0;padding:0;font:400 .74rem/1 var(--mono);color:var(--ink-3)}
.rosterkeys li{display:flex;align-items:center;gap:.35rem}
.rosterkeys .mark{width:1.1rem;height:1.1rem;font-size:.7rem;background:none}
.stamp{margin:14px 0 0;padding:12px 0 12px;border-top:1px solid var(--line);font:400 .72rem/1.3 var(--mono);color:var(--ink-3)}

/* sticky index + find */
.toolbar{position:sticky;top:0;z-index:30;border-bottom:1px solid var(--line);background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(12px)}
@supports not (backdrop-filter:blur(1px)){.toolbar{background:var(--bg)}}
.tools{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;justify-content:space-between;padding-top:8px;padding-bottom:8px}
.toc{display:flex;gap:2px;flex:1 1 auto;min-width:0;overflow-x:auto;scrollbar-width:none}
.toc::-webkit-scrollbar{display:none}
.toc a{display:inline-flex;align-items:center;gap:.4rem;padding:.35rem .6rem;border-radius:8px;color:var(--ink-2);font-size:.84rem;text-decoration:none;white-space:nowrap}
.toc a:hover{background:var(--panel);color:var(--ink)}
.toc .n{font:600 .68rem/1 var(--mono);color:var(--ink-3)}
@media(max-width:700px){.toc{mask-image:linear-gradient(to right,#000 86%,transparent);padding-right:1.4rem}.toc .n{display:none}.find{flex:1 1 100%}}
.find{display:none;align-items:center;gap:.5rem;flex:0 1 280px}
.js .find{display:flex}
.find input{width:100%;min-width:0;padding:.42rem .8rem;border:1px solid var(--line-2);border-radius:999px;background:var(--panel);color:var(--ink);font-size:.85rem}
.find input::placeholder{color:var(--ink-3)}
.find output{font:400 .72rem/1 var(--mono);color:var(--ink-3);white-space:nowrap}
.legend-wrap{border-bottom:1px solid var(--line)}
.legend{display:flex;flex-wrap:wrap;gap:.35rem 1.2rem;list-style:none;margin:0;padding:.7rem 0;font-size:.8rem;color:var(--ink-3)}
.legend li{display:flex;align-items:center;gap:.4rem}
.legend .mark{width:1.25rem;height:1.25rem;font-size:.72rem}

/* blocks */
main{padding-bottom:1rem}
.block{padding:clamp(18px,3vw,30px) 0;border-bottom:1px solid var(--line)}
.block:last-of-type{border-bottom:0}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem .85rem;margin:0 0 1rem}
.head h2{display:flex;align-items:center;margin:0;font-size:clamp(1.2rem,2.2vw,1.5rem);letter-spacing:-.01em}
.kind-active .head h2::before{content:"";width:8px;height:8px;margin-right:.55rem;border-radius:50%;background:var(--amber);box-shadow:0 0 0 4px color-mix(in srgb,var(--amber) 18%,transparent)}
.head .note{margin:0;color:var(--ink-3);font-size:.85rem}
.head .count{margin-left:auto;padding:.3rem .5rem;border:1px solid var(--line);border-radius:999px;font:600 .72rem/1 var(--mono);color:var(--ink-3)}
.cards{display:grid;gap:12px;grid-template-columns:1fr;align-items:start}
@media(min-width:820px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kind-active .cards,.kind-not_planned .cards{grid-template-columns:1fr}
.card{padding:16px 16px 8px;border:1px solid var(--line);border-radius:var(--radius);background:linear-gradient(180deg,var(--panel),var(--panel-2))}
.kind-active .card{border-color:color-mix(in srgb,var(--amber) 26%,var(--line))}
@media(min-width:900px){
  .kind-active .lines,.kind-not_planned .lines{display:grid;grid-template-columns:1fr 1fr;column-gap:2.2rem}
}
.kind-active .line,.kind-not_planned .line{border-top:0;padding:.4rem 0 .5rem}
.card h3{display:flex;align-items:center;justify-content:space-between;gap:.6rem;margin:0 0 .4rem;font-size:1.02rem;line-height:1.3}
.blurb{margin:0 0 .75rem;color:var(--ink-2);font-size:.9rem;max-width:72ch}
.size{display:inline-flex;align-items:center;gap:.35rem;flex:none;padding:.3rem .5rem;border:1px solid var(--line-2);border-radius:999px;font:600 .66rem/1 var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);white-space:nowrap}
.meter{display:inline-flex;gap:2px}
.meter i{width:3px;height:9px;border-radius:1px;background:currentColor}
.size[data-size="small"] .meter i:nth-child(n+2){opacity:.18}
.size[data-size="medium"] .meter i:nth-child(n+3){opacity:.18}
.lines{list-style:none;margin:0;padding:0}
.lines .line:first-child{border-top:0}
.line{display:grid;grid-template-columns:auto 1fr;column-gap:.6rem;row-gap:.05rem;align-items:start;padding:.45rem 0;border-top:1px solid var(--line)}
.line .mark{grid-row:1/span 2;margin-top:.1rem}
.lt{grid-column:2;color:var(--ink-2);font-size:.94rem}
.line.now .lt{color:var(--ink)}
.line.done .lt{color:var(--ink-3);text-decoration:line-through;text-decoration-color:color-mix(in srgb,var(--green) 55%,transparent)}
.line.no .lt{color:var(--ink-3)}
.tail{grid-column:2;color:var(--ink-3);font-size:.78rem}
.banner{margin:0 0 1rem;padding:.55rem .8rem;border-left:3px solid var(--amber);border-radius:0 8px 8px 0;background:color-mix(in srgb,var(--amber) 8%,transparent);color:var(--ink-2);font-size:.88rem;max-width:82ch}

/* roster */
.roster-filter{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.lv-radio{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.chip{display:inline-flex;align-items:center;gap:.45rem;padding:.4rem .7rem;border:1px solid var(--line-2);border-radius:999px;background:var(--panel);color:var(--ink-2);font-size:.82rem;font-weight:500;line-height:1;cursor:pointer;user-select:none}
.chip:hover{border-color:var(--ink-3)}
.chip .mark{width:1.2rem;height:1.2rem;font-size:.72rem}
.chip .n{font:600 .7rem/1 var(--mono);opacity:.6}
.lv-radio:checked+.chip{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.lv-radio:checked+.chip .mark{color:inherit;background:color-mix(in srgb,var(--bg) 12%,transparent)}
.lv-radio:focus-visible+.chip{outline:2px solid var(--blue);outline-offset:2px}
.roster{display:grid;flex:1 0 100%;gap:10px;margin-top:.85rem}
.lv-group{padding:12px 14px;border:1px solid var(--line);border-left:3px solid var(--mk,var(--ink-3));border-radius:12px;background:var(--panel)}
.lv-head h3{display:flex;align-items:center;gap:.5rem;margin:0;font-size:.98rem}
.lv-head .n{margin-left:.1rem;font:600 .72rem/1 var(--mono);color:var(--ink-3)}
.lv-text{margin:.25rem 0 .7rem;color:var(--ink-3);font-size:.82rem}
.tiles{display:grid;gap:6px;grid-template-columns:repeat(auto-fill,minmax(min(100%,200px),1fr));list-style:none;margin:0;padding:0}
.tile{padding:.4rem .55rem;border:1px solid var(--line);border-radius:8px;background:var(--panel-2)}
.tname{display:block;font-size:.9rem;font-weight:600}
.tstat{display:block;font:400 .67rem/1.35 var(--mono);color:var(--ink-3)}
.tawait{color:color-mix(in srgb,var(--amber) 62%,var(--ink-3))}
.tile.quiet{opacity:.55}

/* shipped log */
.log{list-style:none;margin:0;padding:0}
.ship{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;padding:.45rem 0;border-bottom:1px solid var(--line)}
.ship-text{color:var(--ink-2);font-size:.92rem}
.ref{flex:none;padding:.3rem .5rem;border:1px solid var(--line);border-radius:999px;font:600 .7rem/1 var(--mono);color:var(--ink-3);text-decoration:none;white-space:nowrap}
a.ref:hover{color:var(--ink);border-color:var(--line-2)}
@media(min-width:820px){
  .kind-shipped .cards{display:block;columns:2;column-gap:12px}
  .kind-shipped .card{break-inside:avoid;margin:0 0 12px}
}
.log .ship:last-child{border-bottom:0}
.plain{list-style:none;margin:0;padding:0}
.plain li{position:relative;padding:.3rem 0 .3rem 1rem;color:var(--ink-2);font-size:.9rem}
.plain li::before{content:"";position:absolute;left:0;top:.85rem;width:5px;height:5px;border-radius:50%;background:var(--line-2)}

footer{padding:1.4rem 0 2.6rem;border-top:1px solid var(--line);color:var(--ink-3);font-size:.85rem}
footer p{margin:0}
@media (prefers-color-scheme:light){
  .links a:first-child{background:var(--ink);border-color:var(--ink);color:var(--bg)}
}
@media print{
  .toolbar,.find,.roster-filter,.skip{display:none!important}
  body{background:#fff;color:#000}
  .card,.lv-group,.facts,.tile,.size,.ref{background:none!important;border-color:#bbb!important}
  .mark{background:none!important}
  a{color:#000}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
""".strip()

SCRIPT = """
(function () {
  var find = document.getElementById('find');
  if (!find) return;
  var form = document.getElementById('find-form');
  var out = document.getElementById('find-out');
  var items = [].slice.call(document.querySelectorAll('[data-find]'));
  var groups = [].slice.call(document.querySelectorAll('[data-group]'));
  var many = out.getAttribute('data-many') || '{n}';
  var one = out.getAttribute('data-one') || many;
  var none = out.getAttribute('data-none') || '';
  function match(el, q) {
    if (!q) return true;
    var text = el.getAttribute('data-find');
    return !!text && text.indexOf(q) !== -1;
  }
  function apply() {
    var q = find.value.trim().toLowerCase();
    var hits = 0;
    items.forEach(function (el) {
      var hit = match(el, q);
      el.hidden = !hit;
      if (hit) hits += 1;
    });
    groups.slice().reverse().forEach(function (group) {
      if (!q || match(group, q)) {
        group.hidden = false;
        return;
      }
      var kids = group.querySelectorAll('[data-find]');
      var open = false;
      for (var i = 0; i < kids.length; i += 1) {
        if (!kids[i].hidden) { open = true; break; }
      }
      group.hidden = !open;
    });
    var template = hits === 1 ? one : many;
    out.textContent = q ? (hits ? template.replace('{n}', hits) : none) : '';
  }
  find.addEventListener('input', apply);
  find.addEventListener('search', apply);
  if (form) form.addEventListener('submit', function (e) { e.preventDefault(); });
  document.addEventListener('keydown', function (e) {
    var tag = document.activeElement.tagName;
    if (e.key === '/' && document.activeElement !== find && !/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) {
      e.preventDefault();
      find.focus();
    } else if (e.key === 'Escape' && document.activeElement === find && find.value) {
      find.value = '';
      apply();
    }
  });
})();
""".strip()


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


def render(cfg, board, strict_releases=False):
    if board.get("schema") != 2:
        fail("snapshot schema is not 2")
    releases = set(board.get("releases") or [])
    repo = (cfg.get("releases") or {}).get("repo")
    ui = cfg["ui"]
    page = board["page"]
    sections = board["sections"]
    chars = board.get("characters") or []
    groups = character_groups(board) if (board.get("levels") and chars) else []
    marks = {entry["class"]: entry.get("mark", "") for entry in cfg["legend"]}
    words = {entry["class"]: entry.get("text", "") for entry in cfg["legend"]}
    latest = board.get("latest_release")
    if latest and not release_mentioned(board, latest["tag"]):
        if strict_releases:
            fail(f"latest release {latest['tag']} not mentioned in shipped")
        print(f"warn: latest release {latest['tag']} is not listed in shipped", file=sys.stderr)

    def mark_html(state, glyph=None, label=None):
        glyph = marks.get(state, "") if glyph is None else glyph
        if label:
            return (f'<span class="mark {esc(state)}" role="img" aria-label="{esc(label)}">'
                    f'{esc(glyph)}</span>')
        return f'<span class="mark {esc(state)}" aria-hidden="true">{esc(glyph)}</span>'

    def head_html(heading, note, count):
        note_html = f'<p class="note">{esc(note)}</p>' if note else ""
        return (f'<div class="head"><h2>{esc(heading)}</h2>{note_html}'
                f'<span class="count">{count}</span></div>\n')

    def line_html(kind, line):
        state, tag, need = line_state(kind, line, releases)
        text = fill(line["text"], board)
        if text is None:
            return ""
        if state == "done":
            print(f"warn: line {line['id']} is all done; move it to shipped", file=sys.stderr)
        if tag:
            tail = ui["inRelease"].format(tag=tag)
        elif need:
            tail = ui["needsConfirm"]
        else:
            tail = ""
        tail_html = f'<small class="tail">{esc(tail)}</small>' if tail else ""
        return (f'<li class="line {state}" data-find="{esc(find_text(text, tail))}">'
                f'{mark_html(state, label=words.get(state))}'
                f'<span class="lt">{esc(text)}</span>{tail_html}</li>\n')

    body = []
    b = body.append
    nav = []

    for s in sections:
        if s["kind"] not in ("active", "upcoming", "not_planned"):
            continue
        count = sum(len(box["lines"]) for box in s["boxes"])
        b(f'<section class="block kind-{esc(s["kind"])}" id="{esc(s["id"])}" data-group>\n')
        b(head_html(s["heading"], s.get("note"), count))
        b('<div class="cards">\n')
        for box in s["boxes"]:
            b(f'<article class="card" data-group data-find="'
              f'{esc(find_text(box["title"], box.get("blurb")))}">\n')
            b(f'<h3><span>{esc(box["title"])}</span>{size_badge(box)}</h3>\n')
            if box.get("blurb"):
                b(f'<p class="blurb">{esc(box["blurb"])}</p>\n')
            b('<ul class="lines">\n')
            for line in box["lines"]:
                b(line_html(s["kind"], line))
            b('</ul>\n</article>\n')
        b('</div>\n</section>\n')
        nav.append((s["id"], s["heading"], count))

    if groups:
        total = sum(len(group["rows"]) for group in groups)
        b('<section class="block kind-characters" id="characters" data-group>\n')
        b(head_html(page["characters_heading"], None, total))
        if page.get("characters_banner"):
            b(f'<p class="banner">{esc(page["characters_banner"])}</p>\n')
        b('<div class="roster-filter">\n')
        b(f'<input class="lv-radio" type="radio" name="level" id="lv-all" checked>'
          f'<label class="chip" for="lv-all">{esc(ui["all"])}'
          f'<span class="n">{total}</span></label>\n')
        for i, group in enumerate(groups):
            level = group["level"]
            cls = f'lv-{esc(level["key"])}'
            b(f'<input class="lv-radio" type="radio" name="level" id="lv-{i}">'
              f'<label class="chip {cls}" for="lv-{i}">'
              f'{mark_html(cls, level.get("mark", ""))}'
              f'{esc(level["label"])}<span class="n">{len(group["rows"])}</span></label>\n')
        b('<div class="roster">\n')
        for i, group in enumerate(groups):
            level = group["level"]
            cls = f'lv-{esc(level["key"])}'
            b(f'<section class="lv-group {cls}" data-level="{i}" data-group>\n')
            b(f'<div class="lv-head"><h3>{mark_html(cls, level.get("mark", ""))}'
              f'{esc(level["label"])}<span class="n">{len(group["rows"])}</span></h3>')
            if level.get("text"):
                b(f'<p class="lv-text">{esc(level["text"])}</p>')
            b('</div>\n<ul class="tiles">\n')
            for name, _level, n_open, fixed, awaiting in group["rows"]:
                counts = []
                if n_open:
                    counts.append(f'{n_open} {esc(ui["open"])}')
                if fixed:
                    counts.append(f'{fixed} {esc(ui["fixed"])}')
                stat = (f'<span class="tstat">{" · ".join(counts)}</span>'
                        if counts else "")
                if awaiting:
                    stat += (f'<span class="tstat tawait">'
                             f'{awaiting} {esc(ui["awaiting"])}</span>')
                quiet = " quiet" if not n_open else ""
                b(f'<li class="tile{quiet}" data-find="{esc(find_text(name))}">'
                  f'<b class="tname">{esc(name)}</b>{stat}</li>\n')
            b('</ul>\n</section>\n')
        b('</div>\n</div>\n</section>\n')
        nav.append(("characters", page["characters_heading"], total))

    for s in sections:
        if s["kind"] != "shipped":
            continue
        areas = shipped_boxes(board)
        count = sum(len(lines) for _box, lines in areas)
        b(f'<section class="block kind-shipped" id="{esc(s["id"])}" data-group>\n')
        b(head_html(s["heading"], s.get("note"), count))
        b('<div class="cards">\n')
        for box, lines in areas:
            b(f'<article class="card" data-group data-find="'
              f'{esc(find_text(box["title"], box.get("blurb")))}">\n')
            b(f'<h3><span>{esc(box["title"])}</span></h3>\n')
            if box.get("blurb"):
                b(f'<p class="blurb">{esc(box["blurb"])}</p>\n')
            b('<ul class="log">\n')
            for line in lines:
                text = fill(line["text"], board)
                if text is None:
                    continue
                tag = line.get("release")
                if tag and repo:
                    ref = (f'<a class="ref" href="https://github.com/{esc(repo)}/releases/tag/'
                           f'{esc(tag)}">{esc(tag)}</a>')
                elif tag:
                    ref = f'<span class="ref">{esc(tag)}</span>'
                elif line.get("date"):
                    ref = (f'<time class="ref" datetime="{esc(line["date"])}">'
                           f'{esc(line["date"])}</time>')
                else:
                    ref = ""
                b(f'<li class="ship" data-find="{esc(find_text(text, tag))}">'
                  f'<span class="ship-text">{esc(text)}</span>{ref}</li>\n')
            b('</ul>\n</article>\n')
        b('</div>\n</section>\n')
        nav.append((s["id"], s["heading"], count))

    for s in sections:
        if s["kind"] != "background":
            continue
        count = sum(len(box["lines"]) for box in s["boxes"])
        b(f'<section class="block kind-background" id="{esc(s["id"])}" data-group>\n')
        b(head_html(s["heading"], s.get("note"), count))
        b('<div class="cards">\n')
        for box in s["boxes"]:
            b(f'<article class="card" data-group data-find="'
              f'{esc(find_text(box["title"], box.get("blurb")))}">\n')
            b(f'<h3><span>{esc(box["title"])}</span></h3>\n')
            if box.get("blurb"):
                b(f'<p class="blurb">{esc(box["blurb"])}</p>\n')
            b('<ul class="plain">\n')
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is None:
                    continue
                b(f'<li data-find="{esc(find_text(text))}">{esc(text)}</li>\n')
            b('</ul>\n</article>\n')
        b('</div>\n</section>\n')
        nav.append((s["id"], s["heading"], count))

    hero = []
    h = hero.append
    h('<header class="hero wrap">\n<div class="hero-text">\n')
    h(f'<h1>{esc(page["title"])}</h1>\n')
    if page.get("tagline"):
        h(f'<p class="tagline">{esc(page["tagline"])}</p>\n')
    if page.get("goal"):
        h(f'<p class="goal">{esc(page["goal"])}</p>\n')
    if page.get("notice"):
        h(f'<p class="notice">{esc(page["notice"])}</p>\n')
    if page.get("links"):
        links = "".join(f'<a href="{esc(link["href"])}">{esc(link["label"])}</a>'
                        for link in page["links"])
        h(f'<nav class="links" aria-label="{esc(ui["links"])}">{links}</nav>\n')
    h('</div>\n<aside class="facts">\n')
    if latest:
        h(f'<div class="fact">\n<p class="eyebrow">{esc(ui["latest"])}</p>\n')
        if repo:
            h(f'<p class="rel"><a href="https://github.com/{esc(repo)}/releases/tag/'
              f'{esc(latest["tag"])}">{esc(latest["tag"])}</a></p>\n')
        else:
            h(f'<p class="rel">{esc(latest["tag"])}</p>\n')
        h(f'<p class="reldate"><time datetime="{esc(latest["date"])}">'
          f'{esc(latest["date"])}</time></p>\n</div>\n')
    if groups:
        total = sum(len(group["rows"]) for group in groups)
        h(f'<div class="fact">\n<p class="eyebrow"><a href="#characters">'
          f'{esc(page["characters_heading"])}</a></p>\n')
        if total:
            segs = "".join(
                f'<span class="seg lv-{esc(group["level"]["key"])}" '
                f'style="width:{len(group["rows"]) * 100 / total:.2f}%"></span>'
                for group in groups if group["rows"])
            h(f'<div class="rosterbar" aria-hidden="true">{segs}</div>\n')
        h('<ul class="rosterkeys">\n')
        for group in groups:
            level = group["level"]
            cls = "lv-" + esc(level["key"])
            h(f'<li class="{cls}" title="{esc(level["label"])}">'
              f'{mark_html(cls, level.get("mark", ""))}'
              f'<span>{len(group["rows"])}</span></li>\n')
        h('</ul>\n</div>\n')
    h(f'<p class="stamp">{esc(ui["updated"])} {fmt_time(board["exported_at"])}</p>\n')
    h('</aside>\n</header>\n')

    tools = []
    t = tools.append
    t('<div class="toolbar">\n<div class="wrap tools">\n')
    t(f'<nav class="toc" aria-label="{esc(ui["jump"])}">\n')
    for sid, label, count in nav:
        t(f'<a href="#{esc(sid)}">{esc(label)}<span class="n">{count}</span></a>\n')
    t('</nav>\n')
    t('<form class="find" id="find-form" role="search">\n')
    t(f'<label class="sr-only" for="find">{esc(ui["find"])}</label>\n')
    t(f'<input id="find" type="search" placeholder="{esc(ui["findHint"])}" '
      'autocomplete="off" spellcheck="false">\n')
    t(f'<output id="find-out" for="find" aria-live="polite" '
      f'data-many="{esc(ui["matches"])}" data-one="{esc(ui["match"])}" '
      f'data-none="{esc(ui["noResults"])}"></output>\n')
    t('</form>\n</div>\n</div>\n')

    legend = ['<div class="wrap legend-wrap">\n<ul class="legend">\n']
    for entry in cfg["legend"]:
        legend.append(f'<li class="{esc(entry["class"])}">'
                      f'{mark_html(entry["class"])}{esc(entry["text"])}</li>\n')
    legend.append('</ul>\n</div>\n')

    level_css = "".join(
        f'#lv-{i}:checked~.roster>[data-level]:not([data-level="{i}"]){{display:none}}\n'
        for i in range(len(groups)))

    out = []
    w = out.append
    w('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    w(f'<title>{esc(page["title"])}</title>\n'
      '<meta name="robots" content="noindex, nofollow">\n'
      '<meta name="color-scheme" content="dark light">\n'
      f'<link rel="icon" href="{FAVICON}">\n'
      '<script>document.documentElement.className+=" js"</script>\n'
      f'<style>\n{CSS}\n{level_css}</style>\n</head>\n<body>\n')
    w(f'<a class="skip" href="#main">{esc(ui["skip"])}</a>\n')
    out.extend(hero)
    out.extend(tools)
    out.extend(legend)
    w('<main id="main" class="wrap">\n')
    out.extend(body)
    w('</main>\n')
    if page.get("footer"):
        w(f'<footer class="wrap"><p>{esc(page["footer"])}</p></footer>\n')
    w(f'<script>\n{SCRIPT}\n</script>\n</body>\n</html>\n')
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
               if s["kind"] in ("active", "upcoming", "not_planned") for box in s["boxes"])
    n_sh = sum(len(box["lines"]) for s in board["sections"] if s["kind"] == "shipped"
               for box in s["boxes"])
    line = f"ok: {n_up} upcoming, {n_sh} shipped"
    if board.get("levels"):
        line += (f", characters tiles={len(board.get('characters') or [])} "
                 + " ".join(f"{k}={levels.count(k)}" for k in SEVERITY + ("untested", "ready")))
    print(line)


if __name__ == "__main__":
    main()
