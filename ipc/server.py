import socket
import threading
import sys
import errno

from ipc.client_response import ClientResponse

class SocketServer:
  
    daemon = None
    hostname = None
    ip = None
    port = None

    max_connections = 5

    ipc = None

    def __init__( self, daemon, hostname = None, ip = None, port = 0, max_connections = 5 ):
      self.daemon = daemon
      if hostname:
          self.hostname = hostname
          self.ip = socket.gethostbyname( self.hostname )
      elif ip:
          self.ip = ip
          self.hostname = socket.gethostbyaddr( self.ip )
      else:
          raise ValueError("Expected hostname or ip but neither was provided.")
      self.port = port
      self.max_connections = max_connections

    def bind( self ):
        self.daemon.logger.info("Starting socket server.")
        self.ipc = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
        self.ipc.setsockopt( socket.SOL_SOCKET, socket.SO_REUSEADDR, 1 )
        try:
            self.ipc.bind( (self.hostname, self.port) )
        except OSError as e:
            # The port is fixed, so the process holding it is the running daemon
            if e.errno != errno.EADDRINUSE:
                raise
            self.ipc.close()
            print("Already started.")
            sys.exit(1)
        self.ipc.listen(5)
        self.ipc.settimeout(2)
        self.daemon.logger.info("Listening on port {}".format( self.port ))

    def start( self ):  
        self.daemon.logger.info("Starting socket listening loop.")
        self.loop()

    def handle_connection( self, connected_socket, address ):
        try:
            connected_socket.settimeout(10)
            data = connected_socket.recv(512)
            if not data:
                return
            command = data.decode("utf-8")
            client_response = ClientResponse( server=self, connected_socket=connected_socket )
            try:
                self.daemon.ipc( command=command, client_response=client_response )
            finally:
                client_response.close()
        except socket.timeout:
            self.daemon.logger.error("Connection from {} timed out waiting for data.".format(address))
        except ConnectionResetError:
            self.daemon.logger.exception("Connection from {} reset by peer (abrupt disconnection).".format(address))
        except Exception:
            self.daemon.logger.exception("Error handling connection from {}.".format(address))
        finally:
            try:
                connected_socket.close()
            except Exception:
                pass

    def loop( self ):
        def server_listening_loop():
            while self.daemon.pid:
                try:
                    connected_socket, address = self.ipc.accept()
                    self.daemon.logger.info("Received connection from {}.".format(address))
                    thread = threading.Thread(target=self.handle_connection, args=(connected_socket, address))
                    thread.daemon = True
                    thread.start()
                except socket.timeout:
                    continue
                except Exception:
                    self.daemon.logger.exception("Error accepting connection.")

        thread = threading.Thread(target=server_listening_loop, args=())
        thread.daemon = True
        thread.start()
        return thread
        
