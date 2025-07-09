import socket

from ipc.terminator import Terminator

class ClientResponse:
  
    connected_socket = None
    server = None

    def __init__( self, server, connected_socket ):
      self.connected_socket = connected_socket
      self.server = server

    def print( self, string ):
        string += "\n"
        string_as_bytes = string.encode()
        # self.server.daemon.logger.info("Starting to send return data.")
        self.connected_socket.sendall( string_as_bytes )
        # self.server.daemon.logger.info("Finished sending return data.")

    def close( self ):
        # self.server.daemon.logger.info("Sending client response terminator.")
        self.connected_socket.sendall( Terminator.bytes )