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
    page = board["page"]
    out = []
    w = out.append
    w('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
      '<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    w(f'<title>{esc(page["title"])}</title>\n<meta name="robots" content="noindex, nofollow">\n'
      f"<style>\n{CSS}\n</style>\n</head>\n<body>\n")

    lr = board.get("latest_release")
    if lr and not release_mentioned(board, lr["tag"]):
        if strict_releases:
            fail(f"latest release {lr['tag']} not mentioned in shipped")
        print(f"warn: latest release {lr['tag']} is not listed in shipped", file=sys.stderr)
    w("<header>\n")
    w(f'<h1>{esc(page["title"])}</h1>\n')
    if page.get("tagline"):
        w(f'<p>{esc(page["tagline"])}</p>\n')
    if page.get("goal"):
        w(f'<p><strong>{esc(page["goal"])}</strong></p>\n')
    if page.get("notice"):
        w(f'<p>{esc(page["notice"])}</p>\n')
    if page.get("links"):
        w("<nav>" + "".join(f'<a href="{esc(l["href"])}">{esc(l["label"])}</a>'
                            for l in page["links"]) + "</nav>\n")
    stamp = f'<p class="stamp">Last updated {fmt_time(board["exported_at"])}'
    if lr:
        stamp += f' · latest release {esc(lr["tag"])} ({esc(lr["date"])})'
    w(stamp + "</p>\n</header>\n")
    w('<ul class="legend">' + "".join(f'<li class="{esc(l["class"])}">{esc(l["text"])}</li>'
                                      for l in cfg["legend"]) + "</ul>\n")

    sections = board["sections"]
    body = []
    b = body.append
    n_done = 0
    b('<main class="roadmap">\n')
    # The characters grid opens the page: it is the one block the board's
    # column order does not place, so it sits here on purpose.
    levels = board.get("levels") or []
    chars = board.get("characters") or []
    types = board.get("types") or {}
    if levels and chars:
        by_key = {lv["key"]: lv for lv in levels}
        rows = []
        for c in sorted(chars, key=lambda c: c["name"].lower()):
            level, n_open, fixed, awaiting = character_level(c, types)
            lv = by_key.get(level)
            if lv is None:
                print(f"warn: level '{level}' has no card; tile rendered without a mark",
                      file=sys.stderr)
                lv = {"key": level, "mark": "", "label": level, "text": ""}
            rows.append((c["name"], level, n_open, fixed, awaiting, lv))
        counts = {lv["key"]: sum(1 for r in rows if r[1] == lv["key"]) for lv in levels}
        b(f'<section id="characters" class="wide">\n<h2>{esc(page["characters_heading"])}</h2>\n')
        if page.get("characters_banner"):
            b(f'<p class="banner">{esc(page["characters_banner"])}</p>\n')
        b('<div class="levels">\n')
        for lv in levels:
            b(f'<p><b class="{esc(lv["key"])}">{esc(lv["mark"])}</b> {esc(lv["label"])}: '
              f'{counts[lv["key"]]} · {esc(lv["text"])}</p>\n')
        b('</div>\n<div class="tiles">\n')
        for name, level, n_open, fixed, awaiting, lv in rows:
            extra = f" · {awaiting} awaiting confirmation" if awaiting else ""
            b(f'<div class="tile {level}"><b>{esc(lv["mark"])} {esc(name)}</b>'
              f'<span>{esc(lv["label"])}</span><small>{n_open} open · {fixed} fixed{extra}</small></div>\n')
        b("</div>\n</section>\n")

    def box_head(box, extra="card"):
        span = f' <span class="size">{esc(box["size"])} job</span>' if box.get("size") else ""
        b(f'<article class="{extra}">\n<h3>{esc(box["title"])}{span}</h3>\n')
        if box.get("blurb"):
            b(f"<p>{esc(box['blurb'])}</p>\n")

    for s in sections:
        if s["kind"] not in ("active", "upcoming", "not_planned"):
            continue
        note = f' <small>{esc(s["note"])}</small>' if s.get("note") else ""
        b(f'<section id="{esc(s["id"])}">\n<h2>{esc(s["heading"])}{note}</h2>\n')
        for box in s["boxes"]:
            box_head(box)
            b("<ul>\n")
            for line in box["lines"]:
                state, suffix = line_state(s["kind"], line, releases)
                text = fill(line["text"], board)
                if text is None:
                    continue
                if state == "done":
                    n_done += 1
                    print(f"warn: line {line['id']} is all done; move it to shipped", file=sys.stderr)
                b(f'<li class="{state}">{esc(text + suffix)}</li>\n')
            b("</ul>\n</article>\n")
        b("</section>\n")

    for s in sections:
        if s["kind"] != "shipped":
            continue
        note = f' <small>{esc(s["note"])}</small>' if s.get("note") else ""
        b(f'<section id="shipped" class="shipped">\n<h2>{esc(s["heading"])}{note}</h2>\n')
        for box in s["boxes"]:
            box_head(box, "card shipped")
            b("<ul>\n")
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is None:
                    continue
                if line.get("release"):
                    tag = line["release"]
                    tail = (f' <a href="https://github.com/{esc(repo)}/releases/tag/{esc(tag)}">'
                            f"{esc(tag)}</a>")
                elif line.get("date"):
                    tail = f" ({esc(line['date'])})"
                else:
                    tail = ""
                n_done += 1
                b(f'<li class="done">{esc(text)}{tail}</li>\n')
            b("</ul>\n</article>\n")
        b("</section>\n")


    for s in sections:
        if s["kind"] != "background":
            continue
        b(f'<section id="history" class="wide">\n<h2>{esc(s["heading"])}</h2>\n')
        for box in s["boxes"]:
            b(f'<article class="card">\n<h3>{esc(box["title"])}</h3>\n')
            if box.get("blurb"):
                b(f"<p>{esc(box['blurb'])}</p>\n")
            b('<ul class="plain">\n')
            for line in box["lines"]:
                text = fill(line["text"], board)
                if text is None:
                    continue
                b(f"<li>{esc(text)}</li>\n")
            b("</ul>\n</article>\n")
        b("</section>\n")

    b("</main>\n")
    w('<input type="checkbox" id="show-done" class="toggle">\n'
      f'<label for="show-done">Show shipped work ({n_done})</label>\n')
    out.extend(body)
    if page.get("footer"):
        w(f'<footer><p>{esc(page["footer"])}</p></footer>\n')
    w("</body>\n</html>\n")
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
