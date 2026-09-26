import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ripstation import prompts
from ripstation.analyzer import canonical_output_path
from tests.helpers import make_station, track


def answer(*values):
    answers = iter(values)
    return patch("builtins.input", side_effect=lambda _="": next(answers))


class SeriesPromptTests(unittest.TestCase):
    def test_prompt_defaults_to_next_existing_episode(self):
        tracks = [track(1, 1500, 1), track(2, 1530, 2)]
        with tempfile.TemporaryDirectory() as temp_dir:
            season_dir = os.path.join(temp_dir, "Show", "Season 01")
            os.makedirs(season_dir)
            Path(season_dir, "Show.S01E04.mkv").touch()
            with answer("", "", "Show", "", ""), redirect_stdout(io.StringIO()):
                jobs = prompts.prompt_series_jobs(tracks, make_station(temp_dir))
        self.assertEqual(
            [os.path.basename(job.output_path) for job in jobs],
            ["Show.S01E05.mkv", "Show.S01E06.mkv"],
        )

    def test_prompt_skips_reserved_episode_numbers(self):
        tracks = [track(1, 1500, 1), track(2, 1530, 2)]
        with tempfile.TemporaryDirectory() as temp_dir:
            station = make_station(temp_dir)
            reserved = Path(temp_dir, "Show", "Season 01", "Show.S01E04.mkv")
            station.reserved_outputs.add(canonical_output_path(reserved))
            with answer("", "", "Show", "", ""), redirect_stdout(io.StringIO()):
                jobs = prompts.prompt_series_jobs(tracks, station)
        self.assertEqual(
            [Path(job.output_path).name for job in jobs],
            ["Show.S01E05.mkv", "Show.S01E06.mkv"],
        )

    def test_prompt_rejects_tracks_from_multiple_seasons(self):
        tracks = [
            track(0, 1500, 100, title_name="Show S01E01"),
            track(1, 1500, 101, title_name="Show S02E02"),
        ]
        output = io.StringIO()
        with answer("*", ""), redirect_stdout(output):
            jobs = prompts.prompt_series_jobs(tracks, make_station())
        self.assertEqual(jobs, [])
        self.assertIn("mehreren Staffeln (1, 2)", output.getvalue())

    def test_prompt_series_jobs_allows_cancel(self):
        with patch("builtins.input", return_value="q"), redirect_stdout(io.StringIO()):
            self.assertEqual(
                prompts.prompt_series_jobs([track(1, 1500, 1)], make_station()), []
            )

    def test_prompt_integer_allows_cancel(self):
        with patch("builtins.input", return_value="q"):
            self.assertIsNone(prompts.prompt_integer("Staffel", 1, allow_cancel=True))


class MoviePromptTests(unittest.TestCase):
    def test_movie_prompt_can_override_longest_title(self):
        tracks = [track(1, 4000, 1), track(2, 5000, 2)]
        with answer("1", "Movie"), redirect_stdout(io.StringIO()):
            jobs = prompts.prompt_movie_jobs(tracks, "/video")
        self.assertEqual(jobs[0].track_id, 1)
        self.assertEqual(
            jobs[0].output_path, os.path.join("/video", "Movie", "Movie.mkv")
        )
        self.assertEqual(jobs[0].source_filename, "title_t01.mkv")

    def test_prompt_movie_jobs_allows_cancel(self):
        with patch("builtins.input", return_value="q"), redirect_stdout(io.StringIO()):
            self.assertEqual(
                prompts.prompt_movie_jobs([track(1, 4000, 1)], "/video"), []
            )


if __name__ == "__main__":
    unittest.main()
