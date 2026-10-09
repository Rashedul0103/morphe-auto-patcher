import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
import time
import urllib.parse
import urllib.request
import zipfile

from apk_providers import acquire_from_providers
from apk_validator import ApkValidationError, validate_apk, validate_artifact
from bs4 import BeautifulSoup
import requests

CONFIG_FILE = "config.json"

def find_cli_jar():
    jars = glob.glob("morphe-*-all.jar")
    if not jars:
        raise FileNotFoundError("Morphe CLI jar not found in working directory")
    return jars[0]

def run_cmd(cmd_list, silent=False):
    if not silent:
        print(f"Running: {' '.join(cmd_list)}")
    try:
        result = subprocess.run(cmd_list, capture_output=True, text=True, check=False)
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        combined = f"{stdout}\n{stderr}".strip()
        
        if result.returncode != 0:
            if not silent:
                print(f"Warning: Command failed (Exit {result.returncode}).\nStderr: {stderr if stderr else 'None'}")
            return False, combined
        return True, stdout
    except FileNotFoundError:
        return False, f"Command not found: {cmd_list[0]}"

def send_discord_webhook(webhook_url, title, description, color=0x3B82F6, fields=None):
    if not webhook_url or not webhook_url.startswith("http"):
        return
    try:
        payload = {
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "fields": fields or [],
                "footer": {"text": "Auto-Patcher Engine"}
            }]
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AutoPatcher-Bot/1.0"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Warning: Failed to send Discord webhook: {e}")

def pre_flight_checks():
    if not shutil.which("java"):
        print("❌ Error: 'java' is not installed or not in PATH.")
        sys.exit(1)
    if not shutil.which("gh"):
        print("❌ Error: GitHub CLI ('gh') is not installed or not in PATH.")
        sys.exit(1)
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        ok, _ = run_cmd(["gh", "auth", "status"], silent=True)
        if not ok:
            print("❌ Error: Not authenticated with GitHub CLI. Export GH_TOKEN.")
            sys.exit(1)

def get_latest_tag(repo, allow_prerelease=False):
    ok, out = run_cmd(["gh", "release", "list", "-R", repo, "--limit", "10", "--json", "tagName,isPrerelease,isDraft"])
    if not ok or not out:
        return None
    try:
        releases = json.loads(out)
        for r in releases:
            if r.get("isDraft"): continue
            if r.get("isPrerelease") and not allow_prerelease: continue
            return r["tagName"]
    except json.JSONDecodeError:
        return None
    return None

def release_exists(tag):
    ok, _ = run_cmd(["gh", "release", "view", tag, "--json", "tagName"], silent=True)
    return ok

def patch_source_candidates(app, package, config):
    """Return enabled patch repositories in preferred order.

    Order:
    1. app.patch_sources (explicit per-app priority)
    2. enabled configured sources that advertise this package
    3. legacy app.patches_repo
    """
    repos = []

    def add(repo):
        if isinstance(repo, str):
            value = repo.strip()
            if value and value not in repos:
                repos.append(value)

    for repo in app.get("patch_sources", []):
        add(repo)

    for source in config.get("sources", []):
        if not isinstance(source, dict) or source.get("enabled") is False:
            continue
        apps = source.get("apps", [])
        packages = {
            (entry.get("package") if isinstance(entry, dict) else entry)
            for entry in apps
            if entry
        }
        match_keys = {
            str(package or "").strip(),
            str(app.get("patch_package") or "").strip(),
            str(app.get("name") or "").strip(),
        }
        match_keys.discard("")
        if packages.intersection(match_keys):
            add(source.get("repo", ""))

    add(app.get("patches_repo", ""))
    return repos


def prune_old_releases(app_id, keep_count=3):
    if keep_count <= 0: return
    ok, out = run_cmd(["gh", "release", "list", "--limit", "30", "--json", "tagName,isDraft"])
    if not ok or not out: return
    try:
        releases = [r for r in json.loads(out) if not r.get("isDraft") and r.get("tagName", "").startswith(f"{app_id}-")]
        if len(releases) > keep_count:
            to_delete = releases[keep_count:]
            for rel in to_delete:
                tag = rel.get("tagName")
                print(f"Pruning old release: {tag}")
                run_cmd(["gh", "release", "delete", tag, "--yes", "--cleanup-tag"])
    except Exception as e:
        print(f"Warning: Failed to prune old releases: {e}")

def load_patch_metadata_fallback(repo, tag, filter_candidates):
    """Read the published patches-list.json when CLI listing cannot enumerate a package."""
    candidates = [
        f"https://raw.githubusercontent.com/{repo}/{tag}/patches-list.json",
        f"https://raw.githubusercontent.com/{repo}/{tag}/patches-bundle.json",
        f"https://raw.githubusercontent.com/{repo}/main/patches-list.json",
    ]
    data = None
    for url in candidates:
        try:
            response = requests.get(url, timeout=30, headers={"User-Agent": "AutoPatcher-Engine/1.0"})
            if response.status_code != 200:
                continue
            parsed = response.json()
            if isinstance(parsed, dict) and isinstance(parsed.get("patches"), list):
                data = parsed
                break
        except Exception:
            continue
    if not data:
        return None

    normalized_filters = {
        str(value or "").strip().lower()
        for value in (filter_candidates or [])
        if str(value or "").strip()
    }
    for filter_value in filter_candidates or []:
        wanted = str(filter_value or "").strip().lower()
        if not wanted:
            continue

        matched_patches = []
        versions = []
        seen_versions = set()

        for patch in data.get("patches", []):
            if not isinstance(patch, dict):
                continue
            compatible = patch.get("compatiblePackages")
            if isinstance(compatible, dict):
                compatible = list(compatible.values())
            if not isinstance(compatible, list):
                continue

            matched_packages = []
            for package in compatible:
                if not isinstance(package, dict):
                    continue
                package_name = str(package.get("packageName") or "").strip()
                package_title = str(package.get("name") or "").strip()
                if wanted in {package_name.lower(), package_title.lower()}:
                    matched_packages.append(package)

            if not matched_packages:
                continue

            entry = {
                "name": str(patch.get("name") or "").strip(),
                "description": str(patch.get("description") or "").strip(),
                "enabled": bool(patch.get("default", False)),
                "options": patch.get("options") if isinstance(patch.get("options"), list) else [],
            }
            if not entry["name"]:
                continue
            matched_patches.append(entry)

            for package in matched_packages:
                targets = package.get("targets")
                if not isinstance(targets, list):
                    continue
                for target in targets:
                    if not isinstance(target, dict):
                        continue
                    version = str(target.get("version") or "").strip()
                    if version and version not in seen_versions:
                        seen_versions.add(version)
                        versions.append(version)

        if matched_patches and versions:
            return {
                "filter": filter_value,
                "versions": versions,
                "patches": matched_patches,
            }
    return None


def parse_versions(output, package_name):
    versions = []
    capture = False
    for line in output.split('\n'):
        clean_line = line.strip()
        if f"Package name: {package_name}" in clean_line:
            capture = True
            continue
        if capture and clean_line.startswith("Package name:"): break
        if capture and clean_line.startswith("Most common"): continue
        if capture and re.match(r'^\s*([\w\.\-]+)\s+\(\d+\s+patches\)', clean_line):
            versions.append(clean_line.split()[0])
    return versions

def parse_patches(output):
    patches = []
    current_patch = {}
    current_option = {}
    in_options = False

    def push_option():
        nonlocal current_option
        if current_option and "key" in current_option:
            current_patch.setdefault("options", []).append({
                "key": current_option.get("key", ""),
                "title": current_option.get("title", ""),
                "description": current_option.get("description", ""),
                "default": current_option.get("default", "")
            })
        current_option = {}

    def push_patch():
        nonlocal current_patch
        if current_patch and "name" in current_patch:
            push_option()
            current_patch.setdefault("options", [])
            current_patch.setdefault("description", "")
            current_patch.setdefault("enabled", True)
            patches.append(current_patch)
        current_patch = {}

    for line in output.split('\n'):
        clean_line = re.sub(r'^(?:INFO:\s*|\[INFO\]\s*)', '', line.strip())
        if not clean_line: continue

        if clean_line.startswith('Index:'):
            push_patch()
            current_patch = {"options": []}
            in_options = False
            continue

        if clean_line.startswith('Name:'):
            current_patch['name'] = clean_line.split(':', 1)[1].strip().rstrip('.')
            in_options = False
        elif clean_line.startswith('Description:'):
            current_patch['description'] = clean_line.split(':', 1)[1].strip().rstrip('.')
            in_options = False
        elif clean_line.startswith('Enabled:'):
            val = clean_line.split(':', 1)[1].strip().rstrip('.')
            current_patch['enabled'] = val.lower() == 'true'
            in_options = False
        elif clean_line.startswith('Options:'):
            in_options = True
        elif in_options:
            option_line = clean_line.lstrip("- ").strip()
            if option_line.startswith("Key:"):
                push_option()
                current_option['key'] = option_line.split(':', 1)[1].strip().rstrip('.')
            elif option_line.startswith("Title:"):
                current_option['title'] = option_line.split(':', 1)[1].strip().rstrip('.')
            elif option_line.startswith("Description:"):
                current_option['description'] = option_line.split(':', 1)[1].strip().rstrip('.')
            elif option_line.startswith("Default:"):
                current_option['default'] = option_line.split(':', 1)[1].strip().rstrip('.')

    push_patch()
    return patches

def _clean_app_workspace(app_id):
    root = os.path.join(".work", app_id)
    if os.path.exists(root):
        shutil.rmtree(root)
    stock_dir = os.path.join(root, "stock")
    patched_dir = os.path.join(root, "patched")
    os.makedirs(stock_dir, exist_ok=True)
    os.makedirs(patched_dir, exist_ok=True)
    return root, stock_dir, patched_dir


def _update_info_metadata(info_path, **updates):
    """Merge structured build/acquisition state into an app info catalog."""
    try:
        data = {}
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        data.update(updates)
        os.makedirs(os.path.dirname(info_path), exist_ok=True)
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        print(f"Warning: Failed to update {info_path}: {exc}")


ICON_DIR = os.path.join("docs", "catalog", "icons")
ICON_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".avif")


def _looks_like_android_package(value):
    return bool(re.match(r"^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)+$", str(value or "").strip(), re.I))


def _icon_safe_id(app_id):
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(app_id or "app")).strip(".-")
    return value or "app"


def _icon_normalize_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _play_headers():
    return {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _play_meta_image(soup):
    selectors = [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[itemprop="image"]', "content"),
    ]
    for selector, attr in selectors:
        node = soup.select_one(selector)
        if node and node.get(attr):
            return node.get(attr).strip()
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                image = item.get("image")
                if isinstance(image, str) and image.startswith("http"):
                    return image
                if isinstance(image, list):
                    for image_item in image:
                        if isinstance(image_item, str) and image_item.startswith("http"):
                            return image_item
    return ""


def _play_detail(session, package_name, expected_query=""):
    url = "https://play.google.com/store/apps/details?id=" + urllib.parse.quote(package_name, safe="._-") + "&hl=en&gl=US"
    try:
        response = session.get(url, headers=_play_headers(), timeout=20)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        title_node = soup.select_one('meta[property="og:title"]')
        title = (title_node.get("content") if title_node else "") or (soup.title.get_text(" ", strip=True) if soup.title else "")
        icon_url = _play_meta_image(soup)
        if not icon_url:
            return None
        query_text = _icon_normalize_text(expected_query)
        title_text = _icon_normalize_text(title)
        score = sum(1 for token in query_text.split() if len(token) >= 2 and token in title_text)
        return {"package": package_name, "title": title, "icon_url": icon_url, "score": score}
    except Exception as exc:
        print(f"Warning: Play Store detail lookup failed for {package_name}: {exc}")
        return None


def _find_play_store_icon(app_id, app):
    session = requests.Session()
    package_value = str(
        app.get("play_store_package")
        or app.get("android_package")
        or app.get("package")
        or ""
    ).strip()
    explicit_url = str(app.get("play_store_url") or "").strip()

    if explicit_url:
        parsed = urllib.parse.urlparse(explicit_url)
        pkg = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
        if pkg:
            direct = _play_detail(session, pkg, app.get("name") or app_id)
            if direct:
                return direct

    if _looks_like_android_package(package_value):
        direct = _play_detail(session, package_value, app.get("name") or app_id)
        if direct:
            return direct
        # An explicit Android package is authoritative. Do not fall back to
        # an unrelated search result with a similar app name.
        return None

    queries = []
    for raw in (app.get("name"), str(app_id).replace("-", " "), package_value):
        q = str(raw or "").strip()
        if q and not _looks_like_android_package(q) and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)

    for query in queries:
        search_url = "https://play.google.com/store/search?c=apps&q=" + urllib.parse.quote_plus(query) + "&hl=en&gl=US"
        try:
            response = session.get(search_url, headers=_play_headers(), timeout=20)
            if response.status_code != 200:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            package_ids = []
            seen = set()
            for link in soup.select('a[href*="/store/apps/details?id="]'):
                href = link.get("href", "")
                pkg = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("id", [""])[0]
                if pkg and pkg not in seen:
                    seen.add(pkg)
                    package_ids.append(pkg)
            if not package_ids:
                package_ids = list(dict.fromkeys(re.findall(r"/store/apps/details\\?id=([A-Za-z0-9._-]+)", response.text)))
            candidates = []
            for pkg in package_ids[:12]:
                candidate = _play_detail(session, pkg, query)
                if candidate:
                    candidates.append(candidate)
            if candidates:
                candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
                best = candidates[0]
                if best.get("score", 0) > 0:
                    return best
        except Exception as exc:
            print(f"Warning: Play Store search failed for '{query}': {exc}")
    return None


def _cache_icon(app_id, icon_url, package_name=""):
    if not icon_url or not icon_url.startswith("http"):
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    try:
        response = requests.get(icon_url, headers=_play_headers(), timeout=30)
        if response.status_code != 200:
            return None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            return None
        data = response.content
        if not data or len(data) > 4 * 1024 * 1024:
            return None
        ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/webp": ".webp",
            "image/avif": ".avif",
        }.get(content_type, ".png")
        safe_id = _icon_safe_id(app_id)
        for old_ext in ICON_EXTENSIONS:
            old = os.path.join(ICON_DIR, safe_id + old_ext)
            if not old.endswith(ext) and os.path.exists(old):
                os.remove(old)
        path = os.path.join(ICON_DIR, safe_id + ext)
        with open(path, "wb") as fh:
            fh.write(data)
        meta_path = os.path.join(ICON_DIR, safe_id + ".icon-meta.json")
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"package": package_name or "", "source": "google_play"}, fh, indent=2)
        return "catalog/icons/" + os.path.basename(path)
    except Exception as exc:
        print(f"Warning: Failed to cache icon for {app_id}: {exc}")
        return None


def _android_tool(name):
    explicit = os.environ.get(name.upper())
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which(name)
    if found:
        return found
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        build_tools = os.path.join(android_home, "build-tools")
        if os.path.isdir(build_tools):
            versions = sorted(os.listdir(build_tools), reverse=True)
            for version in versions:
                candidate = os.path.join(build_tools, version, name)
                if os.path.isfile(candidate):
                    return candidate
    return None


def extract_apk_icon(apk_path, app_id):
    """Extract a concrete launcher image from the exact validated APK."""
    tool = _android_tool("aapt2") or _android_tool("aapt")
    if not tool or not apk_path or not os.path.isfile(apk_path):
        return None
    ok, output = run_cmd([tool, "dump", "badging", apk_path], silent=True)
    if not ok:
        return None
    icon_paths = []
    for pattern in (
        r"application:\s+.*?icon='([^']+)'",
        r"application-icon-\d+['\"]?\s*[:=]\s*['\"]?([^'\"\s]+)",
    ):
        icon_paths.extend(re.findall(pattern, output or "", re.I))
    icon_paths = list(dict.fromkeys(icon_paths))
    try:
        with zipfile.ZipFile(apk_path) as archive:
            names = archive.namelist()
            # Prefer the launcher path reported by aapt, then fall back to
            # the largest concrete PNG/WebP under res/mipmap*.
            candidates = []
            for name in icon_paths:
                clean = str(name).lstrip("/")
                if clean in names and not clean.lower().endswith(".xml"):
                    candidates.append(clean)
            if not candidates:
                candidates = [
                    n for n in names
                    if n.lower().startswith(("res/mipmap", "res/drawable"))
                    and n.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
                ]
                candidates.sort(key=lambda n: archive.getinfo(n).file_size, reverse=True)
                candidates = candidates[:1]
            if not candidates:
                return None
            chosen = candidates[0]
            data = archive.read(chosen)
    except Exception as exc:
        print(f"Warning: APK icon extraction failed for {app_id}: {exc}")
        return None

    ext = os.path.splitext(chosen)[1].lower()
    if ext not in (".png", ".webp", ".jpg", ".jpeg"):
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    safe_id = _icon_safe_id(app_id)
    path = os.path.join(ICON_DIR, safe_id + "-apk" + ext)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        return "catalog/icons/" + os.path.basename(path)
    except Exception as exc:
        print(f"Warning: Failed to save APK icon for {app_id}: {exc}")
        return None


def _existing_cached_icon(app_id, expected_package=""):
    safe_id = _icon_safe_id(app_id)
    os.makedirs(ICON_DIR, exist_ok=True)
    if expected_package:
        meta_path = os.path.join(ICON_DIR, safe_id + ".icon-meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            if str(meta.get("package") or "").strip() != str(expected_package).strip():
                return None
        except Exception:
            return None
    for ext in ICON_EXTENSIONS:
        path = os.path.join(ICON_DIR, safe_id + ext)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return "catalog/icons/" + os.path.basename(path)
    return None


def ensure_app_icon(app_id, app):
    expected_package = str(
        app.get("android_package")
        or app.get("play_store_package")
        or ""
    ).strip()
    cached = _existing_cached_icon(app_id, expected_package)
    if cached:
        return cached, "cache"
    result = _find_play_store_icon(app_id, app)
    if not result:
        print(f"Icon lookup: no reliable Google Play icon found for {app_id}.")
        return "", ""
    cached_path = _cache_icon(
        app_id,
        result.get("icon_url", ""),
        result.get("package") or expected_package,
    )
    if not cached_path:
        return "", ""
    print(f"Icon lookup: {app_id} -> {result.get('package')} ({result.get('title', '').strip()})")
    return cached_path, "google_play"


def _download_url(url, destination):
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AutoPatcher-Engine/1.0", "Accept": "*/*"}
    )
    with urllib.request.urlopen(req, timeout=60) as response, open(destination, "wb") as fh:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def _stock_candidates(stock_dir):
    return sorted(
        [
            os.path.join(stock_dir, name)
            for name in os.listdir(stock_dir)
            if name.lower().endswith((".apk", ".apkm", ".apks", ".xapk"))
        ]
    )


APK_BUNDLE_SUFFIXES = {".apkm", ".apks", ".xapk"}


def _ensure_apkeditor() -> str:
    """Download APKEditor lazily only when a split bundle needs normalization."""
    tool_dir = os.path.join(".work", "tools")
    os.makedirs(tool_dir, exist_ok=True)
    cached = glob.glob(os.path.join(tool_dir, "*.jar"))
    for candidate in cached:
        if "apkeditor" in os.path.basename(candidate).lower():
            return candidate

    api_url = "https://api.github.com/repos/REAndroid/APKEditor/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Morphe-Auto-Patcher/1.0",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(api_url, headers=headers, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"Unable to discover APKEditor release: HTTP {response.status_code}")
    release = response.json()
    assets = release.get("assets") or []
    jar_asset = next(
        (
            asset for asset in assets
            if str(asset.get("name") or "").lower().endswith(".jar")
            and "sources" not in str(asset.get("name") or "").lower()
        ),
        None,
    )
    if not jar_asset or not jar_asset.get("browser_download_url"):
        raise RuntimeError("Latest APKEditor release does not expose a usable JAR asset")

    destination = os.path.join(tool_dir, "apkeditor.jar")
    _download_url(jar_asset["browser_download_url"], destination)
    if not os.path.isfile(destination) or os.path.getsize(destination) < 1024:
        raise RuntimeError("Downloaded APKEditor JAR is empty or incomplete")
    return destination


def _normalize_stock_artifact(
    app_id: str,
    apk_path: str,
    apk_info,
    package: str,
    version: str,
    apk_arch: str,
):
    """Turn APKM/APKS/XAPK into a single patchable APK without weakening validation."""
    suffix = Path(apk_path).suffix.lower()
    if suffix not in APK_BUNDLE_SUFFIXES:
        return apk_path, apk_info, {}
    # A provider may occasionally label a plain APK with a bundle extension.
    # The validator has already inspected the actual ZIP contents, so do not
    # run APKEditor against a file that is really a single APK.
    if apk_info is not None and getattr(apk_info, "artifact_type", "") == "apk":
        normalized = os.path.join(
            os.path.dirname(apk_path),
            f"{app_id}-stock.apk",
        )
        if os.path.abspath(normalized) != os.path.abspath(apk_path):
            shutil.copy2(apk_path, normalized)
        normalized_info = validate_artifact(
            normalized,
            expected_package=package,
            expected_version=version,
            expected_arch=apk_arch or "auto",
        )
        return normalized, normalized_info, {
            "normalized": False,
            "source_artifact": os.path.basename(apk_path),
            "source_artifact_type": suffix.lstrip("."),
            "normalizer": "content-detected-single-apk",
        }

    source_hash = apk_info.sha256 if apk_info else ""
    source_signers = list(getattr(apk_info, "signer_sha256", []) or [])
    editor = _ensure_apkeditor()
    normalized_unsigned = os.path.join(
        os.path.dirname(apk_path),
        f"{app_id}-normalized-unsigned.apk",
    )
    normalized = os.path.join(
        os.path.dirname(apk_path),
        f"{app_id}-normalized.apk",
    )
    for candidate in (normalized_unsigned, normalized):
        if os.path.exists(candidate):
            os.remove(candidate)

    ok, output = run_cmd([
        "java", "-jar", editor, "merge",
        "-i", apk_path,
        "-o", normalized_unsigned,
        "-clean-meta",
        "-f",
    ])
    if not ok or not os.path.isfile(normalized_unsigned):
        raise RuntimeError(
            f"APKEditor failed to merge {suffix} bundle: {(output or '')[-2000:]}"
        )

    # APKEditor produces the merged package without a usable signature. Sign it
    # with a temporary local key; Morphe will sign the final patched APK later.
    temp_keystore = os.path.join(".work", "tools", "bundle-merge.p12")
    alias = "morphe-merge"
    storepass = "morphe-auto-patcher"
    if not os.path.exists(temp_keystore):
        ok_key, key_out = run_cmd([
            "keytool", "-genkeypair",
            "-storetype", "PKCS12",
            "-keystore", temp_keystore,
            "-storepass", storepass,
            "-keypass", storepass,
            "-alias", alias,
            "-keyalg", "RSA",
            "-keysize", "2048",
            "-validity", "3650",
            "-dname", "CN=Morphe Auto Patcher",
        ])
        if not ok_key:
            raise RuntimeError(f"Unable to create temporary bundle-signing key: {(key_out or '')[-2000:]}")

    apksigner = shutil.which("apksigner") or _android_tool("apksigner")
    if not apksigner:
        raise RuntimeError("apksigner is required to sign the normalized APK")

    ok_sign, sign_out = run_cmd([
        apksigner, "sign",
        "--ks", temp_keystore,
        "--ks-pass", f"pass:{storepass}",
        "--key-pass", f"pass:{storepass}",
        "--ks-key-alias", alias,
        "--out", normalized,
        normalized_unsigned,
    ])
    if not ok_sign or not os.path.isfile(normalized):
        raise RuntimeError(f"Unable to sign normalized APK: {(sign_out or '')[-2000:]}")

    try:
        os.remove(normalized_unsigned)
    except OSError:
        pass
    try:
        os.remove(normalized + ".idsig")
    except OSError:
        pass

    normalized_info = validate_artifact(
        normalized,
        expected_package=package,
        expected_version=version,
        expected_arch=apk_arch or "auto",
    )
    return normalized, normalized_info, {
        "normalized": True,
        "source_artifact": os.path.basename(apk_path),
        "source_artifact_type": suffix.lstrip("."),
        "source_sha256": source_hash,
        "source_signer_sha256": source_signers,
        "normalizer": "APKEditor",
        "normalized_sha256": normalized_info.sha256,
    }


def _validate_stock_file(path, package, version, apk_arch, expected_signer_sha256=None):
    return validate_artifact(
        path,
        expected_package=package,
        expected_version=version,
        expected_arch=apk_arch or "auto",
        expected_signer_sha256=expected_signer_sha256,
    )


def _choose_valid_stock(candidates, package, version, apk_arch, expected_signer_sha256=None):
    valid = []
    errors = []
    for path in candidates:
        try:
            info = _validate_stock_file(path, package, version, apk_arch, expected_signer_sha256)
            valid.append((path, info))
        except ApkValidationError as exc:
            errors.append({"file": os.path.basename(path), "error": str(exc)})

    if not valid:
        return None, None, errors

    wanted = (apk_arch or "auto").lower()
    def score(item):
        arches = {a.lower() for a in item[1].architectures}
        if wanted not in ("", "auto", "automatic") and wanted in arches:
            return 3000
        if wanted not in ("", "auto", "automatic") and "universal" in arches:
            return 2500
        if "universal" in arches:
            return 2000
        if "arm64-v8a" in arches:
            return 1500
        if "armeabi-v7a" in arches:
            return 1000
        return 0

    valid.sort(key=score, reverse=True)
    return valid[0][0], valid[0][1], errors


def resolve_stock_apk(app_id, package, app_version, app_config, expected_signer_sha256=None):
    root, stock_dir, _ = _clean_app_workspace(app_id)
    apk_arch = app_config.get("apk_arch") or "auto"
    attempts = []
    manual_urls = []

    explicit_url = (app_config.get("apk_url") or "").strip()
    if explicit_url:
        explicit_suffix = os.path.splitext(urllib.parse.urlparse(explicit_url).path)[1].lower()
        if explicit_suffix not in (".apk", ".apkm", ".apks", ".xapk"):
            explicit_suffix = ".apk"
        explicit_path = os.path.join(
            stock_dir, f"{app_id}-{app_version or 'explicit'}-url{explicit_suffix}"
        )
        print(f"Trying configured Android artifact URL for {app_id}...")
        try:
            _download_url(explicit_url, explicit_path)
            info = _validate_stock_file(explicit_path, package, app_version, apk_arch, expected_signer_sha256)
            return explicit_path, info, {"status": "downloaded", "provider": "configured_url", "target_version": app_version, "package": package, "apk_arch": apk_arch, "manual_urls": [explicit_url], "attempts": attempts}
        except Exception as exc:
            attempts.append({"provider": "configured_url", "status": "failed", "url": explicit_url, "error": str(exc)})
            if os.path.exists(explicit_path):
                os.remove(explicit_path)

    stock_release_tag = f"stock-{app_id}"
    if release_exists(stock_release_tag):
        print(f"Found stock release: {stock_release_tag}. Downloading exact candidate(s)...")
        ok, release_out = run_cmd([
            "gh", "release", "download", stock_release_tag,
            "--pattern", "*.apk",
            "--pattern", "*.apkm",
            "--pattern", "*.apks",
            "--pattern", "*.xapk",
            "-D", stock_dir,
            "--clobber"
        ])
        if ok:
            apk_path, info, validation_errors = _choose_valid_stock(
                _stock_candidates(stock_dir), package, app_version, apk_arch, expected_signer_sha256
            )
            attempts.extend([{
                "provider": "stock_release", "status": "rejected", **err
            } for err in validation_errors])
            if apk_path:
                print(f"Valid stock APK found in {stock_release_tag}: {os.path.basename(apk_path)}")
                return apk_path, info, {
                    "status": "downloaded",
                    "provider": "stock_release",
                    "target_version": app_version,
                    "package": package,
                    "apk_arch": apk_arch,
                    "manual_urls": [f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}/releases/tag/{stock_release_tag}"],
                    "attempts": attempts,
                }
        else:
            attempts.append({"provider": "stock_release", "status": "download_failed", "error": release_out})

    result = acquire_from_providers(
        app_id=app_id,
        target_version=app_version,
        preferred_arch=apk_arch,
        app_config=app_config,
        destination_dir=stock_dir,
    )
    attempts.extend(result.attempts)
    manual_urls.extend(result.manual_urls)

    if result.path:
        try:
            info = _validate_stock_file(result.path, package, app_version, apk_arch, expected_signer_sha256)
            return result.path, info, {
                "status": "downloaded",
                "provider": result.candidate.provider if result.candidate else "automatic",
                "target_version": app_version,
                "package": package,
                "apk_arch": apk_arch,
                "manual_urls": manual_urls,
                "attempts": attempts,
            }
        except ApkValidationError as exc:
            attempts.append({
                "provider": result.candidate.provider if result.candidate else "automatic",
                "status": "validation_failed",
                "error": str(exc),
            })
            try:
                os.remove(result.path)
            except OSError:
                pass

    return None, None, {
        "status": "manual_required",
        "provider": "",
        "target_version": app_version,
        "package": package,
        "apk_arch": apk_arch,
        "manual_urls": manual_urls,
        "attempts": attempts,
        "error": result.error or "No valid stock APK could be acquired.",
        "workspace": root,
    }

def main():
    print("Starting Auto-Patcher Build...")
    pre_flight_checks()
    has_errors = False

    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found!")
        sys.exit(1)

    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)

    apps = config.get('apps', [])
    if not apps:
        print("No apps configured in config.json. Add sources and apps through the Web UI.")
        sys.exit(0)

    repo_settings = config.get('settings', {})
    auto_prune = repo_settings.get('auto_prune', True)
    keep_releases_count = int(repo_settings.get('keep_releases', 3))
    check_interval_hours = int(repo_settings.get('check_interval_hours', 6))
    trigger_policy = repo_settings.get('trigger_policy', 'on_new_patch')
    discord_webhook = os.environ.get("DISCORD_WEBHOOK") or repo_settings.get("discord_webhook", "").strip()

    catalog_dir = "docs/catalog"
    os.makedirs(catalog_dir, exist_ok=True)

    github_event = os.environ.get("GITHUB_EVENT_NAME", "").lower()
    is_scheduled = (github_event == "schedule")
    state_file = os.path.join(catalog_dir, "build_state.json")
    last_run = 0

    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as sf:
                last_run = json.load(sf).get('last_run', 0)
        except Exception:
            last_run = 0

    now = int(time.time())
    if is_scheduled:
        if (now - last_run) < (check_interval_hours * 3600 - 300):
            print(f"Interval gate: Next scheduled run allowed in {round((check_interval_hours * 3600 - (now - last_run)) / 60)} minutes. Exiting.")
            sys.exit(0)

    try:
        cli_jar = find_cli_jar()
        print(f"Found CLI JAR: {cli_jar}")
    except FileNotFoundError as e:
        print(e)
        sys.exit(1)

    keystore_arg = ["--keystore", "ks.bks"] if os.path.exists("ks.bks") else []
    if keystore_arg: print("Found ks.bks, will use custom signing key.")
    else: print("No ks.bks found. Morphe CLI will use temporary signing key.")

    target_app_filter = os.environ.get('TARGET_APP', '').strip()
    force_build_env = os.environ.get('FORCE_BUILD', '').lower() in ['true', '1']

    for app in apps:
        app_id = app['id']
        info_path = os.path.join(catalog_dir, f"{app_id}.info.json")
        _update_info_metadata(
            info_path,
            build_status={"status": "running", "stage": "patch_source", "updated_at": now}
        )
        icon_path, icon_source = ensure_app_icon(app_id, app)
        if icon_path:
            _update_info_metadata(info_path, icon_url=icon_path, icon_source=icon_source)

        # Skip apps with auto_patch turned off during scheduled runs
        auto_patch = app.get('auto_patch', True)
        if is_scheduled and not auto_patch:
            print(f"Skipping {app_id}: Auto-patch toggle is disabled for this app.")
            continue

        if target_app_filter and app_id != target_app_filter: continue

        package = app['package']
        patch_filter = str(app.get('patch_package') or package).strip()
        android_package = str(app.get('android_package') or "").strip()
        if not android_package and _looks_like_android_package(package):
            android_package = package
        if not android_package:
            play_meta = _find_play_store_icon(app_id, app)
            if play_meta and play_meta.get("package"):
                android_package = str(play_meta["package"]).strip()
                print(f"Resolved Android package for {app_id}: {android_package}")
        arch = app.get('arch') or repo_settings.get('default_arch', 'arm64-v8a')
        apk_arch = app.get('apk_arch') or "auto"
        allow_prerelease = app.get('prerelease', False)
        source_repos = patch_source_candidates(app, patch_filter, config)

        if not source_repos:
            print(f"Failed: no patch sources configured for {app_id}")
            has_errors = True
            continue

        if is_scheduled and trigger_policy == "periodic_rebuild":
            force = True
        else:
            force = app.get('force', False) or force_build_env

        print(f"\n{'='*40}\nProcessing App: {app_id}\n{'='*40}")

        tag_name = None
        patches_repo = None
        mpp_file = None
        compatible_versions = []
        patches_list = []
        source_attempts = []
        selected_patch_filter = patch_filter

        filter_candidates = []
        for value in (patch_filter, app.get("name"), android_package, package):
            value = str(value or "").strip()
            if value and value not in filter_candidates:
                filter_candidates.append(value)

        for idx, candidate_repo in enumerate(source_repos):
            candidate_tag = get_latest_tag(candidate_repo, allow_prerelease)
            if not candidate_tag:
                source_attempts.append({"repo": candidate_repo, "status": "no_release"})
                continue

            candidate_mpp = f"{app_id}-patches-{idx}.mpp"
            dl_cmd = [
                "gh", "release", "download", candidate_tag,
                "--pattern", "*.mpp",
                "-R", candidate_repo,
                "-O", candidate_mpp,
                "--clobber"
            ]
            ok_dl, dl_out = run_cmd(dl_cmd)
            if not ok_dl or not os.path.exists(candidate_mpp):
                source_attempts.append({"repo": candidate_repo, "status": "download_failed", "error": dl_out})
                continue

            compatible_for_filter = None
            patches_for_filter = None
            successful_filter = None

            for filter_value in filter_candidates:
                ok_v, versions_out = run_cmd([
                    "java", "-jar", cli_jar, "list-versions",
                    "--patches", candidate_mpp, "-f", filter_value
                ])
                candidate_versions = parse_versions(versions_out, filter_value) if ok_v else []

                ok_p, patches_out = run_cmd([
                    "java", "-jar", cli_jar, "list-patches", "-o",
                    "--patches", candidate_mpp, "-f", filter_value
                ])
                candidate_patches = parse_patches(patches_out) if ok_p else []

                if candidate_patches and candidate_versions:
                    compatible_for_filter = candidate_versions
                    patches_for_filter = candidate_patches
                    successful_filter = filter_value
                    break

            if not compatible_for_filter or not patches_for_filter:
                fallback = load_patch_metadata_fallback(candidate_repo, candidate_tag, filter_candidates)
                if fallback:
                    compatible_for_filter = fallback["versions"]
                    patches_for_filter = fallback["patches"]
                    successful_filter = fallback["filter"]
                    print(
                        f"CLI package listing unavailable for {app_id}; "
                        f"using {candidate_repo} patches-list.json metadata for '{successful_filter}'."
                    )

            if not compatible_for_filter or not patches_for_filter:
                source_attempts.append({
                    "repo": candidate_repo,
                    "status": "incompatible",
                    "tag": candidate_tag,
                    "filters_tried": filter_candidates
                })
                if os.path.exists(candidate_mpp):
                    os.remove(candidate_mpp)
                continue

            tag_name = candidate_tag
            patches_repo = candidate_repo
            mpp_file = candidate_mpp
            compatible_versions = compatible_for_filter
            patches_list = patches_for_filter
            selected_patch_filter = successful_filter
            print(f"Selected patch source for {app_id}: {candidate_repo} @ {candidate_tag} using filter '{successful_filter}'")
            break

        if not patches_list or not mpp_file or not patches_repo:
            raw_file = os.path.join(catalog_dir, f"{app_id}.raw.txt")
            with open(raw_file, 'w') as f:
                f.write(json.dumps({"source_attempts": source_attempts}, indent=2))
            _update_info_metadata(
                info_path,
                patch_source_attempts=source_attempts,
                patch_source_candidates=source_repos,
                build_status={
                    "status": "failed",
                    "stage": "patch_source",
                    "reason": "No compatible patch source found.",
                    "updated_at": now,
                },
            )
            print(f"Error: No compatible patch source found for {app_id}")
            has_errors = True
            continue

        release_tag = f"{app_id}-{tag_name}"
        if release_exists(release_tag) and not force:
            print(f"Release {release_tag} already exists. Skipping.")
            continue
        elif release_exists(release_tag) and force:
            print(f"Force flag active. Deleting existing release {release_tag}...")
            run_cmd(["gh", "release", "delete", release_tag, "--yes", "--cleanup-tag"])
            
        with open(os.path.join(catalog_dir, f"{app_id}.json"), 'w') as f:
            json.dump(patches_list, f, indent=2)

        app_version = app.get('version', '') or (compatible_versions[0] if compatible_versions else '')
        query = f"{app_id} {app_version}".strip()
        apkmirror_url = f"https://www.apkmirror.com/?post_type=app_release&searchtype=apk&s={urllib.parse.quote(query)}"
        
        prior_signers = []
        if os.path.exists(info_path):
            try:
                with open(info_path, "r") as inf:
                    prior_info = json.load(inf)
                prior_signers = (prior_info.get("stock_apk") or {}).get("signer_sha256") or []
            except Exception:
                prior_signers = []

        with open(info_path, 'w') as f:
            json.dump({
                "patch_tag": tag_name,
                "recommended_version": app_version,
                "compatible_versions": compatible_versions,
                "apkmirror_url": apkmirror_url,
                "patch_source": patches_repo,
                "patch_filter": selected_patch_filter,
                "android_package": android_package or package,
                "patch_source_attempts": source_attempts,
                "patch_source_candidates": source_repos,
                "stock_signer_sha256": prior_signers,
                "icon_url": icon_path,
                "icon_source": icon_source,
                "build_status": {"status": "running", "stage": "apk_acquisition", "updated_at": now}
            }, f, indent=2)

        apk_path, apk_info, acquisition = resolve_stock_apk(
            app_id, android_package or package, app_version, app, expected_signer_sha256=prior_signers
        )

        if apk_path and apk_info:
            try:
                apk_path, apk_info, normalization = _normalize_stock_artifact(
                    app_id,
                    apk_path,
                    apk_info,
                    android_package or package,
                    app_version,
                    apk_arch,
                )
                if normalization:
                    acquisition["normalization"] = normalization
                    acquisition["status"] = "downloaded_and_normalized"
                    print(
                        f"Normalized {normalization['source_artifact_type'].upper()} "
                        f"stock artifact for {app_id} with APKEditor."
                    )
            except Exception as exc:
                acquisition.setdefault("attempts", []).append({
                    "provider": "normalizer",
                    "status": "normalization_failed",
                    "error": str(exc),
                })
                print(f"Error: Stock bundle normalization failed for {app_id}: {exc}")
                try:
                    if apk_path and os.path.exists(apk_path):
                        os.remove(apk_path)
                except OSError:
                    pass
                apk_path = None
                apk_info = None

        if apk_path and apk_info and not icon_path:
            apk_icon_path = extract_apk_icon(apk_path, app_id)
            if apk_icon_path:
                icon_path, icon_source = apk_icon_path, "apk"
                _update_info_metadata(info_path, icon_url=icon_path, icon_source=icon_source)

        try:
            existing_info = {}
            if os.path.exists(info_path):
                with open(info_path, "r") as inf:
                    existing_info = json.load(inf)
            existing_info["apk_acquisition"] = acquisition
            existing_info["stock_apk"] = apk_info.to_dict() if apk_info else None
            existing_info["stock_signer_sha256"] = (
                (acquisition.get("normalization") or {}).get("source_signer_sha256")
                or (apk_info.signer_sha256 if apk_info else existing_info.get("stock_signer_sha256", []))
            )
            with open(info_path, "w") as inf:
                json.dump(existing_info, inf, indent=2)
        except Exception as exc:
            print(f"Warning: Failed to update acquisition metadata: {exc}")

        if not apk_path:
            _update_info_metadata(
                info_path,
                build_status={
                    "status": "failed",
                    "stage": "apk_acquisition",
                    "reason": acquisition.get("error") or "No valid stock APK could be acquired.",
                    "updated_at": now,
                },
            )
            print(f"⚠️ ACTION REQUIRED: Manual stock Android artifact required for '{app_id}'")
            manual_link = (acquisition.get("manual_urls") or [apkmirror_url])[0]
            repository_slug = str(os.environ.get("GITHUB_REPOSITORY", "")).strip("/")
            owner, separator, repository_name = repository_slug.partition("/")
            stock_release_url = (
                f"https://github.com/{repository_slug}/releases/tag/stock-{app_id}"
                if separator else ""
            )
            # The app-specific Patch Manager route opens the existing upload UI.
            # PATCH_MANAGER_URL can override the standard GitHub Pages URL when
            # the repository is served from a custom domain.
            web_ui_base = str(os.environ.get("PATCH_MANAGER_URL", "")).strip()
            if not web_ui_base and owner and repository_name:
                web_ui_base = f"https://{owner}.github.io/{repository_name}/"
            upload_ui_url = (
                f"{web_ui_base.rstrip('/')}/#/upload/{urllib.parse.quote(app_id, safe='')}"
                if web_ui_base else ""
            )
            reason = acquisition.get("error") or "Automatic stock APK acquisition failed."
            if discord_webhook:
                send_discord_webhook(
                    discord_webhook,
                    title=f"⚠️ Manual APK Required: {app_id.capitalize()}",
                    description=(
                        "Automatic download failed for the exact requested version. "
                        "Please download the untouched original APK/APKM/APKS/XAPK, upload it to "
                        f"stock-{app_id}, then retry the build."
                    ),
                    color=0xF59E0B,
                    fields=[
                        {"name": "Target Version", "value": app_version or "Latest", "inline": True},
                        {"name": "Architecture Preference", "value": apk_arch, "inline": True},
                        {"name": "Download Exact APK", "value": f"[Open download page]({manual_link})", "inline": False},
                        {"name": "Upload through Patch Manager", "value": f"[Open {app_id} upload page]({upload_ui_url})" if upload_ui_url else f"[Open stock-{app_id} release]({stock_release_url})", "inline": False},
                        {"name": "GitHub Release fallback", "value": f"[Open stock-{app_id} release]({stock_release_url})" if stock_release_url else "Not available", "inline": False},
                        {"name": "Reason", "value": reason[:1024], "inline": False}
                    ]
                )
            has_errors = True
            continue

        _update_info_metadata(
            info_path,
            build_status={"status": "running", "stage": "patching", "updated_at": now},
        )
        output_apk = os.path.join(".work", app_id, "patched", f"{app_id}-patched.apk")
        os.makedirs(os.path.dirname(output_apk), exist_ok=True)
        if os.path.exists(output_apk): os.remove(output_apk)

        patch_cmd = [
            "java", "-jar", cli_jar, "patch",
            "-p", mpp_file,
            "-o", output_apk,
            "--striplibs", arch,
            "--continue-on-error"
        ]
        patch_cmd.extend(keystore_arg)
        if force: patch_cmd.append("--force")

        for p_name in app.get('enable', []): patch_cmd.extend(["-e", p_name])
        for p_name in app.get('disable', []): patch_cmd.extend(["-d", p_name])

        for p_name, opts in app.get('options', {}).items():
            for key, val in opts.items():
                val_str = str(val).lower() if isinstance(val, bool) else str(val)
                patch_cmd.extend(["-O", f"{p_name}:{key}={val_str}"])

        patch_cmd.append(apk_path)

        print("Executing patch command...")
        ok, patch_out = run_cmd(patch_cmd)

        if not ok or not os.path.exists(output_apk):
            _update_info_metadata(
                info_path,
                build_status={
                    "status": "failed",
                    "stage": "patching",
                    "reason": "Morphe patch command failed or produced no APK.",
                    "updated_at": now,
                },
            )
            print(f"Error: Patching failed for {app_id}!")
            with open(f"{app_id}-patch-error.log", 'w') as f: f.write(patch_out)
            has_errors = True
            continue

        _update_info_metadata(
            info_path,
            build_status={"status": "running", "stage": "output_validation", "updated_at": now},
        )
        try:
            expected_output_packages = [android_package or package]
            if "Clone app" in (app.get("enable") or []):
                expected_output_packages.append(f"app.morphe.android.{app_id}")
            patched_info = validate_apk(
                output_apk,
                expected_package=android_package or package,
                expected_version=app_version,
                expected_arch=arch,
                allowed_packages=expected_output_packages,
            )
        except ApkValidationError as exc:
            _update_info_metadata(
                info_path,
                build_status={
                    "status": "failed",
                    "stage": "output_validation",
                    "reason": str(exc),
                    "updated_at": now,
                },
            )
            with open(f"{app_id}-output-validation-error.log", 'w') as f: f.write(str(exc))
            print(f"Error: Patched APK validation failed for {app_id}: {exc}")
            has_errors = True
            continue

        try:
            _update_info_metadata(info_path, patched_apk=patched_info.to_dict())
        except Exception:
            pass

        _update_info_metadata(
            info_path,
            build_status={"status": "running", "stage": "publishing", "updated_at": now},
        )
        print(f"Publishing release: {release_tag}")
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as tf:
            tf.write(f"Auto-patched {app_id}\n\n- **Patch:** {tag_name}\n- **App Version:** {app_version or 'Auto'}\n- **Architecture:** {arch}\n")
            notes_file = tf.name

        try:
            create_cmd = [
                "gh", "release", "create", release_tag,
                output_apk,
                "--title", f"{app_id} {tag_name}",
                "--notes-file", notes_file
            ]
            ok_rel, rel_out = run_cmd(create_cmd)
            if not ok_rel:
                _update_info_metadata(
                    info_path,
                    build_status={
                        "status": "failed",
                        "stage": "publishing",
                        "reason": rel_out or "GitHub release creation failed.",
                        "updated_at": now,
                    },
                )
                has_errors = True
            else:
                _update_info_metadata(
                    info_path,
                    build_status={
                        "status": "success",
                        "stage": "completed",
                        "release_tag": release_tag,
                        "updated_at": now,
                    },
                )
                print(f"✅ Release {release_tag} published successfully!")
                if auto_prune:
                    prune_old_releases(app_id, keep_releases_count)
                if discord_webhook:
                    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")
                    rel_link = f"https://github.com/{repo_slug}/releases/tag/{release_tag}" if repo_slug else ""
                    send_discord_webhook(
                        discord_webhook,
                        title=f"🎉 Successfully Patched: {app_id.capitalize()}",
                        description=f"New build **{release_tag}** is published and ready to install!",
                        color=0x22C55E,
                        fields=[
                            {"name": "Version", "value": app_version or "Auto", "inline": True},
                            {"name": "Architecture", "value": arch, "inline": True},
                            {"name": "Download Link", "value": f"[View GitHub Release]({rel_link})" if rel_link else release_tag, "inline": False}
                        ]
                    )
        finally:
            if os.path.exists(notes_file): os.remove(notes_file)

    try:
        with open(state_file, 'w') as sf:
            json.dump({"last_run": now}, sf, indent=2)
    except Exception as e:
        print(f"Warning: Failed to save build state timestamp: {e}")

    if has_errors:
        print("\nBuild finished with errors or missing stock APKs.")
        sys.exit(1)
    else:
        print("\n🎉 Build finished successfully!")

if __name__ == "__main__":
    main()
