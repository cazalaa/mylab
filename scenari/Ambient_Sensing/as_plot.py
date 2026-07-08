import time
import re


BULB_RE = re.compile(r"is_bulb_on\s*:\s*(\d+)", re.IGNORECASE)
MOVEMENT_RE = re.compile(
    r"Movement\s+Score\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*%",
    re.IGNORECASE,
)


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

    # Keep this if you want to restart the board app before the test.
    board.reset()
    board.delay(1)

    board.print("--- setting motion idle time to 3000 ms ---")
    board.cli("ai_sensing_set_motion_idle_time 3000")

    board.print("--- starting AI sensing ---")
    out = board.cli("as start")

    last_bulb = 0
    last_movement = 0.0

    bulb, movement = parse_values(out)
    if bulb is not None:
        last_bulb = bulb
    if movement is not None:
        last_movement = movement

    window_s = 60.0

    # Read often, redraw less often.
    poll_s = 0.1
    redraw_period_s = 0.25

    xs = []
    movement_values = []
    bulb_values = []

    t0 = time.time()
    last_sample = 0.0
    last_redraw = 0.0

    board.plot.show(make_figure(xs, movement_values, bulb_values, 0, window_s))

    while True:
        out = board.read()
        bulb, movement = parse_values(out)

        if bulb is not None:
            last_bulb = bulb

        if movement is not None:
            last_movement = movement

        t = round(time.time() - t0, 3)

        # Add one sample per poll using the latest known values.
        if t - last_sample >= poll_s:
            xs.append(t)
            movement_values.append(last_movement)
            bulb_values.append(last_bulb)
            last_sample = t

        # Keep only the last 60 seconds of local data.
        cutoff = t - window_s
        while xs and xs[0] < cutoff:
            xs.pop(0)
            movement_values.pop(0)
            bulb_values.pop(0)

        # Redraw the full plot less often than we read the CLI.
        if t - last_redraw >= redraw_period_s:
            board.plot.show(
                make_figure(xs, movement_values, bulb_values, t, window_s)
            )
            last_redraw = t

        board.delay(poll_s)