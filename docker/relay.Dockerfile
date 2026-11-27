# The relay a personal installation dials out to (ADR 0230): `choregos-relay serve`.
#
# Not one of the platform's workloads, and nothing of the platform runs it: whoever operates a relay
# runs this image (`charts/choregos-relay`, `docs/relay.md`). It routes a phone's TLS by SNI and never
# terminates it, so the image holds no certificate and no key for any installation's name. Nothing of
# the operator's is baked in: the domain, the retention and the limits are variables, the own host's
# certificate and the operator's Secret are mounted. Two ports, both above 1024 so that a user
# without any capability opens them: the SNI listener (9443, which a Service publishes on 443) and
# the relay's own host (8443).
# Extra certificate authorities for the download below, EMPTY here (`docker/build-ca.sh`): a build
# behind a TLS-inspecting proxy passes its authority with `--build-context build-ca=<directory>`
# (`docs/deployment.md`, "Building behind a TLS-inspecting proxy"). Only the builder mounts it, and
# the runtime stage copies nothing of it: the image does not carry the authority.
FROM scratch AS build-ca

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.5.11@sha256:0ac957607303916420297a4c9c213bb33fbd3c888f9cd7f4f7273596ebf42b85 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY packages packages
COPY compat compat
# Workspace members (ADR 0181): uv reads every member's metadata, and this image installs none of their code.
COPY plugins/investment-research/pyproject.toml plugins/investment-research/
COPY plugins/software-delivery/pyproject.toml plugins/software-delivery/
COPY plugins/procurement/pyproject.toml plugins/procurement/
COPY tests/testkit/pyproject.toml tests/testkit/
COPY plugins/vault-transit/pyproject.toml plugins/vault-transit/
COPY apps/api/pyproject.toml apps/api/
COPY apps/orchestrator/pyproject.toml apps/orchestrator/
# `--no-editable`: the relay and the one workspace package it uses (the contract: its messages, its
# bounds, the metadata line's schema) are installed INTO the virtual environment. The runtime stage
# copies that environment and nothing else of the repository.
RUN --mount=type=bind,from=build-ca,target=/mnt/build-ca --mount=type=bind,source=docker/build-ca.sh,target=/mnt/build-ca.sh \
    . /mnt/build-ca.sh && mkdir -p apps/api/src apps/orchestrator/src \
 && uv sync --frozen --no-dev --no-editable --package tranibor-sni-relay

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" \
    TRANIBOR_RELAY_SNI_PORT=9443 \
    TRANIBOR_RELAY_OWN_PORT=8443 \
    TRANIBOR_RELAY_METADATA_PATH=/var/lib/choregos-relay/metadata.jsonl
# Debian's security fixes, applied at build time: the base image (a tag, rebuilt at its own pace)
# can carry a package already fixed in `bookworm-security`.
# The metadata lines' directory belongs to the relay's user and to nobody else; a deployment mounts
# a volume that outlives the container over it.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin choregos \
 && install -d -o 1000 -g 1000 -m 0700 /var/lib/choregos-relay
COPY --from=builder --chown=1000:1000 /app/.venv /app/.venv
USER 1000
# The relay must START in this image and find the contract it was built against: the wheel of
# `tranibor-contracts` carries `relay.yaml` and the metadata line's schema, and a relay that cannot
# validate a line writes none. Checked here, where a missing file fails the build.
RUN choregos-relay --version \
 && python -c "import tranibor_contracts as c; c.relay_path().read_text(); \
      c.load_schema('relay-metadata-line.schema.json')" \
 && TRANIBOR_RELAY_DOMAIN=relay.invalid TRANIBOR_RELAY_RETENTION_DAYS=1 choregos-relay check \
 && test ! -e /app/packages
EXPOSE 9443 8443
ENTRYPOINT ["/usr/bin/tini", "--", "choregos-relay"]
CMD ["serve"]
