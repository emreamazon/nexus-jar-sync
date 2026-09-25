from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

import pytest

import nexus_jar_sync.state as state_module
from nexus_jar_sync.nexus_client import NexusAsset
from nexus_jar_sync.state import (
    ChangeDecision,
    StateError,
    StateStore,
    TargetState,
    determine_change,
)


SHA256 = "a" * 64
SHA1 = "b" * 40


def make_state(**overrides: str) -> TargetState:
    values = {
        "version": "1.0.0",
        "path": "com/example/application/1.0.0/application-1.0.0.jar",
        "checksum_algorithm": "sha256",
        "checksum": SHA256,
        "downloaded_at": "2026-09-23T15:30:00+03:00",
    }
    values.update(overrides)
    return TargetState(**values)


def make_asset(**overrides: object) -> NexusAsset:
    values: dict[str, object] = {
        "version": "1.0.0",
        "filename": "application-1.0.0.jar",
        "download_url": "https://nexus.example.com/application-1.0.0.jar",
        "path": "com/example/application/1.0.0/application-1.0.0.jar",
        "checksums": {"sha256": SHA256, "sha1": SHA1},
    }
    values.update(overrides)
    return NexusAsset(**values)  # type: ignore[arg-type]


def legacy_path(state_directory: Path, target_id: str) -> Path:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", target_id).strip("._-") or "target"
    return state_directory / f"{readable[:60]}-{hashlib.sha256(target_id.encode()).hexdigest()}.json"


def write_state(path: Path, state: TargetState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.__dict__), encoding="utf-8")


def test_missing_state_returns_none_without_filesystem_side_effect(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    assert StateStore(state_directory).load("one") is None
    assert not state_directory.exists()


def test_save_creates_directory_and_round_trips_readable_json(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    expected = make_state()
    store.save("one", expected)
    assert store.load("one") == expected
    text = store.path_for("one").read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text))


def test_two_targets_have_independent_state_files(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    first = make_state(version="1.0")
    second = make_state(version="2.0")
    store.save("one", first)
    store.save("two", second)
    assert store.path_for("one") != store.path_for("two")
    assert store.load("one") == first
    assert store.load("two") == second


def test_long_state_directory_and_target_ids_use_bounded_canonical_names() -> None:
    short_root = Path(tempfile.gettempdir()).resolve()
    desired_directory_length = 150
    stem = ".njs-state-path-" + uuid.uuid4().hex + "-"
    filler_length = desired_directory_length - len(str(short_root)) - 1 - len(stem)
    if filler_length < 1 or filler_length > 120:
        pytest.skip("host temporary root cannot represent the controlled path budget")
    state_directory = short_root / (stem + "x" * filler_length)
    store = StateStore(state_directory)
    target_ids = (
        "application-release-for-a-very-long-independent-target-name-alpha",
        "application-dependencies-for-a-very-long-independent-target-name-bravo",
    )
    states = (make_state(version="1.76.0"), make_state(version="2.0.0"))

    try:
        canonical_length = len(str(store.path_for(target_ids[0])))
        former_length = len(str(legacy_path(state_directory, target_ids[0])))
        assert canonical_length <= 240
        assert former_length >= 260
        for target_id, state in zip(target_ids, states, strict=True):
            store.save(target_id, state)
        assert store.load(target_ids[0]) == states[0]
        assert store.load(target_ids[1]) == states[1]
        assert not list(state_directory.glob(".njs-state-*.tmp"))
    finally:
        shutil.rmtree(state_directory, ignore_errors=True)


@pytest.mark.parametrize("target_id", ["one", "../escape", "C:\\escape", "x" * 1000])
def test_canonical_filename_is_fixed_full_sha256(target_id: str, tmp_path: Path) -> None:
    path = StateStore(tmp_path).path_for(target_id)
    assert path.parent == tmp_path
    assert path.name == hashlib.sha256(target_id.encode()).hexdigest() + ".json"
    assert len(path.name) == 69


@pytest.mark.parametrize("target_id", ["../escape", "..\\escape", "/absolute", "C:\\escape"])
def test_unsafe_target_ids_remain_inside_state_directory(tmp_path: Path, target_id: str) -> None:
    state_directory = tmp_path / "state"
    store = StateStore(state_directory)
    path = store.path_for(target_id)
    assert path.parent == state_directory
    assert "/" not in path.name
    assert "\\" not in path.name
    store.save(target_id, make_state())
    assert path.is_file()


def test_similarly_sanitized_target_ids_do_not_collide(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    assert store.path_for("team/app") != store.path_for("team\\app")


def test_legacy_only_state_loads_without_migration_then_save_creates_canonical(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    old_path = legacy_path(tmp_path, "application")
    original = make_state(version="1.0")
    write_state(old_path, original)
    assert store.load("application") == original
    assert old_path.is_file()
    assert not store.path_for("application").exists()
    store.save("application", make_state(version="2.0"))
    assert old_path.is_file()
    assert store.path_for("application").is_file()


def test_canonical_precedes_legacy_and_canonical_corruption_does_not_fall_back(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)
    write_state(legacy_path(tmp_path, "application"), make_state(version="1.0"))
    write_state(store.path_for("application"), make_state(version="2.0"))
    assert store.load("application") == make_state(version="2.0")
    store.path_for("application").write_text("{broken", encoding="utf-8")
    with pytest.raises(StateError, match="invalid JSON"):
        store.load("application")


def test_malformed_legacy_state_fails_without_arbitrary_file_discovery(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    legacy_path(tmp_path, "application").write_text("{broken", encoding="utf-8")
    similarly_named = tmp_path / ("other-" + hashlib.sha256(b"application").hexdigest() + ".json")
    write_state(similarly_named, make_state())
    with pytest.raises(StateError, match="invalid JSON"):
        store.load("application")
    assert similarly_named.is_file()


def test_invalid_json_raises_without_deleting_file(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    path = store.path_for("one")
    path.write_text("{invalid", encoding="utf-8")
    with pytest.raises(StateError, match="invalid JSON"):
        store.load("one")
    assert path.read_text(encoding="utf-8") == "{invalid"


@pytest.mark.parametrize(
    "value",
    [[], {}, {"version": 1}, {"version": "", "path": "x"}],
)
def test_invalid_state_schema_raises(tmp_path: Path, value: object) -> None:
    store = StateStore(tmp_path)
    store.path_for("one").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(StateError, match="target 'one'"):
        store.load("one")


def test_naive_download_timestamp_is_rejected(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    value = make_state().__dict__ | {"downloaded_at": "2026-09-23T15:30:00"}
    store.path_for("one").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(StateError, match="timezone"):
        store.load("one")


def test_atomic_replacement_updates_existing_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save("one", make_state(version="1.0"))
    store.save("one", make_state(version="2.0"))
    assert store.load("one") == make_state(version="2.0")
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_replace_preserves_existing_state_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path)
    original = make_state(version="1.0")
    store.save("one", original)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    with pytest.raises(StateError, match="Could not save state"):
        store.save("one", make_state(version="2.0"))
    assert store.load("one") == original
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_replace_preserves_canonical_and_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path)
    canonical = store.path_for("one")
    old_path = legacy_path(tmp_path, "one")
    write_state(canonical, make_state(version="1.0"))
    write_state(old_path, make_state(version="0.9"))
    canonical_before = canonical.read_bytes()
    legacy_before = old_path.read_bytes()
    monkeypatch.setattr(state_module.os, "replace", lambda source, destination: (_ for _ in ()).throw(OSError("failure")))
    with pytest.raises(StateError, match="Could not save state"):
        store.save("one", make_state(version="2.0"))
    assert canonical.read_bytes() == canonical_before
    assert old_path.read_bytes() == legacy_before
    assert not list(tmp_path.glob(".njs-state-*.tmp"))


@pytest.mark.parametrize(
    ("state", "asset", "expected"),
    [
        (None, make_asset(), ChangeDecision.FIRST_RUN),
        (make_state(), make_asset(version="2.0"), ChangeDecision.VERSION_CHANGED),
        (make_state(), make_asset(checksums={"sha256": "d" * 64}), ChangeDecision.CHECKSUM_CHANGED),
        (make_state(), make_asset(), ChangeDecision.CURRENT),
        (
            make_state(checksum_algorithm="sha1", checksum=SHA1),
            make_asset(checksums={"sha256": SHA256}),
            ChangeDecision.CHECKSUM_CHANGED,
        ),
        (
            make_state(),
            make_asset(path="com/example/application/1.0.0/unexpected.jar"),
            ChangeDecision.PATH_CHANGED,
        ),
    ],
)
def test_change_decisions(
    state: TargetState | None, asset: NexusAsset, expected: ChangeDecision
) -> None:
    assert determine_change(state, asset) is expected
    assert expected.update_required is (expected is not ChangeDecision.CURRENT)
