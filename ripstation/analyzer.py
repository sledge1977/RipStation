import csv
import os
import re
from collections.abc import Iterable
from pathlib import Path
from statistics import median
from typing import Any

from ripstation.config import (
    SERIES_MIN_LENGTH_SECONDS,
    SERIES_TYPICAL_MAX_SECONDS,
    SERIES_TYPICAL_MIN_SECONDS,
)
from ripstation.models import RipJob, SeriesAnalysis, Track

EPISODE_PATTERNS = (
    re.compile(
        r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})[ ._-]*E(?P<episode>\d{1,3})(?!\d)"
    ),
    re.compile(r"(?i)(?<![A-Z0-9])(?P<season>\d{1,3})x(?P<episode>\d{1,3})(?!\d)"),
    re.compile(
        r"(?i)(?<![A-Z0-9])(?:episode|ep|folge)[ ._-]*(?P<episode>\d{1,3})(?!\d)"
    ),
)


def parse_duration_to_seconds(duration_str: str) -> int:
    parts = duration_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1:
            return int(parts[0])
    except (ValueError, IndexError):
        return 0
    return 0


TINFO_FIELDS = {
    2: "title_name",
    9: "duration_str",
    10: "size_str",
    11: "size_bytes",
    16: "source_filename",
    27: "output_filename",
}


def parse_tinfo_output(output: str) -> list[Track]:
    """Parse MakeMKV robot-mode TINFO lines into tracks."""
    tracks: dict[int, dict[str, Any]] = {}
    for line in output.splitlines():
        if not line.startswith("TINFO:"):
            continue
        try:
            row = next(csv.reader([line.split(":", 1)[1]]))
            if len(row) < 4:
                continue
            track_id = int(row[0])
            attribute_id = int(row[1])
            value = row[3]
            fields = tracks.setdefault(track_id, {"id": track_id})
            field = TINFO_FIELDS.get(attribute_id)
            if field == "size_bytes":
                fields[field] = int(value)
            elif field:
                fields[field] = value
            if field == "duration_str":
                fields["duration_seconds"] = parse_duration_to_seconds(value)
        except (IndexError, ValueError, csv.Error):
            continue
    return [Track(**tracks[track_id]) for track_id in sorted(tracks)]


def parse_track_selection(selection_str: str) -> list[int]:
    selection: list[int] = []
    seen: set[int] = set()
    selection_str = "".join(selection_str.split())
    if not selection_str:
        return []
    try:
        for part in selection_str.split(","):
            if not part:
                continue
            if "-" in part:
                start, end = map(int, part.split("-"))
                step = 1 if end >= start else -1
                values: Iterable[int] = range(start, end + step, step)
            else:
                values = [int(part)]
            for value in values:
                if value not in seen:
                    selection.append(value)
                    seen.add(value)
    except ValueError:
        return []
    return selection


def format_number_ranges(numbers: Any) -> str:
    """Format [1, 2, 3, 6] as '1-3,6'."""
    num_list = list(numbers)
    if not num_list:
        return ""
    parts = []
    start = previous = num_list[0]
    step = None
    for number in num_list[1:]:
        current_step = number - previous
        if step is None and abs(current_step) == 1:
            step = current_step
        if step is not None and current_step == step:
            previous = number
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
        step = None
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def safe_media_name(value: str) -> str:
    """Create one portable path component without changing ordinary spaces."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    value = value.rstrip(". ")
    windows_reserved = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)")
    if windows_reserved.match(value):
        value = f"_{value}"
    return value


def clean_label_for_suggestion(label: str) -> str:
    """Create a sensible default title from a raw disc label."""
    if not label:
        return ""
    text = label.strip()
    text = re.sub(r"[_.]+", " ", text).strip()
    # Strip common disc-specific suffixes like Disc 1, S01D02, Part 2
    text = re.sub(
        r"(?i)\b(?:disc|disk|cd|dvd|bd|part|vol|volume|seite|side|season|s)\s*\d+.*$",
        "",
        text,
    ).strip()
    text = re.sub(r"(?i)\b(?:d|disc|disk)\d+.*$", "", text).strip()
    if text.isupper() and len(text) > 3:
        text = text.title()
    if text.lower() in (
        "dvd",
        "dvd video",
        "dvdrom",
        "bdrom",
        "bluray",
        "blu ray",
        "disc",
        "untitled",
    ):
        return ""
    return safe_media_name(text)


def canonical_output_path(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def extract_episode_info(track: Track) -> tuple[int | None, int | None]:
    """Return (season, episode) if a track label contains an explicit marker."""
    for text in (track.title_name, track.source_filename, track.output_filename):
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(text)
            if match:
                season = match.groupdict().get("season")
                return (int(season) if season else None, int(match.group("episode")))
    return (None, None)


def _duration_clusters(tracks: list[Track]) -> list[list[Track]]:
    """Group tracks with episode-like, similar runtimes."""
    clusters: list[list[Track]] = []
    for track in sorted(tracks, key=lambda t: t.duration_seconds):
        duration = track.duration_seconds
        best_cluster = None
        best_distance = None
        for cluster in clusters:
            center = median(t.duration_seconds for t in cluster)
            tolerance = max(120, center * 0.12)
            distance = abs(duration - center)
            if distance <= tolerance and (
                best_distance is None or distance < best_distance
            ):
                best_cluster = cluster
                best_distance = distance
        if best_cluster is None:
            clusters.append([track])
        else:
            best_cluster.append(track)
    return clusters


def order_episode_tracks(tracks: list[Track]) -> list[Track]:
    """Use explicit episode markers only when they are complete and unambiguous."""
    tracks_list = list(tracks)
    identities = [extract_episode_info(track) for track in tracks_list]
    if not identities or not all(episode is not None for _, episode in identities):
        return sorted(tracks_list, key=lambda track: track.id)

    seasons = {season for season, _ in identities if season is not None}
    episode_numbers = [episode for _, episode in identities]
    if len(seasons) <= 1 and len(set(episode_numbers)) == len(episode_numbers):
        return [
            track
            for _, track in sorted(
                zip(identities, tracks_list, strict=True),
                key=lambda item: item[0][1] or 0,
            )
        ]

    if len(set(identities)) == len(identities):
        return [
            track
            for _, track in sorted(
                zip(identities, tracks_list, strict=True),
                key=lambda item: (
                    item[0][0] if item[0][0] is not None else -1,
                    item[0][1],
                ),
            )
        ]
    return sorted(tracks_list, key=lambda track: track.id)


def analyze_series_tracks(tracks: list[Track]) -> SeriesAnalysis:
    """Recommend probable episode tracks while keeping the decision visible."""
    eligible = [
        track for track in tracks if track.duration_seconds >= SERIES_MIN_LENGTH_SECONDS
    ]
    forced_low_confidence = False
    if not eligible:
        eligible = [track for track in tracks if track.duration_seconds > 0]
        forced_low_confidence = True
    if not eligible:
        return SeriesAnalysis([], {}, "niedrig", 0)

    unique_tracks = []
    fingerprints: dict[tuple[int, int], list[Track]] = {}
    duplicate_of: dict[int, int] = {}
    for track in sorted(eligible, key=lambda t: t.id):
        size = track.size_bytes
        fingerprint = (track.duration_seconds, size) if size else None
        matching_tracks = fingerprints.get(fingerprint, []) if fingerprint else []
        if matching_tracks:
            current_identity = extract_episode_info(track)
            matching_episode = next(
                (
                    candidate
                    for candidate in matching_tracks
                    if extract_episode_info(candidate) == current_identity
                ),
                None,
            )
            if current_identity[1] is None or matching_episode is not None:
                duplicate_of[track.id] = (matching_episode or matching_tracks[0]).id
            else:
                unique_tracks.append(track)
                matching_tracks.append(track)
        else:
            unique_tracks.append(track)
            if fingerprint:
                fingerprints[fingerprint] = [track]

    clusters = _duration_clusters(unique_tracks)
    if not clusters:
        return SeriesAnalysis([], duplicate_of, "niedrig", 0)

    explicitly_numbered = [
        track for track in unique_tracks if extract_episode_info(track)[1] is not None
    ]
    explicit_identities = [extract_episode_info(track) for track in explicitly_numbered]
    explicit_seasons = {
        season for season, _ in explicit_identities if season is not None
    }
    if len(explicit_seasons) > 1:
        tracks_by_season = {
            season: [
                track
                for track in explicitly_numbered
                if extract_episode_info(track)[0] == season
            ]
            for season in explicit_seasons
        }
        selected_season = min(
            explicit_seasons,
            key=lambda season: (-len(tracks_by_season[season]), season),
        )
        best_season_tracks = order_episode_tracks(tracks_by_season[selected_season])
        center = int(median(track.duration_seconds for track in best_season_tracks))
        return SeriesAnalysis(
            [track.id for track in best_season_tracks],
            duplicate_of,
            "niedrig",
            center,
        )

    if len(explicitly_numbered) >= 2 and len(set(explicit_identities)) == len(
        explicit_identities
    ):
        center = int(median(track.duration_seconds for track in explicitly_numbered))
        confidence = (
            "hoch" if len(explicitly_numbered) == len(unique_tracks) else "mittel"
        )
        ordered = order_episode_tracks(explicitly_numbered)
        return SeriesAnalysis(
            [track.id for track in ordered], duplicate_of, confidence, center
        )

    def cluster_score(cluster: list[Track]) -> tuple[bool, int, float]:
        center = median(t.duration_seconds for t in cluster)
        typical = SERIES_TYPICAL_MIN_SECONDS <= center <= SERIES_TYPICAL_MAX_SECONDS
        return (typical, len(cluster), center)

    best = max(clusters, key=cluster_score)
    best_center = int(median(t.duration_seconds for t in best))
    share = len(best) / max(1, len(unique_tracks))
    if forced_low_confidence:
        confidence = "niedrig"
    elif len(best) >= 3 and share >= 0.6:
        confidence = "hoch"
    elif len(best) >= 2:
        confidence = "mittel"
    else:
        confidence = "niedrig"

    ordered = order_episode_tracks(best)
    return SeriesAnalysis(
        [track.id for track in ordered], duplicate_of, confidence, best_center
    )


def build_series_jobs(
    series_name: str,
    season_number: int,
    tracks: list[Track],
    episode_numbers: list[int],
    base_output_dir: str,
) -> list[RipJob]:
    if len(tracks) != len(episode_numbers):
        raise ValueError("Track- und Folgennummern müssen gleich lang sein")
    safe_name = safe_media_name(series_name)
    if not safe_name:
        raise ValueError("Der Serienname darf nicht leer sein")
    jobs = []
    for track, episode_number in zip(tracks, episode_numbers, strict=True):
        filename = f"{safe_name}.S{season_number:02d}E{episode_number:02d}.mkv"
        output = os.path.join(
            base_output_dir, safe_name, f"Season {season_number:02d}", filename
        )
        jobs.append(RipJob(track.id, output, track.output_filename))
    return jobs


def next_episode_number(
    series_name: str,
    season_number: int,
    base_output_dir: str,
    reserved_outputs: set[str] | None = None,
    job_state_lock: Any = None,
) -> int:
    """Continue after existing and currently reserved episodes."""
    safe_name = safe_media_name(series_name)
    season_dir = Path(base_output_dir) / safe_name / f"Season {season_number:02d}"
    # Improved regex: supports dot, space, dash, or underscore before S01E01
    pattern = re.compile(rf"(?i)(?:^|[ ._-])S{season_number:02d}E(\d+)")
    existing_numbers: list[int] = []
    canonical_season_dir = canonical_output_path(season_dir)

    def scan_files():
        if season_dir.is_dir():
            for path in season_dir.glob("*.mkv"):
                match = pattern.search(path.name)
                if match:
                    existing_numbers.append(int(match.group(1)))
        if reserved_outputs is not None:
            for output in reserved_outputs:
                if canonical_output_path(Path(output).parent) != canonical_season_dir:
                    continue
                match = pattern.search(Path(output).name)
                if match:
                    existing_numbers.append(int(match.group(1)))

    if job_state_lock is not None:
        with job_state_lock:
            scan_files()
    else:
        scan_files()

    return max(existing_numbers, default=0) + 1
