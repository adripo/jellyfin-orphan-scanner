"""Checks for the Jellyfin leftover scan.

The integration tests speak HTTP the way Jellyfin 12 does: MediaBrowser
authorization, paged /Items, and IsMissing for virtual rows. No live server
is required.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import find_orphans
from find_orphans import (
    JellyfinClient,
    authorization_header,
    build_report,
    main,
    parse_premiere,
    render_text,
    scan,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "find_orphans.py"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)

VIRTUAL = [
    {
        "Id": "ep-expanse-2",
        "Name": "Remember the Cant",
        "Type": "Episode",
        "SeriesName": "The Expanse",
        "SeriesId": "series-expanse",
        "SeasonName": "Season 1",
        "SeasonId": "season-expanse",
        "ParentIndexNumber": 1,
        "IndexNumber": 2,
        "PremiereDate": "2015-12-15T00:00:00.0000000Z",
        "LocationType": "Virtual",
    },
    {
        "Id": "ep-expanse-10",
        "Name": "Abaddon's Gate",
        "Type": "Episode",
        "SeriesName": "The Expanse",
        "SeriesId": "series-expanse",
        "SeasonName": "Season 1",
        "SeasonId": "season-expanse",
        "ParentIndexNumber": 1,
        "IndexNumber": 10,
        "PremiereDate": "2027-01-15T00:00:00.0000000Z",
        "LocationType": "Virtual",
    },
    {
        "Id": "season-expanse",
        "Name": "Season 1",
        "Type": "Season",
        "SeriesName": "The Expanse",
        "SeriesId": "series-expanse",
        "IndexNumber": 1,
        "LocationType": "Virtual",
    },
    {
        "Id": "ep-other-1",
        "Name": "Undated Pilot",
        "Type": "Episode",
        "SeriesName": "Other Show",
        "SeriesId": "series-other",
        "SeasonName": "Season 2",
        "SeasonId": "season-other",
        "ParentIndexNumber": 2,
        "IndexNumber": 1,
        "LocationType": "Virtual",
    },
    {
        "Id": "season-other",
        "Name": "Season 2",
        "Type": "Season",
        "SeriesName": "Other Show",
        "SeriesId": "series-other",
        "IndexNumber": 2,
        "LocationType": "Virtual",
    },
    {
        "Id": "ep-future-1",
        "Name": "Not Yet",
        "Type": "Episode",
        "SeriesName": "Future Show",
        "SeriesId": "series-future",
        "SeasonName": "Season 1",
        "SeasonId": "season-future",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "PremiereDate": "2027-06-01T00:00:00Z",
        "LocationType": "Virtual",
    },
    {
        "Id": "season-future",
        "Name": "Season 1",
        "Type": "Season",
        "SeriesName": "Future Show",
        "SeriesId": "series-future",
        "IndexNumber": 1,
        "LocationType": "Virtual",
    },
    {
        "Id": "season-empty-virtual",
        "Name": "Season 3",
        "Type": "Season",
        "SeriesName": "Empty Virtual Show",
        "SeriesId": "series-empty",
        "IndexNumber": 3,
        "LocationType": "Virtual",
    },
    {
        "Id": "ep-ghost",
        "Name": "Ghost With Path",
        "Type": "Episode",
        "SeriesName": "Review Show",
        "SeriesId": "series-review",
        "SeasonName": "Season 3",
        "SeasonId": "season-review",
        "ParentIndexNumber": 3,
        "IndexNumber": 4,
        "PremiereDate": "2012-01-01T00:00:00Z",
        "LocationType": "Virtual",
        "Path": "/media/Review Show/Season 3/ghost.mkv",
    },
]

FILESYSTEM = [
    {
        "Id": "season-disk-empty",
        "Name": "Season 4",
        "Type": "Season",
        "SeriesName": "Disk Show",
        "SeriesId": "series-disk",
        "IndexNumber": 4,
        "LocationType": "FileSystem",
        "Path": "/media/Disk Show/Season 4",
        "ChildCount": 0,
        "RecursiveItemCount": 0,
    },
    {
        "Id": "season-disk-full",
        "Name": "Season 9",
        "Type": "Season",
        "SeriesName": "Disk Show",
        "SeriesId": "series-disk",
        "IndexNumber": 9,
        "LocationType": "FileSystem",
        "Path": "/media/Disk Show/Season 9",
        "ChildCount": 8,
        "RecursiveItemCount": 8,
    },
]


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


@pytest.fixture
def jellyfin_server():
    handler = _catalog_handler(VIRTUAL, FILESYSTEM)
    server, url = _serve(handler)
    yield server, url, handler
    server.shutdown()


def _catalog_handler(virtual: list[dict], filesystem: list[dict], status: int = 200, body: bytes | None = None):
    class Handler(BaseHTTPRequestHandler):
        queries: list[dict[str, str]] = []
        authorization: list[str] = []
        legacy_tokens: list[str] = []
        deleted: list[str] = []

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth.startswith("MediaBrowser ") and 'Token="test-key"' in auth

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            self.queries.append({"path": parsed.path, **query})
            self.authorization.append(self.headers.get("Authorization", ""))
            legacy = self.headers.get("X-Emby-Token")
            if legacy:
                self.legacy_tokens.append(legacy)
            if body is not None:
                content_type = "text/html" if body.lstrip().startswith(b"<") else "application/json"
                self._bytes(status, body, content_type)
                return
            if status != 200:
                payload = body if body is not None else b'{"Message":"nope"}'
                self._bytes(status, payload, "application/json" if body is None else "text/html")
                return
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("MediaBrowser ") or 'Token="test-key"' not in auth:
                self._bytes(401, b'{"Message":"Unauthorized"}', "application/json")
                return
            if parsed.path == "/System/Info":
                self._json({"ServerName": "Home", "Version": "12.0.0"})
                return
            if parsed.path.startswith("/Items/"):
                # Jellyfin's single-item route rejects an API key: GetUserById(Guid.Empty)
                # throws ArgumentException, and the client only sees this body.
                self._bytes(400, b"Error processing request.", "text/plain")
                return
            if parsed.path != "/Items":
                self._bytes(404, b'{"Message":"missing"}', "application/json")
                return
            if query.get("Ids"):
                wanted = set(query["Ids"].split(","))
                matched = [item for item in [*virtual, *filesystem] if item["Id"] in wanted]
                self._json({"Items": matched, "TotalRecordCount": len(matched), "StartIndex": 0})
                return
            include = set(query.get("IncludeItemTypes", "").split(","))
            pool = virtual if query.get("IsMissing") == "true" else filesystem
            if query.get("LocationTypes"):
                pool = [item for item in pool if item.get("LocationType") == query["LocationTypes"]]
            pool = [item for item in pool if item["Type"] in include]
            start = int(query.get("StartIndex", "0"))
            limit = int(query.get("Limit", "100"))
            page = pool[start:start + limit]
            self._json({"Items": page, "TotalRecordCount": len(pool), "StartIndex": start})

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            self.authorization.append(self.headers.get("Authorization", ""))
            if not self._authorized():
                self._bytes(401, b'{"Message":"Unauthorized"}', "application/json")
                return
            item_id = parsed.path.removeprefix("/Items/")
            if not parsed.path.startswith("/Items/") or "/" in item_id or not item_id:
                self._bytes(404, b'{"Message":"missing"}', "application/json")
                return
            self.deleted.append(item_id)
            self._bytes(204, b"", "application/json")

        def _json(self, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self._bytes(200, raw, "application/json")

        def _bytes(self, code: int, payload: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return Handler


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def test_parse_premiere_accepts_jellyfin_timestamps() -> None:
    parsed = parse_premiere("2015-12-15T00:00:00.0000000Z")
    assert parsed is not None
    assert parsed == datetime(2015, 12, 15, tzinfo=timezone.utc)
    date_only = parse_premiere("2015-12-15")
    assert date_only is not None
    assert date_only.date().isoformat() == "2015-12-15"
    assert parse_premiere("not-a-date") is None
    assert parse_premiere(None) is None


def test_authorization_header_is_the_jellyfin_12_scheme() -> None:
    header = authorization_header("abc123")
    assert header.startswith("MediaBrowser ")
    assert 'Token="abc123"' in header
    assert "X-Emby-Token" not in header
    with pytest.raises(find_orphans.ConfigError):
        authorization_header('bad"key')


def test_past_and_undated_virtual_episodes_are_leftovers_but_future_ones_are_not() -> None:
    report = build_report(VIRTUAL, FILESYSTEM, now=NOW)
    leftover_ids = [finding.item.id for finding in report.leftover_episodes]
    upcoming_ids = [finding.item.id for finding in report.upcoming_episodes]
    assert leftover_ids == ["ep-other-1", "ep-expanse-2"]
    assert upcoming_ids == ["ep-future-1", "ep-expanse-10"]
    assert [finding.item.id for finding in report.leftover_seasons] == [
        "season-empty-virtual",
        "season-other",
        "season-expanse",
    ]
    assert [finding.item.id for finding in report.upcoming_seasons] == ["season-future"]
    assert [finding.item.id for finding in report.empty_seasons] == ["season-disk-empty"]
    assert [finding.item.id for finding in report.review] == ["ep-ghost"]
    expanse = next(finding for finding in report.leftover_seasons if finding.item.id == "season-expanse")
    assert "upcoming" in expanse.notes
    assert report.warnings == []


def test_specials_without_a_season_name_get_a_label() -> None:
    report = build_report(
        [{
            "Id": "ep-special",
            "Name": "Holiday",
            "Type": "Episode",
            "SeriesName": "Specials Show",
            "ParentIndexNumber": 0,
            "IndexNumber": 1,
            "LocationType": "Virtual",
            "PremiereDate": "2001-12-25T00:00:00Z",
        }],
        [],
        now=NOW,
    )
    text = render_text(
        find_orphans.ScanResult(report, "Home", "12.0.0", "http://jellyfin.local:8096"),
        include_upcoming=False,
    )
    assert "Specials" in text
    assert "S00E01 — Holiday" in text


def test_empty_folder_detection_skips_untrustworthy_child_counts() -> None:
    seasons = [
        {
            "Id": f"season-{index}",
            "Name": f"Season {index}",
            "Type": "Season",
            "SeriesName": "Counted Show",
            "IndexNumber": index,
            "LocationType": "FileSystem",
            "Path": f"/media/Counted Show/Season {index}",
            "ChildCount": 0,
            "RecursiveItemCount": 0,
        }
        for index in (1, 2, 3)
    ]
    report = build_report([], seasons, now=NOW)
    assert report.empty_seasons == []
    assert report.warnings


def test_season_with_only_virtual_episodes_is_not_an_empty_folder() -> None:
    episode = {
        "Id": "ep-only",
        "Name": "Only Missing",
        "Type": "Episode",
        "SeriesName": "Mixed Show",
        "SeasonName": "Season 1",
        "SeasonId": "season-mixed",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "PremiereDate": "2010-01-01T00:00:00Z",
        "LocationType": "Virtual",
    }
    season = {
        "Id": "season-mixed",
        "Name": "Season 1",
        "Type": "Season",
        "SeriesName": "Mixed Show",
        "IndexNumber": 1,
        "LocationType": "FileSystem",
        "Path": "/media/Mixed Show/Season 1",
        "ChildCount": 0,
        "RecursiveItemCount": 0,
    }
    report = build_report([episode], [season], now=NOW)
    assert [finding.item.id for finding in report.leftover_episodes] == ["ep-only"]
    assert report.empty_seasons == []
    assert report.leftover_seasons == []


def test_remote_and_filesystem_hits_stay_out_of_the_leftover_list() -> None:
    report = build_report(
        [
            {"Id": "ep-remote", "Name": "Stream", "Type": "Episode", "SeriesName": "Remote Show", "LocationType": "Remote", "PremiereDate": "2010-01-01T00:00:00Z"},
            {"Id": "ep-disk", "Name": "On Disk", "Type": "Episode", "SeriesName": "Disked Show", "LocationType": "FileSystem", "Path": "/media/real.mkv", "PremiereDate": "2010-01-01T00:00:00Z"},
        ],
        [],
        now=NOW,
    )
    assert report.leftover_episodes == []
    assert {finding.item.id for finding in report.review} == {"ep-remote", "ep-disk"}


def test_an_episode_airing_at_the_cutoff_is_a_leftover() -> None:
    report = build_report(
        [{
            "Id": "ep-now",
            "Name": "Right Now",
            "Type": "Episode",
            "SeriesName": "Cutoff Show",
            "PremiereDate": "2026-10-21T12:00:00Z",
            "LocationType": "Virtual",
        }],
        [],
        now=NOW,
    )
    assert [finding.item.id for finding in report.leftover_episodes] == ["ep-now"]


def test_duplicate_pages_are_collapsed() -> None:
    raw = VIRTUAL[0]
    report = build_report([raw, dict(raw)], [], now=NOW)
    assert len(report.leftover_episodes) == 1


def test_text_report_hides_upcoming_rows_until_asked() -> None:
    report = build_report(VIRTUAL, FILESYSTEM, now=NOW)
    result = find_orphans.ScanResult(report, "Home", "12.0.0", "http://jellyfin.local:8096")
    hidden = render_text(result, include_upcoming=False)
    assert "Remember the Cant" in hidden
    assert "Abaddon's Gate" not in hidden
    assert "Future Show" not in hidden
    assert "No leftover episodes or seasons found." not in hidden
    assert "id=ep-ghost" in hidden
    assert "/media/Disk Show/Season 4" in hidden
    shown = render_text(result, include_upcoming=True)
    assert "Abaddon's Gate" in shown
    assert "premieres 2027-06-01" in shown
    assert "Future Show" in shown


def test_clean_library_says_nothing_was_found() -> None:
    report = build_report([], [], now=NOW)
    text = render_text(find_orphans.ScanResult(report, "Home", "12.0.0", "http://jellyfin.local"), False)
    assert "No leftover episodes or seasons found." in text
    assert "Leftover episodes:          0" in text


def test_client_pages_and_uses_the_current_auth_header(jellyfin_server) -> None:
    _server, url, handler = jellyfin_server
    client = JellyfinClient(url, "test-key", page_size=1, timeout=5)
    result = scan(client, now=NOW)
    assert result.server_version == "12.0.0"
    assert result.report.leftover_total == 2 + 3 + 1
    item_queries = [query for query in handler.queries if query.get("path") == "/Items"]
    assert {query.get("IsMissing") for query in item_queries} == {"true", "false"}
    assert any(query.get("IncludeItemTypes") == "Episode,Season" and query.get("IsMissing") == "true" for query in item_queries)
    assert any(query.get("LocationTypes") == "FileSystem" for query in item_queries)
    assert all("UserId" not in query for query in item_queries)
    assert all(query.get("Recursive") == "true" for query in item_queries)
    starts = [int(query["StartIndex"]) for query in item_queries if query.get("IsMissing") == "true"]
    assert starts[0] == 0
    assert max(starts) == len(VIRTUAL) - 1
    assert handler.legacy_tokens == []
    assert all(value.startswith("MediaBrowser ") for value in handler.authorization)


def test_repeated_page_does_not_loop_forever() -> None:
    class RepeatHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            if parsed.path == "/System/Info":
                payload = {"ServerName": "Home", "Version": "12.0.0"}
            else:
                payload = {
                    "Items": [{"Id": "ep-repeat", "Name": "Repeat", "Type": "Episode", "SeriesName": "Loop Show", "LocationType": "Virtual", "PremiereDate": "2000-01-01T00:00:00Z"}],
                    "TotalRecordCount": 50,
                }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server, url = _serve(RepeatHandler)
    try:
        client = JellyfinClient(url, "test-key", page_size=1, timeout=5)
        result = scan(client, now=NOW)
    finally:
        server.shutdown()
    assert [finding.item.id for finding in result.report.leftover_episodes] == ["ep-repeat"]


def test_cli_against_a_jellyfin_12_server(jellyfin_server) -> None:
    _server, url, _handler = jellyfin_server
    result = _run("--url", url, "--api-key", "test-key", "--page-size", "1", "--timeout", "5")
    assert result.returncode == 0, result.stderr
    assert "Home (12.0.0)" in result.stdout
    assert "Read-only. Nothing was modified." in result.stdout
    assert "Leftover episodes:          2" in result.stdout
    assert "Leftover virtual seasons:   3" in result.stdout
    assert "Empty season folders:       1" in result.stdout
    assert "S01E02 — Remember the Cant — id=ep-expanse-2 — aired 2015-12-15" in result.stdout
    assert "S02E01 — Undated Pilot — id=ep-other-1 — no premiere date" in result.stdout
    assert "virtual season with no episodes — id=season-empty-virtual" in result.stdout
    assert "empty season folder — id=season-disk-empty — /media/Disk Show/Season 4" in result.stdout
    assert "Also contains upcoming virtual episodes." in result.stdout
    assert "Abaddon's Gate" not in result.stdout
    assert "Future Show" not in result.stdout
    assert "Season 9" not in result.stdout
    assert "X-Emby-Token" not in result.stderr
    assert "Connecting to" in result.stderr

    shown = _run("--url", url, "--api-key", "test-key", "--timeout", "5", "--include-upcoming", "--series", "future")
    assert shown.returncode == 0, shown.stderr
    assert "Future Show" in shown.stdout
    assert "Not Yet" in shown.stdout
    assert "The Expanse" not in shown.stdout

    strict = _run("--url", url, "--api-key", "test-key", "--timeout", "5", "--strict")
    assert strict.returncode == 2

    clean = _run("--url", url, "--api-key", "test-key", "--timeout", "5", "--strict", "--series", "No Such Show")
    assert clean.returncode == 0
    assert "No leftover episodes or seasons found." in clean.stdout


def test_cli_json_report(jellyfin_server, tmp_path: Path) -> None:
    _server, url, _handler = jellyfin_server
    output = tmp_path / "report.json"
    result = _run("--url", url, "--api-key", "test-key", "--timeout", "5", "--json", "--output", str(output))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["read_only"] is True
    assert payload["server"]["version"] == "12.0.0"
    assert payload["counts"]["leftover_episodes"] == 2
    assert payload["counts"]["leftover_seasons"] == 3
    assert {item["id"] for item in payload["leftover_episodes"]} == {"ep-expanse-2", "ep-other-1"}
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["counts"] == payload["counts"]


def test_cli_rejects_placeholders_and_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JELLYFIN_URL", raising=False)
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    assert main([]) == 2
    assert main(["--url", "http://IL_TUO_IP_SERVER:8096", "--api-key", "LA_TUA_CHIAVE_API"]) == 2
    assert main(["--url", "192.168.1.10:8096", "--api-key", "real-key"]) == 2


def test_cli_reports_auth_and_connection_failures() -> None:
    handler = _catalog_handler(VIRTUAL, FILESYSTEM, status=401)
    server, url = _serve(handler)
    try:
        rejected = _run("--url", url, "--api-key", "test-key", "--timeout", "5")
    finally:
        server.shutdown()
    assert rejected.returncode == 1
    assert "401" in rejected.stderr
    assert "X-Emby-Token" in rejected.stderr

    html_handler = _catalog_handler(VIRTUAL, FILESYSTEM, status=200, body=b"<html>web client</html>")
    html_server, html_url = _serve(html_handler)
    try:
        html = _run("--url", html_url, "--api-key", "test-key", "--timeout", "5")
    finally:
        html_server.shutdown()
    assert html.returncode == 1
    assert "did not return JSON" in html.stderr

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    offline = _run("--url", f"http://127.0.0.1:{closed_port}", "--api-key", "test-key", "--timeout", "1")
    assert offline.returncode == 1
    assert "Could not connect" in offline.stderr


def test_help_exits_cleanly() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "Read-only" in result.stdout
    assert "JELLYFIN_URL" in result.stdout


def test_deletion_plan_skips_upcoming_rows_and_disk_folders() -> None:
    report = build_report(VIRTUAL, FILESYSTEM, now=NOW)
    plan = find_orphans.plan_deletion(report, delete_empty_folders=False)
    assert [finding.item.id for finding in plan.remove] == [
        "ep-other-1",
        "ep-expanse-2",
        "season-empty-virtual",
        "season-other",
    ]
    skipped = {deferred.finding.item.id for deferred in plan.skip}
    assert skipped == {"season-expanse", "season-disk-empty"}
    with_folders = find_orphans.plan_deletion(report, delete_empty_folders=True)
    assert [finding.item.id for finding in with_folders.remove] == [
        "ep-other-1",
        "ep-expanse-2",
        "season-empty-virtual",
        "season-other",
        "season-disk-empty",
    ]


def test_delete_leaves_a_row_that_gained_a_file_path() -> None:
    report = build_report(VIRTUAL, FILESYSTEM, now=NOW)
    plan = find_orphans.plan_deletion(report, delete_empty_folders=False)
    items = {item["Id"]: dict(item) for item in VIRTUAL}
    items["ep-expanse-2"]["Path"] = "/media/The Expanse/Season 1/Episode 2.mkv"
    items["ep-expanse-2"]["LocationType"] = "FileSystem"

    class Stub:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def get_item(self, item_id: str) -> dict:
            return items[item_id]

        def delete_item(self, item_id: str) -> str:
            self.deleted.append(item_id)
            return "deleted"

    stub = Stub()
    outcome = find_orphans.remove_leftovers(stub, plan, now=NOW)
    assert stub.deleted == ["ep-other-1", "season-empty-virtual", "season-other"]
    assert any(deferred.finding.item.id == "ep-expanse-2" for deferred in outcome.skipped)
    assert outcome.failed == []


def test_failed_episode_keeps_its_season() -> None:
    report = build_report(VIRTUAL, FILESYSTEM, now=NOW)
    plan = find_orphans.plan_deletion(report, delete_empty_folders=False)
    items = {item["Id"]: dict(item) for item in VIRTUAL}

    class Stub:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def get_item(self, item_id: str) -> dict:
            return items[item_id]

        def delete_item(self, item_id: str) -> str:
            if item_id == "ep-other-1":
                raise find_orphans.JellyfinError("Jellyfin returned HTTP 500 for /Items/ep-other-1.")
            self.deleted.append(item_id)
            return "deleted"

    stub = Stub()
    outcome = find_orphans.remove_leftovers(stub, plan, now=NOW)
    assert stub.deleted == ["ep-expanse-2", "season-empty-virtual"]
    assert any(deferred.finding.item.id == "season-other" for deferred in outcome.skipped)
    assert any(deferred.finding.item.id == "ep-other-1" for deferred in outcome.failed)


def test_cli_delete_requires_yes_and_skips_unsafe_rows(jellyfin_server) -> None:
    _server, url, handler = jellyfin_server
    planned = _run("--url", url, "--api-key", "test-key", "--timeout", "5", "--delete")
    assert planned.returncode == 2, planned.stderr
    assert planned.stdout.count("Nothing was deleted.") == 1
    assert "Re-run with --delete --yes" in planned.stdout
    _before, _, after_heading = planned.stdout.partition("Will remove:")
    will_remove, _, _rest = after_heading.partition("Left in place")
    assert "id=ep-expanse-2" in will_remove
    assert "id=ep-expanse-10" not in will_remove
    assert "id=ep-ghost" not in will_remove
    assert handler.deleted == []

    removed = _run("--url", url, "--api-key", "test-key", "--timeout", "5", "--delete", "--yes")
    assert removed.returncode == 0, removed.stderr
    assert handler.deleted == ["ep-other-1", "ep-expanse-2", "season-empty-virtual", "season-other"]
    refreshed = [query.get("Ids") for query in handler.queries if query.get("Ids")]
    assert refreshed[:4] == ["ep-other-1", "ep-expanse-2", "season-empty-virtual", "season-other"]
    assert not any(query["path"].startswith("/Items/") for query in handler.queries)
    assert "Deleted: 4" in removed.stdout
    assert "season-expanse" in removed.stdout
    assert "season-disk-empty" in removed.stdout
    forbidden = {"ep-expanse-10", "ep-future-1", "season-future", "season-expanse", "ep-ghost", "season-disk-empty", "season-disk-full"}
    assert forbidden.isdisjoint(handler.deleted)


def test_cli_delete_can_limit_by_series_and_include_empty_folders(jellyfin_server, tmp_path: Path) -> None:
    _server, url, handler = jellyfin_server
    narrowed = _run(
        "--url", url, "--api-key", "test-key", "--timeout", "5",
        "--delete", "--yes", "--series", "Other Show",
    )
    assert narrowed.returncode == 0, narrowed.stderr
    assert handler.deleted == ["ep-other-1", "season-other"]

    output = tmp_path / "deletion.json"
    folders = _run(
        "--url", url, "--api-key", "test-key", "--timeout", "5",
        "--delete", "--yes", "--delete-empty-folders", "--output", str(output),
    )
    assert folders.returncode == 0, folders.stderr
    assert "season-disk-empty" in handler.deleted
    assert "season-disk-full" not in handler.deleted
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["read_only"] is False
    assert saved["deletion"]["performed"] is True
    assert "season-disk-empty" in {item["id"] for item in saved["deletion"]["deleted"]}


def test_yes_without_delete_is_rejected() -> None:
    assert main(["--url", "http://127.0.0.1:9", "--api-key", "real-key", "--yes"]) == 2
