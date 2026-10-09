import os
import unittest
from unittest.mock import patch
from pathlib import Path

import build


ROOT = Path(__file__).resolve().parents[1]


class DiscordFailureAlertTests(unittest.TestCase):
    def test_all_failure_alerts_are_enabled_by_default(self):
        for stage in build.DISCORD_FAILURE_ALERTS:
            with self.subTest(stage=stage):
                self.assertTrue(build.discord_failure_alert_enabled({}, stage))

    def test_each_failure_alert_can_be_disabled_independently(self):
        settings = {
            "notify_patching_failure": False,
            "notify_output_validation_failure": True,
            "notify_release_publishing_failure": False,
        }
        self.assertFalse(build.discord_failure_alert_enabled(settings, "patching"))
        self.assertTrue(build.discord_failure_alert_enabled(settings, "output_validation"))
        self.assertFalse(build.discord_failure_alert_enabled(settings, "release_publishing"))

    def test_disabled_failure_alert_does_not_send_webhook(self):
        with patch("build.send_discord_webhook") as send:
            result = build.send_build_failure_alert(
                "https://discord.com/api/webhooks/test",
                {"notify_patching_failure": False},
                "patching",
                "youtube",
                "20.40.39",
                "arm64-v8a",
                "sample failure",
            )
        self.assertFalse(result)
        send.assert_not_called()

    def test_failure_alert_contains_diagnostics_and_workflow_link(self):
        env = {
            "GITHUB_REPOSITORY": "Rashedul0103/morphe-auto-patcher",
            "GITHUB_RUN_ID": "123456",
            "GITHUB_SERVER_URL": "https://github.com",
        }
        with patch.dict(os.environ, env), patch("build.send_discord_webhook") as send:
            result = build.send_build_failure_alert(
                "https://discord.com/api/webhooks/test",
                {},
                "output_validation",
                "youtube",
                "20.40.39",
                "arm64-v8a",
                "package mismatch",
            )
        self.assertTrue(result)
        send.assert_called_once()
        args, kwargs = send.call_args
        self.assertIn("Validation Failed", args[1])
        fields = {field["name"]: field["value"] for field in kwargs["fields"]}
        self.assertEqual(fields["Target Version"], "20.40.39")
        self.assertEqual(fields["Architecture"], "arm64-v8a")
        self.assertEqual(fields["Reason"], "package mismatch")
        self.assertIn("/actions/runs/123456", fields["Workflow Run"])

    def test_manual_link_prefers_exact_version_provider_page(self):
        urls = [
            "https://google.com/search?q=%22com.example.app%22+%224.2.1%22+site%3Aapkmirror.com",
            "https://www.apkmirror.com/apk/example/app/app-4-2-1-release/",
        ]
        chosen = build.select_manual_download_url(
            urls, "https://www.apkmirror.com/?s=Example+4.2.1",
            "com.example.app", "4.2.1"
        )
        self.assertEqual(chosen, urls[1])
        self.assertEqual(
            build.manual_download_link_label(chosen, "4.2.1"),
            "Open version download page",
        )

    def test_direct_artifact_link_is_preferred_over_search_pages(self):
        direct = "https://downloads.example.com/com.example.app-4.2.1.apk"
        search = "https://google.com/search?q=%22com.example.app%22+%224.2.1%22"
        self.assertEqual(
            build.select_manual_download_url(
                [search, direct], "", "com.example.app", "4.2.1"
            ),
            direct,
        )

    def test_ui_exposes_independent_failure_alert_toggles(self):
        ui = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        for key in (
            "notify_patching_failure",
            "notify_output_validation_failure",
            "notify_release_publishing_failure",
        ):
            with self.subTest(setting=key):
                self.assertIn(key, ui)
        self.assertIn('data-a="sw_discord_patching"', ui)
        self.assertIn('data-a="sw_discord_validation"', ui)
        self.assertIn('data-a="sw_discord_publishing"', ui)


if __name__ == "__main__":
    unittest.main()
