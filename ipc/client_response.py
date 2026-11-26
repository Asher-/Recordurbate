import socket

from ipc.terminator import Terminator

class ClientResponse:
  
    connected_socket = None
    server = None
    closed = False

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
        # Sends the terminator once: stop and handoff close the response
        # themselves, before the server's own close, and a second terminator
        # reaches the client as undecodable text.
        if self.closed:
            return
        self.closed = True
        self.connected_socket.sendall( Terminator.bytes )