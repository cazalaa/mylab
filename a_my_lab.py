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

if SDM_PATH:
    config["paths"]["sdm"] = SDM_PATH
if COMMANDER_PATH:
    config["paths"]["commander"] = COMMANDER_PATH

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
    except:
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
    except:
        return []

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
        except Exception as e:
            print("Mass erase error:", e)

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
        except Exception as e:
            print("FW upgrade error:", e)

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
<style>
body { font-family: Arial; margin: 20px; }

button { cursor:pointer; padding:5px 10px; margin-left:5px; transition:0.3s; }
.success { background:#4CAF50; color:white; }

.header { display:flex; justify-content:space-between; }
#container { display:flex; gap:40px; }
.column { width:45%; }

li { display:flex; justify-content:space-between; margin:5px 0; }

.star { color:gold; }
.trash { cursor:pointer; color:red; }
</style>
</head>

<body>

<div id="container">

<div class="column">
<div class="header">
<h3>Available Boards (<span id="count">0</span>)</h3>
<button id="refreshBtn" onclick="refresh()">🔄</button>
</div>
<ul id="list"></ul>
</div>

<div class="column" id="rightColumn">
<div class="header">
<h3>Selected Boards</h3>
<div>
<button onclick="addAll()">ALL</button>
<button id="eraseBtn" onclick="massErase()">🧹</button>
<button id="fwBtn" onclick="upgradeFW()">⬆️</button>
</div>
</div>
<ul id="selected"></ul>
</div>

</div>

<script>
let selected = new Set();
let cache = [];

// ----------------------------
async function runWithFeedback(btn, action){
    btn.innerText="⏳";
    btn.classList.remove("success");

    await action();

    btn.innerText="✔";
    btn.classList.add("success");

    setTimeout(()=>{
        btn.classList.remove("success");
        btn.innerText=btn.dataset.icon;
    },3000);
}

// ----------------------------
async function refresh(){
 let btn=document.getElementById("refreshBtn");
 btn.dataset.icon="🔄";

 await runWithFeedback(btn, async ()=>{
  await fetch('/scan',{method:'POST'});
  await new Promise(r=>setTimeout(r,1500));
  await load();
 });
}

// ----------------------------
async function load(){
 let res = await fetch('/adapters');
 let data = await res.json();

 cache = data;
 document.getElementById("count").innerText=data.length;

 let list=document.getElementById("list");
 list.innerHTML="";

 data.forEach(a=>{
  let li=document.createElement("li");

  let left=document.createElement("span");
  left.innerText=a.serialNumber+" - "+a.boardId;

  let right=document.createElement("span");

  if(selected.has(a.serialNumber)){
    let star=document.createElement("span");
    star.className="star";
    star.innerText="★";
    right.appendChild(star);
  }

  li.appendChild(left);
  li.appendChild(right);

  li.draggable=true;
  li.ondragstart=e=>e.dataTransfer.setData("t",JSON.stringify(a));

  list.appendChild(li);
 });
}

// ----------------------------
function addBoard(a){
 if(selected.has(a.serialNumber)) return;

 selected.add(a.serialNumber);

 let li=document.createElement("li");

 let txt=document.createElement("span");
 txt.innerText=a.serialNumber+" - "+a.boardId;

 let trash=document.createElement("span");
 trash.className="trash";
 trash.innerText="🗑";

 trash.onclick=()=>{
  selected.delete(a.serialNumber);
  li.remove();
  load();
 };

 li.appendChild(txt);
 li.appendChild(trash);

 document.getElementById("selected").appendChild(li);

 load();
}

function addAll(){
 cache.forEach(addBoard);
}

// ----------------------------
async function massErase(){
 let btn=document.getElementById("eraseBtn");
 btn.dataset.icon="🧹";

 await runWithFeedback(btn, async ()=>{
  let list = cache.filter(a=>selected.has(a.serialNumber));

  await fetch("/mass_erase",{
    method:"POST",
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({adapters:list})
  });
 });
}

// ----------------------------
async function upgradeFW(){
 let btn=document.getElementById("fwBtn");
 btn.dataset.icon="⬆️";

 await runWithFeedback(btn, async ()=>{
  let list = cache.filter(a=>selected.has(a.serialNumber));

  await fetch("/fw_upgrade",{
    method:"POST",
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({adapters:list})
  });
 });
}

// ----------------------------
window.onload=()=>{
 let right=document.getElementById("rightColumn");

 right.ondragover=e=>e.preventDefault();
 right.ondrop=e=>{
  let d=JSON.parse(e.dataTransfer.getData("t"));
  addBoard(d);
 };

 load();
};
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
            "connectivityType": a.get("connectivityType"),
            "host": a.get("host")
        }
        for a in data if a.get("serialNumber")
    ])

# ----------------------------
if __name__ == "__main__":
    ensure_sdm()
    app.run(port=WEB_PORT, debug=True)