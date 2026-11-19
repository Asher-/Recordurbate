"""Daemon-side seams composed with a real recorder.py.

Streamer.start launches the real recorder.py through a real Popen, and a
running recorder is found through its PID record and adopted by Streamer.adopt.
FakeHls (the CDN) and fake_ytdlp.py (yt-dlp) are the only stand-ins.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = os.path.dirname( os.path.abspath(__file__) )
ROOT = os.path.dirname( TESTS )
sys.path.insert( 0, ROOT )
sys.path.insert( 0, TESTS )
import test_recorder
from test_recorder import FakeHls, probe
from streamer import Streamer
from pid import record_recorder, recorded_recorder

def setUpModule():
    test_recorder.setUpModule()

def tearDownModule():
    test_recorder.tearDownModule()

class FakeDaemon:

    def __init__( self, config ):
        self.config = config
        self.logger = logging.getLogger( "test-integration" )
        self.launches = []

    def record_launch( self, error = None ):
        self.launches.append( error )

class RoomDir( unittest.TestCase ):
    # A daemon working directory: configs/config.json naming fake_ytdlp.py,
    # and recorder.py where Streamer launches it from.

    def setUp( self ):
        self.previous = os.getcwd()
        self.cwd = tempfile.mkdtemp()
        os.makedirs( os.path.join(self.cwd, "configs") )
        self.config = { "youtube-dl_cmd": "{} {}".format(sys.executable, os.path.join(TESTS, "fake_ytdlp.py")),
                        "youtube-dl_config": "configs/unused.config" }
        with open( os.path.join(self.cwd, "configs", "config.json"), "w" ) as f:
            json.dump( self.config, f )
        os.symlink( os.path.join(ROOT, "recorder.py"), os.path.join(self.cwd, "recorder.py") )
        os.chdir( self.cwd )
        self.hls = FakeHls()
        self.env = dict( os.environ )
        os.environ[ "FAKE_HLS_BASE" ] = self.hls.base

    def tearDown( self ):
        self.hls.close()
        os.environ.clear()
        os.environ.update( self.env )
        os.chdir( self.previous )
        shutil.rmtree( self.cwd )

    def recording( self ):
        return os.path.join( self.cwd, "videos", "room", "room 2026-01-01 00_00.ts" )

class StreamerLaunchesRecorderTest( RoomDir ):

    def test_start_records_until_the_stream_ends( self ):
        daemon = FakeDaemon( self.config )
        streamer = Streamer( daemon, "room" )
        thread = streamer.start()
        thread.join( 60 )
        self.assertFalse( thread.is_alive() )
        self.assertEqual( daemon.launches, [ None ] )
        self.assertIsNone( streamer.stream )
        codecs, container, duration = probe( self.recording() )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( container, "mpegts" )
        with open( os.path.join("configs", "capture-room.log") ) as f:
            self.assertIn( "Stream ended", f.read() )

    def test_offline_room_reported_after_the_poll_window_is_not_started( self ):
        # yt-dlp answers 5s after launch, so Streamer waits through the lookup.
        os.environ[ "FAKE_YTDLP_MODE" ] = "offline"
        os.environ[ "FAKE_YTDLP_DELAY" ] = "5"
        daemon = FakeDaemon( self.config )
        streamer = Streamer( daemon, "room" )
        with self.assertLogs( "test-integration", level="INFO" ) as logs:
            daemon.logger.info( "launching room" )
            thread = streamer.start()
            thread.join( 60 )
        self.assertFalse( thread.is_alive() )
        self.assertEqual( daemon.launches, [ None ] )
        self.assertFalse( any( "Started to record" in line for line in logs.output ) )
        self.assertFalse( os.path.exists( self.recording() ) )

    def test_slow_lookup_of_a_live_room_records( self ):
        os.environ[ "FAKE_YTDLP_DELAY" ] = "5"
        daemon = FakeDaemon( self.config )
        streamer = Streamer( daemon, "room" )
        with self.assertLogs( "test-integration", level="INFO" ) as logs:
            thread = streamer.start()
            thread.join( 60 )
        self.assertFalse( thread.is_alive() )
        self.assertEqual( daemon.launches, [ None ] )
        self.assertTrue( any( "Started to record room." in line for line in logs.output ) )
        codecs, container, duration = probe( self.recording() )
        self.assertEqual( codecs, [ "aac", "h264" ] )

class AdoptRecordedRecorderTest( RoomDir ):

    def test_recorded_recorder_is_found_and_adopted( self ):
        # Launched and recorded as Streamer.start does; the readiness pipe says
        # when the room has resolved.
        ready_fd, write_fd = os.pipe()
        recorder = subprocess.Popen( [ sys.executable, "recorder.py", "https://chaturbate.com/room/", str(write_fd) ],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(write_fd,) )
        os.close( write_fd )
        record_recorder( "room", recorder.pid )
        try:
            self.assertEqual( os.read( ready_fd, 16 ), b"ready\n" )
            os.close( ready_fd )
            self.assertEqual( recorded_recorder("room"), recorder.pid )
            streamer = Streamer( FakeDaemon(self.config), "room" )
            with self.assertLogs( "test-integration", level="INFO" ) as logs:
                thread = streamer.adopt( recorded_recorder("room") )
                thread.join( 60 )
            self.assertFalse( thread.is_alive() )
            self.assertTrue( any( "Adopted stream for room validated." in line for line in logs.output ) )
            self.assertIsNone( streamer.stream )
            self.assertFalse( os.path.exists( "configs/pid-room" ) )
        finally:
            if recorder.poll() is None:
                recorder.kill()
            recorder.wait()
        codecs, container, duration = probe( self.recording() )
        self.assertEqual( codecs, [ "aac", "h264" ] )

if __name__ == "__main__":
    unittest.main()
