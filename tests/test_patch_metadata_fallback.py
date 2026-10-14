import unittest
from unittest.mock import Mock, patch

from build import load_patch_metadata_fallback


class PatchMetadataFallbackTests(unittest.TestCase):
    def test_supports_current_package_to_versions_map(self):
        payload = {
            "appNames": {"com.facebook.katana": "Facebook"},
            "patches": [
                {
                    "name": "AMOLED black theme",
                    "description": "Use a dark theme",
                    "use": True,
                    "compatiblePackages": {
                        "com.facebook.katana": ["582.0.0.50.54"]
                    },
                    "options": [],
                },
                {
                    "name": "Unrelated patch",
                    "compatiblePackages": {
                        "com.example.other": ["1.0.0"]
                    },
                },
            ],
        }
        not_found = Mock(status_code=404)
        found = Mock(status_code=200)
        found.json.return_value = payload

        with patch("build.requests.get", side_effect=[not_found, not_found, found]):
            result = load_patch_metadata_fallback(
                "SysAdminDoc/Hushfacebook",
                "v0.12.0",
                ["com.facebook.katana", "Facebook"],
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["filter"], "com.facebook.katana")
        self.assertEqual(result["versions"], ["582.0.0.50.54"])
        self.assertEqual([p["name"] for p in result["patches"]], ["AMOLED black theme"])
        self.assertTrue(result["patches"][0]["enabled"])

    def test_keeps_support_for_legacy_package_object_list(self):
        payload = {
            "patches": [
                {
                    "name": "Legacy patch",
                    "default": True,
                    "compatiblePackages": [
                        {
                            "packageName": "com.example.app",
                            "name": "Example",
                            "targets": [{"version": "2.3.4"}],
                        }
                    ],
                }
            ]
        }
        responses = [Mock(status_code=404), Mock(status_code=404), Mock(status_code=200)]
        responses[-1].json.return_value = payload

        with patch("build.requests.get", side_effect=responses):
            result = load_patch_metadata_fallback(
                "owner/repo", "v1.0.0", ["com.example.app"]
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["versions"], ["2.3.4"])
        self.assertEqual(result["patches"][0]["name"], "Legacy patch")


if __name__ == "__main__":
    unittest.main()
