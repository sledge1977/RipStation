import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rip_station


def track(track_id, duration, size, **extra):
    value = {
        'id': track_id,
        'duration_seconds': duration,
        'duration_str': f"00:{duration // 60:02d}:00",
        'size_bytes': size,
        'output_filename': f"title_t{track_id:02d}.mkv",
    }
    value.update(extra)
    return value


class ParsingTests(unittest.TestCase):
    def test_tinfo_csv_values_may_contain_commas(self):
        output = '\n'.join([
            'TINFO:0,2,0,"Pilot, Part One"',
            'TINFO:0,9,0,"0:42:03"',
            'TINFO:0,10,0,"4.2 GB"',
            'TINFO:0,11,0,"4200000000"',
            'TINFO:0,16,0,"00001.mpls"',
            'TINFO:0,27,0,"title_t00.mkv"',
        ])
        parsed = rip_station.parse_tinfo_output(output)
        self.assertEqual(parsed[0]['title_name'], 'Pilot, Part One')
        self.assertEqual(parsed[0]['duration_seconds'], 2523)
        self.assertEqual(parsed[0]['source_filename'], '00001.mpls')

    def test_selection_preserves_manual_and_reverse_order(self):
        self.assertEqual(rip_station.parse_track_selection('5, 3-1, 5'), [5, 3, 2, 1])

    def test_range_formatting(self):
        self.assertEqual(rip_station.format_number_ranges([1, 2, 3, 6, 8, 7]), '1-3,6,8-7')

    def test_fetch_drive_info_raises_instead_of_exiting(self):
        with patch('rip_station.subprocess.run', side_effect=FileNotFoundError('missing')):
            with self.assertRaises(RuntimeError):
                rip_station.fetch_drive_info()

    def test_track_scan_prefers_device_source(self):
        drive = rip_station.Drive(3, '/dev/sr3', 'DISC', 'Drive')
        result = SimpleNamespace(stdout='TINFO:0,9,0,"0:42:00"')
        with patch('rip_station.subprocess.run', return_value=result) as run:
            tracks = rip_station.get_disc_tracks(drive, 60)
        self.assertEqual(tracks[0]['id'], 0)
        self.assertEqual(run.call_args.args[0][-1], 'dev:/dev/sr3')
        self.assertEqual(drive.media_source, 'dev:/dev/sr3')

    def test_track_scan_falls_back_to_disc_id(self):
        drive = rip_station.Drive(3, '/dev/sr3', 'DISC', 'Drive')
        result = SimpleNamespace(stdout='TINFO:0,9,0,"0:42:00"')
        first_error = subprocess.CalledProcessError(1, ['makemkvcon'])
        with patch('rip_station.subprocess.run', side_effect=[first_error, result]) as run:
            tracks = rip_station.get_disc_tracks(drive, 60)
        self.assertTrue(tracks)
        self.assertEqual(run.call_args_list[0].args[0][-1], 'dev:/dev/sr3')
        self.assertEqual(run.call_args_list[1].args[0][-1], 'disc:3')
        self.assertEqual(drive.media_source, 'disc:3')

    def test_non_tty_menu_accepts_multi_digit_drive_id(self):
        stream = io.StringIO('12\n')
        with patch.object(rip_station.sys, 'stdin', stream), \
                patch('rip_station.select.select', return_value=([stream], [], [])):
            self.assertEqual(rip_station.read_menu_command(), '12')

    def test_drive_id_prefix_waits_only_when_ambiguous(self):
        self.assertFalse(rip_station.command_needs_more_digits('1', [0, 1, 2]))
        self.assertTrue(rip_station.command_needs_more_digits('1', [1, 10, 12]))
        self.assertFalse(rip_station.command_needs_more_digits('10', [1, 10, 12]))

    def test_progress_uses_overall_value_instead_of_current_operation(self):
        self.assertEqual(rip_station.parse_progress_line('PRGV:100,450,1000'), 45)
        self.assertIsNone(rip_station.parse_progress_line('PRGV:100,450,0'))
        self.assertIsNone(rip_station.parse_progress_line('PRGV:invalid'))


class ResponsiveUiTests(unittest.TestCase):
    def setUp(self):
        self.drives = [
            rip_station.Drive(0, '/dev/sr0', 'SERIES_DISC_WITH_A_LONG_NAME', 'HL-DT-ST Blu-ray'),
            rip_station.Drive(1, '/dev/sr1', 'FILM_DISC', 'Pioneer 日本語'),
        ]
        self.drives[0].status = 'Ripping (2/4)'
        self.drives[0].progress = 57
        self.drives[0].current_job = 'Meine Serie.S01E02.mkv'

    def test_every_layout_stays_inside_terminal(self):
        for width in (20, 40, 67, 68, 90, 109, 110, 140):
            for height in (8, 12, 24):
                with self.subTest(width=width, height=height):
                    rendered = rip_station.render_ui(self.drives, width, height)
                    lines = rendered.splitlines()
                    self.assertLessEqual(len(lines) + 1, max(8, height))
                    self.assertTrue(all(rip_station.display_width(line) <= width for line in lines))

    def test_layout_breakpoints(self):
        compact = rip_station.render_ui(self.drives, 50, 24)
        medium = rip_station.render_ui(self.drives, 80, 24)
        wide = rip_station.render_ui(self.drives, 120, 24)
        self.assertIn('[0] /dev/sr0', compact)
        self.assertNotIn('Fortschritt', compact)
        self.assertIn('Medium / Auftrag', medium)
        self.assertNotIn('Fortschritt', medium)
        self.assertIn('Fortschritt', wide)
        self.assertIn('Laufwerk', wide)

    def test_small_height_reports_hidden_drives(self):
        many_drives = [
            rip_station.Drive(index, f'/dev/sr{index}', f'DISC_{index}', 'Drive')
            for index in range(8)
        ]
        rendered = rip_station.render_ui(many_drives, 80, 8)
        self.assertIn('weitere Laufwerke', rendered)

    def test_unicode_text_is_fitted_by_terminal_cells(self):
        fitted = rip_station.fit_text('日本語 und mehr', 10)
        self.assertEqual(rip_station.display_width(fitted), 10)

    def test_series_selection_uses_responsive_layouts(self):
        tracks = [
            track(1, 1500, 1, source_filename='00001.mpls', title_name='Show S01E01'),
            track(2, 1530, 2, source_filename='a_very_long_source_filename.mpls'),
        ]
        analysis = rip_station.analyze_series_tracks(tracks)
        for width in (20, 47, 48, 75, 76, 100):
            with self.subTest(width=width):
                rendered = rip_station.render_series_tracks(tracks, analysis, width)
                self.assertTrue(
                    all(rip_station.display_width(line) <= width for line in rendered.splitlines())
                )


class SeriesDetectionTests(unittest.TestCase):
    def test_recommends_runtime_cluster_and_flags_duplicate(self):
        tracks = [
            track(0, 2580, 4_000_000_000),
            track(1, 2640, 4_100_000_000),
            track(2, 2520, 3_900_000_000),
            track(3, 2640, 4_100_000_000),
            track(4, 900, 900_000_000),
            track(5, 7200, 12_000_000_000),
        ]
        analysis = rip_station.analyze_series_tracks(tracks)
        self.assertEqual(analysis.recommended_ids, [0, 1, 2])
        self.assertEqual(analysis.duplicate_of, {3: 1})
        self.assertEqual(analysis.confidence, 'hoch')

    def test_explicit_episode_markers_determine_order(self):
        tracks = [
            track(2, 1500, 2, title_name='Show S02E08'),
            track(1, 1500, 1, title_name='Show S02E07'),
        ]
        ordered = rip_station.order_episode_tracks(tracks)
        self.assertEqual([item['id'] for item in ordered], [1, 2])
        self.assertEqual(rip_station.extract_episode_info(tracks[0]), (2, 8))

    def test_common_underscore_and_1x_episode_markers(self):
        self.assertEqual(
            rip_station.extract_episode_info({'title_name': 'Show_S03E09_title'}),
            (3, 9),
        )
        self.assertEqual(
            rip_station.extract_episode_info({'title_name': 'Show - 2x04'}),
            (2, 4),
        )

    def test_equal_size_explicit_different_episodes_are_not_duplicates(self):
        tracks = [
            track(1, 1500, 100, title_name='Show S01E01'),
            track(2, 1500, 100, title_name='Show S01E02'),
        ]
        analysis = rip_station.analyze_series_tracks(tracks)
        self.assertEqual(analysis.duplicate_of, {})
        self.assertEqual(analysis.recommended_ids, [1, 2])

    def test_same_episode_number_in_different_seasons_is_not_a_duplicate(self):
        tracks = [
            track(1, 1500, 100, title_name='Show S01E01'),
            track(2, 1500, 100, title_name='Show S02E01'),
        ]
        analysis = rip_station.analyze_series_tracks(tracks)
        self.assertEqual(analysis.duplicate_of, {})

    def test_mixed_seasons_recommend_only_one_season(self):
        tracks = [
            track(0, 1500, 100, title_name='Show S01E01'),
            track(1, 1500, 101, title_name='Show S02E02'),
        ]
        analysis = rip_station.analyze_series_tracks(tracks)
        self.assertEqual(analysis.recommended_ids, [0])
        self.assertEqual(analysis.confidence, 'niedrig')

    def test_explicit_numbers_beat_different_runtimes(self):
        tracks = [
            track(1, 1500, 100, title_name='Show S01E01'),
            track(2, 3000, 200, title_name='Show S01E02'),
            track(3, 600, 50),
        ]
        analysis = rip_station.analyze_series_tracks(tracks)
        self.assertEqual(analysis.recommended_ids, [1, 2])

    def test_incomplete_markers_fall_back_to_track_id(self):
        tracks = [
            track(7, 1500, 7, title_name='Show S01E01'),
            track(3, 1500, 3),
        ]
        self.assertEqual([t['id'] for t in rip_station.order_episode_tracks(tracks)], [3, 7])

    def test_generic_markers_follow_episode_from_single_known_season(self):
        tracks = [
            track(2, 1500, 2, title_name='Episode 6'),
            track(5, 1500, 3, title_name='Episode 7'),
            track(8, 1500, 1, title_name='Show S03E05'),
        ]
        ordered = rip_station.order_episode_tracks(tracks)
        self.assertEqual([item['id'] for item in ordered], [8, 2, 5])


class JobTests(unittest.TestCase):
    def setUp(self):
        with rip_station.job_state_lock:
            rip_station.reserved_outputs.clear()

    def tearDown(self):
        with rip_station.job_state_lock:
            rip_station.reserved_outputs.clear()

    def test_custom_episode_numbers_and_portable_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(rip_station, 'BASE_OUTPUT_DIR', temp_dir):
                jobs = rip_station.build_series_jobs(
                    'A/B: Show', 2,
                    [track(3, 1500, 3), track(4, 1500, 4)],
                    [5, 8],
                )
        self.assertEqual(os.path.basename(jobs[0][1]), 'A_B_ Show.S02E05.mkv')
        self.assertEqual(os.path.basename(jobs[1][1]), 'A_B_ Show.S02E08.mkv')

    def test_track_and_episode_counts_must_match(self):
        with self.assertRaises(ValueError):
            rip_station.build_series_jobs('Show', 1, [track(1, 1500, 1)], [1, 2])

    def test_next_episode_continues_existing_season(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            season_dir = os.path.join(temp_dir, 'Show', 'Season 01')
            os.makedirs(season_dir)
            Path(season_dir, 'Show.S01E03.mkv').touch()
            Path(season_dir, 'Show.S01E07.mkv').touch()
            with patch.object(rip_station, 'BASE_OUTPUT_DIR', temp_dir):
                self.assertEqual(rip_station.next_episode_number('Show', 1), 8)

    def test_season_zero_is_supported(self):
        jobs = rip_station.build_series_jobs('Show', 0, [track(1, 1500, 1)], [1])
        self.assertEqual(os.path.basename(jobs[0][1]), 'Show.S00E01.mkv')

    def test_prompt_defaults_to_next_existing_episode(self):
        tracks = [track(1, 1500, 1), track(2, 1530, 2)]
        with tempfile.TemporaryDirectory() as temp_dir:
            season_dir = os.path.join(temp_dir, 'Show', 'Season 01')
            os.makedirs(season_dir)
            Path(season_dir, 'Show.S01E04.mkv').touch()
            answers = iter(['', '', 'Show', '', ''])
            with patch.object(rip_station, 'BASE_OUTPUT_DIR', temp_dir), \
                    patch('builtins.input', side_effect=lambda _='': next(answers)), \
                    redirect_stdout(io.StringIO()):
                jobs = rip_station.prompt_series_jobs(tracks)
        self.assertEqual([os.path.basename(job[1]) for job in jobs], [
            'Show.S01E05.mkv',
            'Show.S01E06.mkv',
        ])

    def test_prompt_rejects_tracks_from_multiple_seasons(self):
        tracks = [
            track(0, 1500, 100, title_name='Show S01E01'),
            track(1, 1500, 101, title_name='Show S02E02'),
        ]
        answers = iter(['*', ''])
        output = io.StringIO()
        with patch('builtins.input', side_effect=lambda _='': next(answers)), \
                redirect_stdout(output):
            jobs = rip_station.prompt_series_jobs(tracks)
        self.assertEqual(jobs, [])
        self.assertIn('mehreren Staffeln (1, 2)', output.getvalue())

    def test_worker_reports_unexpected_setup_error(self):
        drive = rip_station.Drive(0, '/dev/test', 'disc', 'drive')
        with patch('rip_station.os.makedirs', side_effect=OSError('read only')), \
                redirect_stdout(io.StringIO()):
            rip_station.rip_jobs_worker(drive, [(1, '/video/Show.S01E01.mkv', 'title.mkv')])
        self.assertEqual(drive.status, 'ERROR (Worker)')
        self.assertEqual(drive.progress, 0)

    def test_windows_reserved_names_are_prefixed(self):
        for value in ('CON', 'prn', 'AUX.txt', 'NUL', 'COM1', 'lpt9'):
            with self.subTest(value=value):
                self.assertTrue(rip_station.safe_media_name(value).startswith('_'))
        self.assertEqual(rip_station.safe_media_name('COM10'), 'COM10')

    def test_find_created_mkv_ignores_empty_name_and_stale_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stale = Path(temp_dir, 'stale.mkv')
            stale.touch()
            files_before = {stale}
            created = Path(temp_dir, 'new.mkv')
            created.touch()
            self.assertEqual(
                rip_station.find_created_mkv(temp_dir, '', files_before),
                str(created),
            )

    def test_find_created_mkv_fails_without_a_new_regular_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stale = Path(temp_dir, 'title.mkv')
            stale.touch()
            with self.assertRaises(FileNotFoundError):
                rip_station.find_created_mkv(temp_dir, 'title.mkv', {stale})

    def test_worker_uses_fixed_source_and_isolated_staging_directory(self):
        drive = rip_station.Drive(9, '/dev/sr9', 'DISC', 'Drive')
        commands = []

        class SuccessfulProcess:
            stdout = []
            returncode = 0

            def wait(self):
                return 0

        def start_process(command, **_kwargs):
            commands.append(command)
            Path(command[-1], 'generated.mkv').touch()
            return SuccessfulProcess()

        with tempfile.TemporaryDirectory() as temp_dir:
            final_output = Path(temp_dir, 'Movie.mkv')
            jobs = [(1, str(final_output), '')]
            with patch('rip_station.subprocess.Popen', side_effect=start_process), \
                    patch('rip_station.subprocess.run', return_value=SimpleNamespace(
                        returncode=0, stderr=''
                    )), patch('rip_station.eject_drive'):
                rip_station._rip_jobs_worker(drive, jobs, 'disc:9')

            self.assertTrue(final_output.is_file())
            self.assertEqual(commands[0][4], 'disc:9')
            self.assertNotEqual(commands[0][-1], temp_dir)
            self.assertFalse(list(Path(temp_dir).glob('.ripstation-*')))
            self.assertEqual(drive.status, 'COMPLETED')

    def test_worker_terminates_and_kills_hung_process_before_release(self):
        drive = rip_station.Drive(5, '/dev/sr5', 'DISC', 'Drive')

        class BrokenOutput:
            def __iter__(self):
                raise OSError('stdout failed')

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
                    raise subprocess.TimeoutExpired('makemkvcon', timeout)
                self.returncode = -9
                return self.returncode

            def kill(self):
                self.killed = True

        process = HungProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, 'Movie.mkv')
            jobs = [(1, output, 'title.mkv')]
            drive.busy = True
            with rip_station.job_state_lock:
                rip_station.reserved_outputs.add(rip_station.canonical_output_path(output))
            with patch('rip_station.subprocess.Popen', return_value=process), \
                    redirect_stdout(io.StringIO()):
                rip_station.rip_jobs_worker(
                    drive, jobs, 'dev:/dev/sr5', owns_job_state=True
                )
            self.assertFalse(list(Path(temp_dir).glob('.ripstation-*')))

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertFalse(drive.busy)
        self.assertFalse(rip_station.reserved_outputs)
        self.assertEqual(drive.status, 'ERROR (Worker)')

    def test_metadata_timeout_is_only_a_warning(self):
        drive = rip_station.Drive(6, '/dev/sr6', 'DISC', 'Drive')

        class SuccessfulProcess:
            stdout = []
            returncode = 0

            def wait(self):
                return 0

        def start_process(command, **_kwargs):
            Path(command[-1], 'generated.mkv').touch()
            return SuccessfulProcess()

        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = [(1, os.path.join(temp_dir, 'Movie.mkv'), '')]
            timeout = subprocess.TimeoutExpired('mkvpropedit', 120)
            with patch('rip_station.subprocess.Popen', side_effect=start_process), \
                    patch('rip_station.subprocess.run', side_effect=timeout) as run, \
                    patch('rip_station.eject_drive'):
                rip_station._rip_jobs_worker(drive, jobs, 'dev:/dev/sr6')
        self.assertEqual(drive.status, 'COMPLETED (META WARN)')
        self.assertEqual(run.call_args.kwargs['timeout'], 120)

    def test_eject_has_timeout(self):
        with patch('rip_station.subprocess.run') as run, redirect_stdout(io.StringIO()):
            rip_station.eject_drive('/dev/sr0')
        self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_rescan_does_not_mutate_busy_drive(self):
        drive = rip_station.Drive(2, '/dev/sr2', 'OLD_DISC', 'Old drive')
        drive.busy = True
        drive.status = 'Ripping (1/2)'
        original_drives = rip_station.drives
        rip_station.drives = [drive]
        try:
            with patch('rip_station.fetch_drive_info', return_value=[
                    (12, 'New drive', 'NEW_DISC', '/dev/sr2')
                 ]), patch('rip_station.time.sleep'), redirect_stdout(io.StringIO()):
                rip_station.rescan_idle_drives()
        finally:
            rip_station.drives = original_drives
        self.assertEqual((drive.mkv_id, drive.name, drive.label), (2, 'Old drive', 'OLD_DISC'))

    def test_start_reserves_outputs_and_keeps_source_stable(self):
        entered = rip_station.threading.Event()
        finish = rip_station.threading.Event()
        captured_sources = []
        first_drive = rip_station.Drive(4, '/dev/sr4', 'DISC_A', 'Drive A')
        second_drive = rip_station.Drive(7, '/dev/sr7', 'DISC_B', 'Drive B')

        with tempfile.TemporaryDirectory() as temp_dir:
            jobs = [(1, os.path.join(temp_dir, 'Show.S01E01.mkv'), 'title.mkv')]

            def blocking_worker(_drive, _jobs, disc_source):
                captured_sources.append(disc_source)
                entered.set()
                finish.wait(2)

            with patch('rip_station._rip_jobs_worker', side_effect=blocking_worker):
                thread, error = rip_station.start_rip_jobs(first_drive, jobs)
                try:
                    self.assertIsNone(error)
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(first_drive.busy)
                    self.assertIn(
                        rip_station.canonical_output_path(jobs[0][1]),
                        rip_station.reserved_outputs,
                    )

                    first_drive.mkv_id = 99
                    _, second_error = rip_station.start_rip_jobs(second_drive, jobs)
                    self.assertIn('reserviert', second_error)
                    self.assertFalse(second_drive.busy)
                    self.assertEqual(captured_sources, ['dev:/dev/sr4'])
                finally:
                    finish.set()
                    thread.join(2)

        self.assertFalse(first_drive.busy)
        self.assertFalse(rip_station.reserved_outputs)

    def test_failed_thread_start_releases_drive_and_outputs(self):
        drive = rip_station.Drive(1, '/dev/sr1', 'DISC', 'Drive')
        jobs = [(1, '/video/Movie/Movie.mkv', 'title.mkv')]
        with patch('rip_station.threading.Thread', side_effect=RuntimeError('no thread')):
            thread, error = rip_station.start_rip_jobs(drive, jobs)
        self.assertIsNone(thread)
        self.assertIn('nicht gestartet', error)
        self.assertFalse(drive.busy)
        self.assertFalse(rip_station.reserved_outputs)
        self.assertEqual(drive.status, 'ERROR (Start)')

    def test_existing_output_is_rejected_before_worker_start(self):
        drive = rip_station.Drive(1, '/dev/sr1', 'DISC', 'Drive')
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir, 'Movie.mkv')
            output.touch()
            thread, error = rip_station.start_rip_jobs(
                drive, [(1, str(output), 'title.mkv')]
            )
        self.assertIsNone(thread)
        self.assertIn('vorhanden', error)
        self.assertFalse(drive.busy)

    def test_reserved_episodes_are_included_in_next_number(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(rip_station, 'BASE_OUTPUT_DIR', temp_dir):
            jobs = rip_station.build_series_jobs(
                'Show', 1,
                [track(1, 1500, 1), track(2, 1500, 2)],
                [1, 2],
            )
            with rip_station.job_state_lock:
                rip_station.reserved_outputs.update(
                    rip_station.canonical_output_path(job[1]) for job in jobs
                )
            self.assertEqual(rip_station.next_episode_number('Show', 1), 3)

    def test_movie_prompt_can_override_longest_title(self):
        tracks = [track(1, 4000, 1), track(2, 5000, 2)]
        answers = iter(['1', 'Movie'])
        with patch('builtins.input', side_effect=lambda _='': next(answers)), \
                redirect_stdout(io.StringIO()):
            jobs = rip_station.prompt_movie_jobs(tracks)
        self.assertEqual(jobs[0][0], 1)
        self.assertEqual(os.path.basename(jobs[0][1]), 'Movie.mkv')


if __name__ == '__main__':
    unittest.main()
