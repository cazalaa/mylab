import time
import re


def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")

    # Keep only if you want to restart the board app.
    board.reset()
    board.delay(1)

    board.print("--- Starting RF source ambient sensing ---")
    out = board.cli("as start")
    