# Build stage
FROM python:3.13-slim-bookworm as builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Configure uv
# Compile bytecode for faster startup
ENV UV_COMPILE_BYTECODE=1 
# Disable link mode (copy files instead of hardlinking) since we copy from builder
ENV UV_LINK_MODE=copy 

WORKDIR /app

# Install dependencies
# We use a cache mount to speed up subsequent builds
# We copy only the lock files first to leverage layer caching
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy the application code and sync the project (if needed for package mode, or just checks)
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Final stage
FROM linuxserver/wireguard

ENV API_TOKEN ${API_TOKEN}
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install runtime dependencies (Python)
# We need to ensure python 3.13 is available or installed. 
# Since we are using linuxserver/wireguard, we might need to add python manually if not present,
# BUT we should reuse the python environment from the builder if possible OR install python in the final stage.
# However, copying a venv created with a specific python version to a different image 
# requires the *same* python interpreter path/version.
# A safer bet with `uv` and multi-stage across potentially different base images (uv image is chemically different from linuxserver/wireguard)
# is to install python in the final image and `uv sync` again OR 
# install dependencies *into* the final image using `uv`.

# Let's adjust approach: 
# 1. Install `uv` in the final image (it's a static binary, easy to copy).
# 2. Use `uv` to install the python environment directly in the final image.
# This ensures compatibility with the OS of the final image.

# Install dependencies for python (if needed by the base OS)
# linuxserver/wireguard is base on Alpine
RUN apk add --no-cache \
    python3 \
    curl \
    iptables \
    iproute2 \
    ca-certificates

# Copy uv from the builder image
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./

# Install dependencies directly in the final image 
# (using cache mount to speed it up if built locally with buildkit)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --python=/usr/bin/python3

# Copy application code
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --python=/usr/bin/python3

# S6-Overlay service configuration
COPY service_run /etc/services.d/api/run
RUN chmod +x /etc/services.d/api/run

EXPOSE 51820
EXPOSE 8008

# Remove default CMD as we use S6 services
# (Base image ENTRYPOINT is /init)
