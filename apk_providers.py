from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
import os
import re
import time

import requests
from bs4 import BeautifulSoup, Tag

try:
    import cloudscraper
except ImportError:  # pragma: no cover - workflow installs it
    cloudscraper = None


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DEFAULT_PROVIDER_URLS = {
    "youtube": {
        "apkmirror": "https://www.apkmirror.com/apk/google-inc/youtube/",
        "uptodown": "https://youtube.en.uptodown.com/android/versions",
    },
    "youtube-music": {
        "apkmirror": "https://www.apkmirror.com/apk/google-inc/youtube-music/",
        "uptodown": "https://youtube-music.en.uptodown.com/android/versions",
    },
}


class ProviderError(RuntimeError):
    pass


@dataclass
class ApkCandidate:
    provider: str
    version: str
    page_url: str
    download_page_url: str = ""
    download_url: str = ""
    architecture: str = ""
    dpi: str = ""
    is_bundle: bool = False
    filename_hint: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class AcquisitionResult:
    status: str
    path: Optional[str] = None
    candidate: Optional[ApkCandidate] = None
    attempts: list[dict] = field(default_factory=list)
    manual_urls: list[str] = field(default_factory=list)
    error: str = ""


def _scraper():
    if cloudscraper is not None:
        s = cloudscraper.create_scraper()
    else:
        s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
    return s


def _version_key(version: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", version or "")]


def _normalize_url(base: str, href: str) -> str:
    return urljoin(base, href)


def _clean_version(text: str) -> str:
    return (text or "").strip().lstrip("vV")


class APKMirrorProvider:
    name = "apkmirror"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/") + "/"
        self.session = _scraper()

    def _get(self, url: str):
        response = self.session.get(url, timeout=30)
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code}: {url}")
        body = response.text or ""
        if any(marker in body[:6000] for marker in (
            "Just a moment",
            "cf-chl-",
            "__cf_chl_",
            "Checking your browser",
            "Attention Required",
            "challenge-platform",
            "cf_chl_opt",
        )):
            raise ProviderError("Cloudflare challenge page detected")
        return response

    def _discover_app_page(self, query: str) -> str:
        if not query:
            raise ProviderError("APKMirror app page is not configured")
        search_url = (
            "https://www.apkmirror.com/?post_type=app_release&searchtype=apk&s="
            + requests.utils.quote(str(query))
        )
        response = self._get(search_url)
        soup = BeautifulSoup(response.text, "html.parser")
        candidates = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "/apk/" not in href:
                continue
            title = anchor.get_text(" ", strip=True)
            candidates.append((_normalize_url(search_url, href), title))
        if not candidates:
            raise ProviderError(f"APKMirror app search returned no app page for {query}")
        normalized_query = re.sub(r"[^a-z0-9]+", " ", str(query).lower()).strip()
        def score(item):
            title = re.sub(r"[^a-z0-9]+", " ", item[1].lower()).strip()
            return sum(1 for token in normalized_query.split() if len(token) >= 2 and token in title)
        candidates.sort(key=score, reverse=True)
        return candidates[0][0]

    def get_version_page(self, target_version: str, app_query: str = "") -> str:
        if not self.base_url:
            self.base_url = self._discover_app_page(app_query).rstrip("/") + "/"
        response = self._get(self.base_url)
        soup = BeautifulSoup(response.text, "html.parser")

        # Prefer an actual exact version row from APKMirror's version list.
        exact = _clean_version(target_version)
        for row in soup.select("div.listWidget div"):
            text = _clean_version(row.get_text(" ", strip=True))
            if exact and re.search(rf"(?<!\d){re.escape(exact)}(?!\d)", text):
                anchor = row.find("a", href=True)
                if anchor:
                    return _normalize_url(self.base_url, anchor["href"])

        # Fallback used by the reference Morphe builder and Morphe Manager-style
        # exact-version navigation.
        slug = exact.replace(".", "-")
        for url in (
            f"{self.base_url}{slug}-release/",
            f"{self.base_url}{slug}/",
        ):
            try:
                test = self._get(url)
                if "table" in (test.text or "") or exact in (test.text or ""):
                    return url
            except Exception:
                continue

        raise ProviderError(f"Exact APKMirror version page not found for {target_version}")

    def get_variants(self, version_url: str) -> list[ApkCandidate]:
        response = self._get(version_url)
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find("div", class_="table")
        if not table:
            raise ProviderError("APKMirror variants table not found")

        out: list[ApkCandidate] = []
        rows = table.find_all("div", recursive=False)[1:]
        for row in rows:
            cells = row.find_all("div", class_="table-cell", recursive=False)
            if not cells:
                continue

            link = row.find("a", class_="accent_color", href=True)
            if not link:
                continue

            bundle_tag = row.find("span", class_="apkm-badge")
            is_bundle = bool(bundle_tag and "BUNDLE" in bundle_tag.get_text(" ", strip=True).upper())
            architecture = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            dpi = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            out.append(ApkCandidate(
                provider=self.name,
                version="",
                page_url=_normalize_url("https://www.apkmirror.com/", link["href"]),
                architecture=architecture,
                dpi=dpi,
                is_bundle=is_bundle,
                details={"version_page": version_url},
            ))

        return out

    def _resolve_download_page(self, variant_url: str) -> str:
        response = self._get(variant_url)
        soup = BeautifulSoup(response.text, "html.parser")
        button = soup.find("a", class_="downloadButton", href=True)
        if not button:
            raise ProviderError("APKMirror download button not found")
        return _normalize_url(variant_url, button["href"])

    def _resolve_direct_download(self, download_page_url: str) -> str:
        response = self._get(download_page_url)
        soup = BeautifulSoup(response.text, "html.parser")

        # APKMirror currently exposes the final file link with rel=nofollow.
        direct = soup.find("a", attrs={"rel": "nofollow"}, href=True)
        if not direct:
            direct = soup.find("a", href=True, string=re.compile(r"download", re.I))
        if not direct:
            raise ProviderError("APKMirror direct download link not found")
        return _normalize_url(download_page_url, direct["href"])

    def _score(self, candidate: ApkCandidate, preferred_arch: str) -> int:
        arch = candidate.architecture.lower()
        pref = (preferred_arch or "auto").lower()

        if "x86" in arch:
            return -10000

        if pref not in ("", "auto", "automatic"):
            if pref in arch:
                arch_score = 5000
            elif "universal" in arch:
                arch_score = 4500
            else:
                arch_score = -1000
        else:
            if "universal" in arch:
                arch_score = 5000
            elif "arm64" in arch:
                arch_score = 4000
            elif "armeabi" in arch or "armv7" in arch:
                arch_score = 3000
            else:
                arch_score = 0

        dpi = candidate.dpi.lower()
        if "nodpi" in dpi:
            dpi_score = 300
        elif "120-480" in dpi:
            dpi_score = 200
        elif "480-640" in dpi:
            dpi_score = 100
        else:
            dpi_score = 0

        bundle_penalty = -100 if candidate.is_bundle else 0
        return arch_score + dpi_score + bundle_penalty

    def resolve(self, target_version: str, preferred_arch: str = "auto", app_query: str = "") -> list[ApkCandidate]:
        version_url = self.get_version_page(target_version, app_query=app_query)
        variants = self.get_variants(version_url)
        exact_variants = []

        for candidate in variants:
            candidate.version = target_version
            try:
                candidate.download_page_url = self._resolve_download_page(candidate.page_url)
            except Exception as exc:
                candidate.details["download_page_error"] = str(exc)
                # Keep the variant page as the useful manual fallback if the
                # download button itself is unavailable.
                candidate.download_page_url = candidate.page_url
            exact_variants.append(candidate)

        exact_variants.sort(key=lambda c: self._score(c, preferred_arch), reverse=True)
        return [c for c in exact_variants if self._score(c, preferred_arch) > 0]

    def download(self, candidate: ApkCandidate, destination: str) -> None:
        if not candidate.download_page_url or candidate.download_page_url == candidate.page_url:
            candidate.download_page_url = self._resolve_download_page(candidate.page_url)

        candidate.download_url = self._resolve_direct_download(candidate.download_page_url)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)

        response = self.session.get(
            candidate.download_url,
            headers={"Referer": candidate.download_page_url},
            timeout=60,
            stream=True,
        )
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code} while downloading APK")

        with open(destination, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


class UptodownProvider:
    name = "uptodown"

    def __init__(self, versions_url: str):
        self.versions_url = versions_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.8",
        })

    def _get(self, url: str):
        response = self.session.get(url, timeout=30)
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code}: {url}")
        return response

    def _find_version_page(self, target_version: str) -> str:
        response = self._get(self.versions_url)
        soup = BeautifulSoup(response.text, "html.parser")
        wanted = _clean_version(target_version)

        for anchor in soup.find_all("a", href=True):
            text = _clean_version(anchor.get_text(" ", strip=True))
            if text == wanted or re.search(rf"(?<!\d){re.escape(wanted)}(?!\d)", text):
                href = anchor["href"]
                if "uptodown.com" in href or href.startswith("/"):
                    return _normalize_url(self.versions_url, href)

        raise ProviderError(f"Exact Uptodown version page not found for {target_version}")

    def resolve(self, target_version: str, preferred_arch: str = "auto") -> list[ApkCandidate]:
        version_page = self._find_version_page(target_version)
        response = self._get(version_page)
        soup = BeautifulSoup(response.text, "html.parser")

        candidates: list[ApkCandidate] = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            label = anchor.get_text(" ", strip=True).lower()
            if "/android/download/" in href or "download" in label:
                candidates.append(ApkCandidate(
                    provider=self.name,
                    version=target_version,
                    page_url=version_page,
                    download_page_url=_normalize_url(version_page, href),
                    architecture="",
                    is_bundle="xapk" in label.lower(),
                ))

        if not candidates:
            # The version page itself remains a valid manual starting point.
            candidates.append(ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=version_page,
                download_page_url=version_page,
            ))
        return candidates

    def download(self, candidate: ApkCandidate, destination: str) -> None:
        url = candidate.download_page_url or candidate.page_url
        response = self.session.get(url, timeout=60, allow_redirects=True, stream=True)
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code} while downloading APK")

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            raise ProviderError("Uptodown returned an HTML page instead of an APK file")

        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)


def get_provider_urls(app_id: str, app_config: dict) -> dict:
    configured = app_config.get("apk_sources") or {}
    defaults = DEFAULT_PROVIDER_URLS.get(app_id, {})
    merged = dict(defaults)
    for name, value in configured.items():
        if isinstance(value, str) and value.strip():
            merged[name] = value.strip()
    return merged


def provider_order(app_config: dict) -> list[str]:
    configured = app_config.get("apk_providers")
    if isinstance(configured, list) and configured:
        return [str(x).lower() for x in configured]
    return ["apkmirror", "uptodown"]


def acquire_from_providers(
    app_id: str,
    target_version: str,
    preferred_arch: str,
    app_config: dict,
    destination_dir: str,
) -> AcquisitionResult:
    urls = get_provider_urls(app_id, app_config)
    attempts: list[dict] = []
    manual_urls: list[str] = []

    for provider_name in provider_order(app_config):
        base_url = urls.get(provider_name)
        if not base_url:
            attempts.append({"provider": provider_name, "status": "not_configured"})
            continue

        try:
            if provider_name == "apkmirror":
                provider = APKMirrorProvider(base_url or "")
            elif provider_name == "uptodown":
                provider = UptodownProvider(base_url)
            else:
                attempts.append({"provider": provider_name, "status": "unsupported"})
                continue

            if provider_name == "apkmirror":
                app_query = (
                    app_config.get("name")
                    or app_config.get("android_package")
                    or app_config.get("package")
                    or app_id
                )
                candidates = provider.resolve(target_version, preferred_arch, app_query=app_query)
            else:
                candidates = provider.resolve(target_version, preferred_arch)
            if not candidates:
                raise ProviderError("No compatible candidate found")

            for index, candidate in enumerate(candidates, 1):
                manual_url = candidate.download_page_url or candidate.page_url
                if manual_url and manual_url not in manual_urls:
                    manual_urls.append(manual_url)

                extension = ".apkm" if candidate.is_bundle else ".apk"
                destination = os.path.join(
                    destination_dir,
                    f"{app_id}-{target_version}-{provider_name}-{index}{extension}",
                )

                try:
                    provider.download(candidate, destination)
                    if os.path.exists(destination) and os.path.getsize(destination) > 1024:
                        return AcquisitionResult(
                            status="downloaded",
                            path=destination,
                            candidate=candidate,
                            attempts=attempts,
                            manual_urls=manual_urls,
                        )
                except Exception as exc:
                    attempts.append({
                        "provider": provider_name,
                        "version": target_version,
                        "status": "download_failed",
                        "candidate": {
                            "page_url": candidate.page_url,
                            "download_page_url": candidate.download_page_url,
                            "architecture": candidate.architecture,
                            "dpi": candidate.dpi,
                            "is_bundle": candidate.is_bundle,
                        },
                        "error": str(exc),
                    })
                    try:
                        if os.path.exists(destination):
                            os.remove(destination)
                    except OSError:
                        pass
                    time.sleep(1)

        except Exception as exc:
            attempts.append({
                "provider": provider_name,
                "version": target_version,
                "status": "resolve_failed",
                "error": str(exc),
            })

    return AcquisitionResult(
        status="manual_required",
        attempts=attempts,
        manual_urls=manual_urls,
        error="All automatic APK providers failed for the exact requested version.",
    )
