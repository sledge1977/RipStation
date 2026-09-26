import shutil
import signal
import time
from types import FrameType

from ripstation.analyzer import clean_label_for_suggestion
from ripstation.config import parse_arguments
from ripstation.hardware import get_disc_tracks
from ripstation.models import Drive
from ripstation.prompts import prompt_movie_jobs, prompt_series_jobs
from ripstation.station import Station
from ripstation.ui import clear_screen, print_ui, read_menu_command, status_text
from ripstation.worker import cancel_drive_rip, start_rip_jobs, stop_all_workers


def prompt_cancel_rip(station: Station) -> None:
    """Prompt user to choose a running drive and cancel its rip."""
    active = station.busy_drives()
    if not active:
        print("Kein aktiver Rip-Vorgang vorhanden.")
        time.sleep(1.5)
        return

    print("\nAktive Rips:")
    for d in active:
        print(
            f"  Laufwerk [{d.mkv_id}]: {d.device_path} - {status_text(d)} "
            f"({d.current_job})"
        )
    ans = input("\nLaufwerks-ID zum Abbrechen eingeben (oder 'q' für Zurück): ").strip()
    if ans.lower() == "q" or not ans.isdigit():
        return
    target_id = int(ans)
    target_drive = next((d for d in active if d.mkv_id == target_id), None)
    if target_drive:
        cancel_drive_rip(station, target_drive)
        print(f"Abbruch für Laufwerk {target_id} angefordert.")
    else:
        print(f"Laufwerk {target_id} ist nicht aktiv.")
    time.sleep(1.5)


def setup_drive(station: Station, drive: Drive) -> None:
    """Ask for movie/series, scan the disc and start the confirmed rip jobs."""
    config = station.config
    clear_screen()
    print(f"--- Einrichtung Laufwerk {drive.mkv_id} ({drive.name}) ---")
    media_type = input("Film (m/f) oder Serie (s)? [q=Zurück]: ").strip().lower()
    if media_type in ("m", "f"):
        min_len = config.movie_min_seconds
    elif media_type == "s":
        min_len = config.series_scan_min_seconds
    else:
        return

    print("Scanne Tracks...")
    tracks = get_disc_tracks(drive, min_len, config.makemkv_cmd)
    if not tracks:
        print("Keine passenden Tracks gefunden.")
        time.sleep(2)
        return

    default_name = clean_label_for_suggestion(drive.label)
    if media_type == "s":
        jobs = prompt_series_jobs(tracks, station, default_name=default_name)
    else:
        jobs = prompt_movie_jobs(
            tracks, config.base_output_dir, default_name=default_name
        )

    if jobs:
        _, start_error = start_rip_jobs(station, drive, jobs)
        if start_error:
            print(f"Abbruch: {start_error}")
            time.sleep(3)


def run_dashboard(station: Station) -> None:
    """Redraw the dashboard on changes and dispatch single-key commands."""
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
                d.job_step,
                d.progress,
                d.current_job,
                d.message,
                d.busy,
            )
            for d in station.drives
        )
        snapshot = (terminal.columns, terminal.lines, drive_snapshot)

        if force_render or snapshot != last_snapshot:
            print_ui(station.drives)
            print("Auswahl: ", end="", flush=True)
            last_snapshot = snapshot
            force_render = False

        user_input = read_menu_command(valid_ids=[d.mkv_id for d in station.drives])
        if not user_input:
            continue

        print()
        command = str(user_input).strip().lower()
        force_render = True
        if command == "q":
            if station.busy_drives():
                print(
                    "Ein Rip läuft noch; Beenden ist erst danach möglich "
                    "(oder 'c' zum Abbrechen)."
                )
                time.sleep(2)
                continue
            return
        if command == "r":
            station.rescan_idle_drives()
        elif command == "c":
            prompt_cancel_rip(station)
        elif command.isdigit():
            selected_drive = station.find_drive(int(command))
            if selected_drive and station.drive_is_available(selected_drive):
                setup_drive(station, selected_drive)
            else:
                print("Ungültiges Laufwerk oder beschäftigt.")
                time.sleep(1)


def _raise_keyboard_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(args: list[str] | None = None) -> None:
    station = Station(parse_arguments(args))

    # Ctrl+C already raises KeyboardInterrupt; treat SIGTERM the same way. The
    # cleanup runs in `finally` below instead of inside the signal handler, which
    # could otherwise deadlock on a lock the interrupted code is holding.
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except (ValueError, OSError):
        pass  # not in the main thread (e.g. tests)

    try:
        station.scan_drives()
        run_dashboard(station)
    except KeyboardInterrupt:
        print("\nAbbruch-Signal empfangen. Beende aktive Prozesse...")
    finally:
        stop_all_workers(station)
