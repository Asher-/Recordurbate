import os
import select
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

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

class WaitForExitRoutesTest( unittest.TestCase ):
    # wait_for_exit's routes one at a time. The kernel calls are replaced
    # where the host lacks them (pidfd off Linux) or cannot be made to fail
    # on demand (a PID vanishing between the check and the kqueue
    # registration); the real calls run in AdoptedProcessWaitTest and in
    # test_pidfd_* where os.pidfd_open exists.

    def without_kqueue( self, pidfd_open ):
        # the Linux route: a select module with no kqueue, and pidfd_open
        return mock.patch.multiple( pid, select=types.SimpleNamespace( select=select.select ) ), \
               mock.patch.object( pid.os, "pidfd_open", pidfd_open, create=True )

    def test_kqueue_registration_on_a_vanished_pid_counts_as_exited( self ):
        class VanishedQueue:
            def control( self, changes, max_events, timeout ):
                raise ProcessLookupError()
            def close( self ):
                pass
        if not hasattr( select, "kqueue" ):
            self.skipTest( "no kqueue on this host" )
        with mock.patch.object( pid.select, "kqueue", VanishedQueue ):
            self.assertTrue( pid.wait_for_exit( 1, 0 ) )

    def test_pidfd_of_an_exited_process_is_readable( self ):
        read_fd, write_fd = os.pipe()
        os.close( write_fd )  # readable at once, as a pidfd is once its process exits
        no_kqueue, pidfd = self.without_kqueue( lambda p: read_fd )
        with no_kqueue, pidfd:
            self.assertTrue( pid.wait_for_exit( 1, 5 ) )
        with self.assertRaises( OSError ):
            os.fstat( read_fd )  # wait_for_exit closed it

    def test_pidfd_of_a_running_process_times_out( self ):
        read_fd, write_fd = os.pipe()  # never readable while write_fd is open
        no_kqueue, pidfd = self.without_kqueue( lambda p: read_fd )
        try:
            with no_kqueue, pidfd:
                self.assertFalse( pid.wait_for_exit( 1, 0 ) )
        finally:
            os.close( write_fd )
        with self.assertRaises( OSError ):
            os.fstat( read_fd )

    def test_pidfd_of_a_vanished_pid_counts_as_exited( self ):
        def vanished( p ):
            raise ProcessLookupError()
        no_kqueue, pidfd = self.without_kqueue( vanished )
        with no_kqueue, pidfd:
            self.assertTrue( pid.wait_for_exit( 1, 0 ) )

    @unittest.skipUnless( hasattr( os, "pidfd_open" ), "pidfd is Linux-only" )
    def test_pidfd_waits_for_a_real_process( self ):
        proc = held()
        with mock.patch.multiple( pid, select=types.SimpleNamespace( select=select.select ) ):
            self.assertFalse( pid.wait_for_exit( proc.pid, 0 ) )
            proc.stdin.close()
            self.assertTrue( pid.wait_for_exit( proc.pid, 30 ) )
        proc.wait()

if __name__ == "__main__":
    unittest.main()
