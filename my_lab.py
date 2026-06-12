import subprocess
import time
import requests
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
_SDM_NAME       = "sdm.exe"       if _IS_WINDOWS else "sdm"
_COMMANDER_NAME = "commander.exe" if _IS_WINDOWS else "commander"

# Chemins fallback par plateforme (utilisés uniquement si auto-détection échoue)
if _IS_WINDOWS:
    _DEFAULT_SDM       = str(Path.home() / "AppData/Local/silabs/slt/installs/archive/sdm-win32-x64/sdm.exe")
    _DEFAULT_COMMANDER = str(Path.home() / "AppData/Local/silabs/Commander/commander.exe")
elif _IS_MAC:
    _DEFAULT_SDM       = str(Path.home() / ".silabs/slt/installs/archive/sdm-darwin-arm64/sdm")
    _DEFAULT_COMMANDER = str(Path.home() / ".silabs/slt/installs/archive/Commander.app/Contents/MacOS/commander")
else:  # Linux
    _DEFAULT_SDM       = str(Path.home() / ".silabs/slt/installs/archive/sdm-linux-x64/sdm")
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

def resolve_sdm(path: str | None) -> str | None:
    if path and _is_exec(path):
        return path
    envp = os.environ.get("SDM_BIN")
    if envp and _is_exec(envp):
        return envp
    if _is_exec(_DEFAULT_SDM):
        return _DEFAULT_SDM
    p = shutil.which("sdm")
    if p and _is_exec(p):
        return p
    return _find_in_silabs(_SDM_NAME)

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

SDM_PATH = resolve_sdm(config["paths"].get("sdm"))
COMMANDER_PATH = resolve_commander(config["paths"].get("commander"))

# On ne sauvegarde que si un chemin était absent du config et vient d'être auto-détecté
_save_needed = False
if SDM_PATH and not config["paths"].get("sdm"):
    config["paths"]["sdm"] = SDM_PATH
    _save_needed = True
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
from parsers import get_parser
TERMINAL_PARSER  = config["server"].get("parser",           "auto").strip().lower()
TERMINAL_PRETTY  = config["server"].get("terminal_pretty",  "true").strip().lower() == "true"

SDM_BASE = f"http://{HOST}:{PORT}"

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


# ----------------------------
# SDM
# ----------------------------
def is_sdm_running():
    try:
        return requests.post(f"{SDM_BASE}/api/adapter/clear-and-scan", timeout=2).status_code == 200
    except requests.RequestException:
        return False

def start_sdm():
    subprocess.Popen([SDM_PATH, "server", "start"])

def stop_sdm():
    try:
        subprocess.run([SDM_PATH, "server", "stop"], timeout=5)
        time.sleep(1)
    except Exception as e:
        print(f"[INFO] stop_sdm: {e}")

def restart_sdm():
    """Stop then start SDM to kill any lingering Silink instances."""
    print("[INFO] Restarting SDM to clean up Silink instances...")
    if is_sdm_running():
        stop_sdm()
    start_sdm()
    for _ in range(10):
        time.sleep(1)
        if is_sdm_running():
            print("[INFO] SDM ready.")
            return
    print("[WARN] SDM did not start in time.")

def clear_and_scan():
    requests.post(f"{SDM_BASE}/api/adapter/clear-and-scan")

def get_adapters():
    try:
        return requests.get(f"{SDM_BASE}/api/adapters").json()
    except requests.RequestException as e:
        print(f"[WARN] get_adapters failed: {e}")
        return []

def extract_board_label(a):
    boards = a.get("boards") or []
    return boards[0].get("label", "") if boards else ""

def extract_board(a):
    boards = a.get("boards") or []
    return boards[0].get("id", "Unknown") if boards else "Unknown"

# ----------------------------
# COMMANDER ACTIONS
# ----------------------------

def run_commander(cmd, serial):
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return {"serialNumber": serial, "ok": True, "output": result.stdout}
    except subprocess.CalledProcessError as e:
        return {"serialNumber": serial, "ok": False, "error": e.stderr or str(e)}
    except Exception as e:
        return {"serialNumber": serial, "ok": False, "error": str(e)}

@app.route("/sdm/ui", methods=["POST"])
def sdm_ui():
    try:
        subprocess.Popen([SDM_PATH, "ui", "start"])
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[ERROR] sdm ui start: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

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

# ─────────────────────────────────────────────────────────────────────────────
# Helper: ensure Silink is started for USB adapters and return connections
# ─────────────────────────────────────────────────────────────────────────────
# Helper: ensure Silink is started for USB adapters and return connections.
# list-connections is called AT MOST ONCE per serial per app session — further
# calls use get-info so no extra Silink instances are spawned.
# ─────────────────────────────────────────────────────────────────────────────
_silink_started: set = set()   # serials for which list-connections was called

def get_connections(serial_number: str, timeout: int = 8) -> dict:
    """
    Call list-connections the first time (starts Silink for USB boards), then
    use get-info for subsequent calls so only one Silink instance is spawned.
    list-connections may return either a dict {adapter, connections} or a bare
    list of connections — normalize to dict in both cases.
    """
    if serial_number not in _silink_started:
        url = f"{SDM_BASE}/api/adapter/{serial_number}/list-connections"
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            _silink_started.add(serial_number)
            print(f"[INFO] list-connections {serial_number}: Silink started")
            # Normalize: some SDM versions return a bare list of connections
            if isinstance(data, list):
                # Fetch adapter info separately to get host, connectivityType etc.
                try:
                    info = requests.get(f"{SDM_BASE}/api/adapter/{serial_number}/get-info", timeout=5).json()
                    data = {"adapter": info.get("adapter", {}), "connections": data}
                except Exception:
                    data = {"adapter": {}, "connections": data}
            return data
        except Exception as e:
            print(f"[WARN] list-connections {serial_number}: {e} — falling back to get-info")

    # Already started (or list-connections failed) → just read current state
    r = requests.get(f"{SDM_BASE}/api/adapter/{serial_number}/get-info", timeout=5)
    r.raise_for_status()
    return r.json()

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
    data = get_adapters()
    if isinstance(data, dict):
        data = data.get("adapters") or []

    result = []
    for a in data:
        serial = a.get("serialNumber")
        if not serial:
            continue

        # Check ttyMode via get-info connections array
        tty_mode = True
        try:
            info    = get_connections(serial, timeout=8)
            conns   = info.get("connections", [])
            ports   = {c["portName"]: c["portNumber"] for c in conns}
            tty_mode = not ports.get("serial1")
            print(f"[INFO] get-info {ports}")
        except Exception as e:
            print(f"[WARN] get-info {serial}: {e}")
            tty_mode = False  # safe fallback — show buttons

        result.append({
            "serialNumber":     serial,
            "boardId":          extract_board(a),
            "boardLabel":       extract_board_label(a),
            "connectivityType": a.get("connectivityType"),
            "host":             a.get("host"),
            "label":            a.get("label", ""),
            "nickname":         a.get("nickname", ""),
            "ttyMode":          tty_mode,
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

            # Reuse existing telnet sockets from the terminal window
            vcom_sock  = active_telnets.get(f"{serial}_vcom")
            admin_sock = active_telnets.get(f"{serial}_admin")

            if not vcom_sock or not admin_sock:
                # Sockets not yet open — connect fresh
                info         = get_connections(serial)
                adapter      = info.get("adapter", {})
                conns        = info.get("connections", [])
                ports        = {c["portName"]: c["portNumber"] for c in conns}
                connectivity = adapter.get("connectivityType", "usb")
                host         = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"
                vcom_port    = ports.get("serial1")
                admin_port   = ports.get("admin")
                if not vcom_port or not admin_port:
                    emit_out("✗ Could not get board ports from SDM", "red")
                    return
                board_obj = Board(
                    serial       = serial,
                    host         = host,
                    vcom_port    = vcom_port,
                    admin_port   = admin_port,
                    run_id       = None,
                    scenario_dir = "",
                )
                board_obj.connect()
            else:
                # Inject existing sockets — no new connections needed
                board_obj = Board(
                    serial       = serial,
                    host         = "127.0.0.1",  # not used when sockets provided
                    vcom_port    = 0,
                    admin_port   = 0,
                    run_id       = None,
                    scenario_dir = "",
                )
                board_obj._vcom_sock  = vcom_sock
                board_obj._admin_sock = admin_sock

            # Register so _start_reader dispatches to this board
            active_boards[f"{serial}_vcom"]  = board_obj
            active_boards[f"{serial}_admin"] = board_obj

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


@app.route("/api/terminal-info/<serial>")
def terminal_info(serial):
    try:
        data       = get_connections(serial)
        adapter    = data.get("adapter", {})

        connectivity = adapter.get("connectivityType", "usb")
        host = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"

        conns_list = data.get("connections", [])
        ports      = {c["portName"]: c["portNumber"] for c in conns_list}

        vcom_port  = ports.get("serial1")
        admin_port = ports.get("admin")
        print(f"[INFO] terminal-info {serial}: vcom={vcom_port} admin={admin_port}")

        # Si pas de ports réseau → chercher port série USB (cross-platform)
        tty_path = None
        if not vcom_port and connectivity == "usb":
            for port in serial.tools.list_ports.comports():
                if serial.lower() in (port.serial_number or "").lower():
                    tty_path = port.device
                    break

        return jsonify({
            "serial":       serial,
            "host":         host,
            "vcom":         vcom_port,
            "admin":        admin_port,
            "tty":          tty_path,
            "connectivity": connectivity,
            "label":        adapter.get("label", serial),
            "nickname":     adapter.get("nickname", ""),
        })
    except Exception as e:
        print(f"[ERROR] terminal-info {serial}: {e}")
        return jsonify({"error": str(e)}), 500

# ── WebSocket telnet bridge ────────────────────────
active_telnets = {}   # room_id -> socket
active_boards  = {}   # room_id -> current Board instance (updated each run)
_room_parsers  = {}   # room_id -> BaseParser (for interactive terminal pretty display)

def _extract_lines(text: str, line_buf: list, parser,
                   session_log=None, stream="VCOM") -> list:
    """
    Accumulate TCP fragments into complete lines.
    Feed each line to the parser → ParsedLine dicts.
    Write complete lines to session_log if provided.
    Returns list of ParsedLine dicts.
    """
    combined    = line_buf[0] + text
    combined    = combined.replace("\r\n", "\n").replace("\r", "\n")
    parts       = combined.split("\n")
    line_buf[0] = parts[-1]
    complete    = parts[:-1]

    parsed_lines = []
    log_lines    = []

    for raw_line in complete:
        line = raw_line.strip()
        if not line:
            continue
        if session_log:
            log_lines.append(line)
        # Strip prompt prefix artifact before a {{ line ("> {{(appMode)...")
        parse_line = re.sub(r'^>\s+(?=\{\{)', '', line)
        try:
            parsed_lines.extend(parser.feed(parse_line))
        except Exception as _pe:
            print(f"[PARSER] error on {line!r}: {_pe}")

    if session_log and log_lines:
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(session_log, "a") as f:
            for line in log_lines:
                f.write(f"[{ts}][{stream}] {line}\n")

    return parsed_lines


@socketio.on("connect_telnet")
def handle_connect_telnet(data):
    room    = data["room"]    # e.g. "440114849_vcom"
    host    = data["host"]
    port    = int(data["port"])
    print(f"[TELNET] connect request: room={room} host={host} port={port}")
    join_room(room)

    if room in active_telnets:
        print(f"[TELNET] already connected: {room}")
        emit("terminal_ready", {"room": room})
        return  # déjà connecté

    try:
        tn = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
        tn.settimeout(5)
        tn.connect((host, port))
        tn.settimeout(None)
        print(f"[TELNET] connected: {room}")

        active_telnets[room] = tn

        # Send a CRLF on VCOM connection — board replies with prompt,
        # which primes the AutoParser to detect RAILtest before first manual command
        if room.endswith("_vcom"):
            try:
                tn.sendall(b"\r\n")
            except Exception:
                pass

        # Per-room parser and line buffer for pretty display
        _room_parsers[room] = get_parser(TERMINAL_PARSER)
        parse_line_buf    = [""]   # line fragment accumulator
        _current_group_id = [None] # current cmd group_id (mutable ref)

        def _check_manual_echo(line: str) -> str | None:
            """If line matches a pending manual command, pop and return it."""
            pending = _room_parsers.get(f"{room}_pending_cmds")
            if not pending:
                return None
            clean = line.strip().lstrip(">").strip()
            for i, cmd in enumerate(pending):
                if clean == cmd:
                    pending.pop(i)
                    return cmd
            return None

        # Session log — one file per terminal connection (vcom only, captures both channels)
        serial_id = room.split("_")[0]
        is_vcom   = room.endswith("_vcom")
        session_log = None
        log_line_buf = [""]   # line fragment accumulator for clean log lines
        if is_vcom:
            session_log = get_log_file(serial_id)
            with open(session_log, "a") as f:
                f.write(f"# Session started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# Board: {serial_id}  Host: {host}  Port: {port}\n")
            _room_parsers[f"{serial_id}_session_log"] = session_log

        def reader():
            print(f"[TELNET] reader started: {room}")
            is_vcom = room.endswith("_vcom")
            buf = []
            room_parser = _room_parsers.get(room)
            while True:
                try:
                    chunk = tn.recv(4096)
                    if not chunk:
                        print(f"[TELNET] empty chunk, closing: {room}")
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    # Always emit raw data for xterm (Classic mode)
                    socketio.emit("terminal_output",
                                  {"data": text, "room": room},
                                  room=room)
                    # Parse lines and emit terminal_line events for Modern mode
                    if room_parser and TERMINAL_PRETTY:
                        try:
                            stream = "VCOM" if is_vcom else "ADMIN"
                            parsed = _extract_lines(text, parse_line_buf, room_parser,
                                                    session_log=session_log, stream=stream)
                            for pl in parsed:
                                role = pl.get("role","unknown")
                                if role == "prompt":
                                    _current_group_id[0] = None
                                    # Clear stale pending manual commands
                                    pending = _room_parsers.get(f"{room}_pending_cmds")
                                    if pending:
                                        pending.clear()
                                    continue
                                if role == "unknown":
                                    # Could be a manual command echo not yet recognised
                                    matched = _check_manual_echo(pl["raw"])
                                    if matched:
                                        pl["role"] = "cmd"
                                        pl["display"] = matched
                                        role = "cmd"
                                    else:
                                        continue
                                # Check if this is a manual command echo
                                if role == "cmd":
                                    matched = _check_manual_echo(pl["raw"])
                                    if matched:
                                        pl["display"] = matched  # use exact cmd as typed
                                # Assign group_id
                                if role == "cmd":
                                    import uuid as _uuid
                                    _current_group_id[0] = _uuid.uuid4().hex[:12]
                                    pl["group_id"] = _current_group_id[0]
                                elif role in ("response", "event") and _current_group_id[0]:
                                    pl["group_id"] = _current_group_id[0]
                                    if role == "response":
                                        _current_group_id[0] = None  # response closes group
                                elif role == "boot":
                                    pl["group_id"] = pl.get("boot_id")
                                ts_now = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                                socketio.emit("terminal_line", {**pl, "room": room, "ts": ts_now}, room=room)
                        except Exception as _pe:
                            print(f"[PARSER] error: {_pe}")
                    # Dispatch to board listeners (for script execution)
                    board = active_boards.get(room)
                    if board:
                        buf.append(text)
                        combined  = "".join(buf)
                        prompt    = board._vcom_prompt if is_vcom else board._admin_prompt
                        listeners = board._vcom_listeners if is_vcom else board._admin_listeners
                        for listener in list(listeners):
                            try: listener(combined)
                            except: pass
                        if prompt in combined:
                            buf.clear()
                except Exception as e:
                    print(f"[TELNET] reader error: {room}: {e}")
                    break
            if session_log:
                # Flush any remaining fragment in parse_line_buf
                if parse_line_buf[0].strip():
                    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                    stream = "VCOM" if is_vcom else "ADMIN"
                    with open(session_log, "a") as f:
                        f.write(f"[{ts}][{stream}] {parse_line_buf[0].strip()}\n")
                with open(session_log, "a") as f:
                    f.write(f"# Session ended: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            socketio.emit("terminal_closed", {"room": room}, room=room)
            active_telnets.pop(room, None)
            _room_parsers.pop(room, None)
        threading.Thread(target=reader, daemon=True).start()
        emit("terminal_ready", {"room": room})
        print(f"[TELNET] terminal_ready emitted: {room}")

    except Exception as e:
        print(f"[TELNET] connection failed: {room}: {e}")
        emit("terminal_error", {"message": str(e)})

@socketio.on("disconnect_telnet")
def handle_disconnect_telnet(data):
    room = data.get("room")
    tn   = active_telnets.pop(room, None)
    if tn:
        try: tn.close()
        except: pass

@socketio.on("connect_tty")
def handle_connect_tty(data):
    room     = data["room"]
    tty_path = data["tty"]
    join_room(room)

    if room in active_telnets:
        return

    try:
        ser = serial.Serial(tty_path, baudrate=115200, timeout=0)
        active_telnets[room] = ser

        def reader():
            while True:
                try:
                    chunk = ser.read(4096)
                    if not chunk:
                        continue
                    socketio.emit("terminal_output",
                                  {"data": chunk.decode("utf-8", errors="replace"), "room": room},
                                  room=room)
                except Exception as e:
                    print(f"[TELNET] tty reader error: {e}")
                    break
            socketio.emit("terminal_closed", {"room": room}, room=room)
            active_telnets.pop(room, None)

        threading.Thread(target=reader, daemon=True).start()
        emit("terminal_ready", {"room": room})

    except Exception as e:
        print(f"[ERROR] connect_tty {tty_path}: {e}")
        emit("terminal_error", {"message": str(e)})

# Per-session input buffer for admin channels (accumulate keystrokes → emit terminal_cmd on \r)
_admin_input_buf = {}  # sid -> str

@socketio.on("terminal_input")
def handle_terminal_input(data):
    room = data["room"]
    tn   = active_telnets.get(room)
    if tn:
        try:
            if hasattr(tn, "sendall"):
                tn.sendall(data["data"].encode("utf-8"))
            else:
                tn.write(data["data"].encode("utf-8"))
            # Log manual commands to session log
            serial_id = room.split("_")[0]
            is_vcom   = room.endswith("_vcom")
            stream    = "VCOM" if is_vcom else "ADMIN"
            log_path  = _room_parsers.get(f"{serial_id}_session_log")
            cmd_text  = data["data"].replace("\r\n","").replace("\r","").replace("\n","").strip()
            if log_path and cmd_text and cmd_text not in ("\x7f", "\x08"):
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                with open(log_path, "a") as f:
                    f.write(f"[{ts}][{stream}][CMD] {cmd_text}\n")
            # Accumulate keystrokes for admin display
            sid = request.sid
            raw = data["data"]
            key = f"{room}_{sid}"
            if raw in ("\r", "\n", "\r\n"):
                cmd = _admin_input_buf.pop(key, "").strip()
                if cmd:
                    if is_vcom:
                        # Store expected echo so reader emits terminal_line(cmd)
                        pending = _room_parsers.get(f"{room}_pending_cmds")
                        if pending is None:
                            pending = []
                            _room_parsers[f"{room}_pending_cmds"] = pending
                        pending.append(cmd)
                    else:
                        socketio.emit("terminal_cmd", {"data": cmd, "room": room}, room=room)
            elif raw in ("\x7f", "\x08"):
                _admin_input_buf[key] = _admin_input_buf.get(key, "")[:-1]
            else:
                _admin_input_buf[key] = _admin_input_buf.get(key, "") + raw
        except Exception as e:
            emit("terminal_error", {"message": str(e)})


@app.route("/api/adapter/<serial>/erase", methods=["POST"])
def adapter_erase(serial):
    """Mass erase a single board from Manual Control."""
    try:
        data   = get_connections(serial)
        adapter = data.get("adapter", {})
        conn_type = adapter.get("connectivityType", "usb")
        if conn_type == "usb":
            conn_flag = ["--serialno", serial]
        else:
            conn_flag = ["--ip", adapter.get("host", "")]
        cmd = [COMMANDER_PATH, "device", "masserase"] + conn_flag
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
        data   = get_connections(serial)
        adapter = data.get("adapter", {})
        conn_type = adapter.get("connectivityType", "usb")
        if conn_type == "usb":
            conn_flag = ["--serialno", serial]
        else:
            conn_flag = ["--ip", adapter.get("host", "")]
        cmd = [COMMANDER_PATH, "flash", path] + conn_flag
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ok = result.returncode == 0
        return jsonify({"ok": ok, "msg": result.stderr.strip() if not ok else ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/adapter/<serial>/admin-cmd", methods=["POST"])
def admin_cmd(serial):
    cmd  = request.json.get("cmd", "")
    room = f"{serial}_admin"
    tn   = active_telnets.get(room)

    if not tn:
        # Try to connect on demand using terminal-info
        try:
            data    = get_connections(serial)
            adapter = data.get("adapter", {})
            conns   = data.get("connections", [])
            ports   = {c["portName"]: c["portNumber"] for c in conns}

            connectivity = adapter.get("connectivityType", "usb")
            host = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"
            port = ports.get("admin")

            if not port:
                return jsonify({"ok": False, "error": "no admin port available"}), 400

            tn = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
            tn.settimeout(5)
            tn.connect((host, port))
            tn.settimeout(None)
            active_telnets[room] = tn

            # Start reader thread
            def reader():
                while True:
                    try:
                        chunk = tn.recv(4096)
                        if not chunk:
                            break
                        socketio.emit("terminal_output",
                                      {"data": chunk.decode("utf-8", errors="replace"), "room": room},
                                      room=room)
                    except Exception:
                        break
                active_telnets.pop(room, None)

            threading.Thread(target=reader, daemon=True).start()
            print(f"[INFO] admin-cmd: auto-connected to {host}:{port} for {serial}")

        except Exception as e:
            print(f"[ERROR] admin-cmd connect failed {serial}: {e}")
            return jsonify({"ok": False, "error": f"not connected and auto-connect failed: {str(e)}"}), 400

    try:
        tn.sendall((cmd + "\r\n").encode("utf-8"))
        socketio.emit("terminal_echo",
                      {"data": f"› {cmd}", "room": room},
                      room=room)
        return jsonify({"ok": True})
    except Exception as e:
        active_telnets.pop(room, None)
        return jsonify({"ok": False, "error": str(e)}), 500

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
        adapters_data = requests.get(f"{SDM_BASE}/api/adapters", timeout=3).json()
        if isinstance(adapters_data, dict):
            adapters_data = adapters_data.get("adapters", [])
    except Exception as e:
        return jsonify({"ok": False, "issues": [{"line": None, "msg": f"Cannot reach SDM: {e}"}]}), 500

    # Build adapter lookup maps
    # host → adapter, serialNumber → adapter
    adapter_by_host   = {a.get("host"):         a for a in adapters_data if a.get("host")}
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
        else:
            # Validate with SDM
            try:
                result = subprocess.run(
                    [SDM_PATH, "board", "search", "-s", str(board_id)],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode != 0:
                    issue("board", f"board '{board_id}' not found in SDM")
            except Exception as e:
                issue("board", f"board search failed: {e}")

        # ── connection (optional, replaces jlink_name_or_ip) ──
        # Accepts both old key (jlink_name_or_ip) and new key (connection)
        connection = str(board.get("connection", board.get("jlink_name_or_ip", "usb"))).strip()
        if connection.lower() in ("none", ""):
            connection = "usb"
        matched_adapter = None

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
            futures = {ex.submit(b.admin, "reset"): b for b in self._boards}
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
    """
    Wraps VCOM + ADMIN telnet connections for one board.
    Connected before script runs, never closed automatically.
    """

    def __init__(self, serial, host, vcom_port, admin_port,
                 run_id, scenario_dir, open_terminal=False, nickname=None):
        self.serial        = serial
        self.nickname      = nickname  # scenario nickname (overrides SDM nickname)
        self.host          = host
        self.vcom_port     = vcom_port
        self.admin_port    = admin_port
        self.run_id        = run_id
        self.scenario_dir  = scenario_dir
        self.open_terminal_flag = open_terminal

        # Sockets (set in connect())
        self._vcom_sock  = None
        self._admin_sock = None

        # Config (set by script via config_vcom / config_admin)
        self._vcom_le     = b"\r\n"
        self._vcom_echo   = True
        self._vcom_prompt = ">"
        self._admin_le    = b"\r\n"
        self._admin_echo  = False
        self._admin_prompt = ">"

        self._vcom_listeners  = []
        self._admin_listeners = []
        self._vcom_expecting_echo = [False]  # ← default, always valid
        self._log_path = None
        self._scenario_ctx = None  # set by Scenario when global script is used
        self.parser = get_parser(TERMINAL_PARSER)  # protocol parser (auto/railtest/generic)

    # ── configuration ────────────────────────────────────────
    def config_vcom(self, line_ending="CRLF", echo=True, prompt=">"):
        self._vcom_le     = LINE_ENDINGS.get(line_ending.upper(), b"\r\n")
        self._vcom_echo   = echo
        self._vcom_prompt = prompt

    def config_admin(self, line_ending="CRLF", echo=False, prompt=">"):
        self._admin_le     = LINE_ENDINGS.get(line_ending.upper(), b"\r\n")
        self._admin_echo   = echo
        self._admin_prompt = prompt

    # ── connect ──────────────────────────────────────────────
    def connect(self):
        vcom_room  = f"{self.serial}_vcom"
        admin_room = f"{self.serial}_admin"

        for room, port, attr in [
            (vcom_room,  self.vcom_port,  "_vcom_sock"),
            (admin_room, self.admin_port, "_admin_sock"),
        ]:
            # Always register this Board as the active dispatcher for this room.
            # The reader thread uses active_boards[room] at dispatch time, so
            # reusing a socket across runs automatically picks up the new Board.
            active_boards[room] = self

            if room in active_telnets:
                setattr(self, attr, active_telnets[room])
                print(f"[RUN] reusing existing connection for {room}")
                socketio.emit("terminal_ready", {"room": room}, room=room)
            else:
                s = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
                s.settimeout(5)
                s.connect((self.host, port))
                s.settimeout(None)
                active_telnets[room] = s
                setattr(self, attr, s)
                self._start_reader(s, room)
                print(f"[RUN] new connection for {room}")
                socketio.emit("terminal_ready", {"room": room}, room=room)

        if self.open_terminal_flag:
            def _open():
                url = f"http://127.0.0.1:{WEB_PORT}/terminal/{self.serial}?from_run=1"
                webview.create_window(
                    f"{self.serial} — Terminal",
                    url,
                    width=820, height=620,
                    resizable=True
                )
            threading.Thread(target=_open, daemon=True).start()

    def _start_reader(self, sock, room):
        is_vcom  = room.endswith("_vcom")
        stream   = "vcom" if is_vcom else "admin"
        buf      = []   # accumulates chunks until prompt found
        parse_line_buf = [""]  # line fragment accumulator for pretty

        def flush(board):
            if not buf:
                return
            text = "".join(buf)
            buf.clear()
            run_room = f"run_{board.run_id}"
            socketio.emit("run_output",
                          {"serial": board.serial, "data": text, "stream": stream},
                          room=run_room)

        def reader():
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    board  = active_boards.get(room)
                    groups = []
                    if board and TERMINAL_PRETTY:
                        try:
                            groups = _extract_groups(text, parse_line_buf, board.parser)
                        except Exception as _pe:
                            print(f"[PARSER] error: {_pe}")
                    socketio.emit("terminal_output",
                                  {"data": text, "room": room, "groups": groups},
                                  room=room)
                    if board:
                        if board._log_path:
                            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            with open(board._log_path, "a") as f:
                                for line in text.splitlines():
                                    f.write(f"[{ts}][{stream}] {line}\n")
                        buf.append(text)
                        prompt    = board._vcom_prompt if is_vcom else board._admin_prompt
                        combined  = "".join(buf)
                        listeners = board._vcom_listeners if is_vcom else board._admin_listeners
                        for listener in list(listeners):
                            try: listener(combined)
                            except: pass
                        if prompt in combined:
                            flush(board)
                except Exception:
                    break
            board = active_boards.get(room)
            if board and buf:
                flush(board)
            active_telnets.pop(room, None)
            active_boards.pop(room, None)

        threading.Thread(target=reader, daemon=True).start()

    # ── VCOM ─────────────────────────────────────────────────
    def write_cli(self, data):
        if self._vcom_sock:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self._vcom_sock.sendall(data)
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
        self._vcom_sock.settimeout(timeout)
        data = b""
        try:
            while True:
                chunk = self._vcom_sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except Exception:
            pass
        self._vcom_sock.settimeout(None)
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
        self._vcom_sock.sendall(payload)

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

    # ── ADMIN ────────────────────────────────────────────────
    def _send_admin_raw(self, cmd):
        if not self._admin_sock:
            return
        payload = cmd.encode("utf-8") + self._admin_le
        self._admin_sock.sendall(payload)
        admin_room = f"{self.serial}_admin"
        socketio.emit("terminal_echo",
                      {"data": f"{cmd}", "room": admin_room},
                      room=admin_room)
        socketio.emit("terminal_cmd",
                      {"data": cmd, "room": admin_room},
                      room=admin_room)

    def admin(self, cmd, timeout=10.0):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[ADM] {self.nickname or self.serial} {ts} >>> {cmd}")
        # High-level interpretation
        cmd_lower = cmd.strip().lower()
        if cmd_lower == "reset":
            raw = "target reset 100"
        elif cmd_lower in ("pb0", "button0"):
            self._send_admin_raw("target button press 0")
            time.sleep(0.1)
            self._send_admin_raw("target button release 0")
            return ""
        elif cmd_lower in ("pb1", "button1"):
            self._send_admin_raw("target button press 1")
            time.sleep(0.1)
            self._send_admin_raw("target button release 1")
            return ""
        else:
            raw = cmd

        if not self._admin_sock:
            return ""

        # Register listener BEFORE sending the command to avoid missing
        # responses that arrive immediately (race condition on fast/USB boards)
        event    = threading.Event()
        response = []
        prompt   = self._admin_prompt

        def on_data(combined):
            if prompt in combined:
                response.append(combined)
                event.set()

        self._admin_listeners.append(on_data)

        self._admin_sock.sendall((raw + self._admin_le.decode()).encode("utf-8"))
        admin_room = f"{self.serial}_admin"
        socketio.emit("terminal_echo",
                      {"data": f"\\{raw}", "room": admin_room},
                      room=admin_room)

        event.wait(timeout=timeout)
        self._admin_listeners.remove(on_data)
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        timed_out = not event.is_set()
        print(f"[ADM] {self.nickname or self.serial} {ts} <<< {'TIMEOUT' if timed_out else 'ok'}")
        return "".join(response)

    def reset(self):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[RST] {self.nickname or self.serial} {ts}")
        result = self.admin("reset")
        time.sleep(1.0)
        return result

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

    def button(self, pb, duration=0.1):
        if not self._admin_sock:
            return
        self._send_admin_raw(f"target button press {pb}")
        time.sleep(duration)
        self._send_admin_raw(f"target button release {pb}")

    # ── helpers ───────────────────────────────────────────────
    def _read_until(self, sock, prompt, timeout):
        """Read from sock until prompt is found or timeout expires.
        Uses a deadline loop so partial chunks are accumulated correctly."""
        if not sock:
            return ""
        deadline = time.monotonic() + timeout
        data = b""
        prompt_b = prompt.encode("utf-8")
        sock.settimeout(0.1)
        try:
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if prompt_b in data:
                        break
                except OSError:
                    pass  # timeout on this recv slice, keep looping
        finally:
            sock.settimeout(None)
        return data.decode("utf-8", errors="replace")

    def delay(self, seconds):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[DLY] {self.nickname or self.serial} {ts} {seconds}s")
        time.sleep(seconds)

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
    if connection.lower() in ("usb", "none", ""):
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

    # Get ports
    emit_status("connecting")
    try:
        info    = get_connections(serial)
        adapter = info.get("adapter", {})
        conns   = info.get("connections", [])
        ports   = {c["portName"]: c["portNumber"] for c in conns}
        print(f"[RUN] ports: {ports}")
        connectivity = adapter.get("connectivityType", "usb")
        host = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"
        vcom_port  = ports.get("serial1")
        admin_port = ports.get("admin")
        print(f"[RUN] host={host} vcom={vcom_port} admin={admin_port}")
        if not vcom_port or not admin_port:
            emit_status("error", "Could not get ports from SDM")
            return
    except Exception as e:
        print(f"[RUN] get-info exception: {e}")
        emit_status("error", f"SDM error: {e}")
        return

    # Create Board instance and connect
    board = Board(
        serial        = serial,
        host          = host,
        vcom_port     = vcom_port,
        admin_port    = admin_port,
        run_id        = run_id,
        scenario_dir  = scenario_dir,
        open_terminal = board_cfg.get("open_terminal", False),
        nickname      = board_cfg.get("_nickname"),
    )
    board._log_path = log_path
    try:
        print(f"[RUN] connecting board {serial} host={host} vcom={vcom_port} admin={admin_port}")
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

    # Fetch available adapters from SDM
    try:
        adapters_data = requests.get(f"{SDM_BASE}/api/adapters", timeout=3).json()
        if isinstance(adapters_data, dict):
            adapters_data = adapters_data.get("adapters", [])
        adapter_by_host   = {a.get("host"): a for a in adapters_data if a.get("host")}
        adapter_by_serial = {a.get("serialNumber"): a for a in adapters_data if a.get("serialNumber")}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Cannot reach SDM: {e}"}), 500

    # Resolve each board entry
    resolved_boards = []
    for b in boards_cfg:
        # connection: usb (default) or IP address
        connection = str(b.get("connection", b.get("jlink_name_or_ip", "usb"))).strip()
        if connection.lower() in ("usb", "none", ""):
            connection = "usb"

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
                    info    = get_connections(serial)
                    adapter = info.get("adapter", {})
                    conns   = info.get("connections", [])
                    ports   = {c["portName"]: c["portNumber"] for c in conns}
                    connectivity = adapter.get("connectivityType", "usb")
                    host    = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"
                    vcom_port  = ports.get("serial1")
                    admin_port = ports.get("admin")
                    if not vcom_port or not admin_port:
                        raise RuntimeError("Could not get ports from SDM")
                    board_obj = Board(
                        serial        = serial,
                        host          = host,
                        vcom_port     = vcom_port,
                        admin_port    = admin_port,
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
                    info    = get_connections(serial)
                    adapter = info.get("adapter", {})
                    conns   = info.get("connections", [])
                    ports   = {c["portName"]: c["portNumber"] for c in conns}
                    connectivity = adapter.get("connectivityType", "usb")
                    host    = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"
                    vcom_port  = ports.get("serial1")
                    admin_port = ports.get("admin")
                    if not vcom_port or not admin_port:
                        raise RuntimeError("Could not get ports from SDM")
                    board_obj = Board(
                        serial        = serial,
                        host          = host,
                        vcom_port     = vcom_port,
                        admin_port    = admin_port,
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

    restart_sdm()

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