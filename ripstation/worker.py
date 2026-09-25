import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from ripstation.analyzer import canonical_output_path
from ripstation.config import detect_makemkv_cmd, detect_mkvpropedit_cmd
from ripstation.hardware import drive_source_candidates, eject_drive
from ripstation.models import Drive

job_state_lock = threading.Lock()
reserved_outputs: Set[str] = set()


def parse_progress_line(line: str) -> Optional[int]:
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


def release_output_paths(
    jobs: List[Tuple[int, str, str]],
    target_set: Optional[Set[str]] = None,
    lock: Any = None,
) -> None:
    paths = {canonical_output_path(output) for _, output, _ in jobs}
    lock_to_use = lock or job_state_lock
    set_to_use = target_set if target_set is not None else reserved_outputs
    with lock_to_use:
        set_to_use.difference_update(paths)


def find_created_mkv(
    target_dir: str, source_mkv_filename: str, files_before_rip: Set[Path]
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


def ensure_process_stopped(proc: Optional[subprocess.Popen], timeout: int = 10) -> None:
    """Terminate and, if necessary, kill a child that did not exit normally."""
    if proc is None or getattr(proc, "returncode", None) is not None:
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
        return


def cancel_drive_rip(drive: Drive) -> bool:
    """Request cancellation of an ongoing rip on the given drive."""
    with job_state_lock:
        if not drive.busy:
            return False
        drive.cancel_requested = True
        proc = drive.active_process
    if proc is not None:
        ensure_process_stopped(proc, timeout=5)
    return True


def stop_all_workers(drives_list: List[Drive]) -> None:
    """Stop all active worker processes (e.g. on SIGINT/shutdown)."""
    for drive in drives_list:
        if drive.busy or drive.active_process is not None:
            cancel_drive_rip(drive)


def _rip_jobs_worker(
    drive: Drive,
    jobs: List[Tuple[int, str, str]],
    disc_source: str,
    makemkv_cmd: Optional[str] = None,
    mkvpropedit_cmd: Optional[str] = None,
    auto_eject: bool = True,
) -> None:
    makemkv = makemkv_cmd or detect_makemkv_cmd()
    mkvpropedit = mkvpropedit_cmd or detect_mkvpropedit_cmd()

    drive.status = "Starting..."
    drive.progress = 0
    drive.cancel_requested = False
    total_jobs = len(jobs)
    metadata_warning = False
    first_job_stem = os.path.splitext(
        os.path.basename(jobs[0][1] if jobs else "Unknown Job")
    )[0]
    original_job_name = re.sub(r"(?i)\.S\d+E\d+$", "", first_job_stem)

    for i, (track_id, final_output_path, source_mkv_filename) in enumerate(jobs):
        if drive.cancel_requested:
            drive.status = "CANCELLED"
            break

        job_progress_text = f"({i + 1}/{total_jobs})"
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
            makemkv,
            "-r",
            "--progress=-same",
            "mkv",
            disc_source,
            str(track_id),
            staging_dir,
        ]

        log_file = os.path.join(os.path.dirname(jobs[0][1]), "rip.log")
        creation_flags = 0x08000000 if os.name == "nt" else 0

        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(
                f"\n--- Starting Job {job_progress_text}: Rip Track {track_id} ---\n"
            )
            proc: Optional[subprocess.Popen] = None
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
                    drive.active_process = proc
                except OSError as error:
                    f.write(f"ERROR: MakeMKV konnte nicht gestartet werden: {error}\n")
                    drive.status = "ERROR (Start)"
                    remove_empty_directory(staging_dir)
                    break

                for line in proc.stdout:  # type: ignore
                    if drive.cancel_requested:
                        ensure_process_stopped(proc)
                        break
                    f.write(line)
                    progress = parse_progress_line(line)
                    if progress is not None:
                        drive.progress = progress

                proc.wait()
            finally:
                drive.active_process = None
                ensure_process_stopped(proc)
                remove_empty_directory(staging_dir)

            if drive.cancel_requested:
                drive.status = "CANCELLED"
                f.write("WARNUNG: Vorgang wurde durch Benutzer abgebrochen.\n")
                remove_empty_directory(staging_dir)
                break

            drive.status = f"Processing {job_progress_text}"

            if proc.returncode == 0:
                try:
                    created_file = find_created_mkv(
                        staging_dir, source_mkv_filename, files_before_rip
                    )

                    paths_are_equal = os.path.normcase(
                        os.path.abspath(created_file)
                    ) == os.path.normcase(os.path.abspath(final_output_path))
                    if os.path.exists(final_output_path) and not paths_are_equal:
                        raise FileExistsError(
                            f"Zieldatei existiert bereits, wird nicht überschrieben: {final_output_path}"
                        )

                    if not paths_are_equal:
                        os.rename(created_file, final_output_path)

                    # Metadata title (via mkvpropedit)
                    try:
                        meta_result = subprocess.run(
                            [
                                mkvpropedit,
                                final_output_path,
                                "--edit",
                                "info",
                                "--set",
                                f"title={os.path.splitext(os.path.basename(final_output_path))[0]}",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=120,
                            creationflags=creation_flags,
                        )
                        if meta_result.returncode != 0:
                            metadata_warning = True
                            f.write(
                                f"WARNING: mkvpropedit: {meta_result.stderr.strip()}\n"
                            )
                    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                        metadata_warning = True
                        f.write(
                            f"WARNING: mkvpropedit nicht verfügbar oder Timeout: {error}\n"
                        )
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

    if drive.cancel_requested:
        drive.status = "CANCELLED"
    elif not drive.status.startswith("ERROR"):
        final_status = "COMPLETED (META WARN)" if metadata_warning else "COMPLETED"
        drive.current_job = original_job_name
        drive.progress = 0
        if auto_eject:
            drive.status = "Ejecting..."
            eject_drive(drive.device_path)
        drive.status = final_status

    drive.progress = 0


def rip_jobs_worker(
    drive: Drive,
    jobs: List[Tuple[int, str, str]],
    disc_source: Optional[str] = None,
    owns_job_state: bool = False,
    makemkv_cmd: Optional[str] = None,
    mkvpropedit_cmd: Optional[str] = None,
    auto_eject: bool = True,
) -> None:
    """Keep unexpected filesystem/process errors from leaving a drive busy forever."""
    disc_source = disc_source or drive.media_source or drive_source_candidates(drive)[0]
    try:
        _rip_jobs_worker(
            drive,
            jobs,
            disc_source,
            makemkv_cmd=makemkv_cmd,
            mkvpropedit_cmd=mkvpropedit_cmd,
            auto_eject=auto_eject,
        )
    except Exception as error:
        drive.status = "ERROR (Worker)"
        drive.progress = 0
        print(f"Unerwarteter Fehler bei Laufwerk {drive.device_path}: {error}")
    finally:
        if owns_job_state:
            release_output_paths(jobs)
            with job_state_lock:
                drive.busy = False
                drive.active_process = None


def start_rip_jobs(
    drive: Drive,
    jobs: List[Tuple[int, str, str]],
    makemkv_cmd: Optional[str] = None,
    mkvpropedit_cmd: Optional[str] = None,
    auto_eject: bool = True,
) -> Tuple[Optional[threading.Thread], Optional[str]]:
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
            output
            for output, normalized in zip(outputs, canonical)
            if normalized in duplicate_paths
            or normalized in reserved_outputs
            or os.path.exists(output)
        ]
        if conflicts:
            return (
                None,
                "Zieldatei bereits vorhanden oder reserviert:\n  "
                + "\n  ".join(conflicts),
            )

        reserved_outputs.update(canonical)
        drive.busy = True
        drive.status = "Starting..."
        drive.progress = 0
        drive.cancel_requested = False
        disc_source = drive.media_source or drive_source_candidates(drive)[0]

    try:
        thread = threading.Thread(
            target=rip_jobs_worker,
            args=(
                drive,
                jobs,
                disc_source,
                True,
                makemkv_cmd,
                mkvpropedit_cmd,
                auto_eject,
            ),
            name=f"rip-{drive.device_path}",
        )
        thread.start()
    except Exception as error:
        release_output_paths(jobs)
        with job_state_lock:
            drive.busy = False
            drive.status = "ERROR (Start)"
            drive.active_process = None
        return None, f"Worker konnte nicht gestartet werden: {error}"
    return thread, None
