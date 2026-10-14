import time
import socket
from zeroconf import Zeroconf, ServiceInfo

from ipc.zeroconf_config import ZeroconfConfig

class DiscoverPort:

    service_name = None

    def __init__( self, service_name ):
        self.service_name = service_name

    # get_service_info asks again about every second until the timeout, so an
    # answer lost on the network does not end discovery
    def discover( self, timeout ):
        zeroconf = Zeroconf()
        try:
            info = zeroconf.get_service_info( ZeroconfConfig.service_type, f"{self.service_name}.{ZeroconfConfig.service_type}", timeout=timeout * 1000 )
        finally:
            zeroconf.close()
        return info.port if info else None

