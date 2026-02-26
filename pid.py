import psutil
import subprocess
import signal
import re
import os

def process_active(pid: int or str) -> bool:
    try:
        process = psutil.Process(pid)
    except psutil.Error as error:  # includes NoSuchProcess error
        return False
    if psutil.pid_exists(pid) and process.status() not in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
        return True
    return False
    
def get_pid_by_name_and_args(process_name, args_substring=None, exe_path=None):
    """
    Finds the PID of a process by its name and optionally by a substring within its command-line arguments.

    Args:
        process_name (str): The name of the process (e.g., 'python.exe', 'chrome.exe').
        args_substring (str, optional): A substring to search for within the process's command-line arguments.

    Returns:
        int or None: The PID of the matching process, or None if no match is found.
    """
    processes = psutil.process_iter(['pid'])
    for process in processes:
        try:
            # Check process name
            if process.name().lower() == process_name.lower():
                # If args_substring is provided, check command-line arguments
                should_match = True
                if should_match and args_substring:
                    cmdline = " ".join(process.cmdline())
                    if args_substring not in cmdline:
                        should_match = False
                if should_match and exe_path:
                    if exe_path not in process.exe():
                        should_match = False
                if should_match:
                    return process.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Handle potential errors when accessing process info
            pass
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
        try:
            self._process.wait( timeout=timeout )
            self._returncode = -1
            return self._returncode
        except psutil.TimeoutExpired:
            raise subprocess.TimeoutExpired( cmd="adopted_process", timeout=timeout )
        except psutil.NoSuchProcess:
            self._returncode = -1
            return self._returncode

    def send_signal( self, sig ):
        os.kill( self.pid, sig )

    def terminate( self ):
        self.send_signal( signal.SIGTERM )

def find_running_ytdlp():
    results = {}
    pattern = re.compile( r'chaturbate\.com/([^/]+)/' )
    for process in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = process.info.get('cmdline')
            if not cmdline:
                continue
            cmdline_str = ' '.join( cmdline )
            if 'yt-dlp' not in cmdline_str and 'yt_dlp' not in cmdline_str:
                continue
            match = pattern.search( cmdline_str )
            if match:
                name = match.group(1)
                results[ name ] = process.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return results
