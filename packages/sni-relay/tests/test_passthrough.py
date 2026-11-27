# SPDX-License-Identifier: Apache-2.0
"""The SNI relay routes TLS without terminating it (ADR 0230 §1-4, §6-9; S87-42).

Everything runs in this process, on 127.0.0.1: a relay, an installation's agent paired with it
through its HTTP paths, the installation's edge (a PROXY v2 listener in front of a TLS server holding
the installation's key) and a phone, a TLS client that trusts the test CA. The installation's key is
made after the relay started and is handed to the edge only: whatever certificate the phone sees,
the relay could not have presented it.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import datetime as dt
import json
import logging
import re
import ssl
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import tranibor_contracts as contracts
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jsonschema import Draft202012Validator
from tranibor_contracts.relay import (
    RELAY_ID_PATTERN,
    RelayOpenStream,
    request_signature_claims,
    request_signature_header,
)
from tranibor_sni_relay import passthrough
from tranibor_sni_relay.agent import RelayAgent, RelayRefusedError
from tranibor_sni_relay.passthrough import Relay, RelayConfigError

from ._kit import (
    DOMAIN,
    OWNER,
    SECRET,
    Bench,
    answer,
    bench,
    client_hello,
    closed_without_a_byte,
    config,
    control,
    dial_back,
    eventually,
    raw_phone,
    signed_hello,
)

LINE = Draft202012Validator(
    contracts.load_schema("relay-metadata-line.schema.json"),
    format_checker=Draft202012Validator.FORMAT_CHECKER,
)


def _public(key: ed25519.Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _register(kit: Bench, key: ed25519.Ed25519PrivateKey) -> str:
    """An installation bound in the relay's registry directly, as the owner's approval binds it."""
    pairing = kit.relay.registry.begin(_public(key))
    return kit.relay.registry.approve(pairing.user_code, OWNER).id


async def _paired(kit: Bench, **options: Any) -> tuple[RelayAgent, str, Any]:
    agent = kit.agent(**options)
    relay_id = await kit.pair(agent)
    task = await kit.connect(agent, relay_id)
    return agent, relay_id, task


async def _settled(kit: Bench) -> None:
    await asyncio.wait_for(kit.relay.settle(), 10)


# ───────────────────────── the package stands alone ─────────────────────────


def test_importing_the_package_loads_no_platform_module() -> None:
    """ADR 0230 §1: the relay imports nothing of the API, the orchestrator or the runner."""
    probe = (
        "import sys, tranibor_sni_relay, tranibor_sni_relay.passthrough, tranibor_sni_relay.agent;"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'tranibor_api', 'tranibor_orchestrator', 'tranibor_runner', 'tranibor_core'}))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]", result.stdout


def test_the_relay_serves_every_path_of_its_contract_and_no_other() -> None:
    """`relay.yaml` and the relay's routes say the same thing: four paths, each with its verb."""
    contract = {
        (verb.upper(), path.replace("{stream}", ""))
        for path, item in contracts.load_relay()["paths"].items()
        for verb in item
        if verb in {"get", "post"}
    }
    served = {
        ("POST", passthrough.PAIRING_PATH),
        ("POST", passthrough.POLL_PATH),
        ("GET", passthrough.CONTROL_PATH),
        ("GET", passthrough.DATA_PREFIX),
    }
    assert contract == served


# ───────────────────────── the passthrough ─────────────────────────


async def test_a_tls_client_through_the_relay_sees_the_installation_s_certificate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with bench(tmp_path) as kit:
        agent = kit.agent()
        relay_id = await kit.pair(agent)
        edge, issued = await kit.installation(relay_id)
        agent.target = ("127.0.0.1", edge.port)
        await kit.connect(agent, relay_id)
        name = f"{relay_id}.{DOMAIN}"

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                "127.0.0.1", kit.relay.sni_port, ssl=kit.phone_context, server_hostname=name
            ),
            10,
        )
        tls = writer.get_extra_info("ssl_object")
        assert isinstance(tls, ssl.SSLObject)
        assert tls.getpeercert(binary_form=True) == issued.der, (
            "the phone sees the installation's own certificate"
        )
        writer.write(SECRET)
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(len(SECRET) + 5), 5) == b"edge:" + SECRET
        writer.close()
        await _settled(kit)
        await edge.stop()

    assert edge.received == [SECRET], "the edge decrypted what the phone sent"
    assert edge.proxied_from == ["127.0.0.1"], "the agent wrote the phone's address in a PROXY v2 header"
    [line] = kit.lines()
    assert not list(LINE.iter_errors(line))
    assert line["install_id"] == relay_id
    assert line["close_reason"] == "client_closed"
    assert line["bytes_in"] > len(SECRET) and line["bytes_out"] > len(SECRET)
    log = kit.relay.metadata.path.read_text(encoding="utf-8")
    assert name not in log and DOMAIN not in log, "no server name is kept"
    assert not [record for record in caplog.records if record.name.startswith("tranibor_sni_relay")]
    assert not [record for record in caplog.records if name in record.getMessage()], "nothing logs the name"


def _reachable(root: object) -> Iterator[object]:
    """Every object the relay holds through its own objects and plain containers. It stops at what is
    not the relay's (the event loop, a server, a task), whose state is not the relay's to hold."""
    seen: set[int] = set()
    pending: deque[object] = deque([root])
    while pending:
        item = pending.popleft()
        if id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list | tuple | set | frozenset | deque):
            pending.extend(item)
        elif type(item).__module__.startswith("tranibor_sni_relay"):
            if dataclasses.is_dataclass(item):
                pending.extend(getattr(item, f.name) for f in dataclasses.fields(item))
            if hasattr(item, "__dict__"):
                pending.extend(vars(item).values())
            for slot in getattr(type(item), "__slots__", ()):
                if hasattr(item, slot):
                    pending.append(getattr(item, slot))


async def test_the_relay_holds_no_private_key_for_the_name(tmp_path: Path) -> None:
    """ADR 0230 §4: no certificate and no key for any `<id>` name. The relay's own host has its own
    certificate, for its own name, and that context is the only TLS material it holds."""
    async with bench(tmp_path) as kit:
        agent, relay_id, _ = await _paired(kit)
        edge, _issued = await kit.installation(relay_id)
        agent.target = ("127.0.0.1", edge.port)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", kit.relay.sni_port, ssl=kit.phone_context, server_hostname=f"{relay_id}.{DOMAIN}"
        )
        held = list(_reachable(kit.relay))
        writer.close()
        await _settled(kit)
        await edge.stop()

        keys = [
            o
            for o in held
            if isinstance(o, ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey)
        ]
        assert not keys, f"the relay holds private keys: {keys}"
        pems = [o for o in held if isinstance(o, str | bytes) and "PRIVATE KEY" in str(o)]
        assert not pems
        contexts = [o for o in held if isinstance(o, ssl.SSLContext)]
        assert contexts == [kit.relay.config.own_tls], "the only TLS context is the relay's own host's"
        assert len(held) > 50, "the walk reached the relay's state"

        # The SNI listener speaks no TLS: a ClientHello for the relay's own domain gets no ServerHello.
        reader, writer = await raw_phone(kit.relay.sni_port, client_hello(DOMAIN))
        assert await closed_without_a_byte(reader)
        writer.close()


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        (client_hello("aaaaaaaaaaaa.relay.test"), "unknown_id"),
        (client_hello("not-an-id.relay.test"), "unknown_id"),
        (client_hello("aaaaaaaaaaaa.example.test"), "outside_domain"),
        (client_hello("x.aaaaaaaaaaaa.relay.test"), "outside_domain"),
        (client_hello(None), "no_sni"),
        (b"GET / HTTP/1.1\r\nHost: aaaaaaaaaaaa.relay.test\r\n\r\n", "malformed_client_hello"),
    ],
    ids=["unknown-id", "no-id-label", "outside-domain", "two-labels", "no-sni", "not-tls"],
)
async def test_a_refused_client_hello_is_closed_without_a_byte(
    tmp_path: Path, first: bytes, reason: str
) -> None:
    async with bench(tmp_path) as kit:
        reader, writer = await raw_phone(kit.relay.sni_port, first)
        assert await closed_without_a_byte(reader)
        writer.close()
        await _settled(kit)
    [line] = kit.lines()
    assert not list(LINE.iter_errors(line))
    assert line["close_reason"] == reason and line["install_id"] is None
    assert line["bytes_out"] == 0


async def test_a_client_hello_that_never_completes_is_closed_at_its_bound(tmp_path: Path) -> None:
    async with bench(tmp_path, client_hello_timeout_s=0.3) as kit:
        hello = client_hello("aaaaaaaaaaaa.relay.test")
        reader, writer = await raw_phone(kit.relay.sni_port, hello[:20])
        assert await closed_without_a_byte(reader)
        writer.close()
        await _settled(kit)
    [line] = kit.lines()
    # The record's header was read; its body never completed, and is counted as never read.
    assert line["close_reason"] == "client_hello_timeout" and line["bytes_in"] == 5


async def test_an_installation_without_a_control_session_is_closed_and_nothing_is_stored(
    tmp_path: Path,
) -> None:
    """ADR 0230 §9: no store-and-forward; the line is all that is written."""
    async with bench(tmp_path) as kit:
        relay_id = _register(kit, ed25519.Ed25519PrivateKey.generate())
        hello = client_hello(f"{relay_id}.{DOMAIN}")
        reader, writer = await raw_phone(kit.relay.sni_port, hello + b"GET /api/v1/inbox")
        assert await closed_without_a_byte(reader)
        writer.close()
        await _settled(kit)
        assert kit.relay.pending_streams() == 0
    [line] = kit.lines()
    assert line == {**line, "install_id": relay_id, "close_reason": "no_control_session", "bytes_out": 0}
    assert line["bytes_in"] == len(hello), "the relay read the ClientHello and nothing after it"
    assert [path.name for path in tmp_path.iterdir() if path.suffix == ".jsonl"] == ["relay-metadata.jsonl"]


# ───────────────────────── the dial-back and its ticket ─────────────────────────


async def test_a_replayed_ticket_is_refused(tmp_path: Path) -> None:
    """ADR 0230 §3: single use, bound to its stream. The bytes after the 101 are the phone's, the
    ClientHello first, unchanged."""
    async with bench(tmp_path) as kit:
        agent, relay_id, _ = await _paired(kit, dial_back=False)
        hello = client_hello(f"{relay_id}.{DOMAIN}")
        first_reader, first_writer = await raw_phone(kit.relay.sni_port, hello + b"plain:first")
        await eventually(lambda: len(agent.orders) == 1)
        _second_reader, second_writer = await raw_phone(kit.relay.sni_port, hello + b"plain:second")
        await eventually(lambda: len(agent.orders) == 2)
        one, two = agent.orders

        status, body, *_ = await dial_back(kit, two.stream, one.ticket)
        assert (status, json.loads(body)["code"]) == (401, "ticket_refused"), "another stream's ticket"
        status, body, *_ = await dial_back(kit, "Zt1eKb7yQ4mWn0Xc9RpA3g", one.ticket)
        assert (status, json.loads(body)["code"]) == (401, "ticket_refused"), "an unknown stream"

        status, _, reader, writer = await dial_back(kit, one.stream, one.ticket)
        assert status == 101
        assert await asyncio.wait_for(reader.readexactly(len(hello) + 11), 5) == hello + b"plain:first"
        writer.write(b"from the installation")
        await writer.drain()
        assert await asyncio.wait_for(first_reader.readexactly(21), 5) == b"from the installation"

        status, body, *_ = await dial_back(kit, one.stream, one.ticket)
        assert (status, json.loads(body)["code"]) == (401, "ticket_refused"), "the same ticket, again"

        writer.close()
        assert await asyncio.wait_for(first_reader.read(), 5) == b"", (
            "the installation closing closes the phone"
        )
        status, _, reader_two, _writer_two = await dial_back(kit, two.stream, two.ticket)
        assert status == 101
        assert await asyncio.wait_for(reader_two.readexactly(len(hello) + 12), 5) == hello + b"plain:second"
        second_writer.close()
        assert await asyncio.wait_for(reader_two.read(), 5) == b"", (
            "the phone closing closes the installation"
        )
        first_writer.close()
        await _settled(kit)

    lines = kit.lines()
    assert sorted(line["close_reason"] for line in lines) == ["client_closed", "installation_closed"]
    log = kit.relay.metadata.path.read_text(encoding="utf-8")
    assert "plain:" not in log and "from the installation" not in log, "no byte of a stream is kept"
    assert all(not list(LINE.iter_errors(line)) for line in lines)


async def test_a_dial_back_that_does_not_come_closes_the_phone(tmp_path: Path) -> None:
    async with bench(tmp_path, ticket_ttl_s=0.3) as kit:
        agent, relay_id, _ = await _paired(kit, dial_back=False)
        reader, writer = await raw_phone(kit.relay.sni_port, client_hello(f"{relay_id}.{DOMAIN}"))
        assert await closed_without_a_byte(reader)
        status, body, *_ = await dial_back(kit, agent.orders[0].stream, agent.orders[0].ticket)
        assert (status, json.loads(body)["code"]) == (401, "ticket_refused"), "an expired ticket"
        writer.close()
        await _settled(kit)
    [line] = kit.lines()
    assert (line["install_id"], line["close_reason"]) == (relay_id, "dial_back_timeout")


async def test_the_agent_refuses_an_order_naming_a_host_and_connects_nowhere(tmp_path: Path) -> None:
    """ADR 0230 §3: `relay-open-stream` is closed; an order with a host or a port is refused."""
    async with bench(tmp_path, ticket_ttl_s=0.5) as kit:
        agent, relay_id, _ = await _paired(kit)
        session = kit.relay.control.session(relay_id)
        assert session is not None

        async def open_stream(order: RelayOpenStream) -> None:
            await session.send({**order.model_dump(mode="json"), "host": "198.51.100.9", "port": 22})

        session.open_stream = open_stream  # type: ignore[method-assign]
        reader, writer = await raw_phone(kit.relay.sni_port, client_hello(f"{relay_id}.{DOMAIN}"))
        assert await closed_without_a_byte(reader)
        writer.close()
        await _settled(kit)
    assert len(agent.refused) == 1 and not agent.orders
    assert agent.dialled == [] and agent.targets_opened == 0, "the agent connected nowhere"
    [line] = kit.lines()
    assert line["close_reason"] == "dial_back_timeout"


# ───────────────────────── the control channel ─────────────────────────


@pytest.mark.parametrize(
    "case", ["other-key", "other-relay", "stale-nonce", "stale-iat", "unknown-id", "junk"]
)
async def test_a_bad_hello_is_refused_and_the_live_session_kept(tmp_path: Path, case: str) -> None:
    async with bench(tmp_path) as kit:
        agent, relay_id, task = await _paired(kit)
        live = kit.relay.control.session(relay_id)
        async with control(kit) as socket:
            challenge = await answer(socket)
            assert challenge["type"] == "challenge"
            key, url, nonce, iat, hello_id = (
                agent.key,
                kit.relay.control_url,
                challenge["nonce"],
                None,
                relay_id,
            )
            expected = {
                "other-key": "invalid_signature",
                "other-relay": "invalid_signature",
                "stale-nonce": "stale_nonce",
                "stale-iat": "stale_iat",
                "unknown-id": "unknown_id",
                "junk": "invalid_message",
            }[case]
            if case == "other-key":
                key = ed25519.Ed25519PrivateKey.generate()
            elif case == "other-relay":
                url = "wss://another-relay.test/relay/v1/control"
            elif case == "stale-nonce":
                nonce = "AAAAAAAAAAAAAAAAAAAAAA"
            elif case == "stale-iat":
                iat = int(time.time()) - 61
            elif case == "unknown-id":
                hello_id = "aaaaaaaaaaaa"
            message = (
                '{"type": "hello", "id": 1}'
                if case == "junk"
                else signed_hello(key, url=url, relay_id=hello_id, nonce=nonce, iat=iat)
            )
            await socket.send(message)
            refusal = await answer(socket)
            assert (refusal["type"], refusal["code"]) == ("close", expected)
        assert kit.relay.control.session(relay_id) is live, "the live session is kept"
        assert not task.done()


async def test_a_valid_hello_replaces_the_live_session(tmp_path: Path) -> None:
    async with bench(tmp_path) as kit:
        agent, relay_id, task = await _paired(kit)
        live = kit.relay.control.session(relay_id)
        async with control(kit) as socket:
            challenge = await answer(socket)
            await socket.send(
                signed_hello(
                    agent.key, url=kit.relay.control_url, relay_id=relay_id, nonce=challenge["nonce"]
                )
            )
            welcome = await answer(socket)
            assert welcome["type"] == "welcome" and welcome["hostname"] == f"{relay_id}.{DOMAIN}"
            closed = await asyncio.wait_for(task, 5)
            assert closed is not None and closed.code == "replaced"
            assert kit.relay.control.session(relay_id) not in (None, live)


async def test_the_relay_serves_its_protocol_and_the_one_before_and_refuses_older(tmp_path: Path) -> None:
    """ADR 0230 §2: a relay of protocol 3 serves 3 and 2; a hello of 1 is refused with the window."""
    async with bench(tmp_path, protocol=3) as kit:
        key = ed25519.Ed25519PrivateKey.generate()
        relay_id = _register(kit, key)
        for protocol, expected in ((2, "welcome"), (1, "close")):
            async with control(kit) as socket:
                challenge = await answer(socket)
                assert challenge["protocol"] == {"current": 3, "minimum": 2}
                await socket.send(
                    signed_hello(
                        key,
                        url=kit.relay.control_url,
                        relay_id=relay_id,
                        nonce=challenge["nonce"],
                        protocol=protocol,
                    )
                )
                reply = await answer(socket)
                assert reply["type"] == expected
                if expected == "close":
                    assert reply["code"] == "protocol_unsupported"
                    assert reply["protocol"] == {"current": 3, "minimum": 2}


async def test_a_suspended_id_is_closed_and_its_hello_refused(tmp_path: Path) -> None:
    """ADR 0230 §8."""
    async with bench(tmp_path) as kit:
        agent, relay_id, task = await _paired(kit)
        await kit.relay.suspend(relay_id, "abuse report 2026-11-27")
        closed = await asyncio.wait_for(task, 5)
        assert closed is not None and closed.code == "suspended", "the live session is ended"
        reader, writer = await raw_phone(kit.relay.sni_port, client_hello(f"{relay_id}.{DOMAIN}"))
        assert await closed_without_a_byte(reader)
        writer.close()
        async with control(kit) as socket:
            challenge = await answer(socket)
            await socket.send(
                signed_hello(
                    agent.key, url=kit.relay.control_url, relay_id=relay_id, nonce=challenge["nonce"]
                )
            )
            assert (await answer(socket))["code"] == "suspended"
        await _settled(kit)
    [line] = kit.lines()
    assert (line["install_id"], line["close_reason"]) == (relay_id, "suspended")


# ───────────────────────── the pairing ─────────────────────────


async def test_the_relay_draws_the_id_and_refuses_a_pairing_that_names_one(tmp_path: Path) -> None:
    """ADR 0230 §6: nobody chooses an id; a pairing request naming one is refused as an extra field."""
    async with bench(tmp_path) as kit:
        agent = kit.agent()
        url = kit.relay.origin + "/relay/v1/pairing"
        body = json.dumps(
            {"protocol": 1, "agent_version": "0.30.0", "signing_key": agent.signing_key, "id": "aaaaaaaaaaaa"}
        ).encode()
        claims = request_signature_claims(method="POST", url=url, body=body, iat=int(time.time()))
        signature = jwt.encode(claims, agent.key, algorithm="EdDSA", headers=request_signature_header())
        async with httpx.AsyncClient(verify=kit.ca.client_context(), trust_env=False) as client:
            refused = await client.post(url, content=body, headers={"Relay-Signature": signature})
        assert (refused.status_code, refused.json()["code"]) == (422, "invalid_request")

        pending = await agent.pair()
        assert (
            pending.status == "pending" and pending.user_code is not None and pending.device_code is not None
        )
        with pytest.raises(RelayRefusedError) as soon:
            await agent.poll(pending.device_code)
        assert soon.value.problem.code == "rate_limited" and soon.value.problem.retry_after
        kit.relay.registry.approve(pending.user_code, OWNER)
        kit.clock.advance(5)
        approved = await agent.poll(pending.device_code)
        other = await kit.pair(kit.agent())

    assert approved.status == "approved" and approved.id is not None
    assert re.fullmatch(RELAY_ID_PATTERN, approved.id) and re.fullmatch(RELAY_ID_PATTERN, other)
    assert approved.hostname == f"{approved.id}.{DOMAIN}"
    assert other != approved.id
    assert kit.relay.registry.installation(approved.id).account == OWNER  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("too-large", 413, "body_too_large"),
        ("old-protocol", 426, "protocol_unsupported"),
        ("no-signature", 401, "invalid_signature"),
        ("other-key", 401, "invalid_signature"),
        ("other-url", 401, "request_mismatch"),
        ("stale-iat", 401, "stale_iat"),
        ("unknown-device-code", 404, "unknown_device_code"),
    ],
)
async def test_a_pairing_request_is_refused_in_the_contract_s_order(
    tmp_path: Path, case: str, status: int, code: str
) -> None:
    """`x-request-signature`: size, protocol, schema, signature, what it binds, `iat`, then the path."""
    async with bench(tmp_path) as kit:
        key = ed25519.Ed25519PrivateKey.generate()
        path = "/relay/v1/pairing/poll" if case == "unknown-device-code" else "/relay/v1/pairing"
        url = kit.relay.origin + path
        document: dict[str, Any] = {"protocol": 1, "agent_version": "0.30.0", "signing_key": _public(key)}
        if case == "unknown-device-code":
            document = {"protocol": 1, "device_code": "Gm4cJx0aT9qVb2Rk7LsNw1EyZp5Hd8Ui3Fo6Ct0Xj2Q"}
        elif case == "too-large":
            document["agent_version"] = "0" * 5000
        elif case == "old-protocol":
            document["protocol"] = 0
        body = json.dumps(document).encode()
        signer = ed25519.Ed25519PrivateKey.generate() if case == "other-key" else key
        claims = request_signature_claims(
            method="POST",
            url=kit.relay.origin + "/relay/v1/other" if case == "other-url" else url,
            body=body,
            iat=int(time.time()) - (120 if case == "stale-iat" else 0),
        )
        headers = (
            {}
            if case == "no-signature"
            else {
                "Relay-Signature": jwt.encode(
                    claims, signer, algorithm="EdDSA", headers=request_signature_header()
                )
            }
        )
        async with httpx.AsyncClient(verify=kit.ca.client_context(), trust_env=False) as client:
            refused = await client.post(url, content=body, headers=headers)
    assert (refused.status_code, refused.json()["code"]) == (status, code)
    assert "server_time" in refused.json()
    if code == "protocol_unsupported":
        assert refused.json()["protocol"] == {"current": 1, "minimum": 1}


async def test_a_session_that_answers_its_pings_is_kept(tmp_path: Path) -> None:
    async with bench(tmp_path, ping_interval_s=0.05) as kit:
        _agent, relay_id, task = await _paired(kit)
        live = kit.relay.control.session(relay_id)
        await asyncio.sleep(0.4)
        assert kit.relay.control.session(relay_id) is live and not task.done()


# ───────────────────────── limits ─────────────────────────


async def test_the_limit_of_a_client_address_closes_before_anything_is_read(tmp_path: Path) -> None:
    async with bench(
        tmp_path, limits=dataclasses.replace(config(tmp_path, None).limits, client_connections_per_minute=2)
    ) as kit:
        for _ in range(3):
            reader, writer = await raw_phone(kit.relay.sni_port, client_hello("aaaaaaaaaaaa.relay.test"))
            assert await closed_without_a_byte(reader)
            writer.close()
        await _settled(kit)
    reasons = [line["close_reason"] for line in kit.lines()]
    assert sorted(reasons) == ["limit_client_connections_per_minute", "unknown_id", "unknown_id"]
    [limited] = [line for line in kit.lines() if line["close_reason"].startswith("limit")]
    assert limited["bytes_in"] == 0 and limited["install_id"] is None


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        ({"concurrent_streams": 1}, "limit_concurrent_streams"),
        ({"streams_per_minute": 1}, "limit_streams_per_minute"),
    ],
)
async def test_a_limit_of_the_installation_closes_and_the_line_says_which(
    tmp_path: Path, limit: dict[str, int], reason: str
) -> None:
    limits = dataclasses.replace(config(tmp_path, None).limits, **limit)
    async with bench(tmp_path, limits=limits, ticket_ttl_s=0.5) as kit:
        agent, relay_id, _ = await _paired(kit, dial_back=False)
        hello = client_hello(f"{relay_id}.{DOMAIN}")
        _first_reader, first_writer = await raw_phone(kit.relay.sni_port, hello)
        await eventually(lambda: len(agent.orders) == 1)
        reader, writer = await raw_phone(kit.relay.sni_port, hello)
        assert await closed_without_a_byte(reader)
        writer.close()
        first_writer.close()
        await _settled(kit)
    assert sorted(line["close_reason"] for line in kit.lines()) == sorted(["dial_back_timeout", reason])
    assert all(line["install_id"] == relay_id for line in kit.lines())


async def test_the_bytes_of_a_month_close_the_stream_that_passes_them_and_the_next(tmp_path: Path) -> None:
    limits = dataclasses.replace(config(tmp_path, None).limits, bytes_per_month=1)
    async with bench(tmp_path, limits=limits) as kit:
        agent, relay_id, _ = await _paired(kit, dial_back=False)
        hello = client_hello(f"{relay_id}.{DOMAIN}")
        phone_reader, phone_writer = await raw_phone(kit.relay.sni_port, hello)
        await eventually(lambda: len(agent.orders) == 1)
        status, _, _reader, writer = await dial_back(kit, agent.orders[0].stream, agent.orders[0].ticket)
        assert status == 101
        assert await closed_without_a_byte(phone_reader)
        again_reader, again_writer = await raw_phone(kit.relay.sni_port, hello)
        assert await closed_without_a_byte(again_reader)
        for each in (phone_writer, writer, again_writer):
            each.close()
        await _settled(kit)
    assert [line["close_reason"] for line in kit.lines()] == ["limit_bytes_per_month"] * 2
    assert len(agent.orders) == 1, "past the month's bytes, no stream is opened"


# ───────────────────────── the metadata lines ─────────────────────────


async def test_a_line_past_the_retention_is_deleted(tmp_path: Path) -> None:
    async with bench(tmp_path) as kit:
        kit.clock.advance(-dt.timedelta(days=8).total_seconds())
        reader, writer = await raw_phone(kit.relay.sni_port, client_hello("aaaaaaaaaaaa.relay.test"))
        assert await closed_without_a_byte(reader)
        writer.close()
        await _settled(kit)
        kit.clock.offset = 0
        reader, writer = await raw_phone(kit.relay.sni_port, client_hello(None))
        assert await closed_without_a_byte(reader)
        writer.close()
        await _settled(kit)
        assert len(kit.lines()) == 2
        assert await kit.relay.metadata.prune() == 1
    assert [line["close_reason"] for line in kit.lines()] == ["no_sni"]


@pytest.mark.parametrize("retention", [None, dt.timedelta(0)])
def test_no_retention_no_start(tmp_path: Path, retention: dt.timedelta | None) -> None:
    with pytest.raises(RelayConfigError, match="retention"):
        Relay(config(tmp_path, None, retention=retention))
