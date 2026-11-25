# SPDX-License-Identifier: Apache-2.0
"""The one way a Choregos process connects to Temporal: TLS and the payload codec (ADR 0183).

Every client and every worker of a deployment must agree on the data converter: a payload encrypted
by the API's client is decrypted by the worker that runs the workflow, and the reverse. Before this
module each process called `Client.connect(address, namespace=...)` on its own, in clear, with the
default converter (S80-06). Now they all call `connect_temporal`, which reads one configuration:

- TLS (`TRANIBOR_TEMPORAL_TLS`, `..._TLS_CA_FILE`, `..._TLS_CERT_FILE`, `..._TLS_KEY_FILE`,
  `..._TLS_SERVER_NAME`): the CA that signs the frontend's certificate, and for mutual TLS the
  client's certificate and key. A file named turns TLS on; a certificate without its key is refused.
- The payload codec (`TRANIBOR_TEMPORAL_CODEC_KEYS`, or `..._CODEC_KEYS_FILE` naming a file that
  holds the same text): `id:base64,id:base64`, 32-byte AES keys, the FIRST encrypts, all decrypt.
  The key id travels in each payload's metadata, so a key can rotate: put the new one first, keep
  the old one listed until no open history carries it.

Off by default (dev, kind, compose): an unconfigured process connects in clear with the default
converter, which is exactly what it did before, so the archived histories replay unchanged.

One master keyring, and one random data key per organisation (ADR 0225, superseding ADR 0220 §1 in
part): a process that installed a `DataKeySource` (the API and the workers, over the deployment's key
wrapper) seals an organisation's payloads under that organisation's data key, named in the metadata, so
that destroying the organisation's wrapped key makes them unreadable (crypto-shredding). A process without
one, and every payload written between ADR 0220 and ADR 0225, uses the key derived by HKDF(master key,
organisation id), which still decodes. A payload that belongs to no organisation, and every payload written
before ADR 0220, is under the master key itself. In staging and prod a process with no keyring refuses to
connect unless `TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS` says, in so many words, that clear payloads are
accepted.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.converter import (
    ActivitySerializationContext,
    DataConverter,
    DefaultFailureConverterWithEncodedAttributes,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.service import TLSConfig

from .production import is_production

ENV_TLS = "TRANIBOR_TEMPORAL_TLS"
ENV_TLS_CA_FILE = "TRANIBOR_TEMPORAL_TLS_CA_FILE"
ENV_TLS_CERT_FILE = "TRANIBOR_TEMPORAL_TLS_CERT_FILE"
ENV_TLS_KEY_FILE = "TRANIBOR_TEMPORAL_TLS_KEY_FILE"
ENV_TLS_SERVER_NAME = "TRANIBOR_TEMPORAL_TLS_SERVER_NAME"
ENV_CODEC_KEYS = "TRANIBOR_TEMPORAL_CODEC_KEYS"
ENV_CODEC_KEYS_FILE = "TRANIBOR_TEMPORAL_CODEC_KEYS_FILE"
#: The explicit opt-out: staging and prod start with no payload encryption only when this is true (ADR 0220).
ENV_ALLOW_CLEAR_PAYLOADS = "TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS"
ENV_ENVIRONMENT = "TRANIBOR_ENV"

#: The encoding an encrypted payload carries; the codec leaves every other payload as it found it.
ENCRYPTED_ENCODING = b"binary/encrypted"
#: The metadata key naming the key that encrypted the payload.
KEY_ID_METADATA = "encryption-key-id"
#: The metadata key naming the organisation whose derived key encrypted the payload (ADR 0220). Absent:
#: the master key itself, which is the platform key and what every payload written before ADR 0220 has.
ORG_METADATA = "encryption-org"
#: The metadata key naming the kind of key that sealed the payload (ADR 0225). `org-data-key`: the
#: organisation's random data key, whose id is `encryption-key-id`. Absent: the master keyring's key, or,
#: with `encryption-org`, the key ADR 0220 derives from it.
KEY_SOURCE_METADATA = "encryption-key-source"
ORG_DATA_KEY = b"org-data-key"
#: How long a process keeps an organisation's unwrapped data key (seconds). It bounds how long a process
#: that did not shred the key can still read and write under it (ADR 0225).
ENV_DATA_KEY_TTL = "TRANIBOR_TEMPORAL_DATA_KEY_TTL_S"
DEFAULT_DATA_KEY_TTL_S = 300.0
#: The memo key a workflow carries so that a process that did not start it learns its organisation.
ORG_MEMO = "organisation"
ORG_ID = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
HKDF_INFO = b"tranibor/temporal-payloads/org/v1:"
KEY_BYTES = 32
NONCE_BYTES = 12
KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
TRUE = frozenset({"1", "true", "yes", "on"})


class TemporalConfigError(ValueError):
    """The Temporal connection is configured in a way that cannot work: the process must not start."""


class UnknownCodecKeyError(Exception):
    """A payload was encrypted under a key this process does not hold (rotated out too early)."""


class ShreddedKeyError(UnknownCodecKeyError):
    """The organisation's data key was destroyed (crypto-shredding, ADR 0225): its payloads stay unread."""


class DataKeySource(Protocol):
    """Where an organisation's random data keys live (ADR 0225). Core ships no implementation: the API's
    is a table of keys wrapped by the deployment's `KeyWrapper` (ADR 0103)."""

    async def active_data_key(self, org_id: str) -> tuple[str, bytes]:
        """`(key id, 32-byte key)` that seals the organisation's new payloads; made when it has none."""
        ...

    async def data_key(self, org_id: str, key_id: str) -> bytes:
        """The key `key_id` of the organisation. Raises `ShreddedKeyError` for a destroyed key,
        `UnknownCodecKeyError` for one that does not exist."""
        ...


class DataKeyCache:
    """The unwrapped data keys a process holds, for `ttl_s` at most, shared by every codec of the process.

    `forget(org_id)` drops an organisation's keys at once (the process that shreds or rotates them); any
    other process drops them when the time runs out, so a shredded key stays usable there for `ttl_s` at
    most. A key that could not be read is not remembered: the next payload asks again.
    """

    def __init__(
        self,
        source: DataKeySource,
        *,
        ttl_s: float = DEFAULT_DATA_KEY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = 4096,
    ) -> None:
        self.source = source
        self._ttl_s = ttl_s
        self._clock = clock
        self._capacity = capacity
        self._active: dict[str, tuple[float, str, AESGCM]] = {}
        self._keys: OrderedDict[tuple[str, str], tuple[float, AESGCM]] = OrderedDict()

    def _fresh(self, since: float) -> bool:
        return self._clock() - since < self._ttl_s

    async def active(self, org_id: str) -> tuple[str, AESGCM]:
        cached = self._active.get(org_id)
        if cached is not None and self._fresh(cached[0]):
            return cached[1], cached[2]
        key_id, key = await self.source.active_data_key(org_id)
        if not KEY_ID.fullmatch(key_id) or len(key) != KEY_BYTES:
            raise TemporalConfigError("the organisation key source returned a malformed key")
        cipher = AESGCM(key)
        now = self._clock()
        self._active[org_id] = (now, key_id, cipher)
        self._remember((org_id, key_id), now, cipher)
        return key_id, cipher

    async def cipher(self, org_id: str, key_id: str) -> AESGCM:
        cached = self._keys.get((org_id, key_id))
        if cached is not None and self._fresh(cached[0]):
            return cached[1]
        key = await self.source.data_key(org_id, key_id)
        if len(key) != KEY_BYTES:
            raise TemporalConfigError("the organisation key source returned a malformed key")
        cipher = AESGCM(key)
        self._remember((org_id, key_id), self._clock(), cipher)
        return cipher

    def _remember(self, slot: tuple[str, str], at: float, cipher: AESGCM) -> None:
        self._keys[slot] = (at, cipher)
        self._keys.move_to_end(slot)
        while len(self._keys) > self._capacity:
            self._keys.popitem(last=False)
        if len(self._active) > self._capacity:
            self._active.clear()

    def forget(self, org_id: str | None = None) -> None:
        """Drop the organisation's keys (`None`: every organisation's) from this process."""
        if org_id is None:
            self._active.clear()
            self._keys.clear()
            return
        self._active.pop(org_id, None)
        for slot in [slot for slot in self._keys if slot[0] == org_id]:
            del self._keys[slot]

    def holds(self, org_id: str) -> bool:
        """Whether this process holds a key of the organisation (tests, diagnostics)."""
        return org_id in self._active or any(slot[0] == org_id for slot in self._keys)


_DATA_KEYS: list[DataKeyCache] = []


def install_data_keys(
    source: DataKeySource | None, *, ttl_s: float | None = None, env: Mapping[str, str] | None = None
) -> DataKeyCache | None:
    """Give every Temporal client this process connects from now on the organisations' data keys (ADR 0225).

    The API's lifespan and the worker's start call it with the database's source when the deployment has a
    key wrapper. `None` removes it: organisations' payloads are then sealed under the ADR 0220 derived keys.
    """
    _DATA_KEYS.clear()
    if source is None:
        return None
    values = os.environ if env is None else env
    if ttl_s is None:
        raw = values.get(ENV_DATA_KEY_TTL, "").strip()
        try:
            ttl_s = float(raw) if raw else DEFAULT_DATA_KEY_TTL_S
        except ValueError:
            raise TemporalConfigError(f"{ENV_DATA_KEY_TTL}: a number of seconds, not `{raw}`") from None
        if not 0 <= ttl_s <= 86_400:
            raise TemporalConfigError(f"{ENV_DATA_KEY_TTL}: between 0 and 86400 seconds")
    cache = DataKeyCache(source, ttl_s=ttl_s)
    _DATA_KEYS.append(cache)
    return cache


def installed_data_keys() -> DataKeyCache | None:
    return _DATA_KEYS[0] if _DATA_KEYS else None


@dataclass(frozen=True)
class TemporalSecurity:
    """What a process needs to talk to Temporal safely: TLS material and the codec's keys."""

    tls: bool = False
    ca_cert: bytes | None = None
    client_cert: bytes | None = None
    client_key: bytes | None = None
    server_name: str | None = None
    #: (key id, key), the first one encrypts.
    codec_keys: tuple[tuple[str, bytes], ...] = ()
    #: `TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS`: clear payloads accepted in staging and prod.
    allow_clear: bool = False
    #: The organisations' data keys (ADR 0225): what `install_data_keys` installed in this process.
    data_keys: DataKeyCache | None = field(default=None, compare=False, repr=False)

    @property
    def encrypts(self) -> bool:
        return bool(self.codec_keys)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TemporalSecurity:
        values = os.environ if env is None else env
        ca_file = values.get(ENV_TLS_CA_FILE, "").strip()
        cert_file = values.get(ENV_TLS_CERT_FILE, "").strip()
        key_file = values.get(ENV_TLS_KEY_FILE, "").strip()
        if bool(cert_file) != bool(key_file):
            raise TemporalConfigError(
                f"{ENV_TLS_CERT_FILE} and {ENV_TLS_KEY_FILE} go together: mutual TLS needs both"
            )
        tls = values.get(ENV_TLS, "").strip().lower() in TRUE or bool(ca_file or cert_file)
        inline = values.get(ENV_CODEC_KEYS, "").strip()
        keys_file = values.get(ENV_CODEC_KEYS_FILE, "").strip()
        if inline and keys_file:
            raise TemporalConfigError(f"{ENV_CODEC_KEYS} and {ENV_CODEC_KEYS_FILE}: set one, not both")
        keyring = _read(keys_file, ENV_CODEC_KEYS_FILE).decode().strip() if keys_file else inline
        return cls(
            tls=tls,
            ca_cert=_read(ca_file, ENV_TLS_CA_FILE) if ca_file else None,
            client_cert=_read(cert_file, ENV_TLS_CERT_FILE) if cert_file else None,
            client_key=_read(key_file, ENV_TLS_KEY_FILE) if key_file else None,
            server_name=values.get(ENV_TLS_SERVER_NAME, "").strip() or None,
            codec_keys=parse_codec_keys(keyring),
            allow_clear=values.get(ENV_ALLOW_CLEAR_PAYLOADS, "").strip().lower() in TRUE,
            data_keys=installed_data_keys(),
        )

    def refuse_clear_in_production(self, environment: str | None) -> None:
        """Stop a process that would write clear payloads in staging or prod, unless told to (ADR 0220)."""
        if self.encrypts or self.allow_clear or not is_production(environment):
            return
        raise TemporalConfigError(
            f"{ENV_ENVIRONMENT}={(environment or '').strip()} and no payload encryption: refused. Temporal "
            "histories would hold every ticket, result and decision in clear. Set "
            f"{ENV_CODEC_KEYS_FILE} (the chart's `global.temporal.codec`, on by default), or, knowing that "
            f"clear histories are acceptable here, set {ENV_ALLOW_CLEAR_PAYLOADS}=true "
            "(the chart's `global.temporal.codec.allowClear`)."
        )


def _read(path: str, variable: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise TemporalConfigError(f"{variable}={path}: {exc.strerror or exc}") from None


def parse_codec_keys(text: str) -> tuple[tuple[str, bytes], ...]:
    """`id:base64,id:base64` → ((id, key), ...); empty text, no key. Refuses a malformed keyring."""
    keys: list[tuple[str, bytes]] = []
    for entry in (part.strip() for part in text.split(",")):
        if not entry:
            continue
        key_id, _, encoded = entry.partition(":")
        if not KEY_ID.fullmatch(key_id) or not encoded:
            raise TemporalConfigError(
                f"{ENV_CODEC_KEYS}: expected `id:base64`, with an id of [A-Za-z0-9_-]{{1,32}}"
            )
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise TemporalConfigError(f"{ENV_CODEC_KEYS}: the key `{key_id}` is not valid base64") from None
        if len(key) != KEY_BYTES:
            raise TemporalConfigError(
                f"{ENV_CODEC_KEYS}: the key `{key_id}` must be 32 bytes, not {len(key)}"
            )
        if any(key_id == known for known, _ in keys):
            raise TemporalConfigError(f"{ENV_CODEC_KEYS}: the key id `{key_id}` is listed twice")
        keys.append((key_id, key))
    return tuple(keys)


def tls_config(security: TemporalSecurity) -> TLSConfig | bool:
    """What `Client.connect(tls=...)` takes: `False` in clear, a `TLSConfig` otherwise."""
    if not security.tls:
        return False
    return TLSConfig(
        server_root_ca_cert=security.ca_cert,
        domain=security.server_name,
        client_cert=security.client_cert,
        client_private_key=security.client_key,
    )


def refuse_clear_payloads(environment: str | None = None, env: Mapping[str, str] | None = None) -> None:
    """`refuse_clear_in_production` for a process that has not built its security yet (the API's start-up)."""
    values = os.environ if env is None else env
    TemporalSecurity.from_env(values).refuse_clear_in_production(
        values.get(ENV_ENVIRONMENT) if environment is None else environment
    )


def data_converter(security: TemporalSecurity) -> DataConverter:
    """The converter every client, worker and replayer of the deployment shares.

    Without keys, the SDK's default, unchanged. With keys, the default plus the AES-256-GCM codec,
    and failures whose message and stack trace go through the codec too: a failed activity's message
    can quote a ticket as easily as its result can. The organisations' data keys, when the process
    installed them (ADR 0225), seal what belongs to an organisation.
    """
    if not security.encrypts:
        return DataConverter.default
    return dataclasses.replace(
        DataConverter.default,
        payload_codec=AesGcmCodec(security.codec_keys, data_keys=security.data_keys),
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )


async def connect_temporal(
    address: str, namespace: str, *, security: TemporalSecurity | None = None
) -> Client:
    """Connect to Temporal the deployment's way (ADR 0183). `security` defaults to the environment."""
    resolved = TemporalSecurity.from_env() if security is None else security
    resolved.refuse_clear_in_production(os.environ.get(ENV_ENVIRONMENT))
    return await Client.connect(
        address,
        namespace=namespace,
        tls=tls_config(resolved),
        data_converter=data_converter(resolved),
    )


_SCOPE: ContextVar[str | None] = ContextVar("choregos_temporal_organisation", default=None)


def check_organisation(org_id: str) -> str:
    if not ORG_ID.fullmatch(org_id):
        raise ValueError("an organisation id for the payload codec is 1-128 of [A-Za-z0-9._:@-]")
    return org_id


@contextmanager
def organisation_scope(org_id: str | None) -> Iterator[None]:
    """Payloads encoded inside this block belong to `org_id` (`None`: to the platform). The API wraps the
    calls that start a workflow in it; a worker needs none, it learns the organisation of a workflow from
    what it decodes (`OrganisationDirectory`)."""
    token = _SCOPE.set(None if org_id is None else check_organisation(org_id))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def current_organisation() -> str | None:
    return _SCOPE.get()


class OrganisationDirectory:
    """workflow id -> organisation, learned from the payloads a process decodes or encodes, bounded.

    A workflow's activities and replies are encoded where the workflow's input was decoded, so the
    organisation is known by then; after a restart a replay decodes the history first. An id this process
    never saw is the platform's (and a client reads the workflow's memo, `ORG_MEMO`, before it signals).
    """

    def __init__(self, capacity: int = 20_000) -> None:
        self._capacity = capacity
        self._known: OrderedDict[str, str] = OrderedDict()

    def learn(self, workflow_id: str, org_id: str) -> None:
        self._known[workflow_id] = org_id
        self._known.move_to_end(workflow_id)
        while len(self._known) > self._capacity:
            self._known.popitem(last=False)

    def get(self, workflow_id: str) -> str | None:
        """The organisation, `""` for a workflow known to belong to none, `None` for an id never seen."""
        return self._known.get(workflow_id)


async def organisation_of_workflow(client: Client, workflow_id: str) -> str | None:
    """The organisation a running workflow belongs to, for a client that did not start it in this process.

    The directory first; on a miss, the workflow's memo (`ORG_MEMO`, written when the API started it).
    A workflow without one is the platform's, and that answer is remembered. No codec, no organisation,
    no call.
    """
    codec: Any = getattr(getattr(client, "data_converter", None), "payload_codec", None)
    if not isinstance(codec, AesGcmCodec):
        return None
    known = codec.directory.get(workflow_id)
    if known is not None:
        return known or None
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
        memo = await description.memo()
    except Exception:
        return None
    org_id = memo.get(ORG_MEMO)
    found = check_organisation(org_id) if isinstance(org_id, str) and ORG_ID.fullmatch(org_id) else ""
    codec.directory.learn(workflow_id, found)
    return found or None


def derive_organisation_key(master: bytes, org_id: str) -> bytes:
    """HKDF-SHA256(master, info = version + organisation id): one-way, so an organisation's key reveals
    neither the master key nor another organisation's key (ADR 0220). Kept to read what was sealed under it
    and for a process with no data key source; it cannot be shredded (ADR 0225)."""
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=HKDF_INFO + org_id.encode()
    ).derive(master)


class AesGcmCodec(PayloadCodec, WithSerializationContext):
    """AES-256-GCM over each whole serialized payload; the key id (and the organisation) ride in the metadata.

    The nonce is random per payload (12 bytes, prefixed to the ciphertext). A payload of an organisation
    is sealed under that organisation's random data key when the codec has `data_keys` (ADR 0225: it
    carries `encryption-org`, `encryption-key-source: org-data-key` and the data key's id), else under the
    key ADR 0220 derives from the master key (`encryption-org` only); a payload of no organisation, like
    every payload of ADR 0183, is sealed under the master key. The associated data is the key id, plus the
    organisation when there is one, plus the kind of key, so a payload cannot be passed off under another
    key, kind or organisation. Decoding needs no context: the metadata names the key. A payload that is
    not encrypted is decoded as it is: histories begun before the codec was switched on stay readable.
    """

    def __init__(
        self,
        keys: Sequence[tuple[str, bytes]],
        *,
        directory: OrganisationDirectory | None = None,
        workflow_id: str | None = None,
        data_keys: DataKeyCache | None = None,
    ) -> None:
        if not keys:
            raise TemporalConfigError("the payload codec needs at least one key")
        self._keys = tuple(keys)
        self._encrypting_id = keys[0][0]
        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys}
        self._derived: dict[tuple[str, str], AESGCM] = {}
        self._directory = directory if directory is not None else OrganisationDirectory()
        self._workflow_id = workflow_id
        self._data_keys = data_keys

    @property
    def key_id(self) -> str:
        return self._encrypting_id

    @property
    def data_keys(self) -> DataKeyCache | None:
        return self._data_keys

    @property
    def directory(self) -> OrganisationDirectory:
        return self._directory

    def with_context(self, context: SerializationContext) -> AesGcmCodec:
        workflow_id = (
            context.workflow_id
            if isinstance(context, WorkflowSerializationContext | ActivitySerializationContext)
            else None
        )
        bound = AesGcmCodec(
            self._keys, directory=self._directory, workflow_id=workflow_id, data_keys=self._data_keys
        )
        bound._derived = self._derived  # the derived keys are shared, they are a pure function of the master
        return bound

    def _organisation(self) -> str | None:
        scoped = _SCOPE.get()
        if scoped is not None:
            if self._workflow_id is not None:
                self._directory.learn(self._workflow_id, scoped)
            return scoped
        return (self._directory.get(self._workflow_id) or None) if self._workflow_id is not None else None

    def _cipher(self, key_id: str, org_id: str | None) -> AESGCM:
        if org_id is None:
            return self._ciphers[key_id]
        cached = self._derived.get((key_id, org_id))
        if cached is None:
            master = next(key for known, key in self._keys if known == key_id)
            cached = AESGCM(derive_organisation_key(master, org_id))
            if len(self._derived) > 4096:
                self._derived.clear()
            self._derived[(key_id, org_id)] = cached
        return cached

    @staticmethod
    def _associated(key_id: str, org_id: str | None) -> bytes:
        return key_id.encode() if org_id is None else f"{key_id}|org|{org_id}".encode()

    @staticmethod
    def _associated_data_key(key_id: str, org_id: str) -> bytes:
        return f"{key_id}|org-data-key|{org_id}".encode()

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        org_id = self._organisation()
        metadata: dict[str, bytes] = {"encoding": ENCRYPTED_ENCODING}
        if org_id is not None and self._data_keys is not None:
            key_id, cipher = await self._data_keys.active(org_id)
            associated = self._associated_data_key(key_id, org_id)
            metadata[KEY_ID_METADATA] = key_id.encode()
            metadata[KEY_SOURCE_METADATA] = ORG_DATA_KEY
        else:
            cipher = self._cipher(self._encrypting_id, org_id)
            associated = self._associated(self._encrypting_id, org_id)
            metadata[KEY_ID_METADATA] = self._encrypting_id.encode()
        if org_id is not None:
            metadata[ORG_METADATA] = org_id.encode()
        encoded: list[Payload] = []
        for payload in payloads:
            nonce = os.urandom(NONCE_BYTES)
            sealed = cipher.encrypt(nonce, payload.SerializeToString(), associated)
            encoded.append(Payload(metadata=metadata, data=nonce + sealed))
        return encoded

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        decoded: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding", b"") != ENCRYPTED_ENCODING:
                decoded.append(payload)
                continue
            key_id = payload.metadata.get(KEY_ID_METADATA, b"").decode(errors="replace")
            org_raw = payload.metadata.get(ORG_METADATA)
            org_id = None if org_raw is None else check_organisation(org_raw.decode(errors="replace"))
            source = payload.metadata.get(KEY_SOURCE_METADATA)
            if source is not None:
                cipher, associated = await self._data_key_cipher(source, key_id, org_id)
            else:
                if key_id not in self._ciphers:
                    raise UnknownCodecKeyError(
                        f"a payload is encrypted under the key `{key_id}`, "
                        f"which {ENV_CODEC_KEYS} no longer lists"
                    )
                cipher, associated = self._cipher(key_id, org_id), self._associated(key_id, org_id)
            plain = cipher.decrypt(payload.data[:NONCE_BYTES], payload.data[NONCE_BYTES:], associated)
            original = Payload()
            original.ParseFromString(plain)
            if org_id is not None and self._workflow_id is not None:
                self._directory.learn(self._workflow_id, org_id)
            decoded.append(original)
        return decoded

    async def _data_key_cipher(self, source: bytes, key_id: str, org_id: str | None) -> tuple[AESGCM, bytes]:
        if source != ORG_DATA_KEY:
            raise UnknownCodecKeyError("a payload names a kind of key this process does not know")
        if org_id is None or not KEY_ID.fullmatch(key_id):
            raise UnknownCodecKeyError("a payload sealed under a data key names no organisation")
        if self._data_keys is None:
            raise UnknownCodecKeyError(
                f"a payload of the organisation `{org_id}` is sealed under its data key `{key_id}`, and this "
                "process has no organisation key source (ADR 0225: the deployment's key wrapper)"
            )
        return await self._data_keys.cipher(org_id, key_id), self._associated_data_key(key_id, org_id)


__all__ = [
    "ENCRYPTED_ENCODING",
    "ENV_ALLOW_CLEAR_PAYLOADS",
    "ENV_CODEC_KEYS",
    "ENV_CODEC_KEYS_FILE",
    "ENV_DATA_KEY_TTL",
    "ENV_TLS",
    "ENV_TLS_CA_FILE",
    "ENV_TLS_CERT_FILE",
    "ENV_TLS_KEY_FILE",
    "ENV_TLS_SERVER_NAME",
    "KEY_ID_METADATA",
    "KEY_SOURCE_METADATA",
    "ORG_DATA_KEY",
    "ORG_MEMO",
    "ORG_METADATA",
    "AesGcmCodec",
    "DataKeyCache",
    "DataKeySource",
    "OrganisationDirectory",
    "ShreddedKeyError",
    "TemporalConfigError",
    "TemporalSecurity",
    "UnknownCodecKeyError",
    "connect_temporal",
    "current_organisation",
    "data_converter",
    "derive_organisation_key",
    "install_data_keys",
    "installed_data_keys",
    "organisation_of_workflow",
    "organisation_scope",
    "parse_codec_keys",
    "refuse_clear_payloads",
    "tls_config",
]
