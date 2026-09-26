import os
import re
import shutil
import subprocess
import tempfile
import threading
import traceback
from enum import Enum
from pathlib import Path
from typing import IO

from ripstation.analyzer import canonical_output_path
from ripstation.hardware import drive_source_candidates, eject_drive
from ripstation.models import Drive, DriveStatus, RipJob
from ripstation.process import CREATE_NO_WINDOW, OUTPUT_ENCODING, ensure_process_stopped
from ripstation.station import Station

LOG_FILENAME = "rip.log"


class JobResult(Enum):
    DONE = "done"
    DONE_META_WARN = "done_meta_warn"
    STOPPED = "stopped"


class ProcessStartError(Exception):
    """MakeMKV could not be launched at all."""


class RipLog:
    """rip.log writer that tags every line with its drive.

    Several drives may rip into the same directory; the file is line-buffered
    and opened for appending, so their lines interleave but never mix.
    """

    def __init__(self, handle: IO[str], drive: Drive):
        self.handle = handle
        self.prefix = f"[Laufwerk {drive.mkv_id}] "

    def write(self, text: str) -> None:
        for line in text.splitlines() or [""]:
            self.handle.write(f"{self.prefix}{line}\n")


def open_log(directory: str) -> IO[str]:
    return open(
        os.path.join(directory, LOG_FILENAME),
        "a",
        encoding="utf-8",
        errors="replace",
        buffering=1,
    )


def parse_progress_line(line: str) -> int | None:
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


def find_created_mkv(
    target_dir: str, source_mkv_filename: str, files_before_rip: set[Path]
) -> str:
    """Find only a regular MKV file created by the current MakeMKV invocation."""
    before = {canonical_output_path(path) for path in files_before_rip}
    expected = (
        Path(target_dir) / Path(source_mkv_filename).name
        if source_mkv_filename
        else None
    )
    if (
        expected
        and expected.is_file()
        and canonical_output_path(expected) not in before
    ):
        return str(expected)

    new_files = [
        path
        for path in Path(target_dir).glob("*.mkv")
        if path.is_file() and canonical_output_path(path) not in before
    ]
    if len(new_files) == 1:
        return str(new_files[0])
    expected_text = str(expected) if expected else "kein Dateiname von MakeMKV gemeldet"
    raise FileNotFoundError(
        f"MakeMKV-Ausgabedatei nicht eindeutig gefunden ({expected_text}, "
        f"{len(new_files)} neue MKV-Dateien)"
    )


def remove_empty_directory(path: str) -> None:
    """Remove a completed staging directory, retaining failed rip artifacts."""
    try:
        Path(path).rmdir()
    except OSError:
        pass


def move_without_overwrite(source: str, destination: str) -> None:
    """Publish a staged file without replacing a destination created meanwhile."""
    exists_error = FileExistsError(
        f"Zieldatei existiert bereits, wird nicht überschrieben: {destination}"
    )
    if os.name == "nt":
        # Windows rename fails when the destination already exists.
        try:
            os.rename(source, destination)
        except FileExistsError as error:
            raise exists_error from error
        return

    # Staging is inside the destination directory, so both paths share a FS.
    try:
        os.link(source, destination)
    except FileExistsError as error:
        raise exists_error from error
    except OSError:
        # exFAT/FAT, many SMB and some NFS mounts have no hard links. Outputs
        # are reserved by the station, so only an external writer could race us.
        if os.path.lexists(destination):
            raise exists_error from None
        os.rename(source, destination)
        return
    os.unlink(source)


def cancel_drive_rip(station: Station, drive: Drive) -> bool:
    """Request cancellation of an ongoing rip on the given drive."""
    with station.lock:
        if not drive.busy:
            return False
        drive.cancel_requested = True
        proc = drive.active_process
    if proc is not None:
        ensure_process_stopped(proc, timeout=5)
    return True


def stop_all_workers(station: Station) -> None:
    """Stop all active worker processes (e.g. on SIGINT/shutdown)."""
    for drive in station.busy_drives():
        cancel_drive_rip(station, drive)


def run_makemkv(
    station: Station, drive: Drive, cmd: list[str], log: RipLog
) -> int | None:
    """Stream MakeMKV output into the log; returns None if the user cancelled."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding=OUTPUT_ENCODING,
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as error:
        raise ProcessStartError(str(error)) from error

    try:
        with station.lock:
            drive.active_process = proc
            cancelled = drive.cancel_requested
        if not cancelled:
            for line in proc.stdout or []:
                if drive.cancel_requested:
                    break
                log.write(line)
                progress = parse_progress_line(line)
                if progress is not None:
                    drive.progress = progress
        if drive.cancel_requested:
            return None
        return proc.wait()
    finally:
        with station.lock:
            drive.active_process = None
        ensure_process_stopped(proc)


def publish_output(staging_dir: str, job: RipJob, files_before: set[Path]) -> None:
    created_file = find_created_mkv(staging_dir, job.source_filename, files_before)
    move_without_overwrite(created_file, job.output_path)


def set_title(mkvpropedit_cmd: str, path: str, log: RipLog) -> bool:
    """Set the MKV title to the file name; returns False on a (non-fatal) failure."""
    title = os.path.splitext(os.path.basename(path))[0]
    try:
        result = subprocess.run(
            [mkvpropedit_cmd, path, "--edit", "info", "--set", f"title={title}"],
            capture_output=True,
            encoding=OUTPUT_ENCODING,
            errors="replace",
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        log.write(f"WARNUNG: mkvpropedit nicht verfügbar oder Timeout: {error}")
        return False
    if result.returncode != 0:
        log.write(f"WARNUNG: mkvpropedit: {result.stderr.strip()}")
        return False
    return True


def rip_single_job(
    station: Station, drive: Drive, job: RipJob, disc_source: str, log: RipLog
) -> JobResult:
    """Rip one title into a private staging directory and publish it."""
    log.write(
        f"--- Starte Auftrag ({drive.job_step}): Track {job.track_id} "
        f"-> {job.output_path} ---"
    )
    staging_dir = tempfile.mkdtemp(
        prefix=f".ripstation-{drive.mkv_id}-{job.track_id}-",
        dir=os.path.dirname(job.output_path),
    )
    try:
        files_before = {p for p in Path(staging_dir).glob("*.mkv") if p.is_file()}
        cmd = [
            station.config.makemkv_cmd,
            "-r",
            "--progress=-same",
            "mkv",
            disc_source,
            str(job.track_id),
            staging_dir,
        ]
        try:
            exit_code = run_makemkv(station, drive, cmd, log)
        except ProcessStartError as error:
            log.write(f"FEHLER: MakeMKV konnte nicht gestartet werden: {error}")
            drive.status = DriveStatus.ERROR_START
            drive.message = "MakeMKV nicht startbar (siehe rip.log)"
            return JobResult.STOPPED

        if exit_code is None:
            log.write("WARNUNG: Vorgang wurde durch Benutzer abgebrochen.")
            return JobResult.STOPPED

        if exit_code != 0:
            drive.status = DriveStatus.ERROR_RIP
            if any(Path(staging_dir).iterdir()):
                log.write(f"Unvollständige Dateien verbleiben in: {staging_dir}")
                drive.message = f"Teildateien in {staging_dir}"
            else:
                log.write(
                    "MakeMKV wurde mit einem Fehler beendet; keine Teildatei vorhanden."
                )
            return JobResult.STOPPED

        drive.status = DriveStatus.PROCESSING
        try:
            publish_output(staging_dir, job, files_before)
        except OSError as error:
            log.write(f"FEHLER: {error}")
            drive.status = DriveStatus.ERROR_POST
            drive.message = str(error)
            return JobResult.STOPPED

        if set_title(station.config.mkvpropedit_cmd, job.output_path, log):
            return JobResult.DONE
        return JobResult.DONE_META_WARN
    finally:
        if drive.cancel_requested:
            # A cancelled rip is never resumed; drop the partial file.
            shutil.rmtree(staging_dir, ignore_errors=True)
        else:
            remove_empty_directory(staging_dir)


def _rip_jobs_worker(
    station: Station, drive: Drive, jobs: list[RipJob], disc_source: str
) -> None:
    drive.status = DriveStatus.STARTING
    drive.progress = 0
    metadata_warning = False
    first_job_stem = os.path.splitext(
        os.path.basename(jobs[0].output_path if jobs else "Unknown Job")
    )[0]
    original_job_name = re.sub(r"(?i)\.S\d+E\d+$", "", first_job_stem)

    for index, job in enumerate(jobs, 1):
        if drive.cancel_requested:
            break

        drive.job_step = f"{index}/{len(jobs)}"
        drive.status = DriveStatus.RIPPING
        drive.current_job = os.path.basename(job.output_path)
        drive.progress = 0

        target_dir = os.path.dirname(job.output_path)
        os.makedirs(target_dir, exist_ok=True)
        with open_log(target_dir) as handle:
            result = rip_single_job(
                station, drive, job, disc_source, RipLog(handle, drive)
            )
        if result is JobResult.STOPPED:
            break
        if result is JobResult.DONE_META_WARN:
            metadata_warning = True

    drive.progress = 0
    drive.job_step = ""
    if drive.cancel_requested:
        drive.status = DriveStatus.CANCELLED
    elif not drive.status.is_error:
        drive.current_job = original_job_name
        if station.config.auto_eject:
            drive.status = DriveStatus.EJECTING
            eject_error = eject_drive(drive.device_path)
            if eject_error:
                drive.message = eject_error
        drive.status = (
            DriveStatus.COMPLETED_META_WARN
            if metadata_warning
            else DriveStatus.COMPLETED
        )


def rip_jobs_worker(
    station: Station,
    drive: Drive,
    jobs: list[RipJob],
    disc_source: str | None = None,
) -> None:
    """Keep unexpected filesystem/process errors from leaving a drive busy forever."""
    disc_source = disc_source or drive.media_source or drive_source_candidates(drive)[0]
    try:
        _rip_jobs_worker(station, drive, jobs, disc_source)
    except Exception as error:
        drive.status = DriveStatus.ERROR_WORKER
        drive.progress = 0
        drive.message = str(error)
        try:
            with open_log(os.path.dirname(jobs[0].output_path)) as handle:
                RipLog(handle, drive).write(
                    f"FEHLER: Unerwarteter Fehler\n{traceback.format_exc()}"
                )
        except (OSError, IndexError):
            pass
    finally:
        station.release(drive, jobs)


def start_rip_jobs(
    station: Station, drive: Drive, jobs: list[RipJob]
) -> tuple[threading.Thread | None, str | None]:
    """Atomically reserve a drive and all outputs before starting its worker."""
    if not jobs:
        return None, "Keine Rip-Aufträge vorhanden."

    error_message = station.reserve(drive, jobs)
    if error_message:
        return None, error_message
    # Fixed for the whole job, even if a rescan updates the drive meanwhile.
    disc_source = drive.media_source or drive_source_candidates(drive)[0]

    try:
        thread = threading.Thread(
            target=rip_jobs_worker,
            args=(station, drive, jobs, disc_source),
            name=f"rip-{drive.device_path}",
        )
        thread.start()
    except Exception as error:
        station.release(drive, jobs)
        drive.status = DriveStatus.ERROR_START
        return None, f"Worker konnte nicht gestartet werden: {error}"
    return thread, None
