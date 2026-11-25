import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402
import export_board  # noqa: E402

CFG = {
    "heading": "Characters",
    "banner": "Unverified.",
    "levels": [
        {"key": k, "mark": m, "label": k.title(), "text": k}
        for k, m in (("ready", "●"), ("playable", "◐"), ("desync", "▲"), ("crash", "■"), ("untested", "○"))
    ],
    "skip_cards": [99],
    "type_rank": {"crash": "crash", "freeze": "crash", "desync": "desync", "effect": "playable", "verify": None},
    "verified_ready": [],
}


def card(done=False, nr=False):
    return {"done": done, "needs_repro": nr}


def char(cid, issues):
    return {"id": cid, "name": f"C{cid:02d}", "issues": issues}


def issue(iid, t, done=False, nr=False):
    return {"id": iid, "type": t, "done": done, "needs_repro": nr}


def fixture():
    content = {
        "title": "T", "tagline": "tag", "goal": "goal", "notice": "notice",
        "links": [{"label": "Discord", "href": "https://example.invalid/"}],
        "legend": [{"mark": "▶", "class": "now", "text": "now"}],
        "sizes": {"S": "small"},
        "sections": [
            {"id": "now", "heading": "Now", "note": "n", "groups": [
                {"title": "G", "size": "S", "blurb": "b", "items": [
                    {"text": "{verifying_open} awaiting", "cards": [1], "state": "verifying"},
                    {"text": "a", "cards": [2]}]}]},
            {"id": "not-planned", "heading": "No", "groups": [
                {"title": "No", "items": [{"text": "x", "cards": [3]}]}]},
        ],
        "shipped": [{"title": "S", "items": [{"text": "s", "version": "v1"}, {"text": "d", "date": "2026-01-01"}]}],
        "timeline": [{"date": "2026-01-01", "text": "t"}],
        "caveats": ["c"],
        "characters": CFG,
        "footer": "f",
    }
    board = {
        "exported_at": "2026-11-25T05:30Z",
        "latest_release": {"tag": "v1", "date": "2026-11-24"},
        "releases": ["v1"],
        "stats": {"verifying_open": 7},
        "cards": {"1": card(), "2": card(), "3": card(done=True)},
        "characters": [char(i, [issue(1000 + i, "effect")]) for i in range(50)],
    }
    return content, board


def quiet(fn, *a):
    with contextlib.redirect_stderr(io.StringIO()):
        return fn(*a)


class ItemState(unittest.TestCase):
    def test_missing_card_fails(self):
        with self.assertRaises(SystemExit):
            quiet(build.item_state, "now", {"cards": [5]}, {})

    def test_all_done(self):
        self.assertEqual(build.item_state("next", {"cards": [1, 2]}, {"1": card(True), "2": card(True)})[0], "done")

    def test_needs_repro_is_verifying(self):
        st, suffix = build.item_state("next", {"cards": [1]}, {"1": card(nr=True)})
        self.assertEqual(st, "verifying")
        self.assertIn("confirming", suffix)

    def test_state_now_beats_needs_repro(self):
        self.assertEqual(build.item_state("next", {"cards": [1], "state": "now"}, {"1": card(nr=True)})[0], "now")

    def test_not_planned_is_no(self):
        self.assertEqual(build.item_state("not-planned", {"cards": [1]}, {"1": card(True)})[0], "no")

    def test_defaults(self):
        self.assertEqual(build.item_state("now", {"cards": [1]}, {"1": card()})[0], "now")
        self.assertEqual(build.item_state("later", {"cards": [1]}, {"1": card()})[0], "open")


class CharacterLevel(unittest.TestCase):
    def test_freeze_is_crash(self):
        self.assertEqual(build.character_level(char(1, [issue(1, "freeze"), issue(2, "effect")]), CFG)[0], "crash")

    def test_effect_only_is_playable(self):
        self.assertEqual(build.character_level(char(1, [issue(1, "effect")]), CFG)[0], "playable")

    def test_skipped_or_verify_is_untested(self):
        c = char(1, [issue(99, "crash"), issue(2, "verify"), issue(3, "crash", done=True)])
        self.assertEqual(build.character_level(c, CFG), ("untested", 0, 1, 0))

    def test_unknown_type_fails(self):
        with self.assertRaises(SystemExit):
            quiet(build.character_level, char(1, [issue(1, "weird")]), CFG)

    def test_verified_ready(self):
        cfg = dict(CFG, verified_ready=[1])
        self.assertEqual(build.character_level(char(1, [issue(1, "crash")]), cfg)[0], "ready")


class Render(unittest.TestCase):
    def test_deterministic_and_clean(self):
        content, board = fixture()
        a, b = build.render(content, board), build.render(content, board)
        self.assertEqual(a, b)
        self.assertEqual(a.count('class="tile '), 50)
        self.assertIn("7 awaiting", a)
        self.assertIn("Last updated 2026-11-25 05:30 UTC", a)

    def test_unknown_token_fails(self):
        content, board = fixture()
        content["sections"][0]["groups"][0]["items"][1]["text"] = "{secret}"
        with self.assertRaises(SystemExit):
            quiet(build.render, content, board)

    def test_unknown_release_fails(self):
        content, board = fixture()
        content["shipped"][0]["items"][0]["version"] = "v9"
        with self.assertRaises(SystemExit):
            quiet(build.render, content, board)

    def test_forbidden_text_fails(self):
        content, board = fixture()
        content["caveats"] = ["Impact: high"]
        with self.assertRaises(SystemExit):
            quiet(build.render, content, board)


class Export(unittest.TestCase):
    def test_tasks_json_mode_with_prev_releases(self):
        content, _ = fixture()
        chars = [{"id": 100 + i, "title": f"Character: C{i}", "done": False, "labels": None, "description": "",
                  "related_tasks": {"subtask": [{"id": 500 + i}]}} for i in range(50)]
        issues = [{"id": 500 + i, "title": "x", "done": False, "description": "**Type:** effect",
                   "labels": [{"id": 17, "title": "Needs-Repro"}] if i == 0 else None, "related_tasks": {}}
                  for i in range(50)]
        roster = {"id": 329, "title": "Roster", "done": False, "labels": None, "description": "",
                  "related_tasks": {"subtask": [{"id": c["id"]} for c in chars]}}
        cards = [{"id": n, "title": "c", "done": n == 3, "labels": None, "description": "", "related_tasks": {}}
                 for n in (1, 2, 3)]
        with tempfile.TemporaryDirectory() as d:
            paths = {k: os.path.join(d, k + ".json") for k in ("content", "tasks", "prev", "out")}
            for k, v in (("content", content), ("tasks", [roster] + chars + issues + cards),
                         ("prev", {"releases": ["v1"], "latest_release": {"tag": "v1", "date": "2026-11-24"}})):
                with open(paths[k], "w") as f:
                    json.dump(v, f)
            with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                    contextlib.redirect_stderr(io.StringIO()):
                export_board.main(["--content", paths["content"], "--tasks-json", paths["tasks"],
                                   "--prev", paths["prev"], "--out", paths["out"], "--min-tasks", "1"])
            with open(paths["out"]) as f:
                out = json.load(f)
        self.assertEqual(sorted(out), ["cards", "characters", "exported_at", "latest_release", "releases", "stats"])
        self.assertEqual(out["stats"]["verifying_open"], 1)
        self.assertEqual(out["releases"], ["v1"])
        self.assertEqual(len(out["characters"]), 50)
        self.assertEqual(out["characters"][0]["issues"][0], {"id": 500, "done": False, "type": "effect", "needs_repro": True})
        self.assertTrue(out["cards"]["3"]["done"])


if __name__ == "__main__":
    unittest.main()
