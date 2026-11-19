import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname( os.path.dirname( os.path.abspath(__file__) ) )
sys.path.insert( 0, ROOT )
from pid import find_running_ytdlp

SLEEP = "import time; time.sleep(30)"

class FindRunningYtdlpTest( unittest.TestCase ):

    def setUp( self ):
        # Processes whose command lines look like each kind the scan meets.
        self.procs = {
            "recorder": subprocess.Popen( [ sys.executable, "-c", SLEEP, "recorder.py", "https://chaturbate.com/pidtest_recorder/" ] ),
            "ytdlp": subprocess.Popen( [ sys.executable, "-c", SLEEP, "yt-dlp", "https://chaturbate.com/pidtest_ytdlp/" ] ),
            "lookup": subprocess.Popen( [ sys.executable, "-c", SLEEP, "yt-dlp", "--dump-single-json", "https://chaturbate.com/pidtest_lookup/" ] ),
            "other": subprocess.Popen( [ sys.executable, "-c", SLEEP, "https://chaturbate.com/pidtest_other/" ] ),
        }

    def tearDown( self ):
        for proc in self.procs.values():
            proc.kill()
            proc.wait()

    def test_finds_recorders_and_ytdlp_but_not_lookups( self ):
        found = find_running_ytdlp()
        self.assertEqual( found.get("pidtest_recorder"), self.procs["recorder"].pid )
        self.assertEqual( found.get("pidtest_ytdlp"), self.procs["ytdlp"].pid )
        self.assertNotIn( "pidtest_lookup", found )
        self.assertNotIn( "pidtest_other", found )

if __name__ == "__main__":
    unittest.main()
