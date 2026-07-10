#!/bin/sh
# Download the ONNX models used by the model-based VAD engines.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS_DIR="$REPO_ROOT/models"
mkdir -p "$MODELS_DIR"

SILERO_URL="https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"
SILERO_SHA256="2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"
SILERO_PATH="$MODELS_DIR/silero_vad.onnx"

check_silero() {
    echo "$SILERO_SHA256  $SILERO_PATH" | shasum -a 256 -c - >/dev/null 2>&1
}

if [ -f "$SILERO_PATH" ] && check_silero; then
    echo "silero_vad.onnx already present and verified"
else
    echo "downloading silero_vad.onnx ..."
    curl -sSfL -o "$SILERO_PATH" "$SILERO_URL"
    if ! check_silero; then
        echo "ERROR: silero_vad.onnx sha256 mismatch" >&2
        exit 1
    fi
    echo "silero_vad.onnx downloaded and verified"
fi

# ten-vad ships its model inside the pip package; nothing to download.
