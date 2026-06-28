import time
from random import randint
#-----------------------------------------------------------
# Local variables
#-----------------------------------------------------------

#-----------------------------------------------------------
# Script
#-----------------------------------------------------------

import time
from random import randint

def script(board):
    board.config_vcom(line_ending="CRLF", echo=True, prompt=">")

    board.reset()
    board.delay(1.5)  # wait for board to fully boot
    board.cli("getchannel")
    response = board.cli("tx 1")
    board.print(f"tx 1 response: {response.split(">")[0].strip()}")
    #board.cli("help")
