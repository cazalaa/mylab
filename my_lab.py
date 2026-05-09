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

GROUPS_DIR = os.path.dirname(os.path.abspath(__file__))

# Dimensions
WIN_W          = 800
WIN_COLLAPSED  = 72    # hauteur barre compacte
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

        # Check if connections are empty → TTY mode
        tty_mode = False
        try:
            r2    = requests.get(f"{SDM_BASE}/api/adapter/{serial}/connections", timeout=2)
            conns = r2.json()
            tty_mode = not conns.get("serial1", {}).get("portNumber")
        except:
            pass

        result.append({
            "serialNumber":   serial,
            "boardId":        extract_board(a),
            "boardLabel":     extract_board_label(a),
            "connectivityType": a.get("connectivityType"),
            "host":           a.get("host"),
            "label":          a.get("label", ""),
            "nickname":       a.get("nickname", ""),
            "ttyMode":        tty_mode,
        })

    return jsonify(result)

# ── terminal window API ────────────────────────────
class WindowAPI:
    def set_height(self, height):
        webview.windows[0].resize(WIN_W, height)

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

        # Ports dynamiques via /connections
        r2    = requests.get(f"{SDM_BASE}/api/adapter/{serial}/connections", timeout=3)
        conns = r2.json()

        vcom_port  = conns.get("serial1", {}).get("portNumber")
        admin_port = conns.get("admin",   {}).get("portNumber")

        # Si pas de ports réseau → chercher TTY USB
        tty_path = None
        if not vcom_port and connectivity == "usb":
            # Chercher /dev/tty.usbmodem* ou /dev/cu.usbmodem* contenant le serial
            candidates = (
                glob_module.glob("/dev/tty.usbmodem*") +
                glob_module.glob("/dev/cu.usbmodem*")
            )
            for c in candidates:
                if serial.lower() in c.lower():
                    tty_path = c
                    break
            # Fallback: chercher via pyserial
            if not tty_path:
                for port in serial_tools.list_ports.comports():
                    if serial.lower() in (port.serial_number or "").lower():
                        tty_path = port.device
                        break

        return jsonify({
            "serial":       serial,
            "host":         host,
            "vcom":         vcom_port,
            "admin":        admin_port,
            "tty":          tty_path,   # None si ethernet/réseau
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