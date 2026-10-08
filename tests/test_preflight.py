import json
import unittest
from pathlib import Path

from apk_providers import (
    DEFAULT_PROVIDER_URLS,
    SUPPORTED_ARTIFACT_TYPES,
    provider_order,
)

ROOT = Path(__file__).resolve().parents[1]


class PreflightTests(unittest.TestCase):
    def test_config_apps_have_generic_acquisition_fields(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        apps = config.get("apps", [])
        self.assertGreater(len(apps), 0)

        seen_ids = set()
        seen_packages = set()
        for app in apps:
            app_id = str(app.get("id") or "").strip()
            package = str(app.get("android_package") or "").strip()
            self.assertRegex(app_id, r"^[a-z0-9][a-z0-9_-]*$")
            self.assertRegex(
                package,
                r"^[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+$",
            )
            self.assertNotIn(app_id, seen_ids)
            self.assertNotIn(package, seen_packages)
            seen_ids.add(app_id)
            seen_packages.add(package)

            providers = app.get("apk_providers")
            self.assertIsInstance(providers, list)
            self.assertGreater(len(providers), 0)
            self.assertEqual(
                len(providers),
                len({str(provider).lower() for provider in providers}),
            )

    def test_provider_order_is_deterministic_and_deduplicated(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        for app in config["apps"]:
            order = provider_order(app)
            self.assertEqual(order, list(dict.fromkeys(order)))
            for provider in app["apk_providers"]:
                self.assertIn(str(provider).lower(), order)
            self.assertIn("apkmirror", order)
            self.assertIn("uptodown", order)

    def test_artifact_types_cover_all_supported_android_artifacts(self):
        self.assertEqual(
            SUPPORTED_ARTIFACT_TYPES,
            {"apk", "apkm", "apks", "xapk"},
        )

    def test_no_package_specific_provider_defaults(self):
        self.assertEqual(DEFAULT_PROVIDER_URLS, {})

    def test_build_workflow_has_fast_test_gate_and_manual_build(self):
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("jobs:", workflow)
        self.assertIn("test:", workflow)
        self.assertIn("build:", workflow)
        self.assertIn("needs: test", workflow)
        self.assertIn("github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'", workflow)
        self.assertIn("python3 -m unittest discover", workflow)

    def test_build_script_has_cli_and_target_controls(self):
        build = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn("def find_cli_jar()", build)
        self.assertIn("TARGET_APP", build)
        self.assertIn("FORCE_BUILD", build)
        self.assertIn("GITHUB_EVENT_NAME", build)
        self.assertIn("Build finished with errors or missing stock APKs.", build)

    def test_ui_accepts_all_supported_artifact_extensions(self):
        ui = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".apk,.apkm,.apks,.xapk", ui)
        self.assertNotIn("const PK_REGISTRY", ui)
        self.assertIn("function getAppMeta(pkg)", ui)


if __name__ == "__main__":
    unittest.main()
