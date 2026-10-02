import glob
import json
import os
import re
import subprocess
import sys

CONFIG_FILE = "config.json"

def find_cli_jar():
    jars = glob.glob("morphe-*-all.jar")
    if not jars:
        raise FileNotFoundError("Morphe CLI jar not found in working directory")
    return jars[0]

def run_cmd(cmd):
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    stdout = result.stdout.strip() if result.stdout else ""
    stderr = result.stderr.strip() if result.stderr else ""
    combined = f"{stdout}\n{stderr}".strip()
    
    if result.returncode != 0:
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

def main():
    print("Starting Auto-Patcher Build (Catalog Phase)...")
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

    for app in config.get('apps', []):
        app_id = app['id']
        patches_repo = app['patches_repo']
        package = app['package']
        allow_prerelease = app.get('prerelease', False)

        print(f"\n--- Processing App: {app_id} ---")

        tag_name = get_latest_tag(patches_repo, allow_prerelease)
        if not tag_name:
            print(f"Failed to get release tag for {patches_repo}")
            has_errors = True
            continue

        print(f"Latest patch tag: {tag_name}")
        mpp_file = f"{app_id}-patches.mpp"

        dl_cmd = f"gh release download {tag_name} --pattern \"*.mpp\" -R {patches_repo} -O {mpp_file} --clobber"
        ok, dl_out = run_cmd(dl_cmd)
        if not ok or not os.path.exists(mpp_file):
            print(f"Error: Failed to download {mpp_file}")
            has_errors = True
            continue

        versions_cmd = f"java -jar {cli_jar} list-versions --patches {mpp_file} -f {package}"
        ok_v, versions_out = run_cmd(versions_cmd)
        compatible_versions = parse_versions(versions_out, package) if ok_v else []
        print(f"Found {len(compatible_versions)} compatible versions")

        patches_cmd = f"java -jar {cli_jar} list-patches -o --patches {mpp_file} -f {package}"
        ok_p, patches_out = run_cmd(patches_cmd)
        patches_list = parse_patches(patches_out) if ok_p else []

        catalog_file = os.path.join(catalog_dir, f"{app_id}.json")

        if not patches_list:
            raw_file = os.path.join(catalog_dir, f"{app_id}.raw.txt")
            with open(raw_file, 'w') as f:
                f.write(patches_out if patches_out else "CLI execution produced no output.")
            print(f"Warning: No patches parsed for {app_id}. Debug details written to {raw_file}")
            has_errors = True
        else:
            with open(catalog_file, 'w') as f:
                json.dump(patches_list, f, indent=2)
            print(f"Catalog saved to {catalog_file} ({len(patches_list)} patches)")

    if has_errors:
        print("\nBuild finished with errors.")
        sys.exit(1)
    else:
        print("\nBuild finished successfully!")

if __name__ == "__main__":
    main()
