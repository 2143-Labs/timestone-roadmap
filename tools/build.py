#!/usr/bin/env python3
"""Build the roadmap page from the board snapshot and the chart's config.

Usage: build.py [--config config/roadmap.json] [--board data/board.json]
                [--out site/index.html] [--strict-releases]

Every word on the page comes from the board snapshot (boxes, lines, page text,
character levels and issue types) except the legend wording, the size words and
the connecting words in the config's `words` block. This file holds no
visitor-facing prose. Fails (exit 1, old page untouched) on anything that would
put wrong or internal information on the public page. An unchanged snapshot
gives a byte-identical page.

Layout: a header, a sticky jump bar whose entries carry a status strip for
their section, the character roster with level filters, the active and
upcoming columns side by side as lanes with state filters, the not-planned
list, shipped work as a changelog by release with area filters, and the
background. The filters are CSS only (`:has()`), so the page carries no script;
without `:has()` they do nothing and everything stays visible.
"""
import argparse
import html
import json
import os
import re
import sys

TOKEN_RE = re.compile(r"\{([^{}]*)\}")
MAX_BYTES = 600_000
CLASS_RE = re.compile(r"[a-z][a-z0-9-]*\Z")
HISTORY_RE = re.compile(r"(\d{4}-\d\d-\d\d):\s+(\S.*)\Z", re.S)
# Connecting words the page needs beyond the board's own text; config `words`.
WORD_KEYS = ("updated", "latest_release", "size", "in_release", "fix_released",
             "open", "fixed", "awaiting")


def fail(reason):
    sys.exit(f"build failed: {reason}")


def warn(msg):
    print(f"warn: {msg}", file=sys.stderr)


def esc(s):
    return html.escape(str(s), quote=True)


def words(cfg):
    """The config's `words` block, checked for every key the page uses."""
    w = cfg.get("words")
    if not isinstance(w, dict):
        fail("config has no 'words' block")
    missing = [k for k in WORD_KEYS if not isinstance(w.get(k), str) or not w[k].strip()]
    if missing:
        fail(f"config words missing: {', '.join(missing)}")
    return w


def say(w, key, **fields):
    try:
        return w[key].format(**fields)
    except (KeyError, IndexError, ValueError) as e:
        fail(f"config words.{key} is malformed: {e!r}")


def legend_items(cfg):
    items = cfg.get("legend") or []
    for item in items:
        if not CLASS_RE.match(str(item.get("class", ""))):
            fail(f"legend class {item.get('class')!r} is not a plain class name")
    return items


def _fallback(kind):
    return "now" if kind == "active" else "open"


def line_state(kind, line, releases, w):
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
            return "verifying", say(w, "in_release", tag=tag)
        warn(f"line {line['id']} names release {tag}, which is not a known release")
    if any(not c["done"] and c["needs_repro"] for c in cards):
        return "verifying", say(w, "fix_released")
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


def issue_mix(char, types):
    """Open, counted issues of one character per level key."""
    mix = {}
    for issue in char["issues"]:
        key = None if issue["done"] else types.get(issue.get("type"))
        if key is not None:
            mix[key] = mix.get(key, 0) + 1
    return mix


def fill(text, board):
    """The text with board counts filled in, or None when it holds an unknown token."""
    for tok in TOKEN_RE.findall(text):
        if tok != "verifying_open":
            warn(f"unknown token {{{tok}}}; line dropped")
            return None
    return text.replace("{verifying_open}", str(board["stats"]["verifying_open"]))


def split_time(ts):
    # "2026-11-25T05:30Z" -> ("2026-11-25", "05:30")
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)Z", ts)
    if not m:
        fail(f"bad exported_at '{ts}'")
    return m.group(1), m.group(2)


def version_key(tag):
    m = re.fullmatch(r"v(\d+(?:\.\d+)*)", tag or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def rel_id(tag):
    # Section ids are [a-z0-9-] slugs, so the underscore keeps these apart.
    return "rel_" + re.sub(r"[^A-Za-z0-9.-]", "-", tag)


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


def changelog(entries):
    """Group shipped entries by release (newest first), then by date (newest
    first), then the rest; board order is kept inside a group.

    `entries` are dicts with `release`, `date`, `text` and `area`. Returns
    [(kind, key, entries)] with kind release|date|rest.
    """
    groups = {}
    for e in entries:
        if e["release"]:
            k = ("release", e["release"])
        elif e["date"]:
            k = ("date", e["date"])
        else:
            k = ("rest", "")
        groups.setdefault(k, []).append(e)
    tags = [k for k in groups if k[0] == "release"]
    order = (sorted((k for k in tags if version_key(k[1])), key=lambda k: version_key(k[1]),
                    reverse=True)
             + sorted(k for k in tags if not version_key(k[1]))
             + sorted((k for k in groups if k[0] == "date"), reverse=True)
             + [k for k in groups if k[0] == "rest"])
    return [(k[0], k[1], groups[k]) for k in order]


CSS = """
:root{color-scheme:dark;--bg:#13151a;--panel:#1a1d23;--card:#22262e;--edge:#30353f;--text:#e7e8eb;--soft:#c6c9cf;--muted:#99a0aa;--link:#5aaeff}
*{box-sizing:border-box}
html{scroll-padding-top:5.2rem}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--link)}
.wrap{max-width:1380px;margin:0 auto;padding:0 1rem}
.s-now{--c:#4aa3ff;--g:"▶"}.s-open{--c:#99a0aa;--g:"○"}.s-verifying{--c:#f0b429;--g:"◷"}.s-done{--c:#4caf50;--g:"✔"}.s-no{--c:#f47067;--g:"✕"}
.l-ready{--c:#4caf50}.l-playable{--c:#8bc34a}.l-desync{--c:#f0b429}.l-crash{--c:#f47067}.l-untested{--c:#99a0aa}
.hero{background:radial-gradient(60rem 18rem at 0 0,#1f3350 0,transparent 70%);padding:1.8rem 0 1.2rem}
.top{display:flex;flex-wrap:wrap;gap:1rem 2rem;align-items:flex-end;justify-content:space-between}
h1{margin:0;font-size:clamp(1.8rem,4vw,2.6rem);line-height:1.1;letter-spacing:-.01em}
.tagline{margin:.35rem 0 0;color:var(--soft);font-size:1.05rem}
.links{display:flex;flex-wrap:wrap;gap:.5rem}
.links a{padding:.45rem 1.1rem;border:1px solid var(--edge);border-radius:999px;background:var(--card);color:var(--text);text-decoration:none;font-weight:600}
.links a:first-child{background:#2563eb;border-color:#2563eb;color:#fff}
.links a:hover{border-color:var(--link)}
.goal{margin:1.3rem 0 .5rem;padding-left:.9rem;border-left:4px solid var(--link);font-size:clamp(1.05rem,2vw,1.3rem);font-weight:650;max-width:62ch}
.notice{margin:.4rem 0;color:var(--soft);max-width:75ch}
.stamp{margin:.7rem 0 0;color:var(--muted);font-size:.85rem}
.stamp a{color:inherit}
.jump{position:sticky;top:0;z-index:5;background:#13151aee;border-bottom:1px solid var(--edge);backdrop-filter:blur(6px)}
.jump .wrap{display:flex;gap:.3rem;overflow-x:auto;scrollbar-width:none}
.jump a{flex:1 0 auto;min-width:6.5rem;display:flex;flex-direction:column;gap:.3rem;padding:.55rem .7rem .6rem;border-radius:6px;color:var(--text);text-decoration:none;font-size:.92rem;white-space:nowrap}
.jump a:hover,.jump a:focus-visible{background:var(--card)}
.jump b{margin-left:.35rem;color:var(--muted);font-weight:500}
.jump .bar{height:4px}
.bar{display:flex;gap:1px;height:6px;border-radius:3px;overflow:hidden;background:var(--edge)}
.bar i{background:var(--c)}
main{padding-bottom:1rem}
section{scroll-margin-top:5.2rem}
h2{margin:2.2rem 0 .7rem;font-size:1.55rem;line-height:1.2}
h2 small{margin-left:.35rem;color:var(--muted);font-size:.8rem;font-weight:400}
.sr{position:absolute;width:1px;height:1px;margin:0;opacity:0}
.banner{margin:.4rem 0 1rem;padding:.55rem .85rem;border-left:4px solid #f0b429;border-radius:4px;background:#3a2f12;color:#f3e3bd}
.levels{display:grid;gap:.6rem;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr))}
.lv{display:grid;grid-template-columns:auto 1fr;gap:0 .6rem;align-items:baseline;padding:.6rem .8rem;border:1px solid var(--edge);border-top:3px solid var(--c);border-radius:8px;background:var(--panel);cursor:pointer;user-select:none}
.lv .n{grid-row:span 2;font-size:1.9rem;font-weight:700;line-height:1;color:var(--c)}
.lv span{font-weight:600}
.lv small{color:var(--muted);font-size:.8rem;line-height:1.3}
.mk{color:var(--c)}
div.lv{opacity:.75}
.spread{height:10px;margin:.8rem 0 1rem;border-radius:5px}
.roster{display:grid;gap:.5rem;grid-template-columns:repeat(auto-fill,minmax(10.5rem,1fr));margin:0;padding:0;list-style:none}
.tile{display:flex;flex-direction:column;gap:.15rem;padding:.45rem .6rem .5rem;border-left:4px solid var(--c);border-radius:5px;background:var(--card)}
.tile b{line-height:1.25}
.tile .lvl{color:var(--c);font-size:.8rem}
.tile .bar{height:4px;margin:.15rem 0}
.tile small{color:var(--muted);font-size:.74rem;line-height:1.3}
.chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:1.4rem 0 .2rem}
.chip{display:inline-flex;gap:.4rem;align-items:baseline;padding:.3rem .8rem;border:1px solid var(--edge);border-radius:999px;background:var(--panel);font-size:.88rem;user-select:none}
.chip.st::before{content:var(--g);color:var(--c)}
label.chip{cursor:pointer}
.chip b{color:var(--muted);font-weight:600}
span.chip{border-color:transparent;background:none}
label.chip:hover,label.lv:hover{border-color:var(--c,var(--link))}
input:checked+.chip,input:checked+.lv{border-color:var(--c,var(--link));background:color-mix(in srgb,var(--c,var(--link)) 20%,var(--panel))}
input:focus-visible+.chip,input:focus-visible+.lv{outline:2px solid var(--link);outline-offset:2px}
.lanes{display:grid;gap:1rem;margin-top:.6rem}
@media(min-width:960px){.lanes{grid-auto-flow:column;grid-auto-columns:minmax(19rem,1fr);overflow-x:auto;align-items:start}}
.lane{padding:0 .8rem .9rem;border:1px solid var(--edge);border-radius:10px;background:var(--panel)}
.lane h2{margin:.9rem 0 .5rem;font-size:1.35rem}
.lane h2 small{display:block;margin:.1rem 0 0}
.lane.active{border-top:3px solid #4aa3ff}
@media(max-width:959px){.lane,.lane.active{padding:0;border:0;background:none}}
.box{margin-top:.8rem;padding:.8rem .9rem .7rem;border-radius:8px;background:var(--card)}
.box h3{display:flex;gap:.6rem;justify-content:space-between;align-items:baseline;margin:0;font-size:1.06rem;line-height:1.3}
.size{flex:none;padding:0 .45rem;border:1px solid var(--edge);border-radius:4px;color:var(--muted);font-size:.72rem;font-weight:500;white-space:nowrap}
.box>p{margin:.35rem 0 .5rem;color:var(--soft);font-size:.92rem}
.box>.bar{height:4px;margin:.55rem 0 .2rem}
ul.lines{margin:.4rem 0 0;padding:0;list-style:none}
.ln{position:relative;margin:.35rem 0;padding-left:1.35rem;font-size:.95rem}
.ln::before{content:var(--g);position:absolute;left:0;color:var(--c)}
.lanes .ln.s-done{opacity:.55;text-decoration:line-through}
.sfx{color:var(--muted);font-size:.86em}
.np .box ul{columns:2 22rem;column-gap:2rem}
.np .ln{break-inside:avoid}
.notes{display:grid;gap:.2rem .8rem;grid-template-columns:max-content 1fr;margin:.6rem 0;font-size:.9rem}
.notes dt{font-weight:600}.notes dd{margin:0;color:var(--soft)}
.cl{margin:.8rem 0 0;padding:.2rem 1rem;border-radius:10px;background:var(--panel);list-style:none}
.rel{display:grid;grid-template-columns:8.5rem 1fr;gap:.1rem 1rem;padding:.5rem 0}
.rel+.rel{border-top:1px solid var(--edge)}
.rel h3{display:flex;flex-direction:column;margin:.3rem 0 0;font-size:1rem;line-height:1.3}
.rel h3 a,.rel h3 span{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.rel time{color:var(--muted);font-size:.82rem;font-weight:400}
.rel ul.lines{grid-column:2;margin:0}
@media(max-width:640px){.rel{grid-template-columns:1fr}.rel ul.lines{grid-column:auto}}
.area{display:inline-block;margin-left:.3rem;padding:0 .45rem;border:1px solid var(--edge);border-radius:999px;color:var(--muted);font-size:.72rem;white-space:nowrap}
.bgrid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(20rem,1fr));align-items:start}
.tl{margin:.5rem 0 0;padding:0;list-style:none}
.tl li{display:grid;grid-template-columns:6.4rem 1fr;gap:.6rem;padding:.3rem 0;border-top:1px solid var(--edge);font-size:.93rem}
.tl time{color:var(--muted);font-variant-numeric:tabular-nums}
.plain{margin:.5rem 0 0;padding-left:1.2rem;font-size:.93rem}
.plain li{margin:.3rem 0}
footer{margin-top:2.5rem;padding:1.2rem 0 2rem;border-top:1px solid var(--edge);color:var(--muted);font-size:.9rem}
@supports not selector(:has(a)){label.chip,label.lv{pointer-events:none}}
@media print{:root{--bg:#fff;--panel:#fff;--card:#fff;--edge:#bbb;--text:#000;--soft:#222;--muted:#555;--link:#000}.jump{display:none}.hero{background:none}.banner{background:none;color:#000}.lanes{display:block}}
""".strip()


def bar(parts, cls="bar"):
    """A stacked strip, one segment per (class, count); decorative."""
    segs = "".join(f'<i class="{esc(c)}" style="flex-grow:{n}"></i>' for c, n in parts if n)
    return f'<span class="{cls}" aria-hidden="true">{segs}</span>' if segs else ""


def filter_css(scope, toggle, target, keys, show, holders=()):
    """CSS rules for the checkbox filters inside `.scope`.

    `keys` pairs each checkbox id with the class it selects. Once any `.toggle`
    checkbox is checked, only `.target` items carrying a checked class show (as
    `display: show`); each (holder, display) group left without a visible item
    hides too.
    """
    if not keys:
        return []
    rules = [f".{scope}:has(.{toggle}:checked) .{target}{{display:none}}"]
    rules += [f".{scope}:has(.{toggle}:checked) .{h}{{display:none}}" for h, _ in holders]
    for box_id, cls in keys:
        rules.append(f".{scope}:has(#{box_id}:checked) .{target}.{cls}{{display:{show}}}")
        rules += [f".{scope}:has(#{box_id}:checked) .{h}:has(.{target}.{cls}){{display:{d}}}"
                  for h, d in holders]
    return rules


def render(cfg, board, strict_releases=False):
    if board.get("schema") != 2:
        fail("snapshot schema is not 2")
    w = words(cfg)
    legend = legend_items(cfg)
    releases = set(board.get("releases") or [])
    repo = (cfg.get("releases") or {}).get("repo")
    page = board["page"]
    sections = board["sections"]

    lr = board.get("latest_release")
    if lr and not release_mentioned(board, lr["tag"]):
        if strict_releases:
            fail(f"latest release {lr['tag']} not mentioned in shipped")
        warn(f"latest release {lr['tag']} is not listed in shipped")

    # --- gather: everything is computed before writing, the jump bar needs counts.
    levels = board.get("levels") or []
    chars = board.get("characters") or []
    types = board.get("types") or {}
    roster = []
    if levels and chars:
        known = {lv["key"] for lv in levels}
        for c in sorted(chars, key=lambda c: c["name"].lower()):
            level, n_open, fixed, awaiting = character_level(c, types)
            if level not in known:
                warn(f"level '{level}' has no card; tile rendered without a mark")
            roster.append({"name": c["name"], "level": level, "open": n_open, "fixed": fixed,
                           "awaiting": awaiting, "mix": issue_mix(c, types)})
    by_key = {lv["key"]: lv for lv in levels}
    lv_index = {lv["key"]: i for i, lv in enumerate(levels)}
    lv_count = {lv["key"]: sum(1 for r in roster if r["level"] == lv["key"]) for lv in levels}

    plan = []  # (section, [(box, [(state, text, suffix)])])
    for s in sections:
        if s["kind"] not in ("active", "upcoming", "not_planned"):
            continue
        boxes = []
        for box in s["boxes"]:
            rows = []
            for line in box["lines"]:
                state, suffix = line_state(s["kind"], line, releases, w)
                text = fill(line["text"], board)
                if text is None:
                    continue
                if state == "done":
                    warn(f"line {line['id']} is all done; move it to shipped")
                rows.append((state, text, suffix))
            boxes.append((box, rows))
        plan.append((s, boxes))

    ship_secs, areas, entries = [], [], []
    for s in sections:
        if s["kind"] != "shipped":
            continue
        ship_secs.append(s)
        for box in s["boxes"]:
            a = len(areas)
            areas.append({"box": box, "n": 0, "section": len(ship_secs) - 1})
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is None:
                    continue
                areas[a]["n"] += 1
                entries.append({"release": line.get("release"), "date": line.get("date"),
                                "text": text, "area": a})
    background = [s for s in sections if s["kind"] == "background"]

    def state_counts(boxes):
        counts = {}
        for _, rows in boxes:
            for state, _, _ in rows:
                counts[state] = counts.get(state, 0) + 1
        return counts

    state_keys = [item["class"] for item in legend]
    state_keys += [k for k in ("now", "open", "verifying", "done", "no") if k not in state_keys]
    plan_counts = state_counts([bx for _, boxes in plan for bx in boxes])
    # Only states that occur on the plan filter; the rest stay a plain legend entry.
    chip_states = [item["class"] for item in legend if plan_counts.get(item["class"])]

    def state_bar(counts, cls="bar"):
        return bar([(f"s-{k}", counts.get(k, 0)) for k in state_keys], cls)

    def note(s):
        return f' <small>{esc(s["note"])}</small>' if s.get("note") else ""

    # --- head
    out = []
    o = out.append
    o('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    o(f'<title>{esc(page["title"])}</title>\n<meta name="robots" content="noindex, nofollow">\n')

    css = [CSS]
    css += filter_css("plan", "stf", "ln", [(f"stf_{j}", f"s-{k}") for j, k in enumerate(chip_states)],
                      "list-item", [("box", "block"), ("np", "block")])
    css += filter_css("chars", "lvf", "tile", [(f"lvf_{i}", f"lv{i}") for i, lv in enumerate(levels)
                                                if lv_count[lv["key"]]], "flex")
    css += filter_css("ship", "arf", "ent", [(f"arf_{i}", f"ar{i}") for i in range(len(areas))],
                      "list-item", [("rel", "grid")])
    o("<style>\n" + "\n".join(css) + "\n</style>\n</head>\n<body>\n")

    o('<header class="hero">\n<div class="wrap">\n<div class="top">\n<div>\n')
    o(f'<h1>{esc(page["title"])}</h1>\n')
    if page.get("tagline"):
        o(f'<p class="tagline">{esc(page["tagline"])}</p>\n')
    o("</div>\n")
    if page.get("links"):
        o('<nav class="links">' + "".join(f'<a href="{esc(l["href"])}">{esc(l["label"])}</a>'
                                          for l in page["links"]) + "</nav>\n")
    o("</div>\n")
    if page.get("goal"):
        o(f'<p class="goal">{esc(page["goal"])}</p>\n')
    if page.get("notice"):
        o(f'<p class="notice">{esc(page["notice"])}</p>\n')
    day, clock = split_time(board["exported_at"])
    stamp = esc(say(w, "updated", date=day, time=clock))
    if lr:
        latest = esc(say(w, "latest_release", tag=lr["tag"], date=lr["date"]))
        if any(e["release"] == lr["tag"] for e in entries):
            latest = f'<a href="#{esc(rel_id(lr["tag"]))}">{latest}</a>'
        stamp += " · " + latest
    o(f'<p class="stamp">{stamp}</p>\n</div>\n</header>\n')

    # --- jump bar: one entry per block, with its count and status strip
    jumps = []
    if roster:
        jumps.append(("characters", page["characters_heading"], len(roster),
                      bar([(f"l-{lv['key']}", lv_count[lv["key"]]) for lv in levels])))
    for s, boxes in plan:
        counts = state_counts(boxes)
        jumps.append((s["id"], s["heading"], sum(counts.values()), state_bar(counts)))
    for si, s in enumerate(ship_secs):
        n = sum(a["n"] for a in areas if a["section"] == si)
        jumps.append((s["id"], s["heading"], n, bar([("s-done", n)])))
    for s in background:
        jumps.append((s["id"], s["heading"], None, ""))
    o('<nav class="jump">\n<div class="wrap">\n')
    for sid, heading, n, strip in jumps:
        count = f" <b>{n}</b>" if n is not None else ""
        o(f'<a href="#{esc(sid)}"><span>{esc(heading)}{count}</span>{strip}</a>\n')
    o("</div>\n</nav>\n")

    o('<main class="wrap">\n')

    # --- characters: the board's column order does not place this block, so it
    # opens the page on purpose.
    if roster:
        o(f'<section id="characters" class="chars">\n<h2>{esc(page["characters_heading"])}</h2>\n')
        if page.get("characters_banner"):
            o(f'<p class="banner">{esc(page["characters_banner"])}</p>\n')
        o('<div class="levels">\n')
        for i, lv in enumerate(levels):
            n = lv_count[lv["key"]]
            body = (f'<b class="n">{n}</b><span><span class="mk">{esc(lv["mark"])}</span> '
                    f'{esc(lv["label"])}</span><small>{esc(lv["text"])}</small>')
            if n:
                o(f'<input type="checkbox" id="lvf_{i}" class="lvf sr">'
                  f'<label for="lvf_{i}" class="lv l-{esc(lv["key"])}">{body}</label>\n')
            else:
                o(f'<div class="lv l-{esc(lv["key"])}">{body}</div>\n')
        o("</div>\n")
        o(bar([(f"l-{lv['key']}", lv_count[lv["key"]]) for lv in levels], "bar spread") + "\n")
        o('<ul class="roster">\n')
        for r in roster:
            lv = by_key.get(r["level"], {"mark": "", "label": r["level"]})
            idx = f' lv{lv_index[r["level"]]}' if r["level"] in lv_index else ""
            counts = [say(w, "open", n=r["open"]), say(w, "fixed", n=r["fixed"])]
            if r["awaiting"]:
                counts.append(say(w, "awaiting", n=r["awaiting"]))
            mix = bar([(f"l-{lv2['key']}", r["mix"].get(lv2["key"], 0)) for lv2 in levels])
            mark = f'<span class="mk">{esc(lv["mark"])}</span> ' if lv["mark"] else ""
            o(f'<li class="tile l-{esc(r["level"])}{idx}"><b>{mark}{esc(r["name"])}</b>'
              f'<span class="lvl">{esc(lv["label"])}</span>{mix}'
              f'<small>{esc(" · ".join(counts))}</small></li>\n')
        o("</ul>\n</section>\n")

    # --- the plan: legend chips filter the lanes and the not-planned list
    if plan:
        o('<div class="plan">\n<div class="chips">\n')
        for item in legend:
            k = item["class"]
            if k in chip_states:
                j = chip_states.index(k)
                o(f'<input type="checkbox" id="stf_{j}" class="stf sr">'
                  f'<label for="stf_{j}" class="chip st s-{k}">{esc(item["text"])} '
                  f'<b>{plan_counts[k]}</b></label>\n')
            else:
                o(f'<span class="chip st s-{k}">{esc(item["text"])}</span>\n')
        o("</div>\n")

        def write_box(box, rows):
            span = (f' <span class="size">{esc(say(w, "size", size=box["size"]))}</span>'
                    if box.get("size") else "")
            o(f'<article class="box">\n<h3><span>{esc(box["title"])}</span>{span}</h3>\n')
            if box.get("blurb"):
                o(f"<p>{esc(box['blurb'])}</p>\n")
            counts = state_counts([(box, rows)])
            if len(counts) > 1:
                o(state_bar(counts) + "\n")
            o('<ul class="lines">\n')
            for state, text, suffix in rows:
                sfx = f' <span class="sfx">{esc(suffix)}</span>' if suffix else ""
                o(f'<li class="ln s-{state}">{esc(text)}{sfx}</li>\n')
            o("</ul>\n</article>\n")

        lanes = [(s, boxes) for s, boxes in plan if s["kind"] != "not_planned"]
        if lanes:
            o('<div class="lanes">\n')
            for s, boxes in lanes:
                o(f'<section id="{esc(s["id"])}" class="lane {esc(s["kind"])}">\n'
                  f'<h2>{esc(s["heading"])}{note(s)}</h2>\n')
                strip = state_bar(state_counts(boxes))
                if strip:
                    o(strip + "\n")
                for box, rows in boxes:
                    write_box(box, rows)
                o("</section>\n")
            o("</div>\n")
        for s, boxes in plan:
            if s["kind"] != "not_planned":
                continue
            o(f'<section id="{esc(s["id"])}" class="np">\n<h2>{esc(s["heading"])}{note(s)}</h2>\n')
            for box, rows in boxes:
                write_box(box, rows)
            o("</section>\n")
        o("</div>\n")

    # --- shipped: a changelog by release; the board's boxes become area filters
    for si, s in enumerate(ship_secs):
        mine = [i for i, a in enumerate(areas) if a["section"] == si]
        o(f'<section id="{esc(s["id"])}" class="ship">\n<h2>{esc(s["heading"])}{note(s)}</h2>\n')
        o('<div class="chips">\n')
        for i in mine:
            box = areas[i]["box"]
            o(f'<input type="checkbox" id="arf_{i}" class="arf sr">'
              f'<label for="arf_{i}" class="chip">{esc(box["title"])} <b>{areas[i]["n"]}</b></label>\n')
        o("</div>\n")
        described = [areas[i]["box"] for i in mine
                     if areas[i]["box"].get("blurb") or areas[i]["box"].get("size")]
        if described:
            o('<dl class="notes">\n')
            for box in described:
                size = say(w, "size", size=box["size"]) if box.get("size") else ""
                text = " · ".join(t for t in (box.get("blurb") or "", size) if t)
                o(f"<dt>{esc(box['title'])}</dt><dd>{esc(text)}</dd>\n")
            o("</dl>\n")
        o('<ol class="cl">\n')
        for kind, key, group in changelog([e for e in entries if e["area"] in mine]):
            if kind == "release":
                tag = (f'<a href="https://github.com/{esc(repo)}/releases/tag/{esc(key)}">{esc(key)}</a>'
                       if repo else f"<span>{esc(key)}</span>")
                when = (f' <time datetime="{esc(lr["date"])}">{esc(lr["date"])}</time>'
                        if lr and lr["tag"] == key and lr.get("date") else "")
                o(f'<li class="rel" id="{esc(rel_id(key))}">\n<h3>{tag}{when}</h3>\n')
            elif kind == "date":
                o(f'<li class="rel">\n<h3><time datetime="{esc(key)}">{esc(key)}</time></h3>\n')
            else:
                o('<li class="rel rest">\n')
            o('<ul class="lines">\n')
            for e in group:
                title = areas[e["area"]]["box"]["title"]
                o(f'<li class="ln s-done ent ar{e["area"]}">{esc(e["text"])} '
                  f'<span class="area">{esc(title)}</span></li>\n')
            o("</ul>\n</li>\n")
        o("</ol>\n</section>\n")

    # --- background: dated lines read as a timeline, anything else as a list
    for s in background:
        o(f'<section id="{esc(s["id"])}" class="bg">\n<h2>{esc(s["heading"])}{note(s)}</h2>\n'
          '<div class="bgrid">\n')
        for box in s["boxes"]:
            o(f'<article class="box">\n<h3><span>{esc(box["title"])}</span></h3>\n')
            if box.get("blurb"):
                o(f"<p>{esc(box['blurb'])}</p>\n")
            texts = [t for t in (fill(line["text"], board) for line in box["lines"]) if t is not None]
            dated = [HISTORY_RE.match(t) for t in texts]
            if texts and all(dated):
                o('<ol class="tl">\n')
                for m in dated:
                    o(f'<li><time datetime="{esc(m.group(1))}">{esc(m.group(1))}</time>'
                      f"<span>{esc(m.group(2))}</span></li>\n")
                o("</ol>\n")
            else:
                o('<ul class="plain">\n')
                for t in texts:
                    o(f"<li>{esc(t)}</li>\n")
                o("</ul>\n")
            o("</article>\n")
        o("</div>\n</section>\n")

    o("</main>\n")
    if page.get("footer"):
        o(f'<footer>\n<div class="wrap"><p>{esc(page["footer"])}</p></div>\n</footer>\n')
    o("</body>\n</html>\n")
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
    except (KeyError, TypeError, AttributeError) as e:
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
