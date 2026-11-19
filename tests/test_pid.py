import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname( os.path.dirname( os.path.abspath(__file__) ) )
sys.path.insert( 0, ROOT )
import pid
from pid import record_recorder, forget_recorder, recorded_recorder, AdoptedProcess

# Runs until its stdin is closed, so a test ends it by an event, not a timer.
UNTIL_STDIN_CLOSES = "import sys; sys.stdin.read()"

def held( *args ):
    return subprocess.Popen( [ sys.executable, "-c", UNTIL_STDIN_CLOSES ] + list(args), stdin=subprocess.PIPE )

def release( proc ):
    proc.stdin.close()
    proc.wait()

class RecordedRecorderTest( unittest.TestCase ):

    def setUp( self ):
        self.cwd = os.getcwd()
        self.dir = tempfile.TemporaryDirectory()
        os.chdir( self.dir.name )
        os.mkdir( "configs" )
        self.procs = []

    def tearDown( self ):
        for proc in self.procs:
            if proc.poll() is None:
                release( proc )
        os.chdir( self.cwd )
        self.dir.cleanup()

    def launch( self, *args ):
        proc = held( *args )
        self.procs.append( proc )
        return proc

    def test_live_recorder_for_the_room_is_returned( self ):
        proc = self.launch( "recorder.py", "https://chaturbate.com/alice/" )
        record_recorder( "alice", proc.pid )
        self.assertEqual( recorded_recorder("alice"), proc.pid )
        self.assertTrue( os.path.exists("configs/pid-alice") )

    def test_no_record_is_none( self ):
        self.assertIsNone( recorded_recorder("alice") )

    def test_process_that_is_not_a_recorder_is_refused_and_forgotten( self ):
        proc = self.launch( "https://chaturbate.com/alice/" )
        record_recorder( "alice", proc.pid )
        self.assertIsNone( recorded_recorder("alice") )
        self.assertFalse( os.path.exists("configs/pid-alice") )

    def test_recorder_for_another_room_is_refused( self ):
        proc = self.launch( "recorder.py", "https://chaturbate.com/bob/" )
        record_recorder( "alice", proc.pid )
        self.assertIsNone( recorded_recorder("alice") )

    def test_exited_recorder_is_refused_and_forgotten( self ):
        proc = self.launch( "recorder.py", "https://chaturbate.com/alice/" )
        record_recorder( "alice", proc.pid )
        release( proc )
        self.assertIsNone( recorded_recorder("alice") )
        self.assertFalse( os.path.exists("configs/pid-alice") )

    def test_forget_keeps_a_record_of_a_newer_launch( self ):
        record_recorder( "alice", 2 )
        forget_recorder( "alice", 1 )
        self.assertTrue( os.path.exists("configs/pid-alice") )
        forget_recorder( "alice", 2 )
        self.assertFalse( os.path.exists("configs/pid-alice") )

    def test_forget_without_a_record_does_nothing( self ):
        forget_recorder( "alice", 1 )

class AdoptedProcessWaitTest( unittest.TestCase ):

    def test_wait_times_out_while_the_process_runs( self ):
        proc = held()
        try:
            with self.assertRaises( subprocess.TimeoutExpired ):
                AdoptedProcess( proc.pid ).wait( timeout=0 )
        finally:
            release( proc )

    def test_wait_returns_once_the_process_exits( self ):
        proc = held()
        adopted = AdoptedProcess( proc.pid )
        with self.assertRaises( subprocess.TimeoutExpired ):
            adopted.wait( timeout=0 )
        proc.stdin.close()
        self.assertEqual( adopted.wait(), -1 )
        self.assertEqual( adopted.poll(), -1 )
        proc.wait()

    def test_wait_on_a_process_already_gone_returns_at_once( self ):
        proc = held()
        adopted = AdoptedProcess( proc.pid )
        release( proc )
        self.assertEqual( adopted.wait(), -1 )
        self.assertEqual( adopted.wait(), -1 )

if __name__ == "__main__":
    unittest.main()
