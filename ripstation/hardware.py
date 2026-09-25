import csv
import os
import subprocess
from typing import List, Optional, Tuple

from ripstation.analyzer import parse_tinfo_output
from ripstation.config import detect_makemkv_cmd
from ripstation.models import Drive


def eject_drive(device_path: str) -> None:
    """Eject the optical drive according to the operating system."""
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
        print(f"Fehler beim Auswerfen ({device_path}): {e}")


def drive_source_candidates(drive: Drive) -> List[str]:
    """Prefer the physical device and retain disc ID as a compatibility fallback."""
    candidates = []
    if drive.device_path:
        candidates.append(f"dev:{drive.device_path}")
    candidates.append(f"disc:{drive.mkv_id}")
    return list(dict.fromkeys(candidates))


def fetch_drive_info(
    makemkv_cmd: Optional[str] = None,
) -> List[Tuple[int, str, str, str]]:
    """Runs makemkvcon once and returns parsed drive tuples (id, name, label, dev)."""
    cmd_name = makemkv_cmd or detect_makemkv_cmd()
    try:
        cmd = [cmd_name, "-r", "--cache=1", "info", "disc:9999"]
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
            f"makemkvcon konnte nicht ausgeführt werden ({cmd_name}): {e}"
        ) from e

    drives_info: List[Tuple[int, str, str, str]] = []
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


def get_disc_tracks(
    drive: Drive, min_len_seconds: int, makemkv_cmd: Optional[str] = None
) -> list:
    cmd_name = makemkv_cmd or detect_makemkv_cmd()
    creation_flags = 0x08000000 if os.name == "nt" else 0
    errors = []
    for source in drive_source_candidates(drive):
        cmd = [cmd_name, "-r", "--cache=1", "info", source]
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
