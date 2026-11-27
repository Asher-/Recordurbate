#!/usr/bin/env python3

import os
import sys
import time
import socket
import gc
import subprocess
import errno

# Re-run under the project's venv when launched with any other interpreter.
venv_dir = os.path.join( os.path.dirname( os.path.abspath(__file__) ), "venv" )
if os.path.realpath( sys.prefix ) != os.path.realpath( venv_dir ) and os.path.exists( os.path.join( venv_dir, "bin", "python" ) ):
    venv_python = os.path.join( venv_dir, "bin", "python" )
    os.execv( venv_python, [venv_python, os.path.abspath(__file__)] + sys.argv[1:] )

from ipc.client import SocketClient
from ipc.ipc_config import IpcConfig

from daemon import Daemon

daemon = None
args = None
socket_client = None

#################### Args ####################

def check_num_args(min, max = None):
    argc = len(sys.argv)
    if argc < min and ( max is None or (max and argc > max)):
        usage()
        return False
    return True

#################### Daemon ####################

def port_in_use():
    # Binding the daemon's port fails exactly while a daemon holds it
    probe = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
    probe.setsockopt( socket.SOL_SOCKET, socket.SO_REUSEADDR, 1 )
    try:
        probe.bind( (IpcConfig.host, IpcConfig.port) )
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        return True
    finally:
        probe.close()
    return False

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
    # The daemon reports "Already started." itself when it cannot bind its port
    foreground = "--foreground" in sys.argv
    # launchd starts the daemon with --foreground; any other start of a job
    # launchd has loaded goes through launchd, which would not know of a
    # daemon started here and could start a second one
    if not foreground and launchd_loaded():
        if port_in_use():
            print("Already started.")
            sys.exit(1)
        print("Starting daemon through launchd...")
        sys.exit( launchd_start() )
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

LAUNCHD_LABEL = "com.recordurbate.daemon"  # service.sh's LABEL

def launchd_loaded():
    # Whether launchd has the daemon's job loaded, as service.sh's is_loaded asks
    try:
        return subprocess.call( [ "launchctl", "list", LAUNCHD_LABEL ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL ) == 0
    except OSError:
        return False # no launchctl: not macOS

def launchd_start():
    # Runs the daemon's launchd job now; its exit status
    return subprocess.call( [ "launchctl", "kickstart", "gui/{}/{}".format(os.getuid(), LAUNCHD_LABEL) ] )

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
        if not port_in_use():
            break
    else:
        print("Old daemon did not exit within {}s.".format(max_wait))
        sys.exit(1)
    if launchd_loaded():
        # launchd runs the daemon; one started here would be a second daemon
        # it knows nothing of
        print("Starting new daemon through launchd...")
        sys.exit( launchd_start() )
    print("Starting new daemon...")
    global socket_client
    socket_client = None
    gc.collect()
    daemon = Daemon()
    daemon.start()

#################### Service ####################

def service():
    # enable / disable / status are launchd management, implemented by service.sh
    script = os.path.join( os.path.dirname( os.path.abspath(__file__) ), "service.sh" )
    sys.exit( subprocess.call( [script, sys.argv[1]] ) )

#################### Usage ####################

def ipc( command ):
    global socket_client
    if socket_client is None:
        socket_client = SocketClient( IpcConfig.host, IpcConfig.port )
        try:
            socket_client.start()
        except ConnectionRefusedError:
            socket_client = None
            raise ValueError("Daemon is not running.")
    socket_client.ipc( command )

def usage():
    padding = "       "
    print(  "\nUsage: Recordurbate [add | del] username" )
    print( padding + "Recordurbate [start | stop | restart | upgrade]" )
    print( padding + "Recordurbate [enable | disable | status]" )
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
    "upgrade": upgrade,
    "enable": service,
    "disable": service,
    "status": service
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
