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
    if path and _is_exec(path):
        return path
    envp = os.environ.get("COMMANDER_BIN")
    if envp and _is_exec(envp):
        return envp
    if _is_exec(_DEFAULT_COMMANDER):
        return _DEFAULT_COMMANDER
    p = shutil.which("commander")
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

COMMANDER_PATH = resolve_commander(config["paths"].get("commander"))

# On ne sauvegarde que si un chemin était absent du config et vient d'être auto-détecté
_save_needed = False
if COMMANDER_PATH and not config["paths"].get("commander"):
    config["paths"]["commander"] = COMMANDER_PATH
    _save_needed = True

if _save_needed:
    with open(CONFIG_FILE, "w") as f:
        config.write(f)

HOST = config["server"].get("host", "127.0.0.1")
PORT = config["server"].get("port", "3129")
WEB_PORT = int(config["server"].get("web_port", "8080"))

# Terminal parser config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parsers import get_parser, format_block
TERMINAL_PARSER  = config["server"].get("parser",           "auto").strip().lower()
TERMINAL_PRETTY  = config["server"].get("terminal_pretty",  "true").strip().lower() == "true"

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ----------------------------
# ADAPTER ENUMERATION  (pycommander only — SDM/Silink fully removed)
# ----------------------------
_probe_cache: dict[str, dict] = {}   # serial -> kit_info (adapter probe result)

def invalidate_connections(serial=None):
    """Drop cached adapter probe info (after scan / flash / recover / erase)."""
    if serial is None:
        _probe_cache.clear()
    else:
        _probe_cache.pop(str(serial), None)

def clear_and_scan():
    """Re-enumerate adapters. pycommander has no scan server to poke; just drop
    cached probe data so the next listing is fresh."""
    invalidate_connections()

def get_adapters():
    """USB + network adapters via pycommander (see pyc_list_adapters)."""
    return pyc_list_adapters()

def _kit_cached(serial):
    serial = str(serial)
    if serial not in _probe_cache:
        _probe_cache[serial] = pyc_kit_info(serial=serial)
    return _probe_cache[serial]

# ----------------------------
# COMMANDER ACTIONS
# ----------------------------

def run_commander(cmd, serial):
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        invalidate_connections(serial)
        return {"serialNumber": serial, "ok": True, "output": result.stdout}
    except subprocess.CalledProcessError as e:
        invalidate_connections(serial)
        return {"serialNumber": serial, "ok": False, "error": e.stderr or str(e)}
    except Exception as e:
        invalidate_connections(serial)
        return {"serialNumber": serial, "ok": False, "error": str(e)}


def _conn_flag(serial):
    """Commander connection flag for a serial, from pycommander enumeration:
    --serialno (USB) or --ip <addr> (IP). Returns (flag_list, "usb"|"ip")."""
    a  = {x["serial"]: x for x in pyc_list_adapters()}.get(str(serial))
    ip = (a or {}).get("ip")
    if ip:
        return ["--ip", ip], "ip"
    return ["--serialno", str(serial)], "usb"


def _adapters_compat():
    """pycommander adapters in the legacy {serialNumber, host, nickname, boards}
    shape consumed by the scenario validator/resolver. There is no board
    database without SDM, so `boards` is always empty."""
    return [{
        "serialNumber": a["serial"],
        "host":         a.get("ip"),
        "nickname":     a.get("nickname", ""),
        "boards":       [],
    } for a in pyc_list_adapters()]


def reset_mcu(serial):
    """Reset the target MCU via pycommander `device reset` (Commander-CLI
    fallback). Independent of any admin console — works on USB and IP.
    Returns (ok: bool, msg: str). Single source of truth for MCU reset, used by
    both the Reset button route and Board.reset()."""
    flag, _ = _conn_flag(serial)              # ["--serialno", s] or ["--ip", ip]
    ip = flag[1] if flag[0] == "--ip" else None
    if _PYC_OK:
        try:
            res = _pyc(serial=None if ip else serial, ip=ip).device.reset()
            ok  = bool(res.get("success")) if isinstance(res, dict) else bool(res)
            print(f"[RST-MCU] {serial} via pycommander: {'ok' if ok else res}")
            return ok, ("" if ok else str(res))
        except Exception as e:
            print(f"[RST-MCU] {serial} pycommander error ({e}); CLI fallback")
    try:
        r = subprocess.run([COMMANDER_PATH, "device", "reset"] + flag,
                           capture_output=True, text=True, timeout=30)
        ok  = r.returncode == 0
        msg = (r.stdout if ok else r.stderr).strip()
        print(f"[RST-MCU] {serial} via CLI: {'ok' if ok else msg}")
        return ok, msg
    except Exception as e:
        print(f"[RST-MCU] {serial} error: {e}")
        return False, str(e)


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
# Adapter identity helpers (SDM/Silink fully removed)
# -----------------------------------------------------------------------------
#   - adapter identity: USB uses serialNumber, IP adapters use their real IP
# Never store/use 127.0.0.1 as the identity of a USB adapter.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_host(host: str | None) -> bool:
    return str(host or "").strip().lower() in _LOOPBACK_HOSTS


class EndpointError(RuntimeError):
    """Raised when no usable VCOM endpoint can be resolved for a board."""
    pass



def _find_usb_tty(serial_number):
    """Best-effort: locate the /dev/tty*|/dev/cu* device for a USB serial.

    Only used to *offer* a manual TTY fallback button — never to auto-connect.
    """
    try:
        import serial.tools.list_ports as list_ports
        for port in list_ports.comports():
            if str(serial_number).lower() in (port.serial_number or "").lower():
                return port.device
    except Exception as e:
        print(f"[WARN] list_ports {serial_number}: {e}")
    return None


# -----------------------------------------------------------------------------
# PYCOMMANDER enumeration + VCOM-path resolution  (replaces SDM/Silink)
# -----------------------------------------------------------------------------
# Adapter discovery and the VCOM path come from the silabs-pycommander package
# (`commander adapter list` / `adapter probe`), NOT from SDM/Silink telnet ports.
#   - USB : VCOM is the CDC tty reported as kit_info.vcom_port  -> pyserial
#   - IP  : VCOM is TCP  <ip_address>:IP_VCOM_PORT
# -----------------------------------------------------------------------------
IP_VCOM_PORT = int(config["server"].get("ip_vcom_port", "4901"))

try:
    from pycommander import Commander as _PycCommander
    _PYC_OK = True
except Exception as _pyc_e:           # pragma: no cover
    _PYC_OK = False
    print(f"[WARN] pycommander not available: {_pyc_e}")


def _pyc(serial=None, ip=None):
    return _PycCommander(
        serial_number=str(serial) if serial else None,
        ip_address=ip or None,
        executable_path=Path(COMMANDER_PATH) if COMMANDER_PATH else None,
    )


def pyc_list_adapters():
    """USB + network adapter enumeration via pycommander.

    Returns a list of {serial, ip, nickname, connectivity}. `ip` is None for USB.
    """
    out = []
    if not _PYC_OK:
        return out
    try:
        for u in (_pyc().listAvailableAdapters(list_usb_adapters=True) or []):
            if u.jlink_serial_number:
                out.append({"serial": str(u.jlink_serial_number), "ip": None,
                            "nickname": u.nickname or "", "connectivity": "usb"})
    except Exception as e:
        print(f"[WARN] pyc usb list: {e}")
    try:
        for n in (_pyc().listAvailableAdapters(list_network_adapters=True) or []):
            if n.jlink_serial_number:
                out.append({"serial": str(n.jlink_serial_number), "ip": n.ip_address,
                            "nickname": n.nickname or "", "connectivity": "ip"})
    except Exception as e:
        print(f"[WARN] pyc net list: {e}")
    return out


def pyc_kit_info(serial=None, ip=None):
    """`adapter probe` -> kit_info dict (vcom_port, kit_name, ip_address, ...)."""
    if not _PYC_OK:
        return {}
    try:
        res = _pyc(serial=serial, ip=ip).adapter.probe()
        if isinstance(res, dict) and res.get("success"):
            return res.get("result", {}).get("kit_info", {}) or {}
    except Exception as e:
        print(f"[WARN] pyc probe {serial or ip}: {e}")
    return {}


class VcomEndpoint:
    """Normalized, transport-agnostic VCOM endpoint.

      kind == "serial" -> use `tty`         (USB, pyserial)
      kind == "tcp"    -> use `host`,`port` (IP)
    """
    def __init__(self, serial, connectivity, kind,
                 tty=None, host=None, port=None, nickname="", label=""):
        self.serial       = str(serial)
        self.connectivity = connectivity
        self.kind         = kind
        self.tty          = tty
        self.host         = host
        self.port         = port
        self.nickname     = nickname
        self.label        = label


def _normalize_tty(path):
    """pycommander returns kit_info.vcom_port as a bare device name:
       - macOS/Linux : 'cu.usbmodem0004403304541'  -> needs '/dev/' prefix
       - Windows     : 'COM7'                       -> used as-is by pyserial
    pyserial itself adds the '\\\\.\\' prefix for COM10+ on Windows, so we never
    touch a Windows port. Absolute POSIX paths are left untouched."""
    if not path:
        return path
    if _IS_WINDOWS:
        return path                      # COMx (pyserial handles COM10+ itself)
    if path.startswith("/"):
        return path                      # already absolute (/dev/cu...)
    return f"/dev/{path}"                 # macOS/Linux bare name -> /dev/<name>


def resolve_vcom(serial):
    """Resolve a board's VCOM endpoint purely from pycommander enumeration.

    USB -> VcomEndpoint(kind="serial", tty=...) ; IP -> kind="tcp", host/port.
    Raises EndpointError if nothing usable is found.
    """
    serial   = str(serial)
    adapters = {a["serial"]: a for a in pyc_list_adapters()}
    a        = adapters.get(serial)

    # IP board -> TCP <ip>:IP_VCOM_PORT
    if a and a.get("ip"):
        return VcomEndpoint(serial, "ip", "tcp",
                            host=a["ip"], port=IP_VCOM_PORT,
                            nickname=a.get("nickname", ""))

    # USB board -> CDC tty from kit_info.vcom_port (fallback: pyserial by serial)
    kit = pyc_kit_info(serial=serial)
    tty = _normalize_tty(kit.get("vcom_port")) or _find_usb_tty(serial)
    if tty:
        return VcomEndpoint(serial, "usb", "serial", tty=tty,
                            nickname=(a or {}).get("nickname", "") or kit.get("nickname", ""),
                            label=kit.get("kit_name", ""))

    raise EndpointError(
        f"pycommander n'a pas de path VCOM pour {serial} "
        f"(tty USB introuvable, pas d'IP). Re-scanner ou rebrancher la carte."
    )


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
    clear_and_scan()
    return "OK"

@app.route("/adapters")
def adapters():
    result = []
    for a in pyc_list_adapters():          # USB + network, via pycommander
        serial = a.get("serial")
        if not serial:
            continue
        kit = _kit_cached(serial)          # `adapter probe` (cached)
        result.append({
            "serialNumber":     serial,
            "boardId":          kit.get("kit_part_number", "") or "Unknown",
            "boardLabel":       kit.get("kit_name", "") or "",
            "connectivityType": a.get("connectivity", "usb"),
            "host":             a.get("ip"),
            "label":            kit.get("kit_name", "") or "",
            "nickname":         a.get("nickname", ""),
            "ttyMode":          False,
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
        if not TERMINAL_PRETTY: return
        color_map = {"green": "script", "red": "error", "yellow": "script", "default": "default"}
        import uuid as _uuid
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        socketio.emit("terminal_line", {
            "role": "script", "display": msg, "color": color_map.get(color, "default"),
            "group_id": _uuid.uuid4().hex[:12], "room": room, "ts": ts,
            "detail": None, "cmd": None, "boot_id": None, "boot_first": False,
        }, room=room)

    def run():
        emit_out(f"Running {os.path.basename(script_path)}", "yellow")
        try:
            with open(script_path) as f:
                code = f.read()

            # connect() reuses the terminal's open channel if present, else opens
            # fresh via pycommander (USB tty / IP TCP). Single source of truth.
            board_obj = Board(serial=serial, host="", vcom_port=0,
                              run_id=None, scenario_dir="")
            try:
                board_obj.connect()
            except EndpointError as e:
                emit_out(f"✗ {e}", "red")
                return

            # Ensure the channel reader dispatches RX to this board
            active_boards[f"{serial}_vcom"] = board_obj

            # Don't send anything until the VCOM connection is open & settled
            if not board_obj.wait_vcom_ready():
                emit_out("✗ VCOM connection not ready (serial port not open)", "red")
                return

            namespace = {"board": board_obj, "time": time, "__builtins__": __builtins__}
            exec(code, namespace)
            if "script" in namespace and callable(namespace["script"]):
                namespace["script"](board_obj)
            board_obj.sync()   # let the last command's output + prompt arrive first
            emit_out("Script completed", "green")
        except Exception as e:
            import traceback
            emit_out(f"✗ {e}", "red")
            emit_out(traceback.format_exc(), "red")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})



@app.route("/api/terminal-info/<serial_number>")
def terminal_info(serial_number):
    """Resolve the VCOM endpoint from pycommander (USB tty / IP TCP:4901)."""
    if not _PYC_OK:
        return jsonify({"error": "pycommander unavailable", "serial": serial_number}), 500
    try:
        v = resolve_vcom(serial_number)
    except EndpointError as e:
        print(f"[INFO] terminal-info {serial_number}: {e}")
        return jsonify({"error": str(e), "tty_candidate": None,
                        "serial": serial_number}), 503
    except Exception as e:
        print(f"[ERROR] terminal-info {serial_number}: {e}")
        return jsonify({"error": str(e), "serial": serial_number}), 500

    if v.kind == "tcp":
        print(f"[INFO] terminal-info {serial_number}: IP host={v.host} vcom={v.port}")
        return jsonify({
            "serial":          serial_number,
            "host":            v.host,
            "vcom":            v.port,
            "tty":             None,
            "connectivity":    "ip",
            "label":           v.label or serial_number,
            "nickname":        v.nickname,
        })
    print(f"[INFO] terminal-info {serial_number}: USB tty={v.tty}")
    return jsonify({
        "serial":          serial_number,
        "host":            "127.0.0.1",
        "vcom":            None,
        "tty":             v.tty,
        "connectivity":    "usb",
        "label":           v.label or serial_number,
        "nickname":        v.nickname,
    })

# ── WebSocket telnet bridge ────────────────────────
active_telnets = {}   # room_id -> transport (serial.Serial OR socket) — the ONE handle
active_boards  = {}   # room_id -> current Board instance (updated each run)
# Serializes the "is it already open? -> open it" critical section across the
# terminal handlers (connect_tty/connect_telnet) and Board.connect(), so a USB
# CDC tty is never opened twice (which silently breaks comms on macOS).
_open_lock     = threading.Lock()
_channel_open  = {}   # room_id -> True  (EXPLICIT open flag, single source of truth)
_room_parsers  = {}   # room_id -> BaseParser (for interactive terminal pretty display)


# -----------------------------------------------------------------------------
# Channel manager — a VCOM channel is opened EXACTLY ONCE per room.
# -----------------------------------------------------------------------------
# `active_telnets[room]` holds the single transport; `_channel_open[room]` is the
# explicit flag. Every opener (connect_tty / connect_telnet / Board.connect)
# goes through acquire_channel, under _open_lock, so a second open attempt for an
# already-open room is detected and reused instead of opening a duplicate handle.
# A single reader thread per room handles display + parsing + script listeners.
# -----------------------------------------------------------------------------
def channel_is_open(room):
    return bool(_channel_open.get(room))


def _open_transport(kind, *, tty=None, host=None, port=None):
    if kind == "serial":
        return serial.Serial(_normalize_tty(tty), baudrate=115200, timeout=0)
    s = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
    s.settimeout(5)
    s.connect((host, int(port)))
    s.settimeout(None)
    return s


def acquire_channel(room, kind, *, tty=None, host=None, port=None):
    """THE single place a channel is opened. Idempotent + locked: a given room
    is opened at most once; a later/concurrent attempt reuses the live transport.
    Returns (transport, newly_opened)."""
    with _open_lock:
        if _channel_open.get(room):
            existing = active_telnets.get(room)
            print(f"[CH] {room} already open — reuse (duplicate open ignored)")
            return existing, False
        transport = _open_transport(kind, tty=tty, host=host, port=port)
        active_telnets[room] = transport
        _channel_open[room]  = True
        print(f"[CH] OPEN {room} ({kind})")
        _start_channel_reader(room, transport, kind)
        return transport, True


def close_channel(room):
    with _open_lock:
        transport = active_telnets.pop(room, None)
        _channel_open.pop(room, None)
        active_boards.pop(room, None)
    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass
    socketio.emit("terminal_closed", {"room": room}, room=room)


def _start_channel_reader(room, transport, kind):
    """One reader per channel. Always emits terminal_output + (pretty) blocks,
    dispatches to the active board's listeners, streams run_output for scenarios,
    and writes the session + raw logs. No conditional display suppression: since
    a channel is opened only once, this is the only reader for it."""
    is_vcom   = room.endswith("_vcom")
    serial_id = room.split("_")[0]
    stream    = "VCOM"

    _room_parsers[room] = get_parser(TERMINAL_PARSER)
    parse_line_buf = [""]

    session_log = None
    if is_vcom:
        session_log = get_log_file(serial_id)
        try:
            with open(session_log, "a") as f:
                f.write(f"# Session started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Board: {serial_id}  Room: {room}\n")
        except Exception:
            pass
        _room_parsers[f"{serial_id}_session_log"] = session_log

    import os as _os
    raw_log_path = _os.path.join(
        "logs",
        f"{serial_id}_raw_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{stream.lower()}.log")
    try:
        _os.makedirs("logs", exist_ok=True)
        with open(raw_log_path, "w") as _rf:
            _rf.write(f"# room={room} kind={kind}\n")
    except Exception:
        raw_log_path = None

    def reader():
        print(f"[CH] reader started: {room}")
        buf = []
        room_parser = _room_parsers.get(room)
        while channel_is_open(room):
            try:
                chunk = _t_recv(transport)
                if not chunk:
                    if _is_serial_transport(transport):
                        time.sleep(0.005)
                        continue
                    break  # socket closed
                text = chunk.decode("utf-8", errors="replace")

                if raw_log_path:
                    try:
                        ts_raw = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        with open(raw_log_path, "a") as _rf:
                            printable = text.replace("\r", "\\r").replace("\n", "\\n")
                            _rf.write(f"[{ts_raw}][RX] {chunk.hex()}  |  {printable}\n")
                    except Exception:
                        pass

                # Always display in the terminal
                socketio.emit("terminal_output", {"data": text, "room": room}, room=room)

                # Pretty blocks (Modern View)
                if room_parser and TERMINAL_PRETTY:
                    try:
                        for block in _extract_blocks(text, parse_line_buf, room_parser,
                                                     session_log=session_log, stream=stream):
                            socketio.emit("terminal_block",
                                          {**format_block(block), "room": room}, room=room)
                    except Exception as _pe:
                        print(f"[PARSER] {room}: {_pe}")

                # Script listeners + scenario run streaming
                board = active_boards.get(room)
                if board:
                    buf.append(text)
                    combined  = "".join(buf)
                    prompt    = board._vcom_prompt
                    listeners = board._vcom_listeners
                    for listener in list(listeners):
                        try: listener(combined)
                        except Exception: pass
                    if getattr(board, "run_id", None):
                        socketio.emit("run_output",
                                      {"serial": board.serial, "data": text,
                                       "stream": stream.lower()},
                                      room=f"run_{board.run_id}")
                    if prompt in combined:
                        buf.clear()
            except Exception as e:
                print(f"[CH] reader error {room}: {e}")
                break

        # ── teardown ──
        if session_log:
            try:
                if parse_line_buf[0].strip():
                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    with open(session_log, "a") as f:
                        f.write(f"[{ts}][{stream}] {parse_line_buf[0].strip()}\n")
                with open(session_log, "a") as f:
                    f.write(f"# Session ended: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            except Exception:
                pass
        if room_parser and TERMINAL_PRETTY and hasattr(room_parser, "flush"):
            try:
                for block in (room_parser.flush() or []):
                    socketio.emit("terminal_block",
                                  {**format_block(block), "room": room}, room=room)
            except Exception:
                pass
        close_channel(room)

    threading.Thread(target=reader, daemon=True).start()



# Commands whose response is accumulated verbatim by the parser itself (railtest.py)
_PASSTHROUGH_CMDS = {"help"}  # kept for reference, handled inside RailtestParser

# A bare prompt that ends a reply ("> ", "WSTK> ") — often sent without a newline
_PROMPT_TAIL_RE = re.compile(r'^\w*>\s*$')


def _extract_blocks(text: str, line_buf: list, parser,
                    session_log=None, stream="VCOM") -> list:
    """
    Accumulate TCP fragments into complete lines, feed each to the parser.
    Returns list of complete block dicts (type='cmd' or 'event').

    line_buf[0] : incomplete trailing fragment (persistent across calls)
    """
    combined = line_buf[0] + text
    combined = combined.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    parts       = combined.split("\n")
    line_buf[0] = parts[-1]
    complete    = parts[:-1]

    # A RAILtest prompt ("> ", "WSTK> ") usually arrives WITHOUT a trailing newline,
    # so it would otherwise sit in line_buf until the next command flushes it — which
    # is why a reply used to appear only when the following command was sent. If the
    # leftover fragment is a bare prompt, treat it as a complete line right now.
    tail = line_buf[0].strip()
    if tail and _PROMPT_TAIL_RE.match(tail):
        complete.append(line_buf[0])
        line_buf[0] = ""

    blocks    = []
    log_lines = []
    ts_now    = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

    for raw_line in complete:
        line = raw_line.strip()
        if not line:
            continue
        if session_log:
            log_lines.append(line)
        try:
            blocks.extend(parser.feed(line, ts=ts_now))
        except Exception as _pe:
            print(f"[PARSER] error on {line!r}: {_pe}")

    if session_log and log_lines:
        with open(session_log, "a") as f:
            for ln in log_lines:
                f.write(f"[{ts_now}][{stream}] {ln}\n")

    return blocks


@socketio.on("connect_telnet")
def handle_connect_telnet(data):
    """IP board VCOM over TCP. Single open via the channel manager."""
    room = data["room"]            # e.g. "440114849_vcom"
    host = data["host"]
    port = int(data["port"])
    join_room(room)
    try:
        acquire_channel(room, "tcp", host=host, port=port)
        emit("terminal_ready", {"room": room})
    except Exception as e:
        print(f"[CH] connect_telnet failed {room}: {e}")
        emit("terminal_error", {"message": str(e)})

@socketio.on("disconnect_telnet")
def handle_disconnect_telnet(data):
    close_channel(data.get("room"))

@socketio.on("connect_tty")
def handle_connect_tty(data):
    """USB board VCOM over the raw CDC tty (pyserial). Single open via the
    channel manager — never opens the same tty twice."""
    room = data["room"]
    tty  = _normalize_tty(data["tty"])
    join_room(room)
    try:
        acquire_channel(room, "serial", tty=tty)
        emit("terminal_ready", {"room": room})
    except Exception as e:
        print(f"[CH] connect_tty failed {room}: {e}")
        emit("terminal_error", {"message": str(e)})

# Per-session input state for the terminal channel
_input_buf = {}  # key -> current (unterminated) line being typed


def _tin_ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _tin_send(tn, s):
    """Send a string to a TCP socket or a pyserial port (transport-agnostic)."""
    b = s.encode("utf-8")
    if hasattr(tn, "sendall"):
        tn.sendall(b)
    else:
        tn.write(b)


def _tin_log(serial_id, stream, cmd):
    log_path = _room_parsers.get(f"{serial_id}_session_log")
    if log_path and cmd:
        try:
            with open(log_path, "a") as f:
                f.write(f"[{_tin_ts()}][{stream}][CMD] {cmd}\n")
        except Exception:
            pass


@socketio.on("terminal_input")
def handle_terminal_input(data):
    """VCOM input from xterm (single keystrokes) or the input bar (whole lines).
    The command is held until the line is complete, then sent in one shot to
    avoid the echo/response race; notify_cmd(echo=True) lets the parser
    recognise the board's echo and open the command block."""
    room = data.get("room")
    tn   = active_telnets.get(room)
    if not tn:
        return
    try:
        serial_id = room.split("_")[0]
        key       = f"{room}_{request.sid}"
        raw       = data.get("data", "")
        rp        = _room_parsers.get(room)

        # Backspace / delete — edit the buffer
        if raw in ("\x7f", "\x08"):
            _input_buf[key] = _input_buf.get(key, "")[:-1]
            return

        # Accumulate and split into complete lines
        buf   = _input_buf.get(key, "") + raw
        norm  = buf.replace("\r\n", "\n").replace("\r", "\n")
        parts = norm.split("\n")
        _input_buf[key] = parts[-1]          # keep the unterminated tail
        complete = parts[:-1]

        for line in complete:
            cmd = line.strip()
            if not cmd:
                continue
            if rp and TERMINAL_PRETTY and hasattr(rp, "notify_cmd"):
                rp.notify_cmd(cmd, ts=_tin_ts(), echo=True)
            _tin_send(tn, cmd + "\r\n")
            _tin_log(serial_id, "VCOM", cmd)
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
def adapter_erase(serial):
    """Mass erase a single board from Manual Control."""
    try:
        conn_flag, _ = _conn_flag(serial)
        cmd = [COMMANDER_PATH, "device", "masserase"] + conn_flag
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        invalidate_connections(serial)
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
        conn_flag, _ = _conn_flag(serial)
        cmd = [COMMANDER_PATH, "flash", path] + conn_flag
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        invalidate_connections(serial)
        ok = result.returncode == 0
        return jsonify({"ok": ok, "msg": result.stderr.strip() if not ok else ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/adapter/<serial>/reset-mcu", methods=["POST"])
def adapter_reset_mcu(serial):
    """Reset the target MCU via pycommander. Works on every adapter, including
    USB ones with no admin console."""
    ok, msg = reset_mcu(serial)
    return jsonify({"ok": ok, "msg": msg})


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

    # Load available adapters (pycommander)
    try:
        adapters_data = _adapters_compat()
    except Exception as e:
        return jsonify({"ok": False, "issues": [{"line": None, "msg": f"Cannot enumerate adapters: {e}"}]}), 500

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
        # Note: board-ID validation against the SDM board database was removed
        # with SDM. Board correctness is now confirmed at flash time by Commander.

        # ── connection (optional, replaces jlink_name_or_ip) ──
        # Accepts both old key (jlink_name_or_ip) and new key (connection)
        connection = str(board.get("connection", board.get("jlink_name_or_ip", "usb"))).strip()
        if connection.lower() in ("none", ""):
            connection = "usb"
        matched_adapter = None
        if _is_loopback_host(connection):
            issue("connection", "127.0.0.1/localhost is not an adapter identity. Use 'usb' or the adapter serial number.")

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
          - str   → serial number, scenario nickname, or adapter nickname
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


# ── Transport helpers ────────────────────────────────────────
# A VCOM channel is either a TCP socket (IP boards) or a pyserial
# Serial object (USB CDC). These wrappers let Board treat both identically.
def _is_serial_transport(t):
    return isinstance(t, serial.Serial)

def _t_send(t, data):
    if _is_serial_transport(t):
        t.write(data)
    else:
        t.sendall(data)

def _t_recv(t, n=4096):
    # socket.recv returns b"" only on close; serial.read returns b"" on timeout.
    if _is_serial_transport(t):
        return t.read(n) or b""
    return t.recv(n)

def _t_settimeout(t, timeout):
    if _is_serial_transport(t):
        t.timeout = timeout     # None = blocking, 0 = non-blocking
    else:
        t.settimeout(timeout)


# ── Live plot driver (Plotly "Graphe" view of a board terminal) ──────────
class Plot:
    """Drives the Plotly 'Graphe' view of a board's terminal window.

    All display logic lives in the scenario script; the browser only renders
    (Plotly.react / Plotly.extendTraces). Events are emitted to the board's
    VCOM room — the room the terminal window already joined — so a plain
    `board.plot.*` call reaches the right window, with no per-tab wiring.

    Usage (board script `script(board)` or global `scenario.board(0).plot`):
        board.plot.show(fig)        # full figure: dict OR a plotly go.Figure
        board.plot.extend(ys, x=..., traces=..., maxpoints=...)  # append points
        board.plot.clear()

    `fig` is a raw spec {'data': [...], 'layout': {...}, 'config': {...}}
    (no Python dependency) or a plotly.graph_objects.Figure (auto-serialised).
    Anything pushed must be JSON-serialisable (same rule as socketio.emit).
    """

    def __init__(self, serial):
        self._room = f"{serial}_vcom"

    def show(self, fig):
        spec = json.loads(fig.to_json()) if hasattr(fig, "to_json") else fig
        socketio.emit("plot_figure", spec, room=self._room)
        socketio.sleep(0)

    def extend(self, ys, x=None, traces=None, maxpoints=None):
        if traces is None:
            traces = list(range(len(ys)))
        socketio.emit("plot_extend",
                      {"ys": ys, "x": x, "traces": traces, "maxpoints": maxpoints},
                      room=self._room)
        socketio.sleep(0)

    def clear(self):
        socketio.emit("plot_clear", {}, room=self._room)
        socketio.sleep(0)


class Board:
    """
    Wraps the VCOM connection (pyserial tty for USB / TCP:4901 for IP) for one board.
    Connected before script runs, never closed automatically.
    """

    def __init__(self, serial, host, vcom_port,
                 run_id, scenario_dir, open_terminal=False, nickname=None):
        self.serial        = serial
        self.nickname      = nickname  # scenario nickname (overrides adapter nickname)
        self.host          = host
        self.vcom_port     = vcom_port
        self.run_id        = run_id
        self.scenario_dir  = scenario_dir
        self.open_terminal_flag = open_terminal

        # Socket (set in connect())
        self._vcom_sock  = None

        # Config (set by script via config_vcom)
        self._vcom_le     = b"\r\n"
        self._vcom_echo   = True
        self._vcom_prompt = ">"

        self._vcom_listeners  = []
        self._vcom_expecting_echo = [False]  # ← default, always valid
        self._log_path = None
        self._scenario_ctx = None  # set by Scenario when global script is used
        self.parser = get_parser(TERMINAL_PARSER)  # protocol parser (auto/railtest/generic)
        self.plot = Plot(serial)  # live Plotly view of this board's terminal window

    # ── configuration ────────────────────────────────────────
    def config_vcom(self, line_ending="CRLF", echo=True, prompt=">"):
        self._vcom_le     = LINE_ENDINGS.get(line_ending.upper(), b"\r\n")
        self._vcom_echo   = echo
        self._vcom_prompt = prompt

    # ── connect ──────────────────────────────────────────────
    def connect(self):
        """Acquire the VCOM channel (single open, shared with the terminal).
        Transport is resolved from pycommander: USB -> CDC tty, IP -> TCP:4901."""
        vcom_room = f"{self.serial}_vcom"

        # Register as dispatcher BEFORE acquiring so the reader routes RX to this
        # Board's listeners as soon as it starts.
        active_boards[vcom_room] = self

        if channel_is_open(vcom_room):
            self._vcom_sock = active_telnets.get(vcom_room)
            print(f"[RUN] reusing VCOM channel for {vcom_room}")
        else:
            ep = resolve_vcom(self.serial)
            if ep.kind == "serial":
                self.host, self.vcom_port = "127.0.0.1", 0
                tx, _ = acquire_channel(vcom_room, "serial", tty=ep.tty)
            else:
                self.host, self.vcom_port = ep.host, ep.port
                tx, _ = acquire_channel(vcom_room, "tcp", host=ep.host, port=ep.port)
            self._vcom_sock = tx
        socketio.emit("terminal_ready", {"room": vcom_room}, room=vcom_room)

        if self.open_terminal_flag:
            def _open():
                url = f"http://127.0.0.1:{WEB_PORT}/terminal/{self.serial}?from_run=1"
                webview.create_window(
                    f"{self.serial} — Terminal", url,
                    width=820, height=620, resizable=True)
            threading.Thread(target=_open, daemon=True).start()

    def write_cli(self, data):
        if self._vcom_sock:
            if isinstance(data, str):
                data = data.encode("utf-8")
            _t_send(self._vcom_sock, data)
            vcom_room = f"{self.serial}_vcom"
            cmd_str = data.decode("utf-8", errors="replace").rstrip("\r\n")
            socketio.emit("terminal_echo",
                          {"data": cmd_str, "room": vcom_room},
                          room=vcom_room)
            socketio.emit("terminal_cmd",
                          {"data": cmd_str, "room": vcom_room},
                          room=vcom_room)

    def read_cli(self, timeout=2.0):
        """Read raw data from VCOM with timeout."""
        if not self._vcom_sock:
            return ""
        _t_settimeout(self._vcom_sock, timeout)
        data = b""
        try:
            while True:
                chunk = _t_recv(self._vcom_sock)
                if not chunk:
                    break
                data += chunk
        except Exception:
            pass
        _t_settimeout(self._vcom_sock, None)
        return data.decode("utf-8", errors="replace")

    def cli(self, cmd, timeout=10.0):
        if not self._vcom_sock:
            return ""
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[CLI] {self.nickname or self.serial} {ts} >>> {cmd}")

        event    = threading.Event()
        response = []
        prompt   = self._vcom_prompt

        def on_data(combined):
            lines = combined.splitlines()
            if self.parser.is_response_complete(lines):
                response.append(combined); event.set(); return
            if prompt in combined:
                response.append(combined); event.set()

        self._vcom_listeners.append(on_data)
        payload = cmd.encode("utf-8") + self._vcom_le
        # Notify parser that this command is coming from TX
        vcom_room = f"{self.serial}_vcom"
        room_parser = _room_parsers.get(vcom_room)
        if room_parser and hasattr(room_parser, 'notify_cmd'):
            room_parser.notify_cmd(cmd)
        _t_send(self._vcom_sock, payload)

        event.wait(timeout=timeout)
        self._vcom_listeners.remove(on_data)
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        timed_out = not event.is_set()
        print(f"[CLI] {self.nickname or self.serial} {ts} <<< {'TIMEOUT' if timed_out else 'ok'}")
        raw = "".join(response)
        # Strip command echo (first line) and trailing prompt line
        lines = raw.splitlines()
        # Remove echo line (starts with the command)
        if lines and cmd.strip() in lines[0]:
            lines = lines[1:]
        # Remove trailing prompt line
        prompt = self._vcom_prompt
        while lines and lines[-1].strip() in (prompt, ""):
            lines.pop()
        return "\n".join(lines)

    def read(self, timeout=1.0, pattern=None):
        """Passively capture spontaneous VCOM output (e.g. RAILtest async events
        such as received-packet lines) for up to `timeout` seconds, WITHOUT
        sending any command. If `pattern` (a regex string) is given, returns as
        soon as it appears. Returns the captured text.

        Uses the same listener mechanism as cli(), so it never reads the socket
        directly (the single channel reader stays the sole reader)."""
        if not self._vcom_sock:
            return ""
        import re as _re
        pat    = _re.compile(pattern) if pattern else None
        latest = [""]
        event  = threading.Event()

        def on_data(combined):
            latest[0] = combined
            if pat and pat.search(combined):
                event.set()

        self._vcom_listeners.append(on_data)
        try:
            event.wait(timeout=timeout)
        finally:
            try:
                self._vcom_listeners.remove(on_data)
            except ValueError:
                pass
        return latest[0]

    def reset(self, settle=1.0):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[RST] {self.nickname or self.serial} {ts}")
        # Reset the MCU through pycommander (device reset) — independent of the
        # admin console, so it actually resets on USB adapters too. The board's
        # boot banner then appears on VCOM; a script can wait for it if needed.
        ok, msg = reset_mcu(self.serial)
        if not ok:
            print(f"[RST] {self.serial} reset failed: {msg}")
        time.sleep(settle)
        return ""

    def flash(self, firmware_path: str, serial: str = None, ip: str = None,
              masserase: bool = True, halt_reset: bool = False) -> bool:
        """
        Flash a firmware file onto the board using Simplicity Commander.

        Connection is auto-detected from board.host:
          - host is 127.0.0.1/localhost → USB, uses board serial  (-s <serial>)
          - host is a remote IP         → JLink over IP            (--ip <host>)

        Override with:
            serial="XXXXXXX"   → force -s <serial>
            ip="192.168.1.x"   → force --ip <ip>

        Args:
            firmware_path : path to .hex / .s37 file
            serial        : force a specific serial number (optional)
            ip            : force a specific JLink IP (optional)
            masserase     : mass erase before flash (default True)
            halt_reset    : keep CPU halted after flash (default False)

        Returns:
            True on success, False on failure

        Examples:
            board.flash("/fw/app.hex")
            board.flash("/fw/app.hex", serial="440012345")
            board.flash("/fw/app.hex", ip="192.168.1.100")
            board.flash("/fw/app.hex", masserase=False)
        """
        import os as _os
        firmware_path = _os.path.abspath(firmware_path)
        if not _os.path.isfile(firmware_path):
            print(f"[FLASH] ERROR: file not found: {firmware_path}")
            return False

        # Resolve connection flag
        if serial:
            conn_flag = ["-s", serial]
        elif ip:
            conn_flag = ["--ip", ip]
        elif self.host in ("127.0.0.1", "localhost", "::1"):
            conn_flag = ["-s", self.serial]
        else:
            conn_flag = ["--ip", self.host]

        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[FLASH] {self.nickname or self.serial} {ts} {firmware_path} conn={conn_flag}")

        vcom_room = f"{self.serial}_vcom"

        def _emit(msg):
            print(f"[FLASH] {msg}")
            socketio.emit("terminal_line", {
                "role": "script", "display": msg, "color": "script",
                "group_id": __import__("uuid").uuid4().hex[:12],
                "room": vcom_room, "ts": datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "detail": None, "cmd": None, "boot_id": None, "boot_first": False,
            }, room=vcom_room)

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
    def _read_until(self, sock, prompt, timeout):
        """Read from sock until prompt is found or timeout expires.
        Uses a deadline loop so partial chunks are accumulated correctly."""
        if not sock:
            return ""
        deadline = time.monotonic() + timeout
        data = b""
        prompt_b = prompt.encode("utf-8")
        _t_settimeout(sock, 0.1)
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = _t_recv(sock)
                    if not chunk:
                        if _is_serial_transport(sock):
                            time.sleep(0.02)
                            continue
                        break
                    data += chunk
                    if prompt_b in data:
                        break
                except OSError:
                    pass  # timeout on this recv slice, keep looping
        finally:
            _t_settimeout(sock, None)
        return data.decode("utf-8", errors="replace")

    def delay(self, seconds):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[DLY] {self.nickname or self.serial} {ts} {seconds}s")
        time.sleep(seconds)

    def wait_vcom_ready(self, timeout=5.0, settle=0.2):
        """Ensure the VCOM transport is open AND the channel has settled before
        the script sends its first command. Avoids writing to a serial port that
        has just opened (CDC line settling / board still booting) — which is why
        early commands were getting lost."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self._vcom_sock
            open_ok = t is not None and (not _is_serial_transport(t)
                                         or getattr(t, "is_open", False))
            if open_ok:
                # transport is open; let any open/boot noise settle before TX
                self.sync(idle=settle, timeout=max(0.0, deadline - time.monotonic()))
                return True
            time.sleep(0.05)
        return False

    def sync(self, idle=0.3, timeout=3.0):
        """Block until the VCOM channel has been quiet for `idle` seconds, i.e.
        the last command's response + prompt have finished arriving. Used to
        order the end-of-script banner *after* the final output instead of
        racing it — same idea as waiting for a command's prompt."""
        if not self._vcom_sock:
            return
        last = [time.monotonic()]
        def on_data(_combined):
            last[0] = time.monotonic()
        self._vcom_listeners.append(on_data)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if time.monotonic() - last[0] >= idle:
                    break
                time.sleep(0.05)
        finally:
            try:
                self._vcom_listeners.remove(on_data)
            except ValueError:
                pass

    def checkpoint(self, name):
        """Signal to the global scenario script that this board has reached a named point."""
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
            invalidate_connections(serial)
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


def _run_board(board_cfg, scenario_dir, run_id, script_code):
    serial   = board_cfg.get("_resolved_serial", "unknown")
    nickname = board_cfg.get("_nickname") or None
    print(f"[RUN] _run_board started serial={serial} nickname={nickname}")
    run_room = f"run_{run_id}"
    log_path = get_log_file(serial)
    time.sleep(2.0)

    # Send nickname to UI so log prefix and card name show it
    if nickname:
        socketio.emit("run_board_nickname",
                      {"serial": serial, "nickname": nickname},
                      room=run_room)

    def emit_status(status, msg=""):
        active_runs[run_id]["boards"][serial] = status
        print(f"[RUN] emit_status {serial} → {status} to room run_{run_id}")
        socketio.emit("run_board_status",
                      {"serial": serial, "status": status, "msg": msg},
                      room=run_room)

    def log(stream, text):
        """Write to log file and emit to run panel."""
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(log_path, "a") as f:
            for line in text.splitlines():
                f.write(f"[{ts}][{stream}] {line}\n")

    emit_status("flashing", f"log: {os.path.basename(log_path)}")
    # Write log header
    with open(log_path, "w") as f:
        f.write(f"# Run: {run_id}\n")
        f.write(f"# Serial: {serial}\n")
        f.write(f"# Scenario: {scenario_dir}\n")
        f.write(f"# Started: {datetime.datetime.now().isoformat()}\n")
        f.write(f"# {'='*60}\n\n")
    # Flash
    ok, flash_log = _flash_board(board_cfg, scenario_dir, run_id)
    for line in flash_log:
        log("flash", line)
        socketio.emit("run_output",
                      {"serial": serial, "data": line + "\n", "stream": "flash"},
                      room=run_room)
    if not ok:
        log("error", "Flash failed")
        emit_status("error", "Flash failed")
        return

    # Connect — Board.connect() resolves the VCOM endpoint from pycommander
    # (USB tty / IP TCP) and reuses an open terminal channel if present.
    emit_status("connecting")
    board = Board(
        serial        = serial,
        host          = "",
        vcom_port     = 0,
        run_id        = run_id,
        scenario_dir  = scenario_dir,
        open_terminal = board_cfg.get("open_terminal", False),
        nickname      = board_cfg.get("_nickname"),
    )
    board._log_path = log_path
    try:
        print(f"[RUN] connecting board {serial}")
        board.connect()
        print(f"[RUN] board connected {serial}")
    except Exception as e:
        import traceback
        print(f"[RUN] connect exception: {traceback.format_exc()}")
        emit_status("error", f"Connection failed: {e}")
        return

    # Run script
    emit_status("running")
    if script_code:
        try:
            _exec_board_script(board, script_code, run_id)
            emit_status("done")
        except Exception as e:
            emit_status("error", str(e))
    else:
        emit_status("done")


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

    # Fetch available adapters (pycommander)
    try:
        adapters_data = _adapters_compat()
        adapter_by_host   = {a.get("host"): a for a in adapters_data if a.get("host") and not _is_loopback_host(a.get("host"))}
        adapter_by_serial = {a.get("serialNumber"): a for a in adapters_data if a.get("serialNumber")}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Cannot enumerate adapters: {e}"}), 500

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
            # 127.0.0.1 means the board is local (USB) — keep connection=usb
            if adapter:
                adapter_host = adapter.get("host", "").strip()
                if adapter_host and adapter_host != "127.0.0.1":
                    connection = adapter_host

        if not adapter:
            return jsonify({"ok": False, "error": f"No adapter found for board entry {b.get('board', '?')}"}), 400

        bc = dict(b)
        bc["_resolved_serial"] = adapter.get("serialNumber")
        bc["connection"]       = connection

        # Nickname resolution: YAML > adapter > None
        yaml_nick     = str(b.get("nickname", "")).strip() or None
        adapter_nick  = adapter.get("nickname") or None
        bc["_nickname"] = yaml_nick or adapter_nick

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
                        host          = "", vcom_port = 0,
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
                        host          = "", vcom_port = 0,
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