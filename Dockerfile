# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM lscr.io/linuxserver/wireguard:1.0.20260223-r0-ls124@sha256:216ca1ce9da7bfe26fb752111d952487804bd18933bbd4794970d424bf8adfe9

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Use Alpine's interpreter and wheels; no cross-distribution venv copying.
RUN apk add --no-cache python3 curl ca-certificates \
    && python3 -c 'import sys; assert sys.version_info >= (3, 14), sys.version'

# The pinned base's init-wireguard-confs generates/migrates wg-quick configs;
# svc-wireguard activates them and deletes the default route when none exist.
# Remove these owners and CoreDNS/module-loading from the s6-rc graph, including
# init-config-end's dependency. API bootstrap is the sole interface owner.
# Kernel WireGuard support is supplied by the host; /init and legacy services stay.
RUN rm -rf /etc/s6-overlay/s6-rc.d/init-wireguard-confs \
        /etc/s6-overlay/s6-rc.d/init-wireguard-module \
        /etc/s6-overlay/s6-rc.d/svc-wireguard \
        /etc/s6-overlay/s6-rc.d/svc-coredns \
    && rm /etc/s6-overlay/s6-rc.d/user/contents.d/init-wireguard-confs \
        /etc/s6-overlay/s6-rc.d/user/contents.d/init-wireguard-module \
        /etc/s6-overlay/s6-rc.d/user/contents.d/svc-wireguard \
        /etc/s6-overlay/s6-rc.d/user/contents.d/svc-coredns \
        /etc/s6-overlay/s6-rc.d/init-config-end/dependencies.d/init-wireguard-confs

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --python=/usr/bin/python3

COPY api.py routes.py health.py metrics.py wireguard.py storage.py settings.py \
    models.py service.py errors.py keys.py configuration.py version.py bootstrap.py ./
COPY --chmod=755 service_run /etc/services.d/api/run

EXPOSE 51820/udp 8008/tcp

# S6 and the /init entrypoint are inherited from the WireGuard base image.
