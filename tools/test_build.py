import contextlib
import html.parser
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402
import export_board  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOX, LINE, CONF, NCONF = 100, 101, 102, 103
SIZES = {110: "small", 111: "medium", 112: "large"}
BUCKETS = {"draft": 1, "active": 2, "not_planned": 3, "shipped": 4, "background": 5, "page": 6}
BUCKET_TITLES = {2: "Now (Currently working on this)", 3: "Not planned (Asked for)", 4: "Shipped",
                 5: "Background", 6: "Page text", 1: "Draft", 7: "Next", 8: "Later"}
BUCKET_POS = {2: 200, 7: 300, 8: 400, 3: 500, 4: 600, 5: 700, 6: 800, 1: 100}

WORDS = {"stamp": "Last updated {time} UTC", "latest": "latest release {tag} ({date})",
         "counts": "{open} open · {fixed} fixed", "awaiting": "{n} awaiting confirmation",
         "size": "{size} job", "in_release": "(in {tag}, confirming)",
         "fix_released": "(fix released, confirming)"}
TEAM = {"size": 3, "worst_first": ["crash", "desync", "untested", "playable", "ready"]}

CFG = {
    "project": 19,
    "min_tasks": 0,
    "views": {"boxes": 10, "lines": 11},
    "buckets": BUCKETS,
    "labels": {"box": BOX, "line": LINE, "confirming": CONF, "not_confirming": NCONF,
               "needs_repro": 17, "sizes": {str(k): v for k, v in SIZES.items()}},
    "releases": {"repo": "o/r", "card_prefix": "Release "},
    "forbidden": "\\bSP\\d|Impact|Bug census|\\bP[0-2]\\b|tasks\\.2143\\.me",
    "legend": [{"class": "now", "text": "now"}],
    "words": WORDS,
    "team": TEAM,
}

CHAR, READY, IGNORE = 120, 121, 122
LEVELS = [{"key": "ready", "mark": "●", "label": "Tournament-ready", "text": "clean"},
          {"key": "playable", "mark": "◐", "label": "Playable", "text": "visual"},
          {"key": "desync", "mark": "▲", "label": "Known desyncs", "text": "desync"},
          {"key": "crash", "mark": "■", "label": "Can crash", "text": "crash"},
          {"key": "untested", "mark": "○", "label": "Untested", "text": "none"}]
TYPES = {"crash": "crash", "freeze": "crash", "desync": "desync", "effect": "playable",
         "verify": None}
LABEL_TITLES = {BOX: "Public: box", LINE: "Public: line", CHAR: "Public: character",
                READY: "Public: ready", IGNORE: "Public: ignore"}


# --- board dump builders ---------------------------------------------------

def task(tid, title, done=False, labels=(), desc="", end=None, rel=None):
    return {"id": tid, "title": title, "done": done,
            "labels": [{"id": i, "title": LABEL_TITLES.get(i)} for i in labels] if labels else None,
            "description": desc, "end_date": end, "related_tasks": rel or {}}


def rel(**kw):
    return {k: [{"id": i} for i in v] for k, v in kw.items()}


def box_item(tid, position, bucket, view=10):
    return {"id": tid, "position": position,
            "buckets": [{"id": bucket, "title": BUCKET_TITLES[bucket], "position": BUCKET_POS[bucket],
                         "project_view_id": view}]}


def line_item(lid, position):
    return {"id": lid, "position": position}


def dump(tasks, box_view, line_view=None):
    return {"tasks": tasks, "box_view": box_view, "line_view": line_view or []}


def small_board(with_title=True):
    """Two boxes in Now: one with two lines, one with a release-named line."""
    cfg = dict(CFG)
    tasks = [
        task(1, "Box A", labels=[BOX, 112], desc="blurb A", rel=rel(subtask=[2, 3])),
        task(2, "line one", labels=[LINE], rel=rel(parenttask=[1], related=[900])),
        task(3, "line two", labels=[LINE], rel=rel(parenttask=[1])),
        task(4, "Box B", labels=[BOX], desc="blurb B", rel=rel(subtask=[5])),
        task(5, "shipped line", labels=[LINE], rel=rel(parenttask=[4], related=[950])),
        task(900, "Bug card", desc=""),
        task(950, "Release v0.6.9", done=True),
    ]
    box_view = [box_item(4, 200, 2), box_item(1, 100, 2)]
    if with_title:
        tasks.append(task(30, "Title", labels=[BOX], desc="Roadmap"))
        box_view.append(box_item(30, 900, 6))
    data = dump(tasks, box_view, [line_item(2, 20), line_item(3, 10), line_item(5, 5)])
    return cfg, data


class LineState(unittest.TestCase):
    def line(self, **kw):
        base = {"id": 1, "text": "x", "done": False, "confirming": False, "not_confirming": False,
                "release": None, "date": None, "cards": []}
        base.update(kw)
        return base

    def state(self, kind, line, releases):
        return build.line_state(kind, line, releases, WORDS)

    def test_rule1_not_planned_is_no(self):
        self.assertEqual(self.state("not_planned", self.line(done=True), set()), ("no", ""))

    def test_rule2_done_by_flag(self):
        self.assertEqual(self.state("upcoming", self.line(done=True), set()), ("done", ""))

    def test_rule2_done_when_all_cards_done(self):
        st = self.state("active", self.line(cards=[{"done": True, "needs_repro": False}]), set())
        self.assertEqual(st, ("done", ""))

    def test_rule3_confirming_has_no_suffix(self):
        self.assertEqual(self.state("upcoming", self.line(confirming=True), set()),
                         ("verifying", ""))

    def test_rule4_not_confirming_beats_needs_repro_active(self):
        st = self.state("active", self.line(not_confirming=True,
                                                  cards=[{"done": False, "needs_repro": True}]), set())
        self.assertEqual(st, ("now", ""))

    def test_rule4_not_confirming_beats_needs_repro_upcoming(self):
        st = self.state("upcoming", self.line(not_confirming=True,
                                                    cards=[{"done": False, "needs_repro": True}]), set())
        self.assertEqual(st, ("open", ""))

    def test_rule5_release_in_list(self):
        st = self.state("upcoming", self.line(release="v1"), {"v1"})
        self.assertEqual(st, ("verifying", " (in v1, confirming)"))

    def test_rule5_unknown_release_warns_and_falls_through(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = self.state("upcoming", self.line(release="v9"), {"v1"})
        self.assertEqual(st, ("open", ""))
        self.assertIn("warn:", err.getvalue())

    def test_rule5_unknown_release_falls_through_to_needs_repro(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = self.state("upcoming", self.line(release="v9",
                                                        cards=[{"done": False, "needs_repro": True}]), {"v1"})
        self.assertEqual(st, ("verifying", " (fix released, confirming)"))

    def test_rule6_needs_repro(self):
        st = self.state("upcoming", self.line(cards=[{"done": False, "needs_repro": True}]), set())
        self.assertEqual(st, ("verifying", " (fix released, confirming)"))

    def test_suffix_words_come_from_the_config(self):
        words = dict(WORDS, in_release="[{tag}]", fix_released="[?]")
        self.assertEqual(build.line_state("upcoming", self.line(release="v1"), {"v1"}, words),
                         ("verifying", " [v1]"))
        st = build.line_state("upcoming", self.line(cards=[{"done": False, "needs_repro": True}]),
                              set(), words)
        self.assertEqual(st, ("verifying", " [?]"))

    def test_rule7_fallback(self):
        self.assertEqual(self.state("active", self.line(), set()), ("now", ""))
        self.assertEqual(self.state("upcoming", self.line(), set()), ("open", ""))
        self.assertEqual(self.state("upcoming", self.line(cards=[{"done": True,
                                                                       "needs_repro": False},
                                                                      {"done": False,
                                                                       "needs_repro": False}]), set()),
                         ("open", ""))


class CharacterLevel(unittest.TestCase):
    def char(self, issues, ready=False, cid=1):
        return {"id": cid, "name": "C", "ready": ready, "issues": issues}

    def issue(self, iid, t, done=False, nr=False):
        return {"id": iid, "type": t, "done": done, "needs_repro": nr}

    def test_freeze_is_crash(self):
        c = self.char([self.issue(1, "freeze"), self.issue(2, "effect")])
        self.assertEqual(build.character_level(c, TYPES)[0], "crash")

    def test_effect_only_is_playable(self):
        self.assertEqual(build.character_level(self.char([self.issue(1, "effect")]), TYPES)[0],
                         "playable")

    def test_verify_and_done_is_untested(self):
        c = self.char([self.issue(2, "verify"), self.issue(3, "crash", done=True)])
        self.assertEqual(build.character_level(c, TYPES), ("untested", 0, 1, 0))

    def test_unknown_type_is_uncounted(self):
        self.assertEqual(build.character_level(self.char([self.issue(1, "weird")]), TYPES),
                         ("untested", 0, 0, 0))

    def test_ready_flag(self):
        self.assertEqual(build.character_level(self.char([self.issue(1, "crash")], ready=True),
                                               TYPES)[0], "ready")

    def test_open_mix_is_worst_first_and_sums_to_open(self):
        c = self.char([self.issue(1, "effect"), self.issue(2, "freeze"), self.issue(3, "crash"),
                       self.issue(4, "desync"), self.issue(5, "verify"), self.issue(6, "weird"),
                       self.issue(7, "crash", done=True)])
        mix = build.open_mix(c, TYPES)
        self.assertEqual(mix, [("crash", 2), ("desync", 1), ("playable", 1)])
        self.assertEqual(sum(n for _, n in mix), build.character_level(c, TYPES)[1])

    def test_slugs_are_url_safe_and_unique(self):
        used = set()
        got = [build.slug(n, used) for n in ("C. Viper", "C Viper", "M.O.D.O.K.", "X-23", "!!!", "???")]
        self.assertEqual(got, ["c-viper", "c-viper-2", "m-o-d-o-k", "x-23", "x", "x-2"])


# --- render ----------------------------------------------------------------

def snapshot():
    return {
        "schema": 2,
        "exported_at": "2026-11-25T05:30Z",
        "latest_release": {"tag": "v1", "date": "2026-11-24"},
        "releases": ["v1"],
        "stats": {"verifying_open": 7},
        "page": {"title": "T", "tagline": "tag", "goal": "goal", "notice": "notice", "footer": "f",
                 "characters_heading": "Characters", "characters_banner": "banner",
                 "links": [{"label": "Discord", "href": "https://example.invalid/"}]},
        "sections": [
            {"id": "now", "heading": "Now", "note": "(n)", "kind": "active", "boxes": [
                {"id": 1, "title": "G", "blurb": "b", "size": "small", "lines": [
                    {"id": 11, "text": "{verifying_open} awaiting", "done": False, "confirming": True,
                     "not_confirming": False, "release": None, "date": None, "cards": []},
                    {"id": 12, "text": "a", "done": False, "confirming": False,
                     "not_confirming": False, "release": None, "date": None, "cards": []}]}]},
            {"id": "not-planned", "heading": "No", "note": None, "kind": "not_planned", "boxes": [
                {"id": 2, "title": "No", "blurb": None, "size": None, "lines": [
                    {"id": 13, "text": "x", "done": True, "confirming": False, "not_confirming": False,
                     "release": None, "date": None, "cards": []}]}]},
            {"id": "shipped", "heading": "Shipped", "note": None, "kind": "shipped", "boxes": [
                {"id": 3, "title": "S", "blurb": None, "size": None, "lines": [
                    {"id": 14, "text": "s", "done": True, "confirming": False, "not_confirming": False,
                     "release": "v1", "date": None, "cards": []},
                    {"id": 15, "text": "d", "done": True, "confirming": False, "not_confirming": False,
                     "release": None, "date": "2026-01-01", "cards": []}]}]},
            {"id": "history", "heading": "Background", "note": None, "kind": "background", "boxes": [
                {"id": 4, "title": "How we got here", "blurb": None, "size": None, "lines": [
                    {"id": 16, "text": "2026-01-01: t", "done": False, "confirming": False,
                     "not_confirming": False, "release": None, "date": None, "cards": []}]}]},
        ],
        "characters": [{"id": 900 + i, "name": f"C{i:02d}", "ready": False,
                        "issues": [{"id": 1000 + i, "done": False, "type": "effect",
                                    "needs_repro": False}]} for i in range(2)],
        "levels": LEVELS,
        "types": TYPES,
    }


def render_cfg():
    return dict(CFG)


def quiet(fn, *a, **kw):
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


FULL_LEGEND = [{"class": c, "text": t} for c, t in
               (("now", "doing"), ("open", "planned"), ("verifying", "checking"),
                ("done", "shipped"), ("no", "never"))]


def line(lid, text, **kw):
    base = {"id": lid, "text": text, "done": False, "confirming": False, "not_confirming": False,
            "release": None, "date": None, "cards": []}
    base.update(kw)
    return base


class _Text(html.parser.HTMLParser):
    """What a visitor can read: text outside <style>/<script>, plus the labelling attributes."""

    def __init__(self):
        super().__init__()
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        self.parts += [v for k, v in attrs if k in ("aria-label", "placeholder", "title", "alt") and v]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible(page):
    p = _Text()
    p.feed(page)
    p.close()
    return " ".join(" ".join(p.parts).split())


def script(page):
    return page[page.index("<script>"):page.index("</script>")]


class Render(unittest.TestCase):
    def test_deterministic(self):
        cfg, board = render_cfg(), snapshot()
        self.assertEqual(build.render(cfg, board), build.render(cfg, board))

    def test_markup_and_counts(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('<span class="size">small job</span>', page)
        self.assertIn('<li class="verifying">7 awaiting</li>', page)
        self.assertIn('<li class="no">x</li>', page)
        self.assertIn('<h2>Now <small>(n)</small></h2>', page)
        self.assertIn('<li class="done" data-rel="v1">s '
                      '<a href="https://github.com/o/r/releases/tag/v1">v1</a></li>', page)
        self.assertIn('<li class="done">d (2026-01-01)</li>', page)
        self.assertIn('<p class="stamp">Last updated 2026-11-25 05:30 UTC · latest release '
                      '<a href="#shipped" data-rel="v1">v1</a> (2026-11-24)</p>', page)
        self.assertEqual(page.count('class="tile '), 2)
        self.assertIn('<p class="banner">banner</p>', page)
        self.assertIn('<p class="goal"><strong>goal</strong></p>', page)
        self.assertIn('<p class="notice">notice</p>', page)
        self.assertIn('<nav class="links"><a href="https://example.invalid/">Discord</a></nav>', page)
        self.assertIn("<footer><p>f</p></footer>", page)
        # body order: characters, lanes, shipped, background
        self.assertLess(page.index('id="characters"'), page.index('id="now"'))
        self.assertLess(page.index('id="now"'), page.index('id="shipped"'))
        self.assertLess(page.index('id="shipped"'), page.index('id="history"'))

    def test_unknown_token_drops_the_line(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][0]["boxes"][0]["lines"][1]["text"] = "{secret} kept"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        self.assertNotIn("{secret}", page)
        self.assertNotIn("kept", page)
        self.assertIn("warn:", err.getvalue())
        # the dropped line is not counted either
        self.assertIn('<a href="#now">Now <b>1</b></a>', page)

    def test_forbidden_text_fails(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][3]["boxes"][0]["lines"][0]["text"] = "Impact: high"
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board)

    def test_latest_release_warns_by_default(self):
        cfg, board = render_cfg(), snapshot()
        board["latest_release"] = {"tag": "v2", "date": "2026-11-25"}
        board["releases"] = ["v1", "v2"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        self.assertIn("warn: latest release v2 is not listed in shipped", err.getvalue())
        # no shipped line carries the tag, so there is nothing to jump to
        self.assertIn("latest release v2 (2026-11-25)</p>", page)

    def test_latest_release_strict_fails(self):
        cfg, board = render_cfg(), snapshot()
        board["latest_release"] = {"tag": "v2", "date": "2026-11-25"}
        board["releases"] = ["v1", "v2"]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board, True)

    def test_latest_release_in_background_is_enough(self):
        cfg, board = render_cfg(), snapshot()
        board["latest_release"] = {"tag": "v2", "date": "2026-11-25"}
        board["releases"] = ["v1", "v2"]
        board["sections"][3]["boxes"][0]["lines"].append(line(17, "2026-11-25: v2: something"))
        quiet(build.render, cfg, board, True)
        board["sections"][3]["boxes"][0]["lines"][-1]["text"] = "2026-11-25: v20: something"
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board, True)

    def test_optional_page_keys_absent(self):
        cfg, board = render_cfg(), snapshot()
        for k in ("tagline", "goal", "notice", "footer", "links", "characters_banner"):
            board["page"].pop(k)
        page = quiet(build.render, cfg, board)
        self.assertNotIn('class="links"', page)
        self.assertNotIn("<footer>", page)
        self.assertNotIn("<strong>", page)
        self.assertNotIn('class="banner"', page)
        self.assertNotIn('class="notice"', page)
        self.assertNotIn("<p>tag</p>", page)

    def test_latest_release_null_omits_stamp_part(self):
        cfg, board = render_cfg(), snapshot()
        board["latest_release"] = None
        board["releases"] = []
        page = quiet(build.render, cfg, board)
        self.assertIn("Last updated 2026-11-25 05:30 UTC</p>", page)
        self.assertNotIn("latest release", page)

    def test_no_characters_block(self):
        cfg, board = CFG, snapshot()
        board.pop("characters")
        page = quiet(build.render, cfg, board)
        self.assertEqual(page.count('class="tile '), 0)
        self.assertNotIn('id="characters"', page)
        self.assertNotIn('href="#characters"', page)
        self.assertNotIn('class="banner"', page)
        self.assertNotIn('id="find"', page)
        self.assertNotIn('id="team"', page)

    def test_schema_must_be_2(self):
        cfg, board = render_cfg(), snapshot()
        board["schema"] = 1
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board)


class JumpBar(unittest.TestCase):
    def test_every_section_heading_is_a_link_in_page_order(self):
        page = quiet(build.render, render_cfg(), snapshot())
        # named by the page title; the space keeps "Now 2" two words for a screen reader
        self.assertIn('<nav class="jump" aria-label="T"><a href="#characters">Characters <b>2</b></a>'
                      '<a href="#now">Now <b>2</b></a><a href="#not-planned">No <b>1</b></a>'
                      '<a href="#shipped">Shipped <b>2</b></a><a href="#history">Background</a></nav>',
                      page)

    def test_every_link_has_its_target(self):
        page = quiet(build.render, render_cfg(), snapshot())
        for anchor in re.findall(r'href="#([^"]+)"', page):
            self.assertEqual(page.count(f'id="{anchor}"'), 1, anchor)


class Lanes(unittest.TestCase):
    def test_road_sections_are_lanes_in_board_order(self):
        board = snapshot()
        board["sections"].insert(1, {"id": "next", "heading": "Next", "note": None, "kind": "upcoming",
                                     "boxes": [{"id": 5, "title": "N", "blurb": None, "size": None,
                                                "lines": [line(18, "later")]}]})
        page = quiet(build.render, render_cfg(), board)
        lanes = page[page.index('<div class="lanes">'):page.index('id="shipped"')]
        self.assertEqual(re.findall(r'<section id="([^"]+)" class="lane k-([a-z_]+)">', lanes),
                         [("now", "active"), ("next", "upcoming"), ("not-planned", "not_planned")])
        self.assertIn('<li class="open">later</li>', lanes)
        self.assertNotIn('class="lane', page[page.index('id="shipped"'):])

    def test_legend_is_the_line_filter_and_counts_every_state(self):
        cfg = dict(render_cfg(), legend=FULL_LEGEND)
        page = quiet(build.render, cfg, snapshot())
        legend = page[page.index('<div class="legend">'):page.index('<div class="lanes">')]
        chips = re.findall(r'<button type="button" class="chip (\w+)" data-f="state" value="(\w+)" '
                           r'aria-pressed="false"><span>([^<]+)</span> <b>(\d+)</b></button>',
                           legend)
        # a state no line is in still reads in full: its row is content, not only a control
        self.assertEqual(chips, [("now", "now", "doing", "1"),
                                 ("open", "open", "planned", "0"),
                                 ("verifying", "verifying", "checking", "1"),
                                 ("done", "done", "shipped", "2"),
                                 ("no", "no", "never", "1")])
        self.assertNotIn("disabled", legend)
        # every chip value is a class the lines carry, so the filter has something to match
        for cls in ("now", "verifying", "done", "no"):
            self.assertIn(f'<li class="{cls}"', page)

    def test_done_line_outside_shipped_counts_as_done_and_warns(self):
        cfg, board = dict(render_cfg(), legend=FULL_LEGEND), snapshot()
        board["sections"][0]["boxes"][0]["lines"][1]["done"] = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        self.assertIn("warn: line 12 is all done; move it to shipped", err.getvalue())
        self.assertIn('<li class="done">a</li>', page)
        self.assertIn('value="done" aria-pressed="false"><span>shipped</span> <b>3</b>', page)


class Shipped(unittest.TestCase):
    def board(self):
        board = snapshot()
        board["releases"] = ["v0.6.10", "v0.6.9", "v0.4.0", "v1"]
        board["sections"][2]["boxes"].append(
            {"id": 6, "title": "More", "blurb": None, "size": None, "lines": [
                line(20, "nine", done=True, release="v0.6.9"),
                line(21, "ten", done=True, release="v0.6.10"),
                line(22, "ten again", done=True, release="v0.6.10"),
                line(23, "four", done=True, release="v0.4.0"),
                line(24, "undated", done=True)]})
        return board

    def test_release_chips_newest_first_with_counts(self):
        page = quiet(build.render, render_cfg(), self.board())
        rels = page[page.index('<div class="rels">'):page.index('<div class="boxes">')]
        self.assertEqual(re.findall(r'data-f="rel" value="([^"]+)" aria-pressed="false">'
                                    r'<span>([^<]+)</span> <b>(\d+)</b>', rels),
                         [("v1", "v1", "1"), ("v0.6.10", "v0.6.10", "2"), ("v0.6.9", "v0.6.9", "1"),
                          ("v0.4.0", "v0.4.0", "1")])
        # a release sorts above its own pre-releases, and both above an older release
        tags = ["v0.2.7-dev-pr1", "v0.2.7", "v0.2.4-dev-pr1", "v0.6.10", "v0.6.9", "nightly"]
        self.assertEqual(sorted(tags, key=build.version_key, reverse=True),
                         ["v0.6.10", "v0.6.9", "v0.2.7", "v0.2.7-dev-pr1", "v0.2.4-dev-pr1", "nightly"])
        # each chip matches the data-rel of the lines it filters
        self.assertEqual(page.count('<li class="done" data-rel="v0.6.10">'), 2)
        self.assertIn('<li class="done">undated</li>', page)
        self.assertIn('<a href="#shipped">Shipped <b>7</b></a>', page)

    def test_no_release_chips_without_release_lines(self):
        board = snapshot()
        board["sections"][2]["boxes"][0]["lines"][0]["release"] = None
        board["sections"].pop(3)
        board["latest_release"] = None
        page = quiet(build.render, render_cfg(), board)
        self.assertNotIn('class="rels"', page)
        self.assertNotIn('data-f="rel"', page)

    def test_release_without_a_repo_is_plain_text(self):
        cfg = render_cfg()
        cfg.pop("releases")
        page = quiet(build.render, cfg, snapshot())
        self.assertIn('<li class="done" data-rel="v1">s v1</li>', page)
        self.assertNotIn("github.com", page)


class Background(unittest.TestCase):
    def test_dated_box_is_a_timeline_and_others_stay_plain(self):
        board = snapshot()
        board["sections"][3]["boxes"].append(
            {"id": 7, "title": "Changed", "blurb": "why", "size": None,
             "lines": [line(30, "2026-01-01: dated"), line(31, "not dated")]})
        page = quiet(build.render, render_cfg(), board)
        self.assertIn('<ol class="timeline">\n<li><time datetime="2026-01-01">2026-01-01</time> '
                      "<span>t</span></li>\n</ol>", page)
        self.assertIn('<h3>Changed</h3>\n<p>why</p>\n<ul class="plain">\n<li>2026-01-01: dated</li>\n'
                      "<li>not dated</li>\n</ul>", page)
        self.assertEqual(page.count('<ol class="timeline">'), 1)


class Roster(unittest.TestCase):
    def board(self):
        board = snapshot()
        board["characters"] = [
            {"id": 901, "name": "Zed", "ready": False, "issues": [
                {"id": 1, "done": False, "type": "crash", "needs_repro": True},
                {"id": 2, "done": False, "type": "effect", "needs_repro": False},
                {"id": 3, "done": True, "type": "desync", "needs_repro": False}]},
            {"id": 902, "name": "C. Viper", "ready": False, "issues": []},
            {"id": 903, "name": "Abe", "ready": True, "issues": [
                {"id": 4, "done": False, "type": "desync", "needs_repro": False}]}]
        return board

    def test_tiles_are_pickable_and_carry_today_s_counts(self):
        page = quiet(build.render, render_cfg(), self.board())
        tiles = re.findall(r'<button type="button" class="tile l-(\w+)" data-level="(\w+)" '
                           r'data-name="([^"]+)" value="([^"]+)" aria-pressed="false">', page)
        self.assertEqual(tiles, [("ready", "ready", "abe", "abe"),
                                 ("untested", "untested", "c. viper cviper", "c-viper"),
                                 ("crash", "crash", "zed", "zed")])
        self.assertIn("<b><i>■</i> Zed</b><span>Can crash</span>"
                      "<small>2 open · 1 fixed · 1 awaiting confirmation</small>", page)
        self.assertIn("<b><i>○</i> C. Viper</b><span>Untested</span><small>0 open · 0 fixed</small>",
                      page)
        # the bar under a tile splits its open issues by level, worst first
        self.assertIn('style="width:100%"><i class="l-crash" style="flex:1"></i>'
                      '<i class="l-playable" style="flex:1"></i></span>', page)
        self.assertIn('style="width:0%"></span>', page)

    def test_levels_are_the_roster_filter_and_the_bar(self):
        page = quiet(build.render, render_cfg(), self.board())
        chips = re.findall(r'<button type="button" class="lv l-(\w+)" data-f="level" value="(\w+)" '
                           r'aria-pressed="false"><b class="m">(.)</b> '
                           r'<span class="n">([^<]+)</span> <em>(\d+)</em> '
                           r'<small class="t">([^<]+)</small></button>', page)
        # a level nobody is at keeps its row, undimmed and reachable by keyboard
        self.assertEqual(chips, [("ready", "ready", "●", "Tournament-ready", "1", "clean"),
                                 ("playable", "playable", "◐", "Playable", "0", "visual"),
                                 ("desync", "desync", "▲", "Known desyncs", "0", "desync"),
                                 ("crash", "crash", "■", "Can crash", "1", "crash"),
                                 ("untested", "untested", "○", "Untested", "1", "none")])
        self.assertNotIn(" disabled", page)
        self.assertIn('<div class="meter" aria-hidden="true"><i class="l-ready" style="flex:1"></i>'
                      '<i class="l-crash" style="flex:1"></i><i class="l-untested" style="flex:1"></i>'
                      "</div>", page)
        # the search box is named by the board's own heading
        self.assertIn('<input type="search" id="find" aria-label="Characters" '
                      'placeholder="Characters"', page)

    def test_team_tray_comes_from_the_config(self):
        page = quiet(build.render, render_cfg(), self.board())
        self.assertIn('<div class="team" id="team" data-size="3" '
                      'data-rank="crash desync untested playable ready">', page)
        # every level a tile can have is a filter value, and every ranked level is known
        self.assertLessEqual(set(re.findall(r'class="tile [^"]*" data-level="(\w+)"', page)),
                             set(re.findall(r'data-f="level" value="(\w+)"', page)))

    def test_no_team_in_config_leaves_plain_tiles(self):
        cfg = render_cfg()
        cfg.pop("team")
        page = quiet(build.render, cfg, self.board())
        self.assertNotIn('id="team"', page)
        self.assertEqual(page.count('<div class="tile '), 3)
        self.assertNotIn('<button type="button" class="tile', page)
        self.assertIn("<b><i>■</i> Zed</b><span>Can crash</span>", page)

    def test_card_ids_never_reach_the_page(self):
        board = self.board()
        for i, c in enumerate(board["characters"]):
            c["id"] = 7700001 + i
            for j, issue in enumerate(c["issues"]):
                issue["id"] = 7710001 + 10 * i + j
        n = 7720001
        for s in board["sections"]:
            for box in s["boxes"]:
                box["id"], n = n, n + 1
                for ln in box["lines"]:
                    ln["id"], n = n, n + 1
        page = quiet(build.render, render_cfg(), board)
        self.assertIsNone(re.search(r"77[012]\d{4}", page))


class Words(unittest.TestCase):
    def test_board_words_override_the_config(self):
        cfg, board = dict(render_cfg(), legend=FULL_LEGEND), snapshot()
        board["page"]["words"] = {"stamp": "As of {time}", "size": "{size} sized",
                                  "counts": "{fixed} mended, {open} left", "legend_now": "in hand"}
        page = quiet(build.render, cfg, board)
        self.assertIn('<p class="stamp">As of 2026-11-25 05:30 · latest release', page)
        self.assertIn('<span class="size">small sized</span>', page)
        self.assertIn("<small>0 mended, 1 left</small>", page)
        self.assertIn("<span>in hand</span> <b>1</b>", page)
        self.assertNotIn("Last updated", page)
        self.assertNotIn("<span>doing</span>", page)

    def test_unknown_board_word_warns_and_is_ignored(self):
        cfg, board = render_cfg(), snapshot()
        board["page"]["words"] = {"sidebar": "zzz-unused"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        self.assertIn("warn: word 'sidebar' is not a phrase this page uses", err.getvalue())
        self.assertNotIn("zzz-unused", page)

    def test_board_word_with_a_wrong_placeholder_or_no_text_keeps_the_config_phrase(self):
        cfg, board = dict(render_cfg(), legend=FULL_LEGEND), snapshot()
        board["page"]["words"] = {"counts": "{opne} open, {fixed} fixed", "size": "",
                                  "legend_now": "now {n}", "stamp": "As of {time}"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        for key in ("counts", "size", "legend_now"):
            self.assertIn(f"warn: word '{key}' is empty or holds a placeholder", err.getvalue())
        self.assertNotIn("stamp", err.getvalue())
        self.assertIn("<small>1 open · 0 fixed</small>", page)
        self.assertIn('<span class="size">small job</span>', page)
        self.assertIn("<span>doing</span>", page)
        self.assertIn('<p class="stamp">As of 2026-11-25 05:30 · ', page)
        self.assertNotRegex(visible(page), r"[{}]")

    def test_words_are_escaped_and_placeholders_filled_once(self):
        cfg, board = render_cfg(), snapshot()
        board["page"]["words"] = {"latest": "<b>{tag}</b> & {date} {tag}"}
        board["latest_release"] = {"tag": "v1", "date": "{tag}"}
        page = quiet(build.render, cfg, board)
        link = '<a href="#shipped" data-rel="v1">v1</a>'
        self.assertIn(f"&lt;b&gt;{link}&lt;/b&gt; &amp; {{tag}} {link}</p>", page)

    def test_config_without_words_is_malformed_input(self):
        cfg = render_cfg()
        cfg.pop("words")
        with self.assertRaises(KeyError):
            quiet(build.render, cfg, snapshot())
        with tempfile.TemporaryDirectory() as d:
            paths = [os.path.join(d, n) for n in ("cfg.json", "board.json", "out.html")]
            for path, obj in zip(paths, (cfg, snapshot())):
                with open(path, "w") as f:
                    json.dump(obj, f)
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                build.main(["--config", paths[0], "--board", paths[1], "--out", paths[2]])
            self.assertIn("build failed: malformed input", str(cm.exception.code))
            self.assertFalse(os.path.exists(paths[2]))

    def test_the_code_holds_no_words(self):
        """Every word a visitor reads is a snapshot or config string, never a literal in build.py."""
        n = iter(range(10_000))

        def zz():
            i = next(n)
            return "zz" + chr(97 + i // 26) + chr(97 + i % 26)

        def scrub(obj, keys):
            if isinstance(obj, dict):
                return {k: (zz() if k in keys and isinstance(v, str) else scrub(v, keys))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [scrub(v, keys) for v in obj]
            return obj

        board = scrub(snapshot(), {"title", "tagline", "goal", "notice", "footer", "label", "heading",
                                   "note", "blurb", "size", "text", "name", "characters_heading",
                                   "characters_banner"})
        board["characters"][0]["issues"][0]["needs_repro"] = True
        board["sections"][0]["boxes"][0]["lines"][1]["release"] = "v1"
        board["sections"][0]["boxes"][0]["lines"].append(
            line(19, "zzzz", cards=[{"done": False, "needs_repro": True}]))
        cfg = scrub(dict(render_cfg(), legend=FULL_LEGEND), {"text"})
        cfg["words"] = {k: " ".join([zz()] + re.findall(r"\{\w+\}", v)) for k, v in WORDS.items()}
        page = quiet(build.render, cfg, board)
        seen = visible(page)
        words = set(re.findall(r"[^\W\d_]{2,}", seen))
        self.assertGreater(len(words), 40)
        self.assertEqual({w for w in words if not w.startswith("zz")}, set(), seen)
        for k, v in cfg["words"].items():
            self.assertIn(v.split()[0], seen, k)

    def test_the_script_cannot_write_words_or_reach_the_network(self):
        js = script(quiet(build.render, render_cfg(), snapshot()))
        for banned in ("textContent", "innerHTML", "innerText", "insertAdjacent", "createTextNode",
                       "document.write", "fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket",
                       "EventSource", "import(", ".src", "http"):
            self.assertNotIn(banned, js)

    def test_page_is_self_contained(self):
        """A static file: nothing is loaded at view time; the only URLs are the board's links."""
        page = quiet(build.render, render_cfg(), snapshot())
        for banned in ("<link", "<img", "<iframe", "<script src", "@import", "url(", "@font-face"):
            self.assertNotIn(banned, page)
        self.assertEqual(page.count("<script>"), 1)
        self.assertEqual(sorted(set(re.findall(r'https?://[^"\'\s<)]+', page))),
                         ["https://example.invalid/", "https://github.com/o/r/releases/tag/v1"])


class Baked(unittest.TestCase):
    """The repo's own config and snapshot: what nginx serves until the first live refresh."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "config", "roadmap.json")) as f:
            cls.cfg = json.load(f)
        with open(os.path.join(ROOT, "data", "board.json")) as f:
            cls.board = json.load(f)
        cls.err = io.StringIO()
        with contextlib.redirect_stderr(cls.err):
            cls.page = build.render(cls.cfg, cls.board)
        cls.seen = visible(cls.page)

    def test_committed_page_is_what_the_build_produces(self):
        with open(os.path.join(ROOT, "site", "index.html")) as f:
            self.assertEqual(f.read(), self.page, "run python3 tools/build.py and commit site/index.html")

    def test_build_is_quiet_on_the_committed_snapshot(self):
        self.assertEqual(self.err.getvalue(), "")

    def test_no_placeholder_is_left_unfilled(self):
        self.assertNotRegex(self.seen, r"[{}]")

    def test_cli_builds_the_same_page_and_reports(self):
        with tempfile.TemporaryDirectory() as d:
            out, said = os.path.join(d, "site", "index.html"), io.StringIO()
            with contextlib.redirect_stdout(said), contextlib.redirect_stderr(io.StringIO()):
                build.main(["--config", os.path.join(ROOT, "config", "roadmap.json"),
                            "--board", os.path.join(ROOT, "data", "board.json"), "--out", out])
            with open(out) as f:
                self.assertEqual(f.read(), self.page)
        self.assertRegex(said.getvalue(), r"^ok: \d+ upcoming, \d+ shipped, characters tiles=\d+ ")

    def test_markup_is_balanced_and_ids_are_unique(self):
        void = {"meta", "input", "br", "hr", "img", "link"}
        stack, ids = [], []

        class Check(html.parser.HTMLParser):
            def handle_starttag(self, tag, attrs):
                ids.extend(v for k, v in attrs if k == "id")
                if tag not in void:
                    stack.append(tag)

            def handle_endtag(self, tag):
                assert stack and stack.pop() == tag, f"unbalanced </{tag}> at line {self.getpos()[0]}"

        p = Check()
        p.feed(self.page)
        p.close()
        self.assertEqual(stack, [])
        self.assertEqual(len(ids), len(set(ids)))
        # nothing interactive is nested inside a button
        for inner in re.findall(r"<button\b[^>]*>(.*?)</button>", self.page, re.S):
            self.assertIsNone(re.search(r"<(a|button|input|div|p|ul|ol)\b", inner))

    def has(self, text):
        self.assertIn(" ".join(text.split()), self.seen)

    def test_all_page_text_is_reachable(self):
        page = self.board["page"]
        for key in ("title", "tagline", "goal", "notice", "footer", "characters_heading",
                    "characters_banner"):
            self.has(page[key])
        for link in page["links"]:
            self.has(link["label"])
            self.assertIn(f'href="{link["href"]}"', self.page)
        self.assertIn(build.fmt_time(self.board["exported_at"]), self.seen)
        self.has(self.board["latest_release"]["tag"])
        self.has(self.board["latest_release"]["date"])
        for entry in self.cfg["legend"]:
            self.has(entry["text"])

    def test_every_section_box_and_line_is_reachable(self):
        kinds, n = set(), 0
        for s in self.board["sections"]:
            kinds.add(s["kind"])
            self.has(s["heading"])
            self.assertIn(f'href="#{s["id"]}"', self.page)
            if s.get("note"):
                self.has(s["note"])
            for box in s["boxes"]:
                self.has(box["title"])
                if box.get("blurb"):
                    self.has(box["blurb"])
                if box.get("size"):
                    self.has(self.cfg["words"]["size"].replace("{size}", box["size"]))
                for ln in box["lines"]:
                    text = build.fill(ln["text"], self.board)
                    m = build.DATED_RE.fullmatch(text) if s["kind"] == "background" else None
                    for part in (m.groups() if m else (text,)):
                        self.has(part)
                    if s["kind"] == "shipped" and ln.get("release"):
                        self.assertIn(f'/releases/tag/{ln["release"]}">{ln["release"]}</a>', self.page)
                    elif s["kind"] == "shipped" and ln.get("date"):
                        self.has(f'{text} ({ln["date"]})')
                    n += 1
        self.assertEqual(kinds, {"active", "upcoming", "not_planned", "shipped", "background"})
        self.assertEqual(self.page.count("<li"), n)

    def test_every_character_and_level_is_reachable(self):
        for lv in self.board["levels"]:
            self.has(lv["label"])
            self.has(lv["text"])
            self.has(lv["mark"])
        chars = self.board["characters"]
        self.assertEqual(self.page.count('class="tile '), len(chars))
        for c in chars:
            level, n_open, fixed, awaiting = build.character_level(c, self.board["types"])
            label = next(lv["label"] for lv in self.board["levels"] if lv["key"] == level)
            tally = f"{n_open} open · {fixed} fixed"
            if awaiting:
                tally += f" · {awaiting} awaiting confirmation"
            self.has(f'{c["name"]} {label} {tally}')
        slugs = re.findall(r'class="tile [^>]* value="([^"]+)"', self.page)
        self.assertEqual(len(set(slugs)), len(chars))
        for v in slugs:
            self.assertRegex(v, r"^[a-z0-9-]+$")


# --- export ----------------------------------------------------------------

GH_RELEASES = [{"tag_name": "v1", "draft": False, "prerelease": False,
                "published_at": "2026-11-24T10:00:00Z"}]


def run_export(cfg, data, tmp, prev=None, github=True):
    """Run the exporter's CLI over a board dump; `github=False` simulates an outage."""
    paths = {k: os.path.join(tmp, k + ".json") for k in ("config", "board", "out", "prev")}
    with open(paths["config"], "w") as f:
        json.dump(cfg, f)
    with open(paths["board"], "w") as f:
        json.dump(data, f)
    args = ["--config", paths["config"], "--board-json", paths["board"], "--out", paths["out"]]
    if prev is not None:
        with open(paths["prev"], "w") as f:
            json.dump(prev, f)
        args += ["--prev", paths["prev"]]
    fake = (lambda req, timeout=None: FakeResp(GH_RELEASES)) if github \
        else mock.Mock(side_effect=OSError("offline"))
    err = io.StringIO()
    with mock.patch("urllib.request.urlopen", fake), contextlib.redirect_stderr(err):
        export_board.main(args)
    with open(paths["out"]) as f:
        return json.load(f), err.getvalue()


class ExportSections(unittest.TestCase):
    def test_order_and_records(self):
        cfg, data = small_board()
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d, prev={"releases": ["v1"],
                                                     "latest_release": {"tag": "v1", "date": "2026-11-24"}})
        self.assertEqual(out["schema"], 2)
        self.assertEqual([s["kind"] for s in out["sections"]], ["active"])
        self.assertEqual(out["sections"][0]["id"], "now")
        self.assertEqual(out["sections"][0]["heading"], "Now")
        self.assertEqual(out["sections"][0]["note"], "(Currently working on this)")
        boxes = out["sections"][0]["boxes"]
        # box order follows the box-view position: 1 (pos 100) then 4 (pos 200)
        self.assertEqual([b["id"] for b in boxes][0], 1)
        self.assertEqual([b["title"] for b in boxes], ["Box A", "Box B"])
        self.assertEqual(boxes[0]["size"], "large")
        self.assertEqual(boxes[0]["blurb"], "blurb A")
        self.assertIsNone(boxes[1]["size"])
        # line order follows the lines view: 3 (10), 2 (20), 5 (5)
        self.assertEqual([l["id"] for l in boxes[0]["lines"]], [3, 2])
        self.assertEqual([l["id"] for l in boxes[1]["lines"]], [5])
        self.assertEqual(boxes[1]["lines"][0]["release"], "v0.6.9")
        self.assertEqual(boxes[0]["lines"][1]["cards"], [{"done": False, "needs_repro": False}])
        self.assertEqual(out["stats"]["verifying_open"], 0)
        self.assertEqual(err, "")

    def test_bucket_title_whitespace_is_stripped(self):
        cfg, data = small_board(with_title=False)
        data["tasks"].append(task(30, "Title", labels=[BOX], desc="Roadmap"))
        data["box_view"].append(box_item(30, 900, 6))
        data["tasks"].append(task(31, "Box D", labels=[BOX], rel=rel(subtask=[32])))
        data["tasks"].append(task(32, "a next line", labels=[LINE], rel=rel(parenttask=[31])))
        item = box_item(31, 400, 7)
        item["buckets"][0]["title"] = "  Next (Starts after the current work)   "
        data["box_view"].append(item)
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d)
        nxt = next(s for s in out["sections"] if s["id"] == "next")
        self.assertEqual(nxt["heading"], "Next")
        self.assertEqual(nxt["note"], "(Starts after the current work)")

    def test_line_flags_and_date(self):
        cfg, data = small_board()
        data["tasks"].append(task(6, "Box C", labels=[BOX], rel=rel(subtask=[7])))
        data["tasks"].append(task(7, "dated line", labels=[LINE, NCONF], end="2026-05-24T12:00:00Z",
                                  rel=rel(parenttask=[6], related=[901, 902])))
        data["tasks"] += [task(901, "bug one"), task(902, "bug two")]
        data["tasks"][-2]["labels"] = [{"id": 17}]
        data["box_view"].append(box_item(6, 300, 7))
        data["line_view"].append(line_item(7, 30))
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d)
        line = next(l for s in out["sections"] for b in s["boxes"] if b["id"] == 6
                    for l in b["lines"])
        self.assertEqual(line["date"], "2026-05-24")
        self.assertTrue(line["not_confirming"])
        self.assertFalse(line["confirming"])
        self.assertIsNone(line["release"])
        self.assertEqual(line["cards"], [{"done": False, "needs_repro": True},
                                         {"done": False, "needs_repro": False}])
        self.assertEqual(out["stats"]["verifying_open"], 1)


class ExportExclusions(unittest.TestCase):
    def test_draft_box_is_silent(self):
        cfg, data = small_board()
        data["tasks"].append(task(20, "Draft box", labels=[BOX]))
        data["box_view"].append(box_item(20, 10, 1))
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertEqual(len(out["sections"]), 1)
        self.assertEqual(err, "")

    def test_unlabelled_box_is_silent(self):
        cfg, data = small_board()
        data["tasks"].append(task(21, "No label"))
        data["box_view"].append(box_item(21, 10, 2))
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertEqual({b["id"] for s in out["sections"] for b in s["boxes"]}, {1, 4})
        self.assertEqual(err, "")

    def test_box_with_parent_warns_and_is_absent(self):
        cfg, data = small_board()
        data["tasks"].append(task(22, "Child box", labels=[BOX], rel=rel(parenttask=[1])))
        data["box_view"].append(box_item(22, 10, 2))
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertNotIn(22, {b["id"] for s in out["sections"] for b in s["boxes"]})
        self.assertIn("warn: box card 22 has a parent task", err)

    def test_unlabelled_subtask_warns_and_is_absent(self):
        cfg, data = small_board()
        data["tasks"].append(task(23, "draft line", rel=rel(parenttask=[1])))
        data["tasks"][0]["related_tasks"]["subtask"].append({"id": 23})
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        ids = {l["id"] for s in out["sections"] for b in s["boxes"] for l in b["lines"]}
        self.assertNotIn(23, ids)
        self.assertIn("warn: line card 23 has no line label", err)

    def test_two_parents_warns_and_is_absent(self):
        cfg, data = small_board()
        data["tasks"].append(task(24, "shared line", labels=[LINE], rel=rel(parenttask=[1, 4])))
        data["tasks"][0]["related_tasks"]["subtask"].append({"id": 24})
        data["tasks"][3]["related_tasks"]["subtask"].append({"id": 24})
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        ids = {l["id"] for s in out["sections"] for b in s["boxes"] for l in b["lines"]}
        self.assertNotIn(24, ids)
        self.assertIn("warn: line card 24 does not have exactly one parent", err)

    def test_line_with_subtasks_warns_and_is_absent(self):
        cfg, data = small_board()
        data["tasks"].append(task(25, "branchy line", labels=[LINE],
                                  rel=rel(parenttask=[1], subtask=[3])))
        data["tasks"][0]["related_tasks"]["subtask"].append({"id": 25})
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        ids = {l["id"] for s in out["sections"] for b in s["boxes"] for l in b["lines"]}
        self.assertNotIn(25, ids)
        self.assertIn("warn: line card 25 has subtasks of its own", err)

    def test_box_without_published_lines_warns(self):
        cfg, data = small_board()
        data["tasks"].append(task(26, "Empty box", labels=[BOX]))
        data["box_view"].append(box_item(26, 10, 2))
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertNotIn(26, {b["id"] for s in out["sections"] for b in s["boxes"]})
        self.assertIn("warn: box card 26 has no published lines", err)

    def test_two_size_labels_drop_size_and_bold_blurb_drops_blurb(self):
        cfg, data = small_board()
        data["tasks"][0]["labels"] = [{"id": BOX}, {"id": 110}, {"id": 111}]
        data["tasks"][0]["description"] = "**bold**"
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        box = out["sections"][0]["boxes"][0]
        self.assertIsNone(box["size"])
        self.assertIsNone(box["blurb"])
        self.assertIn("has 2 size labels", err)
        self.assertIn("is not plain text", err)


class ExportPage(unittest.TestCase):
    def page_board(self):
        cfg, data = small_board(with_title=False)
        texts = {"Title": "My roadmap", "Tagline": "a tagline", "Goal": "a goal",
                 "Notice": "a notice", "Footer": "a footer", "Characters banner": "banner"}
        box_view = []
        for i, (title, desc) in enumerate(texts.items()):
            tid = 40 + i
            data["tasks"].append(task(tid, title, labels=[BOX], desc=desc))
            data["box_view"].append(box_item(tid, 100 + i, 6))
        data["tasks"].append(task(50, "Links", labels=[BOX], rel=rel(subtask=[51, 52])))
        data["box_view"].append(box_item(50, 200, 6))
        data["tasks"].append(task(51, "Discord", labels=[LINE], desc="https://discord.gg/x",
                                  rel=rel(parenttask=[50])))
        data["tasks"].append(task(52, "Source", labels=[LINE], desc="no url here",
                                  rel=rel(parenttask=[50])))
        return cfg, data

    def test_page_keys_and_links(self):
        cfg, data = self.page_board()
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertEqual(out["page"]["title"], "My roadmap")
        self.assertEqual(out["page"]["tagline"], "a tagline")
        self.assertEqual(out["page"]["characters_banner"], "banner")
        self.assertEqual(out["page"]["links"], [{"label": "Discord", "href": "https://discord.gg/x"}])
        self.assertIn("warn: link card 52 has no https URL", err)
        self.assertEqual({s["id"] for s in out["sections"]}, {"now"})

    def test_missing_title_exits_1(self):
        cfg, data = self.page_board()
        data["tasks"] = [t for t in data["tasks"] if t["id"] != 40]
        data["box_view"] = [b for b in data["box_view"] if b["id"] != 40]
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            run_export(cfg, data, d)

    def test_empty_page_value_is_omitted_with_warning(self):
        cfg, data = self.page_board()
        next(t for t in data["tasks"] if t["id"] == 41)["description"] = ""
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertNotIn("tagline", out["page"])
        self.assertIn("warn: page card 41", err)

    def test_unlabelled_page_subtasks_publish(self):
        cfg, data = self.page_board()
        next(t for t in data["tasks"] if t["id"] == 51)["labels"] = None
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d)
        self.assertEqual(out["page"]["links"],
                         [{"label": "Discord", "href": "https://discord.gg/x"}])

    def test_words_card_puts_phrase_overrides_in_the_snapshot(self):
        cfg, data = self.page_board()
        data["tasks"].append(task(60, "Words", labels=[BOX], rel=rel(subtask=[61, 62, 63, 64])))
        data["box_view"].append(box_item(60, 300, 6))
        data["tasks"] += [task(61, "Stamp", desc="As of {time}", rel=rel(parenttask=[60])),
                          task(62, "fix released", desc="(being checked)", rel=rel(parenttask=[60])),
                          task(63, "stamp", desc="again", rel=rel(parenttask=[60])),
                          task(64, "size", desc="**bold**", rel=rel(parenttask=[60]))]
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertEqual(out["page"]["words"],
                         {"stamp": "As of {time}", "fix_released": "(being checked)"})
        self.assertIn("warn: word card 63 repeats phrase 'stamp'", err)
        self.assertIn("warn: word card 64 needs a phrase key", err)
        self.assertNotIn("unknown title", err)
        # and the page built from that snapshot says it the board's way
        page = quiet(build.render, CFG, dict(snapshot(), page=dict(snapshot()["page"], **out["page"])))
        self.assertIn('<p class="stamp">As of 2026-11-25 05:30 · ', page)

    def test_no_words_card_leaves_the_snapshot_shape_alone(self):
        cfg, data = self.page_board()
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d)
        self.assertNotIn("words", out["page"])
        self.assertEqual(out["schema"], 2)

    def test_unknown_page_title_warns(self):
        cfg, data = self.page_board()
        data["tasks"].append(task(53, "Sidebar", labels=[BOX], desc="x"))
        data["box_view"].append(box_item(53, 300, 6))
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertIn("warn: page card 53 has an unknown title", err)


class ExportCharacters(unittest.TestCase):
    def char_board(self):
        cfg = dict(CFG)
        tasks = [
            task(1, "Box A", labels=[BOX], desc="b", rel=rel(subtask=[2])),
            task(2, "line one", labels=[LINE], rel=rel(parenttask=[1])),
            task(30, "Title", labels=[BOX], desc="Roadmap"),
            task(31, "Characters heading", labels=[BOX], desc="Characters"),
            task(32, "Levels", labels=[BOX], rel=rel(subtask=[33, 34])),
            task(33, "ready", desc="● |Tournament-ready |clean"),
            task(34, "crash", desc="■ |Can crash |crashes"),
            task(35, "Types", labels=[BOX], rel=rel(subtask=[36, 37, 38])),
            task(36, "crash", desc="crash"),
            task(37, "effect", desc="playable"),
            task(38, "verify", desc="none"),
            task(140, "Ryu", labels=[CHAR, READY], rel=rel(subtask=[141])),
            task(141, "invisible during super", desc="**Type:** crash"),
            task(142, "Morrigan", labels=[CHAR], rel=rel(subtask=[143, 144])),
            task(143, "sound cuts out", desc="**Type:** effect"),
            task(144, "reported elsewhere", labels=[IGNORE], desc="**Type:** crash"),
            task(9, "Release v1", done=True),
        ]
        box_view = [box_item(1, 100, 2), box_item(30, 900, 6), box_item(31, 910, 6),
                    box_item(32, 920, 6), box_item(35, 930, 6)]
        return cfg, dump(tasks, box_view)

    def test_character_cards_are_discovered_by_label(self):
        cfg, data = self.char_board()
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertEqual(err, "")
        self.assertEqual([c["name"] for c in out["characters"]], ["Morrigan", "Ryu"])
        morrigan, ryu = out["characters"]
        self.assertFalse(morrigan["ready"])
        self.assertTrue(ryu["ready"])
        # the ignored issue is dropped before it leaves the exporter
        self.assertEqual([i["id"] for i in morrigan["issues"]], [143])
        self.assertEqual(ryu["issues"],
                         [{"id": 141, "done": False, "type": "crash", "needs_repro": False}])
        self.assertEqual([lv["key"] for lv in out["levels"]], ["ready", "crash"])
        self.assertEqual(out["levels"][0], {"key": "ready", "mark": "●",
                                            "label": "Tournament-ready", "text": "clean"})
        self.assertEqual(out["types"], {"crash": "crash", "effect": "playable", "verify": None})

    def test_no_levels_card_omits_characters(self):
        cfg, data = self.char_board()
        data["tasks"] = [t for t in data["tasks"] if t["id"] not in (32, 33, 34)]
        data["box_view"] = [b for b in data["box_view"] if b["id"] != 32]
        with tempfile.TemporaryDirectory() as d:
            out, err = run_export(cfg, data, d)
        self.assertNotIn("characters", out)
        self.assertIn("no 'Levels' page card", err)

    def test_missing_characters_heading_exits_1(self):
        cfg, data = self.char_board()
        data["tasks"] = [t for t in data["tasks"] if t["id"] != 31]
        data["box_view"] = [b for b in data["box_view"] if b["id"] != 31]
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            run_export(cfg, data, d)

    def test_no_character_cards_omits_key(self):
        cfg, data = small_board()
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d)
        self.assertNotIn("characters", out)
        self.assertNotIn("levels", out)
        self.assertNotIn("types", out)


class ExportReleases(unittest.TestCase):
    def test_etag_only_when_present(self):
        cfg, data = small_board()
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d, prev={"releases": ["v1"],
                                                   "latest_release": {"tag": "v1", "date": "2026-11-24"}})
        self.assertEqual(out["releases"], ["v1"])
        self.assertEqual(out["latest_release"], {"tag": "v1", "date": "2026-11-24"})
        self.assertNotIn("releases_etag", out)
        snap = export_board.export(cfg, data, ["v1"], {"tag": "v1", "date": "2026-11-24"},
                                   releases_etag='W/"x"')
        self.assertEqual(snap["releases_etag"], 'W/"x"')

    def test_no_releases_block_skips_github(self):
        cfg, data = small_board()
        cfg = {k: v for k, v in cfg.items() if k != "releases"}
        with tempfile.TemporaryDirectory() as d:
            out, _ = run_export(cfg, data, d)
        self.assertEqual(out["releases"], [])
        self.assertIsNone(out["latest_release"])
        self.assertIsNone(out["sections"][0]["boxes"][1]["lines"][0]["release"])


class FetchShare(unittest.TestCase):
    def test_requested_urls(self):
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url)
            if "/shares/" in req.full_url:
                return FakeResp({"token": "tok"})
            if "/views/10/tasks" in req.full_url:
                return FakeResp({"items": [{"id": 1}], "total_pages": 2})
            return FakeResp({"items": [], "total_pages": 1})

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hash")
            with open(path, "w") as f:
                f.write("abc\n")
            with mock.patch("urllib.request.urlopen", side_effect=fake):
                data = export_board.fetch_share("https://v", path, CFG)
        self.assertEqual(set(data), {"tasks", "box_view", "line_view"})
        auth = [u for u in seen if "/shares/" in u]
        self.assertEqual(len(auth), 1)
        self.assertTrue(auth[0].endswith("/api/v2/shares/abc/auth"))
        tasks = [u for u in seen if "/projects/19/tasks?" in u]
        self.assertEqual(len(tasks), 2)
        self.assertTrue(any("filter=done+%3D+false" in u for u in tasks))
        self.assertTrue(any("filter=done+%3D+true" in u for u in tasks))
        boxes = [u for u in seen if "/views/10/tasks" in u]
        self.assertEqual(len(boxes), 2)  # two pages, per the fake
        self.assertTrue(all("expand=buckets" in u for u in boxes))
        self.assertTrue(any("sort_by=position" in u and "order_by=asc" in u for u in boxes))
        self.assertEqual(len([u for u in seen if "/views/11/tasks" in u]), 1)
        self.assertFalse(any("expand=buckets" in u for u in seen if "/views/11/tasks" in u))

    def test_empty_share_hash_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hash")
            with open(path, "w") as f:
                f.write("  \n")
            with self.assertRaises(export_board.ExportError):
                export_board.fetch_share("https://v", path, CFG)


class FakeResp:
    """Minimal stand-in for urlopen's response object."""

    def __init__(self, payload, etag=None):
        self._body = json.dumps(payload).encode()
        self.headers = {"ETag": etag} if etag else {}

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


REL = {"repo": "o/r", "card_prefix": "Release "}


class FetchReleases(unittest.TestCase):
    def _prev(self, d, payload):
        path = os.path.join(d, "prev.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def test_url_uses_configured_repo(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            raise OSError("offline")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch("urllib.request.urlopen", side_effect=fake), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(export_board.ExportError):
            export_board.fetch_releases(REL, os.path.join(d, "missing.json"))
        self.assertEqual(seen["url"], "https://api.github.com/repos/o/r/releases?per_page=100")

    def test_not_modified_reuses_previous_silently(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", None, None)

        with tempfile.TemporaryDirectory() as d:
            prev = self._prev(d, {"releases": ["v0.6.8"],
                                  "latest_release": {"tag": "v0.6.8", "date": "2026-11-24"},
                                  "releases_etag": 'W/"etag1"'})
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0ken"}), \
                    mock.patch("urllib.request.urlopen", side_effect=fake), \
                    contextlib.redirect_stderr(err):
                tags, latest, etag = export_board.fetch_releases(REL, prev)
        self.assertEqual(tags, ["v0.6.8"])
        self.assertEqual(latest, {"tag": "v0.6.8", "date": "2026-11-24"})
        self.assertEqual(etag, 'W/"etag1"')
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(seen.get("if-none-match"), 'W/"etag1"')
        self.assertEqual(seen.get("authorization"), "Bearer t0ken")

    def test_new_etag_is_captured(self):
        rel = [{"tag_name": "v0.6.9", "draft": False, "prerelease": False,
                "published_at": "2026-11-25T10:00:00Z"},
               {"tag_name": "v0.6.8", "draft": False, "prerelease": False,
                "published_at": "2026-11-24T10:00:00Z"}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(rel, 'W/"etag2"')), \
                contextlib.redirect_stderr(io.StringIO()):
            tags, latest, etag = export_board.fetch_releases(REL, None)
        self.assertEqual(tags, ["v0.6.9", "v0.6.8"])
        self.assertEqual(latest, {"tag": "v0.6.9", "date": "2026-11-25"})
        self.assertEqual(etag, 'W/"etag2"')

    def test_board_release_card_advances_when_github_fails(self):
        tasks = [{"id": 1170, "title": "Release v0.6.9", "done": True,
                  "start_date": "2026-11-24T00:00:00Z", "end_date": "2026-11-25T00:00:00Z"},
                 {"id": 1171, "title": "Release v0.6.8", "done": True,
                  "start_date": "2026-11-24T00:00:00Z", "end_date": "2026-11-24T00:00:00Z"},
                 {"id": 1172, "title": "Release v0.7.0", "done": False,
                  "start_date": "2026-11-25T00:00:00Z", "end_date": None}]
        with tempfile.TemporaryDirectory() as d:
            prev = self._prev(d, {"releases": ["v0.6.8"],
                                  "latest_release": {"tag": "v0.6.8", "date": "2026-11-24"}})
            err = io.StringIO()
            with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                    contextlib.redirect_stderr(err):
                tags, latest, etag = export_board.fetch_releases(REL, prev, tasks)
        self.assertIn("warn: GitHub releases unavailable", err.getvalue())
        self.assertEqual(tags, ["v0.6.8", "v0.6.9"])
        self.assertEqual(latest, {"tag": "v0.6.9", "date": "2026-11-25"})
        self.assertIsNone(etag)

    def test_fails_without_prev_when_github_fails(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(export_board.ExportError):
                export_board.fetch_releases(REL, os.path.join(d, "missing.json"))


class Plain(unittest.TestCase):
    def test_plain_text_passes(self):
        self.assertEqual(export_board.plain("  two\n  lines  "), "two lines")

    def test_markup_rejected(self):
        for md in ("**b**", "__i__", "`c`", "[x](y)", "<p>x</p>", "# h", "- item", "1. item", "> q"):
            self.assertIsNone(export_board.plain(md), md)

    def test_escapes_undone(self):
        self.assertEqual(export_board.plain("a \\* b"), "a * b")

    def test_empty_is_none(self):
        self.assertIsNone(export_board.plain(""))
        self.assertIsNone(export_board.plain(None))


if __name__ == "__main__":
    unittest.main()
