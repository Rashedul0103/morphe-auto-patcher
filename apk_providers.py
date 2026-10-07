from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse, unquote, parse_qs
import base64
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import requests
try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional CI transport
    curl_requests = None
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

UPTODOWN_API_BASE = "https://www.uptodown.app/eapi"
UPTODOWN_APIKEY_SECRET = "$(=a%\u00b7!45J&S"
UPTODOWN_API_USER_AGENT = (
    "Dalvik/2.1.0 (Linux; U; Android 14; SM-G955F "
    "Build/AP2A.240805.005)"
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
    # APKMirror increasingly enforces browser TLS fingerprints from CI IPs.
    # Prefer curl_cffi's Chrome impersonation, then fall back to cloudscraper.
    if curl_requests is not None:
        try:
            s = curl_requests.Session(impersonate="chrome")
        except Exception:
            s = None
    else:
        s = None
    if s is None:
        if cloudscraper is not None:
            s = cloudscraper.create_scraper()
        else:
            s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
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


MORPHE_API_URL = "https://api.morphe.software"
MORPHE_API_MAX_REDIRECTS = 5


@dataclass(frozen=True)
class MorpheResolvedUrl:
    provider: str
    url: str
    abi: str


def _morphe_api_abis(preferred_arch: str) -> list[str]:
    preferred = (preferred_arch or "").strip().lower().replace("_", "-")
    if preferred and preferred not in ("auto", "automatic"):
        return [preferred]
    # Morphe Manager asks its backend for the first device ABI. On our
    # architecture-neutral server, arm64 is the normal Android default.
    return ["arm64-v8a"]


def _resolve_head_redirect(session, url: str) -> str:
    current = url
    for _ in range(MORPHE_API_MAX_REDIRECTS):
        response = session.head(
            current,
            timeout=15,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = str(response.headers.get("Location") or "").strip()
            if location:
                current = urljoin(current, location)
                continue
        return current
    return current


def resolve_morphe_api_urls(
    expected_package: str,
    target_version: str,
    preferred_arch: str = "auto",
) -> list[MorpheResolvedUrl]:
    package = str(expected_package or "").strip()
    version = _clean_version(target_version)
    if not package or not version:
        return []

    session = _scraper()
    resolved: list[MorpheResolvedUrl] = []
    seen: set[str] = set()

    for abi in _morphe_api_abis(preferred_arch):
        query = f"{package}~{version}~{abi}"
        endpoint = f"{MORPHE_API_URL}/v2/web-search/{quote(query, safe='')}"
        try:
            final_url = _resolve_head_redirect(session, endpoint)
            if final_url == endpoint or final_url.startswith(MORPHE_API_URL):
                # Some network paths reject HEAD. A lightweight GET is a fallback,
                # mirroring the same redirect-only role without downloading an APK.
                response = session.get(
                    endpoint,
                    timeout=20,
                    allow_redirects=True,
                    stream=True,
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                )
                try:
                    final_url = response.url or endpoint
                finally:
                    response.close()

            host = (urlparse(final_url).netloc or "").lower()
            if final_url == endpoint or not host or host.endswith("morphe.software"):
                continue

            if final_url not in seen:
                seen.add(final_url)
                if host.endswith("apkmirror.com"):
                    provider = "apkmirror"
                elif host.endswith("uptodown.com"):
                    provider = "uptodown"
                elif host.endswith("apkpure.com"):
                    provider = "apkpure"
                elif host.endswith("apkcombo.com"):
                    provider = "apkcombo"
                else:
                    # Morphe Manager itself can fall back to Google search for
                    # interactive use. The server cannot download from that page,
                    # so leave it to our normal/manual fallback instead.
                    continue
                resolved.append(MorpheResolvedUrl(provider, final_url, abi))
                print(
                    f"Morphe API resolved {package} {version} ({abi}) -> "
                    f"{final_url}"
                )
        except Exception as exc:
            print(
                f"Morphe API resolver failed for {package} {version} ({abi}): {exc}"
            )
    return resolved


def _primary_version(html: str) -> str:
    """Extract the page's primary artifact version, not historical versions."""
    soup = BeautifulSoup(html or "", "html.parser")

    for selector in (
        'meta[itemprop="softwareVersion"]',
        'meta[property="og:title"]',
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        value = str(node.get("content") or "").strip()
        match = re.search(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)", value)
        if match:
            return match.group(1)

    for node in (soup.find("h1"), soup.select_one("div.version")):
        if not node:
            continue
        value = node.get_text(" ", strip=True)
        match = re.search(r"(?<!\d)(\d+(?:\.\d+)+)(?!\d)", value)
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
    soup = BeautifulSoup(raw, "html.parser")

    # Uptodown and several other catalogs expose this as a structured row.
    for row in soup.select("#technical-information tr"):
        header = row.find("th")
        if header and header.get_text(" ", strip=True).lower() in (
            "package name",
            "package",
            "packagename",
        ):
            value = row.find("td")
            package = value.get_text(" ", strip=True) if value else ""
            if _looks_like_package(package):
                return package

    text = soup.get_text(" ", strip=True)
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
        f"https://search.brave.com/search?q={quote(query)}&source=web",
        f"https://www.mojeek.com/search?q={quote(query)}",
        f"https://www.bing.com/search?format=rss&q={quote(query)}",
        f"https://www.google.com/search?hl=en&num=20&q={quote(query)}",
        f"https://www.bing.com/search?setlang=en&q={quote(query)}",
        f"https://search.yahoo.com/search?p={quote(query)}",
        f"https://html.duckduckgo.com/html/?q={quote(query)}",
    )
    found: list[str] = []
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    })
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

    def decode_bing_or_follow(href: str) -> None:
        parsed = urlparse(href)
        if not parsed.netloc.lower().endswith("bing.com") or parsed.path != "/ck/a":
            return
        values = parse_qs(parsed.query).get("u", [])
        for value in values:
            if value.startswith("a1"):
                payload = value[2:]
                try:
                    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")
                    accept(decoded)
                    if found:
                        return
                except Exception:
                    pass
        try:
            redirected = session.get(href, timeout=20, allow_redirects=True)
            accept(redirected.url or "")
        except Exception:
            pass

    url_pattern = re.compile(r'https?://[^\s<>"\']+', re.I)
    for engine_url in engines:
        try:
            response = session.get(engine_url, timeout=30)
            diagnostics.append(f"{urlparse(engine_url).netloc}:{response.status_code}")
            if response.status_code != 200:
                continue
            body = response.text or ""

            # Bing RSS is plain XML. Parse it directly so lxml is not required.
            if "format=rss" in engine_url or body.lstrip().startswith("<?xml"):
                try:
                    root = ET.fromstring(body)
                    for item in root.findall(".//item"):
                        link = item.findtext("link", default="")
                        accept(link)
                    if len(found) >= limit:
                        break
                except Exception:
                    pass

            if "format=rss" in engine_url or body.lstrip().startswith("<?xml"):
                soup = BeautifulSoup("", "html.parser")
            else:
                soup = BeautifulSoup(body, "html.parser")
            for anchor in soup.select("#b_results li.b_algo h2 a[href], li.b_algo h2 a[href]"):
                href = str(anchor.get("href") or "").strip()
                if href:
                    parsed_href = urlparse(href)
                    if parsed_href.path == "/ck/a":
                        decode_bing_or_follow(href)
                    else:
                        accept(href)

            for anchor in soup.find_all("a", href=True):
                href = str(anchor.get("href") or "").strip()
                if not href:
                    continue
                parsed_href = urlparse(href)
                for key in ("q", "url", "uddg"):
                    for value in parse_qs(parsed_href.query).get(key, []):
                        accept(value)
                decode_bing_or_follow(href)
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
        last_status = None
        for attempt in range(4):
            if attempt:
                time.sleep((2, 4, 8)[attempt - 1])
            response = self.session.get(
                url,
                timeout=30,
                allow_redirects=True,
                headers={
                    "Referer": "https://www.apkmirror.com/" if attempt == 0 else url,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            last_status = response.status_code
            if response.status_code in (403, 408, 429) or response.status_code >= 500:
                retry_after = str(response.headers.get("Retry-After") or "").strip()
                if attempt < 3:
                    try:
                        delay = min(30, max(2, int(float(retry_after))))
                    except ValueError:
                        delay = (2, 5, 10)[attempt]
                    time.sleep(delay)
                    continue
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
        raise ProviderError(f"HTTP {last_status}: {url}")

    def _discover_app_page(self, query: str, expected_package: str = "") -> str:
        if self.base_url:
            return self.base_url

        queries = []
        for value in (
            query,
            expected_package,
            f"{query} Android" if query else "",
            f"{query} for Android" if query else "",
        ):
            value = str(value or "").strip()
            if value and value not in queries:
                queries.append(value)
        if not queries:
            raise ProviderError("APKMirror app identity is not available for discovery")

        candidates = {}

        # APKMirror exposes a machine-readable WordPress search endpoint.
        # Prefer it over fragile search-engine HTML when available.
        for search_query in queries:
            try:
                api_url = (
                    "https://www.apkmirror.com/wp-json/wp/v2/search?search="
                    + quote(search_query)
                    + "&per_page=20"
                )
                api_response = self._get(api_url)
                payload = api_response.json()
                if isinstance(payload, list):
                    for item in payload:
                        url = str(item.get("url") or item.get("link") or "")
                        if "/apk/" in url:
                            candidates.setdefault(_normalize_url(api_url, url), str(item.get("title") or ""))
            except Exception:
                continue

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
                    f"{prefix}-{exact.replace('.', '-')}/",
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
        urls = [
            str(node.get("href") or "")
            for node in soup.select("a.downloadButton[href], a[href*='/download/?key'][href]")
        ]
        for href in dict.fromkeys(urls):
            if href.strip():
                return _normalize_url(variant_url, href)
        raise ProviderError("APKMirror download button not found")

    def _resolve_direct_download(self, download_page_url: str) -> str:
        response = self._get(download_page_url)
        soup = BeautifulSoup(response.text, "html.parser")

        direct = soup.select_one("a#download-link[href]")
        if not direct:
            direct = soup.select_one(
                "a[href*='download.php'][href], a[href*='/download/?key='][href]"
            )
        if not direct:
            direct = soup.find("a", attrs={"rel": "nofollow"}, href=True)
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

    def resolve_seed_url(
        self,
        seed_url: str,
        target_version: str,
        preferred_arch: str = "auto",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        response = self._get(seed_url)
        body = response.text or ""
        exact = _clean_version(target_version)

        version_ok = _primary_version_matches(body, exact) or _version_appears_exact(
            f"{seed_url} {body[:12000]}", exact
        )
        if not version_ok:
            raise ProviderError("Morphe API seed page does not prove the requested version")

        found_package = _extract_package(body)
        if expected_package and found_package and found_package.lower() != expected_package.lower():
            raise ProviderError(
                f"Morphe API seed page has package {found_package}; expected {expected_package}"
            )

        path = urlparse(seed_url).path.lower()
        artifact_type = _artifact_type_from_text(body, "apk")
        if path.endswith("-android-apk-download"):
            return [ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=seed_url,
                download_page_url=seed_url,
                architecture=_extract_architecture(body),
                is_bundle=artifact_type != "apk",
                artifact_type=artifact_type,
                details={"via": "morphe_api"},
            )]

        try:
            variants = self.get_variants(seed_url, target_version)
        except Exception as exc:
            return [ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=seed_url,
                download_page_url=seed_url,
                architecture=_extract_architecture(body),
                is_bundle=artifact_type != "apk",
                artifact_type=artifact_type,
                details={"via": "morphe_api", "variants_error": str(exc)},
            )]

        for candidate in variants:
            candidate.details["via"] = "morphe_api"
        variants.sort(key=lambda c: self._score(c, preferred_arch), reverse=True)
        return [c for c in variants if self._score(c, preferred_arch) > -1000]


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
            search_terms.append(f"site:apkmirror.com/apk/ {expected_package} {exact}")
        if app_query:
            search_terms.append(f'site:apkmirror.com/apk/ "{app_query}" "{exact}"')
            search_terms.append(f"site:apkmirror.com/apk/ {app_query} {exact}")
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

        response = None
        last_status = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            response = self.session.get(
                candidate.download_url,
                headers={
                    "Referer": candidate.download_page_url or candidate.page_url,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "User-Agent": USER_AGENT,
                },
                timeout=120,
                allow_redirects=True,
                stream=True,
            )
            last_status = response.status_code
            if response.status_code == 200:
                break
            response.close()
            if response.status_code not in (403, 408, 429) and response.status_code < 500:
                break
        if response is None or response.status_code != 200:
            raise ProviderError(
                f"HTTP {last_status} while downloading APKMirror artifact"
            )

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
        last_status = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            response = self.session.get(
                url,
                timeout=30,
                allow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            last_status = response.status_code
            if response.status_code == 200:
                return response
            if response.status_code not in (403, 408, 429) and response.status_code < 500:
                break
        raise ProviderError(f"HTTP {last_status}: {url}")

    def _api_get(self, path: str):
        url = f"{UPTODOWN_API_BASE}{path}"
        return self.session.get(
            url,
            headers={
                "User-Agent": UPTODOWN_API_USER_AGENT,
                "Identificador": "Uptodown_Android",
                "Identificador-Version": "707",
                "APIKEY": self._generate_apikey(),
                "Accept": "application/json",
            },
            timeout=30,
        )

    def _resolve_app_id(self, package: str) -> str:
        package = str(package or "").strip()
        if not package:
            return ""
        try:
            response = self._api_get(
                f"/apps/byPackagename/{quote(package, safe='')}"
            )
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                if isinstance(data, dict):
                    app_id = data.get("appID") or data.get("appId") or data.get("id")
                    if app_id:
                        return str(app_id)
        except Exception:
            pass

        # API search is the second native route. Only accept an exact package.
        try:
            response = self._api_get(
                f"/v2/apps/search/{quote(package, safe='')}?page[limit]=10&page[offset]=0"
            )
            if response.status_code == 200:
                payload = response.json()
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                items = data.get("results", []) if isinstance(data, dict) else []
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        found_package = str(
                            item.get("packageName") or item.get("packagename") or ""
                        ).strip()
                        if found_package.lower() == package.lower():
                            app_id = item.get("appID") or item.get("appId") or item.get("id")
                            if app_id:
                                return str(app_id)
        except Exception:
            pass
        return ""

    def _api_detail(self, app_id: str) -> dict:
        response = self._api_get(
            f"/v3/apps/{quote(str(app_id), safe='')}/device/0?countryIsoCode=US"
        )
        if response.status_code != 200:
            return {}
        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return data if isinstance(data, dict) else {}

    def _api_versions(self, app_id: str, limit: int = 100) -> list[dict]:
        response = self._api_get(
            f"/v3/app/{quote(str(app_id), safe='')}/device/1/compatible/versions"
            f"?page[limit]={limit}&page[offset]=0"
        )
        if response.status_code != 200:
            raise ProviderError(
                f"Uptodown versions API returned HTTP {response.status_code}"
            )
        payload = response.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _api_version_matches(record: dict, target_version: str) -> bool:
        wanted = _clean_version(target_version)
        values = (
            record.get("version"),
            record.get("versionName"),
            record.get("version_name"),
        )
        return any(_clean_version(str(value or "")) == wanted for value in values)

    def _api_candidates(
        self,
        app_id: str,
        detail: dict,
        records: list[dict],
        target_version: str,
    ) -> list[ApkCandidate]:
        page_url = ""
        for key in ("url", "appURL", "appUrl", "webUrl"):
            value = str(detail.get(key) or "").strip()
            if value.startswith(("http://", "https://")):
                page_url = value
                break

        candidates: list[ApkCandidate] = []
        for record in records:
            if not self._api_version_matches(record, target_version):
                continue
            file_id = str(
                record.get("fileID") or record.get("fileId") or record.get("fileid") or ""
            ).strip()
            if not file_id:
                continue
            file_type = str(
                record.get("fileType") or record.get("file_type") or record.get("filetype") or "apk"
            ).strip().lower().lstrip(".")
            artifact_type = file_type if file_type in SUPPORTED_ARTIFACT_TYPES else _artifact_type_from_text(file_type, "apk")
            direct_url = ""
            for key in ("downloadURL", "downloadUrl", "download_url", "fileURL", "fileUrl"):
                value = str(record.get(key) or "").strip()
                if value.startswith("https://dw.uptodown.com/dwn/"):
                    direct_url = value
                    break
            architecture = ""
            for key in ("architecture", "arch", "cpu", "abi", "abis", "cpuArchitecture"):
                value = str(record.get(key) or "").strip()
                if value:
                    architecture = value
                    break
            candidates.append(ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=page_url or "https://en.uptodown.com/android",
                download_page_url=page_url or "https://en.uptodown.com/android",
                download_url=direct_url,
                architecture=architecture,
                is_bundle=artifact_type != "apk",
                artifact_type=artifact_type,
                details={
                    "api_native": True,
                    "app_id": app_id,
                    "file_id": file_id,
                    "package": str(detail.get("packagename") or detail.get("packageName") or "").strip(),
                    "version_record": {k: record.get(k) for k in ("version", "versionCode", "fileID", "fileType", "architecture") if k in record},
                },
            ))
        return candidates

    def _discover_app_page(self, query: str, expected_package: str = "") -> str:
        if self.app_url:
            return self.app_url

        queries = []
        for value in (
            query,
            expected_package,
            f"{query} Android" if query else "",
            f"{query} for Android" if query else "",
        ):
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
            f"{query} Android" if query else "",
            f"{query} for Android" if query else "",
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
        if value.startswith("//"):
            parsed = urlparse("https:" + value)
            if parsed.netloc.lower() == "dw.uptodown.com" and parsed.path.startswith("/dwn/"):
                return "https:" + value
            return ""
        if value.startswith("/dwn/"):
            return "https://dw.uptodown.com" + value
        # Uptodown's #detail-download-button[data-url] contains an opaque CDN
        # token. It is not guaranteed to be long or restricted to a narrow
        # character class, so preserve the provider's token verbatim.
        if "://" not in value and not value.startswith(("javascript:", "market:", "intent:")):
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

    @staticmethod
    def _generate_apikey() -> str:
        now = datetime.now(UTC)
        epoch_ms = int(now.timestamp() * 1000)
        offset_ms = (
            now.minute * 60000
            + now.second * 1000
            + now.microsecond // 1000
        )
        hour_epoch = (epoch_ms - offset_ms) // 1000
        raw = UPTODOWN_APIKEY_SECRET + str(hour_epoch)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _api_download_url(self, app_id: str, file_id: str) -> str:
        if not app_id or not file_id:
            raise ProviderError("Uptodown API download resolver requires app and file IDs")
        url = (
            f"{UPTODOWN_API_BASE}/apps/{quote(str(app_id), safe='')}/file/"
            f"{quote(str(file_id), safe='')}/downloadUrl?update=0"
        )
        response = requests.get(
            url,
            headers={
                "User-Agent": UPTODOWN_API_USER_AGENT,
                "Identificador": "Uptodown_Android",
                "Identificador-Version": "707",
                "APIKEY": self._generate_apikey(),
                "Accept": "application/json",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise ProviderError(
                f"Uptodown download API returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
            direct = str((payload.get("data") or {}).get("downloadURL") or "").strip()
        except Exception as exc:
            raise ProviderError(f"Uptodown download API returned invalid JSON: {exc}") from exc
        if not direct.startswith(("http://", "https://")):
            raise ProviderError("Uptodown download API returned no usable downloadURL")
        return direct

    def _direct_from_post_download(self, token: str) -> str:
        token = str(token or "").strip()
        if not token:
            raise ProviderError("Uptodown post-download token is empty")

        parsed_app = urlparse(self.app_url)
        origin = f"{parsed_app.scheme}://{parsed_app.netloc}"
        candidates = [
            f"{origin}/android/post-download/{token.lstrip('/')}",
            f"{self.app_url}/post-download/{token.lstrip('/')}",
            f"{self.app_url}/download/post-download/{token.lstrip('/')}",
        ]

        for url in dict.fromkeys(candidates):
            try:
                response = self._get(url)
            except Exception:
                continue
            direct = self._extract_direct_from_body(response.text or "")
            if direct:
                return direct

        raise ProviderError("Uptodown post-download resolver returned no direct URL")

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
            variant_html = str(child)
            token_match = re.search(
                r"(?:android/)?post-download/([^"'<>\\s'"]+)",
                variant_html,
                re.I,
            )
            post_download_token = token_match.group(1) if token_match else ""

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
                    "post_download_token": post_download_token,
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


    def resolve_seed_url(
        self,
        seed_url: str,
        target_version: str,
        preferred_arch: str = "auto",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        response = self._get(seed_url)
        body = response.text or ""
        exact = _clean_version(target_version)
        if not (
            _primary_version_matches(body, exact)
            or _version_appears_exact(f"{seed_url} {body[:12000]}", exact)
        ):
            raise ProviderError("Morphe API seed page does not prove the requested version")

        found_package = _extract_package(body)
        if expected_package and found_package and found_package.lower() != expected_package.lower():
            raise ProviderError(
                f"Morphe API seed page has package {found_package}; expected {expected_package}"
            )

        path = urlparse(seed_url).path.lower()
        if "/download/" in path:
            artifact_type = _artifact_type_from_text(body, "apk")
            token_match = re.search(
                r'''(?:android/)?post-download/([^"'<>\\s]+)''',
                body,
                re.I,
            )
            return [ApkCandidate(
                provider=self.name,
                version=target_version,
                page_url=seed_url,
                download_page_url=seed_url,
                architecture=_extract_architecture(body),
                is_bundle=artifact_type != "apk",
                artifact_type=artifact_type,
                details={
                    "via": "morphe_api",
                    "post_download_token": token_match.group(1) if token_match else "",
                },
            )]

        # An app/version page returned by the resolver can be handed into the
        # normal native Uptodown API path without search-engine discovery.
        clean = seed_url.rstrip("/")
        if "/versions" in clean:
            self.app_url = clean.split("/versions", 1)[0]
        else:
            self.app_url = clean.split("/download/", 1)[0]
        self.base_url = self.app_url + "/versions"
        data_code = self._data_code()
        records = self._version_record(data_code, target_version)
        candidates: list[ApkCandidate] = []
        for record in records:
            version_page = self._version_page(record)
            fallback_type = _artifact_type_from_text(
                str(record.get("kindFile") or ""), "apk"
            )
            candidates.extend(
                self._variant_candidates(
                    version_page,
                    data_code,
                    target_version,
                    fallback_type,
                )
            )
        for candidate in candidates:
            candidate.details["via"] = "morphe_api"
        candidates.sort(
            key=lambda c: self._arch_score(c, preferred_arch),
            reverse=True,
        )
        return [c for c in candidates if self._arch_score(c, preferred_arch) > -1000]

    def resolve(
        self,
        target_version: str,
        preferred_arch: str = "auto",
        app_query: str = "",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        # First use Uptodown's package-native API. This avoids slug guessing and
        # search-engine dependence entirely when the package is known.
        if expected_package:
            try:
                app_id = self._resolve_app_id(expected_package)
                if app_id:
                    detail = self._api_detail(app_id)
                    real_package = str(
                        detail.get("packagename") or detail.get("packageName") or ""
                    ).strip()
                    if not real_package or real_package.lower() == expected_package.lower():
                        records = self._api_versions(app_id)
                        api_candidates = self._api_candidates(
                            app_id, detail, records, target_version
                        )
                        if api_candidates:
                            api_candidates.sort(
                                key=lambda c: self._arch_score(c, preferred_arch),
                                reverse=True,
                            )
                            return [
                                c for c in api_candidates
                                if self._arch_score(c, preferred_arch) > -1000
                            ]
                    else:
                        raise ProviderError(
                            f"Uptodown API returned mismatched package {real_package}; expected {expected_package}"
                        )
            except ProviderError:
                raise
            except Exception as exc:
                print(f"Uptodown native API discovery failed: {exc}")

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

        # Prefer Uptodown's authenticated eAPI when the version/files endpoint
        # supplied the app and file IDs. This avoids brittle HTML button scraping.
        data_code = str(candidate.details.get("data_code") or "").strip()
        file_id = str(candidate.details.get("file_id") or "").strip()
        if not direct_url and data_code and file_id:
            try:
                direct_url = self._api_download_url(data_code, file_id)
            except Exception as exc:
                candidate.details["api_download_error"] = str(exc)

        if not direct_url:
            post_token = str(candidate.details.get("post_download_token") or "").strip()
            if post_token:
                try:
                    direct_url = self._direct_from_post_download(post_token)
                except Exception as exc:
                    candidate.details["post_download_error"] = str(exc)

        if not direct_url:
            direct_url = self._direct_from_download_page(
                candidate.download_page_url or candidate.page_url
            )
        candidate.download_url = direct_url

        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        if not direct_url.startswith("https://dw.uptodown.com/dwn/"):
            raise ProviderError("Uptodown resolver did not produce a CDN artifact URL")
        response = None
        last_status = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 ** attempt)
            response = self.session.get(
                direct_url,
                headers={
                    "Referer": candidate.download_page_url or candidate.page_url,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": USER_AGENT,
                },
                timeout=120,
                allow_redirects=True,
                stream=True,
            )
            last_status = response.status_code
            if response.status_code == 200:
                break
            response.close()
            if response.status_code not in (403, 408, 429) and response.status_code < 500:
                break
        if response is None or response.status_code != 200:
            raise ProviderError(
                f"HTTP {last_status} while downloading Uptodown artifact"
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




class APKPureProvider:
    name = "apkpure"

    def __init__(self, base_url: str = ""):
        self.session = _scraper()
        self.app_url = ""
        if base_url:
            clean = base_url.rstrip("/")
            self.app_url = clean.split("/download/", 1)[0].split("/versions", 1)[0]

    def _get(self, url: str):
        response = self.session.get(url, timeout=30, allow_redirects=True)
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code}: {url}")
        return response

    def _discover_app_page(self, query: str, expected_package: str = "") -> str:
        if self.app_url:
            return self.app_url

        candidates = {}
        values = []
        for value in (
            expected_package,
            query,
            f"{query} Android" if query else "",
            f"{query} for Android" if query else "",
        ):
            value = str(value or "").strip()
            if value and value not in values:
                values.append(value)

        for value in values:
            url = "https://apkpure.net/search?q=" + quote(value)
            try:
                response = self._get(url)
            except Exception:
                continue
            soup = BeautifulSoup(response.text or "", "html.parser")
            for anchor in soup.find_all("a", href=True):
                href = _normalize_url(url, str(anchor.get("href") or ""))
                if "/app/" not in href:
                    continue
                if urlparse(href).netloc.lower() not in ("apkpure.net", "www.apkpure.net"):
                    continue
                candidates[href.rstrip("/")] = anchor.get_text(" ", strip=True)

        if expected_package:
            for slug_value in (
                query,
                f"{query} Android" if query else "",
                f"{query} for Android" if query else "",
            ):
                slug = _slugify(slug_value)
                if slug:
                    candidates.setdefault(
                        f"https://apkpure.net/{slug}/{expected_package}",
                        slug_value,
                    )

        wanted = re.sub(r"[^a-z0-9]+", " ", str(query).lower()).strip()
        scored = []
        for url, title in list(candidates.items())[:40]:
            try:
                response = self._get(url)
            except Exception:
                continue
            body = response.text or ""
            found_package = _extract_package(body).lower()
            if expected_package and found_package and found_package != expected_package.lower():
                continue
            heading = BeautifulSoup(body, "html.parser").find("h1")
            page_title = heading.get_text(" ", strip=True) if heading else title
            normalized = re.sub(r"[^a-z0-9]+", " ", page_title.lower()).strip()
            score = 0
            if expected_package and found_package == expected_package.lower():
                score += 20000
            if wanted and normalized == wanted:
                score += 5000
            for token in wanted.split():
                if len(token) >= 2 and token in normalized:
                    score += 500
            scored.append((score, url, found_package))

        if expected_package:
            exact = [item for item in scored if item[2] == expected_package.lower()]
            if exact:
                scored = exact
        if not scored:
            raise ProviderError(f"APKPure app discovery failed for {query}")

        scored.sort(key=lambda item: item[0], reverse=True)
        self.app_url = scored[0][1].rstrip("/")
        return self.app_url

    def _verify_page(self, url: str, target_version: str, expected_package: str) -> str:
        try:
            response = self._get(url)
        except Exception:
            return ""
        body = response.text or ""
        if not _primary_version_matches(body, target_version):
            return ""
        if expected_package:
            found = _extract_package(body)
            if not found or found.lower() != expected_package.lower():
                return ""
        return response.url or url

    def _version_page(self, target_version: str, app_query: str, expected_package: str) -> str:
        app_url = self._discover_app_page(app_query, expected_package)
        wanted = _clean_version(target_version)

        for candidate in (
            f"{app_url}/download/{wanted}",
            f"{app_url}/versions/{wanted}",
            f"{app_url}/versions",
        ):
            verified = self._verify_page(candidate, wanted, expected_package)
            if verified:
                return verified

        versions_url = f"{app_url}/versions"
        response = self._get(versions_url)
        soup = BeautifulSoup(response.text or "", "html.parser")
        links = []
        for anchor in soup.find_all("a", href=True):
            href = _normalize_url(versions_url, str(anchor.get("href") or ""))
            label = anchor.get_text(" ", strip=True)
            if not _version_appears_exact(f"{label} {href}", wanted):
                continue
            if "/download/" in href or "/versions/" in href:
                links.append(href)

        for href in dict.fromkeys(links):
            verified = self._verify_page(href, wanted, expected_package)
            if verified:
                return verified

        raise ProviderError(f"Exact APKPure version page not found for {target_version}")

    def _direct_download(self, page_url: str) -> str:
        response = self._get(page_url)
        body = response.text or ""
        soup = BeautifulSoup(body, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            parsed = urlparse(href)
            host = (parsed.netloc or "").lower()
            if parsed.scheme in ("http", "https") and (
                "download.apkpure.com" in host
                or href.lower().endswith((".apk", ".xapk", ".apkm", ".apks"))
            ):
                return href

        for attr in ("data-download-url", "data-url", "download_url", "downloadUrl"):
            pattern = rf"""["']{re.escape(attr)}["']\s*[:=]\s*["']([^"']+)["']"""
            match = re.search(pattern, body, re.I)
            if match and match.group(1).startswith(("http://", "https://")):
                return match.group(1)

        raise ProviderError("APKPure direct download link not found")


    def resolve_seed_url(
        self,
        seed_url: str,
        target_version: str,
        preferred_arch: str = "auto",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        response = self._get(seed_url)
        body = response.text or ""
        exact = _clean_version(target_version)
        if not (
            _primary_version_matches(body, exact)
            or _version_appears_exact(f"{seed_url} {body[:12000]}", exact)
        ):
            raise ProviderError("Morphe API seed page does not prove the requested version")
        found_package = _extract_package(body)
        if expected_package and found_package and found_package.lower() != expected_package.lower():
            raise ProviderError(
                f"Morphe API seed page has package {found_package}; expected {expected_package}"
            )

        artifact_type = _artifact_type_from_text(body, "apk")
        return [ApkCandidate(
            provider=self.name,
            version=target_version,
            page_url=seed_url,
            download_page_url=seed_url,
            architecture=_extract_architecture(body),
            is_bundle=artifact_type != "apk",
            artifact_type=artifact_type,
            details={"via": "morphe_api"},
        )]

    def resolve(
        self,
        target_version: str,
        preferred_arch: str = "auto",
        app_query: str = "",
        expected_package: str = "",
    ) -> list[ApkCandidate]:
        version_page = self._version_page(target_version, app_query, expected_package)
        response = self._get(version_page)
        body = response.text or ""
        artifact_type = _artifact_type_from_text(body, "apk")
        return [ApkCandidate(
            provider=self.name,
            version=target_version,
            page_url=version_page,
            download_page_url=version_page,
            architecture=_extract_architecture(body),
            is_bundle=artifact_type != "apk",
            artifact_type=artifact_type,
            details={"version_page": version_page},
        )]

    def download(self, candidate: ApkCandidate, destination: str) -> None:
        page_url = candidate.download_page_url or candidate.page_url
        direct = candidate.download_url or self._direct_download(page_url)
        candidate.download_url = direct
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        response = self.session.get(
            direct,
            headers={"Referer": page_url, "User-Agent": USER_AGENT},
            timeout=120,
            allow_redirects=True,
            stream=True,
        )
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code} while downloading APKPure artifact")

        with open(destination, "wb") as fh:
            first = True
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if first and _looks_like_html_bytes(chunk):
                    raise ProviderError("APKPure returned HTML instead of an APK artifact")
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
    order = [str(x).lower() for x in configured] if isinstance(configured, list) and configured else []
    for provider_name in ("apkmirror", "uptodown", "apkpure"):
        if provider_name not in order:
            order.append(provider_name)
    return order


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

    morphe_seeds: dict[str, list[MorpheResolvedUrl]] = {}
    if expected_package:
        for seed in resolve_morphe_api_urls(
            expected_package,
            target_version,
            preferred_arch=preferred_arch,
        ):
            morphe_seeds.setdefault(seed.provider, []).append(seed)
            if seed.url not in manual_urls:
                manual_urls.append(seed.url)

    def provider_instance(provider_name: str):
        if provider_name == "apkmirror":
            return APKMirrorProvider(urls.get(provider_name, ""))
        if provider_name == "uptodown":
            return UptodownProvider(urls.get(provider_name, ""))
        if provider_name == "apkpure":
            return APKPureProvider(urls.get(provider_name, ""))
        return None

    def attempt_candidates(
        provider,
        provider_name: str,
        candidates: list[ApkCandidate],
        via: str = "provider",
    ) -> AcquisitionResult | None:
        if not candidates:
            return None

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
                    "via": via,
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
        return None

    ordered_providers = provider_order(app_config)
    for provider_name in ordered_providers:
        provider = provider_instance(provider_name)
        if provider is None:
            continue

        # Official Morphe Manager first resolves a destination page through the
        # Morphe API. Use that exact resolver result before attempting catalog
        # discovery of our own.
        for seed in morphe_seeds.get(provider_name, []):
            try:
                candidates = provider.resolve_seed_url(
                    seed.url,
                    target_version,
                    preferred_arch,
                    expected_package,
                )
                result = attempt_candidates(
                    provider,
                    provider_name,
                    candidates,
                    via=f"morphe_api:{seed.abi}",
                )
                if result:
                    return result
            except Exception as exc:
                attempts.append({
                    "provider": provider_name,
                    "version": target_version,
                    "status": "seed_resolve_failed",
                    "via": f"morphe_api:{seed.abi}",
                    "seed_url": seed.url,
                    "error": str(exc),
                })

        try:
            if provider_name == "apkmirror":
                candidates = provider.resolve(
                    target_version,
                    preferred_arch,
                    app_query=app_query,
                    expected_package=expected_package,
                )
            elif provider_name == "uptodown":
                candidates = provider.resolve(
                    target_version,
                    preferred_arch,
                    app_query=app_query,
                    expected_package=expected_package,
                )
            elif provider_name == "apkpure":
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

            result = attempt_candidates(provider, provider_name, candidates)
            if result:
                return result
        except Exception as exc:
            attempts.append({
                "provider": provider_name,
                "version": target_version,
                "status": "resolve_failed",
                "base_url": urls.get(provider_name) or None,
                "error": str(exc),
                "app_query": app_query,
                "expected_package": expected_package,
            })

    # A resolver can occasionally point to a provider not enabled in the config.
    # Surface it as a manual destination rather than silently discarding it.
    for provider_name, seeds in morphe_seeds.items():
        if provider_name in ordered_providers:
            continue
        for seed in seeds:
            attempts.append({
                "provider": provider_name,
                "version": target_version,
                "status": "resolved_but_disabled",
                "via": f"morphe_api:{seed.abi}",
                "seed_url": seed.url,
            })

    return AcquisitionResult(
        status="manual_required",
        attempts=attempts,
        manual_urls=manual_urls,
        error="All automatic APK providers failed for the exact requested version.",
    )

