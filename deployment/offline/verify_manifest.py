"""Create and verify deterministic SHA-256 manifests for offline bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


MANIFEST_NAME = "SHA256SUMS.json"


class ManifestError(Exception):
    """Raised when a bundle manifest or its contents are unsafe or invalid."""


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ManifestError("Manifest paths must be non-empty strings")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or "." in path.parts
    ):
        raise ManifestError(f"Unsafe manifest path: {value!r}")
    if "\\" in value or path.as_posix() != value:
        raise ManifestError(f"Manifest path is not normalized POSIX form: {value!r}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def distributed_files(root: Path) -> dict[str, Path]:
    resolved_root = root.resolve(strict=True)
    files: dict[str, Path] = {}
    for candidate in resolved_root.rglob("*"):
        if candidate.is_symlink():
            raise ManifestError(f"Symbolic links are not allowed: {candidate.name}")
        if not candidate.is_file() or candidate.name == MANIFEST_NAME:
            continue
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(resolved_root).as_posix()
        except ValueError:
            raise ManifestError("Distributed file escapes the bundle root") from None
        _safe_relative_path(relative)
        files[relative] = resolved
    return files


def write_manifest(root: Path) -> Path:
    root = root.resolve(strict=True)
    entries = [
        {"path": relative, "sha256": _sha256(path)}
        for relative, path in sorted(distributed_files(root).items())
    ]
    manifest_path = root / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps({"algorithm": "sha256", "files": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def verify_manifest(root: Path, manifest_path: Path | None = None) -> None:
    root = root.resolve(strict=True)
    manifest_path = manifest_path or root / MANIFEST_NAME
    try:
        payload: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Could not read manifest: {exc}") from None
    if not isinstance(payload, dict) or payload.get("algorithm") != "sha256":
        raise ManifestError("Manifest algorithm must be 'sha256'")
    raw_entries = payload.get("files")
    if not isinstance(raw_entries, list):
        raise ManifestError("Manifest 'files' must be a list")

    expected: dict[str, str] = {}
    previous = ""
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ManifestError("Manifest entries must be objects")
        relative = entry.get("path")
        checksum = entry.get("sha256")
        safe_path = _safe_relative_path(relative)
        if relative in expected:
            raise ManifestError(f"Duplicate manifest path: {relative}")
        if relative < previous:
            raise ManifestError("Manifest entries are not sorted")
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise ManifestError(f"Invalid SHA-256 value for {relative!r}")
        try:
            int(checksum, 16)
        except ValueError:
            raise ManifestError(f"Invalid SHA-256 value for {relative!r}") from None
        expected[safe_path.as_posix()] = checksum.lower()
        previous = relative

    actual = distributed_files(root)
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    if missing:
        raise ManifestError(f"Missing distributed file: {missing[0]}")
    if unexpected:
        raise ManifestError(f"Unexpected distributed file: {unexpected[0]}")
    for relative, checksum in expected.items():
        if _sha256(actual[relative]) != checksum:
            raise ManifestError(f"Checksum mismatch: {relative}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an extracted nexus-jar-sync bundle")
    parser.add_argument("bundle_directory", type=Path)
    args = parser.parse_args(argv)
    try:
        verify_manifest(args.bundle_directory)
    except (ManifestError, OSError) as exc:
        print(f"Manifest verification failed: {exc}")
        return 1
    print("Manifest verification succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
