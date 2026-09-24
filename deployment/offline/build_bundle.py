"""Build a platform-specific offline installation bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

from verify_manifest import MANIFEST_NAME, write_manifest


BUNDLE_MARKER = ".nexus-jar-sync-bundle-stage"


class BundleBuildError(Exception):
    """Raised when the offline bundle cannot be safely built."""


def _run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BundleBuildError(f"Required command failed: {command[0]} {command[1]}") from exc
    return completed.stdout.strip() if capture else ""


def ensure_clean_checkout(source_root: Path) -> None:
    git_directory = source_root / ".git"
    if not git_directory.exists():
        return
    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source_root,
        capture=True,
    )
    if status:
        raise BundleBuildError("Source checkout is not clean; commit or remove changes first")


def source_commit(source_root: Path) -> str:
    if not (source_root / ".git").exists():
        return "unavailable"
    try:
        return _run(["git", "rev-parse", "HEAD"], cwd=source_root, capture=True)
    except BundleBuildError:
        return "unavailable"


def project_details(source_root: Path) -> tuple[str, tuple[str, ...]]:
    with (source_root / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)["project"]
    return project["version"], tuple(project["dependencies"])


def validate_output_directory(output_directory: Path, source_root: Path) -> Path:
    output = output_directory.resolve(strict=False)
    source = source_root.resolve(strict=True)
    if output == source or output == Path(output.anchor) or output.parent == output:
        raise BundleBuildError("Output directory is too broad or ambiguous")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise BundleBuildError("Output location is not a directory")
    return output


def build_wheelhouse(source_root: Path, wheelhouse: Path, dependencies: tuple[str, ...]) -> None:
    wheelhouse.mkdir()
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(source_root),
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
        ],
        cwd=source_root,
    )
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(wheelhouse),
            *dependencies,
        ],
        cwd=source_root,
    )
    if not list(wheelhouse.glob("*.whl")):
        raise BundleBuildError("Wheelhouse contains no wheels")
    non_wheels = [path.name for path in wheelhouse.iterdir() if path.suffix != ".whl"]
    if non_wheels:
        raise BundleBuildError(f"Wheelhouse contains non-wheel content: {non_wheels[0]}")


def write_metadata(stage: Path, source_root: Path, version: str) -> None:
    metadata = {
        "application": "nexus-jar-sync",
        "application_version": version,
        "source_commit": source_commit(source_root),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "architecture": platform.machine(),
    }
    (stage / "BUILD-METADATA.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def populate_stage(stage: Path, source_root: Path, wheelhouse: Path, version: str) -> None:
    (stage / BUNDLE_MARKER).write_text("nexus-jar-sync offline bundle stage\n", encoding="utf-8")
    shutil.copytree(wheelhouse, stage / "wheelhouse")
    config_dir = stage / "config"
    config_dir.mkdir()
    for name in (
        "config.example.yaml",
        "config.windows.example.yaml",
        "config.linux.example.yaml",
    ):
        shutil.copy2(source_root / "config" / name, config_dir / name)
    shutil.copytree(source_root / "deployment" / "windows", stage / "deployment" / "windows")
    shutil.copytree(source_root / "deployment" / "linux", stage / "deployment" / "linux")
    offline = source_root / "deployment" / "offline"
    shutil.copy2(offline / "README.md", stage / "README-OFFLINE.md")
    tools = stage / "tools"
    tools.mkdir()
    shutil.copy2(offline / "verify_manifest.py", tools / "verify_manifest.py")
    shutil.copy2(offline / "install-offline.ps1", stage / "install-offline.ps1")
    shutil.copy2(offline / "install-offline.sh", stage / "install-offline.sh")
    write_metadata(stage, source_root, version)
    (stage / BUNDLE_MARKER).unlink()
    write_manifest(stage)


def write_archive(stage: Path, archive: Path) -> None:
    if archive.exists():
        raise BundleBuildError(f"Archive already exists: {archive}")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(candidate for candidate in stage.rglob("*") if candidate.is_file()):
            relative = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
            output.writestr(info, path.read_bytes())


def archive_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_bundle(source_root: Path, output_directory: Path) -> tuple[Path, Path]:
    source_root = source_root.resolve(strict=True)
    ensure_clean_checkout(source_root)
    version, dependencies = project_details(source_root)
    output = validate_output_directory(output_directory, source_root)
    tag = f"{platform.system().lower()}-{platform.machine().lower()}-py{sys.version_info.major}{sys.version_info.minor}"
    archive = output / f"nexus-jar-sync-{version}-{tag}.zip"
    checksum_file = archive.with_suffix(archive.suffix + ".sha256")
    if archive.exists() or checksum_file.exists():
        raise BundleBuildError("Bundle output already exists; choose a fresh output directory")

    stage = Path(tempfile.mkdtemp(prefix=".nexus-jar-sync-stage-", dir=output))
    try:
        wheelhouse = stage.parent / f"{stage.name}-wheelhouse"
        try:
            build_wheelhouse(source_root, wheelhouse, dependencies)
            populate_stage(stage, source_root, wheelhouse, version)
            write_archive(stage, archive)
        finally:
            if wheelhouse.exists():
                shutil.rmtree(wheelhouse)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    checksum = archive_checksum(archive)
    checksum_file.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    return archive, checksum_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a nexus-jar-sync offline bundle")
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[2], help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    try:
        archive, checksum_file = build_bundle(args.source_root, args.output_directory)
    except (BundleBuildError, OSError) as exc:
        print(f"Bundle build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Bundle archive: {archive}")
    print(f"Archive checksum: {checksum_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
