import time
import socket
from zeroconf import ServiceBrowser, Zeroconf, ServiceInfo

class DiscoverPort:

    service_name = None
    port = None

    zeroconf = None
    browser = None

    def __init__( self, service_name ):
        self.service_name = service_name

    def start( self ):
        self.zeroconf = Zeroconf()
        self.browser = ServiceBrowser( self.zeroconf, "_recordurbate._tcp.local.", self )

    def stop( self ):
        self.zeroconf.close()

    def add_service(self, zeroconf, type, name): 
        info = zeroconf.get_service_info(type, name)
        if info and info.name == name:
            # print(f"Service '{name}' of type '{type}' discovered on port {info.port}")
            self.port = info.port

    def remove_service(self, zeroconf, type, name):
        return

    def update_service(self, zeroconf, type, name):
        return

