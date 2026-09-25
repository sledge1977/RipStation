import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def detect_makemkv_cmd() -> str:
    """Find MakeMKV executable across operating systems."""
    if os.name == "nt":
        possible_paths = [
            r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
            r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe",
            r"C:\Program Files\MakeMKV\makemkvcon64.exe",
            r"C:\Program Files\MakeMKV\makemkvcon.exe",
        ]
        found = next((p for p in possible_paths if os.path.exists(p)), None)
        if found:
            return found
        which = (
            shutil.which("makemkvcon64.exe")
            or shutil.which("makemkvcon.exe")
            or shutil.which("makemkvcon")
        )
        return which if which else "makemkvcon"
    which = shutil.which("makemkvcon")
    return which if which else "makemkvcon"


def detect_mkvpropedit_cmd() -> str:
    """Find mkvpropedit executable across operating systems."""
    if os.name == "nt":
        possible_paths = [
            r"C:\Program Files\MKVToolNix\mkvpropedit.exe",
            r"C:\Program Files (x86)\MKVToolNix\mkvpropedit.exe",
        ]
        found = next((p for p in possible_paths if os.path.exists(p)), None)
        if found:
            return found
        which = shutil.which("mkvpropedit.exe") or shutil.which("mkvpropedit")
        return which if which else "mkvpropedit"
    which = shutil.which("mkvpropedit")
    return which if which else "mkvpropedit"


def default_output_dir() -> str:
    if os.name == "nt":
        user_profile = os.environ.get("USERPROFILE", str(Path.home()))
        return os.path.join(user_profile, "Videos")
    return os.path.join(str(Path.home()), "Videos")


BASE_OUTPUT_DIR = default_output_dir()

MOVIE_MIN_LENGTH_SECONDS = 60 * 60
SERIES_SCAN_MIN_LENGTH_SECONDS = 60
SERIES_MIN_LENGTH_SECONDS = 8 * 60
SERIES_TYPICAL_MIN_SECONDS = 10 * 60
SERIES_TYPICAL_MAX_SECONDS = 95 * 60


@dataclass
class Config:
    base_output_dir: str
    makemkv_cmd: str
    mkvpropedit_cmd: str
    auto_eject: bool = True
    movie_min_seconds: int = MOVIE_MIN_LENGTH_SECONDS
    series_scan_min_seconds: int = SERIES_SCAN_MIN_LENGTH_SECONDS


def parse_arguments(args: Optional[List[str]] = None) -> Config:
    parser = argparse.ArgumentParser(
        description="RipStation - Multi-Laufwerk Ripping Station für Filme & Serien",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        default=default_output_dir(),
        help="Basisverzeichnis für Ausgabedateien",
    )
    parser.add_argument(
        "--no-eject",
        dest="auto_eject",
        action="store_false",
        default=True,
        help="Medium nach Fertigstellung nicht automatisch auswerfen",
    )
    parser.add_argument(
        "--makemkv",
        dest="makemkv_cmd",
        default=None,
        help="Pfad zur makemkvcon ausführbaren Datei",
    )
    parser.add_argument(
        "--mkvpropedit",
        dest="mkvpropedit_cmd",
        default=None,
        help="Pfad zur mkvpropedit ausführbaren Datei",
    )
    parser.add_argument(
        "--min-movie-len",
        dest="min_movie_len_min",
        type=int,
        default=60,
        help="Mindestlänge für Film-Tracks in Minuten",
    )

    parsed = parser.parse_args(args)

    makemkv_cmd = parsed.makemkv_cmd or detect_makemkv_cmd()
    mkvpropedit_cmd = parsed.mkvpropedit_cmd or detect_mkvpropedit_cmd()

    return Config(
        base_output_dir=os.path.abspath(parsed.output_dir),
        makemkv_cmd=makemkv_cmd,
        mkvpropedit_cmd=mkvpropedit_cmd,
        auto_eject=parsed.auto_eject,
        movie_min_seconds=max(60, parsed.min_movie_len_min * 60),
        series_scan_min_seconds=SERIES_SCAN_MIN_LENGTH_SECONDS,
    )
