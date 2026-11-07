# O evolve sandbox: where candidate upgrades are tested. Built once by evolve/sandbox.py ensure_image() from this
# file and O's own engine/pyproject.toml (never from a candidate's files); runs with --network none.
# Bases are pinned by digest (python:3.12-slim-bookworm, node:22-bookworm-slim, resolved 2026-11-06).
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS node

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && node --version
COPY pyproject.toml /opt/o-deps/pyproject.toml
RUN mkdir -p /opt/o-deps/o_engine && touch /opt/o-deps/o_engine/__init__.py \
 && pip install --no-cache-dir "/opt/o-deps[dev]" \
 && pip uninstall -y o-engine \
 && rm -rf /opt/o-deps
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp/home
WORKDIR /work
