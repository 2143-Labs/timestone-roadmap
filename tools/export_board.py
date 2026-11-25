#!/usr/bin/env python3
"""Export a trimmed board snapshot for the roadmap page.

Reads every task of a Vikunja project (through a read-only link share, or a
JSON dump with --tasks-json) plus the Timestone GitHub releases, and writes
only what build.py needs: done/needs-repro flags for the cards named in the
content file, the character grid, release tags and one count. No card titles
or descriptions leave this script, except character names.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

NEEDS_REPRO = 17
RELEASES_URL = "https://api.github.com/repos/tilderain/Timestone/releases?per_page=100"
TYPE_RE = re.compile(r"\*\*Type:\*\*\s*([a-z]+)")


class ExportError(Exception):
    pass


def _request(url, data=None, headers=None):
    h = {"User-Agent": "timestone-roadmap"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        path = re.sub(r"/shares/[^/]+", "/shares/<hash>", urllib.parse.urlsplit(url).path)
        raise ExportError(f"HTTP {e.code} from {path}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ExportError(f"{type(e).__name__}: {e}") from None


def fetch_share(base, hash_file, project):
    with open(hash_file) as f:
        share = f.read().strip()
    if not share:
        raise ExportError("empty share hash")
    token = _request(f"{base}/api/v2/shares/{urllib.parse.quote(share)}/auth", data={}).get("token")
    if not token:
        raise ExportError("share auth returned no token")
    auth = {"Authorization": f"Bearer {token}"}
    lists = []
    for flt in ("done = false", "done = true"):
        got, page = [], 1
        while True:
            q = urllib.parse.urlencode({"per_page": 50, "page": page, "format": "markdown", "filter": flt})
            r = _request(f"{base}/api/v2/projects/{project}/tasks?{q}", headers=auth)
            got += r.get("items") or []
            if page >= (r.get("total_pages") or 0):
                break
            page += 1
        lists.append(got)
    return lists[0], lists[1]


def fetch_releases(prev_path):
    try:
        rel = _request(RELEASES_URL, headers={"Accept": "application/vnd.github+json"})
        tags = [r["tag_name"] for r in rel]
        final = [r for r in rel if not r.get("draft") and not r.get("prerelease") and r.get("published_at")]
        if not final:
            raise ExportError("no published releases")
        newest = max(final, key=lambda r: r["published_at"])
        return tags, {"tag": newest["tag_name"], "date": newest["published_at"][:10]}
    except (ExportError, KeyError, TypeError) as e:
        if prev_path and os.path.exists(prev_path):
            with open(prev_path) as f:
                prev = json.load(f)
            print(f"warn: GitHub releases unavailable ({e}); reusing previous", file=sys.stderr)
            return prev["releases"], prev["latest_release"]
        raise ExportError(f"GitHub releases: {e}") from None


def content_cards(content):
    for s in content["sections"]:
        for g in s["groups"]:
            for it in g["items"]:
                yield from it.get("cards", [])


def subtask_ids(task):
    return [s["id"] for s in ((task.get("related_tasks") or {}).get("subtask") or [])]


def export(content, tasks, roster, releases, latest):
    idx = {t["id"]: t for t in tasks}
    nr = {t["id"] for t in tasks if not t["done"] and any(l["id"] == NEEDS_REPRO for l in (t.get("labels") or []))}
    cards = {}
    for cid in content_cards(content):
        if cid not in idx:
            raise ExportError(f"card {cid} not on board")
        cards[str(cid)] = {"done": bool(idx[cid]["done"]), "needs_repro": cid in nr}
    if roster not in idx:
        raise ExportError(f"roster card {roster} not on board")
    char_ids = subtask_ids(idx[roster])
    if len(char_ids) != 50:
        raise ExportError(f"roster has {len(char_ids)} characters, expected 50")
    characters = []
    for chid in char_ids:
        if chid not in idx:
            raise ExportError(f"character card {chid} not on board")
        ch = idx[chid]
        issues = []
        for iid in subtask_ids(ch):
            if iid not in idx:
                raise ExportError(f"issue card {iid} not on board")
            t = idx[iid]
            m = TYPE_RE.search(t.get("description") or "")
            issues.append({"id": iid, "done": bool(t["done"]), "type": m.group(1) if m else None,
                           "needs_repro": iid in nr})
        characters.append({"id": chid, "name": ch["title"].removeprefix("Character: "), "issues": issues})
    return {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "latest_release": latest,
        "releases": releases,
        "stats": {"verifying_open": len(nr)},
        "cards": cards,
        "characters": characters,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--content", required=True)
    ap.add_argument("--out", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--share-hash-file")
    src.add_argument("--tasks-json")
    ap.add_argument("--base-url", default="https://tasks.2143.me")
    ap.add_argument("--project", type=int, default=19)
    ap.add_argument("--roster", type=int, default=329)
    ap.add_argument("--prev")
    ap.add_argument("--min-tasks", type=int, default=500)
    a = ap.parse_args(argv)
    try:
        with open(a.content) as f:
            content = json.load(f)
        if a.tasks_json:
            with open(a.tasks_json) as f:
                tasks = json.load(f)
            open_t = [t for t in tasks if not t["done"]]
            done_t = [t for t in tasks if t["done"]]
        else:
            open_t, done_t = fetch_share(a.base_url.rstrip("/"), a.share_hash_file, a.project)
        if not open_t or not done_t:
            raise ExportError(f"got {len(open_t)} open and {len(done_t)} done tasks; share scope wrong?")
        tasks = open_t + done_t
        if len(tasks) < a.min_tasks:
            raise ExportError(f"only {len(tasks)} tasks (< {a.min_tasks}); share scope wrong?")
        releases, latest = fetch_releases(a.prev)
        snap = export(content, tasks, a.roster, releases, latest)
    except (ExportError, OSError, ValueError, KeyError) as e:
        print(f"export failed: {e}", file=sys.stderr)
        sys.exit(1)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, a.out)


if __name__ == "__main__":
    main()
