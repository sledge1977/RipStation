import os
import unittest

from ripstation.config import parse_arguments


class ConfigTests(unittest.TestCase):
    def test_parse_arguments(self):
        cfg = parse_arguments(
            ["-o", "/custom/videos", "--no-eject", "--min-movie-len", "45"]
        )
        self.assertEqual(cfg.base_output_dir, os.path.abspath("/custom/videos"))
        self.assertFalse(cfg.auto_eject)
        self.assertEqual(cfg.movie_min_seconds, 45 * 60)

    def test_explicit_tool_paths_win(self):
        cfg = parse_arguments(["--makemkv", "/opt/mk", "--mkvpropedit", "/opt/pe"])
        self.assertEqual((cfg.makemkv_cmd, cfg.mkvpropedit_cmd), ("/opt/mk", "/opt/pe"))


if __name__ == "__main__":
    unittest.main()
