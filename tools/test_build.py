import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from html.parser import HTMLParser
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
    "legend": [{"class": "now", "text": "now"}, {"class": "verifying", "text": "confirming"},
               {"class": "done", "text": "shipped"}, {"class": "no", "text": "no"}],
    "words": {"updated": "Last updated {date} {time} UTC", "latest_release": "latest release {tag} ({date})",
              "size": "{size} job", "in_release": "(in {tag}, confirming)",
              "fix_released": "(fix released, confirming)", "open": "{n} open", "fixed": "{n} fixed",
              "awaiting": "{n} awaiting confirmation"},
}
W = CFG["words"]

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
        self.assertEqual(build.line_state("not_planned", self.line(done=True), set(), W), ("no", ""))

    def test_rule2_done_by_flag(self):
        self.assertEqual(build.line_state("upcoming", self.line(done=True), set(), W), ("done", ""))

    def test_rule2_done_when_all_cards_done(self):
        st = build.line_state("active", self.line(cards=[{"done": True, "needs_repro": False}]), set(), W)
        self.assertEqual(st, ("done", ""))

    def test_rule3_confirming_has_no_suffix(self):
        self.assertEqual(build.line_state("upcoming", self.line(confirming=True), set(), W),
                         ("verifying", ""))

    def test_rule4_not_confirming_beats_needs_repro_active(self):
        st = build.line_state("active", self.line(not_confirming=True,
                                                  cards=[{"done": False, "needs_repro": True}]), set(), W)
        self.assertEqual(st, ("now", ""))

    def test_rule4_not_confirming_beats_needs_repro_upcoming(self):
        st = build.line_state("upcoming", self.line(not_confirming=True,
                                                    cards=[{"done": False, "needs_repro": True}]), set(), W)
        self.assertEqual(st, ("open", ""))

    def test_rule5_release_in_list(self):
        st = build.line_state("upcoming", self.line(release="v1"), {"v1"}, W)
        self.assertEqual(st, ("verifying", "(in v1, confirming)"))

    def test_rule5_unknown_release_warns_and_falls_through(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = build.line_state("upcoming", self.line(release="v9"), {"v1"}, W)
        self.assertEqual(st, ("open", ""))
        self.assertIn("warn:", err.getvalue())

    def test_rule5_unknown_release_falls_through_to_needs_repro(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            st = build.line_state("upcoming", self.line(release="v9",
                                                        cards=[{"done": False, "needs_repro": True}]), {"v1"}, W)
        self.assertEqual(st, ("verifying", "(fix released, confirming)"))

    def test_rule6_needs_repro(self):
        st = build.line_state("upcoming", self.line(cards=[{"done": False, "needs_repro": True}]), set(), W)
        self.assertEqual(st, ("verifying", "(fix released, confirming)"))

    def test_rule7_fallback(self):
        self.assertEqual(build.line_state("active", self.line(), set(), W), ("now", ""))
        self.assertEqual(build.line_state("upcoming", self.line(), set(), W), ("open", ""))
        self.assertEqual(build.line_state("upcoming", self.line(cards=[{"done": True,
                                                                       "needs_repro": False},
                                                                      {"done": False,
                                                                       "needs_repro": False}]), set(), W),
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


class Render(unittest.TestCase):
    def test_deterministic(self):
        cfg, board = render_cfg(), snapshot()
        self.assertEqual(build.render(cfg, board), build.render(cfg, board))

    def test_markup_and_counts(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('<span class="size">small job</span>', page)
        self.assertIn('<li class="ln s-verifying">7 awaiting</li>', page)
        self.assertIn('<li class="ln s-no">x</li>', page)
        self.assertIn('<h2>Now <small>(n)</small></h2>', page)
        self.assertIn('<a href="https://github.com/o/r/releases/tag/v1">v1</a>', page)
        self.assertIn('<h3><time datetime="2026-01-01">2026-01-01</time></h3>', page)
        self.assertIn('<li class="ln s-done ent ar0">d <span class="area">S</span></li>', page)
        self.assertIn('<ol class="tl">', page)
        self.assertIn('<li><time datetime="2026-01-01">2026-01-01</time><span>t</span></li>', page)
        self.assertIn('Last updated 2026-11-25 05:30 UTC · '
                      '<a href="#rel_v1">latest release v1 (2026-11-24)</a>', page)
        self.assertIn('<li class="rel" id="rel_v1">', page)
        self.assertEqual(page.count('class="tile '), 2)
        self.assertIn('<small>1 open · 0 fixed</small>', page)
        self.assertIn('<p class="banner">banner</p>', page)
        self.assertIn("<p>f</p></div>\n</footer>", page)
        self.assertNotIn("<script", page)
        # body order: characters, lanes, not planned, shipped, background
        for a, b in (('id="characters"', 'id="now"'), ('id="now"', 'id="not-planned"'),
                     ('id="not-planned"', 'id="shipped"'), ('id="shipped"', 'id="history"')):
            self.assertLess(page.index(a), page.index(b))

    def test_jump_bar_counts_every_block(self):
        page = quiet(build.render, render_cfg(), snapshot())
        nav = page[page.index('<nav class="jump">'):page.index("</nav>", page.index('<nav class="jump">'))]
        for href, label in (("characters", "Characters <b>2</b>"), ("now", "Now <b>2</b>"),
                            ("not-planned", "No <b>1</b>"), ("shipped", "Shipped <b>2</b>"),
                            ("history", "Background</span>")):
            self.assertIn(f'<a href="#{href}"><span>{label}', nav)

    def test_lanes_follow_column_order(self):
        cfg, board = render_cfg(), snapshot()
        later = json.loads(json.dumps(board["sections"][0]))
        later.update(id="later", heading="Later", note=None, kind="upcoming")
        board["sections"].insert(1, later)
        nxt = json.loads(json.dumps(later))
        nxt.update(id="next", heading="Next")
        board["sections"].insert(1, nxt)
        page = quiet(build.render, cfg, board)
        lanes = page[page.index('<div class="lanes">'):page.index('id="not-planned"')]
        self.assertLess(lanes.index('id="now" class="lane active"'), lanes.index('id="next" class="lane upcoming"'))
        self.assertLess(lanes.index('id="next"'), lanes.index('id="later"'))
        # the fallback state follows the column kind
        self.assertIn('<li class="ln s-open">a</li>', lanes)
        self.assertIn('<li class="ln s-now">a</li>', lanes)

    def test_state_filters_only_for_states_on_the_plan(self):
        page = quiet(build.render, render_cfg(), snapshot())
        # now, verifying and no occur on the plan; done does not, so it is a plain legend entry
        self.assertIn('<input type="checkbox" id="stf_0" class="stf sr"><label for="stf_0" '
                      'class="chip st s-now">now <b>1</b></label>', page)
        self.assertIn('<label for="stf_1" class="chip st s-verifying">confirming <b>1</b></label>', page)
        self.assertIn('<label for="stf_2" class="chip st s-no">no <b>1</b></label>', page)
        self.assertIn('<span class="chip st s-done">shipped</span>', page)
        self.assertIn(".plan:has(.stf:checked) .ln{display:none}", page)
        self.assertIn(".plan:has(#stf_1:checked) .ln.s-verifying{display:list-item}", page)
        self.assertIn(".plan:has(#stf_1:checked) .box:has(.ln.s-verifying){display:block}", page)
        self.assertIn(".plan:has(#stf_2:checked) .np:has(.ln.s-no){display:block}", page)
        self.assertNotIn("#stf_3", page)

    def test_level_filters_skip_empty_levels(self):
        page = quiet(build.render, render_cfg(), snapshot())
        # both characters are playable (index 1); the other levels show but do not filter
        self.assertIn('<input type="checkbox" id="lvf_1" class="lvf sr">', page)
        self.assertNotIn('id="lvf_0"', page)
        self.assertIn('<div class="lv l-ready"><b class="n">0</b>', page)
        self.assertIn(".chars:has(#lvf_1:checked) .tile.lv1{display:flex}", page)
        self.assertEqual(page.count('class="tile l-playable lv1"'), 2)

    def test_area_filters_cover_shipped_boxes(self):
        page = quiet(build.render, render_cfg(), snapshot())
        self.assertIn('<input type="checkbox" id="arf_0" class="arf sr"><label for="arf_0" '
                      'class="chip">S <b>2</b></label>', page)
        self.assertIn(".ship:has(#arf_0:checked) .rel:has(.ent.ar0){display:grid}", page)
        self.assertNotIn('class="notes"', page)

    def test_shipped_box_blurb_and_size_stay_on_the_page(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][2]["boxes"][0].update(blurb="how it went", size="large")
        page = quiet(build.render, cfg, board)
        self.assertIn("<dt>S</dt><dd>how it went · large job</dd>", page)

    def test_release_without_repo_is_not_linked(self):
        cfg, board = copy.deepcopy(render_cfg()), snapshot()
        cfg.pop("releases")
        page = quiet(build.render, cfg, board)
        self.assertIn("<h3><span>v1</span>", page)
        self.assertNotIn("github.com", page)

    def test_done_line_on_the_plan_warns_and_stays(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][0]["boxes"][0]["lines"][1]["done"] = True
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(cfg, board)
        self.assertIn("warn: line 12 is all done; move it to shipped", err.getvalue())
        self.assertIn('<li class="ln s-done">a</li>', page)
        # no line is "now" any more, so the chips are verifying, done, no
        self.assertIn('<span class="chip st s-now">now</span>', page)
        self.assertIn('id="stf_1" class="stf sr"><label for="stf_1" class="chip st s-done">', page)

    def test_history_with_an_undated_line_is_a_plain_list(self):
        cfg, board = render_cfg(), snapshot()
        board["sections"][3]["boxes"][0]["lines"].append(
            {"id": 17, "text": "no date here", "done": False, "confirming": False,
             "not_confirming": False, "release": None, "date": None, "cards": []})
        page = quiet(build.render, cfg, board)
        self.assertNotIn('<ol class="tl">', page)
        self.assertIn("<li>2026-01-01: t</li>", page)
        self.assertIn("<li>no date here</li>", page)

    def test_card_ids_never_reach_the_page(self):
        cfg, board = render_cfg(), snapshot()
        n = 987000
        for s in board["sections"]:
            for box in s["boxes"]:
                n += 1
                box["id"] = n
                for line in box["lines"]:
                    n += 1
                    line["id"] = n
        for c in board["characters"]:
            n += 1
            c["id"] = n
            for issue in c["issues"]:
                n += 1
                issue["id"] = n
        page = quiet(build.render, cfg, board)
        self.assertNotIn("987", page)

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
            page = build.render(cfg, board)
        self.assertIn("warn: latest release v2 is not listed in shipped", err.getvalue())
        # no changelog entry to point at, so the stamp is plain text
        self.assertIn("· latest release v2 (2026-11-25)</p>", page)

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

    def test_optional_page_keys_absent(self):
        cfg, board = render_cfg(), snapshot()
        for k in ("tagline", "goal", "notice", "footer", "links", "characters_banner"):
            board["page"].pop(k)
        page = quiet(build.render, cfg, board)
        for absent in ('class="links"', "<footer>", 'class="goal"', 'class="notice"',
                       'class="banner"', 'class="tagline"'):
            self.assertNotIn(absent, page)

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
        self.assertNotIn(".chars:has", page)

    def test_schema_must_be_2(self):
        cfg, board = render_cfg(), snapshot()
        board["schema"] = 1
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, board)


class ConfigWords(unittest.TestCase):
    def fails(self, cfg):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            build.render(cfg, snapshot())
        return str(cm.exception)

    def test_words_block_required(self):
        cfg = copy.deepcopy(CFG)
        cfg.pop("words")
        self.assertIn("no 'words' block", self.fails(cfg))

    def test_each_word_required(self):
        cfg = copy.deepcopy(CFG)
        del cfg["words"]["awaiting"]
        cfg["words"]["size"] = "  "
        self.assertIn("config words missing: size, awaiting", self.fails(cfg))

    def test_unknown_field_fails(self):
        cfg = copy.deepcopy(CFG)
        cfg["words"]["open"] = "{count} open"
        self.assertIn("words.open is malformed", self.fails(cfg))

    def test_words_are_used_verbatim(self):
        cfg = copy.deepcopy(CFG)
        cfg["words"].update(open="offen: {n}", size="{size}!")
        page = quiet(build.render, cfg, snapshot())
        self.assertIn("offen: 1", page)
        self.assertIn('<span class="size">small!</span>', page)

    def test_legend_class_must_be_plain(self):
        cfg = copy.deepcopy(CFG)
        cfg["legend"].append({"class": "x{}", "text": "bad"})
        self.assertIn("legend class", self.fails(cfg))


class Changelog(unittest.TestCase):
    def test_release_then_date_then_rest(self):
        def e(release=None, date=None, text="t"):
            return {"release": release, "date": date, "text": text, "area": 0}
        got = build.changelog([e("v0.6.9", text="a"), e(date="2026-01-01"), e(), e("v0.6.10"),
                               e("v1.0"), e(date="2026-02-01"), e("v0.2.7-dev-pr1"),
                               e("v0.6.9", text="b")])
        self.assertEqual([(k, key, [x["text"] for x in g]) for k, key, g in got],
                         [("release", "v1.0", ["t"]), ("release", "v0.6.10", ["t"]),
                          ("release", "v0.6.9", ["a", "b"]), ("release", "v0.2.7-dev-pr1", ["t"]),
                          ("date", "2026-02-01", ["t"]), ("date", "2026-01-01", ["t"]),
                          ("rest", "", ["t"])])

    def test_empty(self):
        self.assertEqual(build.changelog([]), [])


class IssueMix(unittest.TestCase):
    def test_counts_open_counted_issues_per_level(self):
        char = {"issues": [{"done": False, "type": "crash"}, {"done": False, "type": "freeze"},
                           {"done": True, "type": "crash"}, {"done": False, "type": "effect"},
                           {"done": False, "type": "verify"}, {"done": False, "type": None}]}
        self.assertEqual(build.issue_mix(char, TYPES), {"crash": 2, "playable": 1})


# --- the committed page ------------------------------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_inputs():
    with open(os.path.join(REPO, "config", "roadmap.json")) as f:
        cfg = json.load(f)
    with open(os.path.join(REPO, "data", "board.json")) as f:
        board = json.load(f)
    return cfg, board


class VisibleText(HTMLParser):
    """What a visitor can read: text outside <style>/<script>, the tab title and
    the attributes browsers show (title, alt, placeholder, aria-label)."""

    SHOWN_ATTRS = ("title", "alt", "placeholder", "aria-label")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
        self.parts += [v for k, v in attrs if k in self.SHOWN_ATTRS and v]

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def visible_text(page):
    p = VisibleText()
    p.feed(page)
    p.close()
    return "".join(p.parts)


def unworded(text):
    """`text` with every run of letters replaced by '#', keeping {fields}."""
    return re.sub(r"\{[^{}]*\}|[^\W\d_]+",
                  lambda m: m.group(0) if m.group(0).startswith("{") else "#", text)


class CommittedPage(unittest.TestCase):
    def test_baked_page_matches_the_build(self):
        cfg, board = repo_inputs()
        page = quiet(build.render, cfg, board)
        with open(os.path.join(REPO, "site", "index.html")) as f:
            self.assertEqual(f.read(), page, "run python3 tools/build.py and commit site/index.html")

    def test_every_visible_word_comes_from_the_board_or_the_config(self):
        """Strip every letter from the inputs: any letter left on the page is prose in build.py."""
        cfg, board = repo_inputs()
        cfg["legend"] = [dict(item, text=unworded(item["text"])) for item in cfg["legend"]]
        cfg["words"] = {k: unworded(v) for k, v in cfg["words"].items()}
        page_text = board["page"]
        for k, v in list(page_text.items()):
            if isinstance(v, str):
                page_text[k] = unworded(v)
        page_text["links"] = [dict(l, label=unworded(l["label"])) for l in page_text.get("links", [])]
        tags = {t: unworded(t) for t in board["releases"]}
        board["releases"] = list(tags.values())
        if board.get("latest_release"):
            board["latest_release"]["tag"] = unworded(board["latest_release"]["tag"])
        for s in board["sections"]:
            s["heading"] = unworded(s["heading"])
            s["note"] = s["note"] and unworded(s["note"])
            for box in s["boxes"]:
                for k in ("title", "blurb", "size"):
                    box[k] = box[k] and unworded(box[k])
                for line in box["lines"]:
                    line["text"] = unworded(line["text"])
                    line["release"] = line["release"] and unworded(line["release"])
        for c in board["characters"]:
            c["name"] = unworded(c["name"])
        board["levels"] = [dict(lv, mark=unworded(lv["mark"]), label=unworded(lv["label"]),
                                text=unworded(lv["text"])) for lv in board["levels"]]
        text = visible_text(quiet(build.render, cfg, board))
        left = re.findall(r"[^\W\d_]+", text)
        self.assertEqual(left, [], f"words on the page that came from neither input: {left[:10]}")

    def test_everything_on_the_board_is_on_the_page(self):
        cfg, board = repo_inputs()
        text = visible_text(quiet(build.render, cfg, board))
        p = board["page"]
        want = [p[k] for k in ("title", "tagline", "goal", "notice", "footer", "characters_heading",
                               "characters_banner")]
        want += [l["label"] for l in p["links"]]
        want += [v for lv in board["levels"] for v in (lv["mark"], lv["label"], lv["text"])]
        want += [c["name"] for c in board["characters"]]
        want += [item["text"] for item in cfg["legend"]]
        want.append(board["latest_release"]["tag"])
        for s in board["sections"]:
            want += [s["heading"]] + ([s["note"]] if s["note"] else [])
            for box in s["boxes"]:
                want += [box["title"]] + ([box["blurb"]] if box["blurb"] else [])
                if box["size"]:
                    want.append(build.say(cfg["words"], "size", size=box["size"]))
                for line in box["lines"]:
                    t = build.fill(line["text"], board)
                    m = build.HISTORY_RE.match(t) if s["kind"] == "background" else None
                    want += [m.group(1), m.group(2)] if m else [t]
                    want += [line["release"]] if line["release"] else []
                    want += [line["date"]] if line["date"] else []
        missing = [x for x in want if x not in text]
        self.assertEqual(missing, [])
        # one tile per character, and the characters block leads
        page = quiet(build.render, cfg, board)
        self.assertEqual(page.count('<li class="tile '), len(board["characters"]))
        self.assertEqual(len(re.findall(r'<li class="ln s-done ent ar\d+">', page)),
                         sum(len(b["lines"]) for s in board["sections"] if s["kind"] == "shipped"
                             for b in s["boxes"]))


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
