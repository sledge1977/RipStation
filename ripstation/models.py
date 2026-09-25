from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SeriesAnalysis:
    """Result of the conservative, user-confirmed episode detection."""

    recommended_ids: List[int]
    duplicate_of: Dict[int, int]
    confidence: str
    median_duration_seconds: int


class Drive:
    """Represents a disc drive and its current rip status."""

    def __init__(self, mkv_id: int, device_path: str, label: str, name: str):
        self.mkv_id = mkv_id
        self.device_path = device_path
        self.label = label
        self.name = name
        self.status = "IDLE"
        self.current_job = ""
        self.progress = 0
        self.busy = False
        self.media_source: Optional[str] = None
        self.active_process: Any = None
        self.cancel_requested: bool = False

    def __repr__(self) -> str:
        return (
            f"Drive(id={self.mkv_id}, dev={self.device_path!r}, "
            f"name={self.name!r}, label={self.label!r}, status={self.status!r})"
        )
