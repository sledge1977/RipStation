import io
import os
import unittest
from unittest.mock import patch

from ripstation import analyzer, ui
from ripstation.models import Drive, DriveStatus
from ripstation.prompts import render_series_tracks
from tests.helpers import track


class MenuInputTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX stdin handling")
    def test_non_tty_menu_accepts_multi_digit_drive_id(self):
        stream = io.StringIO("12\n")
        with (
            patch.object(ui.sys, "stdin", stream),
            patch("ripstation.ui.select.select", return_value=([stream], [], [])),
        ):
            self.assertEqual(ui.read_menu_command(), "12")

    def test_drive_id_prefix_waits_only_when_ambiguous(self):
        self.assertFalse(ui.command_needs_more_digits("1", [0, 1, 2]))
        self.assertTrue(ui.command_needs_more_digits("1", [1, 10, 12]))
        self.assertFalse(ui.command_needs_more_digits("10", [1, 10, 12]))


class ResponsiveUiTests(unittest.TestCase):
    def setUp(self):
        self.drives = [
            Drive(0, "/dev/sr0", "SERIES_DISC_WITH_A_LONG_NAME", "HL-DT-ST Blu-ray"),
            Drive(1, "/dev/sr1", "FILM_DISC", "Pioneer 日本語"),
        ]
        self.drives[0].status = DriveStatus.RIPPING
        self.drives[0].job_step = "2/4"
        self.drives[0].progress = 57
        self.drives[0].current_job = "Meine Serie.S01E02.mkv"

    def test_every_layout_stays_inside_terminal(self):
        for width in (20, 40, 67, 68, 90, 109, 110, 140):
            for height in (8, 12, 24):
                with self.subTest(width=width, height=height):
                    rendered = ui.render_ui(self.drives, width, height)
                    lines = rendered.splitlines()
                    self.assertLessEqual(len(lines) + 1, max(8, height))
                    self.assertTrue(
                        all(ui.display_width(line) <= width for line in lines)
                    )

    def test_layout_breakpoints(self):
        compact = ui.render_ui(self.drives, 50, 24)
        medium = ui.render_ui(self.drives, 80, 24)
        wide = ui.render_ui(self.drives, 120, 24)
        self.assertIn("[0] /dev/sr0", compact)
        self.assertNotIn("Fortschritt", compact)
        self.assertIn("Medium / Auftrag", medium)
        self.assertNotIn("Fortschritt", medium)
        self.assertIn("Fortschritt", wide)
        self.assertIn("Laufwerk", wide)
        self.assertIn("Rippe (2/4)", wide)
        self.assertIn("57%", wide)

    def test_small_height_reports_hidden_drives(self):
        many_drives = [
            Drive(index, f"/dev/sr{index}", f"DISC_{index}", "Drive")
            for index in range(8)
        ]
        rendered = ui.render_ui(many_drives, 80, 8)
        self.assertIn("weitere Laufwerke", rendered)

    def test_unicode_text_is_fitted_by_terminal_cells(self):
        fitted = ui.fit_text("日本語 und mehr", 10)
        self.assertEqual(ui.display_width(fitted), 10)

    def test_every_status_has_a_translation(self):
        drive = Drive(0, "/dev/sr0", "", "")
        for status in DriveStatus:
            drive.status = status
            self.assertNotIn("_", ui.status_text(drive))

    def test_drive_message_is_shown_with_job(self):
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        drive.status = DriveStatus.COMPLETED
        drive.current_job = "Movie"
        drive.message = "Bitte manuell auswerfen"
        self.assertEqual(ui.drive_info_text(drive), "Movie · Bitte manuell auswerfen")

    def test_series_selection_uses_responsive_layouts(self):
        tracks = [
            track(1, 1500, 1, source_filename="00001.mpls", title_name="Show S01E01"),
            track(2, 1530, 2, source_filename="a_very_long_source_filename.mpls"),
        ]
        analysis = analyzer.analyze_series_tracks(tracks)
        for width in (20, 47, 48, 75, 76, 100):
            with self.subTest(width=width):
                rendered = render_series_tracks(tracks, analysis, width)
                self.assertTrue(
                    all(
                        ui.display_width(line) <= width
                        for line in rendered.splitlines()
                    )
                )


if __name__ == "__main__":
    unittest.main()
