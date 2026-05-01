import subprocess
import time
import requests
import configparser
import os
from flask import Flask, jsonify, render_template, request 

from binresolve import resolve_sdm, resolve_commander

CONFIG_FILE = "config.ini"

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


# ----------------------------
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

# ----------------------------
if __name__ == "__main__":
    ensure_sdm()
    app.run(port=WEB_PORT, debug=True)