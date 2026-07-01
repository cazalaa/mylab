# json_dashboard_test_vehicle.py
#
# Test vehicle for the JSON-described Graph dashboard:
# - controls rendered from JSON
# - Plotly graph rendered from JSON
# - controls send CLI commands through the existing terminal_input path
# - synthetic live data pushed through board.plot.extend(...)
#
# Open the board terminal, click the "Graphe" tab, then run this scenario.

import time
import math
import random


def dashboard_spec():
    return {
        "kind": "dashboard",
        "title": "AI Sensing Dashboard - JSON test vehicle",
        "layout": [
            {
                "type": "controls",
                "id": "top_controls",
                "title": "CLI controls",
                "controls": [
                    {
                        "type": "slider",
                        "id": "motion_idle_time",
                        "label": "Motion idle",
                        "min": 500,
                        "max": 10000,
                        "step": 100,
                        "value": 3000,
                        "unit": "ms",
                        "command": "ai_sensing_set_motion_idle_time {value}",
                        "send": "change",
                    },
                    {
                        "type": "slider",
                        "id": "threshold",
                        "label": "Threshold",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "value": 25,
                        "unit": "%",
                        "command": "ai_sensing_set_threshold {value}",
                        "send": "change",
                    },
                    {
                        "type": "number",
                        "id": "gain",
                        "label": "Gain",
                        "min": 1,
                        "max": 10,
                        "step": 1,
                        "value": 4,
                        "command": "ai_sensing_set_gain {value}",
                    },
                    {
                        "type": "select",
                        "id": "mode",
                        "label": "Mode",
                        "value": "normal",
                        "options": [
                            {"label": "Low power", "value": "low"},
                            {"label": "Normal", "value": "normal"},
                            {"label": "High sensitivity", "value": "high"},
                        ],
                        "command": "ai_sensing_set_mode {value}",
                        "send": "change",
                    },
                    {
                        "type": "button",
                        "id": "read_status",
                        "label": "Read status",
                        "command": "ai_sensing_get_status",
                    },
                    {
                        "type": "button",
                        "id": "toggle_bulb",
                        "label": "Toggle bulb",
                        "command": "ai_sensing_toggle_bulb",
                    },
                ],
            },
            {
                "type": "row",
                "flex": "1",
                "children": [
                    {
                        "type": "plotly",
                        "id": "main_plot",
                        "title": "Movement score and bulb state",
                        "flex": "3",
                        "minHeight": "360px",
                        "figure": {
                            "data": [
                                {
                                    "type": "scatter",
                                    "mode": "lines",
                                    "name": "Movement Score (%)",
                                    "x": [],
                                    "y": [],
                                    "yaxis": "y",
                                },
                                {
                                    "type": "scatter",
                                    "mode": "lines",
                                    "name": "Bulb On",
                                    "x": [],
                                    "y": [],
                                    "yaxis": "y2",
                                    "line": {"shape": "hv"},
                                },
                            ],
                            "layout": {
                                "margin": {"l": 50, "r": 50, "t": 25, "b": 40},
                                "xaxis": {
                                    "title": {"text": "Time (s)"},
                                    "range": [0, 60],
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
                                "legend": {"orientation": "h"},
                            },
                            "config": {"responsive": True, "displaylogo": False},
                        },
                    },
                    {
                        "type": "text",
                        "id": "notes",
                        "flex": "1",
                        "text": "Move sliders/buttons to send generated CLI commands. The plot is synthetic and uses a 60 s moving viewport.",
                    },
                ],
            },
        ],
    }


def script(board):
    board.print("=== JSON dashboard test vehicle ===")
    board.print("Open the Graphe tab.")

    board.delay(3)

    # Send the whole dashboard as JSON through the existing plot_figure path.
    board.plot.dashboard(dashboard_spec())

    t0 = time.time()
    bulb = 0

    while time.time() - t0 < 120:
        t = time.time() - t0

        # Synthetic movement signal.
        movement = (
            45
            + 35 * math.sin(2 * math.pi * 0.035 * t)
            + 10 * math.sin(2 * math.pi * 0.35 * t)
            + random.uniform(-4, 4)
        )
        movement = max(0, min(100, movement))

        # Synthetic bulb state derived from movement.
        if movement > 70:
            bulb = 1
        elif movement < 30:
            bulb = 0

        board.plot.extend(
            [round(movement, 2), bulb],
            x=round(t, 3),
            traces=[0, 1],
            maxpoints=600,
            target="main_plot",
            window_s=60,
        )

        if int(t * 10) % 10 == 0:
            board.print("is_bulb_on : {}".format(bulb))
            board.print("MOTION: {}".format("DETECTED" if movement > 50 else "NONE"))
            board.print("Movement Score={}%".format(round(movement, 1)))

        board.delay(0.1)

    board.print("=== JSON dashboard test vehicle done ===")