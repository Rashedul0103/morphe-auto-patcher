from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse, unquote, parse_qs
import os
import re
import time

import requests
from bs4 import BeautifulSoup
from apk_validator import ApkValidationError, validate_artifact

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

SUPPORTED_ARTIFACT_TYPES = {"apk", "apkm", "apks", "xapk"}


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
    artifact_type: str = "apk"
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


def _version_appears_exact(text: str, target_version: str) -> bool:
    wanted = _clean_version(target_version)
    if not wanted:
        return False
    return bool(re.search(rf"(?<!\d){re.escape(wanted)}(?!\d)", text or ""))


def _primary_version(html: str) -> str:
    """Extract the page's primary artifact version, not historical versions."""
    soup = BeautifulSoup(html or "", "html.parser")
    for node in (soup.find("h1"), soup.select_one('meta[property="og:title"]')):
        if not node:
            continue
        value = node.get("content") if getattr(node, "name", "") == "meta" else node.get_text(" ", strip=True)
        match = re.search(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)", value or "")
        if match:
            return match.group(1)
    text = soup.get_text(" ", strip=True)
    match = re.search(r"\bVersion\s*:\s*(\d+(?:\.\d+)+)\b", text, re.I)
    return match.group(1) if match else ""


def _primary_version_matches(html: str, target_version: str) -> bool:
    primary = _primary_version(html)
    return bool(primary and _clean_version(primary) == _clean_version(target_version))


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _looks_like_package(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$", str(value or "").strip()))


def _extract_package(html: str) -> str:
    raw = html or ""
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    patterns = (
        r"\bPackage(?:\s+Name)?\s*:\s*([A-Za-z0-9_.$]+)",
        r"\bPackage(?:\s+Name)?\s+([A-Za-z0-9_.$]+)",
        r"""["']packageName["']\s*:\s*["']([A-Za-z0-9_.$]+)["']""",
        r"""["']package["']\s*:\s*["']([A-Za-z0-9_.$]+)["']""",
        r"""data-package(?:name)?\s*=\s*["']([A-Za-z0-9_.$]+)["']""",
    )
    for source in (text, raw):
        for pattern in patterns:
            match = re.search(pattern, source, re.I)
            if match and _looks_like_package(match.group(1)):
                return match.group(1)
    return ""


def _artifact_type_from_text(text: str, fallback: str = "apk") -> str:
    haystack = (text or "").lower()
    for kind in ("apkm", "apks", "xapk"):
        if kind in haystack:
            return kind
    if "apk bundle" in haystack or ("base apk" in haystack and "splits" in haystack):
        return "apkm"
    return fallback if fallback in SUPPORTED_ARTIFACT_TYPES else "apk"


def _looks_like_html_bytes(chunk: bytes) -> bool:
    sample = (chunk or b"")[:2048].lstrip().lower()
    return sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _extract_architecture(text: str) -> str:
    values = []
    for value in ("arm64-v8a", "armeabi-v7a", "x86_64", "x86", "universal"):
        if re.search(rf"\b{re.escape(value)}\b", text or "", re.I):
            values.append(value)
    return " + ".join(values)


def _search_result_urls(query: str, allowed_hosts: tuple[str, ...], limit: int = 12) -> list[str]:
    """Discover provider URLs through CI-safe search transports."""
    engines = (
        f"https://r.jina.ai/http://www.google.com/search?hl=en&num=20&q={quote(query)}",
        f"https://r.jina.ai/http://www.bing.com/search?setlang=en&q={quote(query)}",
        f"https://r.jina.ai/http://search.yahoo.com/search?p={quote(query)}",
        f"https://r.jina.ai/http://html.duckduckgo.com/html/?q={quote(query)}",
        f"https://www.google.com/search?hl=en&num=20&q={quote(query)}",
        f"https://www.bing.com/search?setlang=en&q={quote(query)}",
        f"https://search.yahoo.com/search?p={quote(query)}",
        f"https://html.duckduckgo.com/html/?q={quote(query)}",
    )
    found: list[str] = []
    session = _scraper()
    diagnostics = []

    def accept(value: str) -> None:
        value = unquote((value or "").strip())
        if not value:
            return
        value = value.replace("&amp;", "&").replace("\\/", "/")
        parsed = urlparse(value)
        host = (parsed.netloc or "").lower()
        if parsed.scheme not in ("http", "https"):
            return
        if not any(allowed.lower() in host for allowed in allowed_hosts):
            return
        cleaned = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            cleaned += "?" + parsed.query
        if cleaned not in found:
            found.append(cleaned)

    url_pattern = re.compile(r"https?://[^\s<>\"']+", re.I)
    for engine_url in engines:
        try:
            response = session.get(engine_url, timeout=30)
            diagnostics.append(f"{urlparse(engine_url).netloc}:{response.status_code}")
            if response.status_code != 200:
                continue
            body = response.text or ""
            soup = BeautifulSoup(body, "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href") or "").strip()
                if not href:
                    continue
                parsed_href = urlparse(href)
                for key in ("q", "url", "uddg"):
                    for value in parse_qs(parsed_href.query).get(key, []):
                        accept(value)
                accept(href)
            for match in url_pattern.findall(body):
                accept(match)
            for match in re.findall(r"\]\((https?://[^)]+)\)", body):
                accept(match)
            if len(found) >= limit:
                break
        except Exception as exc:
            diagnostics.append(f"{urlparse(engine_url).netloc}:error:{type(exc).__name__}")
            continue

    if not found:
        print("Provider search returned no usable URLs for query={!r}; engines={}".format(query, ", ".join(diagnostics)))
    return found[:limit]

def _apk_mirror_release_parent(url: str) -> str:
    """Turn a variant/download URL into its release landing page."""
    clean = url.rstrip("/") + "/"
    marker = "/release/"
    if marker in clean:
        prefix, suffix = clean.split(marker, 1)
        release_slug = suffix.split("/", 1)[0]
        return f"{prefix}/release/{release_slug}/"
    parts = [x for x in clean.split("/") if x]
    if parts and parts[-1].endswith("android-apk-download"):
        return "/".join(clean.split("/")[:-2]) + "/"
    return clean


class APKMirrorProvider:
    name = "apkmirror"

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/") + "/" if base_url else ""
        self.app_page_candidates: list[str] = [self.base_url] if self.base_url else []
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

    def _discover_app_page(self, query: str, expected_package: str = "") -> str:
        if self.base_url:
            return self.base_url

        queries = []
        for value in (query, expected_package):
            value = str(value or "").strip()
            if value and value not in queries:
                queries.append(value)
        if not queries:
            raise ProviderError("APKMirror app identity is not available for discovery")

        candidates = {}
        for search_query in queries:
            for search_type in ("app", "apk"):
                search_url = (
                    "https://www.apkmirror.com/?post_type=app_release&searchtype="
                    + search_type
                    + "&s="
                    + quote(search_query)
                )
                try:
                    response = self._get(search_url)
                except ProviderError:
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                for anchor in soup.find_all("a", href=True):
                    href = anchor["href"]
                    if "/apk/" not in href:
                        continue
                    url = _normalize_url(search_url, href)
                    if url not in candidates:
                        candidates[url] = anchor.get_text(" ", strip=True)

        if not candidates:
            web_queries = []
            if expected_package:
                web_queries.append(f'site:apkmirror.com/apk/ "{expected_package}"')
            if query:
                web_queries.append(f'site:apkmirror.com/apk/ "{query}"')
            for web_query in web_queries:
                for result_url in _search_result_urls(
                    web_query, ("apkmirror.com",), limit=12
                ):
                    if "/apk/" in result_url:
                        candidates.setdefault(result_url, "")
            if not candidates:
                raise ProviderError(f"APKMirror app search returned no app page for {query}")

        scored = []
        normalized_package = str(expected_package or "").strip().lower()
        normalized_query = re.sub(r"[^a-z0-9]+", " ", str(query).lower()).strip()

        def score_candidates(items):
            scored_local = []
            for url, anchor_title in list(items.items())[:24]:
                score = 0
                package = ""
                title = anchor_title
                try:
                    page = self._get(url)
                    page_soup = BeautifulSoup(page.text, "html.parser")
                    heading = page_soup.find("h1")
                    if heading:
                        title = heading.get_text(" ", strip=True)
                    package = _extract_package(page.text).lower()
                    if normalized_package:
                        if package == normalized_package:
                            score += 20000
                        elif package:
                            score -= 10000
                    title_normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
                    if normalized_query and title_normalized == normalized_query:
                        score += 6000
                    for token in normalized_query.split():
                        if len(token) >= 2 and token in title_normalized:
                            score += 600
                except Exception:
                    continue
                scored_local.append((score, url, title, package))
            return scored_local

        scored = score_candidates(candidates)
        if normalized_package and not any(item[3] == normalized_package for item in scored):
            web_queries = [
                f'site:apkmirror.com/apk/ "{normalized_package}"',
                f'site:apkmirror.com/apk/ "{query}"',
            ]
            for web_query in web_queries:
                for result_url in _search_result_urls(web_query, ("apkmirror.com",), limit=12):
                    if "/apk/" in result_url:
                        candidates.setdefault(result_url, "")
            scored = score_candidates(candidates)

        if not scored:
            raise ProviderError(f"APKMirror app discovery failed for {query}")

        if normalized_package:
            exact_package = [item for item in scored if item[3] == normalized_package]
            unknown_package = [item for item in scored if not item[3]]
            mismatched_package = [item for item in scored if item[3] and item[3] != normalized_package]
            if exact_package:
                scored = exact_package
            elif unknown_package:
                scored = unknown_package
            elif mismatched_package:
                raise ProviderError(
                    f"APKMirror app discovery found only mismatched packages; expected {expected_package}"
                )

        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0]
        self.app_page_candidates = [item[1].rstrip("/") + "/" for item in scored]
        self.base_url = self.app_page_candidates[0]
        return self.base_url

    def get_version_page(
        self,
        target_version: str,
        app_query: str = "",
        expected_package: str = "",
    ) -> str:
        exact = _clean_version(target_version)
        if not exact:
            raise ProviderError("Exact target version is required")

        try:
            base_url = self.base_url or self._discover_app_page(app_query, expected_package)
        except ProviderError:
            base_url = ""

        app_urls = []
        if base_url:
            app_urls.append(base_url)
        for candidate in getattr(self, "app_page_candidates", []) or []:
            if candidate and candidate not in app_urls:
                app_urls.append(candidate)

        def release_matches(url: str) -> str:
            try:
                response = self._get(url)
            except Exception:
                return ""
            body = response.text or ""
            if not _primary_version_matches(body, exact):
                return ""
            if expected_package:
                found_package = _extract_package(body)
                if not found_package or found_package.lower() != expected_package.lower():
                    return ""
            return response.url or url

        # First try provider-native app pages and derive exact release slugs.
        for app_url in app_urls:
            try:
                response = self._get(app_url)
            except Exception:
                continue
            soup = BeautifulSoup(response.text or "", "html.parser")
            landing_package = _extract_package(response.text or "")
            if expected_package and landing_package and landing_package.lower() != expected_package.lower():
                continue

            heading = soup.find("h1")
            title = heading.get_text(" ", strip=True) if heading else ""
            if not title:
                meta = soup.select_one('meta[property="og:title"]')
                title = str(meta.get("content") or "").strip() if meta else ""

            path_parts = [p for p in urlparse(app_url).path.split("/") if p]
            prefixes = []
            for value in (re.sub(r"\b\d+(?:\.\d+)+\b", " ", title), path_parts[-1] if path_parts else "", app_query):
                slug = _slugify(value)
                if slug and slug not in prefixes:
                    prefixes.append(slug)

            for prefix in prefixes:
                for suffix in (
                    f"{prefix}-{exact.replace('.', '-')}-release/",
                    f"{prefix}-{exact.replace('.', '-")}/",
                ):
                    verified = release_matches(_normalize_url(app_url, suffix))
                    if verified:
                        return verified

            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href") or "")
                if not href or href.startswith("#"):
                    continue
                label = anchor.get_text(" ", strip=True)
                if not _version_appears_exact(f"{label} {href}", exact):
                    continue
                if "/release/" not in href and "android-apk-download" not in href:
                    continue
                verified = release_matches(_normalize_url(app_url, href))
                if verified:
                    return verified

        # Then use CI-safe search-engine discovery.
        queries = []
        if expected_package:
            queries.append(f'site:apkmirror.com/apk/ "{expected_package}" "{exact}"')
        if app_query:
            queries.append(f'site:apkmirror.com/apk/ "{app_query}" "{exact}"')
        for search_query in queries:
            for result_url in _search_result_urls(search_query, ("apkmirror.com",), limit=20):
                try:
                    response = self._get(result_url)
                except Exception:
                    continue
                body = response.text or ""
                if not _primary_version_matches(body, exact):
                    continue
                if expected_package:
                    found_package = _extract_package(body)
                    if not found_package or found_package.lower() != expected_package.lower():
                        continue
                final_url = response.url or result_url
                if "/android-apk-download/" in final_url:
                    return final_url
                parent = _apk_mirror_release_parent(final_url)
                verified = release_matches(parent)
                if verified:
                    return verified

        raise ProviderError(f"Exact APKMirror version page not found for {target_version}")
    def get_variants(self, version_url: str, target_version: str) -> list[ApkCandidate]:
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

            row_text = row.get_text(" ", strip=True)
            bundle_tag = row.find("span", class_="apkm-badge")
            artifact_type = _artifact_type_from_text(
                f"{row_text} {link.get_text(' ', strip=True)} {link.get('href', '')}",
                "apkm" if bundle_tag else "apk",
            )
            is_bundle = artifact_type != "apk"
            architecture = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            dpi = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            out.append(ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=_normalize_url("https://www.apkmirror.com/", link["href"]),
                architecture=architecture,
                dpi=dpi,
                is_bundle=is_bundle,
                artifact_type=artifact_type,
                details={"version_page": version_url},
            ))

        return out

    def _resolve_download_page(self, variant_url: str) -> str:
        response = self._get(variant_url)
        soup = BeautifulSoup(response.text, "html.parser")
        button = soup.find("a", class_="downloadButton", href=True)
        if not button:
            button = soup.find("a", attrs={"class": re.compile(r"downloadButton", re.I)}, href=True)
        if not button:
            raise ProviderError("APKMirror download button not found")
        return _normalize_url(variant_url, button["href"])

    def _resolve_direct_download(self, download_page_url: str) -> str:
        response = self._get(download_page_url)
        soup = BeautifulSoup(response.text, "html.parser")

        direct = soup.find("a", attrs={"rel": "nofollow"}, href=True)
        if not direct:
            direct = soup.find("a", href=True, string=re.compile(r"download", re.I))
        if not direct:
            raise ProviderError("APKMirror direct download link not found")
        return _normalize_url(download_page_url, direct["href"])

    def _score(self, candidate: ApkCandidate, preferred_arch: str) -> int:
        arch = candidate.architecture.lower()
        pref = (preferred_arch or "auto").lower()

        if "x86_64" in arch or arch.strip() == "x86":
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

    def resolve(
        self,
        target_version: str,
        preferred_arch: str = "auto",
        app_query: str = "",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        # Prefer search-engine results that already point at an exact
        # APKMirror download page. This bypasses fragile variant-filter URLs.
        direct_candidates: list[ApkCandidate] = []
        exact = _clean_version(target_version)
        search_terms = []
        if expected_package:
            search_terms.append(f'site:apkmirror.com/apk/ "{expected_package}" "{exact}"')
        if app_query:
            search_terms.append(f'site:apkmirror.com/apk/ "{app_query}" "{exact}"')
        seen_direct = set()
        for search_query in search_terms:
            for result_url in _search_result_urls(
                search_query, ("apkmirror.com",), limit=12
            ):
                if "android-apk-download" not in result_url or result_url in seen_direct:
                    continue
                seen_direct.add(result_url)
                try:
                    response = self._get(result_url)
                    body = response.text or ""
                    if not _primary_version_matches(body, exact):
                        continue
                    if expected_package:
                        found_package = _extract_package(body)
                        if found_package and found_package.lower() != expected_package.lower():
                            continue
                    artifact_type = _artifact_type_from_text(body, "apk")
                    direct_candidates.append(
                        ApkCandidate(
                            provider=self.name,
                            version=target_version,
                            page_url=result_url,
                            download_page_url=result_url,
                            architecture=_extract_architecture(body),
                            is_bundle=artifact_type != "apk",
                            artifact_type=artifact_type,
                            details={"search_engine": True},
                        )
                    )
                except Exception:
                    continue
        if direct_candidates:
            direct_candidates.sort(
                key=lambda c: self._score(c, preferred_arch),
                reverse=True,
            )
            return [c for c in direct_candidates if self._score(c, preferred_arch) > -1000]

        try:
            version_url = self.get_version_page(
                target_version,
                app_query=app_query,
                expected_package=expected_package,
            )
        except ProviderError:
            if not self.base_url:
                raise
            self.base_url = ""
            version_url = self.get_version_page(
                target_version,
                app_query=app_query,
                expected_package=expected_package,
            )
        variants = self.get_variants(version_url, target_version)
        exact_variants = []

        for candidate in variants:
            try:
                candidate.download_page_url = self._resolve_download_page(candidate.page_url)
            except Exception as exc:
                candidate.details["download_page_error"] = str(exc)
                candidate.download_page_url = candidate.page_url
            exact_variants.append(candidate)

        exact_variants.sort(key=lambda c: self._score(c, preferred_arch), reverse=True)
        # Unknown architecture is still a candidate; final APK validation is authoritative.
        return [c for c in exact_variants if self._score(c, preferred_arch) > -1000]

    def download(self, candidate: ApkCandidate, destination: str) -> None:
        if not candidate.download_page_url or candidate.download_page_url == candidate.page_url:
            candidate.download_page_url = self._resolve_download_page(candidate.page_url)

        candidate.download_url = self._resolve_direct_download(candidate.download_page_url)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)

        response = self.session.get(
            candidate.download_url,
            headers={"Referer": candidate.download_page_url, "User-Agent": USER_AGENT},
            timeout=90,
            stream=True,
        )
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code} while downloading APKMirror artifact")

        with open(destination, "wb") as fh:
            first = True
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if first and _looks_like_html_bytes(chunk):
                    raise ProviderError("APKMirror returned HTML instead of an APK artifact")
                first = False
                fh.write(chunk)


class UptodownProvider:
    name = "uptodown"

    def __init__(self, base_url: str = ""):
        self.base_url = base_url.rstrip("/") + "/" if base_url else ""
        self.app_url = ""
        if self.base_url:
            if self.base_url.rstrip("/").endswith("/versions"):
                self.app_url = self.base_url.rstrip("/")[:-len("/versions")]
            else:
                self.app_url = self.base_url.rstrip("/")
        self.session = _scraper()

    def _get(self, url: str):
        response = self.session.get(url, timeout=30)
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code}: {url}")
        return response

    def _discover_app_page(self, query: str, expected_package: str = "") -> str:
        if self.app_url:
            return self.app_url

        queries = []
        for value in (query, expected_package):
            value = str(value or "").strip()
            if value and value not in queries:
                queries.append(value)
        if not queries:
            raise ProviderError("Uptodown app identity is not available for discovery")

        candidates = {}
        for search_query in queries:
            search_url = "https://en.uptodown.com/android/search/" + quote(
                _slugify(search_query)
            )
            try:
                response = self._get(search_url)
            except ProviderError:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = anchor["href"]
                if not re.search(r"https?://[^/]+\.uptodown\.com/android(?:/|$)", href):
                    continue
                if "/search/" in href:
                    continue
                candidates[_normalize_url(search_url, href).rstrip("/")] = anchor.get_text(
                    " ", strip=True
                )

        slug_values = [
            query,
            expected_package,
            str(expected_package or "").replace(".", "-"),
            str(expected_package or "").split(".")[-1] if expected_package else "",
        ]
        for value in slug_values:
            slug = _slugify(value)
            if slug:
                candidates.setdefault(
                    f"https://{slug}.en.uptodown.com/android", str(value)
                )

        if not candidates:
            web_queries = []
            if expected_package:
                web_queries.append(f'site:uptodown.com/android "{expected_package}"')
            if query:
                web_queries.append(f'site:uptodown.com/android "{query}"')
            for web_query in web_queries:
                for result_url in _search_result_urls(
                    web_query, ("uptodown.com",), limit=12
                ):
                    if "/android" in result_url:
                        candidates.setdefault(result_url.rstrip("/"), "")
            if not candidates:
                raise ProviderError(f"Uptodown app search returned no candidate for {query}")

        normalized_package = str(expected_package or "").strip().lower()
        normalized_query = re.sub(r"[^a-z0-9]+", " ", str(query).lower()).strip()
        scored = []

        for url, anchor_title in list(candidates.items())[:18]:
            score = 0
            package = ""
            title = anchor_title
            try:
                page = self._get(url)
                page_soup = BeautifulSoup(page.text, "html.parser")
                heading = page_soup.find("h1")
                if heading:
                    title = heading.get_text(" ", strip=True)
                package = _extract_package(page.text).lower()
                if normalized_package:
                    if package == normalized_package:
                        score += 20000
                    elif package:
                        score -= 10000
                title_normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
                if normalized_query and title_normalized == normalized_query:
                    score += 6000
                for token in normalized_query.split():
                    if len(token) >= 2 and token in title_normalized:
                        score += 600
            except Exception:
                continue
            scored.append((score, url, title, package))

        if not scored:
            web_queries = []
            if expected_package:
                web_queries.append(f'site:uptodown.com/android "{expected_package}"')
            if query:
                web_queries.append(f'site:uptodown.com/android "{query}"')
            for web_query in web_queries:
                for result_url in _search_result_urls(web_query, ("uptodown.com",), limit=12):
                    if "/android" in result_url:
                        candidates.setdefault(result_url.rstrip("/"), "")
            if candidates:
                scored = []
                for url, anchor_title in list(candidates.items())[:30]:
                    score = 0
                    package = ""
                    title = anchor_title
                    try:
                        page = self._get(url)
                        page_soup = BeautifulSoup(page.text, "html.parser")
                        heading = page_soup.find("h1")
                        if heading:
                            title = heading.get_text(" ", strip=True)
                        package = _extract_package(page.text).lower()
                        if normalized_package:
                            if package == normalized_package:
                                score += 20000
                            elif package:
                                score -= 10000
                        title_normalized = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
                        if normalized_query and title_normalized == normalized_query:
                            score += 6000
                        for token in normalized_query.split():
                            if len(token) >= 2 and token in title_normalized:
                                score += 600
                    except Exception:
                        continue
                    scored.append((score, url, title, package))
        if not scored:
            raise ProviderError(f"Uptodown app discovery failed for {query}")

        if normalized_package:
            exact_package = [item for item in scored if item[3] == normalized_package]
            if not exact_package:
                raise ProviderError(
                    f"Uptodown app discovery could not verify package {expected_package}"
                )
            scored = exact_package

        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0]
        self.app_url = best[1].rstrip("/")
        self.base_url = self.app_url + "/versions"
        return self.app_url

    def _data_code(self) -> str:
        response = self._get(self.app_url)
        soup = BeautifulSoup(response.text, "html.parser")
        node = soup.select_one("#detail-app-name")
        data_code = node.get("data-code", "") if node else ""
        if not data_code:
            raise ProviderError("Uptodown app data-code was not found")
        return str(data_code).strip()

    def _version_record(self, data_code: str, target_version: str) -> list[dict]:
        wanted = _clean_version(target_version)
        records = []
        for page_number in range(1, 21):
            url = f"{self.app_url}/apps/{data_code}/versions/{page_number}"
            try:
                response = self._get(url)
            except ProviderError:
                if page_number == 1:
                    raise
                break
            try:
                payload = response.json()
            except Exception as exc:
                raise ProviderError(f"Uptodown version API returned invalid JSON: {exc}")
            data = payload.get("data") or []
            if not isinstance(data, list) or not data:
                break
            for item in data:
                if isinstance(item, dict) and _clean_version(str(item.get("version", ""))) == wanted:
                    records.append(item)
            if records:
                return records
        if not records:
            raise ProviderError(f"Exact Uptodown version page not found for {target_version}")
        return records

    def _version_page(self, record: dict) -> str:
        version_url = record.get("versionURL") or {}
        if not isinstance(version_url, dict):
            raise ProviderError("Uptodown version metadata is malformed")
        base = str(version_url.get("url") or "").rstrip("/")
        extra = str(version_url.get("extraURL") or "").strip("/")
        version_id = version_url.get("versionID")
        if not base or version_id is None:
            raise ProviderError("Uptodown version metadata lacks a usable URL")
        parts = [base]
        if extra:
            parts.append(extra)
        parts.append(str(version_id))
        return "/".join(parts)

    @staticmethod
    def _coerce_download_value(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https"):
            host = (parsed.netloc or "").lower()
            if host == "dw.uptodown.com" and parsed.path.startswith("/dwn/"):
                return value
            return ""
        if value.startswith("/dwn/"):
            return "https://dw.uptodown.com" + value
        # Uptodown's data-url is normally a long opaque CDN token.
        if len(value) >= 20 and re.fullmatch(r"[A-Za-z0-9_./+=-]+", value):
            return f"https://dw.uptodown.com/dwn/{value.lstrip('/')}"
        return ""

    def _extract_direct_from_body(self, body: str) -> str:
        soup = BeautifulSoup(body or "", "html.parser")
        nodes = []
        for selector in (
            "#detail-download-button",
            "[data-button-id='detail-download-button']",
            "[data-download-button]",
        ):
            node = soup.select_one(selector)
            if node and node not in nodes:
                nodes.append(node)

        for node in nodes:
            for attr in ("data-url", "data-download-url", "data-file-url"):
                direct = self._coerce_download_value(node.get(attr))
                if direct:
                    return direct

        for node in soup.find_all(attrs={"data-url": True}):
            direct = self._coerce_download_value(node.get("data-url"))
            if direct:
                return direct

        for match in re.findall(r"""data-url\s*=\s*["']([^"']+)["']""", body or "", re.I):
            direct = self._coerce_download_value(match)
            if direct:
                return direct

        for anchor in soup.find_all("a", href=True):
            direct = self._coerce_download_value(anchor.get("href"))
            if direct:
                return direct

        return ""

    def _direct_from_download_page(self, url: str) -> str:
        response = self._get(url)
        direct = self._extract_direct_from_body(response.text or "")
        if direct:
            return direct

        # Some Uptodown download pages expose the token only on the non-"-x"
        # form. Retry that alternate page before giving up.
        alternates = []
        if url.endswith("-x"):
            alternates.append(url[:-2])
        parsed = urlparse(url)
        if "/download/" in parsed.path:
            app_root = parsed.path.split("/download/", 1)[0]
            alternates.append(f"{parsed.scheme}://{parsed.netloc}{app_root}/download")

        for alternate in dict.fromkeys(alternates):
            if not alternate or alternate == url:
                continue
            try:
                probe = self._get(alternate)
                direct = self._extract_direct_from_body(probe.text or "")
                if direct:
                    return direct
            except Exception:
                continue

        raise ProviderError("Uptodown direct download URL not found")

    def _variant_candidates(
        self,
        version_page: str,
        data_code: str,
        target_version: str,
        fallback_type: str,
    ) -> list[ApkCandidate]:
        response = self._get(version_page)
        soup = BeautifulSoup(response.text, "html.parser")
        variants_button = soup.select_one(".button.variants")
        data_version = str(variants_button.get("data-version") or "").strip() if variants_button else ""
        if not data_version:
            artifact_type = fallback_type if fallback_type in SUPPORTED_ARTIFACT_TYPES else "apk"
            download_url = self._direct_from_download_page(version_page)
            return [ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=version_page,
                download_page_url=version_page,
                download_url=download_url,
                is_bundle=artifact_type != "apk",
                artifact_type=artifact_type,
                details={"version_page": version_page, "data_code": data_code},
            )]

        origin = self.app_url.rsplit("/android", 1)[0]
        files_url = f"{origin}/app/{data_code}/version/{data_version}/files"
        files_response = self._get(files_url)
        try:
            payload = files_response.json()
            content = payload.get("content") or ""
        except Exception:
            content = files_response.text or ""

        files_soup = BeautifulSoup(content, "html.parser")
        container = files_soup.select_one(".content") or files_soup
        children = list(container.find_all(recursive=False))
        current_arch = ""
        candidates = []

        for child in children:
            classes = {str(x).lower() for x in (child.get("class") or [])}
            if child.name == "p":
                current_arch = child.get_text(" ", strip=True)
                continue
            if "variant" not in classes:
                continue

            file_type_node = child.select_one(".v-file")
            file_type = _artifact_type_from_text(
                file_type_node.get_text(" ", strip=True) if file_type_node else "",
                fallback_type,
            )
            report = child.select_one(".v-report")
            file_id = str(report.get("data-file-id") or "").strip() if report else ""
            if not file_id:
                continue

            download_page = f"{self.app_url}/download/{file_id}-x"
            try:
                direct_url = self._direct_from_download_page(download_page)
            except Exception as exc:
                direct_url = ""
                detail_error = str(exc)
            else:
                detail_error = ""

            candidates.append(ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=version_page,
                download_page_url=download_page,
                download_url=direct_url,
                architecture=current_arch,
                is_bundle=file_type != "apk",
                artifact_type=file_type,
                details={
                    "version_page": version_page,
                    "data_code": data_code,
                    "data_version": data_version,
                    "file_id": file_id,
                    "download_page_error": detail_error,
                },
            ))

        if not candidates:
            artifact_type = fallback_type if fallback_type in SUPPORTED_ARTIFACT_TYPES else "apk"
            direct_url = self._direct_from_download_page(version_page)
            candidates.append(ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=version_page,
                download_page_url=version_page,
                download_url=direct_url,
                is_bundle=artifact_type != "apk",
                artifact_type=artifact_type,
                details={"version_page": version_page, "data_code": data_code},
            ))
        return candidates

    @staticmethod
    def _arch_score(candidate: ApkCandidate, preferred_arch: str) -> int:
        arch = candidate.architecture.lower().replace("_", "-")
        pref = (preferred_arch or "auto").lower().replace("_", "-")
        if "x86_64" in arch or "x86-64" in arch or arch == "x86":
            if pref not in ("x86", "x86-64", "x86_64"):
                return -10000
        if pref not in ("", "auto", "automatic"):
            if pref in arch or (pref == "arm64-v8a" and "arm64" in arch):
                return 5000
            if "universal" in arch:
                return 4500
            return -1000
        if "universal" in arch:
            return 5000
        if "arm64" in arch:
            return 4000
        if "armeabi" in arch or "arm-v7" in arch:
            return 3000
        return 0

    def resolve(
        self,
        target_version: str,
        preferred_arch: str = "auto",
        app_query: str = "",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        try:
            if target_version:
                search_terms = []
                if expected_package:
                    search_terms.append(
                        f'site:uptodown.com/android "{expected_package}" "{target_version}"'
                    )
                if app_query:
                    search_terms.append(
                        f'site:uptodown.com/android "{app_query}" "{target_version}"'
                    )
                for search_query in search_terms:
                    results = _search_result_urls(
                        search_query, ("uptodown.com",), limit=8
                    )
                    for result_url in results:
                        try:
                            probe = self._get(result_url)
                            body = probe.text or ""
                            if not _version_appears_exact(body, target_version):
                                continue
                            if expected_package:
                                found_package = _extract_package(body)
                                if found_package and found_package.lower() != expected_package.lower():
                                    continue
                            # A version/download page can be used directly; the
                            # normal version API is attempted below when it is
                            # an app landing page.
                            if "/download" in result_url or "/versions" in result_url:
                                self.app_url = result_url.split("/versions", 1)[0].rstrip("/")
                                self.base_url = self.app_url + "/versions"
                                data_code = self._data_code()
                                break
                        except Exception:
                            continue
                    else:
                        continue
                    break
                else:
                    self._discover_app_page(app_query, expected_package)
                    data_code = self._data_code()
            else:
                self._discover_app_page(app_query, expected_package)
                data_code = self._data_code()
        except ProviderError:
            if not self.app_url:
                raise
            # A cached/configured provider URL can go stale; rediscover rather
            # than turning that stale mapping into a permanent failure.
            self.base_url = ""
            self.app_url = ""
            self._discover_app_page(app_query, expected_package)
            data_code = self._data_code()
        records = self._version_record(data_code, target_version)

        all_candidates: list[ApkCandidate] = []
        for record in records:
            version_page = self._version_page(record)
            fallback_type = _artifact_type_from_text(
                str(record.get("kindFile") or ""), "apk"
            )
            try:
                candidates = self._variant_candidates(
                    version_page,
                    data_code,
                    target_version,
                    fallback_type,
                )
            except Exception as exc:
                candidates = [ApkCandidate(
                    provider=self.name,
                    version=target_version,
                    page_url=version_page,
                    download_page_url=version_page,
                    is_bundle=fallback_type != "apk",
                    artifact_type=fallback_type,
                    details={"version_page_error": str(exc)},
                )]
            all_candidates.extend(candidates)

        for candidate in all_candidates:
            candidate.details.setdefault("package", expected_package)
        all_candidates.sort(
            key=lambda c: (
                self._arch_score(c, preferred_arch),
                100 if c.artifact_type == "apk" else 0,
            ),
            reverse=True,
        )
        return [c for c in all_candidates if self._arch_score(c, preferred_arch) > -1000]

    def download(self, candidate: ApkCandidate, destination: str) -> None:
        direct_url = candidate.download_url
        if not direct_url:
            direct_url = self._direct_from_download_page(
                candidate.download_page_url or candidate.page_url
            )
            candidate.download_url = direct_url

        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        if not direct_url.startswith("https://dw.uptodown.com/dwn/"):
            raise ProviderError("Uptodown resolver did not produce a CDN artifact URL")
        response = self.session.get(
            direct_url,
            headers={"Referer": candidate.download_page_url or candidate.page_url},
            timeout=120,
            allow_redirects=True,
            stream=True,
        )
        if response.status_code != 200:
            raise ProviderError(
                f"HTTP {response.status_code} while downloading Uptodown artifact"
            )

        with open(destination, "wb") as fh:
            first = True
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if first and _looks_like_html_bytes(chunk):
                    raise ProviderError("Uptodown returned HTML instead of an APK artifact")
                first = False
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


def _candidate_extension(candidate: ApkCandidate) -> str:
    artifact_type = (candidate.artifact_type or "").lower().lstrip(".")
    if artifact_type in SUPPORTED_ARTIFACT_TYPES:
        return "." + artifact_type
    hint = str(candidate.filename_hint or "").lower()
    for artifact_type in SUPPORTED_ARTIFACT_TYPES:
        if hint.endswith("." + artifact_type):
            return "." + artifact_type
    return ".apk"


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

    app_query = (
        str(app_config.get("name") or "").strip()
        or str(app_config.get("android_package") or "").strip()
        or str(app_config.get("package") or "").strip()
        or app_id
    )
    expected_package = str(
        app_config.get("android_package")
        or app_config.get("play_store_package")
        or ""
    ).strip()
    if not expected_package and _looks_like_package(app_config.get("package", "")):
        expected_package = str(app_config.get("package")).strip()

    for provider_name in provider_order(app_config):
        base_url = urls.get(provider_name, "")

        try:
            if provider_name == "apkmirror":
                provider = APKMirrorProvider(base_url)
                candidates = provider.resolve(
                    target_version,
                    preferred_arch,
                    app_query=app_query,
                    expected_package=expected_package,
                )
            elif provider_name == "uptodown":
                provider = UptodownProvider(base_url)
                candidates = provider.resolve(
                    target_version,
                    preferred_arch,
                    app_query=app_query,
                    expected_package=expected_package,
                )
            else:
                attempts.append({"provider": provider_name, "status": "unsupported"})
                continue

            if not candidates:
                raise ProviderError("No compatible candidate found")

            for index, candidate in enumerate(candidates, 1):
                manual_url = candidate.download_page_url or candidate.page_url
                if manual_url and manual_url not in manual_urls:
                    manual_urls.append(manual_url)

                extension = _candidate_extension(candidate)
                destination = os.path.join(
                    destination_dir,
                    f"{app_id}-{target_version}-{provider_name}-{index}{extension}",
                )

                try:
                    provider.download(candidate, destination)
                    if not os.path.exists(destination) or os.path.getsize(destination) <= 1024:
                        raise ProviderError("Provider returned an empty or incomplete artifact")

                    try:
                        validate_artifact(
                            destination,
                            expected_package=expected_package,
                            expected_version=target_version,
                            expected_arch=preferred_arch or "auto",
                        )
                    except ApkValidationError as exc:
                        raise ProviderError(
                            f"Downloaded artifact failed identity validation: {exc}"
                        ) from exc

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
                            "download_url": candidate.download_url,
                            "architecture": candidate.architecture,
                            "dpi": candidate.dpi,
                            "is_bundle": candidate.is_bundle,
                            "artifact_type": candidate.artifact_type,
                            "details": candidate.details,
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
                "base_url": base_url or None,
                "error": str(exc),
                "app_query": app_query,
                "expected_package": expected_package,
            })

    return AcquisitionResult(
        status="manual_required",
        attempts=attempts,
        manual_urls=manual_urls,
        error="All automatic APK providers failed for the exact requested version.",
    )
