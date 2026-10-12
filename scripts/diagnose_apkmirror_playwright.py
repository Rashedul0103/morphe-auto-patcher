#!/usr/bin/env python3
"""Diagnose ordinary Chromium access to an APKMirror download page.

This is a diagnostic only. It does not attempt to solve CAPTCHAs, spoof browser
fingerprints, or bypass access controls. It reports whether normal browser
navigation and the site's own visible download flow work from GitHub Actions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


DEFAULT_URL = (
    "https://www.apkmirror.com/apk/google-inc/youtube-music/"
    "youtube-music-9-20-53-release/"
    "youtube-music-9-20-53-4-android-apk-download/"
)
CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-chl-",
    "__cf_chl_",
    "challenge-platform",
    "verify you are human",
    "unusual traffic",
)


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


async def inspect_download_controls(page) -> list[dict]:
    """Return a small list of visible, site-provided download controls."""
    try:
        return await page.locator("a[href], button").evaluate_all(
            """els => els.map((el, index) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const visible = style.display !== 'none' &&
                    style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                const href = el.href || '';
                const id = el.id || '';
                const cls = typeof el.className === 'string' ? el.className : '';
                const haystack = (text + ' ' + href + ' ' + id + ' ' + cls).toLowerCase();
                const score = (
                    (id.toLowerCase().includes('downloadbutton') ? 100 : 0) +
                    (haystack.includes('click here to start download') ? 80 : 0) +
                    (haystack.includes('download apk') ? 60 : 0) +
                    (haystack.includes('download') ? 20 : 0)
                );
                return {index, tag: el.tagName.toLowerCase(), text: text.slice(0, 180),
                    href, id, visible, score};
            }).filter(x => x.visible && x.score > 0 && x.href &&
                !x.href.startsWith('javascript:'))
              .sort((a, b) => b.score - a.score).slice(0, 12)"""
        )
    except Exception as exc:
        emit("controls_inspection_error", error=type(exc).__name__, detail=str(exc)[:240])
        return []


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("APKMIRROR_DIAGNOSTIC_URL", DEFAULT_URL))
    parser.add_argument("--expected-package", default="com.google.android.apps.youtube.music")
    parser.add_argument("--expected-version", default="9.20.53")
    parser.add_argument("--navigation-timeout", type=int, default=60000)
    args = parser.parse_args()

    parsed = urlparse(args.url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("apkmirror.com"):
        emit("invalid_input", message="URL must be HTTPS and hosted on apkmirror.com", url=args.url)
        return 2

    emit(
        "start",
        url=args.url,
        expected_package=args.expected_package,
        expected_version=args.expected_version,
        runner="GitHub Actions hosted Ubuntu",
        mode="ordinary headless Chromium; no challenge bypass",
    )

    with tempfile.TemporaryDirectory(prefix="apkmirror-playwright-") as temp_dir:
        download_dir = Path(temp_dir)
        downloads = []
        observed_errors = []
        all_pages = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True,
                downloads_path=str(download_dir),
                viewport={"width": 1365, "height": 900},
                locale="en-US",
                timezone_id="UTC",
            )

            def on_page(new_page):
                all_pages.append(new_page)
                new_page.on("download", lambda item: downloads.append((new_page, item)))

            context.on("page", on_page)
            page = await context.new_page()
            all_pages.append(page)
            page.on("download", lambda item: downloads.append((page, item)))
            page.on(
                "response",
                lambda response: observed_errors.append({
                    "status": response.status,
                    "url": response.url[:400],
                }) if response.status >= 400 and "apkmirror.com" in urlparse(response.url).netloc
                else None,
            )
            page.on(
                "requestfailed",
                lambda request: observed_errors.append({
                    "failed": request.failure,
                    "url": request.url[:400],
                }) if "apkmirror.com" in urlparse(request.url).netloc else None,
            )

            try:
                response = await page.goto(
                    args.url, wait_until="domcontentloaded",
                    timeout=args.navigation_timeout,
                )
                await page.wait_for_timeout(2500)
            except PlaywrightTimeoutError as exc:
                emit("navigation_timeout", detail=str(exc)[:300], url=page.url)
                await browser.close()
                return 3
            except Exception as exc:
                emit("navigation_error", error=type(exc).__name__, detail=str(exc)[:400], url=page.url)
                await browser.close()
                return 3

            status = response.status if response else None
            title = await page.title()
            body = (await page.locator("body").inner_text(timeout=5000))[:5000]
            lower_body = body.lower()
            challenged = any(marker in (title + " " + lower_body).lower() for marker in CHALLENGE_MARKERS)
            emit(
                "page_loaded",
                http_status=status,
                final_url=page.url,
                title=title[:250],
                body_excerpt=re.sub(r"\s+", " ", body)[:700],
                body_chars=len(body),
                challenge_detected=challenged,
                failed_or_blocked_requests=observed_errors[-15:],
            )

            if challenged:
                emit(
                    "blocked",
                    reason="A browser challenge or access-verification page was returned.",
                    next_step="Do not automate around the challenge; use an authorized download URL or manual upload.",
                )
                await browser.close()
                return 4
            if status is not None and status >= 400:
                emit("blocked", reason=f"APKMirror returned HTTP {status} to Chromium.")
                await browser.close()
                return 4

            # The page may start a browser download automatically after its
            # countdown. First wait briefly for that normal site behavior.
            await page.wait_for_timeout(5000)

            # If no automatic download starts, click at most two visible,
            # site-provided download controls. This does not submit forms or
            # interact with challenge pages.
            clicks = 0
            while not downloads and clicks < 2:
                controls = await inspect_download_controls(page)
                emit("download_controls", page_url=page.url, controls=controls[:8])
                if not controls:
                    break

                selected = controls[0]
                try:
                    locator = page.locator("a[href], button").nth(selected["index"])
                    page_count_before = len(all_pages)
                    await locator.click(timeout=7000)
                    await page.wait_for_timeout(2500)
                    if len(all_pages) > page_count_before:
                        page = all_pages[-1]
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception as exc:
                    emit("control_click_error", detail=str(exc)[:250])

                clicks += 1
                await page.wait_for_timeout(3500)
                body = (await page.locator("body").inner_text(timeout=5000))[:5000]
                title = await page.title()
                if any(marker in (title + " " + body).lower() for marker in CHALLENGE_MARKERS):
                    emit("blocked_after_click", page_url=page.url, title=title[:200])
                    await browser.close()
                    return 4

            if not downloads:
                controls = await inspect_download_controls(page)
                emit(
                    "no_download",
                    final_url=page.url,
                    title=(await page.title())[:250],
                    visible_download_controls=controls[:8],
                    observed_errors=observed_errors[-20:],
                    message="Chromium reached a page, but the ordinary page flow did not produce a download.",
                )
                await browser.close()
                return 5

            download_page, download = downloads[0]
            suggested_name = Path(download.suggested_filename).name or "artifact.apk"
            saved_path = download_dir / suggested_name
            await download.save_as(str(saved_path))
            size = saved_path.stat().st_size
            valid_zip = zipfile.is_zipfile(saved_path)
            has_manifest = False
            if valid_zip:
                try:
                    with zipfile.ZipFile(saved_path) as archive:
                        has_manifest = "AndroidManifest.xml" in archive.namelist()
                except (OSError, zipfile.BadZipFile):
                    valid_zip = False

            emit(
                "download_received",
                page_url=download_page.url,
                suggested_filename=suggested_name,
                bytes=size,
                valid_zip=valid_zip,
                contains_android_manifest=has_manifest,
                sha256_note="A full hash is intentionally omitted from this network diagnostic.",
            )
            await browser.close()

            if size <= 0 or not valid_zip or not has_manifest:
                emit("result", success=False, reason="Downloaded content did not validate as an APK-like ZIP.")
                return 6

            emit(
                "result",
                success=True,
                message="Ordinary Chromium navigation initiated a download and received a ZIP containing AndroidManifest.xml.",
                expected_identity_note="The browser route is reachable; package/version validation still belongs to the existing APK validation pipeline.",
            )
            return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
