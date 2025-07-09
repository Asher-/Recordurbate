import psutil
import sys
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
