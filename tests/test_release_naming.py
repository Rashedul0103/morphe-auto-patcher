import unittest

from build import compare_version_numbers, patched_apk_filename


class PatchedApkFilenameTests(unittest.TestCase):
    def test_filename_includes_patch_and_verified_app_versions(self):
        self.assertEqual(
            patched_apk_filename("yt", "v3.4.1", "21.16.2"),
            "yt-patch-v3.4.1_app-21.16.2.apk",
        )

    def test_filename_sanitizes_unsafe_components(self):
        self.assertEqual(
            patched_apk_filename("../yt", "v3/4", "21:16?2"),
            "yt-patch-v3-4_app-21-16-2.apk",
        )

    def test_version_comparison_detects_newer_supported_app(self):
        self.assertEqual(compare_version_numbers("21.17.0", "21.16.2"), 1)
        self.assertEqual(compare_version_numbers("21.16", "21.16.0"), 0)
        self.assertIsNone(compare_version_numbers("Auto", "21.16.2"))


if __name__ == "__main__":
    unittest.main()
