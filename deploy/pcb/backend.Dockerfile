# Backend image of this fork: the upstream release image with this branch's Python on top.
#
# The fork changes only Python under backend/app (pipeline), so rebuilding KiCad, the Rust
# prism-kicad CLI and the simulation engines from source would only add risk and an hour.
# UPSTREAM must be the digest of the release this branch was cut from (v5.0.2-alpha), so
# every file copied here matches the image it lands in.
#   docker build -f deploy/pcb/backend.Dockerfile -t <tag> .
ARG UPSTREAM=ghcr.io/krishna-swaroop/kicad-prism-backend@sha256:24ee3f2137bab05b6e8769702a33a86e763274defc8b3524013cae14bd1e5ca0
FROM ${UPSTREAM}
COPY backend/app/ /app/app/
COPY backend/tests/ /app/tests/
