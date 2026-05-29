import time
from random import randint

def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")

    board.reset()
    board.delay(1.5)  # wait for board to fully boot

    # --- IEEE 802.15.4 2.4 GHz Init ---
    board.print("--- init 2.4GHz 902.15.4 ---" )
    board.cli("rx 0")
    board.cli("config2p4GHz802154")
    board.cli("enable802154 rx 100 192 1000")
    board.cli("configTxOptions 1")

    # --- Set TX Payload (26-byte 802.15.4 frame) ---
    board.print("--- charge Payload ---" )
    board.cli("setTxLength 26")
    board.cli("setTxPayload 0 0x1b 0x61 0x98 0x00 0x34 0x12 0x44 0x33 0x55 0x44")
    board.cli("setTxPayload 10 0x00 0x01 0x02 0x03 0x04 0x05 0x06 0x07 0x08 0x09")
    board.cli("setTxPayload 20 0x0a 0x0b 0x0c 0x0d 0x0e 0x0f")

    # --- Transmit 1 packet ---
    board.print("--- send 1 packet using CLI ---" )
    board.cli("tx 1")
    
    board.print("--- send 1 packet using button ---" )
    board.button(0, 0.3)