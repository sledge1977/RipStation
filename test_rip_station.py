import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
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


class JobTests(unittest.TestCase):
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

    def test_worker_reports_unexpected_setup_error(self):
        drive = rip_station.Drive(0, '/dev/test', 'disc', 'drive')
        with patch('rip_station.os.makedirs', side_effect=OSError('read only')), \
                redirect_stdout(io.StringIO()):
            rip_station.rip_jobs_worker(drive, [(1, '/video/Show.S01E01.mkv', 'title.mkv')])
        self.assertEqual(drive.status, 'ERROR (Worker)')
        self.assertEqual(drive.progress, 0)


if __name__ == '__main__':
    unittest.main()
