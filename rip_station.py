#!/usr/bin/env python3
"""RipStation - Multi-Drive Ripping Station for Movies and TV Series."""

import csv
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Windows-specific import
if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty

from ripstation.analyzer import (
    analyze_series_tracks,
    clean_label_for_suggestion,
    extract_episode_info,
    format_number_ranges,
    order_episode_tracks,
    parse_duration_to_seconds,
    parse_tinfo_output,
    parse_track_selection,
    safe_media_name,
)
from ripstation.analyzer import (
    build_series_jobs as _analyzer_build_series_jobs,
)
from ripstation.analyzer import (
    canonical_output_path as _analyzer_canonical_output_path,
)
from ripstation.analyzer import (
    next_episode_number as _analyzer_next_episode_number,
)
from ripstation.config import (
    MOVIE_MIN_LENGTH_SECONDS,
    SERIES_MIN_LENGTH_SECONDS,
    SERIES_SCAN_MIN_LENGTH_SECONDS,
    SERIES_TYPICAL_MAX_SECONDS,
    SERIES_TYPICAL_MIN_SECONDS,
    Config,
    default_output_dir,
    detect_makemkv_cmd,
    detect_mkvpropedit_cmd,
    parse_arguments,
)
from ripstation.models import Drive, SeriesAnalysis
from ripstation.prompts import (
    prompt_integer,
    render_series_tracks,
)
from ripstation.ui import (
    clear_screen,
    command_needs_more_digits,
    display_width,
    drive_info_text,
    fit_text,
    progress_text,
    read_menu_command,
    render_cards,
    render_table,
    status_text,
    table_row,
)
from ripstation.worker import (
    cancel_drive_rip as _worker_cancel_drive_rip,
)
from ripstation.worker import (
    ensure_process_stopped,
    find_created_mkv,
    move_without_overwrite,
    parse_progress_line,
    remove_empty_directory,
)
from ripstation.worker import (
    stop_all_workers as _worker_stop_all_workers,
)

# --- GLOBAL CONFIGURATION & STATE ---
BASE_OUTPUT_DIR = default_output_dir()
MAKEMKV_CMD = detect_makemkv_cmd()
MKVPROPEDIT_CMD = detect_mkvpropedit_cmd()
AUTO_EJECT = True

drives: List[Drive] = []
reserved_outputs: Set[str] = set()
job_state_lock = threading.Lock()


def drive_is_available(drive: Drive) -> bool:
    with job_state_lock:
        return not drive.busy


def canonical_output_path(path: Any) -> str:
    return _analyzer_canonical_output_path(path)


def release_output_paths(jobs: List[Tuple[int, str, str]]) -> None:
    paths = {canonical_output_path(output) for _, output, _ in jobs}
    with job_state_lock:
        reserved_outputs.difference_update(paths)


def build_series_jobs(
    series_name: str,
    season_number: int,
    tracks: List[Dict[str, Any]],
    episode_numbers: List[int],
) -> List[Tuple[int, str, str]]:
    return _analyzer_build_series_jobs(
        series_name,
        season_number,
        tracks,
        episode_numbers,
        base_output_dir=BASE_OUTPUT_DIR,
    )


def next_episode_number(series_name: str, season_number: int) -> int:
    return _analyzer_next_episode_number(
        series_name,
        season_number,
        base_output_dir=BASE_OUTPUT_DIR,
        reserved_outputs=reserved_outputs,
        job_state_lock=job_state_lock,
    )


# --- HARDWARE & MAKEMKV LOGIC ---


def eject_drive(device_path: str) -> None:
    """Eject the optical drive according to the operating system."""
    try:
        if os.name == "nt":
            clean_dev = device_path.rstrip("\\")
            if not clean_dev.endswith(":"):
                clean_dev += ":"
            ps_script = (
                f"$wm = New-Object -com 'WMPlayer.OCX'; "
                f"$cd = $wm.cdromCollection.getByDriveSpecifier('{clean_dev}'); "
                f"if ($cd) {{ $cd.Eject() }} else {{ exit 1 }}"
            )
            creation_flags = 0x08000000  # CREATE_NO_WINDOW
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                timeout=15,
                creationflags=creation_flags,
            )
            if result.returncode == 0:
                print(f"Laufwerk {clean_dev} ausgeworfen.")
            else:
                print(f"Hinweis: Bitte Laufwerk {device_path} manuell auswerfen.")
        else:
            subprocess.run(
                ["eject", device_path], check=True, capture_output=True, timeout=30
            )
            print(f"Laufwerk {device_path} ausgeworfen.")
    except Exception as e:
        print(f"Fehler beim Auswerfen: {e}")


def fetch_drive_info() -> List[Tuple[int, str, str, str]]:
    """Runs makemkvcon once and returns parsed drive tuples."""
    try:
        cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", "disc:9999"]
        creation_flags = 0x08000000 if os.name == "nt" else 0
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
            creationflags=creation_flags,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as e:
        raise RuntimeError(
            f"makemkvcon konnte nicht ausgeführt werden ({MAKEMKV_CMD}): {e}"
        ) from e

    drives_info = []
    for line in result.stdout.splitlines():
        if line.startswith("DRV:"):
            clean_line = line[4:]
            reader = csv.reader([clean_line])
            row = list(reader)[0]

            if len(row) >= 6:
                d_id = int(row[0])
                d_name = row[4][:14]
                d_label = row[5]
                d_dev = row[6] if len(row) >= 7 else ""
                if d_dev:
                    drives_info.append((d_id, d_name, d_label, d_dev))
    return drives_info


def scan_drives() -> None:
    global drives
    print("Scanne Laufwerke... bitte warten...")
    try:
        drives_info = fetch_drive_info()
        drives = [
            Drive(d_id, d_dev, d_label, d_name)
            for d_id, d_name, d_label, d_dev in drives_info
        ]
    except Exception as e:
        print(f"Fehler beim Suchlauf: {e}")
        drives = []


def rescan_idle_drives() -> None:
    print("Erneuter Suchlauf...")
    try:
        drives_info = fetch_drive_info()
    except Exception as error:
        print(f"Rescan fehlgeschlagen: {error}")
        time.sleep(2)
        return

    info_by_device = {
        d_dev: (d_id, d_name, d_label) for d_id, d_name, d_label, d_dev in drives_info
    }

    for drive in drives:
        info = info_by_device.pop(drive.device_path, None)
        if not drive_is_available(drive):
            continue
        if info:
            new_id, new_name, new_label = info
            drive.mkv_id = new_id
            drive.name = new_name
            drive.label = new_label
            drive.status = "IDLE"
            drive.current_job = ""
            drive.media_source = None
        else:
            drive.label = ""
            drive.status = "IDLE"
            drive.current_job = ""
            drive.media_source = None

    for d_dev, (d_id, d_name, d_label) in info_by_device.items():
        if not any(d.device_path == d_dev for d in drives):
            drives.append(Drive(d_id, d_dev, d_label, d_name))
    time.sleep(1)


def drive_source_candidates(drive: Drive) -> List[str]:
    """Prefer the physical device and retain disc ID as a compatibility fallback."""
    candidates = []
    if drive.device_path:
        candidates.append(f"dev:{drive.device_path}")
    candidates.append(f"disc:{drive.mkv_id}")
    return list(dict.fromkeys(candidates))


def get_disc_tracks(drive: Drive, min_len_seconds: int) -> List[Dict[str, Any]]:
    creation_flags = 0x08000000 if os.name == "nt" else 0
    errors = []
    for source in drive_source_candidates(drive):
        cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", source]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=600,
                creationflags=creation_flags,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ) as error:
            errors.append(f"{source}: {error}")
            continue

        drive.media_source = source
        tracks = parse_tinfo_output(result.stdout)
        return [
            track
            for track in tracks
            if track.get("duration_seconds", 0) >= min_len_seconds
        ]

    print("Trackscan fehlgeschlagen: " + "; ".join(errors))
    return []


# --- WORKER & PROCESS LIFECYCLE ---


def cancel_drive_rip(drive: Drive) -> bool:
    """Request cancellation of an active rip on a specific drive."""
    with job_state_lock:
        if not drive.busy:
            return False
        drive.cancel_requested = True
        proc = drive.active_process
    if proc is not None:
        ensure_process_stopped(proc, timeout=5)
    return True


def stop_all_workers(drives_list: Optional[List[Drive]] = None) -> None:
    """Stop all active rip processes."""
    to_stop = drives if drives_list is None else drives_list
    for drive in to_stop:
        if drive.busy or drive.active_process is not None:
            cancel_drive_rip(drive)


def _rip_jobs_worker(
    drive: Drive, jobs: List[Tuple[int, str, str]], disc_source: str
) -> None:
    drive.status = "Starting..."
    drive.progress = 0
    total_jobs = len(jobs)
    metadata_warning = False
    first_job_stem = os.path.splitext(
        os.path.basename(jobs[0][1] if jobs else "Unknown Job")
    )[0]
    original_job_name = re.sub(r"(?i)\.S\d+E\d+$", "", first_job_stem)

    for i, (track_id, final_output_path, source_mkv_filename) in enumerate(jobs):
        if drive.cancel_requested:
            drive.status = "CANCELLED"
            break

        job_progress_text = f"({i + 1}/{total_jobs})"
        drive.status = f"Ripping {job_progress_text}"
        drive.current_job = os.path.basename(final_output_path)
        drive.progress = 0

        target_dir = os.path.dirname(final_output_path)
        os.makedirs(target_dir, exist_ok=True)
        staging_dir = tempfile.mkdtemp(
            prefix=f".ripstation-{drive.mkv_id}-{track_id}-",
            dir=target_dir,
        )
        files_before_rip = {
            path for path in Path(staging_dir).glob("*.mkv") if path.is_file()
        }

        cmd = [
            MAKEMKV_CMD,
            "-r",
            "--progress=-same",
            "mkv",
            disc_source,
            str(track_id),
            staging_dir,
        ]

        log_file = os.path.join(os.path.dirname(jobs[0][1]), "rip.log")
        creation_flags = 0x08000000 if os.name == "nt" else 0

        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(
                f"\n--- Starting Job {job_progress_text}: Rip Track {track_id} ---\n"
            )
            proc: Optional[subprocess.Popen] = None
            try:
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        creationflags=creation_flags,
                    )
                    with job_state_lock:
                        drive.active_process = proc
                        cancelled = drive.cancel_requested
                    if cancelled:
                        ensure_process_stopped(proc)
                except OSError as error:
                    f.write(f"ERROR: MakeMKV konnte nicht gestartet werden: {error}\n")
                    drive.status = "ERROR (Start)"
                    remove_empty_directory(staging_dir)
                    break

                for line in proc.stdout:  # type: ignore
                    if drive.cancel_requested:
                        ensure_process_stopped(proc)
                        break
                    f.write(line)
                    progress = parse_progress_line(line)
                    if progress is not None:
                        drive.progress = progress

                proc.wait()
            finally:
                with job_state_lock:
                    drive.active_process = None
                ensure_process_stopped(proc)
                remove_empty_directory(staging_dir)

            if drive.cancel_requested:
                drive.status = "CANCELLED"
                f.write("WARNUNG: Vorgang wurde durch Benutzer abgebrochen.\n")
                remove_empty_directory(staging_dir)
                break

            drive.status = f"Processing {job_progress_text}"

            if proc.returncode == 0:
                try:
                    created_file = find_created_mkv(
                        staging_dir, source_mkv_filename, files_before_rip
                    )

                    move_without_overwrite(created_file, final_output_path)

                    # Metadata title (via MKVPROPEDIT_CMD)
                    try:
                        meta_result = subprocess.run(
                            [
                                MKVPROPEDIT_CMD,
                                final_output_path,
                                "--edit",
                                "info",
                                "--set",
                                f"title={os.path.splitext(os.path.basename(final_output_path))[0]}",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=120,
                            creationflags=creation_flags,
                        )
                        if meta_result.returncode != 0:
                            metadata_warning = True
                            f.write(
                                f"WARNING: mkvpropedit: {meta_result.stderr.strip()}\n"
                            )
                    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                        metadata_warning = True
                        f.write(
                            f"WARNING: mkvpropedit nicht verfügbar oder Timeout: {error}\n"
                        )
                except Exception as e:
                    f.write(f"ERROR: {e}\n")
                    drive.status = "ERROR (Post)"
                    remove_empty_directory(staging_dir)
                    break
            else:
                drive.status = "ERROR (Rip)"
                if Path(staging_dir).exists():
                    f.write(f"Unvollständige Dateien verbleiben in: {staging_dir}\n")
                else:
                    f.write(
                        "MakeMKV wurde mit einem Fehler beendet; "
                        "keine Teildatei vorhanden.\n"
                    )
                remove_empty_directory(staging_dir)
                break

        remove_empty_directory(staging_dir)

    if drive.cancel_requested:
        drive.status = "CANCELLED"
    elif not drive.status.startswith("ERROR"):
        final_status = "COMPLETED (META WARN)" if metadata_warning else "COMPLETED"
        drive.current_job = original_job_name
        drive.progress = 0
        if AUTO_EJECT:
            drive.status = "Ejecting..."
            eject_drive(drive.device_path)
        drive.status = final_status

    drive.progress = 0


def rip_jobs_worker(
    drive: Drive,
    jobs: List[Tuple[int, str, str]],
    disc_source: Optional[str] = None,
    owns_job_state: bool = False,
) -> None:
    """Keep unexpected filesystem/process errors from leaving a drive busy forever."""
    disc_source = disc_source or drive.media_source or drive_source_candidates(drive)[0]
    try:
        _rip_jobs_worker(drive, jobs, disc_source)
    except Exception as error:
        drive.status = "ERROR (Worker)"
        drive.progress = 0
        print(f"Unerwarteter Fehler bei Laufwerk {drive.device_path}: {error}")
    finally:
        if owns_job_state:
            release_output_paths(jobs)
            with job_state_lock:
                drive.busy = False
                drive.active_process = None


def start_rip_jobs(
    drive: Drive, jobs: List[Tuple[int, str, str]]
) -> Tuple[Optional[threading.Thread], Optional[str]]:
    """Atomically reserve a drive and all outputs before starting its worker."""
    if not jobs:
        return None, "Keine Rip-Aufträge vorhanden."

    outputs = [output for _, output, _ in jobs]
    canonical = [canonical_output_path(output) for output in outputs]
    with job_state_lock:
        if drive.busy:
            return None, f"Laufwerk {drive.mkv_id} ist bereits beschäftigt."
        duplicate_paths = {path for path in canonical if canonical.count(path) > 1}
        conflicts = [
            output
            for output, normalized in zip(outputs, canonical)
            if normalized in duplicate_paths
            or normalized in reserved_outputs
            or os.path.exists(output)
        ]
        if conflicts:
            return (
                None,
                "Zieldatei bereits vorhanden oder reserviert:\n  "
                + "\n  ".join(conflicts),
            )

        reserved_outputs.update(canonical)
        drive.busy = True
        drive.status = "Starting..."
        drive.progress = 0
        drive.cancel_requested = False
        disc_source = drive.media_source or drive_source_candidates(drive)[0]

    try:
        thread = threading.Thread(
            target=rip_jobs_worker,
            args=(drive, jobs, disc_source, True),
            name=f"rip-{drive.device_path}",
        )
        thread.start()
    except Exception as error:
        release_output_paths(jobs)
        with job_state_lock:
            drive.busy = False
            drive.status = "ERROR (Start)"
            drive.active_process = None
        return None, f"Worker konnte nicht gestartet werden: {error}"
    return thread, None


# --- PROMPTS & UI ---


def prompt_series_jobs(
    tracks: List[Dict[str, Any]], default_name: str = ""
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

    name_prompt = f"Serienname [Enter={default_name}]" if default_name else "Serienname"
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

    inferred_episodes = [extract_episode_info(track)[1] for track in selected_tracks]
    if not all(number is not None for number in inferred_episodes):
        episode_start = next_episode_number(raw_name, season_number)
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

    return build_series_jobs(raw_name, season_number, selected_tracks, episode_numbers)


def prompt_movie_jobs(
    tracks: List[Dict[str, Any]], default_name: str = ""
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

    name_prompt = f"Filmname [Enter={default_name}]" if default_name else "Filmname"
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
    output = os.path.join(BASE_OUTPUT_DIR, movie_name, f"{movie_name}.mkv")
    return [(selected["id"], output, selected.get("output_filename", ""))]


def prompt_cancel_rip() -> None:
    """Prompt user to choose a running drive and cancel its rip."""
    with job_state_lock:
        active = [d for d in drives if d.busy or d.active_process is not None]
    if not active:
        print("Kein aktiver Rip-Vorgang vorhanden.")
        time.sleep(1.5)
        return

    print("\nAktive Rips:")
    for d in active:
        print(
            f"  Laufwerk [{d.mkv_id}]: {d.device_path} - {d.status} ({d.current_job})"
        )
    ans = input("\nLaufwerks-ID zum Abbrechen eingeben (oder 'q' für Zurück): ").strip()
    if ans.lower() == "q" or not ans:
        return
    if ans.isdigit():
        target_id = int(ans)
        target_drive = next((d for d in active if d.mkv_id == target_id), None)
        if target_drive:
            cancel_drive_rip(target_drive)
            print(f"Abbruch für Laufwerk {target_id} angefordert.")
            time.sleep(1.5)
        else:
            print(f"Laufwerk {target_id} ist nicht aktiv.")
            time.sleep(1.5)


def render_ui(
    drive_list: Optional[List[Drive]] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> str:
    """Return a dashboard adapted to terminal width and height."""
    items = drives if drive_list is None else list(drive_list)
    terminal = shutil.get_terminal_size(fallback=(80, 24))
    width = max(20, width if width is not None else terminal.columns)
    height = max(8, height if height is not None else terminal.lines)

    title = f"RIPSTATION · {len(items)} Laufwerk{'e' if len(items) != 1 else ''}"
    lines = [fit_text(title, width, "center"), "─" * width]
    if width >= 110:
        lines.extend(render_table(items, width, height, wide=True))
    elif width >= 68:
        lines.extend(render_table(items, width, height, wide=False))
    else:
        lines.extend(render_cards(items, width, height))
    lines.append("─" * width)
    footer = "[ID] Rip starten  ·  [C] Abbrechen  ·  [R] Neu scannen  ·  [Q] Beenden"
    if width < 72:
        footer = "[ID] Rip  ·  [C] Stop  ·  [R] Scan  ·  [Q] Ende"
    lines.append(fit_text(footer, width, "center"))
    return "\n".join(lines)


def print_ui(drive_list: Optional[List[Drive]] = None) -> None:
    clear_screen()
    print(render_ui(drive_list))


# --- MAIN APPLICATION ENTRY POINT ---


def main(args: Optional[List[str]] = None) -> None:
    global BASE_OUTPUT_DIR, MAKEMKV_CMD, MKVPROPEDIT_CMD, AUTO_EJECT

    config = parse_arguments(args)
    BASE_OUTPUT_DIR = config.base_output_dir
    MAKEMKV_CMD = config.makemkv_cmd
    MKVPROPEDIT_CMD = config.mkvpropedit_cmd
    AUTO_EJECT = config.auto_eject

    def handle_shutdown(sig: int, _frame: Any) -> None:
        print("\nAbbruch-Signal empfangen. Beende aktive Prozesse...")
        stop_all_workers(drives)
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, handle_shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handle_shutdown)
    except (ValueError, OSError):
        pass

    scan_drives()

    last_snapshot = None
    force_render = True

    while True:
        terminal = shutil.get_terminal_size(fallback=(80, 24))
        drive_snapshot = tuple(
            (
                d.mkv_id,
                d.device_path,
                d.name,
                d.label,
                d.status,
                d.progress,
                d.current_job,
                d.busy,
            )
            for d in drives
        )
        snapshot = (terminal.columns, terminal.lines, drive_snapshot)

        if force_render or snapshot != last_snapshot:
            print_ui()
            print("Auswahl: ", end="", flush=True)
            last_snapshot = snapshot
            force_render = False

        user_input = read_menu_command(valid_ids=[drive.mkv_id for drive in drives])

        if user_input:
            print()
            user_input = str(user_input).strip()

            if user_input.lower() == "q":
                with job_state_lock:
                    active_any = any(d.busy for d in drives)
                if active_any:
                    print(
                        "Ein Rip läuft noch; Beenden ist erst danach möglich (oder 'c' zum Abbrechen)."
                    )
                    time.sleep(2)
                    force_render = True
                    continue
                return
            elif user_input.lower() == "r":
                rescan_idle_drives()
                force_render = True
            elif user_input.lower() == "c":
                prompt_cancel_rip()
                force_render = True
            elif user_input.isdigit():
                selected_drive = next(
                    (d for d in drives if d.mkv_id == int(user_input)), None
                )
                if selected_drive and drive_is_available(selected_drive):
                    clear_screen()
                    print(
                        f"--- Einrichtung Laufwerk {selected_drive.mkv_id} ({selected_drive.name}) ---"
                    )
                    media_type = (
                        input("Film (m/f) oder Serie (s)? [q=Zurück]: ").strip().lower()
                    )

                    if media_type in ["m", "f"]:
                        mode = "m"
                        min_len = config.movie_min_seconds
                    elif media_type == "s":
                        mode = "s"
                        min_len = config.series_scan_min_seconds
                    else:
                        force_render = True
                        continue

                    print("Scanne Tracks...")
                    tracks = get_disc_tracks(selected_drive, min_len)
                    if not tracks:
                        print("Keine passenden Tracks gefunden.")
                        time.sleep(2)
                        force_render = True
                        continue

                    default_name = clean_label_for_suggestion(selected_drive.label)
                    jobs = []
                    if mode == "s":
                        jobs = prompt_series_jobs(tracks, default_name=default_name)
                    else:
                        jobs = prompt_movie_jobs(tracks, default_name=default_name)

                    if jobs:
                        _, start_error = start_rip_jobs(selected_drive, jobs)
                        if start_error:
                            print(f"Abbruch: {start_error}")
                            time.sleep(3)
                    force_render = True
                else:
                    print("Ungültiges Laufwerk oder beschäftigt.")
                    time.sleep(1)
                    force_render = True
            force_render = True


if __name__ == "__main__":
    main()
