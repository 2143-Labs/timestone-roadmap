import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
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


def board_tasks():
    """A tiny board: a roster of 50 characters plus the cards the fixture uses."""
    chars = [{"id": 100 + i, "title": f"Character: C{i}", "done": False, "labels": None, "description": "",
              "related_tasks": {"subtask": [{"id": 500 + i}]}} for i in range(50)]
    issues = [{"id": 500 + i, "title": "x", "done": False, "description": "**Type:** effect",
               "labels": [{"id": 17, "title": "Needs-Repro"}] if i == 0 else None, "related_tasks": {}}
              for i in range(50)]
    roster = {"id": 329, "title": "Roster", "done": False, "labels": None, "description": "",
              "related_tasks": {"subtask": [{"id": c["id"]} for c in chars]}}
    cards = [{"id": n, "title": "c", "done": n == 3, "labels": None, "description": "", "related_tasks": {}}
             for n in (1, 2, 3)]
    return [roster] + chars + issues + cards


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

    def test_state_open_beats_needs_repro(self):
        st, suffix = build.item_state("later", {"cards": [1], "state": "open"}, {"1": card(nr=True)})
        self.assertEqual((st, suffix), ("open", ""))

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

    def test_missing_latest_release_only_warns_by_default(self):
        content, board = fixture()
        board["latest_release"] = {"tag": "v2", "date": "2026-11-25"}
        board["releases"] = ["v1", "v2"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            page = build.render(content, board)
        self.assertIn("warn: latest release v2 is not listed in shipped", err.getvalue())
        self.assertEqual(page.count('class="tile '), 50)

    def test_missing_latest_release_fails_when_strict(self):
        content, board = fixture()
        board["latest_release"] = {"tag": "v2", "date": "2026-11-25"}
        board["releases"] = ["v1", "v2"]
        with self.assertRaises(SystemExit):
            quiet(build.render, content, board, True)

    def test_latest_release_in_timeline_is_enough(self):
        content, board = fixture()
        board["latest_release"] = {"tag": "v2", "date": "2026-11-25"}
        board["releases"] = ["v1", "v2"]
        content["timeline"].append({"date": "2026-11-25", "text": "v2: something"})
        quiet(build.render, content, board, True)


class Export(unittest.TestCase):
    def test_tasks_json_mode_with_prev_releases(self):
        content, _ = fixture()
        with tempfile.TemporaryDirectory() as d:
            paths = {k: os.path.join(d, k + ".json") for k in ("content", "tasks", "prev", "out")}
            for k, v in (("content", content), ("tasks", board_tasks()),
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

    def test_export_stores_etag_only_when_present(self):
        content, _ = fixture()
        args = (content, board_tasks(), 329, ["v1"], {"tag": "v1", "date": "2026-11-24"})
        self.assertEqual(export_board.export(*args, releases_etag='W/"x"')["releases_etag"], 'W/"x"')
        self.assertNotIn("releases_etag", export_board.export(*args))


class FetchReleases(unittest.TestCase):
    def _prev(self, d, payload):
        path = os.path.join(d, "prev.json")
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    def test_not_modified_reuses_previous_silently(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", None, None)

        with tempfile.TemporaryDirectory() as d:
            prev = self._prev(d, {"releases": ["v0.6.8"], "latest_release": {"tag": "v0.6.8", "date": "2026-11-24"},
                                  "releases_etag": 'W/"etag1"'})
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t0ken"}), \
                    mock.patch("urllib.request.urlopen", side_effect=fake), \
                    contextlib.redirect_stderr(err):
                tags, latest, etag = export_board.fetch_releases(prev)
        self.assertEqual(tags, ["v0.6.8"])
        self.assertEqual(latest, {"tag": "v0.6.8", "date": "2026-11-24"})
        self.assertEqual(etag, 'W/"etag1"')
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(seen.get("if-none-match"), 'W/"etag1"')
        self.assertEqual(seen.get("authorization"), "Bearer t0ken")

    def test_new_etag_is_captured(self):
        rel = [{"tag_name": "v0.6.9", "draft": False, "prerelease": False, "published_at": "2026-11-25T10:00:00Z"},
               {"tag_name": "v0.6.8", "draft": False, "prerelease": False, "published_at": "2026-11-24T10:00:00Z"}]
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(rel, 'W/"etag2"')), \
                contextlib.redirect_stderr(io.StringIO()):
            tags, latest, etag = export_board.fetch_releases(None)
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
                tags, latest, etag = export_board.fetch_releases(prev, tasks)
        self.assertIn("warn: GitHub releases unavailable", err.getvalue())
        self.assertEqual(tags, ["v0.6.8", "v0.6.9"])
        self.assertEqual(latest, {"tag": "v0.6.9", "date": "2026-11-25"})
        self.assertIsNone(etag)

    def test_fails_without_prev_when_github_fails(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(export_board.ExportError):
                export_board.fetch_releases(os.path.join(d, "missing.json"))


if __name__ == "__main__":
    unittest.main()
