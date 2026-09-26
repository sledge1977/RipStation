import io
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ripstation import app
from ripstation.models import Drive
from tests.helpers import make_station


class DashboardTests(unittest.TestCase):
    def run_in_thread(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        thread.join(1)
        return thread

    def test_quit_with_idle_drive_returns(self):
        station = make_station()
        station.drives = [Drive(0, "/dev/sr0", "DISC", "Drive")]
        with (
            patch("ripstation.app.print_ui"),
            patch("ripstation.app.read_menu_command", return_value="q"),
            redirect_stdout(io.StringIO()),
        ):
            thread = self.run_in_thread(app.run_dashboard, station)
        self.assertFalse(thread.is_alive(), "Beenden hängt bei erkanntem Laufwerk")

    def test_quit_is_refused_while_a_rip_is_running(self):
        station = make_station()
        drive = Drive(0, "/dev/sr0", "DISC", "Drive")
        drive.busy = True
        station.drives = [drive]
        calls = []

        def next_command(**_kwargs):
            calls.append("q")
            if len(calls) == 2:
                drive.busy = False  # the rip finishes after the first attempt
            return "q"

        with (
            patch("ripstation.app.print_ui"),
            patch("ripstation.app.read_menu_command", side_effect=next_command),
            patch("ripstation.app.time.sleep") as sleep,
            redirect_stdout(io.StringIO()) as output,
        ):
            thread = self.run_in_thread(app.run_dashboard, station)
        self.assertFalse(thread.is_alive())
        self.assertIn("Ein Rip läuft noch", output.getvalue())
        sleep.assert_called()
        self.assertEqual(len(calls), 2)

    def test_interrupt_stops_workers_without_signal_handler_cleanup(self):
        with (
            patch("ripstation.app.Station.scan_drives"),
            patch("ripstation.app.run_dashboard", side_effect=KeyboardInterrupt),
            patch("ripstation.app.stop_all_workers") as stop,
            redirect_stdout(io.StringIO()) as output,
        ):
            thread = self.run_in_thread(app.main, [])
        self.assertFalse(thread.is_alive())
        stop.assert_called_once()
        self.assertIn("Abbruch-Signal", output.getvalue())

    def test_regular_quit_also_runs_cleanup(self):
        with (
            patch("ripstation.app.Station.scan_drives"),
            patch("ripstation.app.run_dashboard"),
            patch("ripstation.app.stop_all_workers") as stop,
        ):
            app.main([])
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
