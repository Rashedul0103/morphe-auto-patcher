import json
import unittest
from pathlib import Path

from apk_providers import (
    DEFAULT_PROVIDER_URLS,
    SUPPORTED_ARTIFACT_TYPES,
    provider_order,
    morphe_manual_search_url,
)

ROOT = Path(__file__).resolve().parents[1]


class PreflightTests(unittest.TestCase):
    def test_fresh_config_has_no_bundled_apps_or_patch_sources(self):
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config.get("apps"), [])
        self.assertEqual(config.get("sources"), [])
        self.assertFalse((ROOT / "patches-1.39.1.mpp").exists())

        for info_path in (ROOT / "docs" / "catalog").glob("*.info.json"):
            info = json.loads(info_path.read_text(encoding="utf-8"))
            self.assertNotIn("patch_source", info)
            self.assertNotIn("patch_filter", info)
            self.assertNotIn("patch_source_candidates", info)

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
        self.assertIn("github.event_name == 'push' && contains(github.event.head_commit.message, '[e2e]')", workflow)
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
        self.assertIn("upload: vApp", ui)
        self.assertIn("function openUploadApkDialog(id)", ui)
        self.assertIn("function uploadStockApk(id, file)", ui)
        self.assertIn("Verify & Start Patch", ui)

    def test_morphe_manual_fallback_search_is_exact_version_and_catalog_scoped(self):
        url = morphe_manual_search_url(
            "com.example.sample",
            "4.2.1",
            "auto",
        )
        self.assertTrue(url.startswith("https://google.com/search?q="))
        from urllib.parse import unquote_plus
        query = unquote_plus(url.split("q=", 1)[1])
        self.assertIn('"com.example.sample"', query)
        self.assertIn('"4.2.1"', query)
        self.assertIn("site:apkmirror.com", query)
        self.assertIn("site:uptodown.com", query)
        self.assertIn("site:apkpure.com", query)
        self.assertIn("site:apkcombo.com", query)

    def test_discord_link_label_distinguishes_search_from_provider_page(self):
        build = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn("Search for exact app version", build)
        self.assertIn("Open version download page", build)
        self.assertIn('manual_host == "google.com"', build)

    def test_discord_manual_recovery_links_to_upload_flow(self):
        build = (ROOT / "build.py").read_text(encoding="utf-8")
        self.assertIn("PATCH_MANAGER_URL", build)
        self.assertIn("#/upload/", build)
        self.assertIn("Upload through Patch Manager", build)
        self.assertIn("GitHub Release fallback", build)

# Controlled E2E trigger marker: commits tagged [e2e] run the full build.

if __name__ == "__main__":
    unittest.main()
