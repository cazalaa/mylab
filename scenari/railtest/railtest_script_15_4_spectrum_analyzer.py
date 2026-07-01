import time
import re

# IEEE 802.15.4 2.4 GHz channels
CHANNELS = list(range(11, 27))
RANGES = [(11, 16), (17, 22), (23, 26)]
SCAN_SECONDS = 60.0
COMMAND_TIMEOUT_S = 2.5
POLL_SECONDS = 0.010

# Railtest command response helpers
SPECTRUM_CMD_RE = re.compile(r"\{\{\(spectrumAnalyzer\)", re.IGNORECASE)
CONFIG_RE = re.compile(r"\{\{\(config2p4GHz802154\)", re.IGNORECASE)
RX_DISABLED_RE = re.compile(r"\{\{\(rx\)\}\{Rx:Disabled\}", re.IGNORECASE)
SET_RX_NOTIF_RE = re.compile(r"\{\{\(setRxNotification\)", re.IGNORECASE)

# Common parseable fragments seen in Railtest style outputs.
BRACE_FIELD_RE = re.compile(r"\{([^{}:]+):([^{}]+)\}")
RSSI_FIELD_RE = re.compile(r"rssi\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
CHANNEL_FIELD_RE = re.compile(r"(?:channel|ch)\s*[:=]\s*(\d+)", re.IGNORECASE)


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def read_pending_cli(board):
    """Best-effort drain of async VCOM/CLI output."""
    for name in ("read_vcom", "vcom_read", "read", "readline", "read_line"):
        if hasattr(board, name):
            try:
                return as_text(getattr(board, name)())
            except Exception:
                return ""
    return ""


def cli_wait_for(board, cmd, expected_re=None, timeout_s=COMMAND_TIMEOUT_S, quiet=False):
    """Send one command and wait for an expected formal response or timeout."""
    out = ""

    try:
        out += as_text(board.cli(cmd))
    except Exception as e:
        out += "\n[cli exception for {}: {}]\n".format(cmd, e)

    deadline = time.time() + timeout_s

    while time.time() < deadline:
        if expected_re is None:
            board.delay(POLL_SECONDS)
            return out

        if expected_re.search(out):
            return out

        extra = read_pending_cli(board)
        if extra:
            out += extra
            if expected_re.search(out):
                return out

        board.delay(POLL_SECONDS)

    if expected_re is not None and not expected_re.search(out) and not quiet:
        board.print("WARN: timeout waiting for response to '{}'".format(cmd))

    return out


def cli_try_variants(board, variants, expected_re=None, timeout_s=COMMAND_TIMEOUT_S):
    last = ""
    for cmd in variants:
        out = cli_wait_for(board, cmd, expected_re=expected_re, timeout_s=timeout_s, quiet=True)
        last = out
        low = out.lower()
        if expected_re is not None and expected_re.search(out):
            return out, cmd
        if expected_re is None and "unknown" not in low and "error" not in low and "not found" not in low:
            return out, cmd
    return last, variants[-1]


def parse_spectrum_analyzer_rssi(text, expected_channels):
    """
    Parse RSSI values from spectrumAnalyzer output.

    RAILtest versions differ a bit, so this accepts several parseable forms:
      {{(spectrumAnalyzer)}{channel:11}{rssi:-66.25}}
      {{(spectrumAnalyzer)}{ch:11}{rssi:-66.25}}
      {{...}{11:-66.25}{12:-72.00}...}
      lines containing: channel:11 rssi:-66.25
      lines containing: ch 11 ... rssi -66.25

    Returns {channel: rssi_float}.
    """
    result = {}
    expected = set(expected_channels)
    s = as_text(text)

    # 1) Generic {key:value} extraction from Railtest brace fields.
    fields = [(k.strip(), v.strip()) for k, v in BRACE_FIELD_RE.findall(s)]

    # 1a) direct numeric-channel fields: {11:-66.25}
    for k, v in fields:
        if k.isdigit():
            ch = int(k)
            if ch in expected:
                try:
                    result[ch] = float(v)
                except ValueError:
                    pass

    # 1b) paired fields: {channel:11}{rssi:-66.25}
    last_channel = None
    for k, v in fields:
        kl = k.lower()
        if kl in ("channel", "ch"):
            try:
                candidate = int(v)
                last_channel = candidate if candidate in expected else None
            except ValueError:
                last_channel = None
        elif kl == "rssi" and last_channel is not None:
            try:
                result[last_channel] = float(v)
            except ValueError:
                pass
            last_channel = None

    # 2) Line-based fallback.
    for line in s.splitlines():
        ch_match = CHANNEL_FIELD_RE.search(line)
        rssi_match = RSSI_FIELD_RE.search(line)
        if ch_match and rssi_match:
            ch = int(ch_match.group(1))
            if ch in expected:
                result[ch] = float(rssi_match.group(1))
                continue

        # Very loose fallback: find a channel number and a plausible negative RSSI.
        # Only used if the line mentions spectrum/rssi or starts with a Railtest block.
        lower = line.lower()
        if "rssi" in lower or "spectrum" in lower or "{{" in line:
            nums = re.findall(r"[-+]?\d+(?:\.\d+)?", line)
            for idx, n in enumerate(nums):
                try:
                    ch = int(float(n))
                except ValueError:
                    continue
                if ch in expected:
                    # Pick the nearest later negative-looking number as RSSI.
                    for later in nums[idx + 1:]:
                        try:
                            val = float(later)
                        except ValueError:
                            continue
                        if -130 <= val <= 20 and val != ch:
                            result[ch] = val
                            break
                    break

    return result


def show_spectrum(board, channels, rssi_by_channel, sweep_count, elapsed_s):
    y = [rssi_by_channel.get(ch, None) for ch in channels]
    labels = ["" if v is None else "{:.1f}".format(v) for v in y]

    board.plot.show({
        "data": [
            {
                "type": "bar",
                "name": "RSSI",
                "x": [str(ch) for ch in channels],
                "y": y,
                "text": labels,
                "textposition": "auto",
            }
        ],
        "layout": {
            "title": {
                "text": "802.15.4 RSSI spectrum - sweep {} - {:.1f}s".format(
                    sweep_count,
                    elapsed_s,
                )
            },
            "xaxis": {"title": {"text": "Channel"}},
            "yaxis": {"title": {"text": "RSSI (dBm)"}, "range": [-110, 0]},
            "margin": {"l": 55, "r": 20, "t": 45, "b": 45},
        },
        "config": {"responsive": True, "displaylogo": False},
    })


def print_sweep_line(board, sweep, elapsed, channels, rssi_by_channel):
    line = "sweep {:03d} {:5.1f}s ".format(sweep, elapsed)
    line += " ".join(
        "ch{:02d}:{:>6}".format(
            ch,
            "--" if rssi_by_channel[ch] is None else "{:.1f}".format(rssi_by_channel[ch]),
        )
        for ch in channels
    )
    board.print(line)


def run_spectrum_range(board, start_ch, end_ch):
    """
    Run one parseable Railtest spectrumAnalyzer command for an inclusive range.

    First argument is 0 to disable ASCII-art/non-parseable output:
        spectrumAnalyzer 0 11 16
    """
    cmd = "spectrumAnalyzer 0 {} {}".format(start_ch, end_ch)
    out = cli_wait_for(board, cmd, expected_re=SPECTRUM_CMD_RE, timeout_s=COMMAND_TIMEOUT_S)
    values = parse_spectrum_analyzer_rssi(out, range(start_ch, end_ch + 1))
    return values, out


def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")
    board.reset()
    board.delay(1)

    board.print("--- init 2.4GHz 802.15.4 spectrumAnalyzer RSSI scan ---")

    cli_wait_for(board, "rx 0", expected_re=RX_DISABLED_RE)
    cli_try_variants(board, ["config2p4GHz802154", "config2p4GHz802154 0 0"], expected_re=CONFIG_RE)

    # Keep RX packet events from interleaving with command responses where supported.
    cli_try_variants(
        board,
        ["setNotification 0", "setnotification 0"],
        expected_re=SET_RX_NOTIF_RE,
        timeout_s=0.8,
    )

    rssi_by_channel = {ch: None for ch in CHANNELS}
    start = time.time()
    sweep = 0
    printed_raw_hint = False

    show_spectrum(board, CHANNELS, rssi_by_channel, 0, 0.0)

    try:
        while (time.time() - start) < SCAN_SECONDS:
            sweep += 1
            sweep_updated = False

            for start_ch, end_ch in RANGES:
                if (time.time() - start) >= SCAN_SECONDS:
                    break

                values, raw = run_spectrum_range(board, start_ch, end_ch)

                if values:
                    for ch, rssi in values.items():
                        rssi_by_channel[ch] = rssi
                    sweep_updated = True
                else:
                    board.print(
                        "WARN: no RSSI parsed from spectrumAnalyzer 0 {} {}".format(
                            start_ch,
                            end_ch,
                        )
                    )
                    if not printed_raw_hint:
                        # Print a compact snippet once to adapt the parser if needed.
                        snippet = " ".join(as_text(raw).split())[:300]
                        board.print("RAW spectrumAnalyzer sample: {}".format(snippet))
                        printed_raw_hint = True

                # Update the display after each fast range command.
                show_spectrum(board, CHANNELS, rssi_by_channel, sweep, time.time() - start)

            # Also update at the end of every complete sweep even if no values changed.
            show_spectrum(board, CHANNELS, rssi_by_channel, sweep, time.time() - start)
            print_sweep_line(board, sweep, time.time() - start, CHANNELS, rssi_by_channel)

            if not sweep_updated:
                board.delay(0.05)

    finally:
        cli_wait_for(board, "rx 0", expected_re=RX_DISABLED_RE, timeout_s=0.8, quiet=True)
        cli_try_variants(
            board,
            ["setNotification 1", "setnotification 1"],
            expected_re=SET_RX_NOTIF_RE,
            timeout_s=0.8,
        )
        board.print("--- spectrumAnalyzer RSSI scan done ---")
