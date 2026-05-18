import subprocess
import time
import requests
import configparser
import os
from flask import Flask, jsonify, render_template, request 
from flask_socketio import SocketIO, emit, join_room
import socket as sock_module
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import glob
import webview
import threading
import glob as glob_module
import serial.tools.list_ports 
import pty, os, termios
import yaml

GROUPS_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenari")

# Dimensions
WIN_W          = 800
WIN_COLLAPSED  = 65   # hauteur barre compacte
WIN_EXPANDED   = 700   # hauteur dépliée

from binresolve import resolve_sdm, resolve_commander

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

def ensure_sdm():
    if is_sdm_running():
        return
    start_sdm()
    for _ in range(10):
        time.sleep(1)
        if is_sdm_running():
            return

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

def get_adapter_info(serial: str) -> dict:
    try:
        r = requests.get(f"{SDM_BASE}/api/adapter/{serial}/get-info", timeout=3)
        return r.json().get("adapter", {})
    except requests.RequestException as e:
        print(f"[WARN] get-info {serial}: {e}")
        return {}

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
            r2      = requests.get(f"{SDM_BASE}/api/adapter/{serial}/get-info", timeout=2)
            info    = r2.json()
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

    def pick_directory(self):
        result = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG
        )
        return result[0] if result else None

    def pick_file(self, directory="", file_types=None):
        if file_types is None:
            file_types = ("All files (*.*)",)
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            directory=str(directory),
            allow_multiple=False,
            file_types=tuple(file_types)
        )
        return result[0] if result else None
    
    def open_terminal(self, serial):
        """Appelé depuis le JS via pywebview.api.open_terminal(serial)"""
        def _open():
            # Récupérer les infos de l'adapter
            try:
                r = requests.get(f"{SDM_BASE}/api/adapter/{serial}/get-info", timeout=3)
                info = r.json().get("adapter", {})
            except Exception:
                info = {}

            url = f"http://127.0.0.1:{WEB_PORT}/terminal/{serial}"
            win = webview.create_window(
                f"{serial} — Terminal",
                url,
                width=820, height=520,
                resizable=True
            )
        # create_window doit être appelé depuis le thread pywebview
        webview.windows[0].evaluate_js("")  # assure qu'on est prêt
        threading.Thread(target=_open, daemon=True).start()

# ── terminal page ──────────────────────────────────
@app.route("/terminal/<serial>")
def terminal_page(serial):
    return render_template("terminal.html", serial=serial)

# ── adapter info endpoint (pour le terminal) ───────
@app.route("/api/terminal-info/<serial>")
def terminal_info(serial):
    try:
        r = requests.get(f"{SDM_BASE}/api/adapter/{serial}/get-info", timeout=3)
        data    = r.json()
        adapter = data.get("adapter", {})

        connectivity = adapter.get("connectivityType", "usb")
        host = adapter.get("host", "127.0.0.1") if connectivity != "usb" else "127.0.0.1"

        # connections is a list: [{"portName": "serial1", "portNumber": 4901, ...}, ...]
        conns_list = data.get("connections", [])
        ports      = {c["portName"]: c["portNumber"] for c in conns_list}

        vcom_port  = ports.get("serial1")
        admin_port = ports.get("admin")
        print(f"[INFO] terminal-info {serial}: vcom={vcom_port} admin={admin_port}")

        # Si pas de ports réseau → chercher TTY USB
        tty_path = None
        if not vcom_port and connectivity == "usb":
            candidates = (
                glob_module.glob("/dev/tty.usbmodem*") +
                glob_module.glob("/dev/cu.usbmodem*")
            )
            for c in candidates:
                if serial.lower() in c.lower():
                    tty_path = c
                    break
            if not tty_path:
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
active_telnets = {}  # room_id -> telnetlib.Telnet

@socketio.on("connect_telnet")
def handle_connect_telnet(data):
    room    = data["room"]    # e.g. "440114849_vcom"
    host    = data["host"]
    port    = int(data["port"])
    print(f"[TELNET] connect request: room={room} host={host} port={port}")
    join_room(room)

    if room in active_telnets:
        print(f"[TELNET] already connected: {room}")
        return  # déjà connecté

    try:
        tn = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
        tn.settimeout(5)
        tn.connect((host, port))
        tn.settimeout(None)
        print(f"[TELNET] connected: {room}")
        # Lire le banner initial
        import time
        time.sleep(0.2)
        banner = b""
        tn.setblocking(False)
        try:
            print(f"[TELNET] banner received: {len(banner)} bytes: {banner[:50]}")
        except Exception as be:
            print(f"[TELNET] no banner: {be}")
        tn.setblocking(True)

        active_telnets[room] = tn
        if banner:
            socketio.emit("terminal_output",
                        {"data": banner.decode("utf-8", errors="replace"), "room": room},
                        room=room)

        def reader():
            print(f"[TELNET] reader started: {room}")
            while True:
                try:
                    chunk = tn.recv(4096)
                    if not chunk:
                        print(f"[TELNET] empty chunk, closing: {room}")
                        break
                    print(f"[TELNET] data received: {room} {len(chunk)} bytes")
                    socketio.emit("terminal_output",
                                {"data": chunk.decode("utf-8", errors="replace"), "room": room},
                                room=room)
                except Exception as e:
                    print(f"[TELNET] reader error: {room}: {e}")
                    break
            socketio.emit("terminal_closed", {"room": room}, room=room)
            active_telnets.pop(room, None)
        threading.Thread(target=reader, daemon=True).start()
        emit("terminal_ready", {"room": room})
        print(f"[TELNET] terminal_ready emitted: {room}")

    except Exception as e:
        print(f"[TELNET] connection failed: {room}: {e}")
        emit("terminal_error", {"message": str(e)})

@socketio.on("terminal_input")
def handle_terminal_input(data):
    room = data["room"]
    tn   = active_telnets.get(room)
    if tn:
        try:
            tn.sendall(data["data"].encode("utf-8"))
        except Exception as e:
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
        import serial as pyserial
        ser = pyserial.Serial(tty_path, baudrate=115200, timeout=0)
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
        except Exception as e:
            emit("terminal_error", {"message": str(e)})


@app.route("/api/adapter/<serial>/admin-cmd", methods=["POST"])
def admin_cmd(serial):
    cmd  = request.json.get("cmd", "")
    room = f"{serial}_admin"
    tn   = active_telnets.get(room)

    if not tn:
        # Try to connect on demand using terminal-info
        try:
            r     = requests.get(f"{SDM_BASE}/api/adapter/{serial}/get-info", timeout=3)
            data  = r.json()
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

import yaml

import re as re_module

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
        return re_module.match(r"^\d{9}$", str(s)) is not None
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

        # ── jlink_name_or_ip (optional) ────────────────
        jlink = str(board.get("jlink_name_or_ip", "")).strip()
        if jlink.lower() == "none":
            jlink = ""
        matched_adapter = None

        if jlink:
            # Validate syntax
            if not is_valid_ip(jlink) and not is_valid_serial(jlink):
                issue("jlink_name_or_ip",
                      f"'{jlink}' is not a valid IP address (x.x.x.x) or Silabs serial number (9 digits)")
            else:
                # Find matching adapter
                matched_adapter = adapter_by_host.get(jlink) or adapter_by_serial.get(jlink)

                if not matched_adapter:
                    issue("jlink_name_or_ip",
                          f"'{jlink}' not found in available adapters")
                elif board_id:
                    # Check if board matches
                    yaml_board_norm     = re_module.sub(r'^BRD', '', str(board_id), flags=re_module.IGNORECASE)

                    adapter_boards = matched_adapter.get("boards", [])
                    adapter_board_ids = set()
                    for ab in adapter_boards:
                        for field in ["id", "shortLabel", "label", "pn"]:
                            val = ab.get(field, "")
                            if val:
                                normalized = re_module.sub(r'^BRD', '', val.split()[0], flags=re_module.IGNORECASE).upper()
                                adapter_board_ids.add(normalized)

                    if yaml_board_norm not in adapter_board_ids:
                        nickname = matched_adapter.get("nickname") or matched_adapter.get("serialNumber", "")
                        best_id  = adapter_boards[0].get("shortLabel", adapter_boards[0].get("id", "?")) if adapter_boards else "?"
                        best_short = re_module.sub(r'^BRD', '', best_id, flags=re_module.IGNORECASE)
                        issue("board",
                              f"adapter '{jlink}' ({nickname}) has board '{best_short}' "
                              f"but scenario specifies '{yaml_board_norm}'. "
                              f"Consider changing board to '{best_short}'")
        # If board_id set but no jlink — check if an available adapter has that board
# If board_id set but no jlink — check if an available adapter has that board
        if board_id and not jlink:
            yaml_board_norm = re_module.sub(r'^BRD', '', str(board_id), flags=re_module.IGNORECASE).upper()
            matching = []
            for a in adapters_data:
                adapter_boards = a.get("boards", [])
                for ab in adapter_boards:
                    for field in ["id", "shortLabel"]:
                        val = ab.get(field, "")
                        normalized = re_module.sub(r'^BRD', '', val.split()[0] if val else "", flags=re_module.IGNORECASE).upper()
                        if normalized == yaml_board_norm:
                            matching.append(a)
                            break

            if matching:
                suggestions = ", ".join(
                    f"{a.get('host') or a.get('serialNumber', '?')}"
                    + (f" ({a.get('nickname')})" if a.get('nickname') else "")
                    for a in matching
                )
                warn("jlink_name_or_ip",
                     f"no jlink/IP set — board '{yaml_board_norm}' found on: {suggestions}. "
                     f"Run will pick one randomly")
            else:
                # No exact match — suggest any available adapter
                if adapters_data:
                    any_adapter = adapters_data[0]
                    any_id = any_adapter.get("host") or any_adapter.get("serialNumber", "?")
                    any_nick = f" ({any_adapter.get('nickname')})" if any_adapter.get("nickname") else ""
                    warn("jlink_name_or_ip",
                        f"no jlink/IP set and no adapter with board '{yaml_board_norm}' found")
                else:
                    info("jlink_name_or_ip",
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
    

# ----------------------------
if __name__ == "__main__":
    ensure_sdm()
    t = threading.Thread(
        target=lambda: socketio.run(app, port=WEB_PORT, debug=False, use_reloader=False)
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