import tempfile
from pathlib import Path

from ripstation.config import Config
from ripstation.models import Track
from ripstation.station import Station


def track(track_id, duration, size, **extra):
    fields = {
        "duration_seconds": duration,
        "duration_str": f"00:{duration // 60:02d}:00",
        "size_bytes": size,
        "output_filename": f"title_t{track_id:02d}.mkv",
    }
    fields.update(extra)
    return Track(track_id, **fields)


def make_station(base_output_dir=None, auto_eject=True):
    return Station(
        Config(
            base_output_dir=base_output_dir or tempfile.gettempdir(),
            makemkv_cmd="makemkvcon",
            mkvpropedit_cmd="mkvpropedit",
            auto_eject=auto_eject,
        )
    )


class SuccessfulProcess:
    """Fake MakeMKV run that creates one MKV in the staging directory."""

    returncode = 0

    def __init__(self, command, stdout=()):
        self.stdout = list(stdout)
        Path(command[-1], "generated.mkv").touch()

    def wait(self, timeout=None):
        return 0
