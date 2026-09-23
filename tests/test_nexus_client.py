from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from nexus_jar_sync.config import (
    ArtifactConfig,
    AuthConfig,
    DestinationConfig,
    NetworkConfig,
    NexusConfig,
    RetentionConfig,
    TargetConfig,
)
from nexus_jar_sync.nexus_client import NexusClient, NexusClientError


SHA256 = "a" * 64
SHA1 = "b" * 40
MD5 = "c" * 32


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, json_error: Exception | None = None) -> None:
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, *results: FakeResponse | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_target(
    *,
    classifier: str | None = None,
    extension: str = "jar",
    username: str | None = None,
    password: str | None = None,
    verify_tls: bool = True,
    ca_bundle: Path | None = None,
    timeout: float = 30,
) -> TargetConfig:
    return TargetConfig(
        id="example",
        enabled=True,
        nexus=NexusConfig(
            url="https://nexus.example.com",
            repository="releases",
            group_id="com.example",
            artifact_id="application",
        ),
        destination=DestinationConfig(Path("output")),
        network=NetworkConfig(
            timeout_seconds=timeout,
            verify_tls=verify_tls,
            ca_bundle=ca_bundle,
        ),
        auth=AuthConfig(username=username, password=password),
        artifact=ArtifactConfig(extension=extension, classifier=classifier),
        retention=RetentionConfig(),
    )


def asset_item(
    version: str = "1.0.0",
    *,
    classifier: str | None = None,
    extension: str = "jar",
    checksum: Any = None,
    path: str | None = None,
    download_url: str | None = None,
    metadata: bool = True,
) -> dict[str, Any]:
    suffix = f"-{classifier}" if classifier else ""
    filename = f"application-{version}{suffix}.{extension}"
    value: dict[str, Any] = {
        "path": path or f"com/example/application/{version}/{filename}",
        "downloadUrl": (
            f"https://nexus.example.com/repository/releases/{filename}"
            if download_url is None
            else download_url
        ),
        "checksum": {"SHA256": SHA256.upper(), "sha1": SHA1} if checksum is None else checksum,
    }
    if metadata:
        value["maven2"] = {
            "groupId": "com.example",
            "artifactId": "application",
            "version": version,
            "extension": extension,
            "classifier": classifier,
        }
    return value


def page(*items: dict[str, Any], token: Any = None) -> FakeResponse:
    return FakeResponse({"items": list(items), "continuationToken": token})


def test_endpoint_query_auth_and_network_options_are_passed() -> None:
    session = FakeSession(page(asset_item()))
    target = make_target(username="alice", password="secret", timeout=12, verify_tls=False)

    NexusClient(session).get_latest_asset(target)

    url, options = session.calls[0]
    assert url == "https://nexus.example.com/service/rest/v1/search/assets"
    assert options["params"] == {
        "repository": "releases",
        "maven.groupId": "com.example",
        "maven.artifactId": "application",
        "maven.extension": "jar",
    }
    assert options["auth"] == ("alice", "secret")
    assert options["timeout"] == 12
    assert options["verify"] is False


def test_no_auth_tuple_when_credentials_are_absent() -> None:
    session = FakeSession(page(asset_item()))
    NexusClient(session).get_latest_asset(make_target())
    assert session.calls[0][1]["auth"] is None


def test_custom_ca_bundle_overrides_boolean_tls_value() -> None:
    session = FakeSession(page(asset_item()))
    NexusClient(session).get_latest_asset(
        make_target(verify_tls=False, ca_bundle=Path("certificates/company.pem"))
    )
    assert session.calls[0][1]["verify"] == "certificates\\company.pem" or session.calls[0][1][
        "verify"
    ] == "certificates/company.pem"


def test_pagination_preserves_search_parameters_and_selects_latest() -> None:
    session = FakeSession(
        page(asset_item("1.0.0"), token="next-page"),
        page(asset_item("2.0.0")),
    )
    selected = NexusClient(session).get_latest_asset(make_target())
    assert selected.version == "2.0.0"
    assert len(session.calls) == 2
    first_params = session.calls[0][1]["params"]
    second_params = session.calls[1][1]["params"]
    assert "continuationToken" not in first_params
    assert second_params == {**first_params, "continuationToken": "next-page"}


def test_repeated_continuation_token_fails() -> None:
    session = FakeSession(page(token="again"), page(token="again"))
    with pytest.raises(NexusClientError, match="repeated continuation token") as caught:
        NexusClient(session).get_latest_asset(make_target())
    assert caught.value.retryable is False


def test_main_jar_excludes_classified_jars() -> None:
    session = FakeSession(
        page(asset_item("1.0", classifier="sources"), asset_item("2.0", classifier="javadoc"), asset_item("1.5"))
    )
    assert NexusClient(session).get_latest_asset(make_target()).version == "1.5"


def test_main_jar_rejects_inconsistent_sources_filename_metadata() -> None:
    item = asset_item(
        "2.0",
        path="com/example/application/2.0/application-2.0-sources.jar",
    )
    with pytest.raises(NexusClientError, match="No matching asset"):
        NexusClient(FakeSession(page(item))).get_latest_asset(make_target())


def test_exact_classifier_is_selected_and_sent_as_query_parameter() -> None:
    session = FakeSession(page(asset_item("3.0", classifier="all"), asset_item("4.0")))
    selected = NexusClient(session).get_latest_asset(make_target(classifier="all"))
    assert selected.version == "3.0"
    assert session.calls[0][1]["params"]["maven.classifier"] == "all"


def test_standard_path_fallback_filters_deterministically() -> None:
    session = FakeSession(
        page(
            asset_item("1.0", metadata=False),
            asset_item("2.0", classifier="sources", metadata=False),
        )
    )
    assert NexusClient(session).get_latest_asset(make_target()).version == "1.0"


def test_wrong_extension_is_rejected() -> None:
    session = FakeSession(page(asset_item(extension="pom")))
    with pytest.raises(NexusClientError, match="No matching asset"):
        NexusClient(session).get_latest_asset(make_target())


def test_no_matching_asset_is_clear() -> None:
    session = FakeSession(page(asset_item(classifier="sources")))
    with pytest.raises(NexusClientError, match="target 'example'"):
        NexusClient(session).get_latest_asset(make_target())


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "authentication failed"), (403, "permission denied"), (503, "HTTP 503")],
)
def test_http_errors_are_translated(status: int, message: str) -> None:
    with pytest.raises(NexusClientError, match=message):
        NexusClient(FakeSession(FakeResponse({}, status))).get_latest_asset(make_target())


@pytest.mark.parametrize(
    ("error", "message"),
    [(requests.Timeout(), "timed out"), (requests.ConnectionError(), "Could not connect")],
)
def test_request_failures_are_translated(error: Exception, message: str) -> None:
    with pytest.raises(NexusClientError, match=message):
        NexusClient(FakeSession(error)).get_latest_asset(make_target())


def test_invalid_json_fails_clearly() -> None:
    response = FakeResponse(None, json_error=ValueError("secret response text"))
    with pytest.raises(NexusClientError, match="invalid JSON"):
        NexusClient(FakeSession(response)).get_latest_asset(make_target())


@pytest.mark.parametrize(
    "payload",
    [[], {}, {"items": {}}, {"items": ["not-an-object"]}],
)
def test_malformed_response_schema_fails(payload: Any) -> None:
    with pytest.raises(NexusClientError, match="response|malformed"):
        NexusClient(FakeSession(FakeResponse(payload))).get_latest_asset(make_target())


def test_malformed_continuation_token_fails() -> None:
    with pytest.raises(NexusClientError, match="invalid continuation token"):
        NexusClient(FakeSession(page(asset_item(), token=123))).get_latest_asset(make_target())


def test_invalid_versions_do_not_expose_invalid_version_exception() -> None:
    with pytest.raises(NexusClientError, match="No matching asset has a usable version") as error:
        NexusClient(FakeSession(page(asset_item("not a version")))).get_latest_asset(make_target())
    assert "InvalidVersion" not in str(error.value)


@pytest.mark.parametrize("checksum", [None, {}, {"sha256": "not-hex"}, {"sha512": "a" * 128}])
def test_missing_or_malformed_checksums_fail(checksum: Any) -> None:
    item = asset_item(checksum=checksum)
    if checksum is None:
        item["checksum"] = None
    with pytest.raises(NexusClientError, match="no usable checksum"):
        NexusClient(FakeSession(page(item))).get_latest_asset(make_target())


def test_checksums_are_normalized_immutable_and_prefer_sha256() -> None:
    asset = NexusClient(FakeSession(page(asset_item()))).get_latest_asset(make_target())
    assert dict(asset.checksums) == {"sha256": SHA256, "sha1": SHA1}
    assert asset.canonical_checksum == ("sha256", SHA256)
    with pytest.raises(TypeError):
        asset.checksums["md5"] = MD5  # type: ignore[index]


@pytest.mark.parametrize(
    "digest",
    ["a" * 63, "a" * 65, "g" * 64],
    ids=["short", "long", "non-hex"],
)
def test_malformed_sha256_digest_is_rejected(digest: str) -> None:
    with pytest.raises(NexusClientError, match="no usable checksum"):
        NexusClient(FakeSession(page(asset_item(checksum={"sha256": digest})))).get_latest_asset(
            make_target()
        )


@pytest.mark.parametrize(
    ("algorithm", "digest"),
    [("md5", MD5), ("sha1", SHA1), ("sha256", SHA256)],
)
def test_supported_checksum_lengths_are_accepted(algorithm: str, digest: str) -> None:
    asset = NexusClient(
        FakeSession(page(asset_item(checksum={algorithm: digest.upper()})))
    ).get_latest_asset(make_target())
    assert dict(asset.checksums) == {algorithm: digest}


def test_malformed_preferred_checksum_uses_valid_supported_fallback() -> None:
    asset = NexusClient(
        FakeSession(page(asset_item(checksum={"sha256": "a" * 63, "SHA1": SHA1.upper()})))
    ).get_latest_asset(make_target())
    assert dict(asset.checksums) == {"sha1": SHA1}
    assert asset.canonical_checksum == ("sha1", SHA1)


@pytest.mark.parametrize(
    "download_url",
    [
        "https://public.example.com/repository/application.jar",
        "http://proxy.example.net/repository/application.jar",
    ],
)
def test_valid_http_download_urls_are_accepted(download_url: str) -> None:
    asset = NexusClient(
        FakeSession(page(asset_item(download_url=download_url)))
    ).get_latest_asset(make_target())
    assert asset.download_url == download_url


@pytest.mark.parametrize(
    "download_url",
    [
        "",
        "/repository/application.jar",
        "https:///repository/application.jar",
        "file:///tmp/application.jar",
        "ftp://files.example.com/application.jar",
        "javascript:alert(1)",
        "https://private-user@public.example.com/application.jar",
        "https://:private-password@public.example.com/application.jar",
    ],
    ids=[
        "empty",
        "relative",
        "missing-hostname",
        "file-scheme",
        "ftp-scheme",
        "javascript-scheme",
        "embedded-username",
        "embedded-password",
    ],
)
def test_invalid_or_unsafe_download_urls_are_rejected(download_url: str) -> None:
    with pytest.raises(NexusClientError, match="invalid download URL") as error:
        NexusClient(FakeSession(page(asset_item(download_url=download_url)))).get_latest_asset(
            make_target()
        )
    if download_url:
        assert download_url not in str(error.value)


def test_download_url_credentials_are_not_exposed_in_error() -> None:
    username = "private-download-user"
    password = "private-download-password"
    unsafe_url = f"https://{username}:{password}@public.example.com/application.jar"
    with pytest.raises(NexusClientError) as error:
        NexusClient(FakeSession(page(asset_item(download_url=unsafe_url)))).get_latest_asset(
            make_target()
        )
    assert username not in str(error.value)
    assert password not in str(error.value)


def test_conflicting_duplicate_latest_assets_fail() -> None:
    first = asset_item("2.0")
    second = asset_item("2.0", download_url="https://nexus.example.com/other/application.jar")
    with pytest.raises(NexusClientError, match="Conflicting assets") as caught:
        NexusClient(FakeSession(page(first, second))).get_latest_asset(make_target())
    assert caught.value.retryable is False


def test_credentials_never_appear_in_errors_or_client_repr() -> None:
    username = "highly-private-user"
    password = "highly-private-password"
    client = NexusClient(FakeSession(requests.ConnectionError(f"{username}:{password}")))
    with pytest.raises(NexusClientError) as error:
        client.get_latest_asset(make_target(username=username, password=password))
    combined = f"{error.value!r} {client!r}"
    assert username not in combined
    assert password not in combined


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_transient_request_failures_are_retryable(error: Exception) -> None:
    with pytest.raises(NexusClientError) as caught:
        NexusClient(FakeSession(error)).get_latest_asset(make_target())
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (429, True), (500, True), (503, True), (401, False), (403, False), (400, False), (404, False)],
)
def test_http_retry_classification(status: int, retryable: bool) -> None:
    with pytest.raises(NexusClientError) as caught:
        NexusClient(FakeSession(FakeResponse({}, status))).get_latest_asset(make_target())
    assert caught.value.retryable is retryable


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(None, json_error=ValueError()),
        FakeResponse({"items": {}}),
        page(token=123),
        page(asset_item(classifier="sources")),
        page(asset_item("not a version")),
        page(asset_item(checksum={})),
    ],
)
def test_schema_and_selection_failures_are_not_retryable(response: FakeResponse) -> None:
    with pytest.raises(NexusClientError) as caught:
        NexusClient(FakeSession(response)).get_latest_asset(make_target())
    assert caught.value.retryable is False
