"""Reusable client for discovering Maven assets through the Nexus REST API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from packaging.version import InvalidVersion, Version
import requests

from nexus_jar_sync.config import TargetConfig


class NexusClientError(Exception):
    """Raised when Nexus discovery fails or returns unusable data."""


@dataclass(frozen=True)
class NexusAsset:
    version: str
    filename: str
    download_url: str
    path: str
    checksums: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "checksums", MappingProxyType(dict(self.checksums)))

    @property
    def canonical_checksum(self) -> tuple[str, str]:
        for algorithm in ("sha256", "sha1", "md5"):
            if algorithm in self.checksums:
                return algorithm, self.checksums[algorithm]
        raise NexusClientError(f"Asset '{self.path}' has no usable checksum")


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class _Candidate:
    asset: NexusAsset
    parsed_version: Version


class _UnsortableVersion(Exception):
    """Internal signal for a matching asset with a non-PEP-440 version."""


class NexusClient:
    """Discover the newest exact Maven asset for a configured target."""

    def __init__(self, session: _Session | None = None) -> None:
        self._session = session if session is not None else requests.Session()

    def get_latest_asset(self, target: TargetConfig) -> NexusAsset:
        endpoint = f"{target.nexus.url}/service/rest/v1/search/assets"
        search_params = {
            "repository": target.nexus.repository,
            "maven.groupId": target.nexus.group_id,
            "maven.artifactId": target.nexus.artifact_id,
            "maven.extension": target.artifact.extension,
        }
        if target.artifact.classifier is not None:
            search_params["maven.classifier"] = target.artifact.classifier

        auth = None
        if target.auth.username is not None and target.auth.password is not None:
            auth = (target.auth.username, target.auth.password)
        verify: bool | str = target.network.verify_tls
        if target.network.ca_bundle is not None:
            verify = str(target.network.ca_bundle)

        candidates: list[_Candidate] = []
        unsortable_versions = 0
        continuation_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params = dict(search_params)
            if continuation_token is not None:
                params["continuationToken"] = continuation_token
            response = self._request(
                endpoint,
                params=params,
                auth=auth,
                timeout=target.network.timeout_seconds,
                verify=verify,
                target=target,
            )
            payload = self._response_payload(response, target)
            for item in payload["items"]:
                try:
                    candidate = self._candidate_from_item(item, target)
                except _UnsortableVersion:
                    # Ordinary releases are ordered with packaging.Version. Matching
                    # versions outside that syntax are ignored deterministically.
                    unsortable_versions += 1
                    continue
                if candidate is not None:
                    candidates.append(candidate)

            token = payload.get("continuationToken")
            if token is not None and not isinstance(token, str):
                raise NexusClientError(
                    f"Nexus returned an invalid continuation token for target '{target.id}'"
                )
            if not token:
                break
            if token in seen_tokens:
                raise NexusClientError(
                    f"Nexus repeated continuation token for target '{target.id}'"
                )
            seen_tokens.add(token)
            continuation_token = token

        if not candidates:
            if unsortable_versions:
                raise NexusClientError(
                    f"No matching asset has a usable version for target '{target.id}'"
                )
            raise NexusClientError(f"No matching asset found for target '{target.id}'")

        newest_version = max(candidate.parsed_version for candidate in candidates)
        newest = [candidate.asset for candidate in candidates if candidate.parsed_version == newest_version]
        selected = newest[0]
        for duplicate in newest[1:]:
            if duplicate != selected:
                raise NexusClientError(
                    f"Conflicting assets found for latest version '{selected.version}' "
                    f"of target '{target.id}'"
                )
        return selected

    def _request(self, endpoint: str, *, target: TargetConfig, **kwargs: Any) -> Any:
        try:
            response = self._session.get(endpoint, **kwargs)
        except requests.Timeout:
            raise NexusClientError(f"Nexus request timed out for target '{target.id}'") from None
        except requests.ConnectionError:
            raise NexusClientError(
                f"Could not connect to Nexus server for target '{target.id}'"
            ) from None
        except requests.RequestException:
            raise NexusClientError(f"Nexus request failed for target '{target.id}'") from None

        status_code = getattr(response, "status_code", None)
        if status_code == 401:
            raise NexusClientError(f"Nexus authentication failed for target '{target.id}'")
        if status_code == 403:
            raise NexusClientError(f"Nexus permission denied for target '{target.id}'")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            status = status_code if isinstance(status_code, int) else "unknown"
            raise NexusClientError(
                f"Nexus returned HTTP {status} for target '{target.id}'"
            )
        return response

    @staticmethod
    def _response_payload(response: Any, target: TargetConfig) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise NexusClientError(f"Nexus returned invalid JSON for target '{target.id}'") from None
        if not isinstance(payload, dict):
            raise NexusClientError(
                f"Nexus response root must be an object for target '{target.id}'"
            )
        items = payload.get("items")
        if not isinstance(items, list):
            raise NexusClientError(
                f"Nexus response has invalid 'items' for target '{target.id}'"
            )
        return payload

    def _candidate_from_item(
        self, item: Any, target: TargetConfig
    ) -> _Candidate | None:
        if not isinstance(item, dict):
            raise NexusClientError(f"Nexus returned a malformed asset for target '{target.id}'")
        path = item.get("path")
        download_url = item.get("downloadUrl")
        if not isinstance(path, str) or not path or not isinstance(download_url, str) or not download_url:
            raise NexusClientError(f"Nexus returned a malformed asset for target '{target.id}'")

        coordinates = self._coordinates(item, path, target)
        if coordinates is None:
            return None
        version, filename = coordinates
        try:
            parsed_version = Version(version)
        except InvalidVersion:
            raise _UnsortableVersion from None

        checksums = self._checksums(item.get("checksum"), target)
        return _Candidate(
            asset=NexusAsset(
                version=version,
                filename=filename,
                download_url=download_url,
                path=path,
                checksums=checksums,
            ),
            parsed_version=parsed_version,
        )

    @staticmethod
    def _coordinates(
        item: Mapping[str, Any], path: str, target: TargetConfig
    ) -> tuple[str, str] | None:
        metadata = item.get("maven2")
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise NexusClientError(
                    f"Nexus returned malformed Maven metadata for target '{target.id}'"
                )
            required = ("groupId", "artifactId", "version", "extension")
            if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in required):
                raise NexusClientError(
                    f"Nexus returned malformed Maven metadata for target '{target.id}'"
                )
            classifier = metadata.get("classifier")
            if classifier is not None and not isinstance(classifier, str):
                raise NexusClientError(
                    f"Nexus returned malformed Maven metadata for target '{target.id}'"
                )
            expected_classifier = target.artifact.classifier
            actual_classifier = classifier or None
            if (
                metadata["groupId"] != target.nexus.group_id
                or metadata["artifactId"] != target.nexus.artifact_id
                or metadata["extension"] != target.artifact.extension
                or actual_classifier != expected_classifier
            ):
                return None
            classifier_suffix = f"-{actual_classifier}" if actual_classifier is not None else ""
            expected_filename = (
                f"{target.nexus.artifact_id}-{metadata['version']}{classifier_suffix}."
                f"{target.artifact.extension}"
            )
            filename = PurePosixPath(path).name
            if filename != expected_filename:
                return None
            return metadata["version"], filename

        parts = PurePosixPath(path).parts
        group_parts = tuple(target.nexus.group_id.split("."))
        prefix = (*group_parts, target.nexus.artifact_id)
        if len(parts) != len(prefix) + 2 or tuple(parts[: len(prefix)]) != prefix:
            return None
        version, filename = parts[-2], parts[-1]
        classifier_suffix = (
            f"-{target.artifact.classifier}" if target.artifact.classifier is not None else ""
        )
        expected_filename = (
            f"{target.nexus.artifact_id}-{version}{classifier_suffix}.{target.artifact.extension}"
        )
        if filename != expected_filename:
            return None
        return version, filename

    @staticmethod
    def _checksums(value: Any, target: TargetConfig) -> Mapping[str, str]:
        if not isinstance(value, dict):
            raise NexusClientError(
                f"Nexus asset has no usable checksum for target '{target.id}'"
            )
        normalized: dict[str, str] = {}
        for raw_algorithm, raw_checksum in value.items():
            if not isinstance(raw_algorithm, str) or not isinstance(raw_checksum, str):
                continue
            algorithm = raw_algorithm.strip().lower()
            checksum = raw_checksum.strip().lower()
            if algorithm in {"sha256", "sha1", "md5"} and checksum and all(
                character in "0123456789abcdef" for character in checksum
            ):
                normalized[algorithm] = checksum
        if not normalized:
            raise NexusClientError(
                f"Nexus asset has no usable checksum for target '{target.id}'"
            )
        return normalized
