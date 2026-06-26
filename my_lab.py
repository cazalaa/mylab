import subprocess
import time
import configparser
import os
import sys
import shutil
import json
import glob
import threading
import datetime
import re
import uuid
import yaml
import serial
import serial.tools.list_ports
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit, join_room
import socket as sock_module
from concurrent.futures import ThreadPoolExecutor, as_completed
import webview

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR    = os.path.join(_BASE_DIR, "logs")
GROUPS_DIR = os.path.join(_BASE_DIR, "groups")
SCENARI_DIR = os.path.join(_BASE_DIR, "scenari")

os.makedirs(GROUPS_DIR, exist_ok=True)

# Dimensions
WIN_W          = 800
WIN_COLLAPSED  = 65   # hauteur barre compacte
WIN_EXPANDED   = 700   # hauteur dépliée

# ----------------------------
# BINRESOLVE (intégré)
# ----------------------------
# SDM has been removed. Discovery, reset and flashing now go through
# pycommander (https://github.com/SiliconLabsSoftware/pycommander), which bundles
# its own Simplicity Commander binary — no separate Commander/SDM install needed.
from pycommander import Commander, Adapter
from pycommander_core._ensure_commander import ensure_commander

_SILABS_ROOT    = Path.home() / ".silabs"
_IS_WINDOWS     = sys.platform == "win32"
_IS_MAC         = sys.platform == "darwin"
_COMMANDER_NAME = "commander.exe" if _IS_WINDOWS else "commander"

# Chemins fallback par plateforme (utilisés uniquement si auto-détection échoue)
if _IS_WINDOWS:
    _DEFAULT_COMMANDER = str(Path.home() / "AppData/Local/silabs/Commander/commander.exe")
elif _IS_MAC:
    _DEFAULT_COMMANDER = str(Path.home() / ".silabs/slt/installs/archive/Commander.app/Contents/MacOS/commander")
else:  # Linux
    _DEFAULT_COMMANDER = str(Path.home() / ".silabs/slt/installs/archive/commander-linux-x64/commander")

def _is_exec(p: str) -> bool:
    pp = Path(p)
    if not pp.exists():
        return False
    if _IS_WINDOWS:
        return True
    return os.access(str(pp), os.X_OK)

def _find_in_silabs(name: str) -> str | None:
    if not _SILABS_ROOT.exists():
        return None
    candidates = sorted(_SILABS_ROOT.rglob(name), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if _is_exec(str(candidate)):
            print(f"[INFO] {name} trouvé via ~/.silabs : {candidate}")
            return str(candidate)
    return None

def resolve_commander(path: str | None) -> str | None:
    # Explicit override (config.ini or COMMANDER_BIN) wins, then pycommander's
    # bundled binary, then any system install.
    if path and _is_exec(path):
        return path
    envp = os.environ.get("COMMANDER_BIN")
    if envp and _is_exec(envp):
        return envp
    try:
        bundled = str(ensure_commander(cli=True))
        if _is_exec(bundled):
            return bundled
    except Exception as e:
        print(f"[WARN] pycommander bundled binary unavailable: {e}")
    if _is_exec(_DEFAULT_COMMANDER):
        return _DEFAULT_COMMANDER
    p = shutil.which("commander") or shutil.which("commander-cli")
    if p and _is_exec(p):
        return p
    return _find_in_silabs(_COMMANDER_NAME)

CONFIG_FILE = "config.ini"
ALLOWED_PAGES = {"maintenance", "manual_control", "script_control"}

# ----------------------------
# CONFIG + AUTO RESOLVE
# ----------------------------
config = configparser.ConfigParser()
config.read(CONFIG_FILE)
for _sec in ("paths", "server"):
    if not config.has_section(_sec):
        config.add_section(_sec)

COMMANDER_PATH = resolve_commander(config["paths"].get("commander"))

# On ne sauvegarde que si un chemin était absent du config et vient d'être auto-détecté
_save_needed = False
if COMMANDER_PATH and not config["paths"].get("commander"):
    config["paths"]["commander"] = COMMANDER_PATH
    _save_needed = True

if _save_needed:
    with open(CONFIG_FILE, "w") as f:
        config.write(f)

WEB_PORT = int(config["server"].get("web_port", "8080"))

# ----------------------------
# VCOM transport configuration
# ----------------------------
# Admin console and the protocol parser have been removed. A board now exposes a
# single VCOM channel. USB and IP are identical above the transport:
#   - IP  : raw TCP telnet to <host>:IP_VCOM_PORT
#   - USB : pyserial on the board's CDC tty, at VCOM_BAUDRATE
IP_VCOM_PORT  = int(config["server"].get("ip_vcom_port", "4901"))   # fixed Silabs network VCOM port
VCOM_BAUDRATE = int(config["server"].get("vcom_baudrate", "115200"))  # default, overridable per board

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ----------------------------
# ADAPTER DISCOVERY (pycommander — SDM removed)
# ----------------------------
_commander_singleton = None
_commander_guard     = threading.Lock()

def _commander():
    global _commander_singleton
    with _commander_guard:
        if _commander_singleton is None:
            _commander_singleton = Commander()
        return _commander_singleton

# Discovered adapter records, cached. Probing for board labels is a Commander
# subprocess per board, so we cache and only refresh on an explicit scan.
_adapters_cache       = []
_adapters_cache_guard = threading.Lock()
_adapter_info_cache   = {}   # key (serial or ip) -> [{id,label,pn}]

def _is_ip(s) -> bool:
    parts = str(s).split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False

def _probe_boards(serial=None, ip=None):
    """Best-effort Adapter.info() -> board labels. Cached (slow Commander probe)."""
    key = serial or ip
    if not key:
        return []
    if key in _adapter_info_cache:
        return _adapter_info_cache[key]
    boards = []
    try:
        ad   = Adapter(serial_number=str(serial)) if serial else Adapter(ip_address=str(ip))
        info = ad.info()
        if info and info.board_list:
            for b in info.board_list:
                pn = b.part_number or ""
                boards.append({"id": pn, "label": b.name or pn, "pn": pn})
    except Exception as e:
        print(f"[WARN] probe {key}: {e}")
    _adapter_info_cache[key] = boards
    return boards

def discover_adapters(probe=True):
    """Enumerate USB + network adapters via pycommander.

    Returns records shaped like the old SDM ones so downstream code is unchanged:
      {serialNumber, host, connectivityType, nickname, label, boards:[{id,label,pn}]}
    """
    cmd = _commander()
    found = []
    # listAvailableAdapters accepts only ONE of usb/network per call → two calls.
    try:
        found += [("usb", a) for a in (cmd.listAvailableAdapters(list_usb_adapters=True) or [])]
    except Exception as e:
        print(f"[WARN] list usb adapters: {e}")
    try:
        found += [("eth", a) for a in (cmd.listAvailableAdapters(list_network_adapters=True) or [])]
    except Exception as e:
        print(f"[WARN] list network adapters: {e}")

    out = []
    for kind, a in found:
        serial = str(a.jlink_serial_number) if a.jlink_serial_number is not None else None
        ip     = a.ip_address or None
        out.append({
            "serialNumber":     serial or ip or "",
            "host":             ip or "",
            "connectivityType": "usb" if kind == "usb" else "ethernet",
            "nickname":         a.nickname or "",
            "label":            a.nickname or "",
            "boards":           _probe_boards(serial=serial, ip=ip) if probe else [],
        })
    return out

def refresh_adapters(probe=True):
    """Rebuild the adapter cache (called by the /scan route)."""
    global _adapters_cache
    _adapter_info_cache.clear()
    with _adapters_cache_guard:
        _adapters_cache = discover_adapters(probe=probe)
    return _adapters_cache

def get_adapters():
    """Return cached adapters; discover once if the cache is empty."""
    with _adapters_cache_guard:
        if _adapters_cache:
            return list(_adapters_cache)
    return refresh_adapters()

def adapter_record(serial_or_host):
    """Look up a cached adapter record by serial number or IP host."""
    key = str(serial_or_host)
    for a in get_adapters():
        if a.get("serialNumber") == key or (a.get("host") and a.get("host") == key):
            return a
    return None

def extract_board_label(a):
    boards = a.get("boards") or []
    return boards[0].get("label", "") if boards else ""

def extract_board(a):
    boards = a.get("boards") or []
    return boards[0].get("id", "Unknown") if boards else "Unknown"

# ----------------------------
# COMMANDER ACTIONS
# ----------------------------

def pyc_reset(serial=None, ip=None, device=None):
    """Reset the target device via pycommander (replaces admin 'target reset').

    Works identically for USB (serial) and IP (ip) connected boards.
    """
    args = ["device", "reset"]
    if device:
        args += ["--device", str(device)]
    kw = {}
    if serial:
        kw["serial_number"] = str(serial)
    elif ip:
        kw["ip_address"] = str(ip)
    try:
        res = _commander().runCommand(*args, json_formatted_output=False, **kw)
        ok = res.returncode == 0
        if not ok:
            print(f"[RST] pyc_reset rc={res.returncode}: {res.output[:200]}")
        return ok
    except Exception as e:
        print(f"[RST] pyc_reset failed: {e}")
        return False

def run_commander(cmd, serial):
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return {"serialNumber": serial, "ok": True, "output": result.stdout}
    except subprocess.CalledProcessError as e:
        return {"serialNumber": serial, "ok": False, "error": e.stderr or str(e)}
    except Exception as e:
        return {"serialNumber": serial, "ok": False, "error": str(e)}

@app.route("/mass_erase", methods=["POST"])
def mass_erase():
    adapters = request.json.get("adapters", [])

    def erase(a):
        cmd = [COMMANDER_PATH, "device", "masserase",
               "--serialno" if a["connectivityType"] == "usb" else "--ip",
               a["serialNumber"] if a["connectivityType"] == "usb" else a["host"]]
        return run_commander(cmd, a["serialNumber"])

    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(erase, a): a for a in adapters}
        results = [f.result() for f in as_completed(futures)]

    return jsonify(results)


@app.route("/chip_recover", methods=["POST"])
def chip_recover():
    adapters = request.json.get("adapters", [])

    def recover(a):
        cmd = [COMMANDER_PATH, "device", "recover",
               "--serialno" if a["connectivityType"] == "usb" else "--ip",
               a["serialNumber"] if a["connectivityType"] == "usb" else a["host"]]
        return run_commander(cmd, a["serialNumber"])

    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(recover, a): a for a in adapters}
        results = [f.result() for f in as_completed(futures)]

    return jsonify(results)



@app.route("/fw_upgrade", methods=["POST"])
def fw_upgrade():
    adapters = request.json.get("adapters", [])

    def upgrade(a):
        cmd = [COMMANDER_PATH, "adapter", "fwupgrade",
               "-s" if a["connectivityType"] == "usb" else "--ip",
               a["serialNumber"] if a["connectivityType"] == "usb" else a["host"]]
        return run_commander(cmd, a["serialNumber"])

    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(upgrade, a): a for a in adapters}
        results = [f.result() for f in as_completed(futures)]

    return jsonify(results)

# ----------------------------
# UI
# ----------------------------

# -----------------------------------------------------------------------------
# VCOM transport + endpoint resolution (SDM / Silink removed)
# -----------------------------------------------------------------------------
# A board exposes ONE VCOM channel. USB and IP are identical above the transport:
#   - IP  : raw TCP to <host>:IP_VCOM_PORT
#   - USB : pyserial on the board's CDC tty at the configured baudrate
# VcomLink hides that one difference so all reader/writer code is shared.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_host(host) -> bool:
    return str(host or "").strip().lower() in _LOOPBACK_HOSTS


class EndpointError(RuntimeError):
    """Raised when a board's VCOM endpoint cannot be resolved."""
    pass


class VcomLink:
    """Unified VCOM transport — a TCP socket (IP) or a pyserial port (USB).

    recv() blocks until data is available or the link is closed (returns b""),
    so the reader loop is identical for both transports.
    """

    def __init__(self, kind, sock=None, ser=None, info=""):
        self.kind = kind            # "tcp" | "serial"
        self._sock = sock
        self._ser  = ser
        self.info  = info

    @classmethod
    def open_tcp(cls, host, port, timeout=5):
        s = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.settimeout(None)
        return cls("tcp", sock=s, info=f"{host}:{port}")

    @classmethod
    def open_serial(cls, tty, baudrate):
        ser = serial.Serial(tty, baudrate=int(baudrate), timeout=0)
        return cls("serial", ser=ser, info=f"{tty}@{baudrate}")

    def recv(self, n=4096) -> bytes:
        if self.kind == "tcp":
            return self._sock.recv(n)          # b"" on close
        while True:                            # serial: emulate a blocking recv
            try:
                data = self._ser.read(n)
            except Exception:
                return b""                     # port removed/closed -> EOF
            if data:
                return data
            if not getattr(self._ser, "is_open", False):
                return b""
            time.sleep(0.005)

    def sendall(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if self.kind == "tcp":
            self._sock.sendall(data)
        else:
            self._ser.write(data)

    def close(self):
        try:
            (self._sock if self.kind == "tcp" else self._ser).close()
        except Exception:
            pass


def _find_usb_tty(serial_number):
    """Locate the /dev/tty*|/dev/cu* CDC device for a USB serial number."""
    try:
        import serial.tools.list_ports as list_ports
        for port in list_ports.comports():
            if str(serial_number).lower() in (port.serial_number or "").lower():
                return port.device
    except Exception as e:
        print(f"[WARN] list_ports {serial_number}: {e}")
    return None


def resolve_endpoint(identifier, *, baudrate=None, **_legacy):
    """Resolve where/how to open VCOM for a board. Identical shape for USB & IP:

        {
          "serial":       str,             # USB serial or IP identifier
          "connectivity": "usb"|"ethernet",
          "transport":    "serial"|"tcp",
          "host":         str,             # IP host (tcp) or "" (usb)
          "vcom_port":    int|None,        # IP_VCOM_PORT for tcp
          "tty":          str|None,        # CDC device for usb
          "baudrate":     int,
          "adapter":      dict,            # cached discovery record (may be {})
        }

    `identifier` may be a USB serial number or an IP address.
    Raises EndpointError if no usable VCOM endpoint is found.
    `**_legacy` swallows old kwargs (force=, start=) so existing call sites
    keep working; they have no effect now.
    """
    ident = str(identifier).strip()
    baud  = int(baudrate or VCOM_BAUDRATE)
    rec   = adapter_record(ident) or {}
    connectivity = rec.get("connectivityType")

    host = ""
    if _is_ip(ident):
        host, connectivity = ident, "ethernet"
    elif rec.get("host") and not _is_loopback_host(rec.get("host")):
        host, connectivity = rec["host"], "ethernet"
    else:
        connectivity = connectivity or "usb"

    if connectivity == "ethernet" and host:
        return {
            "serial":       rec.get("serialNumber", ident),
            "connectivity": "ethernet",
            "transport":    "tcp",
            "host":         host,
            "vcom_port":    IP_VCOM_PORT,
            "tty":          None,
            "baudrate":     baud,
            "adapter":      rec,
        }

    # USB → pyserial on the CDC tty
    tty = _find_usb_tty(ident)
    if not tty and rec.get("serialNumber") and rec["serialNumber"] != ident:
        tty = _find_usb_tty(rec["serialNumber"])
    if not tty:
        raise EndpointError(
            f"Aucun port VCOM pour {ident} : carte USB non détectée (tty introuvable). "
            f"Rebrancher la carte ou relancer un scan."
        )
    return {
        "serial":       ident,
        "connectivity": "usb",
        "transport":    "serial",
        "host":         "",
        "vcom_port":    None,
        "tty":          tty,
        "baudrate":     baud,
        "adapter":      rec,
    }


def open_vcom_link(ep) -> VcomLink:
    """Open a VcomLink from a resolved endpoint dict."""
    if ep["transport"] == "tcp":
        return VcomLink.open_tcp(ep["host"], ep["vcom_port"])
    return VcomLink.open_serial(ep["tty"], ep.get("baudrate", VCOM_BAUDRATE))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/page/<name>")
def page(name):
    if name not in ALLOWED_PAGES:
        return "Not found", 404
    return render_template(f"{name}.html")

@app.route("/groups", methods=["GET"])
def list_groups():
    files = glob.glob(os.path.join(GROUPS_DIR, "*.group"))
    return jsonify([os.path.splitext(os.path.basename(f))[0] for f in sorted(files)])

@app.route("/groups/<name>", methods=["GET"])
def load_group(name):
    path = os.path.join(GROUPS_DIR, f"{name}.group")
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))

@app.route("/groups/<name>", methods=["PUT"])
def save_group(name):
    path = os.path.join(GROUPS_DIR, f"{name}.group")
    with open(path, "w") as f:
        json.dump(request.json, f, indent=2)
    return jsonify({"ok": True})

@app.route("/groups/<name>", methods=["DELETE"])
def delete_group(name):
    path = os.path.join(GROUPS_DIR, f"{name}.group")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"ok": True})
@app.route("/scan", methods=["POST"])
def scan():
    refresh_adapters()
    return "OK"

@app.route("/adapters")
def adapters():
    result = []
    for a in get_adapters():
        serial = a.get("serialNumber")
        if not serial:
            continue
        result.append({
            "serialNumber":     serial,
            "boardId":          extract_board(a),
            "boardLabel":       extract_board_label(a),
            "connectivityType": a.get("connectivityType"),
            "host":             a.get("host"),
            "label":            a.get("label", ""),
            "nickname":         a.get("nickname", ""),
            # No more silink TTY fallback distinction: USB always uses pyserial,
            # IP always uses TCP. Kept for frontend compatibility.
            "ttyMode":          a.get("connectivityType") == "usb",
        })
    return jsonify(result)

# ── terminal window API ────────────────────────────
class WindowAPI:
    def set_height(self, height):
        webview.windows[0].resize(WIN_W, height)

    def pick_file(self, directory="", file_types=None):
        if file_types is None:
            file_types = ("All files (*.*)",)
        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN,
            directory=str(directory),
            allow_multiple=False,
            file_types=tuple(file_types)
        )
        return result[0] if result else None

    def pick_directory(self, directory=""):
        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=str(directory),
        )
        return result[0] if result else None

    def save_file(self, directory="", file_types=None):
        if file_types is None:
            file_types = ("YAML files (*.yaml)", "All files (*.*)")
        result = webview.windows[0].create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(directory),
            file_types=tuple(file_types)
        )
        return result[0] if result else None

    def open_terminal(self, serial):
        """Appelé depuis le JS via pywebview.api.open_terminal(serial)"""
        def _open():
            url = f"http://127.0.0.1:{WEB_PORT}/terminal/{serial}"
            webview.create_window(
                f"{serial} — Terminal",
                url,
                width=820, height=620,
                resizable=True,
                js_api=TerminalWindowAPI()
            )
        threading.Thread(target=_open, daemon=True).start()


class TerminalWindowAPI:
    """JS API for terminal windows — provides pick_file."""
    def pick_file(self, directory="", file_types=None):
        if file_types is None:
            file_types = ("Python scripts (*.py)", "All files (*.*)")
        wins = webview.windows
        win  = wins[-1] if len(wins) > 1 else wins[0]
        result = win.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=str(directory),
            allow_multiple=False,
            file_types=tuple(file_types)
        )
        return result[0] if result else None

# ── terminal page ──────────────────────────────────
@app.route("/terminal/<serial>")
def terminal_page(serial):
    terminal_mode = config["server"].get("terminal_mode", "modern").strip().lower()
    return render_template("terminal.html", serial=serial, terminal_mode=terminal_mode)

# ── adapter info endpoint (pour le terminal) ───────
@app.route("/api/terminal-run-script", methods=["POST"])
def terminal_run_script():
    """Run a Python script on a board from the terminal window."""
    serial      = request.json.get("serial", "")
    script_path = request.json.get("script_path", "")
    room        = f"{serial}_vcom"

    if not os.path.isfile(script_path):
        return jsonify({"ok": False, "error": "File not found"}), 400

    def emit_out(msg, color="default"):
        colors = {"green": "\x1b[32m", "red": "\x1b[31m", "yellow": "\x1b[33m", "default": "\x1b[0m"}
        prefix = colors.get(color, "\x1b[0m")
        socketio.emit("terminal_output", {"data": f"{prefix}{msg}\x1b[0m\r\n", "room": room}, room=room)

    def run():
        emit_out(f"Running {os.path.basename(script_path)}", "yellow")
        try:
            with open(script_path) as f:
                code = f.read()

            # Reuse the existing VCOM link from the terminal window if present.
            vcom_link = active_telnets.get(room)
            board_obj = Board(serial=serial, run_id=None, scenario_dir="")
            if vcom_link is not None:
                board_obj._vcom_link = vcom_link
            else:
                # Not yet open — connect fresh (same path for USB & IP)
                try:
                    board_obj.connect()
                except EndpointError as e:
                    emit_out(f"✗ {e}", "red")
                    return

            # Register so the shared reader dispatches VCOM data to this board
            active_boards[room] = board_obj

            namespace = {"board": board_obj, "time": time, "__builtins__": __builtins__}
            exec(code, namespace)
            if "script" in namespace and callable(namespace["script"]):
                namespace["script"](board_obj)
            emit_out("Script completed", "green")
        except Exception as e:
            import traceback
            emit_out(f"✗ {e}", "red")
            emit_out(traceback.format_exc(), "red")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})



@app.route("/api/terminal-info/<serial_number>")
def terminal_info(serial_number):
    try:
        ep = resolve_endpoint(serial_number)
    except EndpointError as e:
        print(f"[INFO] terminal-info {serial_number}: {e}")
        return jsonify({"error": str(e), "serial": serial_number}), 503
    except Exception as e:
        print(f"[ERROR] terminal-info {serial_number}: {e}")
        return jsonify({"error": str(e)}), 500

    adapter = ep["adapter"]
    print(f"[INFO] terminal-info {serial_number}: transport={ep['transport']} "
          f"host={ep['host']} vcom={ep['vcom_port']} tty={ep['tty']}")
    return jsonify({
        "serial":          serial_number,
        "transport":       ep["transport"],     # "tcp" | "serial"
        "host":            ep["host"],
        "vcom":            ep["vcom_port"],
        "tty":             ep["tty"],
        "baudrate":        ep["baudrate"],
        "connectivity":    ep["connectivity"],
        "admin_available": False,                # admin console removed
        "label":           adapter.get("label", serial_number),
        "nickname":        adapter.get("nickname", ""),
    })

# ── WebSocket VCOM bridge ──────────────────────────
active_telnets = {}   # room_id -> VcomLink   (name kept for frontend/back-compat)
active_boards  = {}   # room_id -> current Board instance (updated each run)



# ── shared VCOM reader ─────────────────────────────
# ONE reader for both the interactive terminal and board scripts. It streams raw
# bytes to xterm (terminal_output), logs them, and dispatches accumulated data to
# the active Board's VCOM listeners (cli()/read()) and to the run panel.
_session_logs = {}   # serial_id -> session log path (for command logging)


def _start_vcom_reader(link, room, session_log=None):
    serial_id = room.split("_")[0]
    if session_log:
        _session_logs[serial_id] = session_log
    os.makedirs(LOG_DIR, exist_ok=True)
    raw_log_path = os.path.join(
        LOG_DIR, f"{serial_id}_raw_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_vcom.log")
    try:
        with open(raw_log_path, "w") as _rf:
            _rf.write(f"# room={room} link={getattr(link, 'info', '?')}\n")
    except Exception:
        raw_log_path = None

    log_line_buf = [""]

    def reader():
        buf = []
        while True:
            try:
                chunk = link.recv(4096)
            except Exception as e:
                print(f"[VCOM] reader error {room}: {e}")
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")

            if raw_log_path:
                ts_raw = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                try:
                    with open(raw_log_path, "a") as _rf:
                        printable = text.replace("\r", "\\r").replace("\n", "\\n")
                        _rf.write(f"[{ts_raw}][RX] {chunk.hex()}  |  {printable}\n")
                except Exception:
                    pass

            # Raw passthrough to xterm (Classic terminal — no parser)
            socketio.emit("terminal_output", {"data": text, "room": room}, room=room)

            # Per-line session log
            if session_log:
                combined = (log_line_buf[0] + text).replace("\r\n", "\n").replace("\r", "\n")
                parts = combined.split("\n")
                log_line_buf[0] = parts[-1]
                if len(parts) > 1:
                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    try:
                        with open(session_log, "a") as f:
                            for ln in parts[:-1]:
                                if ln.strip():
                                    f.write(f"[{ts}][VCOM] {ln.strip()}\n")
                    except Exception:
                        pass

            # Dispatch to the active board (script cli()/read() + run panel)
            board = active_boards.get(room)
            if board:
                buf.append(text)
                combined = "".join(buf)
                for listener in list(board._vcom_listeners):
                    try:
                        listener(combined)
                    except Exception:
                        pass
                if board.run_id is not None:
                    socketio.emit("run_output",
                                  {"serial": board.serial, "data": text, "stream": "vcom"},
                                  room=f"run_{board.run_id}")
                if board._vcom_prompt and board._vcom_prompt in combined:
                    buf.clear()

        # ── cleanup ──
        if session_log and log_line_buf[0].strip():
            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            try:
                with open(session_log, "a") as f:
                    f.write(f"[{ts}][VCOM] {log_line_buf[0].strip()}\n")
            except Exception:
                pass
        try:
            link.close()
        except Exception:
            pass
        if active_telnets.get(room) is link:
            active_telnets.pop(room, None)
        active_boards.pop(room, None)
        socketio.emit("terminal_closed", {"room": room}, room=room)

    threading.Thread(target=reader, daemon=True).start()


@socketio.on("connect_vcom")
def handle_connect_vcom(data):
    """Open (or reuse) the single VCOM link for a board. Same path for USB & IP:
    the transport (pyserial tty vs TCP 4901) is resolved server-side."""
    serial = str(data.get("serial") or str(data.get("room", "")).split("_")[0])
    room   = data.get("room") or f"{serial}_vcom"
    if room.endswith("_admin"):
        emit("terminal_error", {"message": "admin console removed"})
        return
    join_room(room)

    if active_telnets.get(room) is not None:
        emit("terminal_ready", {"room": room})
        return

    try:
        ep   = resolve_endpoint(serial, baudrate=data.get("baudrate"))
        link = open_vcom_link(ep)
        active_telnets[room] = link
        session_log = get_log_file(serial)
        try:
            with open(session_log, "a") as f:
                f.write(f"# Session started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Board: {serial}  Link: {link.info}\n")
        except Exception:
            pass
        _start_vcom_reader(link, room, session_log=session_log)
        emit("terminal_ready", {"room": room})
        print(f"[VCOM] ready {room} via {link.info}")
    except EndpointError as e:
        emit("terminal_error", {"message": str(e)})
    except Exception as e:
        print(f"[VCOM] connect failed {room}: {e}")
        emit("terminal_error", {"message": str(e)})


# Back-compat shims: old frontend emitted connect_telnet (IP) / connect_tty (USB).
# Both now resolve to the same server-side VCOM link.
@socketio.on("connect_telnet")
def handle_connect_telnet(data):
    room = data.get("room", "")
    if room.endswith("_admin"):
        emit("terminal_error", {"message": "admin console removed"})
        return
    serial = data.get("serial") or room.split("_")[0]
    handle_connect_vcom({"room": room or f"{serial}_vcom", "serial": serial,
                         "baudrate": data.get("baudrate")})


@socketio.on("connect_tty")
def handle_connect_tty(data):
    room   = data.get("room", "")
    serial = data.get("serial") or room.split("_")[0]
    handle_connect_vcom({"room": room or f"{serial}_vcom", "serial": serial,
                         "baudrate": data.get("baudrate")})


@socketio.on("disconnect_vcom")
@socketio.on("disconnect_telnet")
def handle_disconnect_vcom(data):
    room = data.get("room")
    link = active_telnets.pop(room, None)
    active_boards.pop(room, None)
    if link:
        try:
            link.close()
        except Exception:
            pass


# Per-session input line buffer (VCOM is line-buffered to avoid the echo race)
_term_input_buf = {}


def _tin_ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _tin_log(serial_id, stream, cmd):
    log_path = _session_logs.get(str(serial_id))
    if log_path and cmd:
        try:
            with open(log_path, "a") as f:
                f.write(f"[{_tin_ts()}][{stream}][CMD] {cmd}\n")
        except Exception:
            pass


@socketio.on("terminal_input")
def handle_terminal_input(data):
    """VCOM input. Keystrokes (xterm) or whole lines (input bar) are accumulated
    and sent one line at a time on the terminating newline."""
    room = data.get("room")
    link = active_telnets.get(room)
    if not link:
        return
    try:
        serial_id = room.split("_")[0]
        key = f"{room}_{request.sid}"
        raw = data.get("data", "")

        if raw in ("\x7f", "\x08"):  # backspace edits the buffer
            _term_input_buf[key] = _term_input_buf.get(key, "")[:-1]
            return

        buf   = _term_input_buf.get(key, "") + raw
        norm  = buf.replace("\r\n", "\n").replace("\r", "\n")
        parts = norm.split("\n")
        _term_input_buf[key] = parts[-1]
        for line in parts[:-1]:
            cmd = line.strip()
            link.sendall((line + "\r\n").encode("utf-8"))
            if cmd:
                _tin_log(serial_id, "VCOM", cmd)
                socketio.emit("terminal_cmd", {"data": cmd, "room": room}, room=room)
    except Exception as e:
        emit("terminal_error", {"message": str(e)})


@app.route("/api/jslog", methods=["POST"])
def api_jslog():
    msg = request.json.get("msg", "")
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    import os as _os; _os.makedirs("logs", exist_ok=True)
    with open("logs/jslog.log", "a") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"[JSLOG] {msg}")
    return jsonify({"ok": True})
def _conn_flag_for(identifier):
    """Commander connection flag for a board identifier (serial or IP)."""
    rec = adapter_record(identifier) or {}
    if rec.get("connectivityType") == "ethernet" and rec.get("host"):
        return ["--ip", rec["host"]]
    if _is_ip(identifier):
        return ["--ip", str(identifier)]
    return ["--serialno", str(identifier)]


@app.route("/api/adapter/<serial>/erase", methods=["POST"])
def adapter_erase(serial):
    """Mass erase a single board from Manual Control."""
    try:
        cmd = [COMMANDER_PATH, "device", "masserase"] + _conn_flag_for(serial)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        ok = result.returncode == 0
        return jsonify({"ok": ok, "msg": result.stderr.strip() if not ok else ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/adapter/<serial>/flash", methods=["POST"])
def adapter_flash(serial):
    """Flash a firmware file onto a single board from Manual Control."""
    path = request.json.get("path", "")
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "file not found"}), 400
    try:
        cmd = [COMMANDER_PATH, "flash", path] + _conn_flag_for(serial)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ok = result.returncode == 0
        return jsonify({"ok": ok, "msg": result.stderr.strip() if not ok else ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/adapter/<serial>/reset", methods=["POST"])
def adapter_reset(serial):
    """Reset a single board via pycommander (replaces the old admin reset)."""
    rec = adapter_record(serial) or {}
    is_ip = rec.get("connectivityType") == "ethernet" or _is_ip(serial)
    ok = pyc_reset(ip=(rec.get("host") or serial)) if is_ip else pyc_reset(serial=serial)
    return jsonify({"ok": ok})


@app.route("/api/scenarios")
def list_scenarios():
    base = request.args.get("dir", SCENARI_DIR)
    base = os.path.abspath(base)
    result = []
    if not os.path.isdir(base):
        return jsonify(result)
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        if entry.is_dir():
            files = sorted([
                f.name for f in os.scandir(entry.path)
                if f.is_file() and f.name.endswith(".yaml")
            ])
            # Include directory even if empty
            result.append({"dir": entry.name, "files": files})
    return jsonify(result)

@app.route("/api/scenario-content")
def scenario_content():
    base = request.args.get("dir", SCENARI_DIR)  # ← use absolute default
    path = request.args.get("path", "")
    base = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base, path))
    if not full.startswith(base):
        return jsonify({"error": "invalid path"}), 400
    if not os.path.isfile(full):
        return jsonify({"error": "file not found"}), 404
    with open(full, "r") as f:
        return jsonify({"content": f.read()})

@app.route("/api/scenarios-default")
def scenarios_default():
    return jsonify({"path": SCENARI_DIR})

@app.route("/api/scenario-mkdir", methods=["POST"])
def scenario_mkdir():
    base = request.json.get("base", SCENARI_DIR)
    dir_name = request.json.get("dir", "").strip()
    if not dir_name:
        return jsonify({"error": "empty name"}), 400
    base = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base, dir_name))
    if not full.startswith(base):
        return jsonify({"error": "invalid path"}), 400
    os.makedirs(full, exist_ok=True)
    return jsonify({"ok": True})

@app.route("/api/scenario-open", methods=["GET"])
def scenario_open():
    """Read a YAML file by absolute path for the v2 editor."""
    path = request.args.get("path", "")
    path = os.path.abspath(path)
    if not os.path.isfile(path) or not path.endswith((".yaml", ".yml")):
        return jsonify({"ok": False, "error": "Invalid file"}), 400
    with open(path) as f:
        content = f.read()
    return jsonify({
        "ok":       True,
        "content":  content,
        "dir":      os.path.dirname(path),
        "filename": os.path.basename(path),
    })


@app.route("/api/scenario-open-save", methods=["POST"])
def scenario_open_save():
    """Write YAML content to an absolute path for the v2 editor."""
    path    = request.json.get("path", "")
    content = request.json.get("content", "")
    print(f"[SAVE] path={path!r} content_len={len(content)}")
    path    = os.path.abspath(path)
    print(f"[SAVE] abspath={path!r}")
    if not path.endswith((".yaml", ".yml")):
        print(f"[SAVE] rejected: not yaml")
        return jsonify({"ok": False, "error": "Must be a .yaml file"}), 400
    if not content.strip():
        print(f"[SAVE] rejected: empty content")
        return jsonify({"ok": False, "error": "Content is empty"}), 400
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"[SAVE] wrote {len(content)} chars to {path}")
    return jsonify({"ok": True, "dir": os.path.dirname(path)})


@app.route("/api/scenario-update", methods=["POST"])
def scenario_update():
    """Overwrite an existing scenario file with new content."""
    base    = request.json.get("base", SCENARI_DIR)
    path    = request.json.get("path", "").strip()
    content = request.json.get("content", "")
    base    = os.path.abspath(base)
    full    = os.path.abspath(os.path.join(base, path))
    if not full.startswith(base) or not full.endswith(".yaml"):
        return jsonify({"error": "invalid path"}), 400
    with open(full, "w") as f:
        f.write(content)
    return jsonify({"ok": True})


@app.route("/api/scenario-save", methods=["POST"])
def scenario_save():
    base    = request.json.get("base", SCENARI_DIR)
    dir_name = request.json.get("dir", "").strip()
    name    = request.json.get("name", "").strip()
    content = request.json.get("content", "")
    if not dir_name or not name:
        return jsonify({"error": "missing dir or name"}), 400
    base = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base, dir_name, name + ".yaml"))
    if not full.startswith(base):
        return jsonify({"error": "invalid path"}), 400
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return jsonify({"ok": True})

@app.route("/api/scenario-check", methods=["POST"])
def scenario_check():
    base     = request.json.get("base", SCENARI_DIR)
    path     = request.json.get("path", "")
    content  = request.json.get("content", None)
    dir_name = request.json.get("dir", "")
    base     = os.path.abspath(base)

    if content is not None:
        scenario_dir = os.path.abspath(os.path.join(base, dir_name)) if dir_name else base
    else:
        full = os.path.abspath(os.path.join(base, path))
        if not full.startswith(base) or not os.path.isfile(full):
            return jsonify({"ok": False, "issues": [{"line": None, "msg": "File not found"}]}), 400
        scenario_dir = os.path.dirname(full)
        with open(full) as f:
            content = f.read()

    scenario_dir = os.path.abspath(scenario_dir)

    # Load available adapters
    try:
        adapters_data = get_adapters()
    except Exception as e:
        return jsonify({"ok": False, "issues": [{"line": None, "msg": f"Adapter discovery failed: {e}"}]}), 500

    # Build adapter lookup maps
    # host → adapter, serialNumber → adapter
    adapter_by_host   = {a.get("host"):         a for a in adapters_data if a.get("host") and not _is_loopback_host(a.get("host"))}
    adapter_by_serial = {a.get("serialNumber"):  a for a in adapters_data if a.get("serialNumber")}

    def is_valid_ip(s):
        s = str(s)
        parts = s.split(".")
        if len(parts) != 4: return False
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except:
            return False

    def is_valid_serial(s):
        return re.match(r"^\d{9}$", str(s)) is not None
    # Parse YAML
    try:
        scenario = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "issues": [{"line": None, "msg": f"YAML parse error: {e}"}], "content": content})

    lines = content.split("\n")
    def find_line(keyword, start=0):
        for i, l in enumerate(lines[start:], start=start):
            if keyword in l:
                return i + 1
        return None

    issues = []
    boards = scenario.get("boards", [])

    if not boards:
        issues.append({"line": find_line("boards"), "msg": "No boards defined"})

    board_search_start = 0
    for i, board in enumerate(boards):
        board_line = find_line("- board", board_search_start)
        if board_line:
            board_search_start = board_line

        def issue(key, msg):
            line = find_line(key + ":", board_search_start - 1) if key else board_line
            issues.append({"line": line, "msg": f"Board #{i+1}: {msg}", "level": "error"})

        def warn(key, msg):
            line = find_line(key + ":", board_search_start - 1) if key else board_line
            issues.append({"line": line, "msg": f"Board #{i+1}: {msg}", "level": "warning"})

        def info(key, msg):
            line = find_line(key + ":", board_search_start - 1) if key else board_line
            issues.append({"line": line, "msg": f"ℹ Board #{i+1}: {msg}", "level": "info"})
        # ── board (mandatory) ──────────────────────────
        board_id = board.get("board", "")
        if not board_id:
            issue("board", "'board' is mandatory and is empty")
        # NOTE: the old SDM board-catalog lookup ("sdm board search") is gone.
        # board_id is still cross-checked against discovered adapters below.

        # ── connection (optional, replaces jlink_name_or_ip) ──
        # Accepts both old key (jlink_name_or_ip) and new key (connection)
        connection = str(board.get("connection", board.get("jlink_name_or_ip", "usb"))).strip()
        if connection.lower() in ("none", ""):
            connection = "usb"
        matched_adapter = None
        if _is_loopback_host(connection):
            issue("connection", "127.0.0.1/localhost is a local Silink transport host, not a USB adapter identity. Use 'usb' or the adapter serial number.")

        if connection != "usb":
            if not is_valid_ip(connection) and not is_valid_serial(connection):
                issue("connection",
                      f"'{connection}' must be a valid IP address (x.x.x.x), "
                      f"a Silabs serial number (9 digits), or 'usb'")
            else:
                matched_adapter = adapter_by_host.get(connection) or adapter_by_serial.get(connection)
                if not matched_adapter:
                    issue("connection",
                          f"'{connection}' not found in available adapters")
                elif board_id:
                    yaml_board_norm = re.sub(r'^BRD', '', str(board_id), flags=re.IGNORECASE)
                    adapter_boards  = matched_adapter.get("boards", [])
                    adapter_board_ids = set()
                    for ab in adapter_boards:
                        for field in ["id", "shortLabel", "label", "pn"]:
                            val = ab.get(field, "")
                            if val:
                                normalized = re.sub(r'^BRD', '', val.split()[0], flags=re.IGNORECASE).upper()
                                adapter_board_ids.add(normalized)
                    if yaml_board_norm not in adapter_board_ids:
                        nick      = matched_adapter.get("nickname") or matched_adapter.get("serialNumber", "")
                        best_id   = adapter_boards[0].get("shortLabel", adapter_boards[0].get("id", "?")) if adapter_boards else "?"
                        best_short = re.sub(r'^BRD', '', best_id, flags=re.IGNORECASE)
                        issue("board",
                              f"adapter '{connection}' ({nick}) has board '{best_short}' "
                              f"but scenario specifies '{yaml_board_norm}'. "
                              f"Consider changing board to '{best_short}'")

        # If board_id set but connection=usb — check if an available adapter has that board
        if board_id and connection == "usb":
            yaml_board_norm = re.sub(r'^BRD', '', str(board_id), flags=re.IGNORECASE).upper()
            matching = []
            for a in adapters_data:
                adapter_boards = a.get("boards", [])
                for ab in adapter_boards:
                    for field in ["id", "shortLabel"]:
                        val = ab.get(field, "")
                        normalized = re.sub(r'^BRD', '', val.split()[0] if val else "", flags=re.IGNORECASE).upper()
                        if normalized == yaml_board_norm:
                            matching.append(a)
                            break

            if matching:
                suggestions = ", ".join(
                    f"{a.get('host') or a.get('serialNumber', '?')}"
                    + (f" ({a.get('nickname')})" if a.get('nickname') else "")
                    for a in matching
                )
                warn("connection",
                     f"connection=usb — board '{yaml_board_norm}' found on: {suggestions}. "
                     f"Run will pick one randomly")
            else:
                if adapters_data:
                    warn("connection",
                         f"connection=usb and no adapter with board '{yaml_board_norm}' found")
                else:
                    info("connection",
                         f"no adapters available — connect a board with '{yaml_board_norm}'")
        # ── booleans (optional, with defaults) ────────
        for field, default in [("masserase", True), ("halt_reset", False), ("open_terminal", False)]:
            val = board.get(field)
            if val is not None and not isinstance(val, bool):
                issue(field, f"'{field}' must be true or false (default: {str(default).lower()}), got '{val}'")
            # No issue if missing — defaults apply

        # ── s37_files (optional) ──────────────────────
        s37_files = board.get("s37_files", [])
        if s37_files:
            if not isinstance(s37_files, list):
                issue("s37_files", "'s37_files' must be a list")
            else:
                for f_name in s37_files:
                    if str(f_name).strip() in ("", ".s37"):
                        continue  # placeholder, skip
                    f_path = os.path.join(scenario_dir, str(f_name))
                    if not os.path.isfile(f_path):
                        line = find_line(str(f_name), board_search_start - 1)
                        issues.append({"line": line, "msg": f"Board #{i+1}: s37 file '{f_name}' not found in scenario directory"})

        # ── script (optional) ─────────────────────────
        script = board.get("script", "")
        if script and isinstance(script, str) and script.strip():
            script_path = os.path.join(scenario_dir, script)
            if not os.path.isfile(script_path):
                issue("script", f"script '{script}' not found in scenario directory")

    # ── nickname uniqueness ───────────────────────────────────
    yaml_nicks = [str(b.get("nickname", "")).strip() for b in boards if b.get("nickname")]
    if len(yaml_nicks) != len(set(yaml_nicks)):
        issues.append({"line": find_line("nickname"), "msg": "Duplicate nicknames in scenario", "level": "error"})

    # ── global script (optional) ──────────────────────────────
    global_script = scenario.get("script")
    global_script = str(global_script).strip() if global_script and str(global_script).strip().lower() not in ("none", "null", "") else ""
    if global_script:
        gsp = os.path.join(scenario_dir, global_script)
        if not os.path.isfile(gsp):
            issues.append({"line": find_line("script:"), "msg": f"Global script '{global_script}' not found", "level": "error"})
        else:
            try:
                with open(gsp) as f:
                    compile(f.read(), gsp, "exec")
            except SyntaxError as e:
                issues.append({"line": find_line("script:"), "msg": f"Global script syntax error: {e}", "level": "error"})

    errors_only = [i for i in issues if i.get("level") == "error"]
    return jsonify({
        "ok":      len(issues) == 0,
        "issues":  issues,
        "content": content
    })

@app.route("/api/scenario-copy-file", methods=["POST"])
def scenario_copy_file():
    src      = request.json.get("src", "")
    base     = request.json.get("base", SCENARI_DIR)
    dir_name = request.json.get("dir", "")
    base     = os.path.abspath(base)
    dest_dir = os.path.abspath(os.path.join(base, dir_name))

    if not dest_dir.startswith(base):
        return jsonify({"ok": False, "error": "invalid path"}), 400

    src = os.path.abspath(src)
    filename = os.path.basename(src)
    dest = os.path.join(dest_dir, filename)

    # Already in scenario dir — no copy needed
    if os.path.dirname(src) == dest_dir:
        return jsonify({"ok": True, "filename": filename, "copied": False})

    try:
        import shutil
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, dest)
        return jsonify({"ok": True, "filename": filename, "copied": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    



# ============================================================
# Scenario orchestration class
# ============================================================

class Scenario:
    """
    Injected as `scenario` in the global script.
    Orchestrates all boards : reset, per-board script execution,
    direct board access and checkpoint-based synchronisation.
    """

    def __init__(self, boards, board_cfgs, scenario_dir, run_id):
        self._boards      = boards          # list[Board] in YAML order
        self._board_cfgs  = board_cfgs      # list[dict] — raw YAML board entries
        self._scenario_dir = scenario_dir
        self._run_id      = run_id
        self._run_room    = f"run_{run_id}"

        # Checkpoint synchronisation
        # name -> {serials_expected: set, events: {serial: Event}}
        self._checkpoints = {}
        self._cp_lock     = threading.Lock()

        # Board-script futures (populated by run_board_scripts)
        self._board_futures = []
        self._board_executor = None

        # Wire back-reference so board.checkpoint() can call us
        for b in self._boards:
            b._scenario_ctx = self

    # ── board access ─────────────────────────────────────────
    @property
    def boards(self):
        return list(self._boards)

    def board(self, identifier):
        """
        Access a board by:
          - int   → index in YAML order (0-based)
          - str   → serial number, scenario nickname, or SDM nickname
        Raises ValueError with a helpful message if not found.
        """
        if isinstance(identifier, int):
            if 0 <= identifier < len(self._boards):
                return self._boards[identifier]
            raise ValueError(
                f"scenario.board({identifier}): index out of range "
                f"(scenario has {len(self._boards)} boards, 0-{len(self._boards)-1})"
            )
        s = str(identifier).strip()
        for b in self._boards:
            if b.serial == s or b.nickname == s:
                return b
        raise ValueError(
            f"scenario.board({identifier!r}): no board with serial or nickname '{s}'. "
            f"Available: {[b.nickname or b.serial for b in self._boards]}"
        )

    # ── lifecycle ─────────────────────────────────────────────
    def release_reset(self):
        """Send reset to all boards simultaneously (parallel)."""
        self.print("[scenario] release_reset — resetting all boards in parallel")
        with ThreadPoolExecutor(max_workers=len(self._boards)) as ex:
            futures = {ex.submit(b.reset): b for b in self._boards}
            for fut in as_completed(futures):
                b = futures[fut]
                try:
                    fut.result()
                    self.print(f"[scenario] reset done: {b.nickname or b.serial}")
                except Exception as e:
                    self.print(f"[scenario] reset error on {b.nickname or b.serial}: {e}")

    def run_board_scripts(self, wait=True):
        """
        Execute each board's per-board script in parallel.
        wait=True  (default) — blocks until all scripts finish.
        wait=False           — returns immediately; call wait_board_scripts() later.
        """
        self._board_executor = ThreadPoolExecutor(max_workers=len(self._boards))
        self._board_futures = []
        for b, cfg in zip(self._boards, self._board_cfgs):
            script_file = cfg.get("script", "")
            if not script_file:
                continue
            script_path = os.path.join(self._scenario_dir, script_file)
            if not os.path.isfile(script_path):
                self.print(f"[scenario] WARNING: script not found for {b.nickname or b.serial}: {script_file}")
                continue
            with open(script_path) as f:
                code = f.read()
            self._board_futures.append(
                self._board_executor.submit(_exec_board_script, b, code, self._run_id)
            )
        if wait:
            self.wait_board_scripts()

    def wait_board_scripts(self):
        """Block until all per-board scripts have finished."""
        for fut in as_completed(self._board_futures):
            try:
                fut.result()
            except Exception as e:
                self.print(f"[scenario] board script exception: {e}")
        if self._board_executor:
            self._board_executor.shutdown(wait=False)

    # ── checkpoint synchronisation ────────────────────────────
    def _board_checkpoint(self, board, name):
        """Called by board.checkpoint(name) from a per-board script thread."""
        serial = board.serial
        with self._cp_lock:
            if name not in self._checkpoints:
                # Auto-create with all boards as expected
                self._checkpoints[name] = {
                    "expected": {b.serial for b in self._boards},
                    "arrived":  set(),
                    "events":   {},
                    "global_event": threading.Event(),
                }
            cp = self._checkpoints[name]
            cp["arrived"].add(serial)
            self.print(f"[checkpoint:{name}] {board.nickname or serial} arrived "
                       f"({len(cp['arrived'])}/{len(cp['expected'])})")
            # Notify any per-serial waiters
            if serial in cp["events"]:
                cp["events"][serial].set()
            # If all expected boards arrived — fire the global event
            if cp["expected"].issubset(cp["arrived"]):
                cp["global_event"].set()

    def wait_checkpoint(self, name, serials=None, timeout=30.0):
        """
        Block until the named checkpoint has been reached.
        serials=None  → wait for ALL boards.
        serials=[...]  → wait only for the listed serials/nicknames.
        Returns True if checkpoint reached, False if timeout.
        """
        # Resolve serials list
        if serials is None:
            expected = {b.serial for b in self._boards}
        else:
            expected = set()
            for ident in serials:
                try:
                    expected.add(self.board(ident).serial)
                except ValueError as e:
                    self.print(f"[scenario] wait_checkpoint warning: {e}")

        with self._cp_lock:
            if name not in self._checkpoints:
                self._checkpoints[name] = {
                    "expected": expected,
                    "arrived":  set(),
                    "events":   {},
                    "global_event": threading.Event(),
                }
            cp = self._checkpoints[name]
            # Override expected set with the caller's intent
            cp["expected"] = expected
            # Already there?
            if expected.issubset(cp["arrived"]):
                return True
            evt = cp["global_event"]

        self.print(f"[scenario] waiting checkpoint '{name}' "
                   f"(expecting: {[self._nick(s) for s in expected]})...")
        reached = evt.wait(timeout=timeout)
        if not reached:
            with self._cp_lock:
                cp = self._checkpoints.get(name, {})
                missing = expected - cp.get("arrived", set())
            self.print(f"[scenario] TIMEOUT waiting checkpoint '{name}' — "
                       f"missing: {[self._nick(s) for s in missing]}")
        return reached

    def _nick(self, serial):
        for b in self._boards:
            if b.serial == serial:
                return b.nickname or serial
        return serial

    # ── convenience ──────────────────────────────────────────
    def cli_all(self, cmd, timeout=10.0):
        """Send same CLI command to all boards in parallel, return {serial: response}."""
        results = {}
        with ThreadPoolExecutor(max_workers=len(self._boards)) as ex:
            futures = {ex.submit(b.cli, cmd, timeout): b for b in self._boards}
            for fut in as_completed(futures):
                b = futures[fut]
                try:
                    results[b.nickname or b.serial] = fut.result()
                except Exception as e:
                    results[b.nickname or b.serial] = f"ERROR: {e}"
        return results

    def delay(self, seconds):
        self.print(f"[scenario] delay {seconds}s")
        time.sleep(seconds)

    def print(self, msg):
        run_room = self._run_room
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        socketio.emit("run_output",
                      {"serial": "_scenario_", "data": f"[{ts}] {msg}\n", "stream": "script"},
                      room=run_room)
        print(f"[SCENARIO] {msg}")


def _exec_board_script(board, script_code, run_id):
    """Execute a per-board script. Called from thread pool."""
    run_room = f"run_{run_id}"
    serial   = board.serial

    def emit_status(status, msg=""):
        active_runs[run_id]["boards"][serial] = status
        socketio.emit("run_board_status",
                      {"serial": serial, "status": status, "msg": msg},
                      room=run_room)

    def log_ts(msg):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        socketio.emit("run_output",
                      {"serial": serial, "data": f"[{ts}] {msg}\n", "stream": "script"},
                      room=run_room)

    emit_status("running")
    try:
        namespace = {
            "board": board,
            "time":  time,
            "__builtins__": __builtins__,
        }
        exec(script_code, namespace)
        if "script" in namespace and callable(namespace["script"]):
            namespace["script"](board)
        emit_status("done")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        socketio.emit("run_output",
                      {"serial": serial, "data": tb, "stream": "error"},
                      room=run_room)
        emit_status("error", str(e))
        raise


# ============================================================
# Board class + Run pipeline
# ============================================================

active_runs = {}  # run_id -> {status, boards: {serial: status}}

# ── VCOM line ending helper ──────────────────────────────────
LINE_ENDINGS = {"CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}

class Board:
    """One board's VCOM channel. USB (pyserial) and IP (TCP 4901) are identical
    above the transport, which is hidden behind VcomLink. No admin console, no
    protocol parser: the terminal is raw passthrough and reset goes through
    pycommander."""

    def __init__(self, serial, run_id=None, scenario_dir="",
                 open_terminal=False, nickname=None):
        self.serial        = serial
        self.nickname      = nickname
        self.run_id        = run_id
        self.scenario_dir  = scenario_dir
        self.open_terminal_flag = open_terminal

        # Single VCOM transport (set in connect(), or injected when reusing a
        # link already opened by the terminal window).
        self._vcom_link = None

        # VCOM config (set by script via config_vcom)
        self._vcom_le     = b"\r\n"
        self._vcom_echo   = True
        self._vcom_prompt = ">"

        # Connectivity (filled in connect(); used by reset())
        self._connectivity = None      # "usb" | "ethernet"
        self._host         = ""        # IP host for ethernet boards

        self._vcom_listeners = []
        self._log_path       = None
        self._scenario_ctx   = None    # set by Scenario when a global script runs

    # ── configuration ────────────────────────────────────────
    def config_vcom(self, line_ending="CRLF", echo=True, prompt=">"):
        self._vcom_le     = LINE_ENDINGS.get(line_ending.upper(), b"\r\n")
        self._vcom_echo   = echo
        self._vcom_prompt = prompt

    # ── connect ──────────────────────────────────────────────
    def connect(self):
        room = f"{self.serial}_vcom"
        # Always register as the active dispatcher for this room; the shared
        # reader uses active_boards[room] at dispatch time, so reusing a link
        # across runs automatically picks up the new Board.
        active_boards[room] = self

        ep = resolve_endpoint(self.serial)
        self._connectivity = ep["connectivity"]
        self._host         = ep["host"]

        if room in active_telnets:
            self._vcom_link = active_telnets[room]
            print(f"[RUN] reusing existing VCOM link for {room}")
            socketio.emit("terminal_ready", {"room": room}, room=room)
        else:
            link = open_vcom_link(ep)
            self._vcom_link = link
            active_telnets[room] = link
            _start_vcom_reader(link, room, session_log=self._log_path)
            print(f"[RUN] new VCOM link for {room} via {link.info}")
            socketio.emit("terminal_ready", {"room": room}, room=room)

        if self.open_terminal_flag:
            def _open():
                url = f"http://127.0.0.1:{WEB_PORT}/terminal/{self.serial}?from_run=1"
                webview.create_window(
                    f"{self.serial} — Terminal",
                    url, width=820, height=620, resizable=True,
                )
            threading.Thread(target=_open, daemon=True).start()

    # ── VCOM I/O ─────────────────────────────────────────────
    def _vcom_send(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if self._vcom_link:
            self._vcom_link.sendall(data)

    def read(self, lines=1, timeout=5.0, pattern=None, include_partial=False):
        """Read spontaneous VCOM lines WITHOUT sending a command (e.g. to capture
        an EVENT). Same reader path as cli(): identical on USB and IP, and the
        data stays visible in the terminal.

        lines           : number of complete lines to wait for (default 1).
        timeout         : max seconds.
        pattern         : optional regex/substring; returns as soon as a line
                          matches (lines is then ignored).
        include_partial : also consider the last not-yet-newline-terminated line.
        Returns the captured lines (empty list on timeout).
        """
        if not self._vcom_link:
            return []
        rx = re.compile(pattern) if pattern else None
        event = threading.Event()
        committed = []
        st = {"prev_len": 0, "last": []}

        def _done():
            ls = committed + st["last"]
            if rx:
                return any(rx.search(x) for x in ls)
            return len(ls) >= lines

        def on_data(combined):
            if len(combined) < st["prev_len"]:      # buffer cleared on prompt
                committed.extend(st["last"])
                st["last"] = []
            st["prev_len"] = len(combined)
            cur = [x.rstrip("\r") for x in combined.splitlines()]
            if include_partial or combined.endswith(("\n", "\r")):
                st["last"] = cur
            else:
                st["last"] = cur[:-1]
            if _done():
                event.set()

        self._vcom_listeners.append(on_data)
        try:
            event.wait(timeout=timeout)
        finally:
            try:
                self._vcom_listeners.remove(on_data)
            except ValueError:
                pass

        out = committed + st["last"]
        if rx:
            res = []
            for x in out:
                res.append(x)
                if rx.search(x):
                    break
            return res
        return out[:lines] if lines else out

    def cli(self, cmd, timeout=10.0):
        """Send a command on VCOM and return its response.

        With the parser removed, completion is detected by the prompt plus an
        idle fallback: once data stops arriving for `idle` seconds we return what
        we have. Output whose tail isn't a recognizable prompt returns on the
        idle timer instead of instantly.
        """
        if not self._vcom_link:
            return ""
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[CLI] {self.nickname or self.serial} {ts} >>> {cmd}")

        prompt   = self._vcom_prompt
        event    = threading.Event()
        response = [""]
        last_rx  = [time.monotonic()]

        def on_data(combined):
            response[0] = combined
            last_rx[0]  = time.monotonic()
            # Prompt seen on a fresh line after the echo -> response complete
            tail = combined.splitlines()[-1] if combined.splitlines() else ""
            if prompt and tail.strip().endswith(prompt):
                event.set()

        self._vcom_listeners.append(on_data)
        vcom_room = f"{self.serial}_vcom"
        socketio.emit("terminal_cmd", {"data": cmd, "room": vcom_room}, room=vcom_room)
        self._vcom_send(cmd.encode("utf-8") + self._vcom_le)

        # Wait for prompt, with an idle fallback so we never hang past `timeout`.
        idle     = 0.4
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if event.wait(timeout=0.1):
                break
            if response[0] and (time.monotonic() - last_rx[0]) >= idle:
                break

        try:
            self._vcom_listeners.remove(on_data)
        except ValueError:
            pass
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[CLI] {self.nickname or self.serial} {ts} <<< "
              f"{'ok' if event.is_set() else 'idle/timeout'}")

        raw   = response[0]
        lines = raw.splitlines()
        if lines and cmd.strip() in lines[0]:      # drop echoed command
            lines = lines[1:]
        while lines and lines[-1].strip() in (prompt, ""):  # drop trailing prompt
            lines.pop()
        return "\n".join(lines)

    # ── reset (pycommander) ──────────────────────────────────
    def reset(self, settle=1.0):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[RST] {self.nickname or self.serial} {ts}")
        if self._connectivity == "ethernet" and self._host:
            ok = pyc_reset(ip=self._host)
        elif _is_ip(self.serial):
            ok = pyc_reset(ip=self.serial)
        else:
            ok = pyc_reset(serial=self.serial)
        if not ok:
            print(f"[RST] {self.nickname or self.serial} reset reported failure")
        time.sleep(settle)
        return ""

    # ── flash (Simplicity Commander via pycommander's bundled binary) ─────
    def flash(self, firmware_path: str, serial: str = None, ip: str = None,
              masserase: bool = True, halt_reset: bool = False) -> bool:
        """Flash a firmware file. Connection auto-detected from the board's
        discovered connectivity; override with serial=... or ip=...."""
        import os as _os
        firmware_path = _os.path.abspath(firmware_path)
        if not _os.path.isfile(firmware_path):
            print(f"[FLASH] ERROR: file not found: {firmware_path}")
            return False

        if serial:
            conn_flag = ["--serialno", serial]
        elif ip:
            conn_flag = ["--ip", ip]
        else:
            conn_flag = _conn_flag_for(self.serial)

        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[FLASH] {self.nickname or self.serial} {ts} {firmware_path} conn={conn_flag}")
        vcom_room = f"{self.serial}_vcom"

        def _emit(msg):
            print(f"[FLASH] {msg}")
            socketio.emit("terminal_output",
                          {"data": f"\x1b[33m{msg}\x1b[0m\r\n", "room": vcom_room},
                          room=vcom_room)

        def _run(cmd):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    _emit(f"Flash error: {r.stderr.strip() or r.stdout.strip()}")
                    return False
                return True
            except subprocess.TimeoutExpired:
                _emit("Flash timeout after 120s")
                return False
            except Exception as e:
                _emit(f"Flash exception: {e}")
                return False

        if masserase:
            _emit(f"Erasing {self.nickname or self.serial}...")
            if not _run([COMMANDER_PATH, "device", "masserase"] + conn_flag):
                return False

        _emit(f"Flashing {_os.path.basename(firmware_path)}...")
        cmd = [COMMANDER_PATH, "flash", firmware_path] + conn_flag
        if halt_reset:
            cmd += ["--halt"]
        if not _run(cmd):
            return False

        _emit(f"Flash complete — {_os.path.basename(firmware_path)}")
        return True

    # ── helpers ───────────────────────────────────────────────
    def delay(self, seconds):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[DLY] {self.nickname or self.serial} {ts} {seconds}s")
        time.sleep(seconds)

    def checkpoint(self, name):
        """Signal to the global scenario script that this board reached a point."""
        if self._scenario_ctx:
            self._scenario_ctx._board_checkpoint(self, name)

    def print(self, msg):
        run_room = f"run_{self.run_id}"
        socketio.emit("run_output",
                      {"serial": self.serial, "data": str(msg) + "\n", "stream": "script"},
                      room=run_room)
        if self._log_path:
            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            with open(self._log_path, "a") as f:
                f.write(f"[{ts}][script] {msg}\n")


# ── Run pipeline ─────────────────────────────────────────────

def get_log_file(serial):
    os.makedirs(LOG_DIR, exist_ok=True)
    dt = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Find next increment
    existing = [f for f in os.listdir(LOG_DIR) if f.startswith(serial)]
    n = len(existing) + 1
    return os.path.join(LOG_DIR, f"{serial}_{dt}_{n:03d}.log")

def _flash_board(board_cfg, scenario_dir, run_id=None):
    serial     = board_cfg.get("_resolved_serial", "")
    connection = str(board_cfg.get("connection", board_cfg.get("jlink_name_or_ip", "usb"))).strip()
    if connection.lower() in ("usb", "none", "") or _is_loopback_host(connection):
        connection = "usb"
    print(f"[RUN] _flash_board serial={serial} connection={connection}")

    # Build commander connection flag
    parts = connection.split(".")
    is_ip = len(parts) == 4 and all(p.isdigit() for p in parts)
    if connection == "usb":
        conn_flag = ["-s", serial]
    elif is_ip:
        conn_flag = ["--ip", connection]
    else:
        conn_flag = ["-s", connection]

    log = []

    def emit_status(status, msg=""):
        if run_id:
            run_room = f"run_{run_id}"
            active_runs[run_id]["boards"][serial] = status
            socketio.emit("run_board_status",
                          {"serial": serial, "status": status, "msg": msg},
                          room=run_room)

    def run_cmd(cmd):
        print(f"[RUN] running: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            log.append(" ".join(cmd))
            print(f"[RUN] returncode={result.returncode} stdout={result.stdout[:100]} stderr={result.stderr[:100]}")
            if result.returncode != 0:
                log.append(f"ERROR: {result.stderr.strip()}")
                return False
            return True
        except subprocess.TimeoutExpired:
            log.append(f"ERROR: timeout after 60s")
            return False
        except Exception as e:
            log.append(f"ERROR: {e}")
            return False

    if board_cfg.get("masserase", True):
        emit_status("erasing")
        if not run_cmd([COMMANDER_PATH, "device", "masserase"] + conn_flag):
            return False, log

    for s37 in board_cfg.get("s37_files", []):
        s37_path = os.path.join(scenario_dir, s37)
        emit_status("flashing")
        cmd = [COMMANDER_PATH, "flash", s37_path] + conn_flag
        if board_cfg.get("halt_reset", False):
            cmd += ["--halt"]
        if not run_cmd(cmd):
            return False, log

    print(f"[RUN] _flash_board done serial={serial}")
    return True, log


@app.route("/api/scenario-run", methods=["POST"])
def scenario_run():
    base = request.json.get("base", SCENARI_DIR)
    path = request.json.get("path", "")
    base = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base, path))

    if not full.startswith(base) or not os.path.isfile(full):
        return jsonify({"ok": False, "error": "File not found"}), 400

    scenario_dir = os.path.dirname(full)

    with open(full) as f:
        content = f.read()
    try:
        scenario_yaml = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "error": f"YAML error: {e}"}), 400

    boards_cfg = scenario_yaml.get("boards", [])
    if not boards_cfg:
        return jsonify({"ok": False, "error": "No boards defined"}), 400

    # Fetch available adapters via pycommander
    try:
        adapters_data = get_adapters()
        adapter_by_host   = {a.get("host"): a for a in adapters_data if a.get("host") and not _is_loopback_host(a.get("host"))}
        adapter_by_serial = {a.get("serialNumber"): a for a in adapters_data if a.get("serialNumber")}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Adapter discovery failed: {e}"}), 500

    # Resolve each board entry
    resolved_boards = []
    for b in boards_cfg:
        # connection: usb (default), USB serial number, or real external IP address
        connection = str(b.get("connection", b.get("jlink_name_or_ip", "usb"))).strip()
        if connection.lower() in ("usb", "none", ""):
            connection = "usb"
        if _is_loopback_host(connection):
            return jsonify({
                "ok": False,
                "error": "Do not use 127.0.0.1/localhost as a scenario connection. Use 'usb' or the USB adapter serial number."
            }), 400

        if connection != "usb":
            adapter = adapter_by_host.get(connection) or adapter_by_serial.get(connection)
        else:
            board_id = str(b.get("board", "")).strip()
            adapter = None
            if board_id:
                # Try to match by board type
                norm_id = re.sub(r"^BRD", "", board_id, flags=re.IGNORECASE).upper()
                for a in adapters_data:
                    for ab in a.get("boards", []):
                        for field in ["id", "shortLabel"]:
                            val = ab.get(field, "")
                            if re.sub(r"^BRD", "", val, flags=re.IGNORECASE).upper() == norm_id:
                                adapter = a
                                break
                        if adapter:
                            break
                    if adapter:
                        break
            # No board_id or no match by type → pick first available adapter
            if not adapter and adapters_data:
                adapter = adapters_data[0]
            # If the chosen adapter is reachable via a real external IP, use it
            # 127.0.0.1 means the board is local (USB via SDM) — keep connection=usb
            if adapter:
                adapter_host = adapter.get("host", "").strip()
                if adapter_host and adapter_host != "127.0.0.1":
                    connection = adapter_host

        if not adapter:
            return jsonify({"ok": False, "error": f"No adapter found for board entry {b.get('board', '?')}"}), 400

        bc = dict(b)
        bc["_resolved_serial"] = adapter.get("serialNumber")
        bc["connection"]       = connection

        # Nickname resolution: YAML > SDM > None
        yaml_nick = str(b.get("nickname", "")).strip() or None
        sdm_nick  = adapter.get("nickname") or None
        bc["_nickname"] = yaml_nick or sdm_nick

        resolved_boards.append(bc)

    # Check nickname uniqueness
    nicks = [bc["_nickname"] for bc in resolved_boards if bc["_nickname"]]
    if len(nicks) != len(set(nicks)):
        return jsonify({"ok": False, "error": "Duplicate nicknames in scenario"}), 400

    # Global script (optional)
    global_script_file = scenario_yaml.get("script")
    global_script_file = str(global_script_file).strip() if global_script_file and str(global_script_file).strip().lower() not in ("none", "null", "") else ""
    global_script_code = None
    if global_script_file:
        gsp = os.path.join(scenario_dir, global_script_file)
        if os.path.isfile(gsp):
            with open(gsp) as f:
                global_script_code = f.read()

    # Per-board script codes (optional per board)
    for bc in resolved_boards:
        sf = bc.get("script", "")
        bc["_script_code"] = ""
        if sf:
            sp = os.path.join(scenario_dir, sf)
            if os.path.isfile(sp):
                with open(sp) as f:
                    bc["_script_code"] = f.read()

    # Create run
    run_id = str(uuid.uuid4())[:8]
    active_runs[run_id] = {
        "status":   "running",
        "scenario": path,
        "boards":   {bc["_resolved_serial"]: "pending" for bc in resolved_boards}
    }

    def run_all():
        if global_script_code:
            # ── Mode with global script ───────────────────────────
            # Emit flashing status immediately so cards appear on UI
            run_room = f"run_{run_id}"
            for bc in resolved_boards:
                serial   = bc["_resolved_serial"]
                nickname = bc.get("_nickname")
                if nickname:
                    socketio.emit("run_board_nickname",
                                  {"serial": serial, "nickname": nickname},
                                  room=run_room)
                active_runs[run_id]["boards"][serial] = "flashing"
                socketio.emit("run_board_status",
                              {"serial": serial, "status": "flashing"},
                              room=run_room)

            # Flash all boards in parallel
            with ThreadPoolExecutor(max_workers=len(resolved_boards)) as ex:
                flash_futures = {ex.submit(_flash_board, bc, scenario_dir, run_id): bc
                                 for bc in resolved_boards}
                for fut in as_completed(flash_futures):
                    bc = flash_futures[fut]
                    serial = bc["_resolved_serial"]
                    try:
                        ok, flash_log = fut.result()
                    except Exception as e:
                        ok, flash_log = False, [str(e)]
                    if not ok:
                        active_runs[run_id]["boards"][serial] = "error"
                        socketio.emit("run_board_status",
                                      {"serial": serial, "status": "error", "msg": "Flash failed"},
                                      room=run_room)

            # Abort if any flash failed
            if any(v == "error" for v in active_runs[run_id]["boards"].values()):
                active_runs[run_id]["status"] = "done"
                socketio.emit("run_done", {"run_id": run_id}, room=f"run_{run_id}")
                return

            # Connect all boards
            board_objs = []
            for bc in resolved_boards:
                serial   = bc["_resolved_serial"]
                run_room = f"run_{run_id}"
                active_runs[run_id]["boards"][serial] = "connecting"
                socketio.emit("run_board_status",
                              {"serial": serial, "status": "connecting"},
                              room=run_room)
                try:
                    board_obj = Board(
                        serial        = serial,
                        run_id        = run_id,
                        scenario_dir  = scenario_dir,
                        open_terminal = bc.get("open_terminal", False),
                        nickname      = bc.get("_nickname"),
                    )
                    board_obj._log_path = get_log_file(serial)
                    board_obj.connect()
                    board_objs.append(board_obj)
                    active_runs[run_id]["boards"][serial] = "ready"
                    socketio.emit("run_board_status",
                                  {"serial": serial, "status": "ready"},
                                  room=run_room)
                except Exception as e:
                    active_runs[run_id]["boards"][serial] = "error"
                    socketio.emit("run_board_status",
                                  {"serial": serial, "status": "error", "msg": str(e)},
                                  room=run_room)

            # Run global script
            scenario_obj = Scenario(board_objs, resolved_boards, scenario_dir, run_id)
            run_room = f"run_{run_id}"
            try:
                namespace = {
                    "scenario": scenario_obj,
                    "time":     time,
                    "__builtins__": __builtins__,
                }
                exec(global_script_code, namespace)
                if "script" in namespace and callable(namespace["script"]):
                    namespace["script"](scenario_obj)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                socketio.emit("run_output",
                              {"serial": "_scenario_", "data": tb, "stream": "error"},
                              room=run_room)
        else:
            # ── Mode without global script ────────────────────────
            # Same 3-phase parallel execution as global script mode
            run_room = f"run_{run_id}"

            # Emit initial nicknames + flashing status
            for bc in resolved_boards:
                serial   = bc["_resolved_serial"]
                nickname = bc.get("_nickname")
                if nickname:
                    socketio.emit("run_board_nickname",
                                  {"serial": serial, "nickname": nickname},
                                  room=run_room)
                active_runs[run_id]["boards"][serial] = "flashing"
                socketio.emit("run_board_status",
                              {"serial": serial, "status": "flashing"},
                              room=run_room)

            # Phase 1: flash all boards in parallel
            failed = set()
            t0 = datetime.datetime.now()
            print(f"[RUN] Phase 1 flash start {t0.strftime('%H:%M:%S.%f')[:-3]} boards={[bc['_resolved_serial'] for bc in resolved_boards]}")
            with ThreadPoolExecutor(max_workers=len(resolved_boards)) as ex:
                flash_futures = {ex.submit(_flash_board, bc, scenario_dir, run_id): bc
                                 for bc in resolved_boards}
                for fut in as_completed(flash_futures):
                    bc     = flash_futures[fut]
                    serial = bc["_resolved_serial"]
                    try:
                        ok, flash_log = fut.result()
                    except Exception as e:
                        ok, flash_log = False, [str(e)]
                    if not ok:
                        failed.add(serial)
                        active_runs[run_id]["boards"][serial] = "error"
                        socketio.emit("run_board_status",
                                      {"serial": serial, "status": "error", "msg": "Flash failed"},
                                      room=run_room)

            # Phase 2: connect all successful boards in parallel
            board_objs = []  # list of (board_obj, bc) tuples
            print(f"[RUN] Phase 2 connect start {datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
            def connect_board(bc):
                serial = bc["_resolved_serial"]
                if serial in failed:
                    return None, bc
                active_runs[run_id]["boards"][serial] = "connecting"
                socketio.emit("run_board_status",
                              {"serial": serial, "status": "connecting"},
                              room=run_room)
                try:
                    board_obj = Board(
                        serial        = serial,
                        run_id        = run_id,
                        scenario_dir  = scenario_dir,
                        open_terminal = bc.get("open_terminal", False),
                        nickname      = bc.get("_nickname"),
                    )
                    board_obj._log_path = get_log_file(serial)
                    board_obj.connect()
                    active_runs[run_id]["boards"][serial] = "ready"
                    socketio.emit("run_board_status",
                                  {"serial": serial, "status": "ready"},
                                  room=run_room)
                    return board_obj, bc
                except Exception as e:
                    failed.add(serial)
                    active_runs[run_id]["boards"][serial] = "error"
                    socketio.emit("run_board_status",
                                  {"serial": serial, "status": "error", "msg": str(e)},
                                  room=run_room)
                    return None, bc

            with ThreadPoolExecutor(max_workers=len(resolved_boards)) as ex:
                connect_futures = {ex.submit(connect_board, bc): bc for bc in resolved_boards}
                for fut in as_completed(connect_futures):
                    board_obj, bc = fut.result()
                    if board_obj:
                        board_objs.append((board_obj, bc))

            # Phase 3: run board scripts in parallel
            print(f"[RUN] Phase 3 scripts start {datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]} boards={[b.serial for b,_ in board_objs]}")
            if board_objs:
                with ThreadPoolExecutor(max_workers=len(board_objs)) as ex:
                    futures = [
                        ex.submit(_exec_board_script, board_obj, bc["_script_code"], run_id)
                        for board_obj, bc in board_objs
                    ]
                    for fut in as_completed(futures):
                        try: fut.result()
                        except: pass

        active_runs[run_id]["status"] = "done"
        socketio.emit("run_done", {"run_id": run_id}, room=f"run_{run_id}")

    threading.Thread(target=run_all, daemon=True).start()
    return jsonify({"ok": True, "run_id": run_id})


@app.route("/api/run-status/<run_id>")
def run_status(run_id):
    run = active_runs.get(run_id)
    if not run:
        return jsonify({"error": "not found"}), 404
    return jsonify(run)


@socketio.on("join_run")
def handle_join_run(data):
    run_id = data.get("run_id")
    join_room(f"run_{run_id}")
    print(f"[RUN] client joined room run_{run_id}")
    # Send current state immediately to the newly joined client
    run = active_runs.get(run_id)
    if run:
        for serial, status in run["boards"].items():
            emit("run_board_status", {"serial": serial, "status": status, "msg": ""})
        print(f"[RUN] replayed status for {list(run['boards'].keys())}")

# ----------------------------
if __name__ == "__main__":
    TRACES = "--traces" in sys.argv

    # Silence all print() output unless --traces
    if not TRACES:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")

    # Warm up adapter discovery (pycommander). SDM is no longer used.
    try:
        refresh_adapters()
    except Exception as e:
        print(f"[WARN] initial adapter scan failed: {e}")

    import logging
    log_level = logging.DEBUG if TRACES else logging.ERROR
    logging.getLogger("werkzeug").setLevel(log_level)
    logging.getLogger("socketio").setLevel(log_level)
    logging.getLogger("engineio").setLevel(log_level)

    t = threading.Thread(
        target=lambda: socketio.run(app, port=WEB_PORT, debug=False,
                                    use_reloader=False, log_output=TRACES)
    )
    t.daemon = True
    t.start()
    webview.create_window(
        "My Lab",
        f"http://127.0.0.1:{WEB_PORT}",
        width=WIN_W,
        height=WIN_COLLAPSED,
        resizable=True,
        js_api=WindowAPI()
    )
    webview.start()