import csv
import os
import subprocess

from ripstation.analyzer import parse_tinfo_output
from ripstation.models import Drive, Track
from ripstation.process import CREATE_NO_WINDOW, OUTPUT_ENCODING


def eject_drive(device_path: str) -> str | None:
    """Eject the optical drive; returns a user-facing note if that failed."""
    try:
        if os.name == "nt":
            # Use PowerShell COM object on Windows
            # Handles drive letters such as 'D:', 'D:\', 'D'
            clean_dev = device_path.rstrip("\\")
            if not clean_dev.endswith(":"):
                clean_dev += ":"
            ps_script = (
                f"$wm = New-Object -com 'WMPlayer.OCX'; "
                f"$cd = $wm.cdromCollection.getByDriveSpecifier('{clean_dev}'); "
                f"if ($cd) {{ $cd.Eject() }} else {{ exit 1 }}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                return "Bitte manuell auswerfen"
        else:
            subprocess.run(
                ["eject", device_path], check=True, capture_output=True, timeout=30
            )
    except (OSError, subprocess.SubprocessError) as error:
        return f"Auswerfen fehlgeschlagen: {error}"
    return None


def drive_source_candidates(drive: Drive) -> list[str]:
    """Prefer the physical device and retain disc ID as a compatibility fallback."""
    candidates = []
    if drive.device_path:
        candidates.append(f"dev:{drive.device_path}")
    candidates.append(f"disc:{drive.mkv_id}")
    return list(dict.fromkeys(candidates))


def parse_drive_lines(output: str) -> list[tuple[int, str, str, str]]:
    """Parse DRV lines into (id, name, label, device); malformed lines are skipped."""
    drives_info: list[tuple[int, str, str, str]] = []
    for line in output.splitlines():
        if not line.startswith("DRV:"):
            continue
        try:
            row = next(csv.reader([line[4:]]))
            if len(row) < 7 or not row[6]:
                continue
            drives_info.append((int(row[0]), row[4][:14], row[5], row[6]))
        except (ValueError, csv.Error, StopIteration):
            continue
    return drives_info


def fetch_drive_info(makemkv_cmd: str) -> list[tuple[int, str, str, str]]:
    """Runs makemkvcon once and returns parsed drive tuples (id, name, label, dev)."""
    try:
        result = subprocess.run(
            [makemkv_cmd, "-r", "--cache=1", "info", "disc:9999"],
            capture_output=True,
            check=True,
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
            encoding=OUTPUT_ENCODING,
            errors="replace",
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ) as e:
        raise RuntimeError(
            f"makemkvcon konnte nicht ausgeführt werden ({makemkv_cmd}): {e}"
        ) from e
    return parse_drive_lines(result.stdout)


def get_disc_tracks(
    drive: Drive, min_len_seconds: int, makemkv_cmd: str
) -> list[Track]:
    errors = []
    for source in drive_source_candidates(drive):
        try:
            result = subprocess.run(
                [makemkv_cmd, "-r", "--cache=1", "info", source],
                capture_output=True,
                check=True,
                timeout=600,
                creationflags=CREATE_NO_WINDOW,
                encoding=OUTPUT_ENCODING,
                errors="replace",
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as error:
            errors.append(f"{source}: {error}")
            continue

        drive.media_source = source
        return [
            track
            for track in parse_tinfo_output(result.stdout)
            if track.duration_seconds >= min_len_seconds
        ]

    print("Trackscan fehlgeschlagen: " + "; ".join(errors))
    return []
