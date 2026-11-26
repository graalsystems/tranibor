"""In-memory emulators of the three store protocols, as `httpx.MockTransport` handlers.

They answer what the real services answer for the calls the stores make (status codes, the Blob
service's `x-ms-error-code`), so that the conformance suite runs on every CI run; the real servers
(Azurite, fake-gcs-server, an S3 server) run it when Docker is asked for (`conftest.py`)."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, unquote

import httpx

Handler = Callable[[httpx.Request], httpx.Response]


def s3(buckets: set[str]) -> Handler:
    objects: dict[tuple[str, str], bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        bucket, _, key = request.url.path.lstrip("/").partition("/")
        if bucket not in buckets:
            return httpx.Response(404, text="<Error><Code>NoSuchBucket</Code></Error>")
        if not key:
            return httpx.Response(200 if request.method == "HEAD" else 405)
        if request.method == "PUT":
            objects[bucket, key] = request.content
            return httpx.Response(200)
        if request.method == "DELETE":
            objects.pop((bucket, key), None)
            return httpx.Response(204)
        if (bucket, key) not in objects:
            return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
        return httpx.Response(200, content=objects[bucket, key])

    return answer


def azure_blob(containers: set[str]) -> Handler:
    blobs: dict[tuple[str, str], bytes] = {}

    def error(status: int, code: str) -> httpx.Response:
        return httpx.Response(
            status, headers={"x-ms-error-code": code}, text=f"<Error><Code>{code}</Code></Error>"
        )

    def answer(request: httpx.Request) -> httpx.Response:
        if "x-ms-version" not in request.headers or "Authorization" not in request.headers:
            return error(403, "AuthenticationFailed")
        container, _, name = request.url.path.lstrip("/").partition("/")
        if container not in containers:
            return error(404, "ContainerNotFound")
        if not name:
            query = parse_qs(request.url.query.decode())
            if query.get("restype") == ["container"] and query.get("comp") == ["list"]:
                return httpx.Response(200, text="<EnumerationResults><Blobs/></EnumerationResults>")
            return error(400, "InvalidQueryParameterValue")
        if request.method == "PUT":
            if request.headers.get("x-ms-blob-type") != "BlockBlob":
                return error(400, "MissingRequiredHeader")
            blobs[container, name] = request.content
            return httpx.Response(201)
        if (container, name) not in blobs:
            return error(404, "BlobNotFound")
        if request.method == "DELETE":
            del blobs[container, name]
            return httpx.Response(202)
        return httpx.Response(200, content=blobs[container, name])

    return answer


def gcs(buckets: set[str]) -> Handler:
    objects: dict[tuple[str, str], bytes] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?", 1)[0]
        query = parse_qs(request.url.query.decode())
        segments = raw.lstrip("/").split("/")
        upload = segments[0] == "upload"
        if upload:
            segments = segments[1:]
        # storage/v1/b/<bucket>/o[/<object>]
        if segments[:3] != ["storage", "v1", "b"] or len(segments) < 5 or segments[4] != "o":
            return httpx.Response(400)
        bucket = segments[3]
        if bucket not in buckets:
            return httpx.Response(404, json={"error": {"code": 404, "message": "bucket not found"}})
        if upload and request.method == "POST":
            objects[bucket, query["name"][0]] = request.content
            return httpx.Response(200, json={"name": query["name"][0]})
        if len(segments) == 5:
            return httpx.Response(200, json={"kind": "storage#objects"})
        name = unquote(segments[5])
        if (bucket, name) not in objects:
            return httpx.Response(404, json={"error": {"code": 404}})
        if request.method == "DELETE":
            del objects[bucket, name]
            return httpx.Response(204)
        if query.get("alt") != ["media"]:
            return httpx.Response(200, json={"name": name})
        return httpx.Response(200, content=objects[bucket, name])

    return answer
