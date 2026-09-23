"""Command-line entry point for nexus-jar-sync."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys

from nexus_jar_sync.config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate nexus-jar-sync configuration.")
    parser.add_argument("--config", required=True, help="Path to the YAML configuration file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    print("Configuration loaded successfully.")
    print(f"Enabled targets: {len(config.enabled_targets)}")
    for target in config.enabled_targets:
        print(f"- {target.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
