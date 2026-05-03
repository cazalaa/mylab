import subprocess
import time
import requests
import configparser
import os
from flask import Flask, jsonify, render_template, request 
import json
import glob
import webview
import threading

GROUPS_DIR = os.path.dirname(os.path.abspath(__file__))

# Dimensions
WIN_W          = 800
WIN_COLLAPSED  = 82    # hauteur barre compacte
WIN_EXPANDED   = 700   # hauteur dépliée

from binresolve import resolve_sdm, resolve_commander

CONFIG_FILE = "config.ini"
ALLOWED_PAGES = {"maintenance", "test"}  # whitelist — ajoutez ici vos futures pages

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
@app.route("/mass_erase", methods=["POST"])
def mass_erase():
    adapters = request.json.get("adapters", [])
    results = []

    for a in adapters:
        try:
            if a["connectivityType"] == "usb":
                cmd = [COMMANDER_PATH, "device", "masserase", "--serialno", a["serialNumber"]]
            else:
                cmd = [COMMANDER_PATH, "device", "masserase", "--ip", a["host"]]
            subprocess.run(cmd, check=True)
            results.append({"serialNumber": a["serialNumber"], "ok": True})
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] mass_erase {a.get('serialNumber')}: exit {e.returncode}")
            results.append({"serialNumber": a["serialNumber"], "ok": False})
        except Exception as e:
            print(f"[ERROR] mass_erase unexpected: {e}")
            results.append({"serialNumber": a["serialNumber"], "ok": False})

    return jsonify(results)


@app.route("/fw_upgrade", methods=["POST"])
def fw_upgrade():
    adapters = request.json.get("adapters", [])
    results = []

    for a in adapters:
        try:
            if a["connectivityType"] == "usb":
                cmd = [COMMANDER_PATH, "adapter", "fwupgrade", "-s", a["serialNumber"]]
            else:
                cmd = [COMMANDER_PATH, "adapter", "fwupgrade", "--ip", a["host"]]
            subprocess.run(cmd, check=True)
            results.append({"serialNumber": a["serialNumber"], "ok": True})
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] fw_upgrade {a.get('serialNumber')}: exit {e.returncode}")
            results.append({"serialNumber": a["serialNumber"], "ok": False})
        except Exception as e:
            print(f"[ERROR] fw_upgrade unexpected: {e}")
            results.append({"serialNumber": a["serialNumber"], "ok": False})

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

    return jsonify([
        {
            "serialNumber": a.get("serialNumber"),
            "boardId": extract_board(a),
            "boardLabel": extract_board_label(a),
            "connectivityType": a.get("connectivityType"),
            "host": a.get("host"),
            "label": a.get("label", ""),
            "nickname": a.get("nickname", ""),
        }
        for a in data if a.get("serialNumber")
    ])

class WindowAPI:
    def set_height(self, height):
        webview.windows[0].resize(WIN_W, height)

# ----------------------------
if __name__ == "__main__":
    ensure_sdm()
    t = threading.Thread(target=lambda: app.run(port=WEB_PORT, debug=False, use_reloader=False))
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