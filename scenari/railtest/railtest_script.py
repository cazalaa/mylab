import time
from random import randint
#-----------------------------------------------------------
# Local variables
#-----------------------------------------------------------

#-----------------------------------------------------------
# Script
#-----------------------------------------------------------

class SCRIPT():
    def __init__(self, board, interface, address, base_port):   
        self.board = board
        self.interface = interface
        self.ip = address
        self.base_port = base_port
         
    def script(self):
        t = self.board
        t.open_board_ports(self.ip,self.base_port)
        t.reset()
        print('\nStarting test loop\r\n')

        t.command_cli("getchannel")
        t.command_cli("help")
        t.command_admin("boardid")


        # t.read_cli()
        # time.sleep(randint(1,3))
        # for i in range(30):
        #     r = randint(0,8)
        #     t.write_cli(f"setchannel {r}")
        #     t.write_cli("settxtone 1")
        #     time.sleep(1)
        #     t.write_cli("settxtone 0")
        
        t.button(0,0.3)
        t.close_board_ports()
