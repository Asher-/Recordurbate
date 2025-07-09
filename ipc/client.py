import socket
import threading
import time
import select

from ipc.terminator import Terminator

class SocketClient:
  
    host = None
    ip = None
    port = None

    server = None

    def __init__( self, host, port ):
      self.hostname = host
      self.port = port

    def start( self ):  
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.connect( (self.hostname, self.port) )
        
    def ipc( self, command_string, flags = 0 ):
        command_as_bytes = command_string.encode()
        self.server.sendall( command_as_bytes, flags )
        while True:
            read_sockets, write_sockets, error_sockets = select.select([self.server] , [], [])
            should_break = False
            for pending_server_socket in read_sockets:
                result_bytes = self.server.recv(512)
                if result_bytes:
                    if result_bytes[-Terminator.length:] == Terminator.byte_array:
                        result_bytes = result_bytes[0:-Terminator.length]
                        should_break = True
                        if not result_bytes:
                            break
                    string = result_bytes.decode("utf-8")
                    print(string)
                if should_break:
                    break
            if should_break:
                break
            else:
                time.sleep(1)
