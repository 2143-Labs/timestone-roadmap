#!/usr/bin/env python3
"""Build the roadmap page from the board snapshot and the chart's config.

Usage: build.py [--config config/roadmap.json] [--board data/board.json]
                [--out site/index.html] [--strict-releases]

Every word on the page comes from the board snapshot (boxes, lines, page text,
character levels and issue types) except the legend wording and the size words,
which stay in the config. Fails (exit 1, old page untouched) on anything that
would put wrong or internal information on the public page. An unchanged
snapshot gives a byte-identical page.
"""
import argparse
import html
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"\{([^{}]*)\}")
DATED_RE = re.compile(r"^(\d{4}-\d\d-\d\d):\s+(.+)$")
TAG_RE = re.compile(r"(?<![\w.])(v\d+(?:\.\d+)*)(?!\.?\w)")
MAX_BYTES = 600_000


def fail(reason):
    sys.exit(f"build failed: {reason}")


def esc(s):
    return html.escape(str(s), quote=True)


def _fallback(kind):
    return "now" if kind == "active" else "open"


def line_state(kind, line, releases):
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
            return "verifying", f" (in {tag}, confirming)"
        print(f"warn: line {line['id']} names release {tag}, which is not a known release",
              file=sys.stderr)
    if any(not c["done"] and c["needs_repro"] for c in cards):
        return "verifying", " (fix released, confirming)"
    return _fallback(kind), ""


# Which failure outranks which is a page rule, not board content; the level
# words, marks and captions come from the board snapshot.
SEVERITY = ("crash", "desync", "playable")
SEV_RANK = {"crash": 0, "desync": 1, "playable": 2, "untested": 3, "ready": 4}


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


def character_breakdown(char, types):
    """Return (by_level, by_type) open-issue counts for one character.

    `by_level` maps each SEVERITY key to its open count; `by_type` is a list of
    `(type_name, level_key, count)` tuples sorted by severity then name.
    """
    by_level = {k: 0 for k in SEVERITY}
    counts = {}
    for issue in char["issues"]:
        if issue["done"]:
            continue
        t = issue.get("type")
        key = types.get(t)
        if key is None:
            continue
        if key in by_level:
            by_level[key] += 1
        if t:
            counts[t] = counts.get(t, 0) + 1
    by_type = sorted(
        ((t, types[t], n) for t, n in counts.items()),
        key=lambda item: (SEV_RANK.get(item[1], 9), item[0]),
    )
    return by_level, by_type


def version_key(tag):
    m = re.fullmatch(r"v(\d+(?:\.\d+)*)", tag or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-") or "item"


def linkify_releases(text, releases, repo):
    """Escape `text` and link standalone known release tags to GitHub releases."""
    if not repo or not releases:
        return esc(text)
    out = []
    pos = 0
    for m in TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in releases:
            continue
        out.append(esc(text[pos:m.start()]))
        href = f"https://github.com/{esc(repo)}/releases/tag/{esc(tag)}"
        out.append(f'<a class="rel-tag" href="{href}">{esc(tag)}</a>')
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


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


CSS = """
:root{
  --bg:#0b0e14;--bg-glow:rgba(56,189,248,.07);--surface:#131823;--surface-2:#1a2232;--surface-3:#222c40;
  --border:#242f43;--border-strong:#364663;--text:#eef3fa;--text-muted:#94a3b8;--text-dim:#6b7c95;
  --accent:#38bdf8;--accent-strong:#0ea5e9;--accent-soft:rgba(56,189,248,.13);--accent-border:rgba(56,189,248,.38);
  --ready:#22c55e;--ready-soft:rgba(34,197,94,.14);
  --playable:#84cc16;--playable-soft:rgba(132,204,22,.14);
  --desync:#f59e0b;--desync-soft:rgba(245,158,11,.15);
  --crash:#ef4444;--crash-soft:rgba(239,68,68,.15);
  --untested:#94a3b8;--untested-soft:rgba(148,163,184,.14);
  --now:#38bdf8;--verifying:#f59e0b;--open:#94a3b8;--done:#22c55e;--no:#ef4444;
  --banner-bg:rgba(245,158,11,.11);--banner-border:#f59e0b;--banner-text:#fde68a;
  --shadow:0 8px 24px rgba(0,0,0,.28);--radius:10px;--radius-sm:6px;
  --font:system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,Cantarell,sans-serif;
  --mono:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
}
html[data-theme="light"]{
  --bg:#f2f5fa;--bg-glow:rgba(2,132,199,.07);--surface:#ffffff;--surface-2:#eef2f8;--surface-3:#e2e8f0;
  --border:#d5dfec;--border-strong:#b0c0d6;--text:#0f172a;--text-muted:#475569;--text-dim:#64748b;
  --accent:#0284c7;--accent-strong:#0369a1;--accent-soft:rgba(2,132,199,.10);--accent-border:rgba(2,132,199,.34);
  --ready:#15803d;--ready-soft:rgba(21,128,61,.11);
  --playable:#4d7c0f;--playable-soft:rgba(77,124,15,.11);
  --desync:#b45309;--desync-soft:rgba(180,83,9,.11);
  --crash:#dc2626;--crash-soft:rgba(220,38,38,.11);
  --untested:#64748b;--untested-soft:rgba(100,116,139,.12);
  --now:#0284c7;--verifying:#b45309;--open:#64748b;--done:#15803d;--no:#dc2626;
  --banner-bg:#fffbeb;--banner-border:#d97706;--banner-text:#78350f;
  --shadow:0 6px 20px rgba(15,23,42,.07);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;min-height:100vh;background:radial-gradient(ellipse 90% 420px at 50% -80px,var(--bg-glow),transparent 70%),var(--bg);
  color:var(--text);font:15px/1.55 var(--font);-webkit-font-smoothing:antialiased;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.shell{max-width:1280px;margin:0 auto;padding:0 1.25rem 3rem}
.js-only{display:none!important}
html.js .js-only{display:revert!important}
html.js .js-only.flex-only{display:flex!important}
html.js .js-only.grid-only{display:grid!important}
html.js .js-only.inline-flex-only{display:inline-flex!important}
html.js .panel[hidden]{display:none!important}
[hidden]{display:none!important}

/* Masthead */
.masthead{padding:1.25rem 0 1rem}
.masthead-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.75rem;margin-bottom:.9rem}
.stamp-row{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .75rem;font-size:.82rem;color:var(--text-muted)}
.live-dot{width:8px;height:8px;border-radius:99px;background:var(--ready);box-shadow:0 0 0 3px var(--ready-soft);flex-shrink:0}
.stamp{font-variant-numeric:tabular-nums}
.release-pill{
  display:inline-flex;align-items:center;gap:.35rem;padding:.18rem .58rem;border-radius:99px;
  background:var(--accent-soft);border:1px solid var(--accent-border);color:var(--accent);
  font-size:.8rem;font-family:var(--mono);font-variant-numeric:tabular-nums;
}
.release-pill:hover{text-decoration:none;filter:brightness(1.12)}
.masthead-actions{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem}
.ext-links{display:flex;flex-wrap:wrap;gap:.45rem}
.btn{
  display:inline-flex;align-items:center;gap:.35rem;padding:.38rem .78rem;border-radius:var(--radius-sm);
  border:1px solid var(--border-strong);background:var(--surface-2);color:var(--text);font-size:.86rem;font-weight:600;
  cursor:pointer;transition:background .12s,border-color .12s,transform .1s;text-decoration:none;
}
.btn:hover{background:var(--surface-3);border-color:var(--accent-border);text-decoration:none}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#06121f}
html[data-theme="light"] .btn-primary{color:#fff}
.btn-primary:hover{background:var(--accent-strong);border-color:var(--accent-strong);color:#fff}
.btn-icon{padding:.38rem .55rem}
.btn svg{width:14px;height:14px;flex-shrink:0}

.masthead-title h1{margin:0;font-size:clamp(1.65rem,3.2vw,2.35rem);letter-spacing:-.03em;line-height:1.15}
.tagline{margin:.35rem 0 0;color:var(--text-muted);font-size:1.02rem}

.brief-grid{display:grid;gap:.75rem;margin-top:1rem;grid-template-columns:1fr}
@media(min-width:760px){.brief-grid.two-col{grid-template-columns:1.15fr 1fr}}
.brief-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:.8rem 1rem}
.brief-goal{border-left:4px solid var(--accent);font-size:.96rem}
.brief-notice{border-left:4px solid var(--desync);color:var(--text-muted);font-size:.91rem}

/* Roster Pulse Strip in Masthead */
.roster-pulse{
  margin-top:.9rem;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:.8rem 1rem;
}
.pulse-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.5rem;margin-bottom:.55rem}
.pulse-title{font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);display:flex;align-items:center;gap:.45rem}
.pulse-total{font-family:var(--mono);font-size:.8rem;color:var(--text);background:var(--surface-2);padding:.05rem .45rem;border-radius:99px;border:1px solid var(--border)}
.pulse-bar{display:flex;height:9px;border-radius:99px;overflow:hidden;background:var(--surface-2);gap:2px}
.pulse-seg{height:100%;border:0;padding:0;cursor:pointer;transition:filter .12s,transform .12s;min-width:6px}
.pulse-seg:hover{filter:brightness(1.2)}
.pulse-seg.ready{background:var(--ready)}.pulse-seg.playable{background:var(--playable)}
.pulse-seg.desync{background:var(--desync)}.pulse-seg.crash{background:var(--crash)}.pulse-seg.untested{background:var(--untested)}
.pulse-chips{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.6rem}
.pulse-chip{
  display:inline-flex;align-items:center;gap:.35rem;padding:.22rem .58rem;border-radius:99px;
  border:1px solid var(--border);background:var(--surface-2);font-size:.8rem;color:var(--text-muted);
  cursor:pointer;font-variant-numeric:tabular-nums;transition:border-color .12s,background .12s;
}
.pulse-chip:hover{border-color:var(--border-strong);background:var(--surface-3);color:var(--text)}
.pulse-chip strong{color:var(--text);font-family:var(--mono);font-weight:700}

/* Sticky Command Bar */
.command-bar{
  position:sticky;top:0;z-index:40;margin:0 -1.25rem 1.15rem;padding:.65rem 1.25rem;
  background:rgba(11,14,20,.88);backdrop-filter:blur(12px);border-top:1px solid var(--border);border-bottom:1px solid var(--border);
}
html[data-theme="light"] .command-bar{background:rgba(242,245,250,.90)}
.command-inner{max-width:1280px;margin:0 auto;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.65rem}
.tabs{display:flex;align-items:center;gap:.3rem;overflow-x:auto;scrollbar-width:none;max-width:100%}
.tabs::-webkit-scrollbar{display:none}
.tab{
  display:inline-flex;align-items:center;gap:.4rem;padding:.44rem .82rem;border-radius:var(--radius-sm);
  border:1px solid transparent;background:transparent;color:var(--text-muted);font-weight:600;font-size:.9rem;
  cursor:pointer;white-space:nowrap;transition:background .12s,color .12s,border-color .12s;
}
.tab:hover{background:var(--surface-2);color:var(--text)}
.tab.is-active{background:var(--surface-2);color:var(--text);border-color:var(--accent-border);box-shadow:inset 0 -2px 0 var(--accent)}
.tab-count{
  font-family:var(--mono);font-size:.75rem;padding:.05rem .42rem;border-radius:99px;
  background:var(--surface-3);color:var(--text-muted);font-variant-numeric:tabular-nums;
}
.tab.is-active .tab-count{background:var(--accent-soft);color:var(--accent)}

.search-wrap{position:relative;display:flex;align-items:center;flex:1 1 220px;max-width:340px;margin-left:auto}
.search-icon{position:absolute;left:.65rem;width:15px;height:15px;color:var(--text-dim);pointer-events:none}
.search-input{
  width:100%;padding:.44rem 2.3rem .44rem 2.1rem;border-radius:var(--radius-sm);
  border:1px solid var(--border-strong);background:var(--surface);color:var(--text);font-size:.88rem;
}
.search-input:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
.search-kbd{
  position:absolute;right:.55rem;padding:.05rem .35rem;border-radius:4px;border:1px solid var(--border-strong);
  background:var(--surface-2);color:var(--text-dim);font:700 .72rem var(--mono);pointer-events:none;
}
.search-clear{
  position:absolute;right:.4rem;border:0;background:transparent;color:var(--text-muted);
  cursor:pointer;font-size:1.1rem;line-height:1;padding:.15rem .35rem;border-radius:4px;
}
.search-clear:hover{color:var(--text);background:var(--surface-3)}
.search-status{
  display:flex;align-items:center;justify-content:space-between;gap:.75rem;
  margin-bottom:1rem;padding:.6rem .9rem;border-radius:var(--radius-sm);
  background:var(--accent-soft);border:1px solid var(--accent-border);font-size:.88rem;
}
html.js body.is-searching .rm-controls,html.js body.is-searching .team-bar,
html.js body.is-searching .roster-controls,html.js body.is-searching .shipped-toolbar,
body.is-searching .legend,body.is-searching .levels,body.is-searching .banner{display:none!important}

/* Legend & Roadmap Toolbar */
.rm-controls{
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.65rem;
  margin-bottom:.85rem;padding:.7rem .9rem;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
}
.pill-group{display:flex;flex-wrap:wrap;align-items:center;gap:.35rem}
.pill{
  display:inline-flex;align-items:center;gap:.35rem;padding:.28rem .65rem;border-radius:99px;
  border:1px solid var(--border);background:var(--surface-2);color:var(--text-muted);font-size:.82rem;font-weight:500;
  cursor:pointer;transition:background .12s,border-color .12s,color .12s;text-decoration:none;
}
.pill:hover{background:var(--surface-3);color:var(--text);border-color:var(--border-strong);text-decoration:none}
.pill.is-active{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
.pill-num{font-family:var(--mono);font-size:.74rem;opacity:.85}

.legend{
  display:flex;flex-wrap:wrap;gap:.4rem .55rem;list-style:none;margin:0 0 1.25rem;padding:0;font-size:.84rem;
}
.legend li{
  display:inline-flex;align-items:center;gap:.35rem;padding:.26rem .62rem;border-radius:99px;
  background:var(--surface);border:1px solid var(--border);color:var(--text-muted);
  transition:border-color .12s,background .12s,color .12s;
}
html.js .legend li[data-filter-state]{cursor:pointer}
html.js .legend li[data-filter-state]:hover{border-color:var(--border-strong);color:var(--text);background:var(--surface-2)}
html.js .legend li.is-active{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}
.legend li::before,.line-list li::before{font-weight:700;flex-shrink:0}
li.done::before{content:"✔";color:var(--done)}
li.verifying::before{content:"◷";color:var(--verifying)}
li.now::before{content:"▶";color:var(--now)}
li.open::before{content:"○";color:var(--open)}
li.no::before{content:"✕";color:var(--no)}

/* Sections & Cards */
.rm-section,.char-section,.shipped-section,.history-section{margin-bottom:2rem;scroll-margin-top:4.5rem}
.sec-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:.5rem 1rem;margin-bottom:.85rem;padding-bottom:.45rem;border-bottom:1px solid var(--border)}
.sec-head h2{margin:0;font-size:1.3rem;letter-spacing:-.02em;display:flex;flex-wrap:wrap;align-items:baseline;gap:.5rem}
.sec-head h2 small{font-weight:400;color:var(--text-muted);font-size:.86rem}
.sec-meta{font-family:var(--mono);font-size:.78rem;color:var(--text-dim);font-variant-numeric:tabular-nums}

.box-grid{display:grid;gap:1rem;grid-template-columns:1fr;align-items:start}
@media(min-width:760px){.box-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(min-width:1100px){.kind-active .box-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
.kind-not_planned .box-grid{grid-template-columns:1fr}

.card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:1rem 1.1rem;box-shadow:var(--shadow);display:flex;flex-direction:column;
}
.kind-active .card{border-top:3px solid var(--accent)}
.kind-not_planned .card{border-left:4px solid var(--no)}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:.65rem;margin-bottom:.35rem}
.card-head h3{margin:0;font-size:1.04rem;line-height:1.3;letter-spacing:-.01em}
.size{
  flex-shrink:0;font-family:var(--mono);font-size:.72rem;font-weight:600;padding:.14rem .5rem;
  border-radius:99px;border:1px solid var(--border-strong);color:var(--text-muted);background:var(--surface-2);white-space:nowrap;
}
.size-small{border-color:rgba(34,197,94,.4);color:var(--ready);background:var(--ready-soft)}
.size-medium{border-color:rgba(245,158,11,.42);color:var(--desync);background:var(--desync-soft)}
.size-large{border-color:var(--accent-border);color:var(--accent);background:var(--accent-soft)}
.card-blurb{margin:.15rem 0 .7rem;color:var(--text-muted);font-size:.89rem;line-height:1.48}

.line-list{list-style:none;margin:.3rem 0 0;padding:0;display:flex;flex-direction:column;gap:.48rem}
.kind-not_planned .line-list{display:grid;gap:.5rem 1.5rem;grid-template-columns:1fr}
@media(min-width:760px){.kind-not_planned .line-list{grid-template-columns:repeat(2,minmax(0,1fr))}}
.line-list li{display:flex;align-items:baseline;gap:.55rem;font-size:.92rem;line-height:1.45}
.line-list li::before{width:1rem;text-align:center;font-size:.85rem}
.line-list li.done .line-body{color:var(--text-muted)}
.kind-active .line-list li.done .line-body,.kind-upcoming .line-list li.done .line-body{text-decoration:line-through;opacity:.7}
.line-body{flex:1;min-width:0}
.confirm-tag{
  display:inline-block;margin-left:.35rem;padding:.05rem .42rem;border-radius:4px;
  background:var(--desync-soft);color:var(--desync);font-size:.78rem;font-weight:600;
}
.rel-tag{
  display:inline-block;margin:0 .06rem;padding:.02rem .4rem;border-radius:4px;
  background:var(--accent-soft);border:1px solid var(--accent-border);color:var(--accent);
  font-family:var(--mono);font-size:.77rem;font-weight:600;white-space:nowrap;
}
.rel-tag.tail{margin-left:.32rem}
.rel-tag:hover{text-decoration:none;filter:brightness(1.15)}
.date-tag{
  display:inline-block;margin-left:.3rem;padding:.04rem .4rem;border-radius:4px;
  background:var(--surface-2);border:1px solid var(--border);color:var(--text-muted);
  font-family:var(--mono);font-size:.76rem;white-space:nowrap;
}
.area-tag{
  display:inline-block;margin-right:.45rem;padding:.05rem .45rem;border-radius:4px;
  background:var(--surface-2);border:1px solid var(--border);color:var(--text-muted);
  font-size:.76rem;font-weight:600;vertical-align:baseline;
}

/* Characters Section */
.banner{
  margin:0 0 1rem;padding:.75rem 1rem;border-radius:var(--radius-sm);
  background:var(--banner-bg);border:1px solid var(--banner-border);border-left-width:4px;
  color:var(--banner-text);font-size:.91rem;
}

/* 3-Character Team Checker */
.team-bar{
  margin-bottom:1.1rem;padding:.95rem 1rem;border-radius:var(--radius);
  background:var(--surface);border:1px solid var(--border-strong);box-shadow:var(--shadow);
}
.team-head{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.6rem;margin-bottom:.75rem}
.team-title{display:flex;flex-wrap:wrap;align-items:center;gap:.55rem;font-weight:700;font-size:.95rem}
.team-verdict{
  display:inline-flex;align-items:center;gap:.4rem;padding:.2rem .65rem;border-radius:99px;
  font-size:.82rem;font-weight:600;border:1px solid var(--border);background:var(--surface-2);color:var(--text-muted);
}
.team-verdict.ready{border-color:var(--ready);background:var(--ready-soft);color:var(--ready)}
.team-verdict.playable{border-color:var(--playable);background:var(--playable-soft);color:var(--playable)}
.team-verdict.desync{border-color:var(--desync);background:var(--desync-soft);color:var(--desync)}
.team-verdict.crash{border-color:var(--crash);background:var(--crash-soft);color:var(--crash)}
.team-verdict.untested{border-color:var(--untested);background:var(--untested-soft);color:var(--untested)}
.team-summary{font-size:.82rem;color:var(--text-muted);font-variant-numeric:tabular-nums}
.team-actions{display:flex;gap:.4rem}
.team-slots{display:grid;gap:.65rem;grid-template-columns:1fr}
@media(min-width:720px){.team-slots{grid-template-columns:repeat(3,minmax(0,1fr))}}
.team-slot{
  display:flex;flex-direction:column;gap:.4rem;padding:.65rem .75rem;border-radius:var(--radius-sm);
  border:1px dashed var(--border-strong);background:var(--surface-2);min-height:74px;
}
.team-slot.is-filled{border-style:solid;border-left-width:4px;background:var(--surface)}
.team-slot.ready{border-left-color:var(--ready)}.team-slot.playable{border-left-color:var(--playable)}
.team-slot.desync{border-left-color:var(--desync)}.team-slot.crash{border-left-color:var(--crash)}.team-slot.untested{border-left-color:var(--untested)}
.slot-top{display:flex;align-items:center;justify-content:space-between;gap:.4rem}
.slot-num{font-family:var(--mono);font-size:.72rem;font-weight:700;color:var(--text-dim);padding:.05rem .38rem;border-radius:4px;background:var(--surface-3)}
.slot-select{
  flex:1;min-width:0;padding:.28rem .45rem;border-radius:var(--radius-sm);
  border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:.85rem;
}
.slot-clear{border:0;background:transparent;color:var(--text-muted);cursor:pointer;padding:.1rem .35rem;border-radius:4px;font-size:1rem;line-height:1}
.slot-clear:hover{background:var(--surface-3);color:var(--text)}
.slot-info{font-size:.78rem;color:var(--text-muted);display:flex;flex-wrap:wrap;align-items:center;gap:.35rem;font-variant-numeric:tabular-nums}

/* Level Filter Cards */
.levels{display:grid;gap:.55rem;grid-template-columns:repeat(auto-fit,minmax(195px,1fr));margin-bottom:1rem}
.level-card{
  text-align:left;padding:.65rem .8rem;border-radius:var(--radius-sm);border:1px solid var(--border);
  border-top:3px solid var(--border);background:var(--surface);display:flex;flex-direction:column;gap:.2rem;
  transition:border-color .12s,background .12s,transform .1s;
}
html.js .level-card{cursor:pointer}
html.js .level-card:hover{background:var(--surface-2);border-color:var(--border-strong)}
.level-card.is-active{box-shadow:0 0 0 2px var(--accent);background:var(--surface-2)}
.level-card.ready{border-top-color:var(--ready)}.level-card.playable{border-top-color:var(--playable)}
.level-card.desync{border-top-color:var(--desync)}.level-card.crash{border-top-color:var(--crash)}.level-card.untested{border-top-color:var(--untested)}
.level-card-top{display:flex;align-items:center;justify-content:space-between;gap:.4rem}
.level-card-name{font-size:.86rem;font-weight:700;display:flex;align-items:center;gap:.35rem}
.level-card-count{font-family:var(--mono);font-size:1.1rem;font-weight:800;font-variant-numeric:tabular-nums}
.level-card-text{margin:0;font-size:.78rem;color:var(--text-muted);line-height:1.35}

/* Roster Toolbar & Character Tiles */
.roster-controls{
  display:flex;flex-wrap:wrap;align-items:center;gap:.55rem;margin-bottom:.95rem;
  padding:.65rem .85rem;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
}
.roster-filter-input{
  flex:1 1 180px;max-width:260px;padding:.36rem .65rem;border-radius:var(--radius-sm);
  border:1px solid var(--border-strong);background:var(--surface-2);font-size:.86rem;
}
.roster-select{
  padding:.36rem .6rem;border-radius:var(--radius-sm);border:1px solid var(--border-strong);
  background:var(--surface-2);font-size:.85rem;cursor:pointer;
}

.tiles{display:grid;gap:.65rem;grid-template-columns:repeat(auto-fill,minmax(225px,1fr))}
.tile{
  border:1px solid var(--border);border-left:4px solid var(--border);border-radius:var(--radius-sm);
  background:var(--surface);padding:.65rem .78rem;display:flex;flex-direction:column;gap:.32rem;
  transition:border-color .12s,background .12s;
}
.tile:hover{background:var(--surface-2)}
.tile.in-team{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent-border)}
.tile.ready{border-left-color:var(--ready)}.tile.playable{border-left-color:var(--playable)}
.tile.desync{border-left-color:var(--desync)}.tile.crash{border-left-color:var(--crash)}.tile.untested{border-left-color:var(--untested)}
.tile-top{display:flex;align-items:center;justify-content:space-between;gap:.4rem}
.tile-name{font-size:.95rem;font-weight:700;display:flex;align-items:center;gap:.35rem;min-width:0}
.team-add{
  flex-shrink:0;width:24px;height:24px;border-radius:6px;border:1px solid var(--border-strong);
  background:var(--surface-2);color:var(--text-muted);cursor:pointer;font-weight:700;font-size:.9rem;
  display:inline-flex;align-items:center;justify-content:center;line-height:1;
}
.team-add:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.tile.in-team .team-add{background:var(--accent);border-color:var(--accent);color:#06121f}
.tile-mid{display:flex;align-items:center;justify-content:space-between;gap:.5rem}
.tile-level{font-size:.78rem;font-weight:600}
.sev-bar{display:flex;height:4px;width:54px;border-radius:99px;overflow:hidden;background:var(--surface-3);gap:1px;flex-shrink:0}
.sev-seg-crash{background:var(--crash)}.sev-seg-desync{background:var(--desync)}.sev-seg-playable{background:var(--playable)}
.tile-stats{color:var(--text-muted);font-size:.76rem;font-variant-numeric:tabular-nums;line-height:1.45}
.tile-await{color:var(--desync);font-weight:600;white-space:nowrap}
.tile-types{display:flex;flex-wrap:wrap;gap:.25rem;margin-top:.1rem}
.type-chip{
  font-family:var(--mono);font-size:.69rem;padding:.04rem .36rem;border-radius:4px;border:1px solid var(--border);
  background:var(--surface-2);color:var(--text-muted);
}
.type-chip.crash{border-color:rgba(239,68,68,.38);background:var(--crash-soft);color:var(--crash)}
.type-chip.desync{border-color:rgba(245,158,11,.38);background:var(--desync-soft);color:var(--desync)}
.type-chip.playable{border-color:rgba(132,204,22,.38);background:var(--playable-soft);color:var(--playable)}

.lv-ready,b.ready,.tile-level.ready{color:var(--ready)}
.lv-playable,b.playable,.tile-level.playable{color:var(--playable)}
.lv-desync,b.desync,.tile-level.desync{color:var(--desync)}
.lv-crash,b.crash,.tile-level.crash{color:var(--crash)}
.lv-untested,b.untested,.tile-level.untested{color:var(--untested)}

/* Shipped Section */
.shipped-toolbar{
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.65rem;
  margin-bottom:1rem;padding:.7rem .9rem;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
}
.seg-switch{display:inline-flex;padding:2px;border-radius:var(--radius-sm);background:var(--surface-2);border:1px solid var(--border)}
.seg-btn{
  padding:.3rem .72rem;border-radius:4px;border:0;background:transparent;color:var(--text-muted);
  font-size:.83rem;font-weight:600;cursor:pointer;
}
.seg-btn.is-active{background:var(--accent);color:#06121f}
html[data-theme="light"] .seg-btn.is-active{color:#fff}
.release-chips{display:flex;flex-wrap:wrap;gap:.35rem;flex:1}
.shipped-by-release{display:none}
.shipped-section[data-mode="release"] .shipped-by-area{display:none}
.shipped-section[data-mode="release"] .shipped-by-release{display:grid}
.rel-card-head{display:flex;align-items:center;justify-content:space-between;gap:.5rem;margin-bottom:.5rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.rel-card-head h3{margin:0;font-family:var(--mono);font-size:1.05rem}

/* Background & Timeline */
.history-grid{display:grid;gap:1rem;grid-template-columns:1fr}
@media(min-width:900px){.history-grid{grid-template-columns:1.15fr 1fr;align-items:start}}
.timeline{list-style:none;margin:.4rem 0 0;padding:0 0 0 1.15rem;border-left:2px solid var(--border-strong);display:flex;flex-direction:column;gap:.7rem}
.timeline li{position:relative;display:flex;flex-wrap:wrap;align-items:baseline;gap:.2rem .6rem;font-size:.91rem}
.timeline li::before{
  content:"";position:absolute;left:-1.5rem;top:.45rem;width:9px;height:9px;border-radius:99px;
  background:var(--accent);box-shadow:0 0 0 3px var(--surface);
}
.tl-date{font-family:var(--mono);font-size:.78rem;font-weight:600;color:var(--text-muted);flex-shrink:0;min-width:6.2rem}
.tl-text{flex:1;min-width:180px}
.plain{margin:.35rem 0 0;padding-left:1.25rem;display:flex;flex-direction:column;gap:.5rem;font-size:.91rem}
.plain li{line-height:1.48}

.empty-state{
  padding:2rem 1rem;text-align:center;color:var(--text-muted);background:var(--surface);
  border:1px dashed var(--border-strong);border-radius:var(--radius);font-size:.92rem;
}
@media(max-width:540px){
  .tab{padding:.38rem .56rem;font-size:.83rem}
  .tab-count{padding:.03rem .32rem;font-size:.7rem}
}

/* Footer */
.site-footer{
  margin-top:2.5rem;padding-top:1.1rem;border-top:1px solid var(--border);
  display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.75rem;
  color:var(--text-muted);font-size:.86rem;
}
.site-footer p{margin:0}
.back-top{
  display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface);color:var(--text-muted);
}
.back-top:hover{color:var(--text);border-color:var(--accent);text-decoration:none}

@media print{
  body{background:#fff;color:#000}
  .command-bar,.js-only,.back-top,.team-bar,.roster-controls,.rm-controls,.shipped-toolbar{display:none!important}
  .panel[hidden]{display:block!important}
  .card,.tile,.brief-card,.roster-pulse{box-shadow:none;break-inside:avoid;border-color:#ccc;background:#fff}
}
""".strip()


JS = r"""
(() => {
  const root = document.documentElement;
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  // Theme toggle
  const themeBtn = $("#theme-toggle");
  const syncThemeLabel = () => {
    if (!themeBtn) return;
    const isLight = root.getAttribute("data-theme") === "light";
    themeBtn.setAttribute("aria-pressed", String(isLight));
    themeBtn.title = isLight ? "Dark" : "Light";
    const span = $(".theme-label", themeBtn);
    if (span) span.textContent = isLight ? "Dark" : "Light";
  };
  syncThemeLabel();
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("ts-roadmap-theme", next); } catch (_) {}
      syncThemeLabel();
    });
  }

  // Tabs & Panels
  const tabs = $$(".tab[data-tab]");
  const panels = $$(".panel[data-panel]");
  const validTabs = new Set(tabs.map(t => t.dataset.tab));
  let activeTab = tabs[0] ? tabs[0].dataset.tab : "all";

  const showTab = (tabName, pushHash = true) => {
    if (!validTabs.has(tabName)) tabName = tabs[0] ? tabs[0].dataset.tab : "all";
    activeTab = tabName;
    tabs.forEach(t => {
      const on = t.dataset.tab === tabName;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", String(on));
    });
    if (!$("#global-search") || !$("#global-search").value.trim()) {
      panels.forEach(p => {
        p.hidden = tabName !== "all" && p.dataset.panel !== tabName;
      });
    }
    if (pushHash) updateHash();
  };

  tabs.forEach((tab, idx) => {
    tab.addEventListener("click", () => {
      if (searchInput && searchInput.value.trim()) clearSearch(false);
      showTab(tab.dataset.tab, true);
    });
    tab.addEventListener("keydown", e => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const next = tabs[(idx + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
        next.focus();
        next.click();
      }
    });
  });

  // Roadmap filters (state & size)
  let rmState = "";
  let rmSize = "";
  const rmPanel = $('.panel[data-panel="roadmap"]');
  const legendBtns = $$(".legend li[data-filter-state]");
  const sizeBtns = $$(".pill[data-size-filter]");
  const rmEmpty = $("#rm-empty");

  const applyRoadmapFilters = () => {
    if (!rmPanel) return;
    let visibleLines = 0;
    $$(".rm-section", rmPanel).forEach(sec => {
      let secLines = 0;
      $$(".card", sec).forEach(card => {
        const sizeOk = !rmSize || card.dataset.size === rmSize;
        let cardLines = 0;
        $$(".line-list li", card).forEach(li => {
          const stOk = !rmState || li.dataset.state === rmState;
          const show = sizeOk && stOk;
          li.hidden = !show;
          if (show) cardLines++;
        });
        card.hidden = cardLines === 0;
        secLines += cardLines;
      });
      sec.hidden = secLines === 0;
      visibleLines += secLines;
    });
    if (rmEmpty) rmEmpty.hidden = visibleLines > 0;
  };

  legendBtns.forEach(item => {
    const toggle = () => {
      const st = item.dataset.filterState;
      if (st === "done" && validTabs.has("shipped") && activeTab === "roadmap" && !rmState) {
        const hasRoadmapDone = $$('.panel[data-panel="roadmap"] li[data-state="done"]').length > 0;
        if (!hasRoadmapDone) {
          showTab("shipped", true);
          return;
        }
      }
      rmState = rmState === st ? "" : st;
      legendBtns.forEach(b => {
        const on = b.dataset.filterState === rmState;
        b.classList.toggle("is-active", on);
        b.setAttribute("aria-pressed", String(on));
      });
      applyRoadmapFilters();
    };
    item.addEventListener("click", toggle);
    item.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });

  sizeBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      rmSize = btn.dataset.sizeFilter;
      sizeBtns.forEach(b => b.classList.toggle("is-active", b.dataset.sizeFilter === rmSize));
      applyRoadmapFilters();
    });
  });

  $$(".pill[data-jump-sec]").forEach(link => {
    link.addEventListener("click", e => {
      const target = document.getElementById(link.dataset.jumpSec);
      if (!target) return;
      e.preventDefault();
      if (activeTab !== "roadmap" && activeTab !== "all") showTab("roadmap", false);
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // Characters: filtering, sorting, and 3-character team checker
  const charSec = $("#characters");
  const tilesWrap = charSec ? $(".tiles", charSec) : null;
  const tiles = tilesWrap ? $$(".tile", tilesWrap) : [];
  const levelCards = charSec ? $$(".level-card[data-level-filter]", charSec) : [];
  const charFilterInput = $("#char-filter");
  const charSortSelect = $("#char-sort");
  const charAwaitBtn = $("#char-await-btn");
  const charResetBtn = $("#char-reset-btn");
  const charShown = $("#char-shown-count");
  const charEmpty = $("#char-empty");

  let charLevel = "";
  let charAwaitOnly = false;
  let charQuery = "";
  let charSort = "name";

  const applyCharFilters = () => {
    if (!tilesWrap) return;
    const q = charQuery.trim().toLowerCase();
    const visible = [];
    tiles.forEach(tile => {
      const lvOk = !charLevel || tile.dataset.level === charLevel;
      const awOk = !charAwaitOnly || Number(tile.dataset.awaiting) > 0;
      const qOk = !q || tile.dataset.search.includes(q);
      const show = lvOk && awOk && qOk;
      tile.hidden = !show;
      if (show) visible.push(tile);
    });
    const cmp = {
      name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
      severity: (a, b) => Number(a.dataset.sev) - Number(b.dataset.sev) || Number(b.dataset.open) - Number(a.dataset.open) || a.dataset.name.localeCompare(b.dataset.name),
      open_desc: (a, b) => Number(b.dataset.open) - Number(a.dataset.open) || a.dataset.name.localeCompare(b.dataset.name),
      open_asc: (a, b) => Number(a.dataset.open) - Number(b.dataset.open) || a.dataset.name.localeCompare(b.dataset.name),
      fixed_desc: (a, b) => Number(b.dataset.fixed) - Number(a.dataset.fixed) || a.dataset.name.localeCompare(b.dataset.name),
      await_desc: (a, b) => Number(b.dataset.awaiting) - Number(a.dataset.awaiting) || a.dataset.name.localeCompare(b.dataset.name),
    }[charSort] || ((a, b) => a.dataset.name.localeCompare(b.dataset.name));
    tiles.slice().sort(cmp).forEach(t => tilesWrap.appendChild(t));

    levelCards.forEach(c => {
      const on = c.dataset.levelFilter === charLevel;
      c.classList.toggle("is-active", on);
      c.setAttribute("aria-pressed", String(on));
    });
    if (charAwaitBtn) {
      charAwaitBtn.classList.toggle("is-active", charAwaitOnly);
      charAwaitBtn.setAttribute("aria-pressed", String(charAwaitOnly));
    }
    if (charResetBtn) charResetBtn.hidden = !(charLevel || charAwaitOnly || q || charSort !== "name");
    if (charShown) charShown.textContent = `${visible.length} / ${tiles.length}`;
    if (charEmpty) charEmpty.hidden = visible.length > 0;
  };

  levelCards.forEach(card => {
    card.addEventListener("click", () => {
      const lv = card.dataset.levelFilter;
      charLevel = charLevel === lv ? "" : lv;
      applyCharFilters();
    });
  });

  if (charFilterInput) {
    charFilterInput.addEventListener("input", () => {
      charQuery = charFilterInput.value;
      applyCharFilters();
    });
  }
  if (charSortSelect) {
    charSortSelect.addEventListener("change", () => {
      charSort = charSortSelect.value;
      applyCharFilters();
    });
  }
  if (charAwaitBtn) {
    charAwaitBtn.addEventListener("click", () => {
      charAwaitOnly = !charAwaitOnly;
      applyCharFilters();
    });
  }
  if (charResetBtn) {
    charResetBtn.addEventListener("click", () => {
      charLevel = "";
      charAwaitOnly = false;
      charQuery = "";
      charSort = "name";
      if (charFilterInput) charFilterInput.value = "";
      if (charSortSelect) charSortSelect.value = "name";
      applyCharFilters();
    });
  }

  // Roster pulse bar clicks -> jump to Characters tab filtered
  $$("[data-pulse-level]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (searchInput && searchInput.value.trim()) clearSearch(false);
      const lv = btn.dataset.pulseLevel;
      charLevel = lv === "all" ? "" : lv;
      showTab("characters", true);
      applyCharFilters();
      if (charSec) charSec.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  const pulseTeamBtn = $("#pulse-team-btn");
  if (pulseTeamBtn) {
    pulseTeamBtn.addEventListener("click", () => {
      if (searchInput && searchInput.value.trim()) clearSearch(false);
      showTab("characters", true);
      const tb = $("#team-bar");
      if (tb) tb.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  // Team checker (3 slots)
  const teamBar = $("#team-bar");
  const levelsMeta = (() => {
    try { return JSON.parse(charSec && charSec.dataset.levels || "[]"); }
    catch (_) { return []; }
  })();
  const levelByKey = Object.fromEntries(levelsMeta.map(l => [l.key, l]));
  const tileBySlug = Object.fromEntries(tiles.map(t => [t.dataset.slug, t]));
  const team = ["", "", ""];
  const slotEls = teamBar ? $$(".team-slot", teamBar) : [];
  const verdictEl = $("#team-verdict");
  const summaryEl = $("#team-summary");
  const copyTeamBtn = $("#team-copy");
  const clearTeamBtn = $("#team-clear");

  const renderTeam = (syncUrl = true) => {
    if (!teamBar) return;
    const chosen = team.filter(Boolean).map(s => tileBySlug[s]).filter(Boolean);
    const chosenSet = new Set(team.filter(Boolean));

    slotEls.forEach((slotEl, idx) => {
      const slug = team[idx];
      const tile = slug ? tileBySlug[slug] : null;
      const sel = $(".slot-select", slotEl);
      const clr = $(".slot-clear", slotEl);
      const info = $(".slot-info", slotEl);
      if (sel && sel.value !== slug) sel.value = slug;
      slotEl.className = "team-slot" + (tile ? ` is-filled ${tile.dataset.level}` : "");
      if (clr) clr.hidden = !tile;
      if (info) {
        if (tile) {
          const lv = levelByKey[tile.dataset.level] || { mark: "", label: tile.dataset.level };
          const aw = Number(tile.dataset.awaiting);
          const awText = aw ? ` · ${aw} awaiting confirmation` : "";
          info.innerHTML = `<span class="tile-level ${tile.dataset.level}">${lv.mark} ${lv.label}</span><span>· ${tile.dataset.open} open · ${tile.dataset.fixed} fixed${awText}</span>`;
        } else {
          info.textContent = "";
        }
      }
    });

    tiles.forEach(tile => {
      const on = chosenSet.has(tile.dataset.slug);
      tile.classList.toggle("in-team", on);
      const btn = $(".team-add", tile);
      if (btn) {
        btn.textContent = on ? "✓" : "+";
        btn.setAttribute("aria-pressed", String(on));
      }
    });

    if (verdictEl) {
      if (!chosen.length) {
        verdictEl.className = "team-verdict";
        verdictEl.textContent = "0 / 3";
        if (summaryEl) summaryEl.textContent = "";
      } else {
        const worst = chosen.reduce((a, b) => (Number(a.dataset.sev) <= Number(b.dataset.sev) ? a : b));
        const lv = levelByKey[worst.dataset.level] || { mark: "", label: worst.dataset.level, text: "" };
        verdictEl.className = `team-verdict ${worst.dataset.level}`;
        verdictEl.textContent = `${lv.mark} ${lv.label}`;
        const totOpen = chosen.reduce((s, t) => s + Number(t.dataset.open), 0);
        const totFixed = chosen.reduce((s, t) => s + Number(t.dataset.fixed), 0);
        const totAw = chosen.reduce((s, t) => s + Number(t.dataset.awaiting), 0);
        const awPart = totAw ? ` · ${totAw} awaiting confirmation` : "";
        if (summaryEl) {
          summaryEl.textContent = `${totOpen} open · ${totFixed} fixed${awPart}${lv.text ? " — " + lv.text : ""}`;
        }
      }
    }
    if (copyTeamBtn) copyTeamBtn.hidden = chosen.length === 0;
    if (clearTeamBtn) clearTeamBtn.hidden = chosen.length === 0;
    if (syncUrl) updateHash();
  };

  slotEls.forEach((slotEl, idx) => {
    const sel = $(".slot-select", slotEl);
    const clr = $(".slot-clear", slotEl);
    if (sel) {
      sel.addEventListener("change", () => {
        const v = sel.value;
        if (v) {
          const dup = team.indexOf(v);
          if (dup !== -1 && dup !== idx) team[dup] = "";
        }
        team[idx] = v;
        renderTeam(true);
      });
    }
    if (clr) {
      clr.addEventListener("click", () => {
        team[idx] = "";
        renderTeam(true);
      });
    }
  });

  tiles.forEach(tile => {
    const btn = $(".team-add", tile);
    if (!btn) return;
    btn.addEventListener("click", () => {
      const slug = tile.dataset.slug;
      const existing = team.indexOf(slug);
      if (existing !== -1) {
        team[existing] = "";
      } else {
        const empty = team.indexOf("");
        team[empty !== -1 ? empty : 2] = slug;
      }
      renderTeam(true);
    });
  });

  if (clearTeamBtn) {
    clearTeamBtn.addEventListener("click", () => {
      team[0] = team[1] = team[2] = "";
      renderTeam(true);
    });
  }
  if (copyTeamBtn) {
    copyTeamBtn.addEventListener("click", () => {
      updateHash();
      const url = location.href;
      const done = () => {
        const orig = copyTeamBtn.textContent;
        copyTeamBtn.textContent = "Copied";
        setTimeout(() => { copyTeamBtn.textContent = orig; }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(done, done);
      } else {
        done();
      }
    });
  }

  // Shipped: By area / By release + Release chip filter
  const shippedSec = $("#shipped");
  let shippedMode = "area";
  let shippedRel = "";
  const modeBtns = shippedSec ? $$(".seg-btn[data-ship-mode]", shippedSec) : [];
  const relBtns = shippedSec ? $$(".pill[data-rel-filter]", shippedSec) : [];

  const applyShipped = () => {
    if (!shippedSec) return;
    shippedSec.dataset.mode = shippedMode;
    modeBtns.forEach(b => {
      const on = b.dataset.shipMode === shippedMode;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", String(on));
    });
    relBtns.forEach(b => b.classList.toggle("is-active", b.dataset.relFilter === shippedRel));

    $$(".shipped-by-area .card", shippedSec).forEach(card => {
      let shown = 0;
      $$(".line-list li", card).forEach(li => {
        const ok = !shippedRel || li.dataset.release === shippedRel;
        li.hidden = !ok;
        if (ok) shown++;
      });
      card.hidden = shown === 0;
    });
    $$(".shipped-by-release .card", shippedSec).forEach(card => {
      card.hidden = Boolean(shippedRel) && card.dataset.releaseGroup !== shippedRel;
    });
  };

  modeBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      shippedMode = btn.dataset.shipMode;
      applyShipped();
    });
  });
  relBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      const r = btn.dataset.relFilter;
      shippedRel = shippedRel === r ? "" : r;
      applyShipped();
    });
  });

  // Global Search across all panels
  const searchInput = $("#global-search");
  const searchClear = $("#search-clear");
  const searchKbd = $(".search-kbd");
  const searchStatus = $("#search-status");
  const searchStatusText = $("#search-status-text");
  const searchStatusClear = $("#search-status-clear");
  const globalEmpty = $("#global-empty");

  const runGlobalSearch = () => {
    if (!searchInput) return;
    const raw = searchInput.value;
    const q = raw.trim().toLowerCase();
    document.body.classList.toggle("is-searching", Boolean(q));
    if (searchClear) searchClear.hidden = !q;
    if (searchKbd) searchKbd.hidden = Boolean(q);

    if (!q) {
      if (searchStatus) searchStatus.hidden = true;
      if (globalEmpty) globalEmpty.hidden = true;
      $$('[data-panel="history"] .card, [data-panel="history"] li').forEach(el => {
        el.hidden = false;
      });
      panels.forEach(p => {
        p.hidden = activeTab !== "all" && p.dataset.panel !== activeTab;
      });
      applyRoadmapFilters();
      applyCharFilters();
      applyShipped();
      return;
    }

    let totalMatches = 0;
    panels.forEach(panel => {
      const kind = panel.dataset.panel;
      let panelMatches = 0;

      if (kind === "roadmap") {
        $$(".rm-section", panel).forEach(sec => {
          let secMatches = 0;
          $$(".card", sec).forEach(card => {
            const headHit = (card.dataset.headText || "").includes(q);
            let lineHits = 0;
            $$(".line-list li", card).forEach(li => {
              const hit = headHit || li.textContent.toLowerCase().includes(q);
              li.hidden = !hit;
              if (hit) lineHits++;
            });
            card.hidden = lineHits === 0;
            secMatches += lineHits;
          });
          sec.hidden = secMatches === 0;
          panelMatches += secMatches;
        });
      } else if (kind === "characters") {
        tiles.forEach(tile => {
          const hit = tile.dataset.search.includes(q);
          tile.hidden = !hit;
          if (hit) panelMatches++;
        });
        if (charEmpty) charEmpty.hidden = true;
      } else if (kind === "shipped") {
        if (shippedSec) shippedSec.dataset.mode = "area";
        $$(".shipped-by-area .card", panel).forEach(card => {
          const headHit = (card.dataset.headText || "").includes(q);
          let lineHits = 0;
          $$(".line-list li", card).forEach(li => {
            const hit = headHit || li.textContent.toLowerCase().includes(q);
            li.hidden = !hit;
            if (hit) lineHits++;
          });
          card.hidden = lineHits === 0;
          panelMatches += lineHits;
        });
      } else if (kind === "history") {
        $$(".card", panel).forEach(card => {
          const headHit = (card.dataset.headText || "").includes(q);
          let itemHits = 0;
          $$("li", card).forEach(li => {
            const hit = headHit || li.textContent.toLowerCase().includes(q);
            li.hidden = !hit;
            if (hit) itemHits++;
          });
          card.hidden = itemHits === 0;
          panelMatches += itemHits;
        });
      }

      panel.hidden = panelMatches === 0;
      totalMatches += panelMatches;
    });

    if (searchStatus) searchStatus.hidden = false;
    if (searchStatusText) searchStatusText.textContent = `"${raw.trim()}" — ${totalMatches}`;
    if (globalEmpty) globalEmpty.hidden = totalMatches > 0;
  };

  const clearSearch = (focusBack = false) => {
    if (!searchInput) return;
    searchInput.value = "";
    runGlobalSearch();
    if (focusBack) searchInput.focus();
  };

  if (searchInput) {
    searchInput.addEventListener("input", runGlobalSearch);
    searchInput.addEventListener("keydown", e => {
      if (e.key === "Escape") { clearSearch(false); searchInput.blur(); }
    });
  }
  if (searchClear) searchClear.addEventListener("click", () => clearSearch(true));
  if (searchStatusClear) searchStatusClear.addEventListener("click", () => clearSearch(false));

  document.addEventListener("keydown", e => {
    if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (!["INPUT", "TEXTAREA", "SELECT"].includes(tag) && searchInput) {
        e.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    }
  });

  // Hash sync (tabs, section anchors, and shareable team links)
  function updateHash() {
    const activeTeam = team.filter(Boolean);
    let h = "#" + activeTab;
    if (activeTab === "characters" && activeTeam.length) {
      h += "?team=" + activeTeam.map(encodeURIComponent).join(",");
    }
    try {
      if (location.hash !== h) history.replaceState(null, "", h);
    } catch (_) {}
  }

  const applyFromHash = () => {
    const raw = (location.hash || "").replace(/^#/, "");
    if (!raw) {
      showTab(activeTab, false);
      renderTeam(false);
      return;
    }
    const [base, queryPart] = raw.split("?");
    if (queryPart) {
      const params = new URLSearchParams(queryPart);
      const t = params.get("team");
      if (t) {
        team[0] = team[1] = team[2] = "";
        t.split(",").map(s => s.trim()).filter(s => tileBySlug[s]).slice(0, 3).forEach((s, i) => { team[i] = s; });
      }
    }
    if (validTabs.has(base)) {
      showTab(base, false);
    } else if (base && document.getElementById(base)) {
      const el = document.getElementById(base);
      const panel = el.closest(".panel");
      if (panel && validTabs.has(panel.dataset.panel)) showTab(panel.dataset.panel, false);
      requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "start" }));
    } else {
      showTab(activeTab, false);
    }
    renderTeam(false);
  };

  window.addEventListener("hashchange", applyFromHash);
  applyFromHash();
  applyRoadmapFilters();
  applyCharFilters();
  applyShipped();
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


def _render_box_lines(kind, box, board, releases, repo):
    """Return (html_lines, n_done_count, states_seen) for a roadmap box."""
    items = []
    n_done = 0
    for line in box["lines"]:
        state, suffix = line_state(kind, line, releases)
        text = fill(line["text"], board)
        if text is None:
            continue
        if state == "done":
            n_done += 1
            print(f"warn: line {line['id']} is all done; move it to shipped", file=sys.stderr)
        body = linkify_releases(text, releases, repo)
        suf_html = f'<span class="confirm-tag">{esc(suffix.strip())}</span>' if suffix else ""
        items.append(
            f'<li class="{state}" data-state="{state}">'
            f'<span class="line-body">{body}{suf_html}</span></li>\n'
        )
    return "".join(items), n_done


def render(cfg, board, strict_releases=False):
    if board.get("schema") != 2:
        fail("snapshot schema is not 2")
    releases = set(board.get("releases") or [])
    repo = (cfg.get("releases") or {}).get("repo")
    page = board["page"]
    sections = board["sections"]
    lr = board.get("latest_release")
    if lr and not release_mentioned(board, lr["tag"]):
        if strict_releases:
            fail(f"latest release {lr['tag']} not mentioned in shipped")
        print(f"warn: latest release {lr['tag']} is not listed in shipped", file=sys.stderr)

    # Partition sections by role while preserving board order inside each group
    roadmap_sections = [s for s in sections if s["kind"] in ("active", "upcoming", "not_planned")]
    shipped_sections = [s for s in sections if s["kind"] == "shipped"]
    bg_sections = [s for s in sections if s["kind"] == "background"]

    # Pre-compute character rows and breakdown
    levels = board.get("levels") or []
    chars = board.get("characters") or []
    types = board.get("types") or {}
    has_chars = bool(levels and chars)
    char_rows = []
    level_counts = {}
    if has_chars:
        by_key = {lv["key"]: lv for lv in levels}
        for c in sorted(chars, key=lambda c: c["name"].lower()):
            level, n_open, fixed, awaiting = character_level(c, types)
            lv = by_key.get(level)
            if lv is None:
                print(f"warn: level '{level}' has no card; tile rendered without a mark",
                      file=sys.stderr)
                lv = {"key": level, "mark": "", "label": level, "text": ""}
            by_level, by_type = character_breakdown(c, types)
            char_rows.append((c["name"], level, n_open, fixed, awaiting, lv, by_level, by_type))
        level_counts = {lv["key"]: sum(1 for r in char_rows if r[1] == lv["key"]) for lv in levels}

    # Pre-render Roadmap panel to know exact line counts and sizes
    rm_html_parts = []
    n_rm_lines = 0
    rm_sizes = []
    rm_sec_counts = []
    for s in roadmap_sections:
        sec_lines = 0
        sec_boxes_html = []
        for box in s["boxes"]:
            lines_html, _ = _render_box_lines(s["kind"], box, board, releases, repo)
            cnt = lines_html.count("<li ")
            sec_lines += cnt
            sz = box.get("size") or ""
            if sz and sz not in rm_sizes:
                rm_sizes.append(sz)
            size_html = (f'<span class="size size-{esc(sz)}">{esc(sz)} job</span>'
                         if sz else "")
            blurb_html = (f'<p class="card-blurb">{linkify_releases(box["blurb"], releases, repo)}</p>\n'
                          if box.get("blurb") else "")
            head_text = f"{box['title']} {box.get('blurb') or ''}".lower()
            sec_boxes_html.append(
                f'<article class="card" data-size="{esc(sz)}" data-head-text="{esc(head_text)}">\n'
                f'<div class="card-head"><h3>{esc(box["title"])}</h3>{size_html}</div>\n'
                f'{blurb_html}'
                f'<ul class="line-list">\n{lines_html}</ul>\n'
                f'</article>\n'
            )
        n_rm_lines += sec_lines
        rm_sec_counts.append((s["id"], s["heading"], sec_lines))
        note = f' <small>{esc(s["note"])}</small>' if s.get("note") else ""
        rm_html_parts.append(
            f'<section id="{esc(s["id"])}" class="rm-section kind-{esc(s["kind"])}" '
            f'data-section-kind="{esc(s["kind"])}">\n'
            f'<div class="sec-head"><h2>{esc(s["heading"])}{note}</h2>'
            f'<span class="sec-meta">{sec_lines}</span></div>\n'
            f'<div class="box-grid">\n{"".join(sec_boxes_html)}</div>\n'
            f'</section>\n'
        )

    # Pre-render Shipped panel (both By area and By release views)
    shipped_area_cards = []
    shipped_by_rel = {}
    shipped_earlier = []
    n_shipped = 0
    shipped_heading = shipped_sections[0]["heading"] if shipped_sections else ""
    shipped_note = shipped_sections[0].get("note") if shipped_sections else None
    for s in shipped_sections:
        for box in s["boxes"]:
            box_lines_html = []
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is None:
                    continue
                tag = line.get("release")
                date = line.get("date")
                if tag and repo:
                    tail = (f' <a class="rel-tag tail" href="https://github.com/{esc(repo)}/releases/tag/{esc(tag)}">'
                            f'{esc(tag)}</a>')
                elif tag:
                    tail = f' <span class="rel-tag tail">{esc(tag)}</span>'
                elif date:
                    tail = f' <span class="date-tag">{esc(date)}</span>'
                else:
                    tail = ""
                rel_key = tag or "earlier"
                n_shipped += 1
                box_lines_html.append(
                    f'<li class="done" data-state="done" data-release="{esc(rel_key)}">'
                    f'<span class="line-body">{esc(text)}{tail}</span></li>\n'
                )
                entry = (box["title"], text, date)
                if tag:
                    shipped_by_rel.setdefault(tag, []).append(entry)
                else:
                    shipped_earlier.append(entry)
            blurb_html = (f'<p class="card-blurb">{esc(box["blurb"])}</p>\n'
                          if box.get("blurb") else "")
            head_text = f"{box['title']} {box.get('blurb') or ''}".lower()
            shipped_area_cards.append(
                f'<article class="card" data-head-text="{esc(head_text)}">\n'
                f'<div class="card-head"><h3>{esc(box["title"])}</h3>'
                f'<span class="sec-meta">{len(box_lines_html)}</span></div>\n'
                f'{blurb_html}'
                f'<ul class="line-list">\n{"".join(box_lines_html)}</ul>\n'
                f'</article>\n'
            )

    sorted_rel_tags = sorted(shipped_by_rel.keys(), key=version_key, reverse=True)
    shipped_rel_cards = []
    for tag in sorted_rel_tags:
        entries = shipped_by_rel[tag]
        if repo:
            head_link = f'<a href="https://github.com/{esc(repo)}/releases/tag/{esc(tag)}">{esc(tag)}</a>'
        else:
            head_link = esc(tag)
        li_html = "".join(
            f'<li class="done"><span class="line-body">'
            f'<span class="area-tag">{esc(area)}</span>{esc(text)}</span></li>\n'
            for area, text, _ in entries
        )
        shipped_rel_cards.append(
            f'<article class="card" data-release-group="{esc(tag)}">\n'
            f'<div class="rel-card-head"><h3>{head_link}</h3>'
            f'<span class="sec-meta">{len(entries)}</span></div>\n'
            f'<ul class="line-list">\n{li_html}</ul>\n'
            f'</article>\n'
        )
    if shipped_earlier:
        li_html = "".join(
            f'<li class="done"><span class="line-body">'
            f'<span class="area-tag">{esc(area)}</span>{esc(text)}'
            + (f' <span class="date-tag">{esc(dt)}</span>' if dt else "")
            + '</span></li>\n'
            for area, text, dt in shipped_earlier
        )
        shipped_rel_cards.append(
            f'<article class="card" data-release-group="earlier">\n'
            f'<div class="rel-card-head"><h3>Earlier</h3>'
            f'<span class="sec-meta">{len(shipped_earlier)}</span></div>\n'
            f'<ul class="line-list">\n{li_html}</ul>\n'
            f'</article>\n'
        )

    # Pre-render Background panel
    bg_cards = []
    n_bg = 0
    bg_heading = bg_sections[0]["heading"] if bg_sections else ""
    for s in bg_sections:
        for box in s["boxes"]:
            filled_lines = []
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is not None:
                    filled_lines.append(text)
            n_bg += len(filled_lines)
            dated_matches = [DATED_RE.match(t) for t in filled_lines]
            is_timeline = bool(filled_lines) and (sum(1 for m in dated_matches if m) * 2 >= len(filled_lines))
            blurb_html = (f'<p class="card-blurb">{esc(box["blurb"])}</p>\n'
                          if box.get("blurb") else "")
            head_text = f"{box['title']} {box.get('blurb') or ''}".lower()
            if is_timeline:
                items = []
                for t, m in zip(filled_lines, dated_matches):
                    if m:
                        dt, rest = m.group(1), m.group(2)
                        items.append(
                            f'<li><time class="tl-date">{esc(dt)}</time>'
                            f'<span class="tl-text">{linkify_releases(rest, releases, repo)}</span></li>\n'
                        )
                    else:
                        items.append(
                            f'<li><span class="tl-text">{linkify_releases(t, releases, repo)}</span></li>\n'
                        )
                body_html = f'<ol class="timeline">\n{"".join(items)}</ol>\n'
            else:
                items = "".join(
                    f'<li>{linkify_releases(t, releases, repo)}</li>\n' for t in filled_lines
                )
                body_html = f'<ul class="plain">\n{items}</ul>\n'
            bg_cards.append(
                f'<article class="card" data-head-text="{esc(head_text)}">\n'
                f'<div class="card-head"><h3>{esc(box["title"])}</h3></div>\n'
                f'{blurb_html}{body_html}</article>\n'
            )

    # Assemble full document
    out = []
    w = out.append
    w('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    w(f'<title>{esc(page["title"])}</title>\n<meta name="robots" content="noindex, nofollow">\n'
      '<script>try{var t=localStorage.getItem("ts-roadmap-theme");'
      'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);'
      '}catch(e){}document.documentElement.classList.add("js");</script>\n'
      f'<style>\n{CSS}\n</style>\n</head>\n<body id="top">\n<div class="shell">\n')

    # Masthead
    w('<header class="masthead">\n<div class="masthead-top">\n<div class="stamp-row">\n'
      '<span class="live-dot" aria-hidden="true"></span>\n'
      f'<span class="stamp">Last updated {fmt_time(board["exported_at"])}</span>\n')
    if lr:
        lr_text = f'latest release <strong>{esc(lr["tag"])}</strong> ({esc(lr["date"])})'
        if repo:
            w(f'<a class="release-pill" href="https://github.com/{esc(repo)}/releases/tag/{esc(lr["tag"])}">'
              f'{lr_text}</a>\n')
        else:
            w(f'<span class="release-pill">{lr_text}</span>\n')
    w('</div>\n<div class="masthead-actions">\n')
    if page.get("links"):
        w('<nav class="ext-links" aria-label="Links">')
        arrow_svg = (
            '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            '<path d="M5 11L11 5M6 5h5v5"/></svg>'
        )
        for idx, lk in enumerate(page["links"]):
            cls = "btn btn-primary" if idx == 0 else "btn"
            w(f'<a class="{cls}" href="{esc(lk["href"])}" rel="noopener">{esc(lk["label"])}{arrow_svg}</a>')
        w('</nav>\n')
    w('<button type="button" class="btn btn-icon js-only inline-flex-only" id="theme-toggle" '
      'aria-label="Toggle theme" aria-pressed="false">'
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'
      '<circle cx="8" cy="8" r="3.2"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6'
      'M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/></svg>'
      '<span class="theme-label">Light</span></button>\n')
    w('</div>\n</div>\n')

    w(f'<div class="masthead-title">\n<h1>{esc(page["title"])}</h1>\n')
    if page.get("tagline"):
        w(f'<p class="tagline">{esc(page["tagline"])}</p>\n')
    w('</div>\n')

    if page.get("goal") or page.get("notice"):
        two = " two-col" if (page.get("goal") and page.get("notice")) else ""
        w(f'<div class="brief-grid{two}">\n')
        if page.get("goal"):
            w(f'<div class="brief-card brief-goal"><strong>{esc(page["goal"])}</strong></div>\n')
        if page.get("notice"):
            w(f'<div class="brief-card brief-notice">{esc(page["notice"])}</div>\n')
        w('</div>\n')

    if has_chars:
        total_c = len(char_rows)
        w(f'<div class="roster-pulse" aria-label="{esc(page["characters_heading"])}">\n'
          '<div class="pulse-head">\n'
          f'<div class="pulse-title">{esc(page["characters_heading"])}'
          f' <span class="pulse-total">{total_c}</span></div>\n'
          '<button type="button" class="pill js-only inline-flex-only" id="pulse-team-btn">Team check →</button>\n'
          '</div>\n<div class="pulse-bar">\n')
        for lv in levels:
            cnt = level_counts[lv["key"]]
            if cnt:
                pct = cnt * 100.0 / total_c
                w(f'<button type="button" class="pulse-seg {esc(lv["key"])}" '
                  f'style="width:{pct:.2f}%" data-pulse-level="{esc(lv["key"])}" '
                  f'title="{esc(lv["label"])}: {cnt}" aria-label="{esc(lv["label"])}: {cnt}"></button>\n')
        w('</div>\n<div class="pulse-chips">\n')
        for lv in levels:
            cnt = level_counts[lv["key"]]
            w(f'<button type="button" class="pulse-chip" data-pulse-level="{esc(lv["key"])}">'
              f'<b class="{esc(lv["key"])}">{esc(lv["mark"])}</b> {esc(lv["label"])} '
              f'<strong>{cnt}</strong></button>\n')
        w('</div>\n</div>\n')
    w('</header>\n')

    # Sticky Command Bar (tabs + global search)
    w('<div class="command-bar js-only" id="command-bar">\n<div class="command-inner">\n'
      '<div class="tabs" role="tablist">\n')
    first_tab = True

    def add_tab(tid, label, count=None):
        nonlocal first_tab
        cls = "tab is-active" if first_tab else "tab"
        sel = "true" if first_tab else "false"
        first_tab = False
        cnt_html = f' <span class="tab-count">{count}</span>' if count is not None else ""
        w(f'<button type="button" class="{cls}" role="tab" data-tab="{esc(tid)}" '
          f'aria-selected="{sel}">{esc(label)}{cnt_html}</button>\n')

    if roadmap_sections:
        add_tab("roadmap", "Roadmap", n_rm_lines)
    if has_chars:
        add_tab("characters", page["characters_heading"], len(char_rows))
    if shipped_sections:
        add_tab("shipped", shipped_heading, n_shipped)
    if bg_sections:
        add_tab("history", bg_heading, n_bg)
    add_tab("all", "All")
    w('</div>\n<div class="search-wrap">\n'
      '<svg class="search-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">'
      '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>\n'
      '<input type="search" id="global-search" class="search-input" placeholder="Search…" aria-label="Search">\n'
      '<kbd class="search-kbd">/</kbd>\n'
      '<button type="button" id="search-clear" class="search-clear" hidden aria-label="Clear search">×</button>\n'
      '</div>\n</div>\n</div>\n')

    w('<div id="search-status" class="search-status" hidden aria-live="polite">\n'
      '<span id="search-status-text"></span>\n'
      '<button type="button" id="search-status-clear" class="pill">Clear</button>\n'
      '</div>\n')
    w('<div id="global-empty" class="empty-state" hidden>0</div>\n')

    # Main panels
    w('<main id="main-content">\n')

    # 1. Roadmap panel
    if roadmap_sections:
        w('<div class="panel" data-panel="roadmap">\n')
        w('<div class="rm-controls js-only flex-only">\n<div class="pill-group">\n')
        for sid, shead, scnt in rm_sec_counts:
            w(f'<a class="pill" href="#{esc(sid)}" data-jump-sec="{esc(sid)}">'
              f'{esc(shead)} <span class="pill-num">{scnt}</span></a>\n')
        w('</div>\n')
        if rm_sizes:
            size_order = {"small": 0, "medium": 1, "large": 2}
            ordered_sizes = sorted(rm_sizes, key=lambda x: (size_order.get(x, 9), x))
            w('<div class="pill-group">\n'
              '<button type="button" class="pill is-active" data-size-filter="">All</button>\n')
            for sz in ordered_sizes:
                w(f'<button type="button" class="pill" data-size-filter="{esc(sz)}">{esc(sz)} job</button>\n')
            w('</div>\n')
        w('</div>\n')

        w('<ul class="legend">' + "".join(
            f'<li class="{esc(l["class"])}" data-filter-state="{esc(l["class"])}" '
            f'tabindex="0" role="button" aria-pressed="false">{esc(l["text"])}</li>'
            for l in cfg["legend"]
        ) + '</ul>\n')

        w("".join(rm_html_parts))
        w('<div id="rm-empty" class="empty-state" hidden>0</div>\n')
        w('</div>\n')

    # 2. Characters panel
    if has_chars:
        levels_json = json.dumps(
            [{"key": lv["key"], "mark": lv["mark"], "label": lv["label"], "text": lv["text"]}
             for lv in levels],
            separators=(",", ":"),
        )
        n_await_chars = sum(1 for r in char_rows if r[4] > 0)
        w('<div class="panel" data-panel="characters">\n')
        w(f'<section id="characters" class="char-section" data-levels="{esc(levels_json)}">\n'
          f'<div class="sec-head"><h2>{esc(page["characters_heading"])}</h2>'
          f'<span class="sec-meta" id="char-shown-count">{len(char_rows)} / {len(char_rows)}</span></div>\n')
        if page.get("characters_banner"):
            w(f'<p class="banner">{esc(page["characters_banner"])}</p>\n')

        # 3-Character Team Checker
        char_options = "".join(
            f'<option value="{esc(slugify(name))}">{esc(name)}</option>'
            for name, *_ in char_rows
        )
        w('<div class="team-bar js-only" id="team-bar">\n'
          '<div class="team-head">\n<div class="team-title">'
          '<span>Team check</span>'
          '<span id="team-verdict" class="team-verdict" aria-live="polite">0 / 3</span>'
          '<span id="team-summary" class="team-summary"></span>'
          '</div>\n<div class="team-actions">\n'
          '<button type="button" class="pill" id="team-copy" hidden>Copy link</button>\n'
          '<button type="button" class="pill" id="team-clear" hidden>Clear</button>\n'
          '</div>\n</div>\n<div class="team-slots">\n')
        for slot_i in range(3):
            w(f'<div class="team-slot" data-slot="{slot_i}">\n'
              '<div class="slot-top">\n'
              f'<span class="slot-num">{slot_i + 1}</span>\n'
              f'<select class="slot-select" aria-label="Slot {slot_i + 1}">'
              f'<option value="">—</option>{char_options}</select>\n'
              f'<button type="button" class="slot-clear" hidden aria-label="Clear slot {slot_i + 1}">×</button>\n'
              '</div>\n<div class="slot-info"></div>\n</div>\n')
        w('</div>\n</div>\n')

        # Level filter cards
        w('<div class="levels">\n')
        for lv in levels:
            cnt = level_counts[lv["key"]]
            w(f'<button type="button" class="level-card {esc(lv["key"])}" '
              f'data-level-filter="{esc(lv["key"])}" aria-pressed="false">\n'
              '<div class="level-card-top">'
              f'<span class="level-card-name"><b class="{esc(lv["key"])}">{esc(lv["mark"])}</b> {esc(lv["label"])}</span>'
              f'<span class="level-card-count">{cnt}</span></div>\n'
              f'<p class="level-card-text">{esc(lv["text"])}</p>\n'
              '</button>\n')
        w('</div>\n')

        # Roster filter & sort controls
        w('<div class="roster-controls js-only flex-only">\n'
          '<input type="search" id="char-filter" class="roster-filter-input" '
          'placeholder="Filter…" aria-label="Filter">\n'
          '<select id="char-sort" class="roster-select" aria-label="Sort">\n'
          '<option value="name">A–Z</option>\n'
          '<option value="severity">Severity</option>\n'
          '<option value="open_desc">Most open</option>\n'
          '<option value="open_asc">Fewest open</option>\n'
          '<option value="fixed_desc">Most fixed</option>\n'
          '<option value="await_desc">Awaiting confirmation</option>\n'
          '</select>\n'
          f'<button type="button" id="char-await-btn" class="pill" aria-pressed="false">'
          f'Awaiting confirmation <span class="pill-num">{n_await_chars}</span></button>\n'
          '<button type="button" id="char-reset-btn" class="pill" hidden>Reset</button>\n'
          '</div>\n')

        # Character tiles
        w('<div class="tiles">\n')
        for name, level, n_open, fixed, awaiting, lv, by_level, by_type in char_rows:
            slug = slugify(name)
            sev = SEV_RANK.get(level, 9)
            type_words = " ".join(t for t, _, _ in by_type)
            search_blob = f"{name} {lv['label']} {type_words}".lower()
            extra = (f' · <span class="tile-await">{awaiting} awaiting confirmation</span>'
                     if awaiting else "")
            if n_open > 0:
                segs = "".join(
                    f'<span class="sev-seg-{k}" style="flex:{by_level[k]}"></span>'
                    for k in SEVERITY if by_level[k] > 0
                )
                bar_html = f'<span class="sev-bar" aria-hidden="true">{segs}</span>'
            else:
                bar_html = ""
            chips_html = ""
            if by_type:
                chips = "".join(
                    f'<span class="type-chip {esc(t_lv)}">{esc(t_name)} {t_cnt}</span>'
                    for t_name, t_lv, t_cnt in by_type
                )
                chips_html = f'<div class="tile-types">{chips}</div>'
            w(f'<article class="tile {esc(level)}" data-slug="{esc(slug)}" '
              f'data-name="{esc(name.lower())}" data-level="{esc(level)}" data-sev="{sev}" '
              f'data-open="{n_open}" data-fixed="{fixed}" data-awaiting="{awaiting}" '
              f'data-search="{esc(search_blob)}">\n'
              '<div class="tile-top">'
              f'<b class="tile-name"><span class="lv-{esc(level)}">{esc(lv["mark"])}</span> {esc(name)}</b>'
              f'<button type="button" class="team-add js-only inline-flex-only" '
              f'aria-label="Team check: {esc(name)}" aria-pressed="false" title="Team check">+</button>'
              '</div>\n'
              f'<div class="tile-mid"><span class="tile-level {esc(level)}">{esc(lv["label"])}</span>{bar_html}</div>\n'
              f'<small class="tile-stats">{n_open} open · {fixed} fixed{extra}</small>\n'
              f'{chips_html}</article>\n')
        w('</div>\n<div id="char-empty" class="empty-state" hidden>0</div>\n</section>\n</div>\n')

    # 3. Shipped panel
    if shipped_sections:
        note = f' <small>{esc(shipped_note)}</small>' if shipped_note else ""
        w('<div class="panel" data-panel="shipped">\n')
        w(f'<section id="shipped" class="shipped-section" data-mode="area">\n'
          f'<div class="sec-head"><h2>{esc(shipped_heading)}{note}</h2>'
          f'<span class="sec-meta">{n_shipped}</span></div>\n')
        w('<div class="shipped-toolbar js-only flex-only">\n'
          '<div class="seg-switch" role="group" aria-label="View">\n'
          '<button type="button" class="seg-btn is-active" data-ship-mode="area" aria-pressed="true">By area</button>\n'
          '<button type="button" class="seg-btn" data-ship-mode="release" aria-pressed="false">By release</button>\n'
          '</div>\n<div class="release-chips">\n'
          '<button type="button" class="pill is-active" data-rel-filter="">All</button>\n')
        for tag in sorted_rel_tags:
            w(f'<button type="button" class="pill" data-rel-filter="{esc(tag)}">{esc(tag)}</button>\n')
        if shipped_earlier:
            w('<button type="button" class="pill" data-rel-filter="earlier">Earlier</button>\n')
        w('</div>\n</div>\n')
        w(f'<div class="box-grid shipped-by-area">\n{"".join(shipped_area_cards)}</div>\n')
        w(f'<div class="box-grid shipped-by-release">\n{"".join(shipped_rel_cards)}</div>\n')
        w('</section>\n</div>\n')

    # 4. Background panel
    if bg_sections:
        w('<div class="panel" data-panel="history">\n')
        w(f'<section id="history" class="history-section">\n'
          f'<div class="sec-head"><h2>{esc(bg_heading)}</h2>'
          f'<span class="sec-meta">{n_bg}</span></div>\n'
          f'<div class="history-grid">\n{"".join(bg_cards)}</div>\n'
          '</section>\n</div>\n')

    w('</main>\n')
    if page.get("footer"):
        w(f'<footer class="site-footer"><p>{esc(page["footer"])}</p>'
          '<a href="#top" class="back-top" aria-label="Top">↑</a></footer>\n')
    w('</div>\n')
    w(f'<script>\n{JS}\n</script>\n</body>\n</html>\n')
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
