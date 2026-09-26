import os
import tempfile
import unittest
from pathlib import Path

from ripstation import analyzer
from tests.helpers import track


class ParsingTests(unittest.TestCase):
    def test_tinfo_csv_values_may_contain_commas(self):
        output = "\n".join(
            [
                'TINFO:0,2,0,"Pilot, Part One"',
                'TINFO:0,9,0,"0:42:03"',
                'TINFO:0,10,0,"4.2 GB"',
                'TINFO:0,11,0,"4200000000"',
                'TINFO:0,16,0,"00001.mpls"',
                'TINFO:0,27,0,"title_t00.mkv"',
            ]
        )
        parsed = analyzer.parse_tinfo_output(output)
        self.assertEqual(parsed[0].title_name, "Pilot, Part One")
        self.assertEqual(parsed[0].duration_seconds, 2523)
        self.assertEqual(parsed[0].size_bytes, 4200000000)
        self.assertEqual(parsed[0].source_filename, "00001.mpls")
        self.assertEqual(parsed[0].output_filename, "title_t00.mkv")

    def test_tinfo_ignores_malformed_values(self):
        output = 'TINFO:1,11,0,"many"\nTINFO:x,2,0,"bad"\nTINFO:1,2,0,"Ok"'
        parsed = analyzer.parse_tinfo_output(output)
        self.assertEqual(
            [(t.id, t.title_name, t.size_bytes) for t in parsed], [(1, "Ok", 0)]
        )

    def test_selection_preserves_manual_and_reverse_order(self):
        self.assertEqual(analyzer.parse_track_selection("5, 3-1, 5"), [5, 3, 2, 1])

    def test_range_formatting(self):
        self.assertEqual(analyzer.format_number_ranges([1, 2, 3, 6, 8, 7]), "1-3,6,8-7")

    def test_windows_reserved_names_are_prefixed(self):
        for value in ("CON", "prn", "AUX.txt", "NUL", "COM1", "lpt9"):
            with self.subTest(value=value):
                self.assertTrue(analyzer.safe_media_name(value).startswith("_"))
        self.assertEqual(analyzer.safe_media_name("COM10"), "COM10")

    def test_clean_label_for_suggestion(self):
        self.assertEqual(
            analyzer.clean_label_for_suggestion("THE_MATRIX_1999"), "The Matrix 1999"
        )
        self.assertEqual(
            analyzer.clean_label_for_suggestion("BREAKING_BAD_S01D02"), "Breaking Bad"
        )
        self.assertEqual(analyzer.clean_label_for_suggestion("DISC_1"), "")
        self.assertEqual(analyzer.clean_label_for_suggestion("DVD_VIDEO"), "")
        self.assertEqual(analyzer.clean_label_for_suggestion(""), "")


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
        analysis = analyzer.analyze_series_tracks(tracks)
        self.assertEqual(analysis.recommended_ids, [0, 1, 2])
        self.assertEqual(analysis.duplicate_of, {3: 1})
        self.assertEqual(analysis.confidence, "hoch")

    def test_explicit_episode_markers_determine_order(self):
        tracks = [
            track(2, 1500, 2, title_name="Show S02E08"),
            track(1, 1500, 1, title_name="Show S02E07"),
        ]
        ordered = analyzer.order_episode_tracks(tracks)
        self.assertEqual([item.id for item in ordered], [1, 2])
        self.assertEqual(analyzer.extract_episode_info(tracks[0]), (2, 8))

    def test_common_underscore_and_1x_episode_markers(self):
        self.assertEqual(
            analyzer.extract_episode_info(
                track(0, 1, 1, title_name="Show_S03E09_title")
            ),
            (3, 9),
        )
        self.assertEqual(
            analyzer.extract_episode_info(track(0, 1, 1, title_name="Show - 2x04")),
            (2, 4),
        )

    def test_equal_size_explicit_different_episodes_are_not_duplicates(self):
        tracks = [
            track(1, 1500, 100, title_name="Show S01E01"),
            track(2, 1500, 100, title_name="Show S01E02"),
        ]
        analysis = analyzer.analyze_series_tracks(tracks)
        self.assertEqual(analysis.duplicate_of, {})
        self.assertEqual(analysis.recommended_ids, [1, 2])

    def test_same_episode_number_in_different_seasons_is_not_a_duplicate(self):
        tracks = [
            track(1, 1500, 100, title_name="Show S01E01"),
            track(2, 1500, 100, title_name="Show S02E01"),
        ]
        analysis = analyzer.analyze_series_tracks(tracks)
        self.assertEqual(analysis.duplicate_of, {})

    def test_mixed_seasons_recommend_only_one_season(self):
        tracks = [
            track(0, 1500, 100, title_name="Show S01E01"),
            track(1, 1500, 101, title_name="Show S02E02"),
        ]
        analysis = analyzer.analyze_series_tracks(tracks)
        self.assertEqual(analysis.recommended_ids, [0])
        self.assertEqual(analysis.confidence, "niedrig")

    def test_explicit_numbers_beat_different_runtimes(self):
        tracks = [
            track(1, 1500, 100, title_name="Show S01E01"),
            track(2, 3000, 200, title_name="Show S01E02"),
            track(3, 600, 50),
        ]
        analysis = analyzer.analyze_series_tracks(tracks)
        self.assertEqual(analysis.recommended_ids, [1, 2])

    def test_incomplete_markers_fall_back_to_track_id(self):
        tracks = [track(7, 1500, 7, title_name="Show S01E01"), track(3, 1500, 3)]
        self.assertEqual([t.id for t in analyzer.order_episode_tracks(tracks)], [3, 7])

    def test_generic_markers_follow_episode_from_single_known_season(self):
        tracks = [
            track(2, 1500, 2, title_name="Episode 6"),
            track(5, 1500, 3, title_name="Episode 7"),
            track(8, 1500, 1, title_name="Show S03E05"),
        ]
        ordered = analyzer.order_episode_tracks(tracks)
        self.assertEqual([item.id for item in ordered], [8, 2, 5])


class EpisodeNumberingTests(unittest.TestCase):
    def test_custom_episode_numbers_and_portable_name(self):
        jobs = analyzer.build_series_jobs(
            "A/B: Show", 2, [track(3, 1500, 3), track(4, 1500, 4)], [5, 8], "/video"
        )
        self.assertEqual(os.path.basename(jobs[0].output_path), "A_B_ Show.S02E05.mkv")
        self.assertEqual(os.path.basename(jobs[1].output_path), "A_B_ Show.S02E08.mkv")
        self.assertEqual(jobs[0].source_filename, "title_t03.mkv")

    def test_track_and_episode_counts_must_match(self):
        with self.assertRaises(ValueError):
            analyzer.build_series_jobs("Show", 1, [track(1, 1500, 1)], [1, 2], "/video")

    def test_season_zero_is_supported(self):
        jobs = analyzer.build_series_jobs("Show", 0, [track(1, 1500, 1)], [1], "/video")
        self.assertEqual(os.path.basename(jobs[0].output_path), "Show.S00E01.mkv")

    def test_next_episode_continues_existing_season(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            season_dir = os.path.join(temp_dir, "Show", "Season 01")
            os.makedirs(season_dir)
            Path(season_dir, "Show.S01E03.mkv").touch()
            Path(season_dir, "Show.S01E07.mkv").touch()
            self.assertEqual(analyzer.next_episode_number("Show", 1, temp_dir), 8)

    def test_next_episode_detects_various_separators(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            season_dir = os.path.join(temp_dir, "Show", "Season 01")
            os.makedirs(season_dir)
            Path(season_dir, "Show - S01E02.mkv").touch()
            Path(season_dir, "Show S01E05.mkv").touch()
            Path(season_dir, "Show_S01E06.mkv").touch()
            self.assertEqual(analyzer.next_episode_number("Show", 1, temp_dir), 7)


if __name__ == "__main__":
    unittest.main()
