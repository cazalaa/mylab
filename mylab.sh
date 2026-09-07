#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# mylab.sh — macOS / Linux
#
# ./mylab.sh
# ./mylab.sh --install
# ./mylab.sh --install --jlink-installer /path/to/JLink_Linux_*.tgz
# ./mylab.sh --headless
# ./mylab.sh --remote
# ./mylab.sh --traces
# ./mylab.sh --clean
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PYTHON="$VENV/bin/python"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓ $*${NC}"; }
info() { echo -e "${YELLOW}→ $*${NC}"; }
err()  { echo -e "${RED}✗ $*${NC}"; exit 1; }

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

find_jlink() {
    command -v JLinkExe 2>/dev/null ||
        find /opt/SEGGER /usr/local/SEGGER -type f -name JLinkExe \
            2>/dev/null | head -n1
}

install_jlink() {
    local installer="$1"
    local tmp=""
    local source_dir=""

    [[ -f "$installer" ]] || err "J-Link installer not found: $installer"
    [[ "${OSTYPE:-}" == "linux"* ]] ||
        err "Automatic SEGGER installation is supported only on Linux."

    info "Installing SEGGER J-Link (sudo password may be requested)..."

    case "$installer" in
        *.deb)
            sudo apt-get install -y "$installer"
            ;;
        *.tgz|*.tar.gz)
            tmp="$(mktemp -d)"
            tar -xzf "$installer" -C "$tmp"

            source_dir="$(find "$tmp" -type f -name JLinkExe -printf '%h\n' -quit)"
            [[ -n "$source_dir" ]] ||
                err "No JLinkExe found in $installer"

            sudo rm -rf /opt/SEGGER/JLink
            sudo install -d /opt/SEGGER/JLink
            sudo cp -a "$source_dir"/. /opt/SEGGER/JLink/

            rm -rf "$tmp"
            ;;
        *)
            err "Supported J-Link installers: .deb, .tgz, .tar.gz"
            ;;
    esac

    sudo tee /usr/local/bin/JLinkExe >/dev/null <<'EOF'
#!/bin/sh
cd /opt/SEGGER/JLink || exit 1
exec ./JLinkExe "$@"
EOF
    sudo chmod 755 /usr/local/bin/JLinkExe
}

ensure_jlink() {
    local jlink

    jlink="$(find_jlink || true)"
    [[ -n "$jlink" ]] || err \
        "SEGGER J-Link is missing. Run: ./mylab.sh --install --jlink-installer /path/to/JLink_Linux_*.tgz"

    JLinkExe -version >/dev/null ||
        err "SEGGER J-Link is installed but cannot load its shared library"

    ok "SEGGER J-Link ready"
}

select_python() {
    if have_cmd python3.11; then
        echo "python3.11"
    elif have_cmd python3; then
        echo "python3"
    else
        err "python3 not found. Install Python 3.10 or newer."
    fi
}

cmd_install() {
    echo ""

    local sys_python
    sys_python="$(select_python)"

    if [[ "${OSTYPE:-}" == "linux"* ]] && have_cmd apt-get; then
        info "Installing required Ubuntu runtime libraries..."
        sudo apt-get update
        sudo apt-get install -y \
            ca-certificates \
            libpcre2-16-0 \
            libusb-1.0-0 \
            libudev1
        ok "Ubuntu runtime libraries ready"
    fi

    if [[ -n "${JLINK_INSTALLER:-}" ]]; then
        install_jlink "$JLINK_INSTALLER"
    fi

    ensure_jlink

    if [[ "${OSTYPE:-}" == "linux"* && -f /opt/SEGGER/JLink/99-jlink.rules ]]; then
        info "Installing SEGGER USB rules..."
        sudo install -m 644 /opt/SEGGER/JLink/99-jlink.rules \
            /etc/udev/rules.d/99-jlink.rules
        sudo udevadm control --reload-rules
        sudo udevadm trigger

        if ! id -nG "$USER" | grep -qw dialout; then
            sudo usermod -aG dialout "$USER"
            info "Reconnect to SSH once so the new dialout group takes effect."
        fi
    fi

    if [[ -d "$VENV" ]]; then
        info "Removing the existing virtual environment..."
        rm -rf "$VENV"
    fi

    info "Creating a virtual environment with $sys_python..."
    "$sys_python" -m venv "$VENV" ||
        err "Unable to create the virtual environment"
    ok "Virtual environment created: $VENV"

    info "Updating pip, setuptools and wheel..."
    "$PYTHON" -m pip install --quiet --upgrade pip setuptools wheel

    info "Installing Python dependencies..."
    "$PYTHON" -m pip install --quiet --upgrade \
        -r "$SCRIPT_DIR/requirements.txt"
    ok "Python dependencies installed"

"$PYTHON" - <<'PY'
from pycommander_cli import Commander

commander = Commander()
usb = commander.listAvailableAdapters(list_usb_adapters=True)
network = commander.listAvailableAdapters(list_network_adapters=True)

print(f"PyCommander OK — USB: {len(usb)}, network: {len(network)}")
PY
    ok "PyCommander USB and network discovery verified"

    if [[ "${OSTYPE:-}" == "darwin"* ]]; then
        info "macOS detected — installing PyObjC..."
        "$PYTHON" -m pip uninstall -y \
            pyobjc pyobjc-core pyobjc-framework-Cocoa \
            pyobjc-framework-Quartz pyobjc-framework-WebKit objc \
            >/dev/null 2>&1 || true

        "$PYTHON" -m pip install --quiet --no-cache-dir --force-reinstall \
            pyobjc-core \
            pyobjc-framework-Cocoa \
            pyobjc-framework-Quartz \
            pyobjc-framework-WebKit

"$PYTHON" - <<'PY'
import objc
import Foundation
import AppKit
import WebKit
print("PyObjC OK")
PY
        ok "macOS GUI backend ready"

    elif [[ "${OSTYPE:-}" == "linux"* ]]; then
        info "Headless operation needs no GTK/WebKit packages."
        info "For a local native Linux window only:"
        info "sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
    fi

    echo ""
    ok "Installation complete."
    ok "Start headless mode with: ./mylab.sh --headless"
}

cmd_clean() {
    local log_dir="$SCRIPT_DIR/logs"
    local group_dir="$SCRIPT_DIR/groups"

    find "$SCRIPT_DIR" -type d -name '__pycache__' -prune \
        -exec rm -rf {} + 2>/dev/null || true

    find "$SCRIPT_DIR" -type f \
        \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) \
        -delete 2>/dev/null || true

    ok "Python caches removed"

    if [[ -d "$log_dir" ]] &&
        compgen -G "$log_dir/*.log" >/dev/null 2>&1; then
        rm -f "$log_dir"/*.log
        ok "Logs removed"
    else
        info "No logs to remove"
    fi

    if [[ -d "$group_dir" ]] &&
        compgen -G "$group_dir/*.group" >/dev/null 2>&1; then
        read -rp "Delete saved groups? (y/N) " confirm
        if [[ "$confirm" =~ ^[yYoO]$ ]]; then
            rm -f "$group_dir"/*.group
            ok "Saved groups removed"
        else
            info "Saved groups kept"
        fi
    else
        info "No saved groups to remove"
    fi
}

cmd_run() {
    if [[ ! -f "$PYTHON" ]]; then
        err "Virtual environment not found. Run: ./mylab.sh --install"
    fi

    info "Starting My Lab..."
    cd "$SCRIPT_DIR"
    exec "$PYTHON" my_lab.py "$@"
}

case "${1:-}" in
    --install)
        JLINK_INSTALLER=""

        if [[ "${2:-}" == "--jlink-installer" ]]; then
            JLINK_INSTALLER="${3:-}"
            [[ -n "$JLINK_INSTALLER" ]] ||
                err "Missing path after --jlink-installer"
        elif [[ "${2:-}" == --jlink-installer=* ]]; then
            JLINK_INSTALLER="${2#--jlink-installer=}"
        fi

        cmd_install
        ;;
    --clean)
        cmd_clean
        ;;
    "")
        cmd_run
        ;;
    --traces)
        cmd_run --traces
        ;;
    --headless)
        cmd_run --headless
        ;;
    --remote)
        cmd_run --remote
        ;;
    --headless-traces)
        cmd_run --headless --traces
        ;;
    --remote-traces)
        cmd_run --remote --traces
        ;;
    *)
        echo "Usage: ./mylab.sh [--install | --clean | --headless | --remote | --traces]"
        exit 1
        ;;
esac
