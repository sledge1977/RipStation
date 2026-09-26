import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ripstation.analyzer import build_series_jobs, canonical_output_path
from ripstation.models import Drive, DriveStatus, RipJob
from tests.helpers import make_station, track


def rescan(station, drives_info):
    with (
        patch("ripstation.station.fetch_drive_info", return_value=drives_info),
        patch("ripstation.station.time.sleep"),
        redirect_stdout(io.StringIO()),
    ):
        station.rescan_idle_drives()


class RescanTests(unittest.TestCase):
    def test_rescan_does_not_mutate_busy_drive(self):
        station = make_station()
        drive = Drive(2, "/dev/sr2", "OLD_DISC", "Old drive")
        drive.busy = True
        drive.status = DriveStatus.RIPPING
        station.drives = [drive]
        rescan(station, [(12, "New drive", "NEW_DISC", "/dev/sr2")])
        self.assertEqual(
            (drive.mkv_id, drive.name, drive.label), (2, "Old drive", "OLD_DISC")
        )
        self.assertEqual(len(station.drives), 1)

    def test_rescan_resets_idle_drive_even_when_volume_label_is_unchanged(self):
        station = make_station()
        drive = Drive(0, "/dev/sr0", "SAME_LABEL", "Old drive")
        drive.status = DriveStatus.COMPLETED
        drive.current_job = "Old.Movie"
        drive.message = "Bitte manuell auswerfen"
        drive.media_source = "disc:0"
        station.drives = [drive]
        rescan(station, [(1, "Updated drive", "SAME_LABEL", "/dev/sr0")])
        self.assertEqual(
            (drive.mkv_id, drive.name, drive.label), (1, "Updated drive", "SAME_LABEL")
        )
        self.assertEqual(drive.status, DriveStatus.IDLE)
        self.assertEqual((drive.current_job, drive.message), ("", ""))
        self.assertIsNone(drive.media_source)

    def test_rescan_adds_new_drives_and_clears_missing_media(self):
        station = make_station()
        drive = Drive(0, "/dev/sr0", "OLD", "Drive")
        station.drives = [drive]
        rescan(station, [(1, "Other", "NEW", "/dev/sr1")])
        self.assertEqual(drive.label, "")
        self.assertEqual(
            [d.device_path for d in station.drives], ["/dev/sr0", "/dev/sr1"]
        )


class ReservationTests(unittest.TestCase):
    def test_existing_output_is_rejected(self):
        station = make_station()
        drive = Drive(1, "/dev/sr1", "DISC", "Drive")
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir, "Movie.mkv")
            output.touch()
            error = station.reserve(drive, [RipJob(1, str(output), "title.mkv")])
        self.assertIn("vorhanden", error)
        self.assertFalse(drive.busy)

    def test_duplicate_outputs_in_one_request_are_rejected(self):
        station = make_station()
        drive = Drive(1, "/dev/sr1", "DISC", "Drive")
        jobs = [RipJob(1, "/video/A.mkv"), RipJob(2, "/video/A.mkv")]
        self.assertIn("reserviert", station.reserve(drive, jobs))
        self.assertFalse(station.reserved_outputs)

    def test_release_is_idempotent(self):
        station = make_station()
        drive = Drive(1, "/dev/sr1", "DISC", "Drive")
        jobs = [RipJob(1, "/video/Movie/Movie.mkv")]
        self.assertIsNone(station.reserve(drive, jobs))
        self.assertTrue(drive.busy)
        station.release(drive, jobs)
        station.release(drive, jobs)
        self.assertFalse(drive.busy)
        self.assertFalse(station.reserved_outputs)

    def test_reserved_episodes_are_included_in_next_number(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            station = make_station(temp_dir)
            jobs = build_series_jobs(
                "Show", 1, [track(1, 1500, 1), track(2, 1500, 2)], [1, 2], temp_dir
            )
            station.reserved_outputs.update(
                canonical_output_path(job.output_path) for job in jobs
            )
            self.assertEqual(station.next_episode_number("Show", 1), 3)
            self.assertTrue(os.path.isdir(temp_dir))


if __name__ == "__main__":
    unittest.main()
