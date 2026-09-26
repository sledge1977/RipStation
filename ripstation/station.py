import os
import threading
import time

from ripstation.analyzer import canonical_output_path, next_episode_number
from ripstation.config import Config
from ripstation.hardware import fetch_drive_info
from ripstation.models import Drive, DriveStatus, RipJob


class Station:
    """Runtime state shared by the dashboard and all rip workers.

    `lock` guards `reserved_outputs` and each drive's `busy`, `active_process`
    and `cancel_requested` fields.
    """

    def __init__(self, config: Config):
        self.config = config
        self.drives: list[Drive] = []
        self.lock = threading.Lock()
        self.reserved_outputs: set[str] = set()

    def find_drive(self, mkv_id: int) -> Drive | None:
        return next((d for d in self.drives if d.mkv_id == mkv_id), None)

    def drive_is_available(self, drive: Drive) -> bool:
        with self.lock:
            return not drive.busy

    def busy_drives(self) -> list[Drive]:
        with self.lock:
            return [d for d in self.drives if d.busy or d.active_process is not None]

    def scan_drives(self) -> None:
        print("Scanne Laufwerke... bitte warten...")
        try:
            drives_info = fetch_drive_info(self.config.makemkv_cmd)
        except RuntimeError as error:
            print(f"Fehler beim Suchlauf: {error}")
            drives_info = []
        self.drives = [
            Drive(d_id, d_dev, d_label, d_name)
            for d_id, d_name, d_label, d_dev in drives_info
        ]

    def rescan_idle_drives(self) -> None:
        """Refresh all drives that are not ripping and add newly found ones."""
        print("Erneuter Suchlauf...")
        try:
            drives_info = fetch_drive_info(self.config.makemkv_cmd)
        except RuntimeError as error:
            print(f"Rescan fehlgeschlagen: {error}")
            time.sleep(2)
            return

        info_by_device = {
            d_dev: (d_id, d_name, d_label)
            for d_id, d_name, d_label, d_dev in drives_info
        }
        with self.lock:
            for drive in self.drives:
                info = info_by_device.pop(drive.device_path, None)
                if drive.busy:
                    continue
                if info:
                    drive.mkv_id, drive.name, drive.label = info
                else:
                    drive.label = ""
                drive.reset_idle()

            for d_dev, (d_id, d_name, d_label) in info_by_device.items():
                self.drives.append(Drive(d_id, d_dev, d_label, d_name))
        time.sleep(1)

    def next_episode_number(self, series_name: str, season_number: int) -> int:
        return next_episode_number(
            series_name,
            season_number,
            base_output_dir=self.config.base_output_dir,
            reserved_outputs=self.reserved_outputs,
            job_state_lock=self.lock,
        )

    def reserve(self, drive: Drive, jobs: list[RipJob]) -> str | None:
        """Atomically mark the drive busy and reserve all outputs.

        Returns an error message instead if the drive or an output is taken.
        """
        outputs = [job.output_path for job in jobs]
        canonical = [canonical_output_path(output) for output in outputs]
        with self.lock:
            if drive.busy:
                return f"Laufwerk {drive.mkv_id} ist bereits beschäftigt."
            duplicate_paths = {path for path in canonical if canonical.count(path) > 1}
            conflicts = [
                output
                for output, normalized in zip(outputs, canonical, strict=True)
                if normalized in duplicate_paths
                or normalized in self.reserved_outputs
                or os.path.exists(output)
            ]
            if conflicts:
                return "Zieldatei bereits vorhanden oder reserviert:\n  " + "\n  ".join(
                    conflicts
                )

            self.reserved_outputs.update(canonical)
            drive.busy = True
            drive.status = DriveStatus.STARTING
            drive.job_step = ""
            drive.message = ""
            drive.progress = 0
            drive.cancel_requested = False
        return None

    def release(self, drive: Drive, jobs: list[RipJob]) -> None:
        """Free the drive and its outputs again; safe to call more than once."""
        paths = {canonical_output_path(job.output_path) for job in jobs}
        with self.lock:
            self.reserved_outputs.difference_update(paths)
            drive.busy = False
            drive.active_process = None
