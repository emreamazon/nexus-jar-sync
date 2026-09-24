from __future__ import annotations

import importlib
import json
from pathlib import Path, PurePosixPath
import sys
import zipfile

import pytest

from nexus_jar_sync.config import load_config


ROOT = Path(__file__).parents[1]
OFFLINE = ROOT / "deployment" / "offline"
sys.path.insert(0, str(OFFLINE))
try:
    verifier = importlib.import_module("verify_manifest")
    builder = importlib.import_module("build_bundle")
finally:
    sys.path.remove(str(OFFLINE))


def write(root: Path, relative: str, content: bytes = b"content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_manifest_is_sorted_and_contains_correct_sha256(tmp_path: Path) -> None:
    write(tmp_path, "z/file.txt", b"z")
    write(tmp_path, "a.txt", b"a")
    manifest = verifier.write_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in payload["files"]] == ["a.txt", "z/file.txt"]
    assert payload["files"][0]["sha256"] == (
        "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
    )
    verifier.verify_manifest(tmp_path)


@pytest.mark.parametrize("failure", ["modified", "missing", "unexpected"])
def test_manifest_detects_bundle_content_changes(tmp_path: Path, failure: str) -> None:
    file_path = write(tmp_path, "content.txt", b"original")
    verifier.write_manifest(tmp_path)
    if failure == "modified":
        file_path.write_bytes(b"changed")
    elif failure == "missing":
        file_path.unlink()
    else:
        write(tmp_path, "extra.txt")
    with pytest.raises(verifier.ManifestError, match={
        "modified": "Checksum mismatch",
        "missing": "Missing",
        "unexpected": "Unexpected",
    }[failure]):
        verifier.verify_manifest(tmp_path)


def test_manifest_rejects_duplicate_unsafe_and_unsorted_paths(tmp_path: Path) -> None:
    manifest = tmp_path / verifier.MANIFEST_NAME
    checksum = "0" * 64
    for entries, message in (
        ([{"path": "same", "sha256": checksum}] * 2, "Duplicate"),
        ([{"path": "../escape", "sha256": checksum}], "Unsafe"),
        ([{"path": "/absolute", "sha256": checksum}], "Unsafe"),
        ([{"path": "C:/absolute", "sha256": checksum}], "Unsafe"),
        ([{"path": "b", "sha256": checksum}, {"path": "a", "sha256": checksum}], "sorted"),
    ):
        manifest.write_text(
            json.dumps({"algorithm": "sha256", "files": entries}), encoding="utf-8"
        )
        with pytest.raises(verifier.ManifestError, match=message):
            verifier.verify_manifest(tmp_path)


def test_manifest_rejects_non_normalized_paths(tmp_path: Path) -> None:
    manifest = tmp_path / verifier.MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {
                "algorithm": "sha256",
                "files": [{"path": "folder\\file", "sha256": "0" * 64}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(verifier.ManifestError, match="normalized"):
        verifier.verify_manifest(tmp_path)


def test_bundle_stage_contains_required_files_and_no_forbidden_content(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "source-wheels"
    wheelhouse.mkdir()
    write(wheelhouse, "nexus_jar_sync-0.1.0-py3-none-any.whl", b"wheel")
    write(wheelhouse, "PyYAML-6.0.3-py3-none-any.whl", b"dependency")
    stage = tmp_path / "stage"
    stage.mkdir()
    builder.populate_stage(stage, ROOT, wheelhouse, "0.1.0")

    relative_files = set(verifier.distributed_files(stage))
    assert {
        "BUILD-METADATA.json",
        "README-OFFLINE.md",
        "SHA256SUMS.json",
        "install-offline.ps1",
        "install-offline.sh",
        "tools/verify_manifest.py",
        "config/config.windows.example.yaml",
        "config/config.linux.example.yaml",
        "deployment/windows/install-task.ps1",
        "deployment/linux/nexus-jar-sync.service.example",
    } <= relative_files | {verifier.MANIFEST_NAME}
    all_paths = "\n".join(relative_files).lower()
    for forbidden in (
        ".git/",
        ".venv/",
        "__pycache__",
        ".pytest_cache",
        "config.yaml",
        "credentials.env",
        ".jar",
        ".tmp",
        ".log",
        ".json.tmp",
    ):
        assert forbidden not in all_paths
    verifier.verify_manifest(stage)


def test_fresh_bundle_does_not_absorb_stale_output_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    write(output, "stale-secret.txt", b"stale")
    monkeypatch.setattr(builder, "ensure_clean_checkout", lambda source: None)
    monkeypatch.setattr(builder, "source_commit", lambda source: "unavailable")

    def fake_wheels(source: Path, wheelhouse: Path, dependencies: tuple[str, ...]) -> None:
        wheelhouse.mkdir()
        write(wheelhouse, "nexus_jar_sync-0.1.0-py3-none-any.whl", b"app")
        write(wheelhouse, "dependency-1.0-py3-none-any.whl", b"dependency")

    monkeypatch.setattr(builder, "build_wheelhouse", fake_wheels)
    archive, _ = builder.build_bundle(ROOT, output)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert "stale-secret.txt" not in names
    assert all(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts for name in names)


def test_archive_paths_are_deterministic_normalized_and_safe(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    write(stage, "z.txt")
    write(stage, "folder/a.txt")
    archive = tmp_path / "bundle.zip"
    builder.write_archive(stage, archive)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == ["folder/a.txt", "z.txt"]
        assert all("\\" not in name and not name.startswith("/") for name in bundle.namelist())


def test_metadata_has_required_provenance_without_machine_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder, "source_commit", lambda source: "unavailable")
    builder.write_metadata(tmp_path, ROOT, "0.1.0")
    text = (tmp_path / "BUILD-METADATA.json").read_text(encoding="utf-8")
    metadata = json.loads(text)
    assert {
        "application_version",
        "source_commit",
        "built_at_utc",
        "python_implementation",
        "python_version",
        "platform",
        "architecture",
    } <= metadata.keys()
    assert metadata["source_commit"] == "unavailable"
    assert str(ROOT) not in text
    assert str(Path.home()) not in text


def test_runtime_dependency_set_and_binary_only_wheel_policy_are_unchanged() -> None:
    _, dependencies = builder.project_details(ROOT)
    assert dependencies == (
        "PyYAML>=6.0,<7",
        "requests>=2.31,<3",
        "packaging>=24,<26",
    )
    source = (OFFLINE / "build_bundle.py").read_text(encoding="utf-8")
    assert '"--only-binary=:all:"' in source
    assert '"pip",\n            "download"' in source
    assert "pytest" not in dependencies


def test_builder_rejects_broad_outputs_and_dirty_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(builder.BundleBuildError, match="broad"):
        builder.validate_output_directory(Path(tmp_path.anchor), ROOT)
    with pytest.raises(builder.BundleBuildError, match="broad"):
        builder.validate_output_directory(ROOT, ROOT)
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    monkeypatch.setattr(builder, "_run", lambda *args, **kwargs: " M tracked.txt")
    with pytest.raises(builder.BundleBuildError, match="not clean"):
        builder.ensure_clean_checkout(source)


def test_offline_installers_are_network_closed_and_non_mutating() -> None:
    combined = (OFFLINE / "install-offline.ps1").read_text(encoding="utf-8") + (
        OFFLINE / "install-offline.sh"
    ).read_text(encoding="utf-8")
    assert combined.count("--no-index") == 2
    assert combined.count("--find-links") == 2
    for forbidden in ("Register-ScheduledTask", "systemctl", "config.yaml", "--dry-run"):
        assert forbidden not in combined


@pytest.mark.parametrize(
    "example",
    ["config.example.yaml", "config.windows.example.yaml", "config.linux.example.yaml"],
)
def test_multi_jar_examples_load_with_environment_credentials(
    example: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "NEXUS_USERNAME",
        "NEXUS_PASSWORD",
        "LIBRARY_NEXUS_USERNAME",
        "LIBRARY_NEXUS_PASSWORD",
        "TOOLS_NEXUS_USERNAME",
        "TOOLS_NEXUS_PASSWORD",
    ):
        monkeypatch.setenv(name, f"fake-{name.lower()}")
    config = load_config(ROOT / "config" / example)
    assert len(config.enabled_targets) == 2
    assert any(target.artifact.classifier is None for target in config.enabled_targets)
    assert any(target.artifact.classifier is not None for target in config.enabled_targets)
    assert any(not target.enabled for target in config.targets)


def test_entry_point_and_help_remain_packaged() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'nexus-jar-sync = "nexus_jar_sync.main:main"' in pyproject
