import unittest
from unittest.mock import patch

from build import find_cli_jar, _morphe_cli_jar_version_key


class MorpheCliJarSelectionTests(unittest.TestCase):
    def test_stable_latest_jar_wins_over_tracked_old_dev_jar(self):
        stale = "morphe-desktop-1.17.1-dev.1-all.jar"
        current = "morphe-desktop-1.18.1-all.jar"

        with patch("build.glob.glob", return_value=[stale, current]):
            self.assertEqual(find_cli_jar(), current)

    def test_stable_release_wins_over_prerelease_of_same_version(self):
        stable = "morphe-desktop-1.18.1-all.jar"
        prerelease = "morphe-desktop-1.18.1-rc.1-all.jar"

        self.assertGreater(
            _morphe_cli_jar_version_key(stable),
            _morphe_cli_jar_version_key(prerelease),
        )


if __name__ == "__main__":
    unittest.main()
