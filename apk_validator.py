from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile


BUNDLE_SUFFIXES = {".apkm", ".apks", ".xapk"}


@dataclass
class ApkInfo:
    path: str
    package: str = ""
    version_name: str = ""
    version_code: str = ""
    architectures: list[str] = None
    sha256: str = ""
    size_bytes: int = 0
    signer_sha256: list[str] = None
    artifact_type: str = "apk"

    def __post_init__(self):
        if self.architectures is None:
            self.architectures = []
        if self.signer_sha256 is None:
            self.signer_sha256 = []

    def to_dict(self) -> dict:
        return asdict(self)


class ApkValidationError(ValueError):
    pass


def _find_tool(name: str) -> str | None:
    explicit = os.environ.get(name.upper())
    if explicit and os.path.isfile(explicit):
        return explicit

    found = shutil.which(name)
    if found:
        return found

    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        build_tools = Path(android_home) / "build-tools"
        if build_tools.exists():
            versions = sorted(
                (p for p in build_tools.iterdir() if p.is_dir()),
                key=lambda p: p.name,
                reverse=True
            )
            for version in versions:
                candidate = build_tools / version / name
                if candidate.is_file():
                    return str(candidate)

    return None


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_aapt(path: str) -> str:
    tool = _find_tool("aapt2") or _find_tool("aapt")
    if not tool:
        raise ApkValidationError(
            "Android build-tools were not found. aapt2/aapt is required for APK validation."
        )

    command = [tool, "dump", "badging", path]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise ApkValidationError(f"Unable to inspect APK with {Path(tool).name}: {output[-2000:]}")
    return output


def _parse_badging(path: str, output: str) -> ApkInfo:
    package_match = re.search(
        r"package:\s+name='([^']+)'\s+versionCode='([^']*)'\s+versionName='([^']*)'",
        output,
    )
    if not package_match:
        raise ApkValidationError("APK manifest package/version metadata could not be read.")

    arch_match = re.search(r"native-code:\s*((?:'[^']+'(?:,\s*)?)*)", output)
    architectures = []
    if arch_match:
        architectures = re.findall(r"'([^']+)'", arch_match.group(1))

    return ApkInfo(
        path=path,
        package=package_match.group(1),
        version_code=package_match.group(2),
        version_name=package_match.group(3),
        architectures=architectures,
        sha256=_sha256(path),
        size_bytes=os.path.getsize(path),
    )


def _extract_signers(path: str) -> list[str]:
    tool = _find_tool("apksigner")
    if not tool:
        return []

    result = subprocess.run(
        [tool, "verify", "--print-certs", path],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    return re.findall(
        r"Signer #\d+ certificate SHA-256 digest:\s*([0-9A-Fa-f:]+)",
        output,
    )


def _validate_identity(
    info: ApkInfo,
    expected_package: str,
    expected_version: str,
    expected_arch: str,
    expected_signer_sha256: list[str] | None,
):
    if expected_package and info.package != expected_package:
        raise ApkValidationError(
            f"Wrong package: expected {expected_package}, found {info.package}."
        )

    if expected_version and info.version_name != expected_version:
        raise ApkValidationError(
            f"Wrong version: expected {expected_version}, found {info.version_name}."
        )

    wanted_arch = (expected_arch or "auto").lower()
    if wanted_arch not in ("", "auto", "automatic"):
        normalized = {a.lower() for a in info.architectures}
        if wanted_arch not in normalized and "universal" not in normalized:
            raise ApkValidationError(
                f"Wrong architecture: expected {expected_arch}, found {', '.join(info.architectures) or 'unknown'}."
            )

    expected_signers = {
        value.lower().replace(":", "")
        for value in (expected_signer_sha256 or [])
        if value
    }
    if expected_signers:
        actual_signers = {
            value.lower().replace(":", "")
            for value in info.signer_sha256
        }
        if not expected_signers.intersection(actual_signers):
            raise ApkValidationError(
                "APK signer certificate does not match the configured expected signer."
            )


def _bundle_members(path: str) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = []
            for name in archive.namelist():
                normalized = name.replace("\\", "/")
                if normalized.endswith("/") or not normalized.lower().endswith(".apk"):
                    continue
                # APKM/APKS/XAPK producers are not required to flatten their
                # archive layout. Accept APK modules at any depth; the APK
                # manifest/signature checks below remain authoritative.
                if normalized.lower().startswith("__macosx/"):
                    continue
                names.append(name)
            return names
    except zipfile.BadZipFile as exc:
        raise ApkValidationError("Downloaded split bundle is not a valid ZIP.") from exc


def _is_single_apk_zip(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            return (
                "AndroidManifest.xml" in names
                and "resources.arsc" in names
            )
    except zipfile.BadZipFile:
        return False


def _bundle_architectures(member_names: list[str]) -> list[str]:
    arches = []
    mapping = {
        "arm64_v8a": "arm64-v8a",
        "arm64-v8a": "arm64-v8a",
        "armeabi_v7a": "armeabi-v7a",
        "armeabi-v7a": "armeabi-v7a",
        "x86_64": "x86_64",
        "x86-64": "x86_64",
        "x86": "x86",
    }
    for name in member_names:
        lowered = Path(name).stem.lower()
        for token, normalized in mapping.items():
            if token in lowered and normalized not in arches:
                arches.append(normalized)
    return arches


def _validate_bundle(
    path: str,
    expected_package: str,
    expected_version: str,
    expected_arch: str,
    expected_signer_sha256: list[str] | None,
) -> ApkInfo:
    members = _bundle_members(path)
    if not members:
        # Some download endpoints label a single APK as .apkm/.apks/.xapk.
        # Detect the actual container format from its entries before treating it
        # as an invalid split archive.
        if _is_single_apk_zip(path):
            info = _parse_badging(path, _run_aapt(path))
            info.signer_sha256 = _extract_signers(path)
            info.artifact_type = "apk"
            _validate_identity(
                info,
                expected_package,
                expected_version,
                expected_arch,
                expected_signer_sha256,
            )
            return info
        raise ApkValidationError("Split bundle contains no APK modules.")

    def member_priority(name: str) -> tuple[int, str]:
        base = Path(name).name.lower()
        if base == "base.apk":
            return (0, base)
        if "base" in base and base.endswith(".apk"):
            return (1, base)
        if not any(token in base for token in ("config.", "split_")):
            return (2, base)
        return (3, base)

    members = sorted(members, key=member_priority)
    base_member = members[0]
    embedded_arches = _bundle_architectures(members)

    with tempfile.TemporaryDirectory(prefix="morphe-bundle-") as temp_dir:
        extracted = []
        with zipfile.ZipFile(path) as archive:
            for member in members:
                target = os.path.join(temp_dir, Path(member).name)
                with archive.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                try:
                    with zipfile.ZipFile(target) as apk_zip:
                        if apk_zip.testzip():
                            raise ApkValidationError(f"Corrupt APK module in split bundle: {member}")
                        names = set(apk_zip.namelist())
                        if "AndroidManifest.xml" not in names:
                            raise ApkValidationError(f"Split module lacks AndroidManifest.xml: {member}")
                except zipfile.BadZipFile as exc:
                    raise ApkValidationError(f"Split module is not a valid APK: {member}") from exc
                extracted.append((member, target))

        base_path = next((p for n, p in extracted if n == base_member), extracted[0][1])
        info = _parse_badging(base_path, _run_aapt(base_path))
        info.signer_sha256 = _extract_signers(base_path)

        if embedded_arches:
            info.architectures = list(dict.fromkeys(info.architectures + embedded_arches))

        info.path = path
        info.sha256 = _sha256(path)
        info.size_bytes = os.path.getsize(path)
        info.artifact_type = Path(path).suffix.lower().lstrip(".") or "apk"

        for member, module_path in extracted:
            if module_path == base_path:
                continue
            try:
                module_info = _parse_badging(module_path, _run_aapt(module_path))
            except ApkValidationError:
                continue
            if expected_package and module_info.package != expected_package:
                raise ApkValidationError(
                    f"Split module package mismatch in {member}: "
                    f"expected {expected_package}, found {module_info.package}."
                )
        _validate_identity(
            info,
            expected_package,
            expected_version,
            expected_arch,
            expected_signer_sha256,
        )
        return info


def validate_artifact(
    path: str,
    expected_package: str,
    expected_version: str = "",
    expected_arch: str = "auto",
    expected_signer_sha256: list[str] | None = None,
    allowed_packages: list[str] | None = None,
) -> ApkInfo:
    if not path or not os.path.isfile(path):
        raise ApkValidationError("Android artifact file does not exist.")

    suffix = Path(path).suffix.lower()
    if suffix in BUNDLE_SUFFIXES:
        allowed = {str(x).strip() for x in (allowed_packages or []) if str(x).strip()}
        package = expected_package or (sorted(allowed)[0] if allowed else "")
        return _validate_bundle(
            path,
            package,
            expected_version,
            expected_arch,
            expected_signer_sha256,
        )

    if suffix != ".apk":
        raise ApkValidationError(
            f"Unsupported stock artifact '{suffix}'. Supported formats: .apk, .apkm, .apks, .xapk."
        )

    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ApkValidationError(f"Corrupt APK ZIP entry: {bad}")
            names = set(archive.namelist())
            if "AndroidManifest.xml" not in names or "resources.arsc" not in names:
                raise ApkValidationError("File is not a complete Android APK.")
    except zipfile.BadZipFile as exc:
        raise ApkValidationError("Downloaded file is not a valid APK/ZIP.") from exc

    info = _parse_badging(path, _run_aapt(path))
    info.signer_sha256 = _extract_signers(path)

    allowed = {str(x).strip() for x in (allowed_packages or []) if str(x).strip()}
    if expected_package:
        allowed.add(expected_package)
    if allowed and info.package not in allowed:
        expected_text = ", ".join(sorted(allowed))
        raise ApkValidationError(
            f"Wrong package: expected one of {expected_text}, found {info.package}."
        )
    if expected_version and info.version_name != expected_version:
        raise ApkValidationError(
            f"Wrong version: expected {expected_version}, found {info.version_name}."
        )

    wanted_arch = (expected_arch or "auto").lower()
    if wanted_arch not in ("", "auto", "automatic"):
        normalized = {a.lower() for a in info.architectures}
        if wanted_arch not in normalized and "universal" not in normalized:
            raise ApkValidationError(
                f"Wrong architecture: expected {expected_arch}, found {', '.join(info.architectures) or 'unknown'}."
            )

    expected_signers = {
        value.lower().replace(":", "")
        for value in (expected_signer_sha256 or [])
        if value
    }
    if expected_signers:
        actual_signers = {
            value.lower().replace(":", "")
            for value in info.signer_sha256
        }
        if not expected_signers.intersection(actual_signers):
            raise ApkValidationError(
                "APK signer certificate does not match the configured expected signer."
            )
    return info


def validate_apk(
    path: str,
    expected_package: str,
    expected_version: str = "",
    expected_arch: str = "auto",
    expected_signer_sha256: list[str] | None = None,
    allowed_packages: list[str] | None = None,
) -> ApkInfo:
    if Path(path).suffix.lower() != ".apk":
        raise ApkValidationError(
            f"Patched output must be a single .apk file; received '{Path(path).suffix.lower()}'."
        )
    return validate_artifact(
        path,
        expected_package,
        expected_version,
        expected_arch,
        expected_signer_sha256,
        allowed_packages,
    )
