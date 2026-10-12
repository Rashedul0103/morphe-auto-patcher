import json
import unittest
from pathlib import Path

from apk_providers import (
    DEFAULT_PROVIDER_URLS,
    SUPPORTED_ARTIFACT_TYPES,
    provider_order,
    morphe_manual_search_url,
)
from build import _looks_like_android_package

ROOT = Path(__file__).resolve().parents[1]


class PreflightTests(unittest.TestCase):
    def test_fresh_config_fixture_has_no_bundled_apps_or_patch_sources(self):
        config = json.loads(
            (ROOT / "tests" / "fixtures" / "fresh_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config.get("apps"), [])
        self.assertEqual(config.get("sources"), [])
        self.assertFalse((ROOT / "patches-1.39.1.mpp").exists())


    def test_apkcombo_fallback_is_installed_and_identity_validated(self):
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        providers = (ROOT / "apk_providers.py").read_text(encoding="utf-8")
        self.assertIn("apkcombo-downloader@1.0.3", workflow)
        self.assertIn('shutil.which("apkmd")', providers)
        self.assertIn('"via": "apkcombo_cli"', providers)
        self.assertIn("expected_version=target_version", providers)
        self.assertIn('("apk", "bundle")', providers)

    def test_provider_order_is_deterministic_and_deduplicated(self):
        app = {"apk_providers": ["uptodown", "apkmirror", "uptodown"]}
        order = provider_order(app)
        self.assertEqual(order, ["uptodown", "apkmirror", "apkpure"])
        self.assertEqual(order, provider_order(app))
        self.assertEqual(provider_order({}), ["apkmirror", "uptodown", "apkpure"])

    def test_android_package_matcher_accepts_valid_package_ids(self):
        self.assertTrue(_looks_like_android_package("com.example.app"))
        self.assertTrue(_looks_like_android_package("org.example_2.client"))
        self.assertTrue(_looks_like_android_package(" com.Example.App "))

        for invalid in ("", "com", ".com.example", "com.example.", "https://example.com"):
            with self.subTest(value=invalid):
                self.assertFalse(_looks_like_android_package(invalid))

    def test_add_app_inputs_use_id_selectors(self):
        ui = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn("id=\"n_app_name\"", ui)
        self.assertIn("id=\"n_app_play\"", ui)
        self.assertIn("$('%s')" % "#n_app_name", ui)
        self.assertIn("$('%s')" % "#n_app_play", ui)
        self.assertNotIn("$('n_app_name')", ui)
        self.assertNotIn("$('n_app_play')", ui)

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
        self.assertIn("github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.diagnose_apkmirror != true)", workflow)
        self.assertIn("diagnose-apkmirror:", workflow)
        self.assertIn("playwright==1.63.0", workflow)
        self.assertIn("inputs.apkmirror_url", workflow)
        self.assertIn("python3 -m playwright install --with-deps chromium", workflow)
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
        self.assertIn("async function refreshPatchMetadataFromSources()", ui)
        self.assertIn("packageNames: discoveredNames", ui)
        self.assertIn("info.display_name", ui)
        self.assertIn("data.version || source.tag", ui)
        self.assertIn("xhr.ontimeout", ui)
        self.assertNotIn("xhr.setRequestHeader('X-GitHub-Api-Version'", ui)
        self.assertIn("bottom:calc(10px + env(safe-area-inset-bottom))", ui)
        self.assertIn(".apk,.apkm,.apks,.xapk", ui)
        self.assertNotIn("const PK_REGISTRY", ui)
        self.assertIn("function getAppMeta(pkg)", ui)
        self.assertIn("upload: vApp", ui)
        self.assertIn("function openUploadApkDialog(id)", ui)
        self.assertIn("function uploadStockApk(id, file)", ui)
        self.assertIn("Verify & Start Patch", ui)

    def test_apkmirror_playwright_is_an_isolated_non_bypass_diagnostic(self):
        script = (ROOT / "scripts" / "diagnose_apkmirror_playwright.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        self.assertIn("no challenge bypass", script)
        self.assertIn("CHALLENGE_MARKERS", script)
        self.assertIn("contains_android_manifest", script)
        self.assertIn("diagnose_apkmirror", workflow)
        self.assertIn("inputs.diagnose_apkmirror != true", workflow)
        self.assertIn("APKMIRROR_DIAGNOSTIC_URL", script)

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

    def test_patcher_ui_exposes_known_names_options_and_safe_footer(self):
        ui = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        for package, name in (
            ("com.google.android.inputmethod.latin", "Gboard"),
            ("com.facebook.katana", "Facebook"),
        ):
            self.assertIn(package, ui)
            self.assertIn(name, ui)
        self.assertIn("patch-options-button", ui)
        self.assertIn("Configure patch options", ui)
        self.assertIn("manager-patch-list-end", ui)
        self.assertIn("bottom:calc(10px + env(safe-area-inset-bottom))", ui)
        self.assertIn("upload_retry_file", ui)


# Controlled E2E trigger marker: commits tagged [e2e] run the full build.

if __name__ == "__main__":
    unittest.main()
