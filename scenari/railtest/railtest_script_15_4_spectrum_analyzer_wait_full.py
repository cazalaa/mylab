import time
import re

# IEEE 802.15.4 2.4 GHz channels
START_CH = 11
END_CH = 26
CHANNELS = list(range(START_CH, END_CH + 1))
SCAN_SECONDS = 60.0
COMMAND_TIMEOUT_S = 1.5
FULL_RESPONSE_TIMEOUT_S = 3.0
POLL_SECONDS = 0.02

# RAILtest spectrumAnalyzer row format:
# #{{(spectrumAnalyzer)}{channelIndex}{channel}{frequency}{rssi}}
# {{0}{11}{2405000000}{-83.50}}
SPECTRUM_ROW_RE = re.compile(
    r"\{\{(\d+)\}\{(\d+)\}\{(\d+)\}\{([-+]?\d+(?:\.\d+)?)\}\}"
)


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def decode_spectrum(text, channels=CHANNELS):
    """Return {channel: rssi_float} from RAILtest spectrumAnalyzer output."""
    expected = set(channels)
    values = {}

    for _index, ch_s, _freq_s, rssi_s in SPECTRUM_ROW_RE.findall(as_text(text)):
        ch = int(ch_s)
        if ch in expected:
            values[ch] = float(rssi_s)

    return values


def read_pending_cli(board):
    """Drain pending VCOM text if the Board API exposes a read method."""
    for name in ("read_vcom", "vcom_read", "read", "readline", "read_line"):
        if hasattr(board, name):
            try:
                return as_text(getattr(board, name)())
            except Exception:
                return ""
    return ""


def get_full_spectrum(board, start_ch=START_CH, end_ch=END_CH):
    """
    Send one spectrumAnalyzer command, then do not return until all requested
    channels have been decoded, or until FULL_RESPONSE_TIMEOUT_S expires.

    This prevents the next spectrumAnalyzer command from being sent while the
    tail of the previous response is still arriving.
    """
    channels = list(range(start_ch, end_ch + 1))
    expected = set(channels)
    cmd = "spectrumAnalyzer 0 {} {}".format(start_ch, end_ch)

    raw = as_text(board.cli(cmd, timeout=COMMAND_TIMEOUT_S))
    values = decode_spectrum(raw, channels)

    deadline = time.time() + FULL_RESPONSE_TIMEOUT_S
    while set(values) != expected and time.time() < deadline:
        extra = read_pending_cli(board)
        if extra:
            raw += extra
            values = decode_spectrum(raw, channels)
            continue

        # Let the VCOM reader receive the remaining rows before trying again.
        board.delay(POLL_SECONDS)

    return values, raw


def show_spectrum(board, channels, rssi_by_channel, sweep, elapsed_s):
    y = [rssi_by_channel.get(ch) for ch in channels]
    labels = ["" if v is None else "{:.1f}".format(v) for v in y]

    board.plot.show({
        "data": [{
            "type": "bar",
            "name": "RSSI",
            "x": [str(ch) for ch in channels],
            "y": y,
            "text": labels,
            "textposition": "auto",
        }],
        "layout": {
            "title": {
                "text": "802.15.4 RSSI spectrum - sweep {} - {:.1f}s".format(sweep, elapsed_s)
            },
            "xaxis": {"title": {"text": "Channel"}},
            "yaxis": {"title": {"text": "RSSI (dBm)"}, "range": [-110, 0]},
            "margin": {"l": 55, "r": 20, "t": 45, "b": 45},
        },
        "config": {"responsive": True, "displaylogo": False},
    })


def print_sweep(board, sweep, elapsed_s, channels, rssi_by_channel):
    line = "sweep {:03d} {:5.1f}s ".format(sweep, elapsed_s)
    line += " ".join(
        "ch{:02d}:{:>6}".format(
            ch,
            "--" if rssi_by_channel.get(ch) is None else "{:.1f}".format(rssi_by_channel[ch]),
        )
        for ch in channels
    )
    board.print(line)


def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")
    board.reset()
    board.delay(1)
    board.show_terminal("graph")
    board.print("--- init 2.4GHz 802.15.4 spectrumAnalyzer RSSI scan ---")

    board.cli("rx 0", timeout=COMMAND_TIMEOUT_S)
    board.cli("config2p4GHz802154", timeout=COMMAND_TIMEOUT_S)
    board.cli("setNotification 0", timeout=COMMAND_TIMEOUT_S)

    rssi_by_channel = {ch: None for ch in CHANNELS}
    show_spectrum(board, CHANNELS, rssi_by_channel, 0, 0.0)

    start = time.time()
    sweep = 0

    try:
        while time.time() - start < SCAN_SECONDS:
            sweep += 1

            values, raw = get_full_spectrum(board, START_CH, END_CH)

            if values:
                rssi_by_channel.update(values)

            missing = [ch for ch in CHANNELS if ch not in values]
            if missing:
                snippet = " ".join(as_text(raw).split())[:250]
                board.print(
                    "WARN: incomplete spectrumAnalyzer response, missing {}: {}".format(
                        missing,
                        snippet,
                    )
                )

            elapsed_s = time.time() - start
            show_spectrum(board, CHANNELS, rssi_by_channel, sweep, elapsed_s)
            print_sweep(board, sweep, elapsed_s, CHANNELS, rssi_by_channel)

    finally:
        board.cli("rx 0", timeout=COMMAND_TIMEOUT_S)
        board.cli("setNotifications 1", timeout=COMMAND_TIMEOUT_S)
        board.print("--- spectrumAnalyzer RSSI scan done ---")
