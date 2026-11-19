import logging
import os
import shutil
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
        self.assertEqual( args[:3], [ sys.executable, "recorder.py", "https://chaturbate.com/someroom/" ] )
        self.assertEqual( kwargs["pass_fds"], ( int(args[3]), ) )
        self.assertTrue( kwargs["start_new_session"] )
        self.assertEqual( daemon.launches, [ NO_ERROR ] )

if __name__ == "__main__":
    unittest.main()
