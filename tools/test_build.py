import contextlib
import html
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

BOX, LINE, CONF, NCONF = 100, 101, 102, 103
SIZES = {110: "small", 111: "medium", 112: "large"}
BUCKETS = {"draft": 1, "active": 2, "not_planned": 3, "shipped": 4, "background": 5, "page": 6}
BUCKET_TITLES = {2: "Now (Currently working on this)", 3: "Not planned (Asked for)", 4: "Shipped",
                 5: "Background", 6: "Page text", 1: "Draft", 7: "Next", 8: "Later"}
BUCKET_POS = {2: 200, 7: 300, 8: 400, 3: 500, 4: 600, 5: 700, 6: 800, 1: 100}

CFG = {
    "project": 19,
    "min_tasks": 0,
    "views": {"boxes": 10, "lines": 11},
    "buckets": BUCKETS,
    "labels": {"box": BOX, "line": LINE, "confirming": CONF, "not_confirming": NCONF,
               "needs_repro": 17, "sizes": {str(k): v for k, v in SIZES.items()}},
    "releases": {"repo": "o/r", "card_prefix": "Release "},
    "forbidden": "\\bSP\\d|Impact|Bug census|\\bP[0-2]\\b|tasks\\.2143\\.me",
    "legend": [
        {"class": "now", "mark": "\u25b6", "text": "now"},
        {"class": "open", "mark": "\u25cb", "text": "planned"},
        {"class": "verifying", "mark": "\u25f7", "text": "confirming"},
        {"class": "done", "mark": "\u2714", "text": "shipped"},
        {"class": "no", "mark": "\u2715", "text": "not planned"},
    ],
    "ui": {
        "skip": "Skip to the roadmap",
        "updated": "Last updated",
        "latest": "Latest release",
        "links": "Downloads and community",
        "jump": "Page sections",
        "find": "Find on this page",
        "findHint": "Find a fix, feature or character",
        "matches": "{n} matches",
        "match": "{n} match",
        "noResults": "Nothing here matches that.",
        "all": "All",
        "filterLevel": "Show characters by level",
        "open": "open",
        "fixed": "fixed",
        "awaiting": "awaiting confirmation",
        "inRelease": "(in {tag}, confirming)",
        "needsConfirm": "(fix released, confirming)",
    },
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

    def test_rule1_not_planned_is_no(self):
        self.assertEqual(build.line_state("not_planned", self.line(done=True), set()),
                         ("no", None, False))

    def test_rule2_done_by_flag(self):
        self.assertEqual(build.line_state("upcoming", self.line(done=True), set()),
                         ("done", None, False))

    def test_rule2_done_when_all_cards_done(self):
        st = build.line_state("active", self.line(cards=[{"done": True, "needs_repro": False}]), set())
        self.assertEqual(st, ("done", None, False))

    def test_rule3_confirming_has_no_tag(self):
        self.assertEqual(build.line_state("upcoming", self.line(confirming=True), set()),
                         ("verifying", None, False))

    def test_rule4_not_confirming_beats_needs_repro_active(self):
        st = build.line_state("active", self.line(not_confirming=True,
                                                  cards=[{"done": False, "needs_repro": True}]), set())
        self.assertEqual(st, ("now", None, False))

    def test_rule4_not_confirming_beats_needs_repro_upcoming(self):
        st = build.line_state("upcoming", self.line(not_confirming=True,
                                                    cards=[{"done": False, "needs_repro": True}]), set())
        self.assertEqual(st, ("open", None, False))

    def test_rule5_release_in_list(self):
        st = build.line_state("upcoming", self.line(release="v1"), {"v1"})
        self.assertEqual(st, ("verifying", "v1", False))

    def test_rule5_unknown_release_warns_and_falls_through(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = build.line_state("upcoming", self.line(release="v9"), {"v1"})
        self.assertEqual(st, ("open", None, False))
        self.assertIn("warn:", err.getvalue())

    def test_rule5_unknown_release_falls_through_to_needs_repro(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = build.line_state("upcoming", self.line(release="v9",
                                                        cards=[{"done": False, "needs_repro": True}]), {"v1"})
        self.assertEqual(st, ("verifying", None, True))

    def test_rule6_needs_repro(self):
        st = build.line_state("upcoming", self.line(cards=[{"done": False, "needs_repro": True}]), set())
        self.assertEqual(st, ("verifying", None, True))

    def test_rule7_fallback(self):
        self.assertEqual(build.line_state("active", self.line(), set()), ("now", None, False))
        self.assertEqual(build.line_state("upcoming", self.line(), set()), ("open", None, False))
        self.assertEqual(build.line_state("upcoming", self.line(cards=[{"done": True,
                                                                       "needs_repro": False},
                                                                      {"done": False,
                                                                       "needs_repro": False}]), set()),
                         ("open", None, False))


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


class CharacterGroups(unittest.TestCase):
    def board(self, chars, levels=None):
        return {"levels": levels if levels is not None else LEVELS,
                "characters": chars, "types": TYPES}

    def char(self, name, types, ready=False, cid=1):
        return {"id": cid, "name": name, "ready": ready,
                "issues": [{"id": i, "type": t, "done": False, "needs_repro": False}
                           for i, t in enumerate(types)]}

    def test_groups_follow_the_level_cards(self):
        groups = quiet(build.character_groups,
                       self.board([self.char("b", ["effect"]), self.char("a", ["crash"])]))
        self.assertEqual([g["level"]["key"] for g in groups],
                         ["ready", "playable", "desync", "crash", "untested"])
        self.assertEqual([r[0] for r in groups[3]["rows"]], ["a"])
        self.assertEqual([r[0] for r in groups[1]["rows"]], ["b"])

    def test_rows_are_sorted_by_name_and_counted(self):
        groups = quiet(build.character_groups,
                       self.board([self.char("Zed", ["effect"]),
                                   self.char("amy", ["effect"]),
                                   self.char("Bo", ["effect", "crash"])]))
        playable = [g for g in groups if g["level"]["key"] == "playable"][0]
        self.assertEqual([r[0] for r in playable["rows"]], ["amy", "Zed"])
        self.assertEqual(playable["rows"][0][2], 1)  # open issues
        crash = [g for g in groups if g["level"]["key"] == "crash"][0]
        self.assertEqual([r[0] for r in crash["rows"]], ["Bo"])

    def test_a_level_without_a_card_keeps_its_characters(self):
        levels = [lv for lv in LEVELS if lv["key"] != "crash"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            groups = build.character_groups(self.board([self.char("a", ["crash"])], levels))
        self.assertEqual([g["level"]["key"] for g in groups],
                         ["ready", "playable", "desync", "untested", "crash"])
        self.assertEqual([r[0] for r in groups[-1]["rows"]], ["a"])
        self.assertIn("warn:", err.getvalue())

    def test_nothing_to_group(self):
        self.assertEqual(quiet(build.character_groups,
                               {"levels": [], "characters": [], "types": TYPES}), [])

    def test_no_level_cards_keeps_the_character(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            groups = build.character_groups(
                {"levels": [], "characters": [self.char("a", ["crash"])], "types": TYPES})
        self.assertEqual(len(groups), 1)
        self.assertEqual([r[0] for r in groups[0]["rows"]], ["a"])
        self.assertIn("warn:", err.getvalue())


class ShippedLog(unittest.TestCase):
    def line(self, text, release=None, date=None, lid=1):
        return {"id": lid, "text": text, "done": True, "confirming": False,
                "not_confirming": False, "release": release, "date": date, "cards": []}

    def board(self, lines, releases=("v2", "v1")):
        return {"releases": list(releases), "sections": [
            {"id": "shipped", "heading": "Shipped", "kind": "shipped", "note": None,
             "boxes": [{"id": 1, "title": "S", "blurb": None, "size": None, "lines": lines}]}]}

    def test_newest_release_first(self):
        board = self.board([self.line("old", "v1"), self.line("new", "v2")])
        self.assertEqual([ln["text"] for ln in build.shipped_log(board)], ["new", "old"])

    def test_board_order_within_one_release(self):
        board = self.board([self.line("a", "v2", lid=1), self.line("b", "v2", lid=2)])
        self.assertEqual([ln["text"] for ln in build.shipped_log(board)], ["a", "b"])

    def test_dated_only_lines_follow_releases_newest_date_first(self):
        board = self.board([self.line("dated-old", date="2026-01-02"),
                            self.line("released", "v1"),
                            self.line("dated-new", date="2026-09-30")])
        self.assertEqual([ln["text"] for ln in build.shipped_log(board)],
                         ["released", "dated-new", "dated-old"])

    def test_undated_unreleased_lines_are_last(self):
        board = self.board([self.line("never dated"), self.line("v", "v2"),
                            self.line("dated", date="2026-05-01")])
        self.assertEqual([ln["text"] for ln in build.shipped_log(board)],
                         ["v", "dated", "never dated"])

    def test_unknown_release_falls_back_to_its_date(self):
        board = self.board([self.line("gone", "v0", date="2026-05-01"), self.line("v", "v2")],
                           releases=("v2",))
        self.assertEqual([ln["text"] for ln in build.shipped_log(board)], ["v", "gone"])


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
                     "not_confirming": False, "release": None, "date": None, "cards": []},
                    {"id": 18, "text": "old and done", "done": True, "confirming": False,
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
    return json.loads(json.dumps(CFG))


def quiet(fn, *a, **kw):
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


class Render(unittest.TestCase):
    def test_deterministic(self):
        cfg, board = render_cfg(), snapshot()
        self.assertEqual(build.render(cfg, board), build.render(cfg, board))

    def test_page_structure(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn("<title>T</title>", page)
        self.assertIn("<h1>T</h1>", page)
        self.assertIn('class="tagline">tag</p>', page)
        self.assertIn('class="goal">goal</p>', page)
        self.assertIn('class="notice">notice</p>', page)
        self.assertIn('<a href="https://example.invalid/">Discord</a>', page)
        self.assertIn('<footer class="wrap"><p>f</p></footer>', page)
        self.assertIn('class="legend"', page)
        self.assertIn('id="find"', page)
        for sid in ("now", "not-planned", "characters", "shipped", "history"):
            self.assertIn(f'id="{sid}"', page)

    def test_sections_keep_board_order_and_characters_sit_before_shipped(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertLess(page.index('id="now"'), page.index('id="not-planned"'))
        self.assertLess(page.index('id="not-planned"'), page.index('id="characters"'))
        self.assertLess(page.index('id="characters"'), page.index('id="shipped"'))
        self.assertLess(page.index('id="shipped"'), page.index('id="history"'))

    def test_nav_links_follow_the_page(self):
        page = quiet(build.render, render_cfg(), snapshot())
        nav = page[page.index('<nav class="toc"'):page.index('<form class="find"')]
        self.assertIn('<a href="#now">Now<span class="n">3</span></a>', nav)
        self.assertIn('<a href="#characters">Characters<span class="n">2</span></a>', nav)
        self.assertIn('<a href="#shipped">Shipped<span class="n">2</span></a>', nav)
        self.assertIn('<a href="#history">Background<span class="n">1</span></a>', nav)
        self.assertLess(nav.index("#now"), nav.index("#characters"))
        self.assertLess(nav.index("#characters"), nav.index("#shipped"))

    def test_marks_and_state_tails(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('<li class="line verifying" data-find="7 awaiting">'
                      '<span class="mark verifying" role="img" aria-label="confirming">◷</span>'
                      '<span class="lt">7 awaiting</span></li>', page)
        self.assertIn('<li class="line no" data-find="x">'
                      '<span class="mark no" role="img" aria-label="not planned">✕</span>', page)
        self.assertIn('<span class="mark now" role="img" aria-label="now">▶</span>', page)
        # the legend's own marks stay decorative: its wording sits next to them
        self.assertIn('<li class="now"><span class="mark now" aria-hidden="true">▶</span>now</li>',
                      page)

    def test_release_tail_comes_from_config(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][0]["boxes"][0]["lines"][1] = {
            "id": 19, "text": "in a release", "done": False, "confirming": False,
            "not_confirming": False, "release": "v1", "date": None, "cards": []}
        cfg["ui"]["inRelease"] = "(inside {tag}, awaiting players)"
        page = quiet(build.render, cfg, board)
        self.assertIn('<small class="tail">(inside v1, awaiting players)</small>', page)
        self.assertIn('data-find="in a release (inside v1, awaiting players)"', page)

    def test_needs_repro_tail(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][0]["boxes"][0]["lines"][1] = {
            "id": 19, "text": "fixed", "done": False, "confirming": False,
            "not_confirming": False, "release": None, "date": None,
            "cards": [{"done": False, "needs_repro": True}]}
        page = quiet(build.render, cfg, board)
        self.assertIn('<small class="tail">(fix released, confirming)</small>', page)

    def test_done_lines_are_marked_not_hidden(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('class="line done"', page)
        self.assertIn("old and done", page)
        self.assertNotIn('id="show-done"', page)

    def test_size_badge(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('<span class="size" data-size="small">', page)
        self.assertIn("small</span>", page)
        self.assertEqual(page.count('class="meter"'), 1)

    def test_character_chips_and_groups(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertEqual(page.count('id="lv-all"'), 1)
        self.assertEqual(page.count('class="lv-radio"'), len(LEVELS) + 1)
        self.assertEqual(page.count('class="lv-group '), len(LEVELS))
        self.assertEqual(page.count('<li class="tile'), 2)
        self.assertIn('<label class="chip" for="lv-all">All<span class="n">2</span></label>', page)
        self.assertIn('#lv-0:checked~.roster>[data-level]:not([data-level="0"]){display:none}', page)
        self.assertIn('data-level="0"', page)

    def test_tile_counts_drop_zeros(self):
        cfg, board = render_cfg(), snapshot()
        board["characters"][0]["issues"].append(
            {"id": 4242, "done": True, "type": None, "needs_repro": False})
        page = quiet(build.render, cfg, board)
        self.assertIn('<span class="tstat">1 open · 1 fixed</span>', page)
        self.assertNotIn("0 open", page)
        self.assertNotIn("0 fixed", page)

    def test_awaiting_has_its_own_line(self):
        cfg, board = render_cfg(), snapshot()
        board["characters"][0]["issues"][0]["needs_repro"] = True
        page = quiet(build.render, cfg, board)
        self.assertIn('<span class="tstat tawait">1 awaiting confirmation</span>', page)

    def test_find_index_is_lowercase_board_text(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('data-find="g b"', page)
        self.assertIn('data-find="2026-01-01: t"', page)
        self.assertIn('data-find="c00"', page)
        self.assertIn('data-find="s v1"', page)

    def test_find_output_templates_come_from_config(self):
        cfg, board = render_cfg(), snapshot()
        cfg["ui"]["matches"] = "{n} hits"
        cfg["ui"]["match"] = "{n} hit"
        cfg["ui"]["noResults"] = "no hits"
        page = quiet(build.render, cfg, board)
        self.assertIn('data-many="{n} hits"', page)
        self.assertIn('data-one="{n} hit"', page)
        self.assertIn('data-none="no hits"', page)

    def test_ui_labels_come_from_config(self):
        cfg, board = render_cfg(), snapshot()
        cfg["ui"]["open"] = "free"
        cfg["ui"]["awaiting"] = "waiting on players"
        board["characters"][1]["issues"][0]["done"] = True
        board["characters"][0]["issues"][0]["needs_repro"] = True
        page = quiet(build.render, cfg, board)
        self.assertIn("1 free", page)
        self.assertIn("1 fixed", page)
        self.assertIn("1 waiting on players", page)

    def test_unknown_token_drops_the_line(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][0]["boxes"][0]["lines"][1]["text"] = "{secret} kept"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        self.assertNotIn("{secret}", page)
        self.assertNotIn("kept", page)
        self.assertIn("warn:", err.getvalue())

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
            build.render(cfg, board)
        self.assertIn("warn: latest release v2 is not listed in shipped", err.getvalue())

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
        board["sections"][3]["boxes"][0]["lines"].append(
            {"id": 17, "text": "2026-11-25: v2: something", "done": False, "confirming": False,
             "not_confirming": False, "release": None, "date": None, "cards": []})
        quiet(build.render, cfg, board, True)
        board["sections"][3]["boxes"][0]["lines"][-1]["text"] = "2026-11-25: v20: something"
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board, True)

    def test_latest_release_fact_panel(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('<p class="eyebrow">Latest release</p>', page)
        self.assertIn('<a href="https://github.com/o/r/releases/tag/v1">v1</a>', page)
        self.assertIn('<time datetime="2026-11-24">2026-11-24</time>', page)
        self.assertIn("Last updated 2026-11-25 05:30 UTC", page)

    def test_optional_page_keys_absent(self):
        cfg, board = render_cfg(), snapshot()
        for k in ("tagline", "goal", "notice", "footer", "links", "characters_banner"):
            board["page"].pop(k)
        page = quiet(build.render, cfg, board)
        self.assertNotIn('class="links"', page)
        self.assertNotIn("<footer", page)
        self.assertNotIn('class="goal"', page)
        self.assertNotIn('class="notice"', page)
        self.assertNotIn('class="banner"', page)
        self.assertNotIn(">tag<", page)

    def test_latest_release_null_omits_the_panel(self):
        cfg, board = render_cfg(), snapshot()
        board["latest_release"] = None
        board["releases"] = []
        page = quiet(build.render, cfg, board)
        self.assertNotIn("Latest release", page)
        self.assertNotIn("latest release", page)
        self.assertIn("Last updated 2026-11-25 05:30 UTC", page)

    def test_no_characters_block(self):
        cfg, board = CFG, snapshot()
        board.pop("characters")
        page = quiet(build.render, cfg, board)
        self.assertNotIn('class="tile', page)
        self.assertNotIn('id="characters"', page)
        self.assertNotIn('class="banner"', page)

    def test_schema_must_be_2(self):
        cfg, board = render_cfg(), snapshot()
        board["schema"] = 1
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board)

    def test_page_stays_under_the_size_limit(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertLess(len(page.encode()), build.MAX_BYTES)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def committed():
    with open(os.path.join(ROOT, "config", "roadmap.json")) as f:
        cfg = json.load(f)
    with open(os.path.join(ROOT, "data", "board.json")) as f:
        board = json.load(f)
    return cfg, board


class CommittedBoard(unittest.TestCase):
    """Guards over the snapshot and the page actually served from git."""

    def test_committed_page_matches_the_build(self):
        cfg, board = committed()
        page = quiet(build.render, cfg, board)
        with open(os.path.join(ROOT, "site", "index.html")) as f:
            self.assertEqual(f.read(), page)

    def test_every_board_word_reaches_the_page(self):
        cfg, board = committed()
        page = quiet(build.render, cfg, board)

        def want(text):
            self.assertIn(html.escape(text, quote=True), page)

        for value in board["page"].values():
            if isinstance(value, str):
                want(value)
        for link in board["page"]["links"]:
            want(link["label"])
            self.assertIn(f'href="{html.escape(link["href"], quote=True)}"', page)
        for section in board["sections"]:
            want(section["heading"])
            if section.get("note"):
                want(section["note"])
            for box in section["boxes"]:
                want(box["title"])
                if box.get("blurb"):
                    want(box["blurb"])
                for line in box["lines"]:
                    text = build.fill(line["text"], board)
                    want(text)
        for level in board["levels"]:
            want(level["label"])
            want(level["text"])
            want(level["mark"])
        for char in board["characters"]:
            want(char["name"])

    def test_every_board_item_is_rendered_once(self):
        cfg, board = committed()
        page = quiet(build.render, cfg, board)
        chars = board["characters"]
        self.assertEqual(page.count('<li class="tile'), len(chars))
        self.assertEqual(page.count('<li class="ship"'), sum(
            len(box["lines"]) for s in board["sections"] if s["kind"] == "shipped"
            for box in s["boxes"]))
        self.assertEqual(page.count('<section class="lv-group'), len(board["levels"]))
        for char in chars:
            self.assertEqual(page.count(f'>{html.escape(char["name"], quote=True)}</b>'), 1)

    def test_shipped_runs_newest_release_first(self):
        cfg, board = committed()
        page = quiet(build.render, cfg, board)
        shipped = [line for s in board["sections"] if s["kind"] == "shipped"
                   for box in s["boxes"] for line in box["lines"]]
        used = [tag for tag in board["releases"]
                if any(line.get("release") == tag for line in shipped)]
        first = [l for l in shipped if l.get("release") == used[0]][0]
        last = [l for l in shipped if l.get("release") == used[-1]][0]
        self.assertLess(page.index(html.escape(first["text"], quote=True)),
                        page.index(html.escape(last["text"], quote=True)))

    def test_static_page_has_no_external_resources(self):
        cfg, board = committed()
        page = quiet(build.render, cfg, board)
        self.assertNotIn("<script src", page)
        self.assertNotIn("<link rel=\"stylesheet\"", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<iframe", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("XMLHttpRequest", page)
        self.assertNotIn("fetch(", page)
        self.assertNotIn("http://", page)

    def test_links_point_at_the_board_and_github_releases_only(self):
        cfg, board = committed()
        page = quiet(build.render, cfg, board)
        repo = cfg["releases"]["repo"]
        allowed = {link["href"] for link in board["page"]["links"]}
        for href in re.findall(r'href="([^"]+)"', page):
            href = html.unescape(href)
            ok = (href.startswith("#")
                  or href in allowed
                  or href.startswith(f"https://github.com/{repo}/releases/tag/")
                  or href.startswith("data:image/svg+xml"))
            self.assertTrue(ok, f"unexpected link {href!r}")

    def test_visible_text_comes_from_the_board_and_config(self):
        """Nothing a visitor reads is written in build.py or in a template."""
        cfg = render_cfg()
        for key in cfg["ui"]:
            cfg["ui"][key] = "zu" + key.replace("_", "").lower()
        for i, entry in enumerate(cfg["legend"]):
            entry["text"] = f"zq{i}"
            entry["mark"] = f"zr{i}"
        board = {
            "schema": 2,
            "exported_at": "2026-01-02T03:04Z",
            "latest_release": {"tag": "zz9", "date": "2026-01-01"},
            "releases": ["zz9"],
            "stats": {"verifying_open": 5},
            "page": {"title": "zt1", "tagline": "zt2", "goal": "zt3", "notice": "zt4",
                     "footer": "zt5", "characters_heading": "zt6", "characters_banner": "zt7",
                     "links": [{"label": "zl1", "href": "https://example.invalid/x"}]},
            "sections": [
                {"id": "now", "heading": "zs1", "note": "zs2", "kind": "active", "boxes": [
                    {"id": 1, "title": "zb1", "blurb": "zs3", "size": "small", "lines": [
                        {"id": 11, "text": "zl2", "done": False, "confirming": False,
                         "not_confirming": False, "release": None, "date": None, "cards": []}]}]},
                {"id": "shipped", "heading": "zs4", "note": None, "kind": "shipped", "boxes": [
                    {"id": 2, "title": "zb2", "blurb": None, "size": None, "lines": [
                        {"id": 12, "text": "zl3", "done": True, "confirming": False,
                         "not_confirming": False, "release": "zz9", "date": None, "cards": []}]}]},
                {"id": "history", "heading": "zs5", "note": None, "kind": "background", "boxes": [
                    {"id": 3, "title": "zb3", "blurb": None, "size": None, "lines": [
                        {"id": 13, "text": "zl4", "done": False, "confirming": False,
                         "not_confirming": False, "release": None, "date": None, "cards": []}]}]},
            ],
            "characters": [{"id": 9, "name": "zc1", "ready": False,
                            "issues": [{"id": 1, "done": False, "type": "crash",
                                        "needs_repro": True}]}],
            "levels": [{"key": "crash", "mark": "zm1", "label": "zs6", "text": "zs7"}],
            "types": {"crash": "crash"},
        }
        page = quiet(build.render, cfg, board)

        def strings(obj):
            if isinstance(obj, dict):
                for value in obj.values():
                    yield from strings(value)
            elif isinstance(obj, list):
                for value in obj:
                    yield from strings(value)
            elif isinstance(obj, str):
                yield obj

        allowed = set(strings(board)) | set(strings(cfg))
        allowed.add(build.fmt_time(board["exported_at"]))
        text = re.sub(r"<script\b.*?</script>", " ", page, flags=re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]*>", " ", text)
        text = html.unescape(text)
        for chunk in sorted(allowed, key=len, reverse=True):
            if chunk:
                text = text.replace(chunk, " ")
        self.assertEqual(re.findall(r"[A-Za-z]{2,}", text), [])


class Cli(unittest.TestCase):
    def test_writes_the_page_and_prints_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "roadmap.json")
            board_path = os.path.join(tmp, "board.json")
            out = os.path.join(tmp, "site", "index.html")
            with open(cfg_path, "w") as f:
                json.dump(render_cfg(), f)
            with open(board_path, "w") as f:
                json.dump(snapshot(), f)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                build.main(["--config", cfg_path, "--board", board_path, "--out", out])
            self.assertTrue(buf.getvalue().startswith("ok: "))
            with open(out) as f:
                self.assertIn("<!doctype html>", f.read())


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
