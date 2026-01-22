FROM linuxserver/wireguard as wireguard_api

ENV API_TOKEN ${API_TOKEN}
# Ensure virtual env is used
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Install Python and dependencies
# We use the deadsnakes PPA to get newer python on older ubuntu/debian bases if needed,
# or just rely on apt if the base is new enough. 
# linuxserver/wireguard is usually Alpine or Ubuntu. Let's assume Ubuntu/Debian based on `apt-get`.
# We'll install `uv` to manage the python version and dependencies.

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project definition
COPY pyproject.toml /app/
WORKDIR /app

# Create virtual environment and install dependencies
# We explicitly install python 3.12 using uv
RUN uv venv /opt/venv --python 3.13 && \
    uv sync --frozen --no-install-project || uv sync --no-install-project

# Copy application code
COPY . /app/
# Sync the project itself (if needed, though we just have a script)
# Re-run sync to ensure everything is settled if we added the project as a package, 
# but here we set `package = true` in pyproject.toml so we should install it.
RUN uv sync --frozen || uv sync

EXPOSE 51820
EXPOSE 8008

# Run with the venv python
CMD ["uv", "run", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8008"]
