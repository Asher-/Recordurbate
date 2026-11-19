import subprocess
import select
import threading
import time
import os
import sys
import signal

from pid import process_active, AdoptedProcess

# yt-dlp's Chaturbate extractor raises these for a room that is up but has no public stream
ROOM_STATES = (
    "Room is currently offline",
    "Room is currently in a private show",
    "Performer is currently away",
    "Room is password protected",
    "Hidden session in progress",
)
NO_ERROR = "yt-dlp printed no error"
READY_TIMEOUT = 90  # seconds a launch may take to log "Recording"; recorder.py's own lookup gives up after 60

class Streamer:
  
    name = None
    daemon = None
    stream = None
    started = False

    def __init__( self, daemon, name ):
      self.name = name
      self.daemon = daemon
    
    def ensure_valid_stream( self, pid = None ):
        # Whether the recorder process is alive. start() asks only after
        # recorder_ready has seen the recorder resolve the room; adopt() asks
        # on liveness alone.
        if not pid and self.stream:
            pid = self.stream.pid
        return bool( pid and process_active(pid) )
 
    def start(self):
        def stream_thread():

            ready_fd = None
            try:
                capture_path = "./configs/capture-{}.log".format(self.name)
                with open( capture_path, "a" ) as capture_log:
                    capture_log.write( "\n=== {} capture start {} ===\n".format(self.name, time.strftime("%Y-%m-%d %H:%M:%S")) )
                    capture_log.flush()
                    capture_offset = capture_log.tell()
                    # recorder.py signals on write_fd once the room has resolved
                    ready_fd, write_fd = os.pipe()
                    process_args = [ sys.executable, "recorder.py", "https://chaturbate.com/{}/".format(self.name), str(write_fd) ]
                    try:
                        self.stream = subprocess.Popen( process_args, 0, stdout=capture_log, stderr=capture_log, start_new_session=True, pass_fds=(write_fd,) )
                    finally:
                        os.close( write_fd )
            except OSError:
                if ready_fd is not None:
                    os.close( ready_fd )
                self.daemon.logger.exception("Failed to launch recorder for {}".format(self.name))
                return
            proc = self.stream

            try:
                if self.recorder_ready( ready_fd ) and self.ensure_valid_stream( pid = proc.pid ):
                    self.daemon.logger.info("Started to record {}.".format(self.name))
                    self.daemon.record_launch()
                    self.started = True
                    proc.wait()
                    return
                # stop clears stream, and a stopped launch says nothing about the site
                if self.stream:
                    self.daemon.record_launch( self.launch_error( capture_path, capture_offset ) )
            except Exception:
                self.daemon.logger.exception("stream_thread error for {}".format(self.name))
            finally:
                os.close( ready_fd )
                self.stop( signal_child=False )
            return
        thread = threading.Thread(target=stream_thread, args=())
        thread.daemon = True
        thread.start()
        return thread

    def launch_error( self, capture_path, offset ):
        # The last error yt-dlp printed for this launch, or None when it named a
        # room state, which it can only have heard from the site.
        with open( capture_path, "r", errors="replace" ) as capture_log:
            capture_log.seek( offset )
            errors = [ line.strip() for line in capture_log if line.startswith("ERROR:") ]
        if not errors:
            return NO_ERROR
        if errors[-1].endswith( ROOM_STATES ):
            return None
        return errors[-1]

    def recorder_ready( self, ready_fd ):
        # recorder.py writes to ready_fd once yt-dlp has resolved the room, then
        # closes it. The pipe reaches end of file without a write when the
        # recorder exits first, including when stop() terminates it.
        readable, _, _ = select.select( [ready_fd], [], [], READY_TIMEOUT )
        return bool( readable ) and os.read( ready_fd, 16 ) != b""

    def adopt( self, pid ):
        def adopted_thread():
            self.stream = AdoptedProcess( pid )
            try:
                self.daemon.logger.info("Adopted existing recorder (PID {}) for {}.".format(pid, self.name))
                if self.ensure_valid_stream( pid=pid ):
                    self.started = True
                    self.daemon.logger.info("Adopted stream for {} validated.".format(self.name))
                    self.stream.wait()
                else:
                    self.daemon.logger.info("Adopted stream for {} failed validation, will restart.".format(self.name))
            except Exception:
                self.daemon.logger.exception("adopted_thread error for {}".format(self.name))
            finally:
                self.stop( signal_child=False )
            return
        thread = threading.Thread(target=adopted_thread, args=())
        thread.daemon = True
        thread.start()
        return thread

    def stop( self, signal_child = True ):
        stream = self.stream
        if stream:
            if self.started:
                self.started = False
                if signal_child:
                    self.daemon.logger.info("Signaling {} to stop.".format(self.name))
                    stream.send_signal(signal.SIGINT)
                    try:
                        stream.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.daemon.logger.info("SIGINT timeout for {}, escalating to SIGTERM.".format(self.name))
                self.daemon.logger.info("Stopped {}.".format(self.name))
            if stream.poll() is None:
                stream.terminate()
                stream.wait()
            self.stream = None

