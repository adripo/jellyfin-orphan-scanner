#!/usr/bin/env python3
"""Find leftover episodes and seasons in a Jellyfin library.

Jellyfin keeps a virtual row when an episode or season has metadata but no
media file. Those rows are what people mean by orphaned leftovers: a season
removed from disk, a deleted episode that the database still lists, or a
missing-episode placeholder for something that already aired.

Upcoming episodes are virtual too, and Jellyfin's IsMissing filter returns
them together with real leftovers. This script separates the two.

A normal run only reads the library. ``--delete --yes`` removes virtual
leftover rows that have no file path. Episodes that have not aired, rows
that still point at a file, and empty folders on disk are left alone.

Jellyfin 12 disabled the legacy X-Emby-Token header. The API key is sent as
``Authorization: MediaBrowser Token="..."``, which also works on 10.11.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, TextIO

import requests

PAGE_SIZE_DEFAULT = 200
CLIENT_NAME = "jellyfin-orphans"
CLIENT_VERSION = "1.0.0"
_ITEM_ID = re.compile(r"^[0-9A-Za-z-]{1,80}$")

PLACEHOLDER_MARKERS = (
    "YOUR_API_KEY",
    "YOUR_JELLYFIN",
    "changeme",
)

ITEM_FIELDS = "Path,ChildCount,RecursiveItemCount,MediaSources,ProviderIds,DateCreated,ParentId"

_PREMIERE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:T(?P<time>\d{2}:\d{2}:\d{2})(?:\.(?P<frac>\d+))?)?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?$"
)


class JellyfinError(Exception):
    """The server could not be queried."""


class JellyfinAuthError(JellyfinError):
    """The API key was rejected."""


class ConfigError(Exception):
    """The command was invoked without a usable server address or API key."""


@dataclass(frozen=True)
class MediaItem:
    id: str
    name: str
    item_type: str
    series_name: str
    season_name: str
    series_id: str | None
    season_id: str | None
    index_number: int | None
    parent_index_number: int | None
    premiere: datetime | None
    location_type: str | None
    path: str | None
    child_count: int | None
    recursive_item_count: int | None
    has_media_path: bool

    @property
    def season_number(self) -> int | None:
        if self.item_type == "Season":
            return self.index_number
        return self.parent_index_number

    @property
    def episode_number(self) -> int | None:
        if self.item_type == "Episode":
            return self.index_number
        return None


@dataclass(frozen=True)
class Finding:
    item: MediaItem
    kind: str
    reason: str
    notes: str = ""
    short: str = ""


@dataclass
class Report:
    leftover_episodes: list[Finding] = field(default_factory=list)
    leftover_seasons: list[Finding] = field(default_factory=list)
    empty_seasons: list[Finding] = field(default_factory=list)
    upcoming_episodes: list[Finding] = field(default_factory=list)
    upcoming_seasons: list[Finding] = field(default_factory=list)
    review: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def leftover_total(self) -> int:
        return len(self.leftover_episodes) + len(self.leftover_seasons) + len(self.empty_seasons)


@dataclass(frozen=True)
class ScanResult:
    report: Report
    server_name: str
    server_version: str
    base_url: str


def authorization_header(api_key: str) -> str:
    """Build the Jellyfin 12 authorization header.

    X-Emby-Token, X-MediaBrowser-Token, X-Emby-Authorization, and the
    ``api_key`` query parameter were removed in Jellyfin 12. The MediaBrowser
    scheme is accepted by 10.11 and 12.
    """
    if any(char in api_key for char in '"\r\n'):
        raise ConfigError("The API key contains a quote or a newline, so it cannot go in the Authorization header.")
    return (
        f'MediaBrowser Client="{CLIENT_NAME}", Device="find-orphans", '
        f'DeviceId="find-orphans", Version="{CLIENT_VERSION}", Token="{api_key}"'
    )


def load_env_file(path: str) -> None:
    """Load KEY=VALUE pairs into the environment without overriding real variables."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def looks_like_placeholder(value: str) -> bool:
    upper = value.upper()
    return any(marker.upper() in upper for marker in PLACEHOLDER_MARKERS)


def parse_premiere(value: Any) -> datetime | None:
    """Parse a Jellyfin premiere timestamp.

    The server emits ISO-8601 with up to seven fractional digits and a trailing
    ``Z``. Episodes with no date stay ``None`` so they are not treated as upcoming.
    """
    if not isinstance(value, str):
        return None
    match = _PREMIERE_RE.match(value.strip())
    if match is None:
        return None
    clock = match.group("time") or "00:00:00"
    fraction = (match.group("frac") or "0")[:6].ljust(6, "0")
    tz = match.group("tz") or "Z"
    if tz == "Z":
        tz = "+00:00"
    try:
        parsed = datetime.fromisoformat(f"{match.group('date')}T{clock}.{fraction}{tz}")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _media_path_present(raw: dict[str, Any]) -> bool:
    path = raw.get("Path")
    if isinstance(path, str) and path.strip():
        return True
    sources = raw.get("MediaSources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                source_path = source.get("Path")
                if isinstance(source_path, str) and source_path.strip():
                    return True
    return False


def parse_item(raw: dict[str, Any]) -> MediaItem | None:
    item_id = _clean(raw.get("Id"))
    item_type = _clean(raw.get("Type"))
    if not item_id or item_type not in {"Episode", "Season"}:
        return None

    name = _clean(raw.get("Name"))
    series_name = _clean(raw.get("SeriesName")) or "Unknown series"
    if item_type == "Season":
        season_name = _clean(raw.get("SeasonName")) or name
        series_id = _clean(raw.get("SeriesId")) or _clean(raw.get("ParentId")) or None
        season_id = item_id
    else:
        season_name = _clean(raw.get("SeasonName"))
        series_id = _clean(raw.get("SeriesId")) or None
        season_id = _clean(raw.get("SeasonId")) or _clean(raw.get("ParentId")) or None

    path = _clean(raw.get("Path")) or None
    child_count = _optional_int(raw.get("ChildCount")) if "ChildCount" in raw else None
    recursive_count = _optional_int(raw.get("RecursiveItemCount")) if "RecursiveItemCount" in raw else None
    return MediaItem(
        id=item_id,
        name=name or ("Untitled episode" if item_type == "Episode" else "Untitled season"),
        item_type=item_type,
        series_name=series_name,
        season_name=season_name,
        series_id=series_id,
        season_id=season_id,
        index_number=_optional_int(raw.get("IndexNumber")),
        parent_index_number=_optional_int(raw.get("ParentIndexNumber")),
        premiere=parse_premiere(raw.get("PremiereDate")),
        location_type=_clean(raw.get("LocationType")) or None,
        path=path,
        child_count=child_count,
        recursive_item_count=recursive_count,
        has_media_path=_media_path_present(raw),
    )


def _dedupe(items: Iterable[MediaItem]) -> list[MediaItem]:
    seen: set[str] = set()
    unique: list[MediaItem] = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    return unique


def _episode_sort_key(item: MediaItem) -> tuple[Any, ...]:
    return (
        item.series_name.casefold(),
        item.season_number if item.season_number is not None else 10**9,
        item.episode_number if item.episode_number is not None else 10**9,
        item.name.casefold(),
        item.id,
    )


def _season_sort_key(item: MediaItem) -> tuple[Any, ...]:
    return (
        item.series_name.casefold(),
        item.season_number if item.season_number is not None else 10**9,
        item.name.casefold(),
        item.id,
    )


def _is_future(item: MediaItem, now: datetime) -> bool:
    return item.premiere is not None and item.premiere > now


def _review_reason(item: MediaItem) -> str | None:
    """Return why a virtual-query hit must not be called a leftover."""
    if item.location_type == "Remote":
        return "Remote item. Left out of the leftover list."
    if item.location_type == "FileSystem":
        return "Location is FileSystem, so Jellyfin still considers the file present."
    if item.has_media_path:
        return "Marked virtual, but a file path is still set. Left out of the leftover list."
    if item.location_type not in {None, "Virtual"}:
        return f"Location is {item.location_type}. Left out of the leftover list."
    return None


def _episode_reason(item: MediaItem, now: datetime) -> tuple[str, str]:
    if _is_future(item, now):
        return "upcoming_episode", "No media file yet; the premiere date is still in the future."
    if item.premiere is None:
        return "leftover_episode", "No media file, and Jellyfin has no premiere date."
    return "leftover_episode", "No media file, and the premiere date is in the past."


def build_report(
    virtual_raw: Iterable[dict[str, Any]],
    filesystem_season_raw: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> Report:
    """Classify raw ``/Items`` rows into leftovers, upcoming placeholders, and review items.

    ``IsMissing=true`` on the server means ``IsVirtualItem``, not "already aired".
    Passing ``IsUnaired=false`` would also drop episodes whose premiere date is
    null, and those undated rows are a common leftover. Dates are split here.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)

    virtual_items = _dedupe(item for raw in virtual_raw if (item := parse_item(raw)) is not None)
    filesystem_seasons = _dedupe(
        item for raw in filesystem_season_raw if (item := parse_item(raw)) is not None and item.item_type == "Season"
    )

    report = Report()
    episodes_by_season: dict[str, list[MediaItem]] = defaultdict(list)
    leftover_ids: set[str] = set()
    upcoming_ids: set[str] = set()

    for item in virtual_items:
        if item.item_type != "Episode":
            continue
        if item.season_id:
            episodes_by_season[item.season_id].append(item)
        review_reason = _review_reason(item)
        if review_reason is not None:
            report.review.append(Finding(item, "review", review_reason))
            continue
        kind, reason = _episode_reason(item, moment)
        finding = Finding(item, kind, reason)
        if kind == "upcoming_episode":
            upcoming_ids.add(item.id)
            report.upcoming_episodes.append(finding)
        else:
            leftover_ids.add(item.id)
            report.leftover_episodes.append(finding)

    virtual_season_ids: set[str] = set()
    for item in virtual_items:
        if item.item_type != "Season":
            continue
        virtual_season_ids.add(item.id)
        review_reason = _review_reason(item)
        if review_reason is not None:
            report.review.append(Finding(item, "review", review_reason))
            continue
        linked = episodes_by_season.get(item.id, [])
        linked_kinds = set()
        for episode in linked:
            if episode.id in leftover_ids:
                linked_kinds.add("leftover")
            elif episode.id in upcoming_ids:
                linked_kinds.add("upcoming")
            else:
                linked_kinds.add("other")
        notes = ""
        if "leftover" in linked_kinds and "upcoming" in linked_kinds:
            notes = "Also contains upcoming virtual episodes. Deleting this season later would remove those too."
        if not linked:
            report.leftover_seasons.append(
                Finding(
                    item,
                    "leftover_season",
                    "Virtual season with no media folder and no episodes under it.",
                    notes,
                    short="virtual season with no episodes",
                )
            )
        elif "leftover" in linked_kinds:
            report.leftover_seasons.append(
                Finding(
                    item,
                    "leftover_season",
                    "Virtual season with no media folder. It still contains leftover episodes.",
                    notes,
                    short="virtual season, no media folder",
                )
            )
        elif linked_kinds == {"upcoming"}:
            report.upcoming_seasons.append(
                Finding(
                    item,
                    "upcoming_season",
                    "Virtual season with no media folder. It only contains episodes that have not premiered.",
                    short="virtual season, upcoming episodes only",
                )
            )
        else:
            report.review.append(
                Finding(item, "review", "Virtual season whose episodes were all left out of the leftover list.")
            )

    countable = [season for season in filesystem_seasons if season.child_count is not None]
    suspicious_counts = (
        len(countable) >= 3 and all(season.child_count == 0 for season in countable)
    )
    if suspicious_counts:
        report.warnings.append(
            "Every on-disk season reported zero children, so empty-folder detection was skipped. "
            "That usually means the server did not fill ChildCount. Virtual leftovers above are unaffected."
        )
    else:
        for season in filesystem_seasons:
            if season.id in virtual_season_ids:
                continue
            if season.location_type not in {None, "FileSystem"}:
                continue
            if season.child_count != 0:
                continue
            if season.recursive_item_count not in {None, 0}:
                continue
            if episodes_by_season.get(season.id):
                continue
            report.empty_seasons.append(
                Finding(season, "empty_season", "Season folder has no episodes.", short="empty season folder")
            )

    report.leftover_episodes.sort(key=lambda finding: _episode_sort_key(finding.item))
    report.upcoming_episodes.sort(key=lambda finding: _episode_sort_key(finding.item))
    report.leftover_seasons.sort(key=lambda finding: _season_sort_key(finding.item))
    report.upcoming_seasons.sort(key=lambda finding: _season_sort_key(finding.item))
    report.empty_seasons.sort(key=lambda finding: _season_sort_key(finding.item))
    report.review.sort(key=lambda finding: _episode_sort_key(finding.item))
    return report


def filter_report(report: Report, series: str) -> Report:
    needle = series.casefold()

    def keep(findings: list[Finding]) -> list[Finding]:
        return [finding for finding in findings if needle in finding.item.series_name.casefold()]

    return Report(
        leftover_episodes=keep(report.leftover_episodes),
        leftover_seasons=keep(report.leftover_seasons),
        empty_seasons=keep(report.empty_seasons),
        upcoming_episodes=keep(report.upcoming_episodes),
        upcoming_seasons=keep(report.upcoming_seasons),
        review=keep(report.review),
        warnings=list(report.warnings),
    )


@dataclass(frozen=True)
class Deferred:
    """A leftover that was not removed, with the reason it was left alone."""

    finding: Finding
    reason: str


@dataclass(frozen=True)
class Removed:
    finding: Finding
    detail: str


@dataclass
class DeletionPlan:
    remove: list[Finding] = field(default_factory=list)
    skip: list[Deferred] = field(default_factory=list)


@dataclass
class DeletionOutcome:
    deleted: list[Removed] = field(default_factory=list)
    skipped: list[Deferred] = field(default_factory=list)
    failed: list[Deferred] = field(default_factory=list)


def _require_item_id(item_id: str) -> None:
    if _ITEM_ID.fullmatch(item_id) is None:
        raise JellyfinError(f"Refusing to use {item_id!r} in an item URL.")


def _virtual_delete_block(item: MediaItem) -> str | None:
    if _ITEM_ID.fullmatch(item.id) is None:
        return "The item id cannot be used in a delete request."
    if item.has_media_path or item.path:
        return "A file path is set, so the row was left in place."
    if item.location_type not in {None, "Virtual"}:
        return f"Location is {item.location_type}, so the row was left in place."
    return None


def _empty_folder_block(item: MediaItem) -> str | None:
    if _ITEM_ID.fullmatch(item.id) is None:
        return "The item id cannot be used in a delete request."
    if item.item_type != "Season" or item.location_type != "FileSystem":
        return "This is not an on-disk season folder."
    if item.child_count != 0 or item.recursive_item_count not in {None, 0}:
        return "The season folder is not empty."
    return None


def plan_deletion(report: Report, *, delete_empty_folders: bool) -> DeletionPlan:
    """Choose which leftovers can be removed.

    Virtual episodes and virtual seasons with no file path are eligible.
    A season that also holds upcoming episodes is skipped, because deleting
    the season would remove those episodes too. Empty folders stay on disk
    unless ``delete_empty_folders`` is set.
    """
    plan = DeletionPlan()
    for finding in report.leftover_episodes:
        reason = _virtual_delete_block(finding.item)
        if reason is None:
            plan.remove.append(finding)
        else:
            plan.skip.append(Deferred(finding, reason))
    for finding in report.leftover_seasons:
        if finding.notes:
            plan.skip.append(Deferred(
                finding,
                "This season also has upcoming episodes, so the season row stays. Leftover episodes inside it are still removed.",
            ))
            continue
        reason = _virtual_delete_block(finding.item)
        if reason is None:
            plan.remove.append(finding)
        else:
            plan.skip.append(Deferred(finding, reason))
    for finding in report.empty_seasons:
        if not delete_empty_folders:
            plan.skip.append(Deferred(
                finding,
                "Empty season folder is still on disk. Pass --delete-empty-folders to remove it.",
            ))
            continue
        reason = _empty_folder_block(finding.item)
        if reason is None:
            plan.remove.append(finding)
        else:
            plan.skip.append(Deferred(finding, reason))
    return plan


def _confirm_current(raw: dict[str, Any], finding: Finding, now: datetime) -> str | None:
    current = parse_item(raw)
    if current is None or current.id != finding.item.id or current.item_type != finding.item.item_type:
        return "The server returned a different item, so it was left in place."
    if finding.kind == "empty_season":
        if current.location_type != "FileSystem" or current.child_count != 0 or current.recursive_item_count not in {None, 0}:
            return "The season folder is no longer empty, so it was left in place."
        return None
    if current.has_media_path or current.path:
        return "The item now has a file path, so it was left in place."
    if current.location_type not in {None, "Virtual"}:
        return "The item is no longer virtual, so it was left in place."
    if current.item_type == "Episode" and current.premiere is not None and current.premiere > now:
        return "The premiere date is still in the future, so it was left in place."
    return None


def remove_leftovers(
    client: JellyfinClient,
    plan: DeletionPlan,
    now: datetime | None = None,
    log: TextIO | None = None,
) -> DeletionOutcome:
    """Delete the planned rows, checking each one again immediately before removal.

    Episodes go first. If one of them fails, its season is left in place so a
    later season delete cannot sweep up a row the script failed to handle.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)

    outcome = DeletionOutcome(skipped=list(plan.skip))
    episodes = [finding for finding in plan.remove if finding.item.item_type == "Episode"]
    seasons = [finding for finding in plan.remove if finding.item.item_type == "Season"]
    ordered = episodes + seasons
    failed_season_ids: set[str] = set()

    def say(message: str) -> None:
        if log is not None:
            print(message, file=log)

    for index, finding in enumerate(ordered):
        if (
            finding.item.item_type == "Season"
            and finding.kind != "empty_season"
            and finding.item.id in failed_season_ids
        ):
            outcome.skipped.append(Deferred(
                finding,
                "A leftover episode in this season could not be removed, so the season was left in place.",
            ))
            continue
        say(f"Removing {finding.item.id}")
        try:
            current = client.get_item(finding.item.id)
        except JellyfinAuthError as exc:
            outcome.failed.append(Deferred(finding, str(exc)))
            for rest in ordered[index + 1:]:
                outcome.skipped.append(Deferred(rest, "Deletion stopped because Jellyfin rejected the API key."))
            break
        except JellyfinError as exc:
            outcome.failed.append(Deferred(finding, str(exc)))
            if finding.item.item_type == "Episode" and finding.item.season_id:
                failed_season_ids.add(finding.item.season_id)
            continue
        if current is not None:
            block = _confirm_current(current, finding, moment)
            if block is not None:
                outcome.skipped.append(Deferred(finding, block))
                continue
        try:
            detail = client.delete_item(finding.item.id)
        except JellyfinAuthError as exc:
            outcome.failed.append(Deferred(finding, str(exc)))
            for rest in ordered[index + 1:]:
                outcome.skipped.append(Deferred(rest, "Deletion stopped because Jellyfin rejected the API key."))
            break
        except JellyfinError as exc:
            outcome.failed.append(Deferred(finding, str(exc)))
            if finding.item.item_type == "Episode" and finding.item.season_id:
                failed_season_ids.add(finding.item.season_id)
            continue
        outcome.deleted.append(Removed(finding, "already gone" if detail == "missing" else "removed"))
    return outcome


def season_label(item: MediaItem) -> str:
    if item.season_name:
        return item.season_name
    if item.season_number == 0:
        return "Specials"
    if item.season_number is not None:
        return f"Season {item.season_number}"
    return "Unknown season"


def episode_label(item: MediaItem) -> str:
    prefix = ""
    if item.season_number is not None and item.episode_number is not None:
        prefix = f"S{item.season_number:02d}E{item.episode_number:02d} — "
    elif item.episode_number is not None:
        prefix = f"E{item.episode_number:02d} — "
    return prefix + item.name


def _aired_text(item: MediaItem, kind: str) -> str:
    if item.premiere is None:
        return "no premiere date"
    day = item.premiere.date().isoformat()
    if kind.startswith("upcoming"):
        return f"premieres {day}"
    return f"aired {day}"


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    item = finding.item
    return {
        "id": item.id,
        "type": item.item_type,
        "kind": finding.kind,
        "name": item.name,
        "series": item.series_name,
        "series_id": item.series_id,
        "season": season_label(item),
        "season_id": item.season_id,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "premiere": item.premiere.date().isoformat() if item.premiere else None,
        "location_type": item.location_type,
        "path": item.path,
        "reason": finding.reason,
        "notes": finding.notes,
        "short": finding.short,
    }


def report_to_dict(result: ScanResult) -> dict[str, Any]:
    report = result.report
    return {
        "server": {
            "name": result.server_name,
            "version": result.server_version,
            "url": result.base_url,
        },
        "read_only": True,
        "counts": {
            "leftover_episodes": len(report.leftover_episodes),
            "leftover_seasons": len(report.leftover_seasons),
            "empty_seasons": len(report.empty_seasons),
            "upcoming_episodes": len(report.upcoming_episodes),
            "upcoming_seasons": len(report.upcoming_seasons),
            "review": len(report.review),
        },
        "warnings": report.warnings,
        "leftover_episodes": [_finding_to_dict(finding) for finding in report.leftover_episodes],
        "leftover_seasons": [_finding_to_dict(finding) for finding in report.leftover_seasons],
        "empty_seasons": [_finding_to_dict(finding) for finding in report.empty_seasons],
        "upcoming_episodes": [_finding_to_dict(finding) for finding in report.upcoming_episodes],
        "upcoming_seasons": [_finding_to_dict(finding) for finding in report.upcoming_seasons],
        "review": [_finding_to_dict(finding) for finding in report.review],
    }


@dataclass
class _SeasonBlock:
    order: tuple[Any, ...]
    label: str
    header: str | None = None
    notes: list[str] = field(default_factory=list)
    episodes: list[str] = field(default_factory=list)


def _describe(finding: Finding) -> str:
    item = finding.item
    label = episode_label(item) if item.item_type == "Episode" else season_label(item)
    return f"{item.series_name} — {label} — id={item.id}"


def render_text(result: ScanResult, include_upcoming: bool, *, mode: str = "read-only") -> str:
    report = result.report
    if mode == "delete":
        banner = "Deleting virtual leftovers that have no file path."
    elif mode == "plan":
        banner = "Deletion plan. Nothing was modified."
    else:
        banner = "Read-only. Nothing was modified."
    lines = [
        f"{result.server_name} ({result.server_version})",
        result.base_url,
        banner,
        "",
        f"Leftover episodes:          {len(report.leftover_episodes)}",
        f"Leftover virtual seasons:   {len(report.leftover_seasons)}",
        f"Empty season folders:       {len(report.empty_seasons)}",
        f"Upcoming virtual episodes:  {len(report.upcoming_episodes)}"
        + ("" if include_upcoming else "  (hidden; pass --include-upcoming)"),
        f"Upcoming virtual seasons:   {len(report.upcoming_seasons)}"
        + ("" if include_upcoming else "  (hidden; pass --include-upcoming)"),
        f"Needs review:               {len(report.review)}",
    ]
    for warning in report.warnings:
        lines.extend(["", f"Warning: {warning}"])

    blocks: dict[str, dict[str, _SeasonBlock]] = {}

    def block_key(item: MediaItem) -> str:
        if item.item_type == "Season":
            return item.id
        return item.season_id or season_label(item)

    def season_block(item: MediaItem) -> _SeasonBlock:
        series_blocks = blocks.setdefault(item.series_name, {})
        key = block_key(item)
        if key not in series_blocks:
            series_blocks[key] = _SeasonBlock(
                order=(
                    item.season_number if item.season_number is not None else 10**9,
                    season_label(item).casefold(),
                    key,
                ),
                label=season_label(item),
            )
        return series_blocks[key]

    season_findings = list(report.leftover_seasons) + list(report.empty_seasons)
    if include_upcoming:
        season_findings.extend(report.upcoming_seasons)
    for finding in season_findings:
        block = season_block(finding.item)
        detail = finding.item.path or ""
        tag = finding.short or finding.reason
        header = f"{block.label} — {tag} — id={finding.item.id}"
        if detail:
            header += f" — {detail}"
        block.header = header
        if finding.notes:
            block.notes.append(finding.notes)

    episode_findings = list(report.leftover_episodes)
    if include_upcoming:
        episode_findings.extend(report.upcoming_episodes)
    episode_findings.sort(key=lambda finding: _episode_sort_key(finding.item))
    for finding in episode_findings:
        block = season_block(finding.item)
        block.episodes.append(
            f"{episode_label(finding.item)} — id={finding.item.id} — {_aired_text(finding.item, finding.kind)}"
        )

    if blocks:
        lines.append("")
        for series_name in sorted(blocks, key=str.casefold):
            lines.append("")
            lines.append(series_name)
            for block in sorted(blocks[series_name].values(), key=lambda item: item.order):
                lines.append(f"  {block.header or block.label}")
                for note in block.notes:
                    lines.append(f"    {note}")
                for episode in block.episodes:
                    lines.append(f"    {episode}")
    elif report.leftover_total == 0 and not (include_upcoming and (report.upcoming_episodes or report.upcoming_seasons)):
        lines.extend(["", "No leftover episodes or seasons found."])

    if report.review:
        lines.extend(["", "Needs review (not leftovers)"])
        for finding in report.review:
            lines.append(
                f"  {finding.item.series_name} — {episode_label(finding.item) if finding.item.item_type == 'Episode' else season_label(finding.item)}"
                f" — id={finding.item.id} — {finding.reason}"
            )

    lines.extend([
        "",
        "Upcoming rows are metadata for episodes that have not aired. They are not leftovers.",
        "A library scan has to finish before deleted files show up here. Jellyfin only marks an item",
        "virtual after it notices the file is gone. Rows you never downloaded also show up when",
        "missing-episode display is enabled; the server does not remember that a file used to exist.",
    ])
    return "\n".join(lines) + "\n"


def render_deletion(plan: DeletionPlan, outcome: DeletionOutcome | None) -> str:
    lines = ["", "Deletion"]
    if outcome is None:
        lines.append("Nothing was deleted.")
        lines.append("")
        lines.append(f"Will remove: {len(plan.remove)}")
        for finding in plan.remove:
            lines.append(f"  {_describe(finding)}")
        if plan.skip:
            lines.append("")
            lines.append(f"Left in place: {len(plan.skip)}")
            for deferred in plan.skip:
                lines.append(f"  {_describe(deferred.finding)} — {deferred.reason}")
        if plan.remove:
            lines.extend(["", "Re-run with --delete --yes to remove the rows under \"Will remove\"."])
        else:
            lines.extend(["", "Nothing to delete."])
        return "\n".join(lines) + "\n"

    lines.append(f"Deleted: {len(outcome.deleted)}")
    for removed in outcome.deleted:
        lines.append(f"  {_describe(removed.finding)} — {removed.detail}")
    if outcome.failed:
        lines.append("")
        lines.append(f"Failed: {len(outcome.failed)}")
        for deferred in outcome.failed:
            lines.append(f"  {_describe(deferred.finding)} — {deferred.reason}")
    if outcome.skipped:
        lines.append("")
        lines.append(f"Left in place: {len(outcome.skipped)}")
        for deferred in outcome.skipped:
            lines.append(f"  {_describe(deferred.finding)} — {deferred.reason}")
    return "\n".join(lines) + "\n"


def _deletion_to_dict(plan: DeletionPlan, outcome: DeletionOutcome | None) -> dict[str, Any]:
    def deferred_row(deferred: Deferred) -> dict[str, Any]:
        row = _finding_to_dict(deferred.finding)
        row["reason"] = deferred.reason
        return row

    if outcome is None:
        return {
            "performed": False,
            "would_remove": [_finding_to_dict(finding) for finding in plan.remove],
            "skipped": [deferred_row(deferred) for deferred in plan.skip],
            "deleted": [],
            "failed": [],
        }
    deleted = []
    for removed in outcome.deleted:
        row = _finding_to_dict(removed.finding)
        row["result"] = removed.detail
        deleted.append(row)
    return {
        "performed": True,
        "would_remove": [_finding_to_dict(finding) for finding in plan.remove],
        "skipped": [deferred_row(deferred) for deferred in outcome.skipped],
        "deleted": deleted,
        "failed": [deferred_row(deferred) for deferred in outcome.failed],
    }


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30,
        verify: bool = True,
        page_size: int = PAGE_SIZE_DEFAULT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.page_size = page_size
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = authorization_header(api_key)
        self.session.headers["Accept"] = "application/json"
        self.session.verify = verify

    def _send(self, method: str, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        try:
            response = self.session.request(method, f"{self.base_url}{path}", params=params, timeout=self.timeout)
        except requests.Timeout as exc:
            raise JellyfinError(f"Timed out talking to {self.base_url}.") from exc
        except requests.RequestException as exc:
            raise JellyfinError(f"Could not connect to {self.base_url}. Check the address, port, and that Jellyfin is running.") from exc
        if response.status_code == 401:
            raise JellyfinAuthError(
                "Jellyfin rejected the API key (HTTP 401). "
                "Create one in Dashboard → Advanced → API Keys. "
                'This script sends Authorization: MediaBrowser Token="...". '
                "Jellyfin 12 rejects the older X-Emby-Token header even when the key itself is valid."
            )
        return response

    def _read_json(self, response: requests.Response, path: str) -> dict[str, Any]:
        if response.status_code != 200:
            detail = response.text.strip().replace("\n", " ")[:300]
            raise JellyfinError(f"Jellyfin returned HTTP {response.status_code} for {path}. {detail}".rstrip())
        try:
            payload = response.json()
        except ValueError as exc:
            raise JellyfinError(
                f"{self.base_url} did not return JSON for {path}. "
                "Use the server root, for example http://host:8096, not the /web/ page."
            ) from exc
        if not isinstance(payload, dict):
            raise JellyfinError(f"Jellyfin returned an unexpected payload for {path}.")
        return payload

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._read_json(self._send("GET", path, params), path)

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        """Return one library item, or None when Jellyfin no longer has it.

        The single-item route ``GET /Items/{id}`` resolves the caller with
        ``GetUserById``. An API key has no user, so that call throws
        ``ArgumentException`` and Jellyfin answers HTTP 400 with
        ``Error processing request.`` for every id. The list route already
        used by the scan allows an API key and still returns virtual rows.
        """
        _require_item_id(item_id)
        payload = self._get("/Items", {
            "Ids": item_id,
            "Fields": ITEM_FIELDS,
            "Limit": 1,
            "Recursive": "true",
            "EnableTotalRecordCount": "false",
        })
        items = payload.get("Items") or []
        if not isinstance(items, list):
            raise JellyfinError("Jellyfin returned an Items field that is not a list.")
        for item in items:
            if isinstance(item, dict) and _clean(item.get("Id")) == item_id:
                return item
        return None

    def delete_item(self, item_id: str) -> str:
        """Delete one item. Returns ``missing`` when it was already gone."""
        _require_item_id(item_id)
        path = f"/Items/{item_id}"
        response = self._send("DELETE", path)
        if response.status_code == 404:
            return "missing"
        if response.status_code in {200, 204}:
            return "deleted"
        detail = response.text.strip().replace("\n", " ")[:300]
        if response.status_code == 400 and detail == "Error processing request.":
            detail = (
                "Jellyfin hid an ArgumentException. The server log line "
                f"Error processing request. URL DELETE /Items/{item_id} has the real exception. "
                "The public delete API always tries to remove a file. On a server older than 12 "
                "that throws when a virtual item has an empty path, and there is no request flag to turn file deletion off."
            )
        raise JellyfinError(f"Jellyfin returned HTTP {response.status_code} for {path}. {detail}".rstrip())

    def server_info(self) -> tuple[str, str]:
        payload = self._get("/System/Info", {})
        name = _clean(payload.get("ServerName")) or "Jellyfin"
        version = _clean(payload.get("Version")) or "unknown"
        return name, version

    def iter_items(self, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
        start = 0
        seen: set[str] = set()
        total: int | None = None
        for _page in range(100000):
            page_params = dict(params)
            page_params.update({
                "StartIndex": start,
                "Limit": self.page_size,
                "Recursive": "true",
                "EnableTotalRecordCount": "true",
                "Fields": params.get("Fields", ITEM_FIELDS),
            })
            payload = self._get("/Items", page_params)
            items = payload.get("Items") or []
            if not isinstance(items, list):
                raise JellyfinError("Jellyfin returned an Items field that is not a list.")
            if total is None and isinstance(payload.get("TotalRecordCount"), int):
                total = payload["TotalRecordCount"]

            fresh: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = _clean(item.get("Id"))
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                fresh.append(item)
            if items and not fresh:
                # The server ignored StartIndex and repeated the same page.
                break
            yield from fresh

            received = len(items)
            start += received
            if received == 0:
                break
            if total is not None and start >= total:
                break
            if received < self.page_size:
                break
        else:
            raise JellyfinError("Stopped after 100000 pages. The server did not finish the item list.")


def scan(client: JellyfinClient, now: datetime | None = None, log: TextIO | None = None) -> ScanResult:
    def say(message: str) -> None:
        if log is not None:
            print(message, file=log)

    say(f"Connecting to {client.base_url}")
    name, version = client.server_info()
    say(f"Server {name} {version}")
    say("Reading virtual episodes and seasons...")
    virtual = list(client.iter_items({
        "IncludeItemTypes": "Episode,Season",
        "IsMissing": "true",
        "Fields": ITEM_FIELDS,
    }))
    say("Reading on-disk seasons...")
    filesystem_seasons = list(client.iter_items({
        "IncludeItemTypes": "Season",
        "IsMissing": "false",
        "LocationTypes": "FileSystem",
        "Fields": ITEM_FIELDS,
    }))
    report = build_report(virtual, filesystem_seasons, now=now)
    return ScanResult(report=report, server_name=name, server_version=version, base_url=client.base_url)


def _read_api_key(args: argparse.Namespace) -> str:
    if args.api_key_file:
        with open(args.api_key_file, encoding="utf-8") as handle:
            return handle.read().strip()
    return args.api_key or ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find leftover virtual episodes and seasons in Jellyfin. Read-only unless --delete --yes is passed.",
        epilog=(
            "Set JELLYFIN_URL and JELLYFIN_API_KEY, or copy .env.example to .env. "
            "Example: python find_orphans.py"
        ),
    )
    parser.add_argument("--url", default=os.environ.get("JELLYFIN_URL", ""), help="Jellyfin server root, such as http://192.168.1.10:8096")
    parser.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY", ""), help="API key. Prefer JELLYFIN_API_KEY or --api-key-file so the key stays out of the process list.")
    parser.add_argument("--api-key-file", help="File whose contents are the API key")
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds (default: 30)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE_DEFAULT, help="Items requested per page (default: 200)")
    parser.add_argument("--series", help="Only include series whose name contains this text")
    parser.add_argument("--include-upcoming", action="store_true", help="Also print virtual episodes and seasons that have not premiered yet")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print the report as JSON")
    parser.add_argument("--output", help="Write the JSON report to this file")
    parser.add_argument("--delete", action="store_true", help="Remove virtual leftover episodes and seasons that have no file path. Requires --yes.")
    parser.add_argument("--yes", action="store_true", help="Confirm --delete. Without this flag the script only prints what it would remove.")
    parser.add_argument("--delete-empty-folders", action="store_true", help="With --delete, also remove empty season folders from disk.")
    parser.add_argument("--insecure", action="store_true", help="Do not verify TLS certificates")
    parser.add_argument("--strict", action="store_true", help="Exit with status 2 when a read-only scan finds leftovers")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file(os.path.join(os.getcwd(), ".env"))
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        url = (args.url or "").strip()
        api_key = _read_api_key(args)
        if not url or not api_key:
            raise ConfigError(
                "Set JELLYFIN_URL and JELLYFIN_API_KEY, or pass --url and --api-key. "
                "A copy of the expected variables is in .env.example."
            )
        if looks_like_placeholder(url) or looks_like_placeholder(api_key):
            raise ConfigError(
                "JELLYFIN_URL and JELLYFIN_API_KEY still look like placeholders. "
                "Use the server address you open in the browser (without /web) and a key from Dashboard → Advanced → API Keys."
            )
        if not url.startswith(("http://", "https://")):
            raise ConfigError("JELLYFIN_URL must start with http:// or https://.")
        if args.page_size < 1 or args.page_size > 1000:
            raise ConfigError("--page-size must be between 1 and 1000.")
        if args.timeout <= 0:
            raise ConfigError("--timeout must be greater than 0.")
        if args.yes and not args.delete:
            raise ConfigError("--yes confirms --delete. Pass both flags to remove leftovers.")
        if args.delete_empty_folders and not args.delete:
            raise ConfigError("--delete-empty-folders is used together with --delete.")

        client = JellyfinClient(
            base_url=url,
            api_key=api_key,
            timeout=args.timeout,
            verify=not args.insecure,
            page_size=args.page_size,
        )
        result = scan(client, log=sys.stderr)
        if args.series:
            result = ScanResult(
                report=filter_report(result.report, args.series),
                server_name=result.server_name,
                server_version=result.server_version,
                base_url=result.base_url,
            )
        if args.delete and args.yes:
            mode = "delete"
        elif args.delete:
            mode = "plan"
        else:
            mode = "read-only"
        plan = plan_deletion(result.report, delete_empty_folders=args.delete_empty_folders) if args.delete else None
        outcome = None
        if plan is not None and args.yes:
            outcome = remove_leftovers(client, plan, log=sys.stderr)
        payload = report_to_dict(result)
        payload["read_only"] = outcome is None or not outcome.deleted
        if plan is not None:
            payload["deletion"] = _deletion_to_dict(plan, outcome)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
        if args.as_json:
            json.dump(payload, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(render_text(result, include_upcoming=args.include_upcoming, mode=mode))
            if plan is not None:
                sys.stdout.write(render_deletion(plan, outcome))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except JellyfinError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.delete and not args.yes:
        return 2 if plan is not None and plan.remove else 0
    if outcome is not None and outcome.failed:
        return 1
    if not args.delete and args.strict and result.report.leftover_total:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
