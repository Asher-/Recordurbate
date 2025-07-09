import subprocess
import threading
import time
class Streamer:
  
    name = None
    daemon = None
    stream = None

    def __init__( self, daemon, name ):
      self.name = name
      self.daemon = daemon
    
 
    def start(self):
        def stream_thread():
            process_args = self.daemon.config["youtube-dl_cmd"].split(" ") + ["https://chaturbate.com/{}/".format(self.name), "--config-location", self.daemon.config["youtube-dl_config"]] 
            stream = subprocess.Popen( process_args, 0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL )
            # sleep (10)? and make sure process didn't immediately exit
            poll_wait_time = self.daemon.config["process_poll_wait_time"]
            if poll_wait_time is None:
                poll_wait_time = 10
            time.sleep( poll_wait_time )
            if stream.poll() is None:
                self.daemon.logger.info("Stream for {} appears to be healthy - validating.".format(self.name))
                # stream_error = stream.stderror.readline()
                if not False:
                    self.daemon.logger.info("Started to record {}.".format(self.name))
                    self.stream = stream
            stream.wait()
            if self.stream:
                self.stream = None
                self.daemon.logger.info("Stopped {}.".format(streamer.name))
            return
        thread = threading.Thread(target=stream_thread, args=())
        thread.daemon = True
        thread.start()
        return thread

    def stop( self ):
        if self.stream:
            self.daemon.logger.info("Signaling {} to stop.".format(self.name))
            self.stream.send_signal(signal.SIGINT)
            self.stream.wait()
            self.stream = None
            self.daemon.logger.info("Stopped {}.".format(streamer.name))

