import time
import re


BULB_RE = re.compile(r"is_bulb_on\s*:\s*(\d+)", re.IGNORECASE)
MOVEMENT_RE = re.compile(r"Movement\s+Score\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE)


def read_pending_cli(board):
    if hasattr(board, "read_vcom"):
        return board.read_vcom()

    if hasattr(board, "vcom_read"):
        return board.vcom_read()

    if hasattr(board, "read"):
        return board.read()

    if hasattr(board, "readline"):
        return board.readline()

    if hasattr(board, "read_line"):
        return board.read_line()

    try:
        return board.cli("")
    except Exception:
        return ""


def parse_values(text):
    if text is None:
        return None, None

    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")

    bulb_value = None
    movement_score = None

    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue

        bulb_match = BULB_RE.search(line)
        if bulb_match:
            bulb_value = int(bulb_match.group(1))

        movement_match = MOVEMENT_RE.search(line)
        if movement_match:
            movement_score = float(movement_match.group(1))

    return bulb_value, movement_score


def make_figure(xs, movement_values, bulb_values, t_now, window_s):
    x_min = max(0, t_now - window_s)
    x_max = max(window_s, t_now)

    return {
        "data": [
            {
                "type": "scatter",
                "mode": "lines",
                "name": "Movement Score (%)",
                "x": xs,
                "y": movement_values,
                "yaxis": "y",
            },
            {
                "type": "scatter",
                "mode": "lines",
                "name": "Bulb On",
                "x": xs,
                "y": bulb_values,
                "yaxis": "y2",
                "line": {"shape": "hv"},
            },
        ],
        "layout": {
            "title": {"text": "Movement Score / Bulb State - last 60 s"},
            "xaxis": {
                "title": {"text": "Time (s)"},
                "range": [x_min, x_max],
            },
            "yaxis": {
                "title": {"text": "Movement Score (%)"},
                "range": [0, 100],
            },
            "yaxis2": {
                "title": {"text": "is_bulb_on"},
                "overlaying": "y",
                "side": "right",
                "range": [-0.1, 1.1],
                "tickmode": "array",
                "tickvals": [0, 1],
                "ticktext": ["off", "on"],
            },
            "legend": {
                "orientation": "h",
            },
        },
    }


def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")

    board.print("--- Motion / bulb live plot, rolling 60 s viewport ---")

    # Keep only if you want to restart the board app.
    board.reset()
    board.delay(1)

    board.print("--- setting motion idle time to 3000 ms ---")
    out = board.cli("ai_sensing_set_motion_idle_time 3000")

    last_bulb = 0
    last_movement = 0.0

    bulb, movement = parse_values(out)
    if bulb is not None:
        last_bulb = bulb
    if movement is not None:
        last_movement = movement

    window_s = 60.0
    poll_s = 0.1

    xs = []
    movement_values = []
    bulb_values = []

    t0 = time.time()
    last_redraw = 0.0
    redraw_period_s = 0.1

    # Initial empty plot.
    board.plot.show(make_figure(xs, movement_values, bulb_values, 0, window_s))

    while True:
        out = read_pending_cli(board)
        bulb, movement = parse_values(out)

        if bulb is not None:
            last_bulb = bulb

        if movement is not None:
            last_movement = movement

        t = round(time.time() - t0, 3)

        # Add one sample every loop using the latest known values.
        xs.append(t)
        movement_values.append(last_movement)
        bulb_values.append(last_bulb)

        # Keep only the last 60 seconds of local data.
        cutoff = t - window_s
        while xs and xs[0] < cutoff:
            xs.pop(0)
            movement_values.pop(0)
            bulb_values.pop(0)

        # Redraw periodically so the x-axis range follows the latest 60 seconds.
        # This is the part that "zooms" to the last 60s.
        if t - last_redraw >= redraw_period_s:
            board.plot.show(make_figure(xs, movement_values, bulb_values, t, window_s))
            last_redraw = t

        # board.delay(poll_s)