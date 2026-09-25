"""Command-line entry point for nexus-jar-sync."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import logging
from pathlib import Path
import sys

from nexus_jar_sync.config import ConfigError, load_config
from nexus_jar_sync.downloader import ArtifactDownloader
from nexus_jar_sync.logging_config import configure_logging
from nexus_jar_sync.nexus_client import NexusClient
from nexus_jar_sync.retry import RetryExecutor
from nexus_jar_sync.release import ReleaseAssembler
from nexus_jar_sync.state import StateStore
from nexus_jar_sync.sync import SyncService, SyncSummary, TargetSyncStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Synchronize Nexus JAR artifacts.")
    parser.add_argument("--config", required=True, help="Path to the YAML configuration file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and report changes without modifying artifacts or state",
    )
    parser.add_argument(
        "--test-download",
        action="store_true",
        help="Perform real downloads into an isolated test output",
    )
    parser.add_argument("--test-output", help="New absolute root for --test-download")
    return parser


def create_sync_service(logger: logging.Logger) -> SyncService:
    """Construct explicit production dependencies for one synchronization run."""
    nexus_client = NexusClient()
    downloader = ArtifactDownloader()
    release_assembler = ReleaseAssembler(downloader)
    retry_executor = RetryExecutor(logger=logger.getChild("retry"))
    return SyncService(
        nexus_client=nexus_client,
        downloader=downloader,
        release_assembler=release_assembler,
        retry_executor=retry_executor,
        state_store_factory=StateStore,
        clock=lambda: datetime.now(timezone.utc),
        logger=logger.getChild("sync"),
    )


def format_summary(summary: SyncSummary, *, dry_run: bool) -> str:
    lines = ["Dry-Run Summary" if dry_run else "Sync Summary", ""]
    for result in summary.results:
        version = result.version or "-"
        detail = result.change.value if result.change is not None else result.message
        lines.append(
            f"{result.target_id}  {result.status.value.upper()}  {version}  {detail}"
        )
    if summary.results:
        lines.append("")
    lines.extend(
        [
            f"Updated: {summary.updated_count}",
            f"Would update: {summary.would_update_count}",
            f"Current: {summary.current_count}",
            f"Failed: {summary.failed_count}",
        ]
    )
    if dry_run:
        lines.extend(
            ["", "No artifact, destination, or state changes were made."]
        )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[logging.Logger], SyncService] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.test_download and args.dry_run:
        build_parser().error("--test-download cannot be combined with --dry-run")
    if args.test_download != (args.test_output is not None):
        build_parser().error("--test-download and --test-output must be used together")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        logger = configure_logging(config.logging)
    except OSError:
        print("Logging initialization failed.", file=sys.stderr)
        return 2

    factory = service_factory or create_sync_service
    service = factory(logger)
    try:
        if args.test_download:
            try:
                output = _validated_test_output(Path(args.test_output), config)
                output.mkdir()
            except (ConfigError, OSError) as exc:
                print(f"Test output error: {exc}", file=sys.stderr)
                return 2
            print("TEST DOWNLOAD: performs real network reads and downloads; Nexus and production paths remain read-only.")
            summary = service.run_test_download(config, output)
        else:
            summary = service.run(config, dry_run=args.dry_run)
    finally:
        service.close()
    print(format_summary(summary, dry_run=args.dry_run))
    return 1 if summary.failed_count else 0


def _validated_test_output(path: Path, config: object) -> Path:
    from nexus_jar_sync.config import AppConfig

    if not path.is_absolute():
        raise ConfigError("--test-output must be absolute")
    resolved = path.resolve(strict=False)
    if (
        resolved.exists()
        or resolved == Path(resolved.anchor)
        or resolved.parent == Path(resolved.anchor)
    ):
        raise ConfigError("--test-output must be a new, non-root path")
    home = Path.home().resolve(strict=False)
    workspace = Path.cwd().resolve(strict=True)
    if resolved in {home, workspace}:
        raise ConfigError("--test-output cannot be a home or workspace root")
    assert isinstance(config, AppConfig)
    protected = [config.state.directory.resolve(strict=False)] + [
        target.destination.directory.resolve(strict=False) for target in config.targets
    ]
    for candidate in protected:
        if _paths_overlap(resolved, candidate):
            raise ConfigError("--test-output overlaps a production path")
    if resolved.parent == resolved or not resolved.parent.exists():
        raise ConfigError("--test-output parent must exist")
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        try:
            right.relative_to(left)
            return True
        except ValueError:
            return False


if __name__ == "__main__":
    raise SystemExit(main())
