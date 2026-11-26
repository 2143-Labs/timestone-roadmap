#!/usr/bin/env python3
"""Build the roadmap page from the curated content and a board snapshot.

Usage: build.py [--content content/roadmap.json] [--board data/board.json] [--out site/index.html]
                [--strict-releases]

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
FORBIDDEN_RE = re.compile(r"\bSP\d|Impact|Bug census|\bP[0-2]\b|tasks\.2143\.me")
MAX_BYTES = 600_000
LEVEL_ORDER = ("crash", "desync", "playable")
RELEASE_URL = "https://github.com/tilderain/Timestone/releases/tag/"


def fail(reason):
    sys.exit(f"build failed: {reason}")


def esc(s):
    return html.escape(str(s), quote=True)


def card_flags(item, cards):
    flags = []
    for cid in item.get("cards", []):
        c = cards.get(str(cid))
        if c is None:
            fail(f"card {cid} missing from board snapshot")
        flags.append(c)
    return flags


def item_state(section_id, item, cards):
    """Return (state, suffix) for an upcoming item; first matching rule wins."""
    flags = card_flags(item, cards)
    if section_id == "not-planned":
        return "no", ""
    if flags and all(c["done"] for c in flags):
        return "done", ""
    if item.get("state") == "now":
        return "now", ""
    if item.get("state") == "verifying":
        return "verifying", ""
    # Curated items whose cards are only partly done: stay plain open instead of
    # claiming a fix is released and awaiting confirmation.
    if item.get("state") == "open":
        return "open", ""
    if item.get("shipped_in"):
        return "verifying", f" (in {item['shipped_in']}, confirming)"
    if any(not c["done"] and c["needs_repro"] for c in flags):
        return "verifying", " (fix released, confirming)"
    return ("now" if section_id == "now" else "open"), ""


def character_level(char, cfg):
    """Return (level, open, fixed, awaiting) for one character."""
    skip = set(cfg.get("skip_cards", []))
    rank = cfg["type_rank"]
    open_ranks, n_open, fixed, awaiting = set(), 0, 0, 0
    for issue in char["issues"]:
        t = issue.get("type")
        if t is not None and t not in rank:
            fail(f"unknown issue type '{t}' on card {issue['id']}")
        if issue["id"] in skip:
            continue
        if issue["done"]:
            fixed += 1
            continue
        if issue.get("needs_repro"):
            awaiting += 1
        r = rank.get(t) if t is not None else None
        if r is not None:
            n_open += 1
            open_ranks.add(r)
    if char["id"] in cfg.get("verified_ready", []):
        level = "ready"
    else:
        level = next((lv for lv in LEVEL_ORDER if lv in open_ranks), "untested")
    return level, n_open, fixed, awaiting


def fill(text, board):
    for tok in TOKEN_RE.findall(text):
        if tok != "verifying_open":
            fail(f"unknown token {{{tok}}} in '{text}'")
    return text.replace("{verifying_open}", str(board["stats"]["verifying_open"]))


def fmt_time(ts):
    # "2026-11-25T05:30Z" -> "2026-11-25 05:30 UTC"
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)Z", ts)
    if not m:
        fail(f"bad exported_at '{ts}'")
    return f"{m.group(1)} {m.group(2)} UTC"


CSS = """
*{box-sizing:border-box}
body{margin:0 auto;max-width:1300px;padding:1rem;background:#1b1d22;color:#e6e6e6;font:16px/1.5 system-ui,sans-serif}
a{color:#4aa3ff}
h1{margin:.2rem 0}
h2{grid-column:1/-1;margin:1.5rem 0 0}
h2 small{font-weight:normal;color:#9aa0a6;font-size:.8rem}
h3{margin:0 0 .4rem;font-size:1.1rem}
header p{margin:.3rem 0}
nav a{margin-right:1rem}
.stamp{color:#9aa0a6;font-size:.9rem}
.card{background:#262a31;border-radius:6px;padding:1rem}
.card p{margin:.2rem 0 .6rem;color:#c4c7cc}
.size{float:right;font-size:.75rem;font-weight:normal;color:#9aa0a6;border:1px solid #444a53;border-radius:4px;padding:0 .4rem}
.roadmap{display:grid;gap:1rem;grid-template-columns:1fr}
@media(min-width:700px){.roadmap{grid-template-columns:repeat(2,1fr)}}
@media(min-width:1100px){.roadmap{grid-template-columns:repeat(3,1fr)}}
.roadmap>section{display:contents}
.roadmap>section.wide{display:block;grid-column:1/-1}
ul{padding-left:0}
.card li,.legend li{list-style:none;margin:.25rem 0}
.card li::before,.legend li::before{content:"○ ";color:#9aa0a6}
li.done::before{content:"✔ ";color:#4caf50}
li.verifying::before{content:"◷ ";color:#f0b429}
li.now::before{content:"▶ ";color:#4aa3ff}
li.open::before{content:"○ ";color:#9aa0a6}
li.no::before{content:"✕ ";color:#e5534b}
.card li.done{text-decoration:line-through;opacity:.5}
.legend{display:flex;flex-wrap:wrap;gap:.2rem 1.5rem;font-size:.9rem}
.card ol.plain li,.card ul.plain li{list-style:revert;margin-left:1.5rem}
.card .plain li::before{content:none}
.toggle{position:absolute;opacity:0}
.toggle+label{display:inline-block;margin:.8rem 0;padding:.4rem .9rem;border:1px solid #4aa3ff;border-radius:6px;cursor:pointer;user-select:none}
#show-done:checked+label::after{content:" ✓"}
#show-done:focus-visible+label{outline:2px solid #4aa3ff}
#show-done:not(:checked)~main li.done,#show-done:not(:checked)~main .shipped{display:none}
.banner{background:#3a2f12;border-left:4px solid #f0b429;padding:.5rem .8rem;border-radius:4px}
.levels p{margin:.2rem 0}
.tiles{display:grid;gap:.5rem;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));margin-top:.8rem}
.tile{border-left:4px solid;background:#262a31;border-radius:4px;padding:.4rem .6rem;display:flex;flex-direction:column}
.tile span{font-size:.85rem}
.tile small{color:#9aa0a6;font-size:.75rem}
.ready{border-color:#4caf50}.playable{border-color:#8bc34a}.desync{border-color:#f0b429}.crash{border-color:#e5534b}.untested{border-color:#9aa0a6}
b.ready{color:#4caf50}b.playable{color:#8bc34a}b.desync{color:#f0b429}b.crash{color:#e5534b}b.untested{color:#9aa0a6}
footer{margin-top:2rem;color:#9aa0a6;font-size:.9rem}
""".strip()


def release_mentioned(content, tag):
    """True when `tag` is listed as a shipped version (or in the timeline)."""
    for g in content.get("shipped") or []:
        for it in g.get("items") or []:
            if it.get("version") == tag:
                return True
    for t in content.get("timeline") or []:
        if t.get("version") == tag or str(t.get("text") or "").startswith(tag):
            return True
    return False


def render(content, board, strict_releases=False):
    releases = set(board["releases"])
    out = []
    w = out.append
    w('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
      '<title>Timestone roadmap</title>\n<meta name="robots" content="noindex, nofollow">\n'
      f"<style>\n{CSS}\n</style>\n</head>\n<body>\n")

    lr = board["latest_release"]
    if not release_mentioned(content, lr["tag"]):
        if strict_releases:
            fail(f"latest release {lr['tag']} not mentioned in shipped")
        print(f"warn: latest release {lr['tag']} is not listed in shipped", file=sys.stderr)
    w("<header>\n")
    w(f"<h1>{esc(content['title'])}</h1>\n<p>{esc(content['tagline'])}</p>\n")
    w(f"<p><strong>{esc(content['goal'])}</strong></p>\n<p>{esc(content['notice'])}</p>\n")
    w("<nav>" + "".join(f'<a href="{esc(l["href"])}">{esc(l["label"])}</a>' for l in content["links"]) + "</nav>\n")
    w(f'<p class="stamp">Last updated {fmt_time(board["exported_at"])} · latest release '
      f'{esc(lr["tag"])} ({esc(lr["date"])})</p>\n</header>\n')
    w('<ul class="legend">' + "".join(f'<li class="{esc(l["class"])}">{esc(l["text"])}</li>'
                                      for l in content["legend"]) + "</ul>\n")

    body = []
    b = body.append
    n_done = 0
    b('<main class="roadmap">\n')
    for s in content["sections"]:
        sid = s["id"]
        note = f' <small>{esc(s["note"])}</small>' if s.get("note") else ""
        b(f'<section id="{esc(sid)}">\n<h2>{esc(s["heading"])}{note}</h2>\n')
        for g in s["groups"]:
            size = g.get("size")
            span = f' <span class="size">{esc(content["sizes"][size])} job</span>' if size else ""
            b(f'<article class="card">\n<h3>{esc(g["title"])}{span}</h3>\n')
            if g.get("blurb"):
                b(f"<p>{esc(g['blurb'])}</p>\n")
            b("<ul>\n")
            for it in g["items"]:
                state, suffix = item_state(sid, it, board["cards"])
                if it.get("shipped_in") and it["shipped_in"] not in releases:
                    fail(f"version {it['shipped_in']} not a release")
                if state == "done":
                    n_done += 1
                    if sid != "not-planned":
                        print(f"warn: item '{it['text']}' is all done; move it to shipped", file=sys.stderr)
                b(f'<li class="{state}">{esc(fill(it["text"], board) + suffix)}</li>\n')
            b("</ul>\n</article>\n")
        b("</section>\n")

    b('<section id="shipped" class="shipped">\n<h2>Shipped</h2>\n')
    for g in content["shipped"]:
        b(f'<article class="card shipped">\n<h3>{esc(g["title"])}</h3>\n<ul>\n')
        for it in g["items"]:
            v = it.get("version")
            if v:
                if v not in releases:
                    fail(f"version {v} not a release")
                tail = f' <a href="{RELEASE_URL}{esc(v)}">{esc(v)}</a>'
            elif it.get("date"):
                tail = f" ({esc(it['date'])})"
            else:
                tail = ""
            n_done += 1
            b(f'<li class="done">{esc(fill(it["text"], board))}{tail}</li>\n')
        b("</ul>\n</article>\n")
    b("</section>\n")

    ch = content["characters"]
    chars = board["characters"]
    if len(chars) != 50:
        fail(f"{len(chars)} characters, expected 50")
    levels = {lv["key"]: lv for lv in ch["levels"]}
    rows = []
    for c in sorted(chars, key=lambda c: c["name"].lower()):
        level, n_open, fixed, awaiting = character_level(c, ch)
        rows.append((c["name"], level, n_open, fixed, awaiting))
    counts = {k: sum(1 for r in rows if r[1] == k) for k in levels}
    b(f'<section id="characters" class="wide">\n<h2>{esc(ch["heading"])}</h2>\n')
    b(f'<p class="banner">{esc(ch["banner"])}</p>\n<div class="levels">\n')
    for lv in ch["levels"]:
        b(f'<p><b class="{esc(lv["key"])}">{esc(lv["mark"])}</b> {esc(lv["label"])}: '
          f'{counts[lv["key"]]} · {esc(lv["text"])}</p>\n')
    b('</div>\n<div class="tiles">\n')
    for name, level, n_open, fixed, awaiting in rows:
        lv = levels[level]
        extra = f" · {awaiting} awaiting confirmation" if awaiting else ""
        b(f'<div class="tile {level}"><b>{esc(lv["mark"])} {esc(name)}</b>'
          f'<span>{esc(lv["label"])}</span><small>{n_open} open · {fixed} fixed{extra}</small></div>\n')
    b("</div>\n</section>\n")

    b('<section id="history" class="wide">\n<h2>Background</h2>\n')
    b('<article class="card">\n<h3>How we got here</h3>\n<ol class="plain">\n')
    for t in content["timeline"]:
        b(f"<li>{esc(t['date'])}: {esc(t['text'])}</li>\n")
    b('</ol>\n</article>\n<article class="card">\n<h3>Changed or removed</h3>\n<ul class="plain">\n')
    for c in content["caveats"]:
        b(f"<li>{esc(c)}</li>\n")
    b("</ul>\n</article>\n</section>\n</main>\n")

    w('<input type="checkbox" id="show-done" class="toggle">\n'
      f'<label for="show-done">Show shipped work ({n_done})</label>\n')
    out.extend(body)
    w(f"<footer><p>{esc(content['footer'])}</p></footer>\n</body>\n</html>\n")
    page = "".join(out)

    m = FORBIDDEN_RE.search(page)
    if m:
        fail(f"forbidden text '{m.group(0)}' near '{page[max(0, m.start() - 40):m.end() + 40]}'")
    if len(page.encode()) >= MAX_BYTES:
        fail(f"page is {len(page.encode())} bytes")
    n_tiles = page.count('class="tile ')
    if n_tiles != 50:
        fail(f"{n_tiles} tiles")
    return page


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the roadmap page.")
    ap.add_argument("--content", default="content/roadmap.json")
    ap.add_argument("--board", default="data/board.json")
    ap.add_argument("--out", default="site/index.html")
    ap.add_argument("--strict-releases", action="store_true",
                    help="fail when the latest release is not listed in the shipped section")
    a = ap.parse_args(argv)
    try:
        with open(a.content) as f:
            content = json.load(f)
        with open(a.board) as f:
            board = json.load(f)
    except (OSError, ValueError) as e:
        fail(str(e))
    try:
        page = render(content, board, a.strict_releases)
        levels = [character_level(c, content["characters"])[0] for c in board["characters"]]
    except (KeyError, TypeError) as e:
        fail(f"malformed input: {e!r}")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, a.out)
    n_up = sum(len(g["items"]) for s in content["sections"] for g in s["groups"])
    n_sh = sum(len(g["items"]) for g in content["shipped"])
    print(f"ok: {n_up} upcoming, {n_sh} shipped, characters "
          + " ".join(f"{k}={levels.count(k)}" for k in ("crash", "desync", "playable", "untested", "ready")))


if __name__ == "__main__":
    main()
