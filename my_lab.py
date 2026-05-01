import subprocess
import time
import requests
import configparser
import os
from flask import Flask, jsonify, render_template_string, request

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

    for a in adapters:
        try:
            if a["connectivityType"] == "usb":
                cmd = [COMMANDER_PATH, "device", "masserase", "--serialno", a["serialNumber"]]
            else:
                cmd = [COMMANDER_PATH, "device", "masserase", "--ip", a["host"]]

            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] mass_erase {a.get('serialNumber')}: exit {e.returncode}")
        except Exception as e:
            print(f"[ERROR] mass_erase unexpected: {e}")

    return "OK"


@app.route("/fw_upgrade", methods=["POST"])
def fw_upgrade():
    adapters = request.json.get("adapters", [])

    for a in adapters:
        try:
            if a["connectivityType"] == "usb":
                cmd = [COMMANDER_PATH, "adapter", "fwupgrade", "-s", a["serialNumber"]]
            else:
                cmd = [COMMANDER_PATH, "adapter", "fwupgrade", "--ip", a["host"]]

            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] fw_upgrade {a.get('serialNumber')}: exit {e.returncode}")
        except Exception as e:
            print(f"[ERROR] fw_upgrade unexpected: {e}")

    return "OK"

# ----------------------------
# UI
# ----------------------------
@app.route("/")
def index():
    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>My Lab</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: 'IBM Plex Sans', sans-serif;
    background: #f4f3f0;
    color: #1a1a1a;
    min-height: 100vh;
    padding: 28px;
}

/* ── topbar ── */
.topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
}
.topbar-left { display: flex; align-items: baseline; gap: 10px; }
.app-title { font-size: 18px; font-weight: 500; letter-spacing: -.3px; }
.count-badge {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    background: #1a1a1a;
    color: #f4f3f0;
    padding: 2px 8px;
    border-radius: 20px;
}
.topbar-actions { display: flex; gap: 8px; align-items: center; }

/* ── buttons ── */
button {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 12px;
    font-weight: 500;
    height: 34px;
    padding: 0 14px;
    border: 1px solid #d0cfc9;
    border-radius: 8px;
    background: #fff;
    color: #1a1a1a;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    transition: background .15s, border-color .15s, transform .1s;
}
button:hover { background: #ece9e3; border-color: #b0afa9; }
button:active { transform: scale(.97); }
button.icon-only { width: 34px; padding: 0; justify-content: center; font-size: 14px; }
button.primary { background: #1a1a1a; color: #f4f3f0; border-color: #1a1a1a; }
button.primary:hover { background: #333; }
button.success { background: #1D9E75; color: #E1F5EE; border-color: #1D9E75; }

/* ── grid ── */
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* ── panels ── */
.panel {
    background: #fff;
    border: 1px solid #e0dfd9;
    border-radius: 14px;
    overflow: hidden;
}
.panel-header {
    padding: 12px 16px;
    border-bottom: 1px solid #e0dfd9;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #faf9f7;
}
.panel-title {
    font-size: 11px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: #888;
}
.panel-body { padding: 10px; min-height: 120px; }

/* ── board cards ── */

.board-serial-line {
    display: flex;
    align-items: baseline;
    gap: 4px;
}
.board-nickname {
    font-size: 11px;
    color: #1D9E75;
    font-weight: 500;
}
                                  
.board-label {
    font-size: 11px;
    color: #555;
    margin-top: 1px;
}
.board-host {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px;
    color: #185FA5;
    margin-top: 1px;
}
                                                                                                      
.board-card {
    border: 1px solid #e0dfd9;
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: grab;
    transition: border-color .15s, box-shadow .15s;
    background: #fff;
}
.board-card:hover { border-color: #aaa; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.board-card:last-child { margin-bottom: 0; }
.board-info { display: flex; flex-direction: column; gap: 2px; }
.board-serial {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    font-weight: 500;
    color: #1a1a1a;
}
.board-id { font-size: 11px; color: #888; }
.board-right { display: flex; align-items: center; gap: 8px; }
.pill {
    font-size: 10px;
    font-weight: 500;
    padding: 2px 8px;
    border-radius: 20px;
    text-transform: uppercase;
    letter-spacing: .04em;
}
.pill-usb { background: #E6F1FB; color: #185FA5; }
.pill-ip  { background: #FAEEDA; color: #854F0B; }
.star-icon { color: #f5c842; font-size: 13px; }

/* ── selected cards ── */
.sel-card {
    border: 1px solid #e0dfd9;
    border-radius: 10px;
    padding: 9px 12px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #fff;
}
.sel-card:last-child { margin-bottom: 0; }
.sel-serial {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 12px;
    color: #1a1a1a;
}
.sel-id { font-size: 11px; color: #888; margin-top: 2px; }
.remove-btn {
    width: 22px; height: 22px; padding: 0; border-radius: 6px;
    border: 1px solid #e0dfd9; background: #faf9f7;
    font-size: 12px; color: #999; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
    transition: background .15s, color .15s;
}
.remove-btn:hover { background: #FCEBEB; color: #A32D2D; border-color: #F09595; }

/* ── drop zone ── */
.drop-hint {
    text-align: center;
    font-size: 12px;
    color: #bbb;
    padding: 28px 0;
    border: 1.5px dashed #ddd;
    border-radius: 10px;
}
.panel-body.drag-over { background: #f0f8f5; }
</style>
</head>
<body>

<div class="topbar">
    <div class="topbar-left">
        <span class="app-title">My Lab</span>
        <span class="count-badge" id="count">0 adapters</span>
    </div>
    <div class="topbar-actions">
        <button class="icon-only" id="refreshBtn" onclick="refresh()" title="Refresh" data-icon="↺">↺</button>
        <button onclick="addAll()">All</button>
        <button id="eraseBtn" onclick="massErase()" data-icon="Erase">Erase</button>
        <button id="fwBtn" onclick="upgradeFW()" class="primary" data-icon="↑ FW">↑ FW</button>
    </div>
</div>

<div class="grid">
    <div class="panel">
        <div class="panel-header">
            <span class="panel-title">Available</span>
        </div>
        <div class="panel-body" id="list"></div>
    </div>

    <div class="panel">
        <div class="panel-header">
            <span class="panel-title">Selected</span>
        </div>
        <div class="panel-body" id="selected" ondragover="event.preventDefault()" ondrop="onDrop(event)">
            <div class="drop-hint" id="dropHint">Drag adapters here or click All</div>
        </div>
    </div>
</div>

<script>
let selected = new Set();
let cache = [];

async function runWithFeedback(btn, label, action) {
    const icon = btn.dataset.icon;
    btn.innerText = "…";
    btn.classList.remove("success");
    await action();
    btn.innerText = "✓";
    btn.classList.add("success");
    setTimeout(() => { btn.classList.remove("success"); btn.innerText = icon; }, 3000);
}

async function refresh() {
    const btn = document.getElementById("refreshBtn");
    await runWithFeedback(btn, "↺", async () => {
        await fetch('/scan', { method: 'POST' });
        await new Promise(r => setTimeout(r, 1500));
        await load();
    });
}

async function load() {
    const res = await fetch('/adapters');
    cache = await res.json();

    const n = cache.length;
    document.getElementById("count").innerText = n + " adapter" + (n !== 1 ? "s" : "");

    const list = document.getElementById("list");
    list.innerHTML = "";

    if (cache.length === 0) {
        list.innerHTML = '<div class="drop-hint">No adapters found</div>';
        return;
    }

    cache.forEach(a => {
        const card = document.createElement("div");
        card.className = "board-card";
        card.draggable = true;
        card.ondragstart = e => e.dataTransfer.setData("t", JSON.stringify(a));
        card.onclick = () => addBoard(a);

        const typeClass = a.connectivityType === "usb" ? "pill-usb" : "pill-ip";

        const isIP = a.connectivityType !== "usb";
        const hostLine = isIP && a.host ? `<span class="board-host">${a.host}</span>` : "";
        const nicknameSpan = a.nickname ? `<span class="board-nickname"> — ${a.nickname}</span>` : "";
        const secondLine = [a.boardLabel, a.label].filter(Boolean).join(" · ");

        card.innerHTML = `
            <div class="board-info">
                <div class="board-serial-line">
                    <span class="board-serial">${a.serialNumber}</span>
                    ${nicknameSpan}
                </div>
                <span class="board-label">${secondLine}</span>
                ${hostLine}
            </div>
            <div class="board-right">
                <span class="pill ${typeClass}">${a.connectivityType || "?"}</span>
                ${selected.has(a.serialNumber) ? '<span class="star-icon">★</span>' : ''}
            </div>`;
        list.appendChild(card);
    });
}

function renderSelected() {
    const container = document.getElementById("selected");
    const hint = document.getElementById("dropHint");
    container.querySelectorAll(".sel-card").forEach(e => e.remove());

    if (selected.size === 0) {
        hint.style.display = "";
        return;
    }
    hint.style.display = "none";

    cache.filter(a => selected.has(a.serialNumber)).forEach(a => {
        const card = document.createElement("div");
        card.className = "sel-card";
        const secondLine = [a.boardLabel, a.label].filter(Boolean).join(" · ");
        const hostSuffix = a.host && a.connectivityType !== "usb" ? ` — ${a.host}` : "";

        card.innerHTML = `
            <div>
                <div class="sel-serial">${a.serialNumber}${a.nickname ? ` — ${a.nickname}` : ""}</div>
                <div class="sel-id">${secondLine}${hostSuffix}</div>
            </div>
            <button class="remove-btn" title="Remove">✕</button>`;
        card.querySelector(".remove-btn").onclick = () => {
            selected.delete(a.serialNumber);
            renderSelected();
            load();
        };
        container.appendChild(card);
    });
}

function addBoard(a) {
    if (selected.has(a.serialNumber)) return;
    selected.add(a.serialNumber);
    renderSelected();
    load();
}

function addAll() { cache.forEach(addBoard); }

function onDrop(e) {
    const a = JSON.parse(e.dataTransfer.getData("t"));
    addBoard(a);
}

async function massErase() {
    const btn = document.getElementById("eraseBtn");
    await runWithFeedback(btn, "Erase", async () => {
        const list = cache.filter(a => selected.has(a.serialNumber));
        await fetch("/mass_erase", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ adapters: list })
        });
    });
}

async function upgradeFW() {
    const btn = document.getElementById("fwBtn");
    await runWithFeedback(btn, "↑ FW", async () => {
        const list = cache.filter(a => selected.has(a.serialNumber));
        await fetch("/fw_upgrade", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ adapters: list })
        });
    });
}

window.onload = () => load();
</script>
</body>
</html>
""")

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