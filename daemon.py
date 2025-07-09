import sys
import os
import logging
import signal
import time
import threading
from ipc.server import SocketServer

from streamer import Streamer
import config as Config

class Daemon:

    config = None

    pid = None
    logger = None
    ipc_client = None
    streamers = {}

    logfile = "./configs/rb.log"
    
    def __init__( self ):
        self.initLogger()
        self.init_config()
        self.init_signals()
    
    def initLogger( self ):
        self.logger = logging.getLogger( "Recordurbate" )
        self.logger.setLevel( logging.DEBUG )        
        fh = logging.FileHandler(self.logfile)
        fh.setLevel(logging.DEBUG)
        FORMAT = "[%(asctime)s %(filename)s:%(lineno)s - %(funcName)s()]- %(message)s"
        fh.setFormatter(logging.Formatter(FORMAT))
        self.logger.addHandler(fh)

    def init_config( self ):
        self.config = Config.load_config()
        for idx, name in enumerate(self.config["streamers"]):
            self.streamers[ name ] = Streamer( self, name )

    def init_signals( self ):
        def stop_signal(signum, stack):
            self.stop()
        signal.signal(signal.SIGINT, stop_signal)
        signal.signal(signal.SIGTERM, stop_signal)

    def daemonize(self):
        if not self.pid:
            self.logger.info("Starting daemon")

            # start socket server for ipc            
            self.ipc_client = SocketServer( daemon=self, hostname="localhost" )

            self.logger.debug("calling os.fork()")

            # double fork
            try:
                # Note: os.fork() "returns 0 in the child process and child’s process id in the parent process".
                # See https://www.geeksforgeeks.org/python-os-fork-method/
                self.pid = os.fork()
                if self.pid > 0:
                    self.logger.debug("Parent PID = {}".format(os.getpid()))
                    self.pid = None
                    sys.exit(0)
                
                self.pid = os.fork()
                if self.pid > 0:
                    self.logger.debug("Fork PID = {}".format(os.getpid()))
                    self.pid = None
                    sys.exit(0)
                self.pid = os.getpid()
                self.logger.debug("Double Fork PID = {} - fully daemonized".format(self.pid))

            except Exception as e:
                # Issue #65 - os.fork() fails in Windows but it does not throw OSError which bypassed the catch block. Resolved issue by changing "OSError" to "Exception".
                self.logger.exception("Failed to daemonize, {}. ".format(e))
                print("Failed to daemonize. Note: run recordurbate on Linux terminal instead of Windows. See " + self.logfile + " for details.")
                sys.exit(1)

            self.ipc_client.start()

            # close std's to complete daemonization
            sys.stdin.close()
            sys.stdout.close()
            sys.stderr.close()
    
            self.logger.info("Successfully started daemon, pid: {}".format(self.pid))

    def reload_config(self):
        new_config = Config.load_config()
        new_streamers = {}
        for index, name in enumerate(new_config["streamers"]):
            if name in self.streamers:
                new_streamers[ name ] = self.streamers[ name ]
                del self.streamers[ name ]
            else:
                new_streamers[ name ] = Streamer( self, name )
        for index, streamer in enumerate(self.streamers.items()):
            self.logger.info("{} has been removed".format(streamer.name))
        if self.config:
            del self.config
        if self.streamers:
            del self.streamers
        self.config = new_config
        self.streamers = new_streamers

    def start(self):
        self.daemonize()
        self.run()

    def stop(self, client_response = None):
        if self.pid:
            self.logger.info("Caught stop signal, stopping")
            client_response.close() # Since we exit we have to do this here
            self.pid = None
            time.sleep(1)
            sys.exit(0)

    def restart(self, client_response):
        self.stop()
        self.start()

    def add_streamer( self, name, save_config = True, client_response = None ):
        if name in self.streamers:
            client_response.print("{} has already been added".format(username))
        else:
            self.streamers[ name ] = Streamer( self, name )
            config["streamers"].append(name)
            if save_config and Config.save_config(config):
                client_response.print("{} has been added".format(name))

    def delete_streamer( self, name, save_config = True, client_response = None ):
        if name not in self.streamers:
            client_response.print("{} hasn't been added".format(username))
        else:
            self.streamers[ name ].stop()
            del self.streamers[ name ]
            index = Config.find_in_config(username, config)
            del self.config[ index ]
            if save_config and Config.save_config(config):
                client_response.print("{} has been deleted".format(username))

    def import_streamers( self, path, client_response = None ):
        with open(path, "r") as f:
            for line in f:
                name = line.rstrip()
                self.add_streamer( name, False )
        if Config.save_config(config):
            client_response.print("Streamers imported, Config saved")

    def export_streamers( self, path = None, client_response = None ):
        if path is None:
            path = self.config["default_export_location"]
        with open(path, "w") as f:
            for streamer in config["streamers"]:
                f.write(streamer + "\n")
        client_response.print("Wrote streamers to file")

    def list_streamers(self, client_response = None):
        streamer_count = len(self.streamers)
        if streamer_count == 0:
            client_response.print('No streamers in recording list ({}).'.format(streamer_count))
        else:
            client_response.print("Streamers in recording list:\n")
            live_streamer_count = 0
            for name in self.streamers:
                streamer = self.streamers[name]
                if streamer.stream:
                    client_response.print('* ' + streamer.name)
                    ++live_streamer_count
            
            if live_streamer_count > 0 and live_streamer_count < streamer_count:
                client_response.print("\n")
            for name in self.streamers:
                streamer = self.streamers[name]
                if not streamer.stream:
                    client_response.print('- ' + streamer.name)

    def list_streamers_online(self, client_response = None):
        streamer_count = len(self.streamers)
        if streamer_count == 0:
            client_response.print('No streamers in recording list ({}).'.format(streamer_count))
        else:
            client_response.print("Streamers currently online:\n")
            live_streamer_count = 0
            for name in self.streamers:
                streamer = self.streamers[name]
                if streamer.stream:
                    client_response.print('* ' + streamer.name)
                    ++live_streamer_count

    def list_streamers_offline(self, client_response = None):
        streamer_count = len(self.streamers)
        if streamer_count == 0:
            client_response.print('No streamers in recording list ({}).'.format(streamer_count))
        else:
            client_response.print("Streamers currently offline:\n")
            for name in self.streamers:
                streamer = self.streamers[name]
                if not streamer.stream:
                    client_response.print('- ' + streamer.name)

    def run(self):
        def daemon_run_loop():
            while self.pid:
                try:
                    if self.config is None or self.config["auto_reload_config"]:
                        self.reload_config()

                    for name in self.streamers:
                        streamer = self.streamers[name]
                        if streamer.stream:
                            continue
                        else:
                            streamer.start()
                        if self.config["rate_limit_time"]:
                            time.sleep(self.config["rate_limit_time"])

                    for i in range(60): # wait 1 min in 1 second intervals
                        if not self.pid:
                            break
                        time.sleep(1)
                    
                except Exception:
                    self.logger.exception("loop error")
                    time.sleep(1)

            # loop ended, stop all recording
            for name in self.streamers:
                streamer = self.streamers[name]
                streamer.stop()
            
            self.logger.info("Successfully stopped.")
        thread = threading.Thread(target=daemon_run_loop, args=())
        thread.start()
        return thread        

    def ipc( self, command, client_response ):
        argv = command.split(" ")
        match argv[0]:
            case "stop":
                self.stop( client_response=client_response )
            case "restart":
                self.restart( client_response=client_response )
            case "add":
                self.add_streamer( name=argv[1], client_response=client_response )
            case "del":
                self.delete_streamer( name=argv[1], client_response=client_response )
            case "list":
                if len(argv) > 1:
                    match argv[1]:
                        case "online":
                            self.list_streamers_online( client_response=client_response )
                        case "offline":
                            self.list_streamers_offline( client_response=client_response )
                else:
                    self.list_streamers( client_response=client_response )
            case "import":
                self.import_streamers( path=argv[1], client_response=client_response )
            case "export":
                self.export_streamers( path=argv[1], client_response=client_response )
