"""
Pretty-printer for RAILtest output.

Two public entry points:
  _fmt_rt(struct)   -> (display, color)   : one {{...}} structure -> human line
  format_block(blk) -> display dict       : a parser block -> ready-to-render block

A parser block (input):
  {"kind": "cmd",   "cmd": "rx 1", "lines": ["{{...}}", "plain", ...], "ts": "..."}
  {"kind": "event", "lines": ["{{...}}"],                              "ts": "..."}
  {"kind": "boot",  "lines": ["{{(reset)...}}", ...],                  "ts": "..."}

A display block (output, sent verbatim to the browser):
  {
    "kind": "cmd" | "event" | "boot",
    "ts":    "12:34:56.789",
    "title":  "rx 1",                 # header text
    "subtitle": "RX -> 1  t=...",     # optional one-line summary (cmd only)
    "color": "default|error|warning|success|boot",
    "lines": [ {"text": "...", "color": "..."} ],   # expandable detail (may be empty)
  }
"""
import re

_CMD_RE   = re.compile(r'\(([A-Za-z_]\w*)\)')
_TOKEN_RE = re.compile(r'\{([^{}]+)\}')

# severity ranking — higher wins when colouring a whole block
_SEVERITY = {"default": 0, "boot": 1, "success": 2, "warning": 3, "error": 4}


def _worse(a, b):
    return a if _SEVERITY.get(a, 0) >= _SEVERITY.get(b, 0) else b


def _is_struct(s):
    return s.startswith("{") or s.startswith("#{")


def struct_name(struct):
    """Return the RAILtest command name inside a {{(name)...}} structure."""
    m = _CMD_RE.search(struct)
    if m:
        return m.group(1)
    m = re.match(r'^\{+\s*(\w+)', struct)
    return m.group(1) if m else None


def _tokens(line):
    cmd = struct_name(line)
    fields, values = {}, []
    for tok in _TOKEN_RE.findall(line):
        if ':' in tok:
            k, _, v = tok.partition(':')
            fields[k.strip()] = v.strip()
        else:
            values.append(tok.strip())
    return cmd, fields, values


def _fmt_rt(line):
    """Pretty-print a single {{...}} structure -> (display, color)."""
    cmd, fields, values = _tokens(line)
    if not cmd:
        return line.strip(), 'default'

    color = 'default'
    if cmd == 'rx':
        d = f"RX -> {fields.get('Rx', '?')}" + (f"  t={fields['Time']}" if 'Time' in fields else "")
    elif cmd == 'tx':
        d = f"TX started  PacketTx:{fields.get('PacketTx', '')}" + (f"  t={fields['Time']}" if 'Time' in fields else "")
    elif cmd == 'txEnd':
        status = fields.get('txStatus', '?')
        sent   = fields.get('transmitted', '?')
        failed = fields.get('failed', '0')
        d = f"TX {status}  sent:{sent}  failed:{failed}" + (f"  t={fields['lastTxTime']}" if 'lastTxTime' in fields else "")
        try:
            if status != 'Complete' or int(failed or 0) > 0:
                color = 'error'
        except ValueError:
            pass
    elif cmd == 'appMode':
        mode = next((v for v in fields.values() if v in ('Enabled', 'Disabled')), '?')
        d = f"Mode -> {mode}  PacketTx:{fields.get('PacketTx', '')}"
    elif cmd == 'rxPacket':
        rssi = fields.get('rssi', '?')
        crc  = fields.get('crc', '')
        t    = fields.get('timeUs', fields.get('Time', ''))
        d = f"RX packet  RSSI:{rssi}" + (f"  CRC:{crc}" if crc else "") + (f"  t={t}" if t else "")
        if crc and crc != 'Pass':
            color = 'error'
        else:
            color = 'success'
    elif cmd == 'rxAckTimeout':
        d, color = 'ACK timeout', 'warning'
    elif cmd == 'rxAbort':
        d = 'RX aborted' + (f"  cause:{fields['errorCode']}" if 'errorCode' in fields else "")
        color = 'error'
    elif cmd == 'assert':
        d = 'Assert  ' + ' | '.join(f"{k}:{v}" for k, v in fields.items())
        color = 'error'
    elif cmd == 'reset':
        d = f"Boot -> {fields.get('App', '?')}" + (f"  built:{fields['Built']}" if 'Built' in fields else "") + (f"  cause:{fields['Cause']}" if 'Cause' in fields else "")
        color = 'boot'
    elif cmd in ('radio', 'system', 'pti'):
        d = f"{cmd}  " + ' | '.join(f"{k}:{v}" for k, v in fields.items())
        color = 'boot'
    elif cmd == 'getmemw':
        d = f"getmemw  {len(values)} value(s)"
    else:
        parts = [f"{k}:{v}" for k, v in fields.items()]
        d = (f"{cmd}  " + ' | '.join(parts)) if parts else cmd

    return d, color


def _pretty_lines(raws):
    """Turn raw lines into [{text,color,raw}], tracking worst colour."""
    out, worst = [], 'default'
    for raw in raws:
        s = raw.strip()
        if not s:
            continue
        if _is_struct(s):
            disp, color = _fmt_rt(s)
        else:
            disp, color = s, 'default'
        out.append({"text": disp, "color": color, "raw": s})
        worst = _worse(worst, color)
    return out, worst


def format_block(block):
    kind = block.get("kind", "event")
    ts   = block.get("ts", "")

    if kind == "cmd":
        # The command alone — shown immediately, before its reply arrives.
        return {
            "kind": "cmd",
            "ts": ts,
            "title": block.get("cmd", "?"),
            "subtitle": "",
            "color": "default",
            "lines": [],
        }

    if kind == "response":
        pl, worst = _pretty_lines(block.get("lines", []))
        title = pl[0]["text"] if pl else ""
        return {
            "kind": "response",
            "ts": ts,
            "title": title,
            "subtitle": (f"{len(pl)} lignes" if len(pl) > 1 else ""),
            "color": worst,
            "lines": [{"text": p["text"], "color": p["color"]} for p in pl],
        }

    if kind == "boot":
        pl, _ = _pretty_lines(block.get("lines", []))
        app = cause = ""
        for raw in block.get("lines", []):
            if struct_name(raw) == "reset":
                _, fields, _ = _tokens(raw)
                app, cause = fields.get("App", app), fields.get("Cause", cause)
        title = "Boot" + (f" -> {app}" if app else "")
        return {
            "kind": "boot",
            "ts": ts,
            "title": title,
            "subtitle": (f"cause {cause}" if cause else ""),
            "color": "boot",
            "lines": [{"text": p["text"], "color": "boot"} for p in pl],
        }

    # event
    pl, worst = _pretty_lines(block.get("lines", []))
    title = pl[0]["text"] if pl else ""
    # detail = raw structure(s) when they carry more than the one-line summary
    detail = []
    for p in pl:
        if p["raw"] != p["text"]:
            detail.append({"text": p["raw"], "color": p["color"]})
    return {
        "kind": "event",
        "ts": ts,
        "title": title,
        "subtitle": "",
        "color": worst,
        "lines": detail,
    }
