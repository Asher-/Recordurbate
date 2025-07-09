import psutil
import sys
import os

def get_pid_by_name_and_args(process_name, args_substring=None):
    """
    Finds the PID of a process by its name and optionally by a substring within its command-line arguments.

    Args:
        process_name (str): The name of the process (e.g., 'python.exe', 'chrome.exe').
        args_substring (str, optional): A substring to search for within the process's command-line arguments.

    Returns:
        int or None: The PID of the matching process, or None if no match is found.
    """
    for process in psutil.process_iter(['pid']):
        try:
            # Check process name
            if process.name().lower() == process_name.lower():
                # If args_substring is provided, check command-line arguments
                if args_substring:
                    cmdline = " ".join(process.cmdline())
                    if args_substring in cmdline:
                        return process.info['pid']
                else:
                    # No args_substring, return PID based on name only
                    return process.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Handle potential errors when accessing process info
            pass
    return None
