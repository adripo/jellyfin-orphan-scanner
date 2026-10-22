# Jellyfin leftover scan

Read-only scan for orphaned episodes and seasons left behind in a Jellyfin library. A leftover is a virtual library row with no media file: a deleted episode Jellyfin still lists, a season folder that is empty, or a missing-episode placeholder for something that has already aired.

Upcoming episodes are virtual too. Jellyfin returns them from the same `IsMissing` filter, and this script keeps them out of the leftover list.

A normal run does not change the library. `--delete --yes` removes virtual leftover episodes and seasons that have no file path.

## Compatibility

Targets Jellyfin Server 12.x. It uses the `Authorization: MediaBrowser Token="..."` API-key header required by v12; this header is also accepted by Jellyfin 10.11.

## What was wrong in the first draft

The draft queried `GET /Items?IncludeItemTypes=Episode&IsMissing=true` and printed every row. Against Jellyfin 12 that misses the leftovers you actually want, and it can fail before it prints anything:

- Jellyfin 12 rejects `X-Emby-Token`. The key has to be sent as `Authorization: MediaBrowser Token="..."`. That header also works on 10.11. A 401 from the old header looks like a bad key even when the key is valid.
- `IsMissing=true` means "virtual item", not "already aired". Episodes that have not premiered yet are included.
- Seasons are a separate item type. The draft never requested them, so an empty virtual season with no episode rows was invisible.
- There was no paging. A long library can come back as a partial page.
- A season directory can still exist on disk after every episode file is gone. That is an empty folder, not a virtual item, so `IsMissing` does not return it.

`IsUnaired=false` is not a fix for the date problem. Jellyfin applies it as `PremiereDate < now`, which drops episodes that have no premiere date. Undated rows are a common leftover, so the split is done after the download.

## Setup

Use the server root, the same host and port you open in a browser, without `/web`.

Create a key in Dashboard → Advanced → API Keys. Put it in the environment or a `.env` file (see `.env.example`). The command line works too, but the key then shows up in the process list.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# edit JELLYFIN_URL and JELLYFIN_API_KEY
.venv/bin/python find_orphans.py
```

Useful flags:

```bash
.venv/bin/python find_orphans.py --include-upcoming
.venv/bin/python find_orphans.py --series "The Expanse"
.venv/bin/python find_orphans.py --json --output leftovers.json
.venv/bin/python find_orphans.py --strict   # exit 2 when leftovers exist
.venv/bin/python find_orphans.py --delete   # show what would be removed
.venv/bin/python find_orphans.py --delete --yes
```

`--insecure` skips TLS verification for a server using a self-signed certificate.

Run a full library scan in Jellyfin before trusting the report. Jellyfin only marks a row virtual after it notices the file is gone. If missing-episode display is enabled, the list also includes past episodes you never downloaded. The server does not remember that a file used to exist.

## Sample

Against a library that still has a deleted episode, an undated virtual episode, an empty virtual season, and an empty season folder, the report looks like this. Episodes that have not aired are counted and then hidden:

```text
Home (12.0.0)
http://192.168.1.10:8096
Read-only. Nothing was modified.

Leftover episodes:          2
Leftover virtual seasons:   3
Empty season folders:       1
Upcoming virtual episodes:  2  (hidden; pass --include-upcoming)
Upcoming virtual seasons:   1  (hidden; pass --include-upcoming)
Needs review:               1

Disk Show
  Season 4 — empty season folder — id=season-disk-empty — /media/Disk Show/Season 4

Empty Virtual Show
  Season 3 — virtual season with no episodes — id=season-empty-virtual

Other Show
  Season 2 — virtual season, no media folder — id=season-other
    S02E01 — Undated Pilot — id=ep-other-1 — no premiere date

The Expanse
  Season 1 — virtual season, no media folder — id=season-expanse
    Also contains upcoming virtual episodes. Deleting this season later would remove those too.
    S01E02 — Remember the Cant — id=ep-expanse-2 — aired 2015-12-15
```

## How to read the report

- **Leftover episodes** have no file, and the premiere date is in the past or missing.
- **Leftover virtual seasons** have no season folder. A season that only exists to hold episodes that have not aired yet is listed as upcoming instead.
- **Empty season folders** exist on disk and contain no episodes.
- **Needs review** matched the virtual query but still has a file path, or Jellyfin still classifies the location as a real file. Those rows are not called leftovers.

## Removing leftovers

Run a scan first and read the report. Then:

```bash
.venv/bin/python find_orphans.py --delete
.venv/bin/python find_orphans.py --delete --yes --output deletion.json
```

`--delete` on its own prints the rows it would remove and does not call the delete API. `--delete --yes` removes them. Each row is loaded again immediately before the delete, and the script stops that row if a file path or a future premiere date has appeared. That reload uses `GET /Items?Ids=`, the same list route as the scan. `GET /Items/{id}` rejects an API key with HTTP 400 and the body `Error processing request.`, because Jellyfin looks up a user id the key does not have.

What `--delete --yes` removes:

- Virtual episodes with no file path whose premiere date is in the past, or missing.
- Virtual seasons with no file path, after their leftover episodes, when the season does not also hold upcoming episodes.

What it leaves in place:

- Episodes and seasons that have not premiered yet. Deleting a season that still has upcoming episodes would remove those too, so the season row stays and only the aired leftover episodes inside it are removed.
- Anything with a file path, a real media location, or a remote location.
- Empty season folders on disk. Those are real directories. Add `--delete-empty-folders` to remove them as well.

`--series` limits both the report and the delete to that show. `--output` writes the scan and, when you pass `--delete`, the deletion result.

Exit status is `0` after a successful scan or a delete with no failures, `2` when the URL or key is missing, and `1` when the server cannot be queried or a delete fails. `--delete` without `--yes` exits `2` when there is something to remove. `--strict` exits `2` when a read-only scan finds leftovers.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

The tests run the client against a local stand-in for the Jellyfin 12 item API. They do not contact a real server.

---

AI tools were used to assist with coding and documentation for this project.
