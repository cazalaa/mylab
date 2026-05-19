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
    


# ============================================================
# Board class + Run pipeline
# ============================================================

import uuid
import importlib.util

active_runs = {}  # run_id -> {status, boards: {serial: status}}

# ── VCOM line ending helper ──────────────────────────────────
LINE_ENDINGS = {"CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}

class Board:
    """
    Wraps VCOM + ADMIN telnet connections for one board.
    Connected before script runs, never closed automatically.
    """

    def __init__(self, serial, host, vcom_port, admin_port,
                 run_id, scenario_dir, open_terminal=False):
        self.serial        = serial
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
        self._admin_prompt = "WSTK>"

        self._vcom_listeners  = []
        self._admin_listeners = []

    # ── configuration ────────────────────────────────────────
    def config_vcom(self, line_ending="CRLF", echo=True, prompt=">"):
        self._vcom_le     = LINE_ENDINGS.get(line_ending.upper(), b"\r\n")
        self._vcom_echo   = echo
        self._vcom_prompt = prompt

    def config_admin(self, line_ending="CRLF", echo=False, prompt="WSTK>"):
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
            if room in active_telnets:
                # Reuse existing socket — no new reader needed
                setattr(self, attr, active_telnets[room])
                print(f"[RUN] reusing existing connection for {room}")
            else:
                s = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
                s.settimeout(5)
                s.connect((self.host, port))
                s.settimeout(None)
                active_telnets[room] = s
                setattr(self, attr, s)
                self._start_reader(s, room)
                print(f"[RUN] new connection for {room}")

        # Open terminal window if requested
        if self.open_terminal_flag:
            def _open():
                url = f"http://127.0.0.1:{WEB_PORT}/terminal/{self.serial}"
                webview.create_window(
                    f"{self.serial} — Terminal",
                    url,
                    width=820, height=520,
                    resizable=True
                )
            threading.Thread(target=_open, daemon=True).start()

    def _start_reader(self, sock, room):
        run_room = f"run_{self.run_id}"
        is_vcom  = room.endswith("_vcom")
        listeners = self._vcom_listeners if is_vcom else self._admin_listeners

        def reader():
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    # Forward to terminal
                    socketio.emit("terminal_output",
                                  {"data": text, "room": room}, room=room)
                    # Forward to run output panel
                    socketio.emit("run_output",
                                  {"serial": self.serial, "data": text,
                                   "stream": "vcom" if is_vcom else "admin"},
                                  room=run_room)
                    # Notify any waiting cli()/admin() calls
                    for listener in list(listeners):
                        try:
                            listener(text)
                        except Exception:
                            pass
                except Exception:
                    break
            active_telnets.pop(room, None)

        threading.Thread(target=reader, daemon=True).start()

    # ── VCOM ─────────────────────────────────────────────────
    def write_cli(self, data):
        if self._vcom_sock:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self._vcom_sock.sendall(data)
            vcom_room = f"{self.serial}_vcom"
            socketio.emit("terminal_echo",
                          {"data": data.decode("utf-8", errors="replace"), "room": vcom_room},
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
        payload = cmd.encode("utf-8") + self._vcom_le
        self._vcom_sock.sendall(payload)

        # Echo command to terminal
        vcom_room = f"{self.serial}_vcom"
        socketio.emit("terminal_echo",
                      {"data": f"\u203a {cmd}", "room": vcom_room},
                      room=vcom_room)

        # Wait for prompt using event — reader thread will signal us
        event    = threading.Event()
        response = []
        prompt   = self._vcom_prompt

        def on_data(data):
            response.append(data)
            if prompt in data:
                event.set()

        self._vcom_listeners.append(on_data)
        event.wait(timeout=timeout)
        self._vcom_listeners.remove(on_data)

        return "".join(response)

    # ── ADMIN ────────────────────────────────────────────────
    def _send_admin_raw(self, cmd):
        if not self._admin_sock:
            return
        payload = cmd.encode("utf-8") + self._admin_le
        self._admin_sock.sendall(payload)
        admin_room = f"{self.serial}_admin"
        socketio.emit("terminal_echo",
                      {"data": f"\u203a {cmd}", "room": admin_room},
                      room=admin_room)

    def admin(self, cmd, timeout=10.0):
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

        self._admin_sock.sendall((raw + self._admin_le.decode()).encode("utf-8"))
        admin_room = f"{self.serial}_admin"
        socketio.emit("terminal_echo",
                      {"data": f"\u203a {raw}", "room": admin_room},
                      room=admin_room)

        event    = threading.Event()
        response = []
        prompt   = self._admin_prompt

        def on_data(data):
            response.append(data)
            if prompt in data:
                event.set()

        self._admin_listeners.append(on_data)
        event.wait(timeout=timeout)
        self._admin_listeners.remove(on_data)

        return "".join(response)

    def reset(self):
        result = self.admin("reset")
        # Wait for boot banner to fully arrive — reader thread handles display
        time.sleep(1.0)
        return result

    def button(self, pb, duration=0.1):
        if not self._admin_sock:
            return
        self._send_admin_raw(f"target button press {pb}")
        time.sleep(duration)
        self._send_admin_raw(f"target button release {pb}")

    # ── helpers ───────────────────────────────────────────────
    def _read_until(self, sock, prompt, timeout):
        if not sock:
            return ""
        sock.settimeout(timeout)
        data = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if prompt.encode() in data:
                    break
        except Exception:
            pass
        sock.settimeout(None)
        return data.decode("utf-8", errors="replace")

    def delay(self, seconds):
        self.print(f"[delay {seconds}s]")
        time.sleep(seconds)

    def print(self, msg):
        run_room = f"run_{self.run_id}"
        socketio.emit("run_output",
                      {"serial": self.serial, "data": str(msg) + "\n", "stream": "script"},
                      room=run_room)


# ── Run pipeline ─────────────────────────────────────────────

def _flash_board(board_cfg, scenario_dir, run_id):
    serial  = board_cfg.get("_resolved_serial", "")
    jlink   = str(board_cfg.get("jlink_name_or_ip", "")).strip()
    print(f"[RUN] _flash_board serial={serial} jlink={jlink}")
    is_ip   = "." in jlink
    log     = []

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
            print(f"[RUN] TIMEOUT running: {' '.join(cmd)}")
            log.append(f"ERROR: timeout after 60s")
            return False
        except Exception as e:
            print(f"[RUN] EXCEPTION: {e}")
            log.append(f"ERROR: {e}")
            return False

    conn_flag = ["--ip", jlink] if is_ip else ["-s", jlink]

    if board_cfg.get("masserase", True):
        print(f"[RUN] starting masserase")
        if not run_cmd([COMMANDER_PATH, "device", "masserase"] + conn_flag):
            return False, log

    for s37 in board_cfg.get("s37_files", []):
        s37_path = os.path.join(scenario_dir, s37)
        print(f"[RUN] flashing {s37_path}")
        cmd = [COMMANDER_PATH, "flash", s37_path] + conn_flag
        if board_cfg.get("halt_reset", False):
            cmd += ["--halt"]
        if not run_cmd(cmd):
            return False, log

    print(f"[RUN] _flash_board done serial={serial}")
    return True, log


def _run_board(board_cfg, scenario_dir, run_id, script_code):
    serial = board_cfg.get("_resolved_serial", "unknown")
    print(f"[RUN] _run_board started serial={serial}")
    run_room = f"run_{run_id}"
    time.sleep(2.0)

    def emit_status(status, msg=""):
        active_runs[run_id]["boards"][serial] = status
        print(f"[RUN] emit_status {serial} → {status} to room run_{run_id}")
        socketio.emit("run_board_status",
                      {"serial": serial, "status": status, "msg": msg},
                      room=run_room)

    emit_status("flashing")

    # Flash
    ok, flash_log = _flash_board(board_cfg, scenario_dir, run_id)
    for line in flash_log:
        socketio.emit("run_output",
                      {"serial": serial, "data": line + "\n", "stream": "flash"},
                      room=run_room)
    if not ok:
        emit_status("error", "Flash failed")
        return

    # Get ports
    emit_status("connecting")
    try:
        r     = requests.get(f"{SDM_BASE}/api/adapter/{serial}/get-info", timeout=3)
        info  = r.json()
        print(f"[RUN] get-info response: {info.keys()}")
        adapter = info.get("adapter", {})
        conns   = info.get("connections", [])
        print(f"[RUN] connections: {conns}")
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
        serial       = serial,
        host         = host,
        vcom_port    = vcom_port,
        admin_port   = admin_port,
        run_id       = run_id,
        scenario_dir = scenario_dir,
        open_terminal = board_cfg.get("open_terminal", False)
    )
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
    print(f"[RUN] board.connect() done for {serial}, starting script")
    emit_status("running")
    print(f"[RUN] executing script for {serial}")
    try:
        namespace = {
            "board": board,
            "time": time,
            "__builtins__": __builtins__,
        }
        exec(script_code, namespace)
        print(f"[RUN] script exec done, calling script(board)")
        if "script" in namespace and callable(namespace["script"]):
            namespace["script"](board)
        print(f"[RUN] script(board) returned")
        emit_status("done")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[RUN] script exception: {tb}")
        socketio.emit("run_output",
                      {"serial": serial, "data": tb, "stream": "error"},
                      room=run_room)
        emit_status("error", str(e))


@app.route("/api/scenario-run", methods=["POST"])
def scenario_run():
    base = request.json.get("base", SCENARI_DIR)
    path = request.json.get("path", "")
    base = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base, path))

    if not full.startswith(base) or not os.path.isfile(full):
        return jsonify({"ok": False, "error": "File not found"}), 400

    scenario_dir = os.path.dirname(full)

    # Parse scenario
    with open(full) as f:
        content = f.read()
    try:
        scenario = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "error": f"YAML error: {e}"}), 400

    boards = scenario.get("boards", [])
    if not boards:
        return jsonify({"ok": False, "error": "No boards defined"}), 400

    # Resolve adapter serial numbers from jlink/IP
    try:
        adapters_data = requests.get(f"{SDM_BASE}/api/adapters", timeout=3).json()
        if isinstance(adapters_data, dict):
            adapters_data = adapters_data.get("adapters", [])
        adapter_by_host   = {a.get("host"): a for a in adapters_data if a.get("host")}
        adapter_by_serial = {a.get("serialNumber"): a for a in adapters_data if a.get("serialNumber")}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Cannot reach SDM: {e}"}), 500

    # Resolve serials and load scripts
    resolved_boards = []
    for b in boards:
        jlink = str(b.get("jlink_name_or_ip", "")).strip()
        if jlink and jlink.lower() != "none":
            adapter = adapter_by_host.get(jlink) or adapter_by_serial.get(jlink)
        else:
            # Pick any matching board from available
            board_id = str(b.get("board", "")).strip()
            adapter = None
            for a in adapters_data:
                for ab in a.get("boards", []):
                    for field in ["id", "shortLabel"]:
                        val = ab.get(field, "")
                        if re.sub(r"^BRD", "", val, flags=re.IGNORECASE).upper() == \
                           re.sub(r"^BRD", "", board_id, flags=re.IGNORECASE).upper():
                            adapter = a
                            break
                if adapter:
                    break

        if not adapter:
            return jsonify({"ok": False, "error": f"No adapter found for board {b.get('board', '?')}"}), 400

        bc = dict(b)
        bc["_resolved_serial"] = adapter.get("serialNumber")
        resolved_boards.append(bc)

    # Load script file
    script_file = boards[0].get("script", "")  # use first board's script for now
    script_code = ""
    if script_file:
        script_path = os.path.join(scenario_dir, script_file)
        if os.path.isfile(script_path):
            with open(script_path) as f:
                script_code = f.read()
    print(f"[RUN] resolved boards: {[b['_resolved_serial'] for b in resolved_boards]}")  # ← here
    print(f"[RUN] script_file: {script_file}, script_code length: {len(script_code)}")   # ← here
    # print(f"[RUN] run_id: {run_id}") 
    # Create run
    run_id = str(uuid.uuid4())[:8]
    active_runs[run_id] = {
        "status": "running",
        "scenario": path,
        "boards": {b["_resolved_serial"]: "pending" for b in resolved_boards}
    }

    # Start parallel threads
    def run_all():
        with ThreadPoolExecutor(max_workers=len(resolved_boards)) as executor:
            futures = [
                executor.submit(_run_board, bc, scenario_dir, run_id, script_code)
                for bc in resolved_boards
            ]
            for f in as_completed(futures):
                pass
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