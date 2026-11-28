# timestone-roadmap

Player-facing roadmap for [Timestone](https://github.com/tilderain/Timestone), the
UMVC3 rollback mod. Served at <https://roadmap.b.hero.rehab> (test domain, `noindex`)
by the Argo CD Application `timestone-roadmap` in `2143-Labs/argo`
(`apps/timestone-roadmap.yaml`), which deploys `main` of this repo.

## How the page is built

Every word a visitor reads is a card in Vikunja project 19 (<https://tasks.2143.me/>).
Git holds the wiring, the CSS, the script and the guards, no prose.

- `config/roadmap.json` is the wiring: which project, which two views, which bucket
  and label ids, the legend wording, the size words, the page's few fixed phrases
  (`words`), the team size and which level counts as worst (`team`), the GitHub repo
  release links point at. Ids are stable; column and label *names* are not used.
- `tools/export_board.py` reads the project through a read-only link share and writes
  a trimmed snapshot (`data/board.json`, schema 2): the published boxes and lines with
  their text, the state flags, the character grid, release tags and two counts. Card
  ids never reach the page; titles and descriptions of box/line/page cards do, because
  they *are* the page text.
- `tools/build.py` turns the snapshot plus the config into one static `site/index.html`:
  markup, CSS and one inline script in a single file, nothing loaded at view time.
  It holds no words of its own (a test fails if one slips in). It refuses to build on
  internal wording (story points, Impact, P0-P2, board links) or a malformed snapshot.
  A mistake on a single card unpublishes that card with a `warn:` line instead, because
  editors work on the board and cannot see a stalled build.

## How the page works

The page is a status board, and every label on it is also a control. No label was
invented for that: each control is a word the board already had.

- **Section headings** are the sticky jump bar, each with its number of lines. `Now`,
  `Next`, `Later` and `Not planned` sit side by side as lanes, in board column order;
  `Shipped` and `Background` follow.
- **Level rows** (`Can crash`, `Known desyncs`, …) filter the character roster; the bar
  above them shows all characters by level. The search box, named by the `Characters
  heading` card, narrows the roster as you type; Enter picks the first match.
- **Character tiles** are picked into a team (`team.size`, three for UMVC3). The page
  answers with the team's worst level (`team.worst_first` ranks the level keys) and keeps
  the team in the address (`?team=dante,vergil,zero`, shareable) and in the browser's
  localStorage, so a returning player sees their team first. The bar under a tile splits
  its open issues by level. Remove `team` from the config and tiles are plain tiles.
- **The legend** filters lines by state and counts each state.
- **Release tags** above `Shipped` filter shipped work to one release; the latest
  release in the stamp is a shortcut to what it shipped.
- A `Background` box whose every line starts `YYYY-MM-DD: ` is drawn as a timeline.

The script only shows, hides and copies what is already in the page. Without it the
controls do nothing and every section still reads top to bottom.

In the cluster a `refresh` sidecar runs both tools every 15 minutes into a shared
`/data` volume. nginx serves `/data/index.html`; until the sidecar's first good build
(cold start, or the share is missing or broken) it serves the copy baked into git at
`site/index.html`. A failed run logs one line and leaves the last good page in place.
The sidecar starts from the baked `data/board.json`, so when the unauthenticated GitHub
API is rate-limited (60 requests an hour, shared by everything behind the home IP) it
reuses the last known releases.

Visitors reach nginx through Anubis (`templates/anubis.yaml`): the route keeps its
rate-limit and CrowdSec gates and points at the `anubis` Service, which shows a
short proof-of-work challenge about once a week per browser, blocks known AI crawlers, and gives
link unfurlers (Discord, Slack) a fixed preview card. `anubis.enabled=false` routes
straight to nginx again. With the Anubis pod down the page answers 503. Only this
host is covered: the lobby join links on `mm.b.hero.rehab` are not behind Anubis.

## Edit the text on the board

Nothing on the page is edited in git. Two Kanban/list views of project 19 do the work:

- **Public roadmap** (Kanban) — its columns are the page's sections, in column order.
- **Public lines** (list view) — drag to reorder lines inside their box.

| Label | Goes on | Meaning |
|---|---|---|
| `Public: box` | box cards | publishes the card as a box; required |
| `Public: line` | line cards | publishes the line; unlabelled subtasks stay drafts |
| `Public: confirming` | line | force ◷ with no suffix |
| `Public: not confirming` | line | suppress the automatic Needs-Repro ◷ |
| `Public: size S` / `M` / `L` | box | the `small` / `medium` / `large job` badge |
| `Public: character` | character card | publishes the tile; the card title is the name on it |
| `Public: ready` | character card | tile becomes Tournament-ready |
| `Public: ignore` | issue card | not counted for its character |

- **Box** = card with `Public: box`, no parent task, in any column except `Draft`.
  Title = the box heading, description = the blurb (plain text, no markdown), size
  label = the badge. Box order = drag order inside its column; section order = column
  order. A column titled `Heading (note)` renders as `Heading` plus a small note.
- **Line** = subtask of a box with `Public: line`, exactly one parent, no subtasks of
  its own. Title = the line text. Lines are ordered by the `Public lines` view.
- **State** comes from cards the line is *related* to. A related card titled
  `Release vX` means it shipped in that release (the shipped tail links to it); every
  other related card is a state card: done → ✔, open with `Needs-Repro` → ◷ "fix
  released, confirming". In the `Now` column an unmarked line reads as ▶, elsewhere ○.
- **Columns with a meaning**: `Draft` (never published), `Shipped` (rendered as shipped
  work), `Background` (each box is a card with its lines as a plain bulleted list),
  `Page text` (see below). Every other column is an ordinary section.
- **Page text** boxes, keyed by their title: `Title`, `Tagline`, `Goal`, `Notice`,
  `Footer`, `Characters heading` (the description is the `<h2>` text and the name of the
  search box), `Characters banner` (the description is the text) and `Links`, whose
  subtasks are one link each (title = label, description = the `https://` address; the
  first link is the highlighted button). Two of them carry the
  character words: `Levels`, whose subtasks are the level rows in creation order
  (title = level key, description = `<mark> | <label> | <caption>`), and `Types`,
  whose subtasks map one `**Type:**` word to a level (title = the word, description =
  the level key, or `none` to leave the word uncounted). Their subtasks need no label.
- **Words** (optional page text box, not on the board today): each subtask rewords one
  of the page's fixed phrases (title = the key, description = the phrase, placeholders
  kept), and wins over `words` in `config/roadmap.json`. The keys are `stamp`
  (`Last updated {time} UTC`), `latest` (`latest release {tag} ({date})`), `counts`
  (`{open} open · {fixed} fixed`), `awaiting` (`{n} awaiting confirmation`), `size`
  (`{size} job`), `in release` (`(in {tag}, confirming)`), `fix released`, and
  `legend now` / `legend open` / `legend verifying` / `legend done` / `legend no` for
  the legend rows. Subtasks need no label; an unknown key is ignored with a `warn:`.
- **Characters** are the cards wearing `Public: character`; their subtasks are their
  issues, and the word after `**Type:**` picks the level through `Types`. A card with
  `Public: ready` is Tournament-ready whatever its issues say; a card with no ranked
  open issue reads Untested.
- `{verifying_open}` inside a line is replaced by the number of open Needs-Repro cards.

A new card is published only once it has its label and its box is outside `Draft`; cards
added from inside a column start unlabelled. The page refreshes every 15 minutes, or
at once with `kubectl --context closet-as-admin -n timestone-roadmap rollout restart
deploy/timestone-roadmap`.

A box's size is a judgment of the work left for the whole feature as players will see
it, not a sum of points. A card estimate can cover a narrower scope than the line text:
Replays (card 758, 8 points) is a large job because saving, offline playback, a Gallery
menu entry and version handling are all still to build. A box belongs under Now only
while someone is working on it.

## A second project on the same chart

Set `config` to that board's wiring as a JSON string (same shape as
`config/roadmap.json`: project id, view ids, bucket ids, label ids, `legend` and
`words`; `team` only if the board has characters to pick) and `baked: false`
so the chart ships no Timestone snapshot and no Timestone page. The pod then starts
unready and becomes ready when the first refresh has built the page.

## Refresh the baked copy

Run the exporter in the pod, where the link share lives, and build locally:

```bash
K="kubectl --context closet-as-admin -n timestone-roadmap exec -i deploy/timestone-roadmap -c refresh --"
$K sh -c 'cat > /data/.probe-config.json' < config/roadmap.json
$K python3 -B - --config /data/.probe-config.json --share-hash-file /etc/vikunja-share/hash \
   --base-url http://vikunja.default.svc --prev /data/board.json --out /data/.probe-board.json \
   < tools/export_board.py
$K cat /data/.probe-board.json > data/board.json
$K rm -f /data/.probe-config.json /data/.probe-board.json
python3 tools/build.py && python3 -m unittest tools/test_build.py
```

Commit `data/board.json` and `site/index.html`.

## Check the deployed page

`curl` from outside gets the Anubis challenge, so read it from inside the pod:

```bash
kubectl --context closet-as-admin -n timestone-roadmap exec deploy/timestone-roadmap -c refresh -- python3 -c 'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8080/").read().decode())'
```

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
how old it is. `warn: box <id> has no published lines; skipped` (and friends) mean one
card stayed off the page: fix the card, not the code.
