"""S86-17: the object store takes a cloud identity — temporary AWS credentials, signed with their
session token, and the token sources of Azure and Google, each one against a scripted transport."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from tranibor_adapters.storage import (
    AzureBlobObjectStore,
    AzureWorkloadIdentity,
    ContainerAwsCredentials,
    GceMetadataToken,
    GcsObjectStore,
    ObjectStoreError,
    S3ObjectStore,
    StaticAwsCredentials,
    WebIdentityAwsCredentials,
    aws_credentials,
    build_object_store,
    sigv4_headers,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
SIGNED = {
    "method": "PUT",
    "host": "s3.eu-west-3.amazonaws.com",
    "path": "/choregos/deck",
    "payload_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "access_key": "ASIAEXAMPLE",
    "secret_key": "secret",
    "region": "eu-west-3",
    "now": NOW,
}
STS_ANSWER = """<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleWithWebIdentityResult>
    <Credentials>
      <AccessKeyId>ASIA{n}</AccessKeyId>
      <SecretAccessKey>temporary-secret-{n}</SecretAccessKey>
      <SessionToken>session-token-{n}</SessionToken>
      <Expiration>2026-11-25T13:00:00Z</Expiration>
    </Credentials>
  </AssumeRoleWithWebIdentityResult>
</AssumeRoleWithWebIdentityResponse>"""


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now.timestamp()

    def __call__(self) -> float:
        return self.now


def _signed_headers(authorization: str) -> list[str]:
    return authorization.split("SignedHeaders=")[1].split(",", maxsplit=1)[0].split(";")


# ───────── the session token is sent and signed ─────────


def test_a_session_token_is_sent_and_signed() -> None:
    """Temporary credentials are refused by S3 without `x-amz-security-token`, and a token sent but
    left out of the signed headers is refused too: both are checked here."""
    with_token = sigv4_headers(**SIGNED, session_token="FwoGZXIvYXdzEXAMPLE")  # type: ignore[arg-type]
    without = sigv4_headers(**SIGNED)  # type: ignore[arg-type]
    assert with_token["x-amz-security-token"] == "FwoGZXIvYXdzEXAMPLE"
    assert "x-amz-security-token" in _signed_headers(with_token["Authorization"])
    assert "x-amz-security-token" not in without
    assert "x-amz-security-token" not in _signed_headers(without["Authorization"])
    assert with_token["Authorization"] != without["Authorization"], "the token is part of the signature"


async def test_the_s3_store_signs_with_temporary_credentials() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    store = S3ObjectStore(
        "s3://choregos",
        region="eu-west-3",
        credentials=StaticAwsCredentials("ASIAEXAMPLE", "secret", "FwoGZXIvYXdzEXAMPLE"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(answer)),
    )
    await store.put("deck", b"PK", "application/zip")
    assert seen[0].headers["x-amz-security-token"] == "FwoGZXIvYXdzEXAMPLE"
    assert "x-amz-security-token" in _signed_headers(seen[0].headers["Authorization"])
    assert seen[0].headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/")


# ───────── EKS IRSA: web identity ─────────


async def test_irsa_exchanges_the_projected_token_at_sts_unsigned_and_caches_it(
    tmp_path: pathlib.Path,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-jwt-1\n")
    clock = Clock()
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host.startswith("sts."):
            n = sum(1 for r in seen if r.url.host.startswith("sts."))
            return httpx.Response(200, text=STS_ANSWER.format(n=n))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    source = aws_credentials(
        region="us-east-1",
        environ={
            "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/tranibor-api",
            "AWS_WEB_IDENTITY_TOKEN_FILE": str(token_file),
            "AWS_REGION": "eu-west-3",
        },
        client=client,
    )
    assert isinstance(source, WebIdentityAwsCredentials)
    source._cache._clock = clock  # the cache's clock: the test moves time forward
    store = S3ObjectStore("s3://choregos", region="eu-west-3", credentials=source, client=client)

    await store.put("a", b"1", "text/plain")
    await store.put("b", b"2", "text/plain")
    sts = [r for r in seen if r.url.host.startswith("sts.")]
    assert len(sts) == 1, "the credentials are cached between calls"
    assert str(sts[0].url) == "https://sts.eu-west-3.amazonaws.com/"
    assert sts[0].method == "POST" and "Authorization" not in sts[0].headers, (
        "AssumeRoleWithWebIdentity is unsigned"
    )
    form = parse_qs(sts[0].content.decode())
    assert form["Action"] == ["AssumeRoleWithWebIdentity"]
    assert form["RoleArn"] == ["arn:aws:iam::123456789012:role/tranibor-api"]
    assert form["WebIdentityToken"] == ["projected-jwt-1"]
    puts = [r for r in seen if r.method == "PUT"]
    assert all(r.headers["x-amz-security-token"] == "session-token-1" for r in puts)
    assert puts[0].headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ASIA1/")

    # Five minutes before the expiry (13:00), the credentials are fetched again, with the rotated token.
    token_file.write_text("projected-jwt-2")
    clock.now = datetime(2026, 11, 25, 12, 56, tzinfo=UTC).timestamp()
    await store.put("c", b"3", "text/plain")
    sts = [r for r in seen if r.url.host.startswith("sts.")]
    assert len(sts) == 2
    assert parse_qs(sts[1].content.decode())["WebIdentityToken"] == ["projected-jwt-2"]
    assert seen[-1].headers["x-amz-security-token"] == "session-token-2"


async def test_an_sts_refusal_names_its_code_and_never_the_token(tmp_path: pathlib.Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("projected-jwt-secret")
    refusal = (
        "<ErrorResponse><Error><Code>InvalidIdentityToken</Code>"
        "<Message>projected-jwt-secret</Message></Error></ErrorResponse>"
    )
    source = WebIdentityAwsCredentials(
        "arn:aws:iam::1:role/r",
        str(token_file),
        sts_endpoint="https://sts.example.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, text=refusal))),
    )
    with pytest.raises(ObjectStoreError, match="InvalidIdentityToken") as raised:
        await source.credentials()
    assert "projected-jwt-secret" not in str(raised.value)
    missing = WebIdentityAwsCredentials("arn:aws:iam::1:role/r", str(tmp_path / "absent"))
    with pytest.raises(ObjectStoreError, match="cannot read the token file"):
        await missing.credentials()
    empty = tmp_path / "empty"
    empty.write_text("  ")
    with pytest.raises(ObjectStoreError, match="is empty"):
        await WebIdentityAwsCredentials("arn:aws:iam::1:role/r", str(empty)).credentials()
    incomplete = WebIdentityAwsCredentials(
        "arn:aws:iam::1:role/r",
        str(token_file),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<x/>"))),
    )
    assert incomplete.sts_endpoint == "https://sts.amazonaws.com"
    with pytest.raises(ObjectStoreError, match="without credentials"):
        await incomplete.credentials()


# ───────── EKS Pod Identity: the container credentials endpoint ─────────


async def test_pod_identity_asks_the_agent_with_the_token_of_its_file(tmp_path: pathlib.Path) -> None:
    token_file = tmp_path / "eks-pod-identity-token"
    token_file.write_text("pod-identity-jwt")
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "169.254.170.23":
            body = {
                "AccessKeyId": "ASIAPOD",
                "SecretAccessKey": "pod-secret",
                "Token": "pod-session",
                "Expiration": "2099-01-01T00:00:00Z",
            }
            return httpx.Response(200, text=json.dumps(body))
        return httpx.Response(200, content=b"bytes")

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    source = aws_credentials(
        environ={
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(token_file),
        },
        client=client,
    )
    assert isinstance(source, ContainerAwsCredentials)
    store = S3ObjectStore("s3://choregos", credentials=source, client=client)
    assert await store.get("k") == b"bytes"
    assert await store.get("k") == b"bytes"
    agent = [r for r in seen if r.url.host == "169.254.170.23"]
    assert len(agent) == 1, "cached until shortly before it expires"
    assert agent[0].headers["Authorization"] == "pod-identity-jwt"
    assert agent[0].url.path == "/v1/credentials"
    assert seen[-1].headers["x-amz-security-token"] == "pod-session"


async def test_the_container_endpoint_is_checked_and_its_answer_read_carefully() -> None:
    with pytest.raises(ValueError, match="must be https"):
        ContainerAwsCredentials("http://attacker.example/creds")
    with pytest.raises(ValueError, match="must be https"):
        ContainerAwsCredentials("file:///etc/passwd")
    for allowed in (
        "http://127.0.0.1:8080/creds",
        "http://localhost/creds",
        "http://[fd00:ec2::23]/v1/credentials",
        "https://creds.example/x",
    ):
        ContainerAwsCredentials(allowed)
    refusing = ContainerAwsCredentials(
        "http://169.254.170.2/creds",
        token="static-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(ObjectStoreError, match="answered 401"):
        await refusing.credentials()
    garbled = ContainerAwsCredentials(
        "http://169.254.170.2/creds",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="{}"))),
    )
    with pytest.raises(ObjectStoreError, match="without credentials"):
        await garbled.credentials()
    no_expiry = ContainerAwsCredentials(
        "http://169.254.170.2/creds",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"AccessKeyId": "A", "SecretAccessKey": "S"})
            )
        ),
    )
    assert (await no_expiry.credentials()).session_token == ""
    bad_date = ContainerAwsCredentials(
        "http://169.254.170.2/creds",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"AccessKeyId": "A", "SecretAccessKey": "S", "Expiration": "tomorrow"}
                )
            )
        ),
    )
    with pytest.raises(ObjectStoreError, match="not a date"):
        await bad_date.credentials()


def test_the_credentials_are_chosen_in_order(tmp_path: pathlib.Path) -> None:
    irsa = {"AWS_ROLE_ARN": "arn:aws:iam::1:role/r", "AWS_WEB_IDENTITY_TOKEN_FILE": str(tmp_path / "t")}
    pod = {"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials"}
    ecs = {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"}
    assert isinstance(aws_credentials("AKIA", "s", environ=irsa | pod), StaticAwsCredentials)
    assert isinstance(aws_credentials(environ=irsa | pod), WebIdentityAwsCredentials)
    assert isinstance(aws_credentials(environ=pod), ContainerAwsCredentials)
    from_ecs = aws_credentials(environ=ecs)
    assert isinstance(from_ecs, ContainerAwsCredentials)
    assert from_ecs.full_uri == "http://169.254.170.2/v2/credentials/abc"
    assert aws_credentials(environ={}) is None
    assert aws_credentials(environ={"AWS_ROLE_ARN": "arn:aws:iam::1:role/r"}) is None, (
        "a role without a token"
    )
    regional = aws_credentials(region="ap-south-1", environ=irsa)
    assert isinstance(regional, WebIdentityAwsCredentials)
    assert regional.sts_endpoint == "https://sts.ap-south-1.amazonaws.com"
    custom = aws_credentials(environ=irsa | {"AWS_ENDPOINT_URL_STS": "https://sts.internal/"})
    assert isinstance(custom, WebIdentityAwsCredentials) and custom.sts_endpoint == "https://sts.internal"
    assert "secret" not in repr(StaticAwsCredentials("AKIA", "secret", "token"))


async def test_a_store_without_any_credentials_says_so() -> None:
    store = S3ObjectStore(
        "s3://choregos",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
    )
    with pytest.raises(ObjectStoreError, match="no AWS identity"):
        await store.check()
    with_identity = S3ObjectStore(
        "s3://choregos",
        credentials=StaticAwsCredentials("A", "S"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))),
    )
    with pytest.raises(ObjectStoreError, match=r"refused the credentials \(static keys\)"):
        await with_identity.check()


# ───────── Azure Workload Identity ─────────


async def test_azure_workload_identity_presents_the_federated_token_as_client_assertion(
    tmp_path: pathlib.Path,
) -> None:
    token_file = tmp_path / "azure-identity-token"
    token_file.write_text("federated-jwt")
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "entra-token", "expires_in": 3599})
        return httpx.Response(201)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    identity = AzureWorkloadIdentity.from_environment(
        {
            "AZURE_CLIENT_ID": "11111111-client",
            "AZURE_TENANT_ID": "22222222-tenant",
            "AZURE_FEDERATED_TOKEN_FILE": str(token_file),
            "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/",
        },
        client=client,
    )
    assert identity is not None
    store = AzureBlobObjectStore(
        "azblob://artifacts/choregos",
        endpoint="https://acme.blob.core.windows.net",
        token_source=identity,
        client=client,
    )
    await store.put("deck", b"PK", "application/zip")
    await store.put("deck2", b"PK", "application/zip")
    login = [r for r in seen if r.url.host == "login.microsoftonline.com"]
    assert len(login) == 1, "the token is cached"
    assert str(login[0].url) == "https://login.microsoftonline.com/22222222-tenant/oauth2/v2.0/token"
    form = parse_qs(login[0].content.decode())
    assert form["client_assertion"] == ["federated-jwt"]
    assert form["client_assertion_type"] == ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
    assert form["client_id"] == ["11111111-client"]
    assert form["scope"] == ["https://storage.azure.com/.default"]
    assert form["grant_type"] == ["client_credentials"]
    blob = [r for r in seen if r.url.host == "acme.blob.core.windows.net"]
    assert blob[0].headers["Authorization"] == "Bearer entra-token"
    assert AzureWorkloadIdentity.from_environment({"AZURE_CLIENT_ID": "x"}) is None


async def test_an_entra_refusal_names_its_error_and_never_the_assertion(tmp_path: pathlib.Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("federated-secret-jwt")

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_client", "error_description": "federated-secret-jwt"}
        )

    identity = AzureWorkloadIdentity(
        "client", "tenant", str(token_file), client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(ObjectStoreError, match="invalid_client") as raised:
        await identity.token()
    assert "federated-secret-jwt" not in str(raised.value)
    garbled = AzureWorkloadIdentity(
        "client",
        "tenant",
        str(token_file),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="nope"))),
    )
    with pytest.raises(ObjectStoreError, match="without an access token"):
        await garbled.token()
    html = AzureWorkloadIdentity(
        "client",
        "tenant",
        str(token_file),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="<html>"))),
    )
    with pytest.raises(ObjectStoreError, match="502"):
        await html.token()


# ───────── GKE Workload Identity ─────────


async def test_gke_workload_identity_asks_the_metadata_server() -> None:
    seen: list[httpx.Request] = []
    clock = Clock()

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "metadata.google.internal":
            n = sum(1 for r in seen if r.url.host == "metadata.google.internal")
            return httpx.Response(
                200, json={"access_token": f"ya29.{n}", "expires_in": 3599, "token_type": "Bearer"}
            )
        return httpx.Response(200, content=b"bytes")

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    token = GceMetadataToken(host="metadata.google.internal", client=client, clock=clock)
    store = GcsObjectStore("gs://choregos-files/prod", token_source=token, client=client)
    assert await store.get("deck") == b"bytes"
    assert await store.get("deck") == b"bytes"
    metadata = [r for r in seen if r.url.host == "metadata.google.internal"]
    assert len(metadata) == 1
    assert metadata[0].url.path == "/computeMetadata/v1/instance/service-accounts/default/token"
    assert metadata[0].headers["Metadata-Flavor"] == "Google"
    assert seen[-1].headers["Authorization"] == "Bearer ya29.1"
    clock.now += 3600
    await store.get("deck")
    assert seen[-1].headers["Authorization"] == "Bearer ya29.2", "refreshed once expired"


async def test_a_metadata_server_refusal_says_what_to_bind() -> None:
    refusing = GceMetadataToken(
        host="metadata.google.internal",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    with pytest.raises(ObjectStoreError, match="bound to a Google service account"):
        await refusing.token()
    garbled = GceMetadataToken(
        host="metadata.google.internal",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"expires_in": "x"}))
        ),
    )
    with pytest.raises(ObjectStoreError, match="without an access token"):
        await garbled.token()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    unreachable = GceMetadataToken(
        host="metadata.google.internal", client=httpx.AsyncClient(transport=httpx.MockTransport(down))
    )
    with pytest.raises(ObjectStoreError, match=r"metadata server .* unreachable"):
        await unreachable.token()


def test_a_bad_expires_in_is_read_as_an_hour() -> None:
    from tranibor_adapters.storage.tokens import _expiry

    assert _expiry({"expires_in": "soon"}, lambda: 0.0) == 3600.0
    assert _expiry({"expires_in": 60}, lambda: 10.0) == 70.0


# ───────── the settings pick the store and its identity ─────────


def test_the_kind_picks_the_store_and_the_settings_win_over_the_pod_identity(tmp_path: pathlib.Path) -> None:
    azure_env = {
        "AZURE_CLIENT_ID": "c",
        "AZURE_TENANT_ID": "t",
        "AZURE_FEDERATED_TOKEN_FILE": str(tmp_path / "token"),
    }
    s3 = build_object_store("s3", "s3://b", access_key="A", secret_key="S", session_token="T", environ={})
    assert isinstance(s3, S3ObjectStore) and isinstance(s3.credentials, StaticAwsCredentials)
    assert build_object_store("s3", "s3://b", environ={}).credentials is None  # type: ignore[attr-defined]

    azure = build_object_store(
        "azure-blob", "azblob://c", endpoint="https://a.blob.core.windows.net", environ=azure_env
    )
    assert isinstance(azure, AzureBlobObjectStore)
    assert isinstance(azure.token_source, AzureWorkloadIdentity)
    assert azure.authorisation == "Azure workload identity"
    keyed = build_object_store(
        "azure-blob",
        "azblob://c",
        endpoint="https://a.blob.core.windows.net",
        account_key="a2V5",
        environ=azure_env,
    )
    assert isinstance(keyed, AzureBlobObjectStore) and keyed.token_source is None
    assert keyed.authorisation == "shared key"

    gcs = build_object_store("gcs", "gs://b", environ={"GCE_METADATA_HOST": "169.254.169.254"})
    assert isinstance(gcs, GcsObjectStore) and isinstance(gcs.token_source, GceMetadataToken)
    assert gcs.token_source.host == "169.254.169.254"
    assert gcs.endpoint == "https://storage.googleapis.com"
    with pytest.raises(ValueError, match="unknown object store kind `minio`"):
        build_object_store("minio", "s3://b")
