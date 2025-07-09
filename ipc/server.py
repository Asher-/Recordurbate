import socket
import threading
import sys

from zeroconf import ServiceInfo, Zeroconf, NonUniqueNameException

from ipc.client_response import ClientResponse
from ipc.zeroconf_config import ZeroconfConfig

class SocketServer:
  
    daemon = None
    hostname = None
    ip = None
    port = None

    max_connections = 5
    properties = {}
    publish_zeroconf = None

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
            ZeroconfConfig.service_type,
            f"{ZeroconfConfig.service_name}.{ZeroconfConfig.service_type}",
            addresses=[self.ip],
            port=self.port,
            properties=self.properties,
            server=self.hostname, 
        )

        self.publish_zeroconf = Zeroconf()
        
        try:
            self.publish_zeroconf.register_service(info)
        except NonUniqueNameException:
            print("Already started.")
            sys.exit(1)

        self.daemon.logger.info("Registered service '{}' on port {}".format( ZeroconfConfig.service_name, self.port ))

    def unregisterZeroConf( self ):
        self.publish_zeroconf.unregister_service(info)
        self.publish_zeroconf.close()

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
                try:
                    connected_socket, address = self.ipc.accept()
                    self.daemon.logger.info("Received connection from {}.".format(address))
                    bytes = connected_socket.recv(512)
                    command = bytes.decode("utf-8")
                    client_response = ClientResponse( server=self, connected_socket=connected_socket )
                    self.daemon.ipc( command=command, client_response=client_response )
                    client_response.close()
                except ConnectionResetError:
                    self.daemon.logger.exception("Connection from {} reset by peer (abrupt disconnection).".format(address))

        thread = threading.Thread(target=server_listening_loop, args=())
        thread.daemon = True
        thread.start()
        return thread
        
