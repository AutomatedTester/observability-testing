#!/usr/bin/env bash
# Download (once) and run the Jaeger all-in-one native binary -- no Docker.
#
# Jaeger v2 all-in-one enables the OTLP receiver (gRPC :4317, HTTP :4318) and
# serves the UI at http://localhost:16686 with in-memory storage by default.
#
# Override the pinned version with JAEGER_VERSION=x.y.z if needed.
set -euo pipefail

cd "$(dirname "$0")/.."
DEST=".jaeger"
mkdir -p "$DEST"

# --- detect platform ---
case "$(uname -s)" in
  Darwin) OS="darwin" ;;
  Linux)  OS="linux" ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="amd64" ;;
  *) echo "Unsupported arch: $(uname -m)" >&2; exit 1 ;;
esac

# --- resolve version (latest v2 unless pinned) ---
VERSION="${JAEGER_VERSION:-}"
if [[ -z "$VERSION" ]]; then
  echo "Resolving latest Jaeger release..." >&2
  # Write to a file first: piping curl into grep -m1 triggers SIGPIPE, which
  # combined with `set -o pipefail` would abort the script.
  curl -fsSL https://api.github.com/repos/jaegertracing/jaeger/releases/latest \
    -o "$DEST/latest.json"
  VERSION="$(grep '"tag_name"' "$DEST/latest.json" | head -n1 | sed -E 's/.*"v?([0-9.]+)".*/\1/')"
  rm -f "$DEST/latest.json"
fi
if [[ -z "$VERSION" ]]; then
  echo "Could not resolve Jaeger version. Set JAEGER_VERSION=x.y.z and retry." >&2
  exit 1
fi

BIN="$DEST/jaeger"
if [[ ! -x "$BIN" ]]; then
  TARBALL="jaeger-${VERSION}-${OS}-${ARCH}.tar.gz"
  URL="https://github.com/jaegertracing/jaeger/releases/download/v${VERSION}/${TARBALL}"
  echo "Downloading $URL" >&2
  curl -fsSL "$URL" -o "$DEST/$TARBALL"
  tar -xzf "$DEST/$TARBALL" -C "$DEST" --strip-components=1
  rm -f "$DEST/$TARBALL"
  # v2 ships the 'jaeger' binary; v1 ships 'jaeger-all-in-one'. Normalize to $BIN.
  if [[ ! -x "$BIN" && -x "$DEST/jaeger-all-in-one" ]]; then
    ln -sf jaeger-all-in-one "$BIN"
  fi
fi

echo "Starting Jaeger $VERSION -> UI at http://localhost:16686 (Ctrl-C to stop)" >&2
if [[ -x "$DEST/jaeger-all-in-one" && ! -e "$DEST/jaeger" ]]; then
  exec "$DEST/jaeger-all-in-one" --collector.otlp.enabled=true
fi
exec "$BIN"
