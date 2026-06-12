"""
RAILtest stateful parser.

Recognises the RAILtest wire format and returns ParsedLine dicts.

Line types:
  Prompt      : /^[^{]*>\s*$/
  Echo        : > cmd           (board echoes the sent command)
  Single resp : {{(cmd)}{k:v}...}}
  Multi header: #{{(cmd)}{tag}...}}
  Data row    : {{v1}{v2}...}}   (follows multi header)
  Payload     : inline in rxPacket fields
  Boot seq    : reset + radio + system + pti  → grouped with boot_id
"""

from __future__ import annotations
import re, uuid
from parsers import BaseParser, _parsed, _prompt

# ── Regexes ───────────────────────────────────────────────────────────────────
_PROMPT_RE  = re.compile(r'^[^{]*>\s*$')
_ECHO_RE    = re.compile(r'^>\s+\S')
_RT_RE      = re.compile(r'^\s*#?\{\{')
_CMD_RE     = re.compile(r'\(([^)]+)\)')
_TOKEN_RE   = re.compile(r'\{([^{}]+)\}')
_HEADER_RE  = re.compile(r'^\s*#\{\{')
_END_RE     = re.compile(r'\}\}\s*$')
_BOOT_CMDS  = {"reset", "radio", "system", "pti"}
_RSSI_WARN  = -100


def _tokens(line):
    cmd = None
    m = _CMD_RE.search(line)
    if m: cmd = m.group(1)
    fields, values = {}, []
    for tok in _TOKEN_RE.findall(line):
        if tok.startswith("(") and tok.endswith(")"): continue
        if ":" in tok:
            k, _, v = tok.partition(":")
            fields[k.strip()] = v.strip()
        else:
            values.append(tok.strip())
    return cmd, fields, values


def _fmt(cmd, fields):
    if not cmd: return ""
    if cmd == "reset":
        app = fields.get("App","?"); built = fields.get("Built",""); cause = fields.get("Cause","")
        return f"Boot — {app}" + (f"  built {built}" if built else "") + (f"  cause {cause}" if cause else "")
    if cmd == "radio":
        return "radio — " + " | ".join(f"{k}: {v}" for k,v in fields.items())
    if cmd == "system":
        return "system — " + " | ".join(f"{k}: {v}" for k,v in fields.items())
    if cmd == "pti":
        return "pti — " + " | ".join(f"{k}: {v}" for k,v in fields.items())
    if cmd == "appMode":
        mode = next((v for v in fields.values() if v=="Enabled"), "?")
        pktTx = fields.get("PacketTx",""); t = fields.get("Time","")
        return f"Mode → {mode}  PacketTx:{pktTx}" + (f"  t={t}" if t else "")
    if cmd == "txEnd":
        status = fields.get("txStatus","?"); sent = fields.get("transmitted","?")
        failed = fields.get("failed","0"); t = fields.get("lastTxTime","")
        return f"TX {status}  sent:{sent}  failed:{failed}" + (f"  t={t}" if t else "")
    if cmd == "rxPacket":
        rssi = fields.get("rssi","?"); lqi = fields.get("lqi","")
        crc  = fields.get("crc","");   t   = fields.get("timeUs", fields.get("Time",""))
        parts = [f"RSSI:{rssi}"]
        if lqi: parts.append(f"LQI:{lqi}")
        if crc: parts.append(f"CRC:{crc}")
        if t:   parts.append(f"t={t}")
        return "RX packet — " + " | ".join(parts)
    if cmd == "tx":
        pktTx = fields.get("PacketTx",""); t = fields.get("Time","")
        return f"TX started — PacketTx:{pktTx}" + (f"  t={t}" if t else "")
    if cmd == "rx":
        state = fields.get("Rx","?"); t = fields.get("Time","")
        return f"RX → {state}" + (f"  t={t}" if t else "")
    if cmd == "rxAckTimeout":
        dur = fields.get("ackTimeoutDuration","")
        return "ACK timeout" + (f"  duration:{dur}" if dur else "")
    if cmd == "rxAbort":
        cause = fields.get("errorCode", fields.get("Cause",""))
        return "RX aborted" + (f"  cause:{cause}" if cause else "")
    if cmd == "assert":
        return "Assert — " + " | ".join(f"{k}:{v}" for k,v in fields.items())
    # Generic
    parts = [f"{k}: {v}" for k,v in fields.items()]
    return (f"{cmd} — " + " | ".join(parts)) if parts else cmd


def _color(cmd, fields):
    if cmd in ("assert", "rxAbort"): return "error"
    if cmd == "txEnd":
        if fields.get("txStatus") != "Complete" or int(fields.get("failed","0") or 0) > 0:
            return "error"
        return "default"
    if cmd == "rxPacket":
        if fields.get("crc","Pass") != "Pass": return "error"
        try:
            if int(fields.get("rssi","0")) < _RSSI_WARN: return "warning"
        except: pass
        return "default"
    if cmd == "rxAckTimeout": return "warning"
    return "default"


class RailtestParser(BaseParser):
    name = "railtest"

    def __init__(self):
        self._boot_id    : str | None  = None
        self._boot_fields: dict        = {}
        self._boot_lines : list[str]   = []
        self._multi_cmd  : str | None  = None
        self._multi_tags : list[str]   = []
        self._multi_rows : list        = []
        self._multi_raw  : list[str]   = []
        self._rx_pending : dict | None = None
        self._rx_raw     : str | None  = None

    def detect(self, line): return bool(_RT_RE.match(line.strip()))

    def feed(self, line: str) -> list[dict]:
        s = line.strip()
        out = []

        # ── Prompt ──────────────────────────────────────────────────────────
        if _PROMPT_RE.match(s):
            out.extend(self._flush_rx())
            out.extend(self._flush_multi())
            if self._boot_id:
                self._boot_id = None; self._boot_fields = {}; self._boot_lines = []
            return out + [_prompt(line)]

        # ── Echo (> cmd) ─────────────────────────────────────────────────────
        if _ECHO_RE.match(s):
            # May be prefixed prompt + event fused in one chunk: "> {{(appMode)..."
            rest = s[1:].strip()
            if rest.startswith("{{"):
                # Treat the RT part as a normal RT line
                return self.feed(rest)
            cmd_name = rest.split()[0] if rest else ""
            out.extend(self._flush_rx())
            return out + [_parsed("cmd", rest, line, cmd=cmd_name)]

        # ── Not RT ───────────────────────────────────────────────────────────
        if not _RT_RE.match(s):
            # Plain echo without > (second rx 0 in log — board didn't echo with >)
            # Treat as cmd echo if it looks like a bare command word
            if s and not s.startswith("{"):
                return [_parsed("cmd", s, line, cmd=s.split()[0])]
            return []

        complete = bool(_END_RE.search(s))

        # ── Multi header ─────────────────────────────────────────────────────
        if _HEADER_RE.match(s):
            out.extend(self._flush_rx())
            cmd, _, tags = _tokens(s)
            self._multi_cmd = cmd; self._multi_tags = tags
            self._multi_rows = []; self._multi_raw = [line]
            return out

        # ── Data row (inside multi) ──────────────────────────────────────────
        if self._multi_tags and complete:
            cmd, _, values = _tokens(s)
            if not cmd and values:
                self._multi_rows.append(values)
                self._multi_raw.append(line)
                return []
            # Something else — flush multi first
            out.extend(self._flush_multi())

        # ── Parse RT line ────────────────────────────────────────────────────
        cmd, fields, values = _tokens(s)

        # Boot sequence
        if cmd in _BOOT_CMDS:
            out.extend(self._flush_rx())
            if not self._boot_id:
                self._boot_id = uuid.uuid4().hex[:8]
                self._boot_fields = {}
                self._boot_lines  = []
            is_first = len(self._boot_lines) == 0
            self._boot_lines.append(line)
            self._boot_fields[cmd] = fields
            # Build current detail
            detail_parts = []
            for bc in ("reset","radio","system","pti"):
                f2 = self._boot_fields.get(bc,{})
                if f2: detail_parts.append(bc+": "+" | ".join(f"{k}: {v}" for k,v in f2.items()))
            detail = "\n".join(detail_parts) if detail_parts else None
            rf = self._boot_fields.get("reset", fields if cmd=="reset" else {})
            summary = _fmt("reset", rf) if rf else f"Boot — {cmd}"
            return out + [_parsed("boot", summary, line, cmd=cmd, color="boot",
                                  detail=detail, boot_id=self._boot_id, boot_first=is_first)]

        # Non-boot → close boot state
        if self._boot_id:
            self._boot_id = None; self._boot_fields = {}; self._boot_lines = []

        # rxPacket
        if cmd == "rxPacket":
            out.extend(self._flush_rx())
            self._rx_pending = {"cmd": cmd, "fields": fields}
            self._rx_raw = line
            if "payload" in fields:
                out.extend(self._flush_rx())
            return out

        # Close rx if open
        out.extend(self._flush_rx())

        if not cmd: return out
        if not complete: return out

        display = _fmt(cmd, fields) or s
        color   = _color(cmd, fields)
        return out + [_parsed("event", display, line, cmd=cmd, color=color)]

    # ── Flush helpers ─────────────────────────────────────────────────────────

    def _flush_rx(self):
        if not self._rx_pending: return []
        p = self._rx_pending; fields = p["fields"]
        summary = _fmt("rxPacket", fields)
        payload = fields.get("payload","")
        detail  = ("payload: " + payload) if payload else None
        color   = _color("rxPacket", fields)
        self._rx_pending = None; self._rx_raw = None
        return [_parsed("event", summary, self._rx_raw or "", cmd="rxPacket",
                        color=color, detail=detail)]

    def _flush_multi(self):
        if not self._multi_tags: return []
        cmd  = self._multi_cmd or "?"
        tags = self._multi_tags
        rows = self._multi_rows
        summary = f"{cmd} — {len(rows)} row(s)"
        if tags and rows:
            header = " | ".join(tags)
            lines  = [header, "-"*len(header)] + [" | ".join(r) for r in rows]
            detail = "\n".join(lines)
        else:
            detail = None
        self._multi_tags = []; self._multi_cmd = None
        self._multi_rows = []; self._multi_raw = []
        return [_parsed("response", summary, "", cmd=cmd, detail=detail)]

    def is_response_complete(self, lines):
        for line in reversed(lines):
            s = line.strip()
            if not s: continue
            if _HEADER_RE.match(s): return False
            if _RT_RE.match(s) and _END_RE.search(s):
                return not any(_HEADER_RE.match(l.strip()) for l in lines if l.strip())
            return False
        return False
