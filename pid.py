import psutil
import select
import subprocess
import signal
import os

RECORD_DIR = "configs"

def process_active(pid: int or str) -> bool:
    try:
        process = psutil.Process(pid)
    except psutil.Error as error:  # includes NoSuchProcess error
        return False
    if psutil.pid_exists(pid) and process.status() not in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
        return True
    return False

def record_path( name ):
    return os.path.join( RECORD_DIR, "pid-{}".format(name) )

def record_recorder( name, pid ):
    # Written when Streamer launches a recorder, so a daemon started after a
    # handoff can adopt it.
    with open( record_path(name), "w" ) as f:
        f.write( "{}\n".format(pid) )

def forget_recorder( name, pid ):
    # Removes the record when it still names pid; a newer launch may have
    # replaced it.
    path = record_path( name )
    try:
        with open( path ) as f:
            recorded = f.read().strip()
        if recorded == str(pid):
            os.remove( path )
    except FileNotFoundError:
        pass

def recorded_recorder( name ):
    # The PID recorded for name's recorder when that process is still a live
    # recorder.py for that room, else None. A stale record is removed.
    path = record_path( name )
    try:
        with open( path ) as f:
            pid = int( f.read().strip() )
    except (FileNotFoundError, ValueError):
        return None
    try:
        cmdline = " ".join( psutil.Process(pid).cmdline() )
    except psutil.Error:
        cmdline = ""
    if process_active(pid) and "recorder.py" in cmdline and "chaturbate.com/{}/".format(name) in cmdline:
        return pid
    forget_recorder( name, pid )
    return None

class AdoptedProcess:

    def __init__( self, pid ):
        self.pid = pid
        self._process = psutil.Process( pid )
        self._returncode = None

    def poll( self ):
        if self._returncode is not None:
            return self._returncode
        try:
            status = self._process.status()
            if status in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
                self._returncode = -1
                return self._returncode
            return None
        except psutil.NoSuchProcess:
            self._returncode = -1
            return self._returncode

    def wait( self, timeout=None ):
        # An adopted recorder is not our child, so waitpid cannot reach it;
        # kqueue reports its exit as an event.
        if self._returncode is not None:
            return self._returncode
        queue = select.kqueue()
        try:
            exit_event = select.kevent( self.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT )
            try:
                events = queue.control( [exit_event], 1, timeout )
            except ProcessLookupError:
                events = [ exit_event ]  # already gone
        finally:
            queue.close()
        if not events:
            raise subprocess.TimeoutExpired( cmd="adopted_process", timeout=timeout )
        self._returncode = -1
        return self._returncode

    def send_signal( self, sig ):
        os.kill( self.pid, sig )

    def terminate( self ):
        self.send_signal( signal.SIGTERM )
