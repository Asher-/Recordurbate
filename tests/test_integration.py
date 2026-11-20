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
import signal
import threading
import time
import unittest
from unittest import mock

TESTS = os.path.dirname( os.path.abspath(__file__) )
ROOT = os.path.dirname( TESTS )
sys.path.insert( 0, ROOT )
sys.path.insert( 0, TESTS )
import test_recorder
from test_recorder import FakeHls, probe
from fifo_wait import open_fifo
import streamer as streamer_module
from streamer import Streamer
from pid import recorded_recorder
import daemon as daemon_module

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

    def gate_lookup_on_readiness_wait( self ):
        # yt-dlp answers only once Streamer is inside its readiness wait:
        # fake_ytdlp.py blocks reading this FIFO until the wait opens and
        # closes its write end.
        gate = os.path.join( self.cwd, "gate" )
        os.mkfifo( gate )
        os.environ[ "FAKE_YTDLP_GATE" ] = gate
        real_select = streamer_module.select.select
        def open_gate_then_select( *args ):
            open_fifo( gate, "w", 30 ).close()
            return real_select( *args )
        patcher = mock.patch.object( streamer_module.select, "select", open_gate_then_select )
        patcher.start()
        self.addCleanup( patcher.stop )

    def test_offline_room_reported_during_the_readiness_wait_is_not_started( self ):
        os.environ[ "FAKE_YTDLP_MODE" ] = "offline"
        self.gate_lookup_on_readiness_wait()
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

    def test_lookup_answered_during_the_readiness_wait_records( self ):
        self.gate_lookup_on_readiness_wait()
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

    def hold_lookup( self ):
        # fake_ytdlp.py blocks opening this FIFO, so the recorder's lookup
        # never answers; cleanup opens it so the held lookup can finish.
        gate = os.path.join( self.cwd, "gate" )
        os.mkfifo( gate )
        os.environ[ "FAKE_YTDLP_GATE" ] = gate
        held = os.open( gate, os.O_RDWR )
        self.addCleanup( os.close, held )

    def test_recorder_that_never_signals_ready_is_not_started( self ):
        self.hold_lookup()
        patcher = mock.patch.object( streamer_module, "READY_TIMEOUT", 1 )
        patcher.start()
        self.addCleanup( patcher.stop )
        daemon = FakeDaemon( self.config )
        streamer = Streamer( daemon, "room" )
        with self.assertLogs( "test-integration", level="INFO" ) as logs:
            daemon.logger.info( "launching room" )
            thread = streamer.start()
            thread.join( 60 )
        self.assertFalse( thread.is_alive() )
        # the deadline passed, so the launch counts against the site and the
        # recorder is ended
        self.assertEqual( len( daemon.launches ), 1 )
        self.assertFalse( any( "Started to record" in line for line in logs.output ) )
        self.assertIsNone( streamer.stream )
        self.assertFalse( os.path.exists( "configs/pid-room" ) )

    def test_stop_during_the_readiness_wait_reports_nothing( self ):
        self.hold_lookup()
        waiting = threading.Event()
        real_select = streamer_module.select.select
        def report_wait( *args ):
            waiting.set()
            return real_select( *args )
        patcher = mock.patch.object( streamer_module.select, "select", report_wait )
        patcher.start()
        self.addCleanup( patcher.stop )
        daemon = FakeDaemon( self.config )
        streamer = Streamer( daemon, "room" )
        thread = streamer.start()
        self.assertTrue( waiting.wait( 30 ) )
        recorder = streamer.stream
        streamer.stop()
        thread.join( 60 )
        self.assertFalse( thread.is_alive() )
        self.assertIsNotNone( recorder.poll() )
        # a stopped launch says nothing about the site
        self.assertEqual( daemon.launches, [] )
        self.assertIsNone( streamer.stream )

class AdoptRecordedRecorderTest( RoomDir ):
    # A recorder launched as Streamer.start launches it, by a daemon that has
    # since gone; it records its own PID, and the readiness pipe says when the
    # room has resolved.

    def launch_recorder( self ):
        ready_fd, write_fd = os.pipe()
        recorder = subprocess.Popen( [ sys.executable, "recorder.py", "https://chaturbate.com/room/", str(write_fd) ],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(write_fd,) )
        os.close( write_fd )
        self.addCleanup( self.end, recorder )
        self.assertEqual( os.read( ready_fd, 16 ), b"ready\n" )
        os.close( ready_fd )
        return recorder

    def end( self, recorder ):
        if recorder.poll() is None:
            recorder.kill()
        recorder.wait()

    def test_recorded_recorder_is_found_and_adopted( self ):
        recorder = self.launch_recorder()
        self.assertEqual( recorded_recorder("room"), recorder.pid )
        streamer = Streamer( FakeDaemon(self.config), "room" )
        with self.assertLogs( "test-integration", level="INFO" ) as logs:
            thread = streamer.adopt( recorded_recorder("room") )
            thread.join( 60 )
        self.assertFalse( thread.is_alive() )
        self.assertTrue( any( "Adopted stream for room validated." in line for line in logs.output ) )
        self.assertIsNone( streamer.stream )
        self.assertEqual( recorder.wait(), 0 )
        # the recorder removed its own record as it exited
        self.assertFalse( os.path.exists( "configs/pid-room" ) )
        codecs, container, duration = probe( self.recording() )
        self.assertEqual( codecs, [ "aac", "h264" ] )

    def test_daemon_adopts_the_recorded_recorder_at_start( self ):
        recorder = self.launch_recorder()
        daemon = daemon_module.Daemon.__new__( daemon_module.Daemon )
        daemon.logger = logging.getLogger( "test-integration" )
        daemon.config = self.config
        daemon.streamers_lock = threading.Lock()
        daemon.streamers = { "room": Streamer( daemon, "room" ) }
        threads = []
        real_adopt = Streamer.adopt
        def adopt_and_keep_thread( streamer, pid ):
            threads.append( real_adopt( streamer, pid ) )
            return threads[-1]
        with mock.patch.object( Streamer, "adopt", adopt_and_keep_thread ), \
             self.assertLogs( "test-integration", level="INFO" ) as logs:
            daemon.adopt_existing()
            self.assertEqual( len(threads), 1 )
            threads[0].join( 60 )
        self.assertFalse( threads[0].is_alive() )
        self.assertTrue( any( "Adopting recorder PID {} for room.".format(recorder.pid) in line for line in logs.output ) )
        self.assertTrue( any( "Adopted stream for room validated." in line for line in logs.output ) )
        self.assertIsNone( daemon.streamers["room"].stream )

class RecorderStopTest( RoomDir ):

    def test_sigterm_during_the_lookup_ends_the_recorder_and_its_record( self ):
        # fake_ytdlp.py blocks on this FIFO; opening its write end returns
        # once the lookup is waiting on it, and it is never answered.
        gate = os.path.join( self.cwd, "gate" )
        os.mkfifo( gate )
        os.environ[ "FAKE_YTDLP_GATE" ] = gate
        recorder = subprocess.Popen( [ sys.executable, "recorder.py", "https://chaturbate.com/room/" ],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True )
        writer = open_fifo( gate, "w", 30 )
        try:
            self.assertEqual( recorded_recorder("room"), recorder.pid )
            recorder.send_signal( signal.SIGTERM )
            out, _ = recorder.communicate( timeout=15 )
        finally:
            writer.close()
            if recorder.poll() is None:
                recorder.kill()
                recorder.wait()
        self.assertEqual( recorder.returncode, 1 )
        self.assertIn( b"Cannot record room: yt-dlp lookup interrupted", out )
        self.assertFalse( os.path.exists( "configs/pid-room" ) )

if __name__ == "__main__":
    unittest.main()
