#!/usr/bin/env python3

import sys
import time
import socket
import gc

from zeroconf import ServiceInfo, Zeroconf, NonUniqueNameException

from ipc.client import SocketClient
from ipc.discover_port import DiscoverPort
from ipc.zeroconf_config import ZeroconfConfig

from daemon import Daemon

daemon = None
host = "localhost"
ip = "127.0.0.1"
port = 0
args = None
socket_client = None

port_timeout = 10

#################### Args ####################

def check_num_args(min, max = None):
    argc = len(sys.argv)
    if argc < min and ( max is None or (max and argc > max)):
        usage()
        return False
    return True

#################### Daemon ####################

def discover_port():
    discover = DiscoverPort( "Recordurbate" )
    discover.start()
    timeout = 0
    while ( discover.port is None and timeout < port_timeout ):
        time.sleep(1)
        timeout += 1
    if discover.port is None:
        raise ValueError("Unable to discover port.")
    else:
        global port
        port = discover.port
    discover.stop()

#################### Add / Remove ####################

def add():
    if not check_num_args(3): 
        print("Expected 3rd argument specifying streamer to add.")
        return
    ipc( " ".join(sys.argv[1:] ) )

def remove():
    if not check_num_args(3): 
        print("Expected 3rd argument specifying streamer to remove.")
        return    
    ipc( " ".join(sys.argv[1:] ) )

#################### List ####################

def list_streamers():
    if not check_num_args(2,3): 
        print("Encountered unexpected extra arguments {}.".format(" ".join(sys.argv[3:])))      
        return
    ipc( " ".join(sys.argv[1:] ) )

#################### Import / Export ####################

def import_streamers():
    if not check_num_args(3): 
      print("Expected 3rd argument specifying file for import source.")
      return
    ipc( " ".join(sys.argv[1:] ) )


def export_streamers():
    if len(sys.argv) not in (2, 3): 
        print("Encountered unexpected extra arguments {}.".format(" ".join(sys.argv[3:])))      
        return usage()
    ipc( " ".join(sys.argv[1:] ) )

#################### Start / Stop / Restart ####################

def start():
    if not check_num_args(2): 
        print("Encountered unexpected extra arguments {}.".format(" ".join(sys.argv[2:])))      
        return
    def ensure_service_not_started():
        global host, ip, port
        server_address = socket.inet_aton( ip )
        info = ServiceInfo(
            ZeroconfConfig.service_type,
            f"{ZeroconfConfig.service_name}.{ZeroconfConfig.service_type}",
            addresses=[ip],
            port=port,
            properties={},
            server=host, 
        )
        publish_zeroconf = Zeroconf()
        try:
            publish_zeroconf.register_service(info)
        except NonUniqueNameException:
            print("Already started.")
            sys.exit(1)
        publish_zeroconf.unregister_service(info)
        return True
    if ensure_service_not_started():
        foreground = "--foreground" in sys.argv
        daemon = Daemon()
        daemon.start(foreground=foreground)

def stop():
    if not check_num_args(2): 
        print("Encountered unexpected extra arguments {}.".format(" ".join(sys.argv[2:])))      
        return      
    ipc( "stop" )
    sys.exit(0)

def restart():
    if not check_num_args(2): 
        print("Encountered unexpected extra arguments {}.".format(" ".join(sys.argv[2:])))      
        return      
    ipc( "restart" )

def upgrade():
    if not check_num_args(2): 
        print("Encountered unexpected extra arguments {}.".format(" ".join(sys.argv[2:])))
        return
    print("Sending handoff to running daemon...")
    try:
        ipc( "handoff" )
    except Exception as e:
        print("Failed to send handoff: {}".format(e))
        sys.exit(1)
    print("Waiting for old daemon to exit...")
    max_wait = 15
    for i in range(max_wait):
        time.sleep(1)
        try:
            global host, ip, port
            server_address = socket.inet_aton( ip )
            info = ServiceInfo(
                ZeroconfConfig.service_type,
                f"{ZeroconfConfig.service_name}.{ZeroconfConfig.service_type}",
                addresses=[ip],
                port=0,
                properties={},
                server=host,
            )
            publish_zeroconf = Zeroconf()
            try:
                publish_zeroconf.register_service(info)
                publish_zeroconf.unregister_service(info)
                publish_zeroconf.close()
                break
            except NonUniqueNameException:
                publish_zeroconf.close()
                continue
        except Exception:
            break
    else:
        print("Old daemon did not exit within {}s.".format(max_wait))
        sys.exit(1)
    print("Starting new daemon...")
    global socket_client
    socket_client = None
    gc.collect()
    daemon = Daemon()
    daemon.start()

#################### Usage ####################

def ipc( command ):
    global socket_client
    if socket_client is None:
        discover_port()
        socket_client = SocketClient( host, port )
        socket_client.start()
    socket_client.ipc( command )

def usage():
    padding = "       "
    print(  "\nUsage: Recordurbate [add | del] username" )
    print( padding + "Recordurbate [start | stop | restart | upgrade]" )
    print( padding + "Recordurbate list" )
    print( padding + "Recordurbate import list.txt" )
    print( padding + "Recordurbate export [file location]\n" )
    print( padding + "Recordurbate help\n" )

argument_map = {
    "help": usage,
    "add": add,
    "del": remove,
    "list": list_streamers,
    "import": import_streamers,
    "export": export_streamers,
    "start": start, 
    "stop": stop, 
    "restart": restart,
    "upgrade": upgrade
}

#################### __main__ ####################

if __name__ == "__main__":
    # try:
        func = argument_map.get(sys.argv[1], usage)
        func()
    # except SystemExit: # prevents usage showing twice when starting
    #     pass
    # except Exception as e:
    #     usage()
