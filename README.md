# timestone-roadmap

Player-facing roadmap for [Timestone](https://github.com/tilderain/Timestone), the
UMVC3 rollback mod. Served at <https://roadmap.b.hero.rehab> (test domain, `noindex`)
by the Argo CD Application `timestone-roadmap` in `2143-Labs/argo`
(`apps/timestone-roadmap.yaml`), which deploys `main` of this repo.

## How the page is built

- `content/roadmap.json` is the hand-written text: sections, items, shipped work,
  timeline, caveats. Each upcoming item names the board cards (Vikunja project 19)
  that decide its state. Card ids are never rendered.
- `tools/export_board.py` reads the board and the GitHub releases and writes a
  trimmed snapshot (`data/board.json`): done / needs-repro flags, the character
  grid, release tags, one count. No card titles or descriptions.
- `tools/build.py` turns content + snapshot into one static `site/index.html`.
  It refuses to build on unknown cards, unknown releases, internal wording
  (story points, Impact, P0-P2, board links) or a wrong tile count.

In the cluster, a `refresh` sidecar runs both tools every 15 minutes into a shared
`/data` volume, using a read-only Vikunja link share. nginx serves `/data/index.html`;
until the sidecar's first good build (cold start, or the share is missing or broken)
it serves the copy baked into git at `site/index.html`. A failed run logs one line
and leaves the last good page in place. The sidecar starts from the baked
`data/board.json`, so when the unauthenticated GitHub API is rate-limited (60 requests
an hour, shared by everything behind the home IP) it reuses the last known releases.

Live refresh changes states, counts and the character grid only. New items or
wording need a PR.

Visitors reach nginx through Anubis (`templates/anubis.yaml`): the route keeps its
rate-limit and CrowdSec gates and points at the `anubis` Service, which shows a
short proof-of-work challenge about once a week per browser, blocks known AI crawlers, and gives
link unfurlers (Discord, Slack) a fixed preview card. `anubis.enabled=false` routes
straight to nginx again. With the Anubis pod down the page answers 503. Only this
host is covered: the lobby join links on `mm.b.hero.rehab` are not behind Anubis.

## Edit the text

1. Edit `content/roadmap.json`.
2. Run `python3 tools/build.py && python3 -m unittest tools/test_build.py`.
3. Commit `content/` and `site/index.html` and open a PR. Merging to `main` deploys it.

## Refresh the baked copy

```bash
kubectl --context closet-as-admin -n timestone-roadmap exec deploy/timestone-roadmap -c refresh -- cat /data/board.json > data/board.json
python3 tools/build.py
```

Commit `data/board.json` and `site/index.html`.

## Rotate the link share

1. In Vikunja, delete the old link share on project 19 and create a new **read-only** one.
2. Store its hash (the part between `/share/` and `/auth` in the link) in OpenBao, without
   leaving it in shell history:

   ```bash
   f=$(mktemp /dev/shm/seed-XXXXXX)
   $EDITOR "$f"   # contents exactly: {"hash":"<paste>"}
   bao kv put -mount=consumers john2143-com/timestone-roadmap/vikunja-link-share @"$f"
   shred -u "$f"
   ```

3. External Secrets picks it up within 1 h, or immediately with:

   ```bash
   kubectl -n timestone-roadmap annotate externalsecret timestone-roadmap-vikunja force-sync=$(date +%s) --overwrite
   ```

## Troubleshooting

```bash
kubectl -n timestone-roadmap logs deploy/timestone-roadmap -c refresh --tail=20
```

`refresh ok <time>` is a good run. `export failed: <reason>` or `build failed: <reason>`
means the page kept its last good version; the `Last updated` stamp on the page shows
how old it is.
