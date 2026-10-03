import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

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

def resolve_stock_apk(app_id, package, app_version):
    os.makedirs("apks", exist_ok=True)

    def find_local():
        if app_version:
            m = glob.glob(f"apks/{app_id}-{app_version}.apk*") + glob.glob(f"apks/{app_id}-{app_version}.apkm*")
            if m: return m[0]
            m = glob.glob(f"apks/{package}*{app_version}*.apk*") + glob.glob(f"apks/{package}*{app_version}*.apkm*")
            if m: return m[0]
        m = glob.glob(f"apks/{app_id}.apk*") + glob.glob(f"apks/{app_id}.apkm*")
        if m: return m[0]
        m = glob.glob(f"apks/{package}*.apk*") + glob.glob(f"apks/{package}*.apkm*")
        if m: return m[0]
        return None

    local_file = find_local()
    if local_file: return local_file

    stock_release_tag = f"stock-{app_id}"
    if release_exists(stock_release_tag):
        print(f"Found remote drop-bucket release: {stock_release_tag}. Downloading...")
        dl_cmd = ["gh", "release", "download", stock_release_tag, "--pattern", "*.apk*", "--pattern", "*.apkm*", "-D", "apks/", "--clobber"]
        ok, _ = run_cmd(dl_cmd)
        if ok:
            downloaded = find_local()
            if downloaded: return downloaded
            matching_apks = glob.glob(f"apks/{app_id}*") + glob.glob(f"apks/{package}*")
            matching_apks = [f for f in matching_apks if f.endswith(('.apk', '.apkm'))]
            if matching_apks:
                matching_apks.sort(key=os.path.getmtime, reverse=True)
                return matching_apks[0]

    return None

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

        # Skip apps with auto_patch turned off during scheduled runs
        auto_patch = app.get('auto_patch', True)
        if is_scheduled and not auto_patch:
            print(f"Skipping {app_id}: Auto-patch toggle is disabled for this app.")
            continue

        if target_app_filter and app_id != target_app_filter: continue

        patches_repo = app['patches_repo']
        package = app['package']
        arch = app.get('arch') or repo_settings.get('default_arch', 'arm64-v8a')
        allow_prerelease = app.get('prerelease', False)

        if is_scheduled and trigger_policy == "periodic_rebuild":
            force = True
        else:
            force = app.get('force', False) or force_build_env

        print(f"\n{'='*40}\nProcessing App: {app_id}\n{'='*40}")

        tag_name = get_latest_tag(patches_repo, allow_prerelease)
        if not tag_name:
            print(f"Failed to get release tag for {patches_repo}")
            has_errors = True
            continue

        release_tag = f"{app_id}-{tag_name}"
        if release_exists(release_tag) and not force:
            print(f"Release {release_tag} already exists. Skipping.")
            continue
        elif release_exists(release_tag) and force:
            print(f"Force flag active. Deleting existing release {release_tag}...")
            run_cmd(["gh", "release", "delete", release_tag, "--yes", "--cleanup-tag"])

        mpp_file = f"{app_id}-patches.mpp"
        dl_cmd = ["gh", "release", "download", tag_name, "--pattern", "*.mpp", "-R", patches_repo, "-O", mpp_file, "--clobber"]
        ok, _ = run_cmd(dl_cmd)
        if not ok or not os.path.exists(mpp_file):
            print(f"Error: Failed to download {mpp_file}")
            has_errors = True
            continue

        ok_v, versions_out = run_cmd(["java", "-jar", cli_jar, "list-versions", "--patches", mpp_file, "-f", package])
        compatible_versions = parse_versions(versions_out, package) if ok_v else []

        ok_p, patches_out = run_cmd(["java", "-jar", cli_jar, "list-patches", "-o", "--patches", mpp_file, "-f", package])
        patches_list = parse_patches(patches_out) if ok_p else []

        if not patches_list:
            raw_file = os.path.join(catalog_dir, f"{app_id}.raw.txt")
            with open(raw_file, 'w') as f:
                f.write(patches_out if patches_out else "CLI returned no output.")
            print(f"Warning: No patches parsed. Debug details written to {raw_file}")
            has_errors = True
            continue
            
        with open(os.path.join(catalog_dir, f"{app_id}.json"), 'w') as f:
            json.dump(patches_list, f, indent=2)

        app_version = app.get('version', '') or (compatible_versions[0] if compatible_versions else '')
        query = f"{app_id} {app_version}".strip()
        apkmirror_url = f"https://www.apkmirror.com/?post_type=app_release&searchtype=apk&s={urllib.parse.quote(query)}"
        
        with open(os.path.join(catalog_dir, f"{app_id}.info.json"), 'w') as f:
            json.dump({
                "patch_tag": tag_name,
                "recommended_version": app_version,
                "compatible_versions": compatible_versions,
                "apkmirror_url": apkmirror_url
            }, f, indent=2)

        apk_path = resolve_stock_apk(app_id, package, app_version)
        if not apk_path:
            print(f"⚠️ ACTION REQUIRED: Stock APK Missing for '{app_id}'")
            if discord_webhook:
                send_discord_webhook(
                    discord_webhook,
                    title=f"⚠️ Stock APK Missing: {app_id.capitalize()}",
                    description=f"Action required: A compatible stock APK is needed to patch **{app_id}**.",
                    color=0xF59E0B,
                    fields=[
                        {"name": "Target Version", "value": app_version or "Latest", "inline": True},
                        {"name": "Architecture", "value": arch, "inline": True},
                        {"name": "APKMirror Link", "value": f"[Open APKMirror]({apkmirror_url})", "inline": False},
                        {"name": "Upload Instructions", "value": f"Upload to GitHub Release `stock-{app_id}` or place in `apks/{app_id}.apk`.", "inline": False}
                    ]
                )
            has_errors = True
            continue

        output_apk = f"{app_id}-patched.apk"
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
            print(f"Error: Patching failed for {app_id}!")
            with open(f"{app_id}-patch-error.log", 'w') as f: f.write(patch_out)
            has_errors = True
            continue

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
            ok_rel, _ = run_cmd(create_cmd)
            if not ok_rel: 
                has_errors = True
            else:
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
