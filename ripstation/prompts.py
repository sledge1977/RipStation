import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ripstation.analyzer import (
    analyze_series_tracks,
    build_series_jobs,
    canonical_output_path,
    extract_episode_info,
    format_number_ranges,
    next_episode_number,
    order_episode_tracks,
    parse_track_selection,
    safe_media_name,
)
from ripstation.config import BASE_OUTPUT_DIR
from ripstation.models import SeriesAnalysis
from ripstation.ui import fit_text, table_row
from ripstation.worker import job_state_lock, reserved_outputs


def prompt_integer(
    prompt: str,
    default: Optional[int] = None,
    minimum: int = 0,
    allow_cancel: bool = True,
) -> Optional[int]:
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if allow_cancel and value.lower() == "q":
            return None
        if not value and default is not None:
            return default
        try:
            number = int(value)
            if number >= minimum:
                return number
        except ValueError:
            pass
        print(f"Bitte eine ganze Zahl ab {minimum} eingeben (oder 'q' zum Abbrechen).")


def render_series_tracks(
    tracks: List[Dict[str, Any]], analysis: SeriesAnalysis, width: Optional[int] = None
) -> str:
    """Render the episode candidate list without exceeding terminal width."""
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    width = max(20, width if width is not None else terminal_width)
    recommended = analysis.recommended_ids

    def marker_for(track):
        if track["id"] in analysis.duplicate_of:
            return f"~{analysis.duplicate_of[track['id']]}"
        return "*" if track["id"] in recommended else ""

    def source_for(track):
        parts = [
            track.get(field)
            for field in ("source_filename", "title_name")
            if track.get(field)
        ]
        return " | ".join(parts) or track.get("output_filename", "")

    lines = []
    if width >= 76:
        widths = [6, 4, 10, 10, width - 42]
        lines.append(
            table_row(["Mark.", "ID", "Dauer", "Größe", "Quelle / Titel"], widths)
        )
        lines.append("─" * width)
        for track in tracks:
            lines.append(
                table_row(
                    [
                        marker_for(track),
                        track["id"],
                        track.get("duration_str", "?"),
                        track.get("size_str", "?"),
                        source_for(track),
                    ],
                    widths,
                )
            )
    elif width >= 48:
        widths = [4, 3, 9, width - 25]
        lines.append(table_row(["M.", "ID", "Dauer", "Quelle / Titel"], widths))
        lines.append("─" * width)
        for track in tracks:
            lines.append(
                table_row(
                    [
                        marker_for(track),
                        track["id"],
                        track.get("duration_str", "?"),
                        source_for(track),
                    ],
                    widths,
                )
            )
    else:
        for track in tracks:
            heading = (
                f"{marker_for(track):>3} [{track['id']}] "
                f"{track.get('duration_str', '?')} · {track.get('size_str', '?')}"
            )
            lines.extend(
                (fit_text(heading, width), fit_text(f"    {source_for(track)}", width))
            )
    return "\n".join(lines)


def prompt_series_jobs(
    tracks: List[Dict[str, Any]],
    default_name: str = "",
    base_output_dir: Optional[str] = None,
) -> List[Tuple[int, str, str]]:
    """Interactive series setup. Returns validated rip jobs or an empty list."""
    analysis = analyze_series_tracks(tracks)
    recommended = analysis.recommended_ids
    print("\nGefundene Titel (* Empfehlung, ~ mögliche Dublette):")
    print(render_series_tracks(tracks, analysis))
    if recommended:
        print(
            f"Empfehlung ({analysis.confidence}, typische Dauer "
            f"{analysis.median_duration_seconds // 60} Min.): {format_number_ranges(recommended)}"
        )
    else:
        print("Keine verlässliche Empfehlung möglich.")

    default_selection = format_number_ranges(recommended)
    while True:
        terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
        if terminal_width < 60:
            prompt = f"Tracks (*=alle, q=zurück, Enter={default_selection})"
        else:
            suffix = f" [Enter={default_selection}]" if default_selection else ""
            prompt = f"Track-IDs, '*' für alle oder 'q' zum Abbrechen{suffix}"
        selection = input(f"{prompt}: ").strip()
        if selection.lower() == "q":
            return []
        if not selection:
            selected_ids = recommended
        elif selection == "*":
            selected_ids = [track["id"] for track in tracks]
        else:
            selected_ids = parse_track_selection(selection)
        available = {track["id"] for track in tracks}
        if selected_ids and all(track_id in available for track_id in selected_ids):
            break
        print("Ungültige oder leere Auswahl.")

    by_id = {track["id"]: track for track in tracks}
    selected_tracks = [by_id[track_id] for track_id in selected_ids]
    if not selection and recommended:
        selected_tracks = order_episode_tracks(selected_tracks)

    order_default = format_number_ranges(track["id"] for track in selected_tracks)
    order_value = input(
        f"Reihenfolge [Enter={order_default}, 'r'=umkehren, sonst Track-IDs, q=Abbrechen]: "
    ).strip()
    if order_value.lower() == "q":
        return []
    if order_value.lower() == "r":
        selected_tracks.reverse()
    elif order_value:
        ordered_ids = parse_track_selection(order_value)
        if set(ordered_ids) != set(selected_ids) or len(ordered_ids) != len(
            selected_ids
        ):
            print("Reihenfolge enthält nicht genau die ausgewählten Tracks. Abbruch.")
            return []
        selected_tracks = [by_id[track_id] for track_id in ordered_ids]

    inferred_seasons = {
        season
        for season, episode in map(extract_episode_info, selected_tracks)
        if season is not None and episode is not None
    }
    if len(inferred_seasons) > 1:
        season_list = ", ".join(str(season) for season in sorted(inferred_seasons))
        print(
            f"Abbruch: Die Auswahl enthält Titel aus mehreren Staffeln ({season_list})."
        )
        print("Bitte nur Titel einer Staffel auswählen.")
        return []

    name_prompt = (
        f"Serienname [Enter={default_name}, q=Abbrechen]"
        if default_name
        else "Serienname [q=Abbrechen]"
    )
    raw_name = input(f"{name_prompt}: ").strip()
    if raw_name.lower() == "q":
        return []
    if not raw_name and default_name:
        raw_name = default_name
    if not safe_media_name(raw_name):
        print("Serienname ist leer oder ungültig. Abbruch.")
        return []

    season_default = next(iter(inferred_seasons)) if inferred_seasons else 1
    season_number = prompt_integer("Staffel", season_default, minimum=0)
    if season_number is None:
        return []

    base_dir = base_output_dir or BASE_OUTPUT_DIR
    inferred_episodes = [extract_episode_info(track)[1] for track in selected_tracks]
    if not all(number is not None for number in inferred_episodes):
        episode_start = next_episode_number(
            raw_name,
            season_number,
            base_output_dir=base_dir,
            reserved_outputs=reserved_outputs,
            job_state_lock=job_state_lock,
        )
        inferred_episodes = list(
            range(episode_start, episode_start + len(selected_tracks))
        )
    episode_default = format_number_ranges(inferred_episodes)
    while True:
        value = input(
            f"Folgennummern [Enter={episode_default}; z.B. 5-8 oder 5,6,9,10, q=Abbrechen]: "
        ).strip()
        if value.lower() == "q":
            return []
        episode_numbers = (
            inferred_episodes if not value else parse_track_selection(value)
        )
        if len(episode_numbers) == len(selected_tracks) and all(
            n >= 1 for n in episode_numbers
        ):
            if len(set(episode_numbers)) == len(episode_numbers):
                break
        print(
            f"Bitte genau {len(selected_tracks)} unterschiedliche Folgennummern eingeben."
        )

    return build_series_jobs(
        raw_name,
        season_number,
        selected_tracks,
        episode_numbers,
        base_output_dir=base_dir,
    )


def prompt_movie_jobs(
    tracks: List[Dict[str, Any]],
    default_name: str = "",
    base_output_dir: Optional[str] = None,
) -> List[Tuple[int, str, str]]:
    """Let the user confirm the longest movie candidate or choose another one."""
    longest = max(tracks, key=lambda track: track.get("duration_seconds", 0))
    analysis = SeriesAnalysis(
        recommended_ids=[longest["id"]],
        duplicate_of={},
        confidence="",
        median_duration_seconds=longest.get("duration_seconds", 0),
    )
    print("\nGefundene Filmtitel (* längster Titel / Empfehlung):")
    print(render_series_tracks(tracks, analysis))
    while True:
        value = input(f"Track-ID [Enter={longest['id']}, q=Abbrechen]: ").strip()
        if value.lower() == "q":
            return []
        selected_id = longest["id"] if not value else None
        if value:
            parsed = parse_track_selection(value)
            if len(parsed) == 1:
                selected_id = parsed[0]
        selected = next((track for track in tracks if track["id"] == selected_id), None)
        if selected:
            break
        print("Bitte genau eine vorhandene Track-ID eingeben.")

    name_prompt = (
        f"Filmname [Enter={default_name}, q=Abbrechen]"
        if default_name
        else "Filmname [q=Abbrechen]"
    )
    name_input = input(f"{name_prompt}: ").strip()
    if name_input.lower() == "q":
        return []
    if not name_input and default_name:
        raw_name = default_name
    else:
        raw_name = name_input
    movie_name = safe_media_name(raw_name)
    if not movie_name:
        print("Filmname ist leer oder ungültig. Abbruch.")
        return []
    base_dir = base_output_dir or BASE_OUTPUT_DIR
    output = os.path.join(base_dir, movie_name, f"{movie_name}.mkv")
    return [(selected["id"], output, selected.get("output_filename", ""))]
