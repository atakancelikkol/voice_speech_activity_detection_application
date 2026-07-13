# syntax=docker/dockerfile:1

# ---------- build stage: compile the ctypes C libraries as Linux .so ----------
# The OS-aware Makefiles emit .so here (they emit .dylib on a dev Mac). Kept in a
# separate stage so gcc/make never ship in the runtime image.
FROM python:3.12-slim-bookworm AS cbuild
RUN apt-get update && apt-get install -y --no-install-recommends build-essential make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Makefile ./
COPY third_party/ third_party/
RUN make build-c

# ---------- runtime stage ----------
FROM python:3.12-slim-bookworm
# uv: fast, lockfile-reproducible dependency install
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# libgomp1 is onnxruntime's runtime dependency; curl fetches the ONNX model
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Silero ONNX model (the C-lib engines are built in the cbuild stage; ten-vad
# ships its model in the wheel). Baked into the image so the engine is available
# out of the box — done early so app-code edits don't re-download it.
RUN mkdir -p models && \
    curl -sSfL -o models/silero_vad.onnx \
      "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx" && \
    echo "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f  models/silero_vad.onnx" \
      | sha256sum -c -

# 1) dependencies first so editing app code doesn't re-resolve everything
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) app source + the Linux .so built above, then install the project itself
COPY . .
COPY --from=cbuild /app/third_party/ third_party/
RUN uv sync --frozen --no-dev
# ten_vad ships prebuilt wheels that may not cover every CPU arch; make it
# best-effort so the image still builds — its engine just reports "unavailable".
RUN uv pip install ten-vad || echo "WARN: ten-vad not installed on this platform"

EXPOSE 8080
# --no-client: no softphone client in the container — recording is the browser's
# own microphone over /api/record. --host 0.0.0.0 so the VM can serve it.
CMD ["uv", "run", "vad-server", "--no-client", "--host", "0.0.0.0", "--http-port", "8080"]
