import os
import signal
import subprocess
import time
import threading

import json
from pprint import pprint

import requests
from config import load_config

import asyncio
from concurrent.futures import Future


class Bot:

    error = False
    running = True
    config = None
    processes = []

    logger = None

    def __init__(self, logger):
        self.logger = logger

        # load config
        self.reload_config()

        # reg signals
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def stop(self, signum, stack):
        if self.running:
            self.logger.info("Caught stop signal, stopping")
            self.running = False

    def reload_config(self):

        # if config not loaded at all
        if self.config is None:
            self.config = load_config()

            for idx, name in enumerate(self.config["streamers"]):
                # add info
                self.config["streamers"][idx] = [name, False]

            return

        # load new
        new_config = load_config()

        # remove all deleted streamers
        for idx, streamer in enumerate(self.config["streamers"]):
            if streamer[0] not in new_config["streamers"]:
                self.logger.info("{} has been removed".format(streamer[0]))
                del self.config["streamers"][idx]

        # add all new streamers
        for new_streamer in new_config["streamers"]:

            # find streamer
            found = False
            for streamer in self.config["streamers"]:
                if streamer[0] == new_streamer:
                    found = True

            # add if not found
            if not found:
                self.config["streamers"].append([new_streamer, False])

    def handle_stream(self, streamer):
        """
        Runs the given args in a subprocess.Popen, and then calls the function
        on_exit when the subprocess completes.
        on_exit is a callable object, and popen_args is a list/tuple of args that 
        would give to subprocess.Popen.
        """
        def run_in_thread():
            process_args = self.config["youtube-dl_cmd"].split(" ") + ["https://chaturbate.com/{}/".format(streamer[0]), "--config-location", self.config["youtube-dl_config"]] 
            process = subprocess.Popen( process_args, 0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL )
            # sleep (10)? and make sure process didn't immediately exit
            poll_wait_time = self.config["process_poll_wait_time"]
            if poll_wait_time is None:
                poll_wait_time = 3
            time.sleep( poll_wait_time )
            if process.poll() is None:
                self.logger.info("Started to record {}.".format(streamer[0]))
                streamer[1] = process
            process.wait()
            streamer[1] = False
            return
        thread = threading.Thread(target=run_in_thread, args=())
        thread.start()
        # returns immediately after the thread starts
        return thread

    def run(self):
        while self.running:
            
            # debug
            try:

                # reload config
                if self.config["auto_reload_config"]:
                    self.reload_config()

                # check to start recording
                for idx, streamer in enumerate(self.config["streamers"]):

                    # if already recording
                    if streamer[1]:
                        continue

                    self.handle_stream( streamer )

                    if self.config["rate_limit"]:
                        time.sleep(self.config["rate_limit_time"])

                # wait 1 min in 1 second intervals
                for i in range(60):
                    if not self.running:
                        break

                    time.sleep(1)
                
            except Exception:
                self.logger.exception("loop error")
                time.sleep(1)

        # loop ended, stop all recording
        for idx, streamer in enumerate(self.config["streamers"]):
            if streamer[1]:
                self.logger.info("Signaling {} to stop.".format(streamer[0]))
                streamer[1].send_signal(signal.SIGINT)
                streamer[1].wait()
                self.logger.info("Stopped {}.".format(streamer[0]))
        
        self.logger.info("Successfully stopped.")
