import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname( os.path.dirname( os.path.abspath(__file__) ) )
sys.path.insert( 0, ROOT )
import streamer
from streamer import Streamer, NO_ERROR

class FakeDaemon:

    def __init__( self, config = None ):
        self.config = config or {}
        self.logger = logging.getLogger( "test-streamer" )
        self.launches = []

    def record_launch( self, error = None ):
        self.launches.append( error )

class InTempDir( unittest.TestCase ):

    def setUp( self ):
        self.previous = os.getcwd()
        self.cwd = tempfile.mkdtemp()
        os.chdir( self.cwd )

    def tearDown( self ):
        os.chdir( self.previous )
        shutil.rmtree( self.cwd )

class EnsureValidStreamTest( unittest.TestCase ):

    def test_alive_process_is_valid( self ):
        proc = subprocess.Popen( [ "sleep", "30" ] )
        try:
            self.assertTrue( Streamer(FakeDaemon(), "x").ensure_valid_stream(pid=proc.pid) )
        finally:
            proc.kill()
            proc.wait()

    def test_exited_process_is_invalid( self ):
        proc = subprocess.Popen( [ "true" ] )
        proc.wait()
        self.assertFalse( Streamer(FakeDaemon(), "x").ensure_valid_stream(pid=proc.pid) )

    def test_no_process_is_invalid( self ):
        self.assertFalse( Streamer(FakeDaemon(), "x").ensure_valid_stream() )

    def test_uses_own_stream_pid( self ):
        s = Streamer( FakeDaemon(), "x" )
        s.stream = subprocess.Popen( [ "sleep", "30" ] )
        try:
            self.assertTrue( s.ensure_valid_stream() )
        finally:
            s.stream.kill()
            s.stream.wait()

class ExitedProcess:

    pid = 999999

    def poll( self ):
        return 1

    def wait( self, timeout = None ):
        return 1

class StartTest( InTempDir ):

    def test_launches_recorder_for_the_room( self ):
        os.makedirs( "configs" )
        daemon = FakeDaemon( { "process_poll_wait_time": 2 } )
        launched = []
        def fake_popen( args, *rest, **kwargs ):
            launched.append( (args, kwargs) )
            return ExitedProcess()
        with mock.patch.object( streamer.subprocess, "Popen", fake_popen ):
            Streamer( daemon, "someroom" ).start().join( 10 )
        args, kwargs = launched[0]
        self.assertEqual( args, [ sys.executable, "recorder.py", "https://chaturbate.com/someroom/" ] )
        self.assertTrue( kwargs["start_new_session"] )
        self.assertEqual( daemon.launches, [ NO_ERROR ] )

class StallingProcess:
    # wait() times out until the process is signalled, `waits` times at most.

    def __init__( self, waits ):
        self.waits = waits
        self.signals = []

    def wait( self, timeout = None ):
        if self.signals:
            return 0
        self.waits -= 1
        if self.waits < 0:
            return 0
        raise subprocess.TimeoutExpired( "recorder", timeout )

    def send_signal( self, sig ):
        self.signals.append( sig )

    def terminate( self ):
        self.signals.append( signal.SIGTERM )

class WatchdogTest( InTempDir ):

    def watch( self, filename ):
        os.makedirs( os.path.join("videos", "x") )
        open( os.path.join("videos", "x", filename), "wb" ).close()
        proc = StallingProcess( waits=5 )
        Streamer( FakeDaemon({ "stale_stream_timeout": 0 }), "x" ).wait_with_watchdog( proc )
        return proc.signals

    def test_unchanging_ts_is_stale( self ):
        self.assertEqual( self.watch("rec.ts"), [ signal.SIGINT ] )

    def test_unchanging_part_is_stale( self ):
        self.assertEqual( self.watch("rec.mp4.part"), [ signal.SIGINT ] )

    def test_other_files_are_not_watched( self ):
        self.assertEqual( self.watch("rec.mp4"), [] )

if __name__ == "__main__":
    unittest.main()
