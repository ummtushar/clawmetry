#!/bin/bash
# Clawmetry — One-line installer (macOS + Linux)
# Usage: curl -fsSL https://raw.githubusercontent.com/vivekchand/clawmetry/main/install.sh | bash
set -e

echo "🔭 Installing Clawmetry — OpenClaw Observability Dashboard"
echo ""

OS="$(uname -s)"
INSTALL_DIR=""
USE_SUDO=""
BIN_DIR=""

case "$OS" in
  Darwin)
    echo "→ Detected macOS"
    INSTALL_DIR="$HOME/.clawmetry"
    BIN_DIR="$HOME/.local/bin"
    USE_SUDO=""

    # Ensure python3 is available
    if ! command -v python3 &>/dev/null; then
      if command -v brew &>/dev/null; then
        echo "→ Installing Python via Homebrew..."
        brew install python3
      else
        echo "❌ Python3 not found. Install it with: brew install python3"
        echo "   (Get Homebrew: https://brew.sh)"
        exit 1
      fi
    fi
    ;;
  Linux)
    echo "→ Detected Linux"
    INSTALL_DIR="/opt/clawmetry"
    BIN_DIR="/usr/local/bin"
    USE_SUDO="sudo"

    # Install python3-venv if needed
    if command -v apt-get &>/dev/null; then
      echo "→ Installing Python venv (apt)..."
      sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip >/dev/null 2>&1
    elif command -v yum &>/dev/null; then
      echo "→ Installing Python venv (yum)..."
      sudo yum install -y python3 python3-pip >/dev/null 2>&1
    elif command -v dnf &>/dev/null; then
      echo "→ Installing Python venv (dnf)..."
      sudo dnf install -y python3 python3-pip >/dev/null 2>&1
    elif command -v apk &>/dev/null; then
      echo "→ Installing Python venv (apk)..."
      sudo apk add python3 py3-pip >/dev/null 2>&1
    elif command -v pacman &>/dev/null; then
      echo "→ Installing Python venv (pacman)..."
      sudo pacman -Sy --noconfirm python python-pip >/dev/null 2>&1
    fi
    ;;
  *)
    echo "❌ Unsupported OS: $OS"
    echo "   Clawmetry supports macOS and Linux."
    echo "   On Windows, use WSL2: https://docs.microsoft.com/en-us/windows/wsl/"
    exit 1
    ;;
esac

# Create isolated venv (remove old one to ensure clean state)
echo "→ Creating virtual environment at $INSTALL_DIR..."
$USE_SUDO rm -rf "$INSTALL_DIR"
$USE_SUDO python3 -m venv "$INSTALL_DIR"
$USE_SUDO "$INSTALL_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1

# Install clawmetry
echo "→ Installing clawmetry from PyPI..."
$USE_SUDO "$INSTALL_DIR/bin/pip" install --no-cache-dir --upgrade clawmetry >/dev/null 2>&1

# Create symlink for easy access
mkdir -p "$BIN_DIR" 2>/dev/null || $USE_SUDO mkdir -p "$BIN_DIR"
$USE_SUDO ln -sf "$INSTALL_DIR/bin/clawmetry" "$BIN_DIR/clawmetry"

# Ensure BIN_DIR is in PATH (macOS ~/.local/bin may not be)
if [ "$OS" = "Darwin" ] && [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "⚠️  Add $BIN_DIR to your PATH:"
  SHELL_NAME="$(basename "$SHELL")"
  case "$SHELL_NAME" in
    zsh)  echo "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc" ;;
    bash) echo "    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
    *)    echo "    export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

# Detect OpenClaw workspace
WORKSPACE=""
if [ -d "$HOME/.openclaw" ]; then
  WORKSPACE="$HOME/.openclaw"
elif [ -d "/root/.openclaw" ]; then
  WORKSPACE="/root/.openclaw"
else
  # Check Docker-style paths: /docker/openclaw-*/data/.openclaw
  DOCKER_WS=$(find /docker -maxdepth 4 -name ".openclaw" -type d 2>/dev/null | head -1)
  if [ -n "$DOCKER_WS" ]; then
    WORKSPACE="$DOCKER_WS"
  fi
fi

CLAWMETRY_BIN="$BIN_DIR/clawmetry"
if ! command -v clawmetry &>/dev/null; then
  CLAWMETRY_BIN="$INSTALL_DIR/bin/clawmetry"
fi

VERSION=$("$CLAWMETRY_BIN" --version 2>/dev/null || echo 'installed')

echo ""
echo "✅ ClawMetry $VERSION installed successfully!"
echo ""

if [ -n "$WORKSPACE" ]; then
  echo "  OpenClaw workspace detected: $WORKSPACE"
else
  echo "  ⚠️  No OpenClaw workspace found. Make sure OpenClaw is installed and running."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Onboarding — ask user if they want cloud or local ─────────────────────────

"$CLAWMETRY_BIN" onboard < /dev/tty
