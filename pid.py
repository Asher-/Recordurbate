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
    # Written by recorder.py for itself as it starts, so a daemon started
    # after a handoff or a crash can adopt it. The record is written to a
    # temporary file and renamed into place, so a crash never leaves a
    # partial one.
    os.makedirs( RECORD_DIR, exist_ok=True )
    path = record_path( name )
    temporary = "{}.{}.tmp".format( path, pid )
    with open( temporary, "w" ) as f:
        f.write( "{}\n".format(pid) )
    os.replace( temporary, path )

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

def wait_for_exit( pid, timeout ):
    # Whether pid exited within timeout seconds (None waits without bound):
    # kqueue's NOTE_EXIT on macOS and BSD, a pidfd on Linux.
    if hasattr( select, "kqueue" ):
        queue = select.kqueue()
        try:
            exit_event = select.kevent( pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT )
            try:
                return bool( queue.control( [exit_event], 1, timeout ) )
            except ProcessLookupError:
                return True  # already gone
        finally:
            queue.close()
    try:
        pidfd = os.pidfd_open( pid )
    except ProcessLookupError:
        return True  # already gone
    try:
        readable, _, _ = select.select( [pidfd], [], [], timeout )
        return bool( readable )
    finally:
        os.close( pidfd )

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
        # the kernel reports its exit as an event instead.
        if self._returncode is not None:
            return self._returncode
        if not wait_for_exit( self.pid, timeout ):
            raise subprocess.TimeoutExpired( cmd="adopted_process", timeout=timeout )
        self._returncode = -1
        return self._returncode

    def send_signal( self, sig ):
        os.kill( self.pid, sig )

    def terminate( self ):
        self.send_signal( signal.SIGTERM )
