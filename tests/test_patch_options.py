import unittest

from build import build_patch_option_args, parse_patches


class PatchOptionTests(unittest.TestCase):
    def test_cli_patch_metadata_preserves_typed_option_fields(self):
        patches = parse_patches("""Index: 0
Name: Theme
Description: Pick a theme
Enabled: true
Options:
- Key: theme
- Title: Theme
- Description: Select a theme
- Type: kotlin.String
- Required: true
- Default: SYSTEM
- Values: {\"System\":\"SYSTEM\",\"Dark\":\"DARK\"}
""")

        option = patches[0]["options"][0]
        self.assertEqual(option["type"], "kotlin.String")
        self.assertTrue(option["required"])
        self.assertEqual(option["default"], "SYSTEM")
        self.assertEqual(option["values"], {"System": "SYSTEM", "Dark": "DARK"})

    def test_saved_options_are_passed_in_morphe_cli_format(self):
        self.assertEqual(
            build_patch_option_args({
                "Theme": {"theme": "DARK", "enabled": True, "ignored": None},
                "Installer": {"name": "com.android.vending"},
                "invalid": "not-an-option-map",
            }),
            [
                "-O", "Theme:theme=DARK",
                "-O", "Theme:enabled=true",
                "-O", "Installer:name=com.android.vending",
            ],
        )


if __name__ == "__main__":
    unittest.main()
