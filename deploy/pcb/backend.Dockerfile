# Backend image of this fork: the upstream release image with this branch's Python on top.
#
# The fork changes only Python under backend/app (pipeline), so rebuilding KiCad, the Rust
# prism-kicad CLI and the simulation engines from source would only add risk and an hour.
# UPSTREAM must be the digest of the upstream release last merged into this branch (v5.2.0-alpha), so
# every file copied here matches the image it lands in.
#   docker build -f deploy/pcb/backend.Dockerfile -t <tag> .
ARG UPSTREAM=ghcr.io/krishna-swaroop/kicad-prism-backend@sha256:c974b18c30db3259316bd693adfe6f280c545c2686513853db8c4231a0c676ec
FROM ${UPSTREAM}
COPY backend/app/ /app/app/
COPY backend/tests/ /app/tests/
