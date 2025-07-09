import socket
import threading

from zeroconf import ServiceInfo, Zeroconf
from ipc.client_response import ClientResponse

class SocketServer:
  
    daemon = None
    hostname = None
    ip = None
    port = None

    max_connections = 5
    service_type = "_recordurbate._tcp.local."
    service_name = "Recordurbate"
    properties = {}
    zeroconf = None

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

    def registerZeroConf( self ):
        own_address = self.ipc.getsockname()
        self.ip = own_address[0]
        self.port = own_address[1]
        server_address = socket.inet_aton( self.ip )

        info = ServiceInfo(
            self.service_type,
            f"{self.service_name}.{self.service_type}",
            addresses=[self.ip],
            port=self.port,
            properties=self.properties,
            server=self.hostname, 
        )

        self.zeroconf = Zeroconf()
        self.daemon.logger.info("Registering service '{}' on port {}".format( self.service_name, self.port ))
        self.zeroconf.register_service(info)

    def unregisterZeroConf( self ):
        self.zeroconf.unregister_service(info)
        self.zeroconf.close()

    def start( self ):  
        self.daemon.logger.info("Starting socket server.")
        self.ipc = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
        self.ipc.bind( (self.hostname, self.port) )
        self.ipc.listen(5)
        self.registerZeroConf()
        self.daemon.logger.info("Starting socket listening loop.")
        self.loop()

    def stop( self ):
        self.unregisterZeroConf()

    def loop( self ):
        def server_listening_loop():
            command = ""
            while self.daemon.pid:
                connected_socket, address = self.ipc.accept()
                self.daemon.logger.info("Received connection from {}.".format( address))
                bytes = connected_socket.recv(512)
                command = bytes.decode("utf-8")
                client_response = ClientResponse( server=self, connected_socket=connected_socket )
                self.daemon.ipc( command=command, client_response=client_response )
                client_response.close()
        thread = threading.Thread(target=server_listening_loop, args=())
        thread.daemon = True
        thread.start()
        return thread
        
