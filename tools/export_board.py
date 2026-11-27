#!/usr/bin/env python3
"""Export a trimmed board snapshot for the roadmap page.

Reads a Vikunja project through a read-only link share (or a JSON dump with
--board-json) plus the GitHub releases named in the config, and writes only
what build.py needs. The structure and every word readers see are cards on the
board: a box is a card with the box label in a published bucket, its lines are
its published subtasks, page text is a card in the page bucket.

Card titles and descriptions of box/line/page cards therefore leave this
script (they are the page text); nothing else does, except character names and
the nested card flags build.py renders.
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

MARKUP_RE = re.compile(r"\*\*|__|`|\]\(|<[A-Za-z/!]|^\s*(?:#|[-*+] |\d+\. |>)", re.M)
MD_ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
URL_RE = re.compile(r"https://[^\s<>()\[\]\"']+")
PLAIN_PAGE_KEYS = ("title", "tagline", "goal", "notice", "footer")


class ExportError(Exception):
    pass


def _request_meta(url, data=None, headers=None):
    """Request `url`; return (parsed json, etag).

    The json is None when the server answers 304 Not Modified, so callers that
    send If-None-Match can reuse what they already have.
    """
    h = {"User-Agent": "board-roadmap"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            rh = getattr(r, "headers", None)
            return json.load(r), (rh.get("ETag") if rh is not None else None)
    except urllib.error.HTTPError as e:
        try:
            if e.code == 304:
                rh = getattr(e, "headers", None)
                return None, (rh.get("ETag") if rh is not None else None)
        finally:
            e.close()
        path = re.sub(r"/shares/[^/]+", "/shares/<hash>", urllib.parse.urlsplit(url).path)
        raise ExportError(f"HTTP {e.code} from {path}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ExportError(f"{type(e).__name__}: {e}") from None


def _request(url, data=None, headers=None):
    return _request_meta(url, data=data, headers=headers)[0]


def _pages(base, path, params, auth):
    """Every item of one paginated listing."""
    got, page = [], 1
    while True:
        q = urllib.parse.urlencode(dict(params, page=page))
        r = _request(f"{base}{path}?{q}", headers=auth)
        got += r.get("items") or []
        if page >= (r.get("total_pages") or 0):
            break
        page += 1
    return got


def fetch_share(base, hash_file, cfg):
    """Return {"tasks", "box_view", "line_view"} for the configured project."""
    with open(hash_file) as f:
        share = f.read().strip()
    if not share:
        raise ExportError("empty share hash")
    token = _request(f"{base}/api/v2/shares/{urllib.parse.quote(share)}/auth", data={}).get("token")
    if not token:
        raise ExportError("share auth returned no token")
    auth = {"Authorization": f"Bearer {token}"}
    project = cfg["project"]
    tasks_path = f"/api/v2/projects/{project}/tasks"
    plain = {"per_page": 50, "format": "markdown"}
    open_t = _pages(base, tasks_path, dict(plain, filter="done = false"), auth)
    done_t = _pages(base, tasks_path, dict(plain, filter="done = true"), auth)
    boxes = _pages(base, f"/api/v2/projects/{project}/views/{cfg['views']['boxes']}/tasks",
                   dict(plain, sort_by="position", order_by="asc", expand="buckets"), auth)
    lines = _pages(base, f"/api/v2/projects/{project}/views/{cfg['views']['lines']}/tasks",
                   dict(plain, sort_by="position", order_by="asc"), auth)
    return {"tasks": open_t + done_t, "box_view": boxes, "line_view": lines}


def plain(md):
    """The text `md` when it is plain prose, else None (markup would render wrong)."""
    if not md:
        return None
    if MARKUP_RE.search(md):
        return None
    return MD_ESC.sub(r"\1", re.sub(r"\s+", " ", md).strip()) or None


def _version_key(tag):
    m = re.fullmatch(r"v(\d+(?:\.\d+)*)", tag or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _is_newer(tag, date, latest):
    """True when (tag, date) is newer than the latest_release dict `latest`."""
    key, cur = _version_key(tag), _version_key((latest or {}).get("tag"))
    old_date = (latest or {}).get("date") or ""
    if key is not None and cur is not None:
        return key > cur or (key == cur and date > old_date)
    return date > old_date


def board_release(tasks, prefix):
    """Newest done `<prefix>v*` card on the board, as (tag, date) or None.

    Vikunja is the fallback source of truth when GitHub is rate limited.
    """
    best = None
    for t in tasks or []:
        title = (t.get("title") or "").strip()
        if not t.get("done") or not title.startswith(prefix + "v"):
            continue
        tag = title[len(prefix):].split()[0].rstrip(":,;")
        if _version_key(tag) is None:
            continue
        date = str(t.get("end_date") or t.get("start_date") or "")[:10]
        if best is None or _is_newer(tag, date, {"tag": best[0], "date": best[1]}):
            best = (tag, date)
    return best


def fetch_releases(rel_cfg, prev_path, tasks=None):
    """Return (tags, latest_release, etag) from GitHub, with fallbacks.

    Sends GITHUB_TOKEN when it is set and If-None-Match when the previous
    snapshot carries an ETag, so an unchanged release list costs no rate limit.
    On a 304 the previous snapshot is reused silently. On any other failure the
    previous snapshot is reused with a warning, and a newer release card on the
    board (Vikunja #1170 and friends) is merged in.
    """
    prev = None
    if prev_path and os.path.exists(prev_path):
        try:
            with open(prev_path) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    if not isinstance(prev, dict):
        prev = None
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    etag = (prev or {}).get("releases_etag")
    if etag:
        headers["If-None-Match"] = etag
    url = f"https://api.github.com/repos/{rel_cfg['repo']}/releases?per_page=100"
    try:
        rel, got_etag = _request_meta(url, headers=headers)
        if rel is None:  # 304 Not Modified: nothing changed, no warning
            if not prev or "releases" not in prev or "latest_release" not in prev:
                raise ExportError("HTTP 304 without a usable previous snapshot")
            return prev["releases"], prev["latest_release"], got_etag or etag
        tags = [r["tag_name"] for r in rel]
        final = [r for r in rel if not r.get("draft") and not r.get("prerelease") and r.get("published_at")]
        if not final:
            raise ExportError("no published releases")
        newest = max(final, key=lambda r: r["published_at"])
        return tags, {"tag": newest["tag_name"], "date": newest["published_at"][:10]}, got_etag
    except (ExportError, KeyError, TypeError, AttributeError) as e:
        if not prev or not prev.get("releases"):
            raise ExportError(f"GitHub releases: {e}") from None
        print(f"warn: GitHub releases unavailable ({e}); reusing previous", file=sys.stderr)
        tags = list(prev["releases"])
        latest = dict(prev.get("latest_release") or {"tag": tags[0], "date": ""})
        newer = board_release(tasks, rel_cfg["card_prefix"])
        if newer:
            tag, date = newer
            if tag not in tags:
                tags.append(tag)
            if _is_newer(tag, date, latest):
                latest = {"tag": tag, "date": date or latest.get("date", "")}
        return tags, latest, prev.get("releases_etag")


def label_ids(task):
    return {l["id"] for l in (task.get("labels") or [])}


def subtask_ids(task):
    return [s["id"] for s in ((task.get("related_tasks") or {}).get("subtask") or [])]


def _heading_note(title):
    m = re.fullmatch(r"(.*?)\s*(\([^()]*\))?", (title or "").strip())
    return (m.group(1), m.group(2)) if m else (title or "", None)


def _section_id(heading, bucket_id, used):
    sid = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    if not sid or sid in used or sid in ("shipped", "history", "characters"):
        sid = f"{sid}-{bucket_id}" if sid else f"section-{bucket_id}"
    used.add(sid)
    return sid


def export(cfg, data, releases, latest, releases_etag=None):
    views, buckets_cfg, labels = cfg["views"], cfg["buckets"], cfg["labels"]
    box_label, line_label = labels["box"], labels["line"]
    rel_cfg = cfg.get("releases")
    needs_repro = labels.get("needs_repro")
    sizes = {int(k): v for k, v in (labels.get("sizes") or {}).items()}
    release_title = re.compile(re.escape(rel_cfg["card_prefix"]) + r"(v\d+(?:\.\d+)*)\b") if rel_cfg else None
    idx = {t["id"]: t for t in data["tasks"]}
    nr = {i for i, t in idx.items() if not t["done"] and needs_repro is not None
          and needs_repro in label_ids(t)}

    def rel(task, kind):
        ids = [r.get("id") for r in ((task.get("related_tasks") or {}).get(kind) or [])]
        return [i for i in ids if i in idx]

    def warn(msg):
        print(f"warn: {msg}", file=sys.stderr)

    # Boxes: published, top-level, outside Draft, ordered by column then card.
    boxes, box_bucket, box_order = [], {}, {}
    for item in data["box_view"]:
        tid = item["id"]
        t = idx.get(tid)
        if t is None or box_label not in label_ids(t):
            continue
        bucket = next((b for b in (item.get("buckets") or [])
                       if b.get("project_view_id") == views["boxes"]), None)
        if bucket is None:
            warn(f"card {tid} has no bucket in the boxes view; skipped")
            continue
        if bucket["id"] == buckets_cfg["draft"]:
            continue
        if rel(t, "parenttask"):
            warn(f"box card {tid} has a parent task; skipped")
            continue
        boxes.append(tid)
        box_bucket[tid] = bucket
        box_order[tid] = (bucket.get("position") or 0, bucket["id"], item.get("position") or 0, tid)
    boxes.sort(key=lambda tid: box_order[tid])

    line_pos = {item["id"]: (item.get("position") or 0) for item in data["line_view"]}

    def published_lines(box_id):
        got = []
        for lid in rel(idx[box_id], "subtask"):
            line = idx[lid]
            if line_label not in label_ids(line):
                warn(f"line card {lid} has no line label; skipped")
                continue
            if rel(line, "parenttask") != [box_id]:
                warn(f"line card {lid} does not have exactly one parent; skipped")
                continue
            if rel(line, "subtask"):
                warn(f"line card {lid} has subtasks of its own; skipped")
                continue
            got.append(lid)
        got.sort(key=lambda lid: (0, line_pos[lid], lid) if lid in line_pos else (1, 0, lid))
        return got

    def line_record(line):
        lids = label_ids(line)
        tags, cards = [], []
        for rid in rel(line, "related"):
            r = idx[rid]
            if box_label in label_ids(r) or line_label in label_ids(r):
                continue
            m = release_title.match((r.get("title") or "").strip()) if release_title else None
            if m:
                tags.append(m.group(1))
            else:
                cards.append({"done": bool(r["done"]), "needs_repro": rid in nr})
        date = str(line.get("end_date") or "")[:10]
        return {
            "id": line["id"],
            "text": (line.get("title") or "").strip(),
            "done": bool(line["done"]),
            "confirming": labels["confirming"] in lids,
            "not_confirming": labels["not_confirming"] in lids,
            "release": max(tags, key=_version_key) if tags else None,
            "date": None if (not date or date.startswith("0001")) else date,
            "cards": cards,
        }

    def box_record(t, lines):
        lids = label_ids(t)
        words = [w for k, w in sizes.items() if k in lids]
        if len(words) > 1:
            warn(f"box card {t['id']} has {len(words)} size labels; size dropped")
            size = None
        else:
            size = words[0] if words else None
        blurb = plain(t.get("description"))
        if blurb is None and (t.get("description") or "").strip():
            warn(f"box card {t['id']} description is not plain text; blurb dropped")
        return {"id": t["id"], "title": (t.get("title") or "").strip(), "blurb": blurb,
                "size": size, "lines": lines}

    # Page text cards, keyed by title.
    page = {}
    for tid in boxes:
        t = idx[tid]
        if box_bucket[tid]["id"] != buckets_cfg["page"]:
            continue
        key = re.sub(r"\s+", "_", (t.get("title") or "").strip().lower())
        if key == "links":
            links = []
            for lid in published_lines(tid):
                m = URL_RE.search(idx[lid].get("description") or "")
                if not m:
                    warn(f"link card {lid} has no https URL; skipped")
                    continue
                links.append({"label": (idx[lid].get("title") or "").strip(), "href": m.group(0)})
            page["links"] = links
        elif key in PLAIN_PAGE_KEYS + ("characters_banner",):
            value = plain(t.get("description"))
            if value is None:
                if (t.get("description") or "").strip():
                    warn(f"page card {tid} has no usable text for '{key}'; omitted")
                else:
                    warn(f"page card {tid} is empty for '{key}'; omitted")
            else:
                page[key] = value
        else:
            warn(f"page card {tid} has an unknown title; ignored")

    # Sections: one per bucket that has a published box, in column order.
    kinds = {buckets_cfg["active"]: "active", buckets_cfg["not_planned"]: "not_planned",
             buckets_cfg["shipped"]: "shipped", buckets_cfg["background"]: "background"}
    used, by_bucket, bucket_order = set(), {}, {}
    for tid in boxes:
        bucket = box_bucket[tid]
        if bucket["id"] == buckets_cfg["page"] or bucket["id"] in by_bucket:
            continue
        kind = kinds.get(bucket["id"], "upcoming")
        heading, note = _heading_note(bucket.get("title") or "")
        sid = {"shipped": "shipped", "background": "history"}.get(kind) \
            or _section_id(heading, bucket["id"], used)
        used.add(sid)
        by_bucket[bucket["id"]] = {"id": sid, "heading": heading, "note": note, "kind": kind,
                                   "_bucket": bucket["id"], "boxes": []}
        bucket_order[bucket["id"]] = (bucket.get("position") or 0, bucket["id"])
    for tid in boxes:
        bucket = box_bucket[tid]
        if bucket["id"] == buckets_cfg["page"]:
            continue
        lines = [line_record(idx[lid]) for lid in published_lines(tid)]
        if not lines:
            warn(f"box card {tid} has no published lines; skipped")
            continue
        by_bucket[bucket["id"]]["boxes"].append(box_record(idx[tid], lines))
    sections = sorted(by_bucket.values(), key=lambda s: bucket_order[s["_bucket"]]) \
        if by_bucket else []
    for s in sections:
        s.pop("_bucket", None)

    characters = []
    if cfg.get("characters"):
        ccfg = cfg["characters"]
        roster = ccfg["roster"]
        if roster not in idx:
            raise ExportError(f"roster card {roster} not on board")
        char_ids = subtask_ids(idx[roster])
        if len(char_ids) != ccfg["count"]:
            raise ExportError(f"roster has {len(char_ids)} characters, expected {ccfg['count']}")
        type_re = re.compile(ccfg["type_pattern"])
        for chid in char_ids:
            if chid not in idx:
                raise ExportError(f"character card {chid} not on board")
            ch = idx[chid]
            issues = []
            for iid in subtask_ids(ch):
                if iid not in idx:
                    raise ExportError(f"issue card {iid} not on board")
                t = idx[iid]
                m = type_re.search(t.get("description") or "")
                issues.append({"id": iid, "done": bool(t["done"]), "type": m.group(1) if m else None,
                               "needs_repro": iid in nr})
            characters.append({"id": chid, "name": (ch.get("title") or "").removeprefix(ccfg["name_prefix"]),
                               "ready": ccfg["ready_label"] in label_ids(ch), "issues": issues})
    if "title" not in page:
        raise ExportError("page text card 'Title' missing or empty")

    snap = {
        "schema": 2,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "latest_release": latest,
        "releases": releases,
        "stats": {"verifying_open": len(nr)},
        "page": page,
        "sections": sections,
    }
    if cfg.get("characters"):
        snap["characters"] = characters
    if releases_etag:
        snap["releases_etag"] = releases_etag
    return snap


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--share-hash-file")
    src.add_argument("--board-json")
    ap.add_argument("--base-url", default="https://tasks.2143.me")
    ap.add_argument("--prev")
    a = ap.parse_args(argv)
    try:
        with open(a.config) as f:
            cfg = json.load(f)
        if a.board_json:
            with open(a.board_json) as f:
                data = json.load(f)
        else:
            data = fetch_share(a.base_url.rstrip("/"), a.share_hash_file, cfg)
        open_t = [t for t in data["tasks"] if not t["done"]]
        done_t = [t for t in data["tasks"] if t["done"]]
        if not open_t or not done_t:
            raise ExportError(f"got {len(open_t)} open and {len(done_t)} done tasks; share scope wrong?")
        if len(data["tasks"]) < cfg["min_tasks"]:
            raise ExportError(f"only {len(data['tasks'])} tasks (< {cfg['min_tasks']}); share scope wrong?")
        if cfg.get("releases"):
            releases, latest, etag = fetch_releases(cfg["releases"], a.prev, tasks=data["tasks"])
        else:
            releases, latest, etag = [], None, None
        snap = export(cfg, data, releases, latest, etag)
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
