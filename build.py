import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

CONFIG_FILE = "config.json"

def find_cli_jar():
    jars = glob.glob("morphe-*-all.jar")
    if not jars:
        raise FileNotFoundError("Morphe CLI jar not found in working directory")
    return jars[0]

def run_cmd(cmd, silent=False):
    if not silent:
        print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    stdout = result.stdout.strip() if result.stdout else ""
    stderr = result.stderr.strip() if result.stderr else ""
    combined = f"{stdout}\n{stderr}".strip()
    
    if result.returncode != 0:
        if not silent:
            print(f"Warning: Command failed: {cmd}\nStderr: {stderr if stderr else 'None'}")
        return False, combined
    return True, stdout

def get_latest_tag(repo, allow_prerelease=False):
    ok, out = run_cmd(f"gh release list -R {repo} --limit 10 --json tagName,isPrerelease,isDraft")
    if not ok or not out:
        return None
    try:
        releases = json.loads(out)
        for r in releases:
            if r.get("isDraft"):
                continue
            if r.get("isPrerelease") and not allow_prerelease:
                continue
            return r["tagName"]
    except json.JSONDecodeError:
        return None
    return None

def release_exists(tag):
    ok, _ = run_cmd(f"gh release view {tag} --json tagName", silent=True)
    return ok

def parse_versions(output, package_name):
    versions = []
    capture = False
    for line in output.split('\n'):
        clean_line = line.strip()
        if f"Package name: {package_name}" in clean_line:
            capture = True
            continue
        if capture and clean_line.startswith("Package name:"):
            break
        if capture and clean_line.startswith("Most common"):
            continue
        if capture and re.match(r'^\s*([\w\.\-]+)\s+\(\d+\s+patches\)', clean_line):
            version = clean_line.split()[0]
            versions.append(version)
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
        if not clean_line:
            continue

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
    """Checks local 'apks/' first, then checks for a 'stock-<app_id>' GitHub Release."""
    os.makedirs("apks", exist_ok=True)

    def find_local():
        # Priority 1: Exact version match with id or package
        if app_version:
            m = glob.glob(f"apks/{app_id}-{app_version}.apk*") + glob.glob(f"apks/{app_id}-{app_version}.apkm*")
            if m: return m[0]
            m = glob.glob(f"apks/{package}*{app_version}*.apk*") + glob.glob(f"apks/{package}*{app_version}*.apkm*")
            if m: return m[0]

        # Priority 2: Generic ID match (e.g. youtube.apk / youtube.apkm)
        m = glob.glob(f"apks/{app_id}.apk*") + glob.glob(f"apks/{app_id}.apkm*")
        if m: return m[0]

        # Priority 3: Generic package match (e.g. com.google.android.youtube*.apk)
        m = glob.glob(f"apks/{package}*.apk*") + glob.glob(f"apks/{package}*.apkm*")
        if m: return m[0]
        return None

    local_file = find_local()
    if local_file:
        return local_file

    # Remote GitHub Release drop-bucket resolution (stock-<app_id>)
    stock_release_tag = f"stock-{app_id}"
    if release_exists(stock_release_tag):
        print(f"Found remote drop-bucket release: {stock_release_tag}. Downloading...")
        # Morphe CLI natively accepts .apk and .apkm
        dl_stock_cmd = (
            f"gh release download {stock_release_tag} "
            f"--pattern \"*.apk*\" --pattern \"*.apkm*\" "
            f"-D apks/ --clobber"
        )
        ok, _ = run_cmd(dl_stock_cmd)
        if ok:
            downloaded = find_local()
            if downloaded:
                return downloaded
            
            # Scoped fallback: only look for files related to this app
            matching_apks = (
                glob.glob(f"apks/{app_id}*") + 
                glob.glob(f"apks/{package}*")
            )
            matching_apks = [f for f in matching_apks if f.endswith(('.apk', '.apkm'))]
            if matching_apks:
                matching_apks.sort(key=os.path.getmtime, reverse=True)
                return matching_apks[0]

    return None

def main():
    print("Starting Auto-Patcher Build...")
    has_errors = False

    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found!")
        sys.exit(1)

    try:
        cli_jar = find_cli_jar()
        print(f"Found CLI JAR: {cli_jar}")
    except FileNotFoundError as e:
        print(e)
        sys.exit(1)

    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)

    catalog_dir = "docs/catalog"
    os.makedirs(catalog_dir, exist_ok=True)

    keystore_arg = "--keystore ks.bks" if os.path.exists("ks.bks") else ""
    if keystore_arg:
        print("Found ks.bks, will use custom signing key.")
    else:
        print("No ks.bks found. Morphe CLI will use temporary signing key.")

    target_app_filter = os.environ.get('TARGET_APP', '').strip()
    force_build_env = os.environ.get('FORCE_BUILD', '').lower() in ['true', '1']

    for app in config.get('apps', []):
        app_id = app['id']

        if target_app_filter and app_id != target_app_filter:
            print(f"Skipping {app_id} (Target filter is: {target_app_filter})")
            continue

        patches_repo = app['patches_repo']
        package = app['package']
        arch = app.get('arch', 'arm64-v8a')
        allow_prerelease = app.get('prerelease', False)
        force = app.get('force', False) or force_build_env

        print(f"\n==========================================")
        print(f"Processing App: {app_id}")
        print(f"==========================================")

        tag_name = get_latest_tag(patches_repo, allow_prerelease)
        if not tag_name:
            print(f"Failed to get release tag for {patches_repo}")
            has_errors = True
            continue

        release_tag = f"{app_id}-{tag_name}"
        if release_exists(release_tag) and not force:
            print(f"Release {release_tag} already exists. Skipping (set force=true to rebuild).")
            continue
        elif release_exists(release_tag) and force:
            print(f"Force flag active. Deleting existing release {release_tag}...")
            run_cmd(f"gh release delete {release_tag} --yes --cleanup-tag")

        mpp_file = f"{app_id}-patches.mpp"
        dl_cmd = f"gh release download {tag_name} --pattern \"*.mpp\" -R {patches_repo} -O {mpp_file} --clobber"
        ok, _ = run_cmd(dl_cmd)
        if not ok or not os.path.exists(mpp_file):
            print(f"Error: Failed to download {mpp_file}")
            has_errors = True
            continue

        # 1. Update Catalog & Retrieve Compatible Versions
        ok_v, versions_out = run_cmd(f"java -jar {cli_jar} list-versions --patches {mpp_file} -f {package}")
        compatible_versions = parse_versions(versions_out, package) if ok_v else []

        ok_p, patches_out = run_cmd(f"java -jar {cli_jar} list-patches -o --patches {mpp_file} -f {package}")
        patches_list = parse_patches(patches_out) if ok_p else []

        catalog_file = os.path.join(catalog_dir, f"{app_id}.json")
        info_file = os.path.join(catalog_dir, f"{app_id}.info.json")

        if not patches_list:
            raw_file = os.path.join(catalog_dir, f"{app_id}.raw.txt")
            with open(raw_file, 'w') as f:
                f.write(patches_out if patches_out else "CLI returned no output.")
            print(f"Warning: No patches parsed. Debug details written to {raw_file}")
            has_errors = True
            continue
        else:
            with open(catalog_file, 'w') as f:
                json.dump(patches_list, f, indent=2)
            print(f"Catalog saved to {catalog_file} ({len(patches_list)} patches)")

        # Resolve Target App Version
        app_version = app.get('version', '')
        if not app_version and compatible_versions:
            app_version = compatible_versions[0]

        # Generate Morphe-Manager-style APKMirror helper search URL
        query = f"{app_id} {app_version}".strip()
        apkmirror_url = f"https://www.apkmirror.com/?post_type=app_release&searchtype=apk&s={urllib.parse.quote(query)}"

        # Save metadata for Web UI
        meta_data = {
            "patch_tag": tag_name,
            "recommended_version": app_version,
            "compatible_versions": compatible_versions,
            "apkmirror_url": apkmirror_url
        }
        with open(info_file, 'w') as f:
            json.dump(meta_data, f, indent=2)

        # 2. Resolve Stock APK (Local or Drop-Bucket Release)
        apk_path = resolve_stock_apk(app_id, package, app_version)

        if not apk_path:
            print("\n" + "="*60)
            print(f"⚠️  ACTION REQUIRED: Stock APK Missing for '{app_id}'")
            print("="*60)
            print(f"Target Version: {app_version or 'Latest'}")
            print(f"Download link:  {apkmirror_url}")
            print("\nHow to supply the APK:")
            print(f"  Option A: Place '{app_id}.apk' or '{app_id}-{app_version}.apk' in 'apks/' folder.")
            print(f"  Option B: Upload the APK/APKM to a GitHub Release tagged 'stock-{app_id}'.")
            print("="*60 + "\n")
            has_errors = True
            continue

        print(f"Found stock APK: {apk_path}")

        # 3. Construct and Execute Patch Command
        output_apk = f"{app_id}-patched.apk"
        if os.path.exists(output_apk):
            os.remove(output_apk)

        patch_flags = [f"-p {mpp_file}", f"-o {output_apk}", f"--striplibs {arch}", "--continue-on-error"]
        if keystore_arg:
            patch_flags.append(keystore_arg)
        if force:
            patch_flags.append("--force")

        for p_name in app.get('enable', []):
            patch_flags.append(f'-e "{p_name}"')
        for p_name in app.get('disable', []):
            patch_flags.append(f'-d "{p_name}"')

        for p_name, opts in app.get('options', {}).items():
            for key, val in opts.items():
                patch_flags.append(f'-O "{p_name}:{key}={val}"')

        # Target input APK placed strictly at the end
        patch_cmd = f"java -jar {cli_jar} patch {' '.join(patch_flags)} \"{apk_path}\""
        print("Executing patch command...")
        ok, patch_out = run_cmd(patch_cmd)

        if not ok or not os.path.exists(output_apk):
            print(f"Error: Patching failed for {app_id}!")
            with open(f"{app_id}-patch-error.log", 'w') as f:
                f.write(patch_out)
            has_errors = True
            continue

        print(f"Successfully created: {output_apk}")

        # 4. Create GitHub Release
        print(f"Publishing release: {release_tag}")
        release_notes = (
            f"Auto-patched {app_id}\n\n"
            f"- **Patch Repository:** {patches_repo}\n"
            f"- **Patch Version:** {tag_name}\n"
            f"- **App Version:** {app_version or 'Auto-detected'}\n"
            f"- **Architecture:** {arch}\n"
        )

        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as tf:
            tf.write(release_notes)
            notes_file = tf.name

        try:
            create_cmd = f"gh release create {release_tag} \"{output_apk}\" --title \"{app_id} {tag_name}\" --notes-file \"{notes_file}\""
            ok_rel, rel_err = run_cmd(create_cmd)
            if ok_rel:
                print(f"✅ Release {release_tag} published successfully!")
            else:
                print(f"Error publishing release {release_tag}:\n{rel_err}")
                has_errors = True
        finally:
            if os.path.exists(notes_file):
                os.remove(notes_file)

    if has_errors:
        print("\nBuild finished with errors or missing stock APKs.")
        sys.exit(1)
    else:
        print("\n🎉 Build finished successfully!")

if __name__ == "__main__":
    main()
