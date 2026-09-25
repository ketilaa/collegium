FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# The services read untrusted web content; they run without root, and the
# code and environment in /app stay owned by root, so they cannot change them.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin collegium
USER collegium
CMD ["collegium", "worker"]
