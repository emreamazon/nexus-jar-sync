from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys

import pytest

from nexus_jar_sync.run_lock import ProductionRunLock, RunLockError


def test_overlapping_run_is_rejected_and_stale_file_is_harmless(tmp_path: Path) -> None:
    state = tmp_path / "state"
    with ProductionRunLock(state):
        with pytest.raises(RunLockError, match="already running"):
            with ProductionRunLock(state):
                pytest.fail("overlapping lock acquired")
    assert (state / ".nexus-jar-sync.lock").is_file()
    with ProductionRunLock(state):
        pass


@pytest.mark.parametrize("error", [RuntimeError("expected"), KeyboardInterrupt()])
def test_lock_releases_after_failure_and_interruption(tmp_path: Path, error: BaseException) -> None:
    state = tmp_path / "state"
    with pytest.raises(type(error)):
        with ProductionRunLock(state):
            raise error
    with ProductionRunLock(state):
        pass


def test_independent_state_contexts_do_not_interfere(tmp_path: Path) -> None:
    with ProductionRunLock(tmp_path / "one"), ProductionRunLock(tmp_path / "two"):
        pass


def test_crashed_process_does_not_permanently_block_future_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    script = (
        "import os,sys; from pathlib import Path; "
        "from nexus_jar_sync.run_lock import ProductionRunLock; "
        "lock=ProductionRunLock(Path(sys.argv[1])); lock.__enter__(); os._exit(23)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(state)], env=environment, check=False
    )
    assert result.returncode == 23
    with ProductionRunLock(state):
        pass
