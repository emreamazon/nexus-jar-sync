from __future__ import annotations

from pathlib import Path

import pytest

from nexus_jar_sync.config import (
    ArtifactConfig,
    AuthConfig,
    DestinationConfig,
    NetworkConfig,
    NexusConfig,
    RetentionConfig,
    TargetConfig,
)
from nexus_jar_sync.lifecycle import ArtifactLifecycleManager, LifecycleError


def make_target(
    destination: Path,
    *,
    keep: int = 1,
    classifier: str | None = None,
    extension: str = "jar",
    artifact_id: str = "application",
) -> TargetConfig:
    return TargetConfig(
        id="example",
        enabled=True,
        nexus=NexusConfig(
            url="https://nexus.example.com",
            repository="releases",
            group_id="com.example",
            artifact_id=artifact_id,
        ),
        destination=DestinationConfig(destination),
        network=NetworkConfig(),
        auth=AuthConfig(),
        artifact=ArtifactConfig(extension=extension, classifier=classifier),
        retention=RetentionConfig(keep_previous_versions=keep),
    )


def create_files(directory: Path, *names: str) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in names:
        path = directory / name
        path.write_bytes(name.encode())
        paths[name] = path
    return paths


@pytest.mark.parametrize(
    ("keep", "expected_remaining"),
    [
        (0, {"application-3.0.jar"}),
        (1, {"application-3.0.jar", "application-2.0.jar"}),
        (2, {"application-3.0.jar", "application-2.0.jar", "application-1.0.jar"}),
    ],
)
def test_retention_counts_current_plus_previous_versions(
    tmp_path: Path, keep: int, expected_remaining: set[str]
) -> None:
    files = create_files(
        tmp_path,
        "application-1.0.jar",
        "application-2.0.jar",
        "application-3.0.jar",
    )
    removed = ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=keep), files["application-3.0.jar"]
    )
    assert {path.name for path in tmp_path.iterdir()} == expected_remaining
    assert isinstance(removed, tuple)
    assert {path.name for path in removed} == {
        "application-1.0.jar",
        "application-2.0.jar",
        "application-3.0.jar",
    } - expected_remaining


def test_current_artifact_is_retained_even_when_not_newest(tmp_path: Path) -> None:
    files = create_files(tmp_path, "application-1.0.jar", "application-2.0.jar")
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-1.0.jar"]
    )
    assert files["application-1.0.jar"].exists()


def test_main_target_ignores_classifiers_unrelated_files_directories_and_temps(
    tmp_path: Path,
) -> None:
    names = (
        "application-1.0.jar",
        "application-2.0.jar",
        "application-1.0-sources.jar",
        "application-1.0-javadoc.jar",
        "application-1.0-linux.jar",
        "other-1.0.jar",
        "application-1.0.pom",
        ".application-1.0.jar.random.download.tmp",
        "notes.txt",
    )
    files = create_files(tmp_path, *names)
    (tmp_path / "application-0.1.jar").mkdir()
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-2.0.jar"]
    )
    assert not files["application-1.0.jar"].exists()
    for name in names[2:]:
        assert files[name].exists()
    assert (tmp_path / "application-0.1.jar").is_dir()


def test_classified_target_manages_only_exact_classifier(tmp_path: Path) -> None:
    files = create_files(
        tmp_path,
        "application-1.0-all.jar",
        "application-2.0-all.jar",
        "application-1.0.jar",
        "application-1.0-sources.jar",
    )
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0, classifier="all"), files["application-2.0-all.jar"]
    )
    assert not files["application-1.0-all.jar"].exists()
    assert files["application-1.0.jar"].exists()
    assert files["application-1.0-sources.jar"].exists()


def test_versions_use_packaging_order(tmp_path: Path) -> None:
    files = create_files(
        tmp_path,
        "application-1.9.jar",
        "application-1.10.jar",
        "application-2.0.jar",
    )
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=1), files["application-2.0.jar"]
    )
    assert files["application-1.10.jar"].exists()
    assert not files["application-1.9.jar"].exists()


def test_unparseable_versions_are_retained(tmp_path: Path) -> None:
    files = create_files(tmp_path, "application-2.0.jar", "application-release-x.jar")
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-2.0.jar"]
    )
    assert files["application-release-x.jar"].exists()


def test_main_target_retains_parseable_classifier_like_versions(tmp_path: Path) -> None:
    files = create_files(
        tmp_path,
        "application-3.0.jar",
        "application-1.0.jar",
        "application-1.0-1.jar",
        "application-1.0-rc1.jar",
        "application-1.0-post1.jar",
    )
    removed = ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-3.0.jar"]
    )
    assert {path.name for path in removed} == {"application-1.0.jar"}
    for name in (
        "application-1.0-1.jar",
        "application-1.0-rc1.jar",
        "application-1.0-post1.jar",
    ):
        assert files[name].exists()


@pytest.mark.parametrize(
    "old_version",
    ["1.0", "1.0.0", "1.0rc1", "1.0.post1", "1.0+build"],
)
def test_main_target_still_deletes_canonical_old_versions(
    tmp_path: Path, old_version: str
) -> None:
    files = create_files(
        tmp_path,
        f"application-{old_version}.jar",
        "application-3.0.jar",
    )
    removed = ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-3.0.jar"]
    )
    assert removed == (files[f"application-{old_version}.jar"],)
    assert not files[f"application-{old_version}.jar"].exists()


def test_explicit_classifier_can_manage_noncanonical_version_spelling(tmp_path: Path) -> None:
    files = create_files(
        tmp_path,
        "application-1.0-1-all.jar",
        "application-3.0-all.jar",
    )
    removed = ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0, classifier="all"),
        files["application-3.0-all.jar"],
    )
    assert removed == (files["application-1.0-1-all.jar"],)
    assert not files["application-1.0-1-all.jar"].exists()


def test_normalized_equivalent_versions_are_retained_conservatively(tmp_path: Path) -> None:
    files = create_files(
        tmp_path,
        "application-3.0.jar",
        "application-1.0.jar",
        "application-1.0.0.jar",
    )
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-3.0.jar"]
    )
    assert files["application-1.0.jar"].exists()
    assert files["application-1.0.0.jar"].exists()


def test_retention_does_not_recurse(tmp_path: Path) -> None:
    files = create_files(tmp_path, "application-2.0.jar")
    nested = tmp_path / "nested"
    nested_file = create_files(nested, "application-1.0.jar")["application-1.0.jar"]
    ArtifactLifecycleManager().apply_retention(
        make_target(tmp_path, keep=0), files["application-2.0.jar"]
    )
    assert nested_file.exists()


@pytest.mark.parametrize("kind", ["outside", "mismatched"])
def test_invalid_current_path_is_rejected(tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    if kind == "outside":
        current = tmp_path / "application-2.0.jar"
    else:
        current = destination / "other-2.0.jar"
    current.write_bytes(b"current")
    with pytest.raises(LifecycleError, match="Current artifact"):
        ArtifactLifecycleManager().apply_retention(make_target(destination), current)
    assert current.exists()


def test_deletion_failure_is_fail_fast_and_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = create_files(
        tmp_path,
        "application-1.0.jar",
        "application-2.0.jar",
        "application-3.0.jar",
    )
    original_unlink = Path.unlink

    def fail_oldest(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "application-1.0.jar":
            raise OSError("simulated deletion failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_oldest)
    with pytest.raises(LifecycleError, match="application-1.0.jar.*target 'example'"):
        ArtifactLifecycleManager().apply_retention(
            make_target(tmp_path, keep=0), files["application-3.0.jar"]
        )
    assert files["application-3.0.jar"].exists()
    assert files["application-1.0.jar"].exists()
    assert files["application-2.0.jar"].exists()


def test_retention_does_not_create_state(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    state_directory = tmp_path / "state"
    files = create_files(destination, "application-1.0.jar")
    assert ArtifactLifecycleManager().apply_retention(
        make_target(destination, keep=0), files["application-1.0.jar"]
    ) == ()
    assert not state_directory.exists()
