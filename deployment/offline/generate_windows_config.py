"""Generate one validated Windows multi-artifact configuration without secrets."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def _text(value: str, field: str) -> str:
    value = value.strip()
    if not value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} is empty or contains control characters")
    return value


def _url(value: str, field: str, allow_http: bool) -> str:
    value = _text(value, field)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{field} must be an absolute HTTP/HTTPS URL without userinfo")
    if parsed.scheme == "http" and not allow_http:
        raise ValueError(f"{field} uses HTTP; explicit acknowledgement is required")
    return value.rstrip("/")


def _absolute(value: str, field: str) -> str:
    value = _text(value, field)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field} must be absolute")
    return str(path)


def build_config(arguments: argparse.Namespace) -> dict[str, object]:
    auth: dict[str, str] | None = None
    if bool(arguments.username_env) != bool(arguments.password_env):
        raise ValueError("credential environment variable names must be supplied together")
    if arguments.username_env:
        auth = {
            "username_env": _text(arguments.username_env, "username environment variable"),
            "password_env": _text(arguments.password_env, "password environment variable"),
        }
    nexus_url = _url(arguments.nexus_url, "Nexus URL", arguments.allow_http)
    target: dict[str, object] = {
        "id": "nexus-version-taker",
        "enabled": True,
        "nexus": {
            "url": nexus_url,
            "repository": _text(arguments.repository, "repository"),
            "group_id": _text(arguments.group_id, "group ID"),
            "artifact_id": _text(arguments.primary_artifact_id, "primary artifact ID"),
        },
        "release_artifacts": [
            {"id": "windows-obfuscated", "artifact_id": _text(arguments.windows_obs, "windows obs artifact ID")},
            {"id": "linux", "artifact_id": _text(arguments.linux_versions, "linux versions artifact ID")},
            {"id": "linux-obfuscated", "artifact_id": _text(arguments.linux_obs, "linux obs artifact ID")},
        ],
        "artifact": {"extension": "jar", "classifier": None},
        "destination": {"directory": _absolute(arguments.destination, "destination")},
        "companions": [
            {
                "id": "dependencies", "url": _url(arguments.dependencies_url, "dependencies URL", arguments.allow_http),
                "filename": "dependencies.7z", "action": "extract_7z", "keep_archive": False,
                "extract_to": ".", "strip_single_root": True,
            },
            {
                "id": "license", "url": _url(arguments.license_url, "license URL", arguments.allow_http),
                "filename": "license.txt", "action": "copy",
            },
        ],
    }
    if auth:
        target["auth"] = auth
    network: dict[str, object] = {"verify_tls": True}
    if arguments.ca_bundle:
        network["ca_bundle"] = _absolute(arguments.ca_bundle, "CA bundle")
    target["network"] = network
    return {
        "logging": {"level": "INFO", "file": _absolute(arguments.log_file, "log file"), "max_file_size_mb": 5, "backup_count": 3},
        "state": {"directory": _absolute(arguments.state_directory, "state directory")},
        "tools": {"seven_zip_executable": _absolute(arguments.seven_zip, "7-Zip executable"), "extraction_timeout_seconds": 300},
        "targets": [target],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    for name in (
        "output", "nexus-url", "repository", "group-id", "primary-artifact-id",
        "windows-obs", "linux-versions", "linux-obs", "destination",
        "dependencies-url", "license-url", "seven-zip", "log-file", "state-directory",
    ):
        result.add_argument("--" + name, required=True)
    result.add_argument("--ca-bundle")
    result.add_argument("--username-env")
    result.add_argument("--password-env")
    result.add_argument("--allow-http", action="store_true")
    result.add_argument("--plan", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = build_config(args)
        output = Path(args.output)
        if not output.is_absolute():
            raise ValueError("configuration output must be absolute")
        if args.plan:
            print("Configuration plan validated (no file written).")
            return 0
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(value, stream, sort_keys=False, allow_unicode=True)
    except (OSError, ValueError) as error:
        print(f"Configuration generation failed: {error}")
        return 2
    print(f"Configuration created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
