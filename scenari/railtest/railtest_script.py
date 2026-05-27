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
    board.config_admin(line_ending="CRLF", echo=False, prompt=">")

    board.reset()
    board.delay(1.5)  # wait for board to fully boot
    board.cli("getchannel")
    board.cli("tx 1")
    #board.cli("help")
    board.admin("boardid")
    board.button(0, 0.3)