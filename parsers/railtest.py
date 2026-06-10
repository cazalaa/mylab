r"""
RAILtest stateful parser.

Wire format:
  Single response : {{(cmd)}{k:v}...}}         — complete on }}
  Multi  response : #{{(cmd)}{tag1}...}}        — header
                    {{v1}{v2}...}}              — data rows (N >= 1)
  Event           : {{(name)}{k:v}...}}         — spontaneous
  Payload         : {v1}{v2}...}}               — follows rxPacket (no (cmd))
  Prompt          : /^[^{]*>\s*$/               — flushes pending group

Group rules (applied in feed() order):
  1. BOOT sequence  reset+radio+system+pti → single "boot" group
  2. rxPacket+payload → single "rx" event group
  3. All other single-line events → immediate group
  4. Multi-response (header + data rows) → flushed at prompt
  5. Unknown lines → passed through

Categories and colors:
  boot     → "boot"    (blue-ish background)
  rx       → "rx"      default
  tx/txEnd → "tx"      default
  error    → "error"   (red)  — txStatus≠Complete, CRC:Fail, assert, rxAbort
  warning  → "warning" (amber) — rxAckTimeout, RSSI below threshold
  mode     → "info"    default
"""

from __future__ import annotations
import re
from parsers import BaseParser, make_group

# ── Regexes ───────────────────────────────────────────────────────────────────

_CMD_RE     = re.compile(r'\(([^)]+)\)')
_TOKEN_RE   = re.compile(r'\{([^{}]+)\}')
_RT_LINE    = re.compile(r'^\s*#?\{\{')
_PROMPT_RE  = re.compile(r'^[^{]*>\s*$')
_HEADER_RE  = re.compile(r'^\s*#\{\{')
_SINGLE_END = re.compile(r'\}\}\s*$')
# Payload: starts with { but no (cmd), ends with }}
# Values are signed ints, hex (0x…), or plain words
_PAYLOAD_RE = re.compile(r'^\{(?!\{?\([a-zA-Z]).*\}\}\s*$')

# ── Boot sequence cmds (emitted right after reset) ────────────────────────────
_BOOT_CMDS = {"reset", "radio", "system", "pti"}

# ── RSSI warning threshold (dBm) ─────────────────────────────────────────────
_RSSI_WARN = -100


def _parse_tokens(line: str):
    """Return (cmd, fields_dict, bare_values_list)."""
    cmd    = None
    fields = {}
    values = []
    m = _CMD_RE.search(line)
    if m:
        cmd = m.group(1)
    for tok in _TOKEN_RE.findall(line):
        if tok.startswith("(") and tok.endswith(")"):
            continue
        if ":" in tok:
            k, _, v = tok.partition(":")
            fields[k.strip()] = v.strip()
        else:
            values.append(tok.strip())
    return cmd, fields, values


def _color_for(cmd: str, fields: dict) -> str:
    """Derive a display color from the parsed command and its fields."""
    if cmd == "assert":
        return "error"
    if cmd == "rxAbort":
        return "error"
    if cmd == "txEnd":
        status = fields.get("txStatus", "")
        failed = int(fields.get("failed", "0") or "0")
        if status != "Complete" or failed > 0:
            return "error"
        return "success"
    if cmd == "rxPacket":
        crc  = fields.get("crc", "Pass")
        rssi = fields.get("rssi", "0")
        if crc != "Pass":
            return "error"
        try:
            if int(rssi) < _RSSI_WARN:
                return "warning"
        except ValueError:
            pass
        return "default"
    if cmd == "rxAckTimeout":
        return "warning"
    return "default"


def _category_for(cmd: str) -> str | None:
    mapping = {
        "reset":  "boot", "radio": "boot", "system": "boot", "pti": "boot",
        "rx":     "rx",   "rxPacket": "rx", "rxAbort": "rx", "rxAckTimeout": "rx",
        "tx":     "tx",   "txEnd": "tx",
        "appMode": "mode",
    }
    return mapping.get(cmd)


# ── Pretty formatting ─────────────────────────────────────────────────────────

def _fmt(cmd: str | None, fields: dict, raw: str) -> str:
    """Human-readable one-line summary for a parsed RAILtest line."""
    if not cmd:
        return raw

    if cmd == "reset":
        app   = fields.get("App", "?")
        built = fields.get("Built", "")
        cause = fields.get("Cause", "")
        extra = (f"  built {built}" if built else "") + (f"  cause {cause}" if cause else "")
        return f"↺ Reset — {app}{extra}"

    if cmd == "appMode":
        mode  = next((v for v in fields.values() if v == "Enabled"), "?")
        pktTx = fields.get("PacketTx", "")
        t     = fields.get("Time", "")
        return f"◉ Mode → {mode}  PacketTx:{pktTx}" + (f"  t={t}" if t else "")

    if cmd == "txEnd":
        status = fields.get("txStatus", "?")
        sent   = fields.get("transmitted", "?")
        failed = fields.get("failed", "0")
        t      = fields.get("lastTxTime", "")
        ok     = "✓" if status == "Complete" and failed == "0" else "✗"
        return f"↑ TX {ok} {status}  sent:{sent}  failed:{failed}" + (f"  t={t}" if t else "")

    if cmd == "rxPacket":
        rssi = fields.get("rssi", "?")
        lqi  = fields.get("lqi", "")
        crc  = fields.get("crc", "")
        t    = fields.get("timeUs", fields.get("Time", ""))
        payload = fields.get("payload", "")
        parts = [f"RSSI:{rssi}"]
        if lqi:  parts.append(f"LQI:{lqi}")
        if crc:  parts.append(f"CRC:{crc}")
        if t:    parts.append(f"t={t}")
        summary = "↓ RX packet — " + " | ".join(parts)
        return summary

    if cmd == "tx":
        pktTx = fields.get("PacketTx", "")
        t     = fields.get("Time", "")
        return f"↑ TX started — PacketTx:{pktTx}" + (f"  t={t}" if t else "")

    if cmd == "rx":
        state = fields.get("Rx", "?")
        t     = fields.get("Time", "")
        return f"⟳ RX → {state}" + (f"  t={t}" if t else "")

    if cmd == "rxAckTimeout":
        dur = fields.get("ackTimeoutDuration", "")
        return f"⚠ ACK timeout" + (f"  duration:{dur}" if dur else "")

    if cmd == "rxAbort":
        cause = fields.get("errorCode", fields.get("Cause", ""))
        return f"✗ RX aborted" + (f"  cause:{cause}" if cause else "")

    if cmd == "assert":
        msg = " | ".join(f"{k}:{v}" for k, v in fields.items())
        return f"‼ Assert — {msg}"

    # Generic fallback
    label = cmd
    parts = [f"{k}: {v}" for k, v in fields.items()]
    return (f"{label} — " + " | ".join(parts)) if parts else label


def _fmt_payload(values: list[str]) -> str:
    """Format payload values as hex bytes."""
    return "  payload: " + " ".join(values)


# ── RailtestParser ────────────────────────────────────────────────────────────

class RailtestParser(BaseParser):
    name = "railtest"

    def __init__(self):
        self._reset()

    def _reset(self):
        # Boot sequence accumulator
        self._boot_lines:    list[str] = []
        self._boot_fields:   dict      = {}  # aggregated fields from boot cmds
        self._in_boot:       bool      = False

        # rxPacket accumulator (waits for payload line)
        self._rx_pending:    dict | None = None   # parsed rxPacket
        self._rx_raw:        str  | None = None

        # Multi-response accumulator
        self._multi_tags:    list[str]  = []
        self._multi_cmd:     str | None = None
        self._multi_rows:    list[str]  = []
        self._multi_raw:     list[str]  = []

    # ── detection ────────────────────────────────────────────────────────────

    def detect(self, line: str) -> bool:
        return bool(_RT_LINE.match(line.strip()))

    # ── stateful feed interface ───────────────────────────────────────────────

    def feed(self, line: str) -> list[dict]:
        """
        Process one complete line.  Returns ready groups (may be empty).
        Prompt flushes all pending accumulators.
        """
        stripped = line.strip()
        groups   = []

        # ── Prompt ──────────────────────────────────────────────────────────
        if _PROMPT_RE.match(stripped):
            groups.extend(self.flush())
            return groups

        # ── Not a RAILtest line ──────────────────────────────────────────────
        if not _RT_LINE.match(stripped) and not _PAYLOAD_RE.match(stripped):
            # Flush rx_pending if we had one (no payload came)
            if self._rx_pending:
                groups.append(self._close_rx())
            return groups

        # ── Multi-response header ────────────────────────────────────────────
        if _HEADER_RE.match(stripped):
            # Flush any open rx first
            if self._rx_pending:
                groups.append(self._close_rx())
            cmd, _, tags = _parse_tokens(stripped)
            self._multi_cmd  = cmd
            self._multi_tags = tags
            self._multi_rows = []
            self._multi_raw  = [line]
            return groups

        # ── Multi-response data row ──────────────────────────────────────────
        if self._multi_tags:
            cmd, _, values = _parse_tokens(stripped)
            if not cmd and values:
                self._multi_rows.append(values)
                self._multi_raw.append(line)
                return groups
            # Something else came — close multi first
            groups.append(self._close_multi())

        # ── Parse the RAILtest line ──────────────────────────────────────────
        cmd, fields, values = _parse_tokens(stripped)

        # Payload line (no cmd, values only, follows rxPacket)
        if not cmd and values and _PAYLOAD_RE.match(stripped):
            if self._rx_pending:
                groups.append(self._close_rx(payload_values=values, payload_raw=line))
            # else: orphan payload — ignore
            return groups

        if not cmd:
            # Unknown RT line — flush rx if open, pass through
            if self._rx_pending:
                groups.append(self._close_rx())
            return groups

        # ── Boot sequence ────────────────────────────────────────────────────
        if cmd in _BOOT_CMDS:
            # Flush any open rx first
            if self._rx_pending:
                groups.append(self._close_rx())
            if not self._in_boot:
                self._in_boot     = True
                self._boot_lines  = []
                self._boot_fields = {}
                import uuid as _uuid
                self._boot_id = str(_uuid.uuid4())[:8]
            self._boot_lines.append(line)
            self._boot_fields[cmd] = fields
            # Emit a per-line group immediately so JS can display the boot block
            # and suppress the raw line. The first line creates the block;
            # subsequent lines carry the same boot_id so JS can update the detail.
            is_first = len(self._boot_lines) == 1
            # Build current summary (from reset if available)
            rf = self._boot_fields.get("reset", fields if cmd == "reset" else {})
            app   = rf.get("App", "?")
            built = rf.get("Built", "")
            cause = rf.get("Cause", "")
            summary = f"↺ Boot — {app}" + (f"  built {built}" if built else "") + (f"  cause {cause}" if cause else "")
            # Build current detail from all lines so far
            detail_parts = []
            for bc in ("reset", "radio", "system", "pti"):
                f2 = self._boot_fields.get(bc, {})
                if f2:
                    detail_parts.append(bc + ": " + " | ".join(f"{k}: {v}" for k, v in f2.items()))
            detail = "\n".join(detail_parts) if detail_parts else None
            g = make_group(
                type_    = "boot",
                summary  = summary,
                detail   = detail,
                raw      = [line],
                category = "boot",
                color    = "boot",
                cmd      = cmd,
            )
            g["boot_id"]   = self._boot_id
            g["boot_first"] = is_first
            groups.append(g)
            return groups

        # Non-boot cmd → close boot state (already emitted line-by-line)
        if self._in_boot:
            self._in_boot     = False
            self._boot_lines  = []
            self._boot_fields = {}

        # ── rxPacket ─────────────────────────────────────────────────────────
        if cmd == "rxPacket":
            # Flush previous rx if still pending
            if self._rx_pending:
                groups.append(self._close_rx())
            self._rx_pending = {"cmd": cmd, "fields": fields}
            self._rx_raw     = line
            # If payload is inline in the fields, close immediately
            if "payload" in fields:
                groups.append(self._close_rx())
            return groups  # else wait for separate payload line

        # ── All other events / responses ─────────────────────────────────────
        # Flush rx if open (new event closed it)
        if self._rx_pending:
            groups.append(self._close_rx())

        summary  = _fmt(cmd, fields, stripped)
        color    = _color_for(cmd, fields)
        category = _category_for(cmd)
        groups.append(make_group(
            type_    = "event",
            summary  = summary,
            raw      = [line],
            category = category,
            color    = color,
            cmd      = cmd,
        ))
        return groups

    def flush(self) -> list[dict]:
        """Force-flush all pending accumulators (called at prompt or disconnect)."""
        groups = []
        if self._rx_pending:
            groups.append(self._close_rx())
        # Boot is already emitted line-by-line — just reset state
        if self._in_boot:
            self._in_boot     = False
            self._boot_lines  = []
            self._boot_fields = {}
        if self._multi_tags:
            groups.append(self._close_multi())
        return groups

    # ── Accumulators ─────────────────────────────────────────────────────────

    def _close_rx(self, payload_values: list[str] | None = None,
                  payload_raw: str | None = None) -> dict:
        p       = self._rx_pending
        raw     = [self._rx_raw] + ([payload_raw] if payload_raw else [])
        fields  = p["fields"]
        summary = _fmt("rxPacket", fields, self._rx_raw or "")
        # Payload may be inline in the rxPacket line itself (payload: 0x.. 0x..)
        # or in a separate payload line
        inline_payload = fields.get("payload", "")
        if payload_values:
            detail = "payload: " + " ".join(payload_values)
        elif inline_payload:
            detail = "payload: " + inline_payload
        else:
            detail = None
        color    = _color_for("rxPacket", fields)
        self._rx_pending = None
        self._rx_raw     = None
        return make_group(
            type_    = "event",
            summary  = summary,
            detail   = detail,
            raw      = raw,
            category = "rx",
            color    = color,
            cmd      = "rxPacket",
        )

    def _close_boot(self) -> dict:
        lines  = self._boot_lines
        fields = self._boot_fields
        # Build summary from reset line
        rf = fields.get("reset", {})
        app   = rf.get("App", "?")
        built = rf.get("Built", "")
        cause = rf.get("Cause", "")
        summary = f"↺ Boot — {app}" + (f"  built {built}" if built else "") + (f"  cause {cause}" if cause else "")
        # Build detail: one line per boot cmd
        detail_parts = []
        for cmd in ("reset", "radio", "system", "pti"):
            f = fields.get(cmd, {})
            if f:
                parts = " | ".join(f"{k}: {v}" for k, v in f.items())
                detail_parts.append(f"{cmd}: {parts}")
        detail = "\n".join(detail_parts) if detail_parts else None
        self._in_boot     = False
        self._boot_lines  = []
        self._boot_fields = {}
        return make_group(
            type_    = "boot",
            summary  = summary,
            detail   = detail,
            raw      = lines,
            category = "boot",
            color    = "boot",
        )

    def _close_multi(self) -> dict:
        cmd  = self._multi_cmd or "?"
        tags = self._multi_tags
        rows = self._multi_rows
        raw  = self._multi_raw
        # Build summary
        summary = f"◈ {cmd} — {len(rows)} row(s)"
        # Build detail table
        if tags and rows:
            header = " | ".join(tags)
            lines  = [header, "-" * len(header)]
            for row in rows:
                lines.append(" | ".join(row))
            detail = "\n".join(lines)
        else:
            detail = None
        self._multi_tags = []
        self._multi_cmd  = None
        self._multi_rows = []
        self._multi_raw  = []
        return make_group(
            type_    = "event",
            summary  = summary,
            detail   = detail,
            raw      = raw,
            category = "info",
            color    = "default",
        )

    # ── Low-level parse_line (kept for compatibility) ─────────────────────────

    def parse_line(self, line: str) -> dict:
        stripped = line.strip()
        if _PROMPT_RE.match(stripped):
            return {"type": "prompt", "cmd": None, "fields": {}, "raw": line, "display": line}
        if not _RT_LINE.match(stripped):
            return {"type": "unknown", "cmd": None, "fields": {}, "raw": line, "display": line}
        cmd, fields, values = _parse_tokens(stripped)
        display = _fmt(cmd, fields, stripped) if cmd else line
        return {"type": "event" if cmd else "unknown", "cmd": cmd,
                "fields": fields, "raw": line, "display": display}

    # ── is_response_complete (used by cli() for early exit) ───────────────────

    def is_response_complete(self, lines: list[str]) -> bool:
        for line in reversed(lines):
            s = line.strip()
            if not s:
                continue
            if _HEADER_RE.match(s):
                return False
            if _RT_LINE.match(s) and _SINGLE_END.search(s):
                has_header = any(_HEADER_RE.match(l.strip()) for l in lines if l.strip())
                return not has_header
            return False
        return False

    # ── format_line (kept for compatibility) ─────────────────────────────────

    def format_line(self, parsed: dict) -> str:
        return _fmt(parsed.get("cmd"), parsed.get("fields", {}), parsed.get("raw", ""))
