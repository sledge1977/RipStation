import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


@dataclass(frozen=True)
class Track:
    """One title reported by MakeMKV's robot-mode TINFO output."""

    id: int
    title_name: str = ""
    duration_str: str = ""
    duration_seconds: int = 0
    size_str: str = ""
    size_bytes: int = 0
    source_filename: str = ""
    output_filename: str = ""


class RipJob(NamedTuple):
    """A single MakeMKV title and the final file it is published to."""

    track_id: int
    output_path: str
    source_filename: str = ""


@dataclass
class SeriesAnalysis:
    """Result of the conservative, user-confirmed episode detection."""

    recommended_ids: list[int]
    duplicate_of: dict[int, int]
    confidence: str
    median_duration_seconds: int


class DriveStatus(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RIPPING = "RIPPING"
    PROCESSING = "PROCESSING"
    EJECTING = "EJECTING"
    COMPLETED = "COMPLETED"
    COMPLETED_META_WARN = "COMPLETED_META_WARN"
    CANCELLED = "CANCELLED"
    ERROR_START = "ERROR_START"
    ERROR_RIP = "ERROR_RIP"
    ERROR_POST = "ERROR_POST"
    ERROR_WORKER = "ERROR_WORKER"

    @property
    def is_error(self) -> bool:
        return self.value.startswith("ERROR")


class Drive:
    """Represents a disc drive and its current rip status."""

    def __init__(self, mkv_id: int, device_path: str, label: str, name: str):
        self.mkv_id = mkv_id
        self.device_path = device_path
        self.label = label
        self.name = name
        self.status = DriveStatus.IDLE
        # "2/4" while a multi-track job is running; shown next to the status
        self.job_step = ""
        self.current_job = ""
        # Short note for the dashboard (errors, eject problems, kept files)
        self.message = ""
        self.progress = 0
        self.busy = False
        self.media_source: str | None = None
        self.active_process: subprocess.Popen[str] | None = None
        self.cancel_requested = False

    def reset_idle(self) -> None:
        self.status = DriveStatus.IDLE
        self.job_step = ""
        self.current_job = ""
        self.message = ""
        self.progress = 0
        self.media_source = None

    def __repr__(self) -> str:
        return (
            f"Drive(id={self.mkv_id}, dev={self.device_path!r}, "
            f"name={self.name!r}, label={self.label!r}, status={self.status.value!r})"
        )
