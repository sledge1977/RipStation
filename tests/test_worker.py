import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ripstation import worker
from ripstation.analyzer import canonical_output_path
from ripstation.models import Drive, DriveStatus, RipJob
from tests.helpers import SuccessfulProcess, make_station

TITLE_OK = SimpleNamespace(returncode=0, stderr="")


class HelperTests(unittest.TestCase):
    def test_progress_uses_overall_value_instead_of_current_operation(self):
        self.assertEqual(worker.parse_progress_line("PRGV:100,450,1000"), 45)
        self.assertIsNone(worker.parse_progress_line("PRGV:100,450,0"))
        self.assertIsNone(worker.parse_progress_line("PRGV:invalid"))

    def test_find_created_mkv_ignores_empty_name_and_stale_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stale = Path(temp_dir, "stale.mkv")
            stale.touch()
            created = Path(temp_dir, "new.mkv")
            created.touch()
            self.assertEqual(
                worker.find_created_mkv(temp_dir, "", {stale}), str(created)
            )

    def test_find_created_mkv_fails_without_a_new_regular_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stale = Path(temp_dir, "title.mkv")
            stale.touch()
            with self.assertRaises(FileNotFoundError):
                worker.find_created_mkv(temp_dir, "title.mkv", {stale})

    def test_staged_file_never_replaces_existing_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "source.mkv")
            target = Path(temp_dir, "target.mkv")
            source.write_bytes(b"new")
            target.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                worker.move_without_overwrite(str(source), str(target))
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(source.read_bytes(), b"new")

    @unittest.skipIf(os.name == "nt", "hard links are only used on POSIX")
    def test_move_falls_back_to_rename_without_hard_link_support(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "source.mkv")
            target = Path(temp_dir, "target.mkv")
            source.write_bytes(b"new")
            with patch(
                "ripstation.worker.os.link", side_effect=PermissionError(1, "EPERM")
            ):
                worker.move_without_overwrite(str(source), str(target))
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(source.exists())

    @unittest.skipIf(os.name == "nt", "hard links are only used on POSIX")
    def test_fallback_still_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir, "source.mkv")
            target = Path(temp_dir, "target.mkv")
            source.write_bytes(b"new")
            target.write_bytes(b"original")
            with (
                patch("ripstation.worker.os.link", side_effect=OSError(95, "ENOTSUP")),
                self.assertRaises(FileExistsError),
            ):
                worker.move_without_overwrite(str(source), str(target))
            self.assertEqual(target.read_bytes(), b"original")


class CancelTests(unittest.TestCase):
    def test_cancel_drive_rip(self):
        station = make_station()
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        drive.busy = True
        drive.active_process = SimpleNamespace(
            poll=lambda: None,
            terminate=lambda: None,
            wait=lambda timeout=None: 0,
            returncode=None,
        )
        self.assertTrue(worker.cancel_drive_rip(station, drive))
        self.assertTrue(drive.cancel_requested)

    def test_cancel_idle_drive_is_rejected(self):
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        self.assertFalse(worker.cancel_drive_rip(make_station(), drive))

    def test_stop_all_workers(self):
        station = make_station()
        d1 = Drive(1, "/dev/sr1", "D1", "Drive1")
        d2 = Drive(2, "/dev/sr2", "D2", "Drive2")
        d1.busy = True
        station.drives = [d1, d2]
        worker.stop_all_workers(station)
        self.assertTrue(d1.cancel_requested)
        self.assertFalse(d2.cancel_requested)

    def test_cancellation_before_worker_runs_is_preserved(self):
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        drive.cancel_requested = True
        with patch("subprocess.Popen") as popen:
            worker._rip_jobs_worker(
                make_station(), drive, [RipJob(0, "/unused/Movie.mkv")], "dev:/dev/sr0"
            )
        popen.assert_not_called()
        self.assertEqual(drive.status, DriveStatus.CANCELLED)

    def test_cancellation_during_process_launch_stops_process(self):
        station = make_station()
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        drive.busy = True

        class Process:
            stdout = []
            returncode = None
            terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

        process = Process()

        def launch(_command, **_kwargs):
            self.assertTrue(worker.cancel_drive_rip(station, drive))
            return process

        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "Movie.mkv")
            with patch("subprocess.Popen", side_effect=launch):
                worker._rip_jobs_worker(
                    station, drive, [RipJob(0, output)], "dev:/dev/sr0"
                )
        self.assertTrue(process.terminated)
        self.assertEqual(drive.status, DriveStatus.CANCELLED)

    def test_cancelled_rip_removes_partial_file(self):
        station = make_station()
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        drive.busy = True

        class PartialProcess:
            returncode = None

            def __init__(self, command):
                Path(command[-1], "partial.mkv").write_bytes(b"x")
                self.stdout = self.lines()

            def lines(self):
                yield "PRGV:0,10,100\n"
                drive.cancel_requested = True
                yield "PRGV:0,20,100\n"

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = [RipJob(0, os.path.join(temp_dir, "Movie.mkv"))]
            with patch(
                "subprocess.Popen", side_effect=lambda cmd, **_: PartialProcess(cmd)
            ):
                worker._rip_jobs_worker(station, drive, jobs, "dev:/dev/sr0")
            self.assertFalse(list(Path(temp_dir).glob(".ripstation-*")))
        self.assertEqual(drive.status, DriveStatus.CANCELLED)


class WorkerTests(unittest.TestCase):
    def run_worker(self, jobs, popen, run=TITLE_OK, drive=None, station=None):
        station = station or make_station()
        drive = drive or Drive(9, "/dev/sr9", "DISC", "Drive")
        run_kwargs = (
            {"side_effect": run}
            if isinstance(run, Exception)
            else {"return_value": run}
        )
        with (
            patch("subprocess.Popen", side_effect=popen),
            patch("subprocess.run", **run_kwargs) as run_mock,
            patch("ripstation.worker.eject_drive", return_value=None) as eject,
        ):
            worker._rip_jobs_worker(station, drive, jobs, "disc:9")
        return drive, run_mock, eject

    def test_worker_uses_fixed_source_and_isolated_staging_directory(self):
        commands = []

        def start_process(command, **kwargs):
            commands.append((command, kwargs))
            return SuccessfulProcess(command)

        with tempfile.TemporaryDirectory() as temp_dir:
            final_output = Path(temp_dir, "Movie.mkv")
            drive, _, eject = self.run_worker(
                [RipJob(1, str(final_output))], start_process
            )
            self.assertTrue(final_output.is_file())
            self.assertFalse(list(Path(temp_dir).glob(".ripstation-*")))
        command, kwargs = commands[0]
        self.assertEqual(command[4], "disc:9")
        self.assertNotEqual(command[-1], temp_dir)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(drive.status, DriveStatus.COMPLETED)
        self.assertEqual(drive.current_job, "Movie")
        eject.assert_called_once_with("/dev/sr9")

    def test_worker_resets_progress_before_each_track(self):
        drive = Drive(3, "/dev/sr3", "DISC", "Drive")
        progress_at_start = []

        def start_process(command, **_kwargs):
            progress_at_start.append(drive.progress)
            return SuccessfulProcess(command, stdout=["PRGV:0,100,100\n"])

        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = [
                RipJob(1, os.path.join(temp_dir, "Show.S01E01.mkv")),
                RipJob(2, os.path.join(temp_dir, "Show.S01E02.mkv")),
            ]
            self.run_worker(jobs, start_process, drive=drive)
        self.assertEqual(progress_at_start, [0, 0])
        self.assertEqual(drive.current_job, "Show")

    def test_log_lines_are_tagged_with_drive(self):
        def start_process(command, **_kwargs):
            return SuccessfulProcess(command, stdout=['MSG:1005,0,1,"Hello"\n'])

        with tempfile.TemporaryDirectory() as temp_dir:
            self.run_worker(
                [RipJob(1, os.path.join(temp_dir, "Movie.mkv"))], start_process
            )
            lines = Path(temp_dir, "rip.log").read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("[Laufwerk 9] ") for line in lines))
        self.assertIn('[Laufwerk 9] MSG:1005,0,1,"Hello"', lines)

    def test_metadata_timeout_is_only_a_warning(self):
        timeout = subprocess.TimeoutExpired("mkvpropedit", 120)
        with tempfile.TemporaryDirectory() as temp_dir:
            drive, run, _ = self.run_worker(
                [RipJob(1, os.path.join(temp_dir, "Movie.mkv"))],
                lambda command, **_: SuccessfulProcess(command),
                run=timeout,
            )
        self.assertEqual(drive.status, DriveStatus.COMPLETED_META_WARN)
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_eject_problem_is_shown_on_drive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "subprocess.Popen", side_effect=lambda c, **_: SuccessfulProcess(c)
                ),
                patch("subprocess.run", return_value=TITLE_OK),
                patch(
                    "ripstation.worker.eject_drive",
                    return_value="Bitte manuell auswerfen",
                ),
            ):
                drive = Drive(1, "D:", "DISC", "Drive")
                worker._rip_jobs_worker(
                    make_station(),
                    drive,
                    [RipJob(1, os.path.join(temp_dir, "M.mkv"))],
                    "disc:1",
                )
        self.assertEqual(drive.status, DriveStatus.COMPLETED)
        self.assertEqual(drive.message, "Bitte manuell auswerfen")

    def test_no_eject_when_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, eject = self.run_worker(
                [RipJob(1, os.path.join(temp_dir, "Movie.mkv"))],
                lambda command, **_: SuccessfulProcess(command),
                station=make_station(auto_eject=False),
            )
        eject.assert_not_called()

    def test_failed_rip_without_artifact_logs_that_no_partial_file_exists(self):
        class FailedProcess:
            stdout = []
            returncode = 1

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as temp_dir:
            drive, _, _ = self.run_worker(
                [RipJob(1, os.path.join(temp_dir, "Movie.mkv"), "title.mkv")],
                lambda *_a, **_k: FailedProcess(),
            )
            log = Path(temp_dir, "rip.log").read_text(encoding="utf-8")
        self.assertIn("keine Teildatei vorhanden", log)
        self.assertNotIn("Unvollständige Dateien verbleiben", log)
        self.assertEqual(drive.status, DriveStatus.ERROR_RIP)

    def test_failed_rip_keeps_partial_file_and_reports_it(self):
        class FailedProcess:
            stdout = []
            returncode = 1

            def __init__(self, command):
                Path(command[-1], "partial.mkv").write_bytes(b"x")

            def wait(self, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as temp_dir:
            drive, _, _ = self.run_worker(
                [RipJob(1, os.path.join(temp_dir, "Movie.mkv"))],
                lambda command, **_: FailedProcess(command),
            )
            kept = list(Path(temp_dir).glob(".ripstation-*/partial.mkv"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(drive.status, DriveStatus.ERROR_RIP)
        self.assertIn("Teildateien in", drive.message)

    def test_missing_makemkv_is_a_start_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            drive, _, _ = self.run_worker(
                [RipJob(1, os.path.join(temp_dir, "Movie.mkv"))],
                FileNotFoundError("makemkvcon"),
            )
            self.assertFalse(list(Path(temp_dir).glob(".ripstation-*")))
        self.assertEqual(drive.status, DriveStatus.ERROR_START)

    def test_worker_reports_unexpected_setup_error(self):
        station = make_station()
        drive = Drive(0, "/dev/test", "disc", "drive")
        with patch("ripstation.worker.os.makedirs", side_effect=OSError("read only")):
            worker.rip_jobs_worker(
                station, drive, [RipJob(1, "/video/Show.S01E01.mkv")]
            )
        self.assertEqual(drive.status, DriveStatus.ERROR_WORKER)
        self.assertEqual(drive.message, "read only")
        self.assertEqual(drive.progress, 0)

    def test_worker_terminates_and_kills_hung_process_before_release(self):
        station = make_station()
        drive = Drive(5, "/dev/sr5", "DISC", "Drive")

        class BrokenOutput:
            def __iter__(self):
                raise OSError("stdout failed")

        class HungProcess:
            stdout = BrokenOutput()
            returncode = None
            terminated = False
            killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired("makemkvcon", timeout)
                self.returncode = -9
                return self.returncode

            def kill(self):
                self.killed = True

        process = HungProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = [RipJob(1, os.path.join(temp_dir, "Movie.mkv"), "title.mkv")]
            self.assertIsNone(station.reserve(drive, jobs))
            with patch("subprocess.Popen", return_value=process):
                worker.rip_jobs_worker(station, drive, jobs, "dev:/dev/sr5")
            self.assertFalse(list(Path(temp_dir).glob(".ripstation-*")))
            self.assertIn(
                "stdout failed", Path(temp_dir, "rip.log").read_text(encoding="utf-8")
            )

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertFalse(drive.busy)
        self.assertFalse(station.reserved_outputs)
        self.assertEqual(drive.status, DriveStatus.ERROR_WORKER)


class StartTests(unittest.TestCase):
    def test_start_reserves_outputs_and_keeps_source_stable(self):
        station = make_station()
        entered = threading.Event()
        finish = threading.Event()
        captured_sources = []
        first_drive = Drive(4, "/dev/sr4", "DISC_A", "Drive A")
        second_drive = Drive(7, "/dev/sr7", "DISC_B", "Drive B")

        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = [RipJob(1, os.path.join(temp_dir, "Show.S01E01.mkv"), "title.mkv")]

            def blocking_worker(_station, _drive, _jobs, disc_source):
                captured_sources.append(disc_source)
                entered.set()
                finish.wait(2)

            with patch(
                "ripstation.worker._rip_jobs_worker", side_effect=blocking_worker
            ):
                thread, error = worker.start_rip_jobs(station, first_drive, jobs)
                try:
                    self.assertIsNone(error)
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(first_drive.busy)
                    self.assertIn(
                        canonical_output_path(jobs[0].output_path),
                        station.reserved_outputs,
                    )

                    first_drive.mkv_id = 99
                    _, second_error = worker.start_rip_jobs(station, second_drive, jobs)
                    self.assertIn("reserviert", second_error)
                    self.assertFalse(second_drive.busy)
                    self.assertEqual(captured_sources, ["dev:/dev/sr4"])
                finally:
                    finish.set()
                    thread.join(2)

        self.assertFalse(first_drive.busy)
        self.assertFalse(station.reserved_outputs)

    def test_failed_thread_start_releases_drive_and_outputs(self):
        station = make_station()
        drive = Drive(1, "/dev/sr1", "DISC", "Drive")
        jobs = [RipJob(1, "/video/Movie/Movie.mkv", "title.mkv")]
        with patch(
            "ripstation.worker.threading.Thread", side_effect=RuntimeError("no")
        ):
            thread, error = worker.start_rip_jobs(station, drive, jobs)
        self.assertIsNone(thread)
        self.assertIn("nicht gestartet", error)
        self.assertFalse(drive.busy)
        self.assertFalse(station.reserved_outputs)
        self.assertEqual(drive.status, DriveStatus.ERROR_START)

    def test_empty_job_list_is_rejected(self):
        drive = Drive(1, "/dev/sr1", "DISC", "Drive")
        thread, error = worker.start_rip_jobs(make_station(), drive, [])
        self.assertIsNone(thread)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
