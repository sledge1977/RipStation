#!/usr/bin/env python3
import subprocess
import os
import time
import threading
import sys
import csv
import select
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from statistics import median

# Windows-specific import
if os.name == 'nt':
    import msvcrt
else:
    import termios
    import tty

# --- CONFIGURATION ---

# Unterscheidung der Pfade und Befehle je nach Betriebssystem
if os.name == 'nt':
    # WINDOWS
    # Passe diesen Pfad an, falls MakeMKV woanders installiert ist
    BASE_OUTPUT_DIR = os.path.join(os.environ['USERPROFILE'], "Videos")
    possible_paths = [
        r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
        r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe",
        r"C:\Program Files\MakeMKV\makemkvcon64.exe"
    ]
    # Nimm den ersten Pfad, der existiert, sonst Standard
    MAKEMKV_CMD = next((p for p in possible_paths if os.path.exists(p)), "makemkvcon")
else:
    # LINUX
    BASE_OUTPUT_DIR = os.path.join(Path.home(), "Videos")
    MAKEMKV_CMD = "makemkvcon"

MOVIE_MIN_LENGTH_SECONDS = 60 * 60
SERIES_SCAN_MIN_LENGTH_SECONDS = 60
SERIES_MIN_LENGTH_SECONDS = 8 * 60
SERIES_TYPICAL_MIN_SECONDS = 10 * 60
SERIES_TYPICAL_MAX_SECONDS = 95 * 60

# Global list of drives
drives = []
reserved_outputs = set()
job_state_lock = threading.Lock()


@dataclass
class SeriesAnalysis:
    """Result of the conservative, user-confirmed episode detection."""

    recommended_ids: list
    duplicate_of: dict
    confidence: str
    median_duration_seconds: int


def drive_is_available(drive):
    with job_state_lock:
        return not drive.busy

# --- HELPER FUNCTIONS ---

def clear_screen():
    if os.name == 'nt':
        os.system('cls')
    elif sys.stdout.isatty():
        # One write causes noticeably less flicker than spawning `clear`.
        sys.stdout.write('\033[2J\033[H')


def command_needs_more_digits(digits, valid_ids):
    if valid_ids is None:
        return True
    prefix = str(digits)
    return any(
        str(drive_id).startswith(prefix) and len(str(drive_id)) > len(prefix)
        for drive_id in valid_ids
    )


def read_menu_command(timeout=1.0, digit_timeout=0.75, valid_ids=None):
    """Read dashboard commands immediately while accepting multi-digit IDs."""
    if os.name == 'nt':
        end_wait = time.time() + timeout
        while time.time() < end_wait:
            if msvcrt.kbhit():
                try:
                    char = msvcrt.getwche()
                except (OSError, UnicodeError):
                    return None
                if char in ('\r', '\n'):
                    return None
                if not char.isdigit():
                    return char
                digits = char
                if not command_needs_more_digits(digits, valid_ids):
                    return digits
                digit_deadline = time.time() + digit_timeout
                while len(digits) < 6 and time.time() < digit_deadline:
                    if not msvcrt.kbhit():
                        time.sleep(0.02)
                        continue
                    try:
                        next_char = msvcrt.getwche()
                    except (OSError, UnicodeError):
                        break
                    if next_char in ('\r', '\n'):
                        break
                    if not next_char.isdigit():
                        break
                    digits += next_char
                    if not command_needs_more_digits(digits, valid_ids):
                        break
                    digit_deadline = time.time() + digit_timeout
                return digits
            time.sleep(0.05)
        return None

    if not sys.stdin.isatty():
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if readable:
            line = sys.stdin.readline()
            return line.strip() if line else 'q'
        return None

    previous_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
        if not readable:
            return None
        char = sys.stdin.read(1)
        if not char:
            return 'q'
        if not char.isdigit():
            return char
        digits = char
        if not command_needs_more_digits(digits, valid_ids):
            return digits
        while len(digits) < 6:
            readable, _, _ = select.select([sys.stdin], [], [], digit_timeout)
            if not readable:
                break
            next_char = sys.stdin.read(1)
            if next_char in ('\r', '\n', ''):
                break
            if not next_char.isdigit():
                break
            digits += next_char
            if not command_needs_more_digits(digits, valid_ids):
                break
        return digits
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, previous_settings)

def eject_drive(device_path):
    """Versucht das Laufwerk je nach OS auszuwerfen."""
    try:
        if os.name == 'nt':
            # Einfacher PowerShell Befehl zum Auswerfen (funktioniert meistens für das erste Laufwerk)
            # Für spezifische Laufwerksbuchstaben ist es unter Windows komplexer ohne externe Tools.
            # Wir geben hier nur eine Meldung aus, um Abstürze zu vermeiden.
            print(f"Hinweis: Bitte Laufwerk {device_path} manuell auswerfen (Windows Eject nicht implementiert).")
        else:
            subprocess.run(
                ["eject", device_path], check=True, capture_output=True, timeout=30
            )
            print(f"Laufwerk {device_path} ausgeworfen.")
    except Exception as e:
        print(f"Fehler beim Auswerfen: {e}")

# --- CORE LOGIC ---

def fetch_drive_info():
    """Runs makemkvcon once and returns parsed drive tuples."""
    try:
        cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", "disc:9999"]
        # creationflags verhindern auf Windows, dass ein leeres CMD-Fenster aufpoppt (optional)
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = 0x08000000 # CREATE_NO_WINDOW
            
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120, creationflags=creation_flags)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
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

class Drive:
    def __init__(self, mkv_id, device_path, label, name):
        self.mkv_id = mkv_id          
        self.device_path = device_path 
        self.label = label            
        self.status = "IDLE"      
        self.current_job = ""         
        self.progress = 0             
        self.name = name
        self.busy = False
        self.media_source = None

def scan_drives():
    global drives
    print("Scanning drives... please wait...")
    try:
        drives_info = fetch_drive_info()
        drives = [Drive(d_id, d_dev, d_label, d_name) for d_id, d_name, d_label, d_dev in drives_info]
    except Exception as e:
        print(f"Error during scan: {e}")
        drives = []

def rescan_idle_drives():
    print("Rescanning drives...")
    try:
        drives_info = fetch_drive_info()
    except Exception as error:
        print(f"Rescan fehlgeschlagen: {error}")
        time.sleep(2)
        return

    info_by_device = {d_dev: (d_id, d_name, d_label) for d_id, d_name, d_label, d_dev in drives_info}

    for drive in drives:
        info = info_by_device.pop(drive.device_path, None)
        if not drive_is_available(drive):
            # Keep every property used by an active worker stable.
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

def parse_duration_to_seconds(duration_str):
    parts = duration_str.split(':')
    seconds = 0
    try:
        if len(parts) == 3: seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2: seconds = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1: seconds = int(parts[0])
    except (ValueError, IndexError): return 0
    return seconds


def parse_tinfo_output(output):
    """Parse MakeMKV robot-mode TINFO lines into track dictionaries."""
    tracks = {}
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
            track = tracks.setdefault(track_id, {'id': track_id})

            if attribute_id == 2:
                track['title_name'] = value
            elif attribute_id == 9:
                track['duration_str'] = value
                track['duration_seconds'] = parse_duration_to_seconds(value)
            elif attribute_id == 10:
                track['size_str'] = value
            elif attribute_id == 11:
                track['size_bytes'] = int(value)
            elif attribute_id == 16:
                track['source_filename'] = value
            elif attribute_id == 27:
                track['output_filename'] = value
        except (IndexError, ValueError, csv.Error):
            continue
    return sorted(tracks.values(), key=lambda track: track['id'])

def drive_source_candidates(drive):
    """Prefer the physical device and retain disc ID as a compatibility fallback."""
    candidates = []
    if drive.device_path:
        candidates.append(f"dev:{drive.device_path}")
    candidates.append(f"disc:{drive.mkv_id}")
    return list(dict.fromkeys(candidates))


def get_disc_tracks(drive, min_len_seconds):
    creation_flags = 0x08000000 if os.name == 'nt' else 0
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
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
            errors.append(f"{source}: {error}")
            continue

        drive.media_source = source
        tracks = parse_tinfo_output(result.stdout)
        return [
            track for track in tracks
            if track.get('duration_seconds', 0) >= min_len_seconds
        ]

    print("Trackscan fehlgeschlagen: " + "; ".join(errors))
    return []

def parse_track_selection(selection_str):
    selection = []
    seen = set()
    selection_str = ''.join(selection_str.split())
    if not selection_str: return []
    try:
        for part in selection_str.split(','):
            if not part: continue
            if '-' in part:
                start, end = map(int, part.split('-'))
                step = 1 if end >= start else -1
                values = range(start, end + step, step)
            else:
                values = [int(part)]
            for value in values:
                if value not in seen:
                    selection.append(value)
                    seen.add(value)
    except ValueError: return []
    return selection


def _duration_clusters(tracks):
    """Group tracks with episode-like, similar runtimes."""
    clusters = []
    for track in sorted(tracks, key=lambda t: t.get('duration_seconds', 0)):
        duration = track.get('duration_seconds', 0)
        best_cluster = None
        best_distance = None
        for cluster in clusters:
            center = median(t['duration_seconds'] for t in cluster)
            tolerance = max(120, center * 0.12)
            distance = abs(duration - center)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                best_cluster = cluster
                best_distance = distance
        if best_cluster is None:
            clusters.append([track])
        else:
            best_cluster.append(track)
    return clusters


def analyze_series_tracks(tracks):
    """Recommend probable episode tracks while keeping the decision visible."""
    eligible = [
        track for track in tracks
        if track.get('duration_seconds', 0) >= SERIES_MIN_LENGTH_SECONDS
    ]
    forced_low_confidence = False
    if not eligible:
        # Keep very short-form series usable, but label the guess as uncertain.
        eligible = [track for track in tracks if track.get('duration_seconds', 0) > 0]
        forced_low_confidence = True
    if not eligible:
        return SeriesAnalysis([], {}, "niedrig", 0)

    unique_tracks = []
    fingerprints = {}
    duplicate_of = {}
    for track in sorted(eligible, key=lambda t: t['id']):
        size = track.get('size_bytes', 0)
        fingerprint = (track.get('duration_seconds', 0), size) if size else None
        matching_tracks = fingerprints.get(fingerprint, []) if fingerprint else []
        if matching_tracks:
            current_identity = extract_episode_info(track)
            matching_episode = next(
                (
                    candidate for candidate in matching_tracks
                    if extract_episode_info(candidate) == current_identity
                ),
                None,
            )
            if current_identity[1] is None or matching_episode is not None:
                duplicate_of[track['id']] = (
                    matching_episode or matching_tracks[0]
                )['id']
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
                track for track in explicitly_numbered
                if extract_episode_info(track)[0] == season
            ]
            for season in explicit_seasons
        }
        selected_season = min(
            explicit_seasons,
            key=lambda season: (-len(tracks_by_season[season]), season),
        )
        best_season_tracks = order_episode_tracks(tracks_by_season[selected_season])
        center = int(median(track['duration_seconds'] for track in best_season_tracks))
        return SeriesAnalysis(
            [track['id'] for track in best_season_tracks],
            duplicate_of,
            "niedrig",
            center,
        )

    if (
        len(explicitly_numbered) >= 2
        and len(set(explicit_identities)) == len(explicit_identities)
    ):
        center = int(median(track['duration_seconds'] for track in explicitly_numbered))
        confidence = "hoch" if len(explicitly_numbered) == len(unique_tracks) else "mittel"
        ordered = order_episode_tracks(explicitly_numbered)
        return SeriesAnalysis(
            [track['id'] for track in ordered], duplicate_of, confidence, center
        )

    def cluster_score(cluster):
        center = median(t['duration_seconds'] for t in cluster)
        typical = SERIES_TYPICAL_MIN_SECONDS <= center <= SERIES_TYPICAL_MAX_SECONDS
        return (typical, len(cluster), center)

    best = max(clusters, key=cluster_score)
    best_center = int(median(t['duration_seconds'] for t in best))
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
        [track['id'] for track in ordered], duplicate_of, confidence, best_center
    )


EPISODE_PATTERNS = (
    re.compile(r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})[ ._-]*E(?P<episode>\d{1,3})(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])(?P<season>\d{1,3})x(?P<episode>\d{1,3})(?!\d)"),
    re.compile(r"(?i)(?<![A-Z0-9])(?:episode|ep|folge)[ ._-]*(?P<episode>\d{1,3})(?!\d)"),
)


def extract_episode_info(track):
    """Return (season, episode) if a track label contains an explicit marker."""
    fields = ('title_name', 'source_filename', 'output_filename')
    for field in fields:
        text = track.get(field, '')
        for pattern in EPISODE_PATTERNS:
            match = pattern.search(text)
            if match:
                season = match.groupdict().get('season')
                return (int(season) if season else None, int(match.group('episode')))
    return (None, None)


def order_episode_tracks(tracks):
    """Use explicit episode markers only when they are complete and unambiguous."""
    tracks = list(tracks)
    identities = [extract_episode_info(track) for track in tracks]
    if not identities or not all(episode is not None for _, episode in identities):
        return sorted(tracks, key=lambda track: track['id'])

    seasons = {season for season, _ in identities if season is not None}
    episode_numbers = [episode for _, episode in identities]
    if len(seasons) <= 1 and len(set(episode_numbers)) == len(episode_numbers):
        return [
            track
            for _, track in sorted(
                zip(identities, tracks),
                key=lambda item: item[0][1],
            )
        ]

    if len(set(identities)) == len(identities):
        return [
            track
            for _, track in sorted(
                zip(identities, tracks),
                key=lambda item: (
                    item[0][0] if item[0][0] is not None else -1,
                    item[0][1],
                ),
            )
        ]
    return sorted(tracks, key=lambda track: track['id'])


def parse_progress_line(line):
    """Return MakeMKV's overall PRGV progress as a percentage."""
    if not line.startswith("PRGV:"):
        return None
    try:
        _current, total, maximum = map(
            int,
            line.split(":", 1)[1].strip().split(",")[:3],
        )
        if maximum <= 0:
            return None
        return max(0, min(100, int((total / maximum) * 100)))
    except (ValueError, IndexError):
        return None


def format_number_ranges(numbers):
    """Format [1, 2, 3, 6] as '1-3,6'."""
    numbers = list(numbers)
    if not numbers:
        return ""
    parts = []
    start = previous = numbers[0]
    step = None
    for number in numbers[1:]:
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


def safe_media_name(value):
    """Create one portable path component without changing ordinary spaces."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value.strip())
    value = value.rstrip('. ')
    windows_reserved = re.compile(
        r'(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)'
    )
    if windows_reserved.match(value):
        value = f"_{value}"
    return value


def canonical_output_path(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def release_output_paths(jobs):
    paths = {canonical_output_path(output) for _, output, _ in jobs}
    with job_state_lock:
        reserved_outputs.difference_update(paths)

def build_series_jobs(series_name, season_number, tracks, episode_numbers):
    if len(tracks) != len(episode_numbers):
        raise ValueError("Track- und Folgennummern müssen gleich lang sein")
    safe_name = safe_media_name(series_name)
    if not safe_name:
        raise ValueError("Der Serienname darf nicht leer sein")
    jobs = []
    for track, episode_number in zip(tracks, episode_numbers):
        filename = f"{safe_name}.S{season_number:02d}E{episode_number:02d}.mkv"
        output = os.path.join(
            BASE_OUTPUT_DIR, safe_name, f"Season {season_number:02d}", filename
        )
        jobs.append((track['id'], output, track.get('output_filename', '')))
    return jobs


def next_episode_number(series_name, season_number):
    """Continue after existing and currently reserved episodes."""
    safe_name = safe_media_name(series_name)
    season_dir = Path(BASE_OUTPUT_DIR) / safe_name / f"Season {season_number:02d}"
    pattern = re.compile(rf"(?i)\.S{season_number:02d}E(\d+)")
    existing_numbers = []
    canonical_season_dir = canonical_output_path(season_dir)
    # The worker releases reservations under the same lock and only after moving
    # its files. We therefore cannot miss an episode between both observations.
    with job_state_lock:
        if season_dir.is_dir():
            for path in season_dir.glob("*.mkv"):
                match = pattern.search(path.name)
                if match:
                    existing_numbers.append(int(match.group(1)))
        for output in reserved_outputs:
            if canonical_output_path(Path(output).parent) != canonical_season_dir:
                continue
            match = pattern.search(Path(output).name)
            if match:
                existing_numbers.append(int(match.group(1)))
    return max(existing_numbers, default=0) + 1


def prompt_integer(prompt, default=None, minimum=0):
    while True:
        suffix = f" [{default}]" if default is not None else ""
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        try:
            number = int(value)
            if number >= minimum:
                return number
        except ValueError:
            pass
        print(f"Bitte eine ganze Zahl ab {minimum} eingeben.")


def render_series_tracks(tracks, analysis, width=None):
    """Render the episode candidate list without exceeding terminal width."""
    terminal_width = shutil.get_terminal_size(fallback=(80, 24)).columns
    width = max(20, width if width is not None else terminal_width)
    recommended = analysis.recommended_ids

    def marker_for(track):
        if track['id'] in analysis.duplicate_of:
            return f"~{analysis.duplicate_of[track['id']]}"
        return "*" if track['id'] in recommended else ""

    def source_for(track):
        parts = [
            track.get(field) for field in ('source_filename', 'title_name')
            if track.get(field)
        ]
        return " | ".join(parts) or track.get('output_filename', '')

    lines = []
    if width >= 76:
        widths = [6, 4, 10, 10, width - 42]
        lines.append(table_row(["Mark.", "ID", "Dauer", "Größe", "Quelle / Titel"], widths))
        lines.append('─' * width)
        for track in tracks:
            lines.append(table_row([
                marker_for(track),
                track['id'],
                track.get('duration_str', '?'),
                track.get('size_str', '?'),
                source_for(track),
            ], widths))
    elif width >= 48:
        widths = [4, 3, 9, width - 25]
        lines.append(table_row(["M.", "ID", "Dauer", "Quelle / Titel"], widths))
        lines.append('─' * width)
        for track in tracks:
            lines.append(table_row([
                marker_for(track),
                track['id'],
                track.get('duration_str', '?'),
                source_for(track),
            ], widths))
    else:
        for track in tracks:
            heading = (
                f"{marker_for(track):>3} [{track['id']}] "
                f"{track.get('duration_str', '?')} · {track.get('size_str', '?')}"
            )
            lines.extend((fit_text(heading, width), fit_text(f"    {source_for(track)}", width)))
    return '\n'.join(lines)


def prompt_series_jobs(tracks):
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
        if selection.lower() == 'q':
            return []
        if not selection:
            selected_ids = recommended
        elif selection == '*':
            selected_ids = [track['id'] for track in tracks]
        else:
            selected_ids = parse_track_selection(selection)
        available = {track['id'] for track in tracks}
        if selected_ids and all(track_id in available for track_id in selected_ids):
            break
        print("Ungültige oder leere Auswahl.")

    by_id = {track['id']: track for track in tracks}
    selected_tracks = [by_id[track_id] for track_id in selected_ids]
    if not selection and recommended:
        selected_tracks = order_episode_tracks(selected_tracks)

    order_default = format_number_ranges(track['id'] for track in selected_tracks)
    order_value = input(
        f"Reihenfolge [Enter={order_default}, 'r'=umkehren, sonst Track-IDs]: "
    ).strip()
    if order_value.lower() == 'r':
        selected_tracks.reverse()
    elif order_value:
        ordered_ids = parse_track_selection(order_value)
        if set(ordered_ids) != set(selected_ids) or len(ordered_ids) != len(selected_ids):
            print("Reihenfolge enthält nicht genau die ausgewählten Tracks. Abbruch.")
            return []
        selected_tracks = [by_id[track_id] for track_id in ordered_ids]

    inferred_seasons = {
        season for season, episode in map(extract_episode_info, selected_tracks)
        if season is not None and episode is not None
    }
    if len(inferred_seasons) > 1:
        season_list = ", ".join(str(season) for season in sorted(inferred_seasons))
        print(f"Abbruch: Die Auswahl enthält Titel aus mehreren Staffeln ({season_list}).")
        print("Bitte nur Titel einer Staffel auswählen.")
        return []

    raw_name = input("Serienname: ").strip()
    if not safe_media_name(raw_name):
        print("Serienname ist leer oder ungültig. Abbruch.")
        return []

    season_default = next(iter(inferred_seasons)) if inferred_seasons else 1
    season_number = prompt_integer("Staffel", season_default, minimum=0)

    inferred_episodes = [extract_episode_info(track)[1] for track in selected_tracks]
    if not all(number is not None for number in inferred_episodes):
        episode_start = next_episode_number(raw_name, season_number)
        inferred_episodes = list(range(episode_start, episode_start + len(selected_tracks)))
    episode_default = format_number_ranges(inferred_episodes)
    while True:
        value = input(
            f"Folgennummern [Enter={episode_default}; z.B. 5-8 oder 5,6,9,10]: "
        ).strip()
        episode_numbers = inferred_episodes if not value else parse_track_selection(value)
        if len(episode_numbers) == len(selected_tracks) and all(n >= 1 for n in episode_numbers):
            if len(set(episode_numbers)) == len(episode_numbers):
                break
        print(f"Bitte genau {len(selected_tracks)} unterschiedliche Folgennummern eingeben.")

    return build_series_jobs(raw_name, season_number, selected_tracks, episode_numbers)


def prompt_movie_jobs(tracks):
    """Let the user confirm the longest movie candidate or choose another one."""
    longest = max(tracks, key=lambda track: track.get('duration_seconds', 0))
    analysis = SeriesAnalysis(
        recommended_ids=[longest['id']],
        duplicate_of={},
        confidence="",
        median_duration_seconds=longest.get('duration_seconds', 0),
    )
    print("\nGefundene Filmtitel (* längster Titel / Empfehlung):")
    print(render_series_tracks(tracks, analysis))
    while True:
        value = input(f"Track-ID [Enter={longest['id']}, q=Abbrechen]: ").strip()
        if value.lower() == 'q':
            return []
        selected_id = longest['id'] if not value else None
        if value:
            parsed = parse_track_selection(value)
            if len(parsed) == 1:
                selected_id = parsed[0]
        selected = next((track for track in tracks if track['id'] == selected_id), None)
        if selected:
            break
        print("Bitte genau eine vorhandene Track-ID eingeben.")

    movie_name = safe_media_name(input("Filmname: "))
    if not movie_name:
        print("Filmname ist leer oder ungültig. Abbruch.")
        return []
    output = os.path.join(BASE_OUTPUT_DIR, movie_name, f"{movie_name}.mkv")
    return [(selected['id'], output, selected.get('output_filename', ''))]

def find_created_mkv(target_dir, source_mkv_filename, files_before_rip):
    """Find only a regular MKV file created by the current MakeMKV invocation."""
    before = {canonical_output_path(path) for path in files_before_rip}
    expected = (
        Path(target_dir) / Path(source_mkv_filename).name
        if source_mkv_filename else None
    )
    if expected and expected.is_file() and canonical_output_path(expected) not in before:
        return str(expected)

    new_files = [
        path for path in Path(target_dir).glob("*.mkv")
        if path.is_file() and canonical_output_path(path) not in before
    ]
    if len(new_files) == 1:
        return str(new_files[0])
    expected_text = str(expected) if expected else "kein Dateiname von MakeMKV gemeldet"
    raise FileNotFoundError(
        f"MakeMKV-Ausgabedatei nicht eindeutig gefunden ({expected_text}, "
        f"{len(new_files)} neue MKV-Dateien)"
    )


def remove_empty_directory(path):
    """Remove a completed staging directory, retaining failed rip artifacts."""
    try:
        Path(path).rmdir()
    except OSError:
        pass


def ensure_process_stopped(proc, timeout=10):
    """Terminate and, if necessary, kill a child that did not exit normally."""
    if proc is None or getattr(proc, 'returncode', None) is not None:
        return
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except (OSError, ProcessLookupError):
        # The process disappeared between poll and signal.
        return


def _rip_jobs_worker(drive, jobs, disc_source):
    drive.status = "Starting..."
    drive.progress = 0
    total_jobs = len(jobs)
    metadata_warning = False
    first_job_stem = os.path.splitext(os.path.basename(jobs[0][1] if jobs else "Unknown Job"))[0]
    original_job_name = re.sub(r"(?i)\.S\d+E\d+$", "", first_job_stem)

    for i, (track_id, final_output_path, source_mkv_filename) in enumerate(jobs):
        job_progress_text = f"({i+1}/{total_jobs})"
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
            MAKEMKV_CMD, "-r", "--progress=-same", "mkv",
            disc_source, str(track_id), staging_dir,
        ]
        
        log_file = os.path.join(os.path.dirname(jobs[0][1]), "rip.log")

        creation_flags = 0x08000000 if os.name == 'nt' else 0

        with open(log_file, "a") as f:
            f.write(f"\n--- Starting Job {job_progress_text}: Rip Track {track_id} ---\n")
            proc = None
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
                except OSError as error:
                    f.write(f"ERROR: MakeMKV konnte nicht gestartet werden: {error}\n")
                    drive.status = "ERROR (Start)"
                    remove_empty_directory(staging_dir)
                    break

                for line in proc.stdout:
                    f.write(line)
                    progress = parse_progress_line(line)
                    if progress is not None:
                        drive.progress = progress

                proc.wait()
            finally:
                ensure_process_stopped(proc)
                remove_empty_directory(staging_dir)

            drive.status = f"Processing {job_progress_text}"

            if proc.returncode == 0:
                try:
                    created_file = find_created_mkv(
                        staging_dir, source_mkv_filename, files_before_rip
                    )

                    paths_are_equal = (
                        os.path.normcase(os.path.abspath(created_file))
                        == os.path.normcase(os.path.abspath(final_output_path))
                    )
                    if os.path.exists(final_output_path) and not paths_are_equal:
                        raise FileExistsError(
                            f"Zieldatei existiert bereits, wird nicht überschrieben: {final_output_path}"
                        )

                    if not paths_are_equal:
                        os.rename(created_file, final_output_path)
                    
                    # Metadata title (requires mkvpropedit in PATH)
                    try:
                        mkv_cmd = "mkvpropedit" # Assumed in PATH
                        meta_result = subprocess.run(
                            [mkv_cmd, final_output_path, "--edit", "info", "--set",
                             f"title={os.path.splitext(os.path.basename(final_output_path))[0]}"],
                            capture_output=True,
                            text=True,
                            timeout=120,
                            creationflags=creation_flags,
                        )
                        if meta_result.returncode != 0:
                            metadata_warning = True
                            f.write(f"WARNING: mkvpropedit: {meta_result.stderr.strip()}\n")
                    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                        metadata_warning = True
                        f.write(f"WARNING: mkvpropedit nicht verfügbar oder Timeout: {error}\n")
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

    if not drive.status.startswith("ERROR"):
        final_status = "COMPLETED (META WARN)" if metadata_warning else "COMPLETED"
        drive.status = "Ejecting..."
        drive.current_job = original_job_name 
        drive.progress = 0
        eject_drive(drive.device_path)
        drive.status = final_status
    
    drive.progress = 0


def rip_jobs_worker(drive, jobs, disc_source=None, owns_job_state=False):
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


def start_rip_jobs(drive, jobs):
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
            output for output, normalized in zip(outputs, canonical)
            if normalized in duplicate_paths
            or normalized in reserved_outputs
            or os.path.exists(output)
        ]
        if conflicts:
            return None, "Zieldatei bereits vorhanden oder reserviert:\n  " + "\n  ".join(conflicts)

        reserved_outputs.update(canonical)
        drive.busy = True
        drive.status = "Starting..."
        drive.progress = 0
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
        return None, f"Worker konnte nicht gestartet werden: {error}"
    return thread, None


def display_width(value):
    """Terminal cell width, including wide and combining Unicode characters."""
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def fit_text(value, width, align='left'):
    """Truncate and pad text to exactly `width` terminal cells."""
    if width <= 0:
        return ''
    value = str(value)
    if display_width(value) > width:
        result = ''
        available = max(0, width - 1)
        for char in value:
            char_width = display_width(char)
            if display_width(result) + char_width > available:
                break
            result += char
        value = result + ('…' if width > 0 else '')

    padding = width - display_width(value)
    if align == 'right':
        return (' ' * padding) + value
    if align == 'center':
        left = padding // 2
        return (' ' * left) + value + (' ' * (padding - left))
    return value + (' ' * padding)


def status_text(drive):
    status = drive.status or "IDLE"
    translations = (
        ("COMPLETED (META WARN)", "Fertig · Metadatenwarnung"),
        ("COMPLETED", "Fertig"),
        ("Starting", "Startet"),
        ("Ripping", "Rippe"),
        ("Processing", "Verarbeite"),
        ("Ejecting", "Werfe aus"),
        ("ERROR (Start)", "Fehler · Programmstart"),
        ("ERROR (Rip)", "Fehler · Rip"),
        ("ERROR (Post)", "Fehler · Nachbearbeitung"),
        ("ERROR (Worker)", "Fehler · Verarbeitung"),
        ("ERROR", "Fehler"),
        ("IDLE", "Bereit"),
    )
    for prefix, translated in translations:
        if status.startswith(prefix):
            return translated + status[len(prefix):]
    return status


def drive_info_text(drive):
    if drive.current_job and drive.status not in ("IDLE", ""):
        return drive.current_job
    return drive.label or "Kein Medium"


def progress_text(drive, width):
    if not drive.status.startswith("Ripping") or width < 8:
        return ""
    progress = max(0, min(100, int(drive.progress)))
    bar_width = max(1, width - 7)
    filled = round(bar_width * progress / 100)
    bar = ('█' * filled) + ('░' * (bar_width - filled))
    return f"[{bar}] {progress:3d}%"


def table_row(values, widths, alignments=None):
    alignments = alignments or ['left'] * len(values)
    return " │ ".join(
        fit_text(value, width, alignment)
        for value, width, alignment in zip(values, widths, alignments)
    )


def render_table(drive_list, width, height, wide):
    if wide:
        widths = [3, 16, 18, 20, 13, width - 85]
        headers = ["ID", "Gerät", "Laufwerk", "Status", "Fortschritt", "Medium / Auftrag"]
    else:
        widths = [3, 14, 20, width - 46]
        headers = ["ID", "Gerät", "Status", "Medium / Auftrag"]

    max_rows = max(1, height - 7)
    clipped = len(drive_list) > max_rows
    visible_count = max_rows - 1 if clipped else max_rows
    visible_drives = drive_list[:max(0, visible_count)]

    lines = [table_row(headers, widths), '─' * width]
    for drive in visible_drives:
        if wide:
            values = [
                drive.mkv_id,
                drive.device_path,
                drive.name,
                status_text(drive),
                progress_text(drive, widths[4]),
                drive_info_text(drive),
            ]
        else:
            state = status_text(drive)
            if drive.status.startswith("Ripping"):
                state = f"{state} · {drive.progress}%"
            values = [drive.mkv_id, drive.device_path, state, drive_info_text(drive)]
        lines.append(table_row(values, widths, ['right'] + ['left'] * (len(widths) - 1)))

    if clipped:
        hidden = len(drive_list) - len(visible_drives)
        lines.append(fit_text(f"… {hidden} weitere Laufwerke", width))
    elif not drive_list:
        lines.append(fit_text("Keine Laufwerke erkannt · R zum erneuten Scannen", width))
    return lines


def render_cards(drive_list, width, height):
    budget = max(1, height - 5)
    if not drive_list:
        return [fit_text("Keine Laufwerke erkannt", width)]

    max_cards = budget // 2
    clipped = len(drive_list) > max_cards
    if clipped:
        max_cards = max(0, (budget - 1) // 2)

    lines = []
    for drive in drive_list[:max_cards]:
        heading = f"[{drive.mkv_id}] {drive.device_path} · {status_text(drive)}"
        detail = f"    {drive.name} · {drive_info_text(drive)}"
        if drive.status.startswith("Ripping"):
            detail = f"    {drive.progress}% · {drive_info_text(drive)}"
        lines.extend((fit_text(heading, width), fit_text(detail, width)))

    if clipped:
        lines.append(fit_text(f"… {len(drive_list) - max_cards} weitere Laufwerke", width))
    return lines


def render_ui(drive_list=None, width=None, height=None):
    """Return a dashboard adapted to terminal width and height."""
    drive_list = drives if drive_list is None else list(drive_list)
    terminal = shutil.get_terminal_size(fallback=(80, 24))
    width = max(20, width if width is not None else terminal.columns)
    height = max(8, height if height is not None else terminal.lines)

    title = f"RIPSTATION · {len(drive_list)} Laufwerk{'e' if len(drive_list) != 1 else ''}"
    lines = [fit_text(title, width, 'center'), '─' * width]
    if width >= 110:
        lines.extend(render_table(drive_list, width, height, wide=True))
    elif width >= 68:
        lines.extend(render_table(drive_list, width, height, wide=False))
    else:
        lines.extend(render_cards(drive_list, width, height))
    lines.append('─' * width)
    footer = "[ID] Rip starten  ·  [R] Neu scannen  ·  [Q] Beenden"
    if width < 54:
        footer = "[ID] Rip  ·  [R] Scan  ·  [Q] Ende"
    lines.append(fit_text(footer, width, 'center'))
    return '\n'.join(lines)


def print_ui():
    clear_screen()
    print(render_ui())

def main():
    scan_drives()

    last_snapshot = None
    force_render = True

    while True:
        terminal = shutil.get_terminal_size(fallback=(80, 24))
        drive_snapshot = tuple(
            (d.mkv_id, d.device_path, d.name, d.label, d.status,
             d.progress, d.current_job, d.busy)
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
            
            if user_input.lower() == 'q':
                if any(not drive_is_available(d) for d in drives):
                    print("Ein Rip läuft noch; Beenden ist erst danach möglich.")
                    time.sleep(2)
                    force_render = True
                    continue
                return
            elif user_input.lower() == 'r':
                rescan_idle_drives()
                force_render = True
            elif user_input.isdigit():
                selected_drive = next((d for d in drives if d.mkv_id == int(user_input)), None)
                if selected_drive and drive_is_available(selected_drive):
                    
                    clear_screen()
                    print(f"--- Setup Drive {selected_drive.mkv_id} ---")
                    media_type = input("Movie (m) or Series (s)? ").lower()
                    
                    if media_type not in ['m', 's']:
                        force_render = True; continue

                    min_len = MOVIE_MIN_LENGTH_SECONDS if media_type == 'm' else SERIES_SCAN_MIN_LENGTH_SECONDS

                    print("Scanning tracks...")
                    tracks = get_disc_tracks(selected_drive, min_len)
                    if not tracks:
                        print("No tracks found."); time.sleep(2); force_render=True; continue
                    
                    jobs = []
                    if media_type == 's':
                        jobs = prompt_series_jobs(tracks)
                    else:
                        jobs = prompt_movie_jobs(tracks)

                    if jobs:
                        _, start_error = start_rip_jobs(selected_drive, jobs)
                        if start_error:
                            print(f"Abbruch: {start_error}")
                            time.sleep(3)
                    force_render = True
                else:
                    print("Invalid drive or busy."); time.sleep(1); force_render=True
            force_render = True

if __name__ == "__main__":
    main()
