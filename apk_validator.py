from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess
import zipfile


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
                reverse=True,
            )
            for version in versions:
                candidate = version / name
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

    info = ApkInfo(
        path=path,
        package=package_match.group(1),
        version_code=package_match.group(2),
        version_name=package_match.group(3),
        architectures=architectures,
        sha256=_sha256(path),
        size_bytes=os.path.getsize(path),
    )
    return info


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
    return re.findall(r"Signer #\d+ certificate SHA-256 digest:\s*([0-9A-Fa-f:]+)", output)


def validate_apk(
    path: str,
    expected_package: str,
    expected_version: str = "",
    expected_arch: str = "auto",
    expected_signer_sha256: list[str] | None = None,
    allowed_packages: list[str] | None = None,
) -> ApkInfo:
    if not path or not os.path.isfile(path):
        raise ApkValidationError("APK file does not exist.")

    suffix = Path(path).suffix.lower()
    if suffix != ".apk":
        raise ApkValidationError(
            f"Unsupported stock artifact '{suffix}'. A single APK is required by this build pipeline."
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
