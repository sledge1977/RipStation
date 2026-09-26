import io
import subprocess
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from ripstation import hardware
from ripstation.models import Drive


class DriveScanTests(unittest.TestCase):
    def test_fetch_drive_info_raises_instead_of_exiting(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("missing")):
            with self.assertRaises(RuntimeError):
                hardware.fetch_drive_info("makemkvcon")

    def test_fetch_drive_info_decodes_output_as_utf8(self):
        result = SimpleNamespace(stdout="")
        with patch("subprocess.run", return_value=result) as run:
            hardware.fetch_drive_info("makemkvcon")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_malformed_drive_lines_are_skipped(self):
        output = "\n".join(
            [
                'DRV:x,2,999,1,"BD-RE","BAD","/dev/sr9"',
                'DRV:0,2,999,1,"BD-RE HL-DT-ST","SHOW_S01","/dev/sr0"',
                'DRV:1,256,999,0,"","",""',
                'DRV:2,2,999,1,"DVD"',
            ]
        )
        self.assertEqual(
            hardware.parse_drive_lines(output),
            [(0, "BD-RE HL-DT-ST", "SHOW_S01", "/dev/sr0")],
        )

    def test_track_scan_prefers_device_source(self):
        drive = Drive(3, "/dev/sr3", "DISC", "Drive")
        result = SimpleNamespace(stdout='TINFO:0,9,0,"0:42:00"')
        with patch("subprocess.run", return_value=result) as run:
            tracks = hardware.get_disc_tracks(drive, 60, "makemkvcon")
        self.assertEqual(tracks[0].id, 0)
        self.assertEqual(run.call_args.args[0][-1], "dev:/dev/sr3")
        self.assertEqual(drive.media_source, "dev:/dev/sr3")

    def test_track_scan_falls_back_to_disc_id(self):
        drive = Drive(3, "/dev/sr3", "DISC", "Drive")
        result = SimpleNamespace(stdout='TINFO:0,9,0,"0:42:00"')
        first_error = subprocess.CalledProcessError(1, ["makemkvcon"])
        with patch("subprocess.run", side_effect=[first_error, result]) as run:
            tracks = hardware.get_disc_tracks(drive, 60, "makemkvcon")
        self.assertTrue(tracks)
        self.assertEqual(run.call_args_list[0].args[0][-1], "dev:/dev/sr3")
        self.assertEqual(run.call_args_list[1].args[0][-1], "disc:3")
        self.assertEqual(drive.media_source, "disc:3")

    def test_track_scan_filters_short_titles(self):
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        result = SimpleNamespace(stdout='TINFO:0,9,0,"0:00:30"\nTINFO:1,9,0,"0:05:00"')
        with (
            patch("subprocess.run", return_value=result),
            redirect_stdout(io.StringIO()),
        ):
            tracks = hardware.get_disc_tracks(drive, 60, "makemkvcon")
        self.assertEqual([t.id for t in tracks], [1])


class EjectTests(unittest.TestCase):
    def test_eject_has_timeout(self):
        with patch("subprocess.run") as run:
            self.assertIsNone(hardware.eject_drive("/dev/sr0"))
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_eject_failure_is_returned_not_printed(self):
        output = io.StringIO()
        error = subprocess.CalledProcessError(1, ["eject"])
        with (
            patch("ripstation.hardware.os.name", "posix"),
            patch("subprocess.run", side_effect=error),
            redirect_stdout(output),
        ):
            message = hardware.eject_drive("/dev/sr0")
        self.assertIn("Auswerfen fehlgeschlagen", message)
        self.assertEqual(output.getvalue(), "")

    def test_eject_drive_windows_powershell(self):
        with (
            patch("ripstation.hardware.os.name", "nt"),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.assertIsNone(hardware.eject_drive("D:"))
        self.assertEqual(run.call_args[0][0][0], "powershell")
        self.assertIn("getByDriveSpecifier('D:')", run.call_args[0][0][-1])


if __name__ == "__main__":
    unittest.main()
