import subprocess
import threading
import time
import os
import signal

from pid import process_active, get_pid_by_name_and_args

class Streamer:
  
    name = None
    daemon = None
    stream = None
    started = False

    def __init__( self, daemon, name ):
      self.name = name
      self.daemon = daemon
    
    def ensure_valid_stream( self, ytdlp_pid = None, ffmpeg_pid = None, recursion=0):
        if not ytdlp_pid and self.stream:
            ytdlp_pid = self.stream.pid
        if ytdlp_pid and not process_active(ytdlp_pid):
            ytdlp_pid = None
        ffmpeg_pid = get_pid_by_name_and_args('ffmpeg', args_substring='/' + self.name + '/')#, exe_path=os.getcwd() + '/venv/bin') 
        if ytdlp_pid:
            if ffmpeg_pid:
                return True
            elif recursion > 5:
                return False
            else:
                time.sleep(5)
                # If we have yt-dlp but not ffmpeg it probably hasn't started yet.
                # Maybe we want to add a recursion count but it hasn't been an issue yet.
                return self.ensure_valid_stream(recursion=recursion+1)
        else:
            if ffmpeg_pid:
                self.daemon.logger.exception("Lost parent yt-dlp; killing stranded ffmpeg with PID {}".format(ffmpeg_pid))
                os.kill( ffmpeg_pid, signal.SIGTERM )
            else:
                return False
 
    def start(self):
        def stream_thread():

            def ignore_sigchld():
                signal.signal(signal.SIGCHLD, signal.SIG_IGN)

            process_args = self.daemon.config["youtube-dl_cmd"].split(" ") + ["https://chaturbate.com/{}/".format(self.name), "--config-location", self.daemon.config["youtube-dl_config"]] 
            self.stream = subprocess.Popen( process_args, 0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=ignore_sigchld )
            proc = self.stream
    
            try:
                # poll in 1s intervals to detect early exit (e.g. streamer offline)
                poll_wait_time = self.daemon.config["process_poll_wait_time"]
                if poll_wait_time is None:
                    poll_wait_time = 10
                for _ in range(poll_wait_time):
                    if not self.stream or proc.poll() is not None:
                        break
                    time.sleep(1)
                if self.stream and proc.poll() is None:
                    # self.daemon.logger.info("Stream for {} appears to be healthy - validating.".format(self.name))
                    if self.ensure_valid_stream( ytdlp_pid = proc.pid ):
                        self.daemon.logger.info("Started to record {}.".format(self.name))
                        self.started = True
                        proc.wait()
                        self.cleanup_ffmpeg()
            except Exception:
                self.daemon.logger.exception("stream_thread error for {}".format(self.name))
            finally:
                self.stop( signal_child=False )
            return
        thread = threading.Thread(target=stream_thread, args=())
        thread.daemon = True
        thread.start()
        return thread

    def cleanup_ffmpeg( self ):
        ffmpeg_pid = get_pid_by_name_and_args('ffmpeg', args_substring='/' + self.name + '/')
        if ffmpeg_pid:
            self.daemon.logger.info("Killing orphaned ffmpeg (PID {}) for {}".format(ffmpeg_pid, self.name))
            os.kill( ffmpeg_pid, signal.SIGTERM )

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

