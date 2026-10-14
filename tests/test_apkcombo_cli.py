import unittest
from unittest.mock import patch

from apk_providers import _apkcombo_command


class APKComboCommandTests(unittest.TestCase):
    def test_javascript_cli_is_run_through_bun(self):
        bun_path = "/home/runner/.bun/bin/bun"
        cli_path = "/home/runner/.bun/bin/apkmd"
        args = ["download", "youtube", "com.google.android.youtube", "--type", "apk"]

        with patch("apk_providers.shutil.which", return_value=bun_path):
            command = _apkcombo_command(cli_path, args)

        self.assertEqual(command, [bun_path, cli_path, *args])

    def test_bun_on_path_is_used_when_which_finds_it(self):
        with patch("apk_providers.shutil.which", return_value=None):
            self.assertEqual(
                _apkcombo_command("/tmp/apkmd", ["--help"]),
                ["bun", "/tmp/apkmd", "--help"],
            )


if __name__ == "__main__":
    unittest.main()
