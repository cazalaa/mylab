# ─────────────────────────────────────────────────────────────
# plot_demo_global.py — démo de la vue « 📈 Graphe » (Plotly)
#
# À poser comme script global d'un scénario. Pousse config + données
# vers le terminal du board 0 via board.plot.* — aucune dépendance,
# aucun firmware requis (les données sont synthétiques).
#
# Dans le terminal du board 0, clique sur l'onglet « 📈 Graphe ».
# ─────────────────────────────────────────────────────────────
import time, math


def script(scenario):
    b0 = scenario.board(0)
    scenario.print("=== démo Graphe : début ===")

    # Laisse la fenêtre terminal se charger et rejoindre sa room VCOM
    # avant le 1er envoi (sinon le premier plot_figure est manqué).
    scenario.print("Ouvre l'onglet « 📈 Graphe » dans le terminal du board 0…")
    time.sleep(3)

    # 1) Courbe live — sinus bruité ~6 s, fenêtre glissante de 200 points
    scenario.print("→ courbe live")
    b0.plot.show({
        "data": [{"type": "scatter", "mode": "lines", "name": "signal",
                  "x": [], "y": []}],
        "layout": {"title": {"text": "Signal live"},
                   "xaxis": {"title": {"text": "t (s)"}},
                   "yaxis": {"title": {"text": "valeur"}}},
    })
    t0 = time.time()
    while time.time() - t0 < 6:
        t = time.time() - t0
        y = math.sin(2 * math.pi * 0.5 * t) + 0.1 * math.sin(2 * math.pi * 7 * t)
        b0.plot.extend([y], x=round(t, 3), traces=[0], maxpoints=200)
        time.sleep(0.05)

    # 2) Barres — distribution statique
    scenario.print("→ barres")
    b0.plot.show({
        "data": [{"type": "bar",
                  "x": ["ch11", "ch12", "ch13", "ch14", "ch15"],
                  "y": [12, 34, 7, 28, 19]}],
        "layout": {"title": {"text": "Comptage par canal"}},
    })
    time.sleep(2)

    # 3) Double axe — deux flux live (gauche / droite)
    scenario.print("→ double axe")
    b0.plot.show({
        "data": [
            {"type": "scatter", "mode": "lines", "name": "RSSI",
             "x": [], "y": [], "yaxis": "y"},
            {"type": "scatter", "mode": "lines", "name": "Temp",
             "x": [], "y": [], "yaxis": "y2"},
        ],
        "layout": {
            "title": {"text": "RSSI / Temp"},
            "yaxis": {"title": {"text": "RSSI (dBm)"}},
            "yaxis2": {"title": {"text": "Temp (°C)"},
                       "overlaying": "y", "side": "right"},
        },
    })
    t0 = time.time()
    while time.time() - t0 < 6:
        t = time.time() - t0
        rssi = -60 + 10 * math.sin(2 * math.pi * 0.3 * t)
        temp = 25 + 2 * math.sin(2 * math.pi * 0.1 * t)
        b0.plot.extend([rssi, temp], x=round(t, 3), traces=[0, 1], maxpoints=200)
        time.sleep(0.05)

    # 4) Jauge — show() ré-appelé à chaque mesure (idempotent => morph)
    scenario.print("→ jauge")
    for v in [-90, -70, -55, -48, -62]:
        b0.plot.show({
            "data": [{"type": "indicator", "mode": "gauge+number", "value": v,
                      "title": {"text": "RSSI"},
                      "gauge": {"axis": {"range": [-120, 0]}}}],
            "layout": {},
        })
        time.sleep(0.8)

    scenario.print("=== démo Graphe : fin ===")
