"""Validate release archives without installing them."""

from __future__ import annotations

import argparse
import email
import re
import tarfile
import zipfile
from pathlib import Path

EXPECTED_NAME = "powermonitor-hvpm"
EXPECTED_VERSION = "0.1.0"
LIVE_MODULES = {
    "powermonitor/cli.py",
    "powermonitor/live.py",
    "powermonitor/backends/sampling.py",
}
VENDORED_FILES = {
    "HVPM.py",
    "Operations.py",
    "calibrationData.py",
    "pmapi.py",
    "sampleEngine.py",
    "LICENSE.txt",
}


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _single(paths: list[Path], kind: str) -> Path:
    if len(paths) != 1:
        raise RuntimeError(f"Expected exactly one {kind}, found {len(paths)}")
    return paths[0]


def _check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_name = _single(
            [Path(name) for name in names if name.endswith(".dist-info/METADATA")],
            "wheel METADATA file",
        )
        metadata = email.message_from_bytes(archive.read(str(metadata_name)))
        if _normalized_name(metadata["Name"]) != EXPECTED_NAME:
            raise RuntimeError(f"Unexpected distribution name: {metadata['Name']}")
        if metadata["Version"] != EXPECTED_VERSION:
            raise RuntimeError(f"Unexpected distribution version: {metadata['Version']}")
        requirements = metadata.get_all("Requires-Dist", [])
        if any("git+" in requirement or " @ " in requirement for requirement in requirements):
            raise RuntimeError("Wheel metadata contains a direct URL dependency")

        entry_points_name = _single(
            [Path(name) for name in names if name.endswith(".dist-info/entry_points.txt")],
            "wheel entry_points.txt file",
        )
        entry_points = archive.read(str(entry_points_name)).decode()
        if "powermonitor = powermonitor.cli:main" not in entry_points:
            raise RuntimeError("Wheel does not provide the powermonitor command")

        missing_live = LIVE_MODULES - set(names)
        if missing_live:
            raise RuntimeError(
                f"Wheel is missing terminal sampling modules: {sorted(missing_live)}"
            )

        vendor_prefix = "powermonitor/_vendor/pymonsoon/"
        packaged_vendor = {
            name.removeprefix(vendor_prefix) for name in names if name.startswith(vendor_prefix)
        }
        missing = VENDORED_FILES - packaged_vendor
        if missing:
            raise RuntimeError(f"Wheel is missing vendored SDK files: {sorted(missing)}")

        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in names):
            raise RuntimeError("Wheel is missing the project license")
        if not any(name.endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md") for name in names):
            raise RuntimeError("Wheel is missing third-party notices")


def _check_sdist(path: Path) -> None:
    required_suffixes = {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        *(f"src/{name}" for name in LIVE_MODULES),
        *(f"src/powermonitor/_vendor/pymonsoon/{name}" for name in VENDORED_FILES),
    }
    with tarfile.open(path, "r:gz") as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
    missing = {
        suffix
        for suffix in required_suffixes
        if not any(name.endswith(f"/{suffix}") for name in members)
    }
    if missing:
        raise RuntimeError(f"Source distribution is missing files: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    wheel = _single(sorted(args.directory.glob("*.whl")), "wheel")
    sdist = _single(sorted(args.directory.glob("*.tar.gz")), "source distribution")
    _check_wheel(wheel)
    _check_sdist(sdist)
    print(f"Validated {wheel.name} and {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
