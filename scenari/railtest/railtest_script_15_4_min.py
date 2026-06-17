import time
from random import randint

def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")

    # --- IEEE 802.15.4 2.4 GHz Init ---
    board.print("--- init 2.4GHz 902.15.4 ---" )
    board.cli("rx 0")
    board.cli("config2p4GHz802154")
    board.cli("tx 2")
    #board.cli("getmemw 0 4")
    board.delay(1)
    board.cli("rx 0")