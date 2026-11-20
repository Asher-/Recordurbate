import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import requests

ROOT = os.path.dirname( os.path.dirname( os.path.abspath(__file__) ) )
sys.path.insert( 0, ROOT )
import recorder

def split_boxes( data ):
    boxes = []
    offset = 0
    while offset < len( data ):
        size = int.from_bytes( data[offset:offset + 4], "big" )
        kind = data[offset + 4:offset + 8].decode("latin-1")
        if size == 1:
            size = int.from_bytes( data[offset + 8:offset + 16], "big" )
        boxes.append( (kind, data[offset:offset + size]) )
        offset += size
    return boxes

def fragmented_mp4( path, source, codec ):
    # Returns (init, [fragment, ...]) of a 6s fragmented MP4, the shape of an
    # LL-HLS fMP4 track: ftyp+moov, then one moof+mdat per segment.
    subprocess.run( [ "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-t", "6" ] + codec +
                    [ "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "1000000", "-f", "mp4", path ],
                    check=True )
    with open( path, "rb" ) as f:
        data = f.read()
    init = b""
    fragments = []
    moof = b""
    for kind, box in split_boxes( data ):
        if kind in ("ftyp", "moov"):
            init += box
        elif kind == "moof":
            moof = box
        elif kind == "mdat":
            fragments.append( moof + box )
    return init, fragments

def probe( path ):
    out = subprocess.run( [ "ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration,format_name", "-of", "json", path ],
                          check=True, stdout=subprocess.PIPE ).stdout
    info = json.loads( out )
    return sorted( s["codec_name"] for s in info["streams"] ), info["format"]["format_name"], float( info["format"]["duration"] )

class Fixtures:
    tmp = None
    video = None
    audio = None

def setUpModule():
    Fixtures.tmp = tempfile.mkdtemp()
    Fixtures.video = fragmented_mp4( os.path.join(Fixtures.tmp, "v.mp4"), "testsrc=size=320x240:rate=25",
                                     [ "-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p" ] )
    Fixtures.audio = fragmented_mp4( os.path.join(Fixtures.tmp, "a.mp4"), "sine=frequency=440:sample_rate=48000",
                                     [ "-c:a", "aac" ] )

def tearDownModule():
    shutil.rmtree( Fixtures.tmp )

LL_HLS = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:2
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=2.430000
#EXT-X-PART-INF:PART-TARGET=0.800000
#EXT-X-MEDIA-SEQUENCE:5296
#EXT-X-MAP:URI="/streams/init_3_video.m4s?session=abc"
#EXT-X-PROGRAM-DATE-TIME:2026-11-19T10:22:16.401+00:00
#EXTINF:1.600000,
/streams/seg_3_5296_video.m4s?session=abc
#EXT-X-PROGRAM-DATE-TIME:2026-11-19T10:22:18.001+00:00
#EXTINF:1.600000,
/streams/seg_3_5297_video.m4s?session=abc
#EXT-X-PART:DURATION=0.766000,URI="/streams/part_3_5298_0_video.m4s?session=abc",INDEPENDENT=YES
#EXT-X-PART:DURATION=0.800000,URI="/streams/part_3_5298_1_video.m4s?session=abc"
#EXTINF:1.600000,
/streams/seg_3_5298_video.m4s?session=abc
#EXT-X-PART:DURATION=0.800000,URI="/streams/part_3_5299_0_video.m4s?session=abc",INDEPENDENT=YES
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="/streams/part_3_5299_1_video.m4s?session=abc"
"""

class ParsePlaylistTest( unittest.TestCase ):

    def test_full_segments_numbered_from_media_sequence( self ):
        segments, init_uri, ended = recorder.parse_playlist( LL_HLS )
        self.assertEqual( segments, [
            (5296, "/streams/seg_3_5296_video.m4s?session=abc"),
            (5297, "/streams/seg_3_5297_video.m4s?session=abc"),
            (5298, "/streams/seg_3_5298_video.m4s?session=abc"),
        ] )
        self.assertEqual( init_uri, "/streams/init_3_video.m4s?session=abc" )
        self.assertFalse( ended )

    def test_endlist( self ):
        segments, init_uri, ended = recorder.parse_playlist( LL_HLS + "#EXT-X-ENDLIST\n" )
        self.assertTrue( ended )
        self.assertEqual( len(segments), 3 )

    def test_no_map_no_sequence( self ):
        segments, init_uri, ended = recorder.parse_playlist( "#EXTM3U\n#EXTINF:2,\na.ts\n#EXTINF:2,\nb.ts\n" )
        self.assertEqual( segments, [ (0, "a.ts"), (1, "b.ts") ] )
        self.assertIsNone( init_uri )

class FakeResponse:

    def __init__( self, status, content = b"" ):
        self.status_code = status
        self.content = content

class FakeSession:

    def __init__( self, outcomes ):
        self.outcomes = list( outcomes )
        self.calls = 0

    def get( self, url, timeout ):
        self.calls += 1
        outcome = self.outcomes.pop( 0 )
        if isinstance( outcome, Exception ):
            raise outcome
        return outcome

@mock.patch.object( recorder.time, "sleep" )
class FetchTest( unittest.TestCase ):

    def test_first_success( self, sleep ):
        session = FakeSession( [ FakeResponse(200, b"x") ] )
        self.assertEqual( recorder.fetch(session, "u"), (b"x", None) )
        self.assertEqual( session.calls, 1 )

    def test_retries_after_connection_error( self, sleep ):
        session = FakeSession( [ requests.ConnectionError("reset"), FakeResponse(200, b"x") ] )
        self.assertEqual( recorder.fetch(session, "u"), (b"x", None) )
        self.assertEqual( session.calls, 2 )

    def test_gives_up_after_three_attempts( self, sleep ):
        session = FakeSession( [ requests.ReadTimeout("t") ] * 3 )
        data, reason = recorder.fetch( session, "u" )
        self.assertIsNone( data )
        self.assertIn( "ReadTimeout", reason )
        self.assertEqual( session.calls, 3 )
        self.assertEqual( sleep.call_count, 2 )

    def test_stops_on_not_found( self, sleep ):
        session = FakeSession( [ FakeResponse(404) ] )
        self.assertEqual( recorder.fetch(session, "u"), (None, "HTTP 404") )
        self.assertEqual( session.calls, 1 )

    def test_retries_server_error( self, sleep ):
        session = FakeSession( [ FakeResponse(503), FakeResponse(200, b"x") ] )
        self.assertEqual( recorder.fetch(session, "u"), (b"x", None) )

class MuxerTest( unittest.TestCase ):

    def test_muxes_fmp4_tracks_into_mpegts( self ):
        path = os.path.join( Fixtures.tmp, "muxed.ts" )
        video_init, video = Fixtures.video
        audio_init, audio = Fixtures.audio
        muxer = recorder.Muxer( path, [ video_init, audio_init ] )
        for fragment in video:
            muxer.feed( 0, fragment )
        for fragment in audio:
            muxer.feed( 1, fragment )
        muxer.close()
        codecs, container, duration = probe( path )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( container, "mpegts" )
        self.assertAlmostEqual( duration, 6, delta=0.5 )
        self.assertFalse( muxer.alive() )

    def test_restarted_muxer_appends( self ):
        path = os.path.join( Fixtures.tmp, "appended.ts" )
        video_init, video = Fixtures.video
        audio_init, audio = Fixtures.audio
        for half in ( slice(0, 3), slice(3, None) ):
            muxer = recorder.Muxer( path, [ video_init, audio_init ] )
            for fragment in video[half]:
                muxer.feed( 0, fragment )
            for fragment in audio[half]:
                muxer.feed( 1, fragment )
            muxer.close()
        codecs, container, duration = probe( path )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        subprocess.run( [ "ffmpeg", "-v", "error", "-i", path, "-f", "null", "-" ], check=True )

class FakeHls:
    # Serves a live LL-HLS room from the fixtures: one segment per track
    # becomes available each second, the playlist window holds three, and
    # ENDLIST follows the last. Segments in `failing` answer 500.

    FIRST = 100

    def __init__( self, failing = (), outage = None, hang = False ):
        # outage=(start, end) answers 503 to everything in that window of
        # seconds; hang=True never answers a media segment.
        self.failing = set( failing )
        self.outage = outage
        self.hang = hang
        self.started = time.time()
        # set once two media segments of each track have been served: the
        # recorder fetches a track in order, so its first has reached the muxer
        self.served = threading.Event()
        self.served_lock = threading.Lock()
        self.served_counts = {}
        fake = self
        class Handler( http.server.BaseHTTPRequestHandler ):
            def log_message( self, *args ):
                pass
            def do_GET( self ):
                fake.handle( self )
        self.server = http.server.ThreadingHTTPServer( ("127.0.0.1", 0), Handler )
        self.base = "http://127.0.0.1:{}".format( self.server.server_address[1] )
        threading.Thread( target=self.server.serve_forever, daemon=True ).start()

    def track( self, letter ):
        return Fixtures.video if letter == "v" else Fixtures.audio

    def handle( self, request ):
        letter, name = request.path.split("?")[0].strip("/").split("/")
        elapsed = time.time() - self.started
        if self.outage and self.outage[0] <= elapsed < self.outage[1]:
            request.send_response( 503 )
            request.end_headers()
            return
        if self.hang and name.startswith("seg_"):
            time.sleep( 30 )
            return
        init, fragments = self.track( letter )
        if name == "playlist.m3u8":
            available = min( len(fragments), int(time.time() - self.started) + 1 )
            first = max( 0, available - 3 )
            lines = [ "#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-TARGETDURATION:1",
                      "#EXT-X-MEDIA-SEQUENCE:{}".format(self.FIRST + first), '#EXT-X-MAP:URI="init.mp4"' ]
            for index in range( first, available ):
                lines += [ "#EXTINF:1.0,", "seg_{}.m4s".format(self.FIRST + index) ]
            if available == len( fragments ) and time.time() - self.started > len( fragments ) + 1:
                lines.append( "#EXT-X-ENDLIST" )
            body = ( "\n".join(lines) + "\n" ).encode()
        elif name == "init.mp4":
            body = init
        else:
            sequence = int( name[len("seg_"):-len(".m4s")] )
            if (letter, sequence) in self.failing:
                request.send_response( 500 )
                request.end_headers()
                return
            body = fragments[ sequence - self.FIRST ]
        request.send_response( 200 )
        request.send_header( "Content-Length", str(len(body)) )
        request.end_headers()
        request.wfile.write( body )
        if name.startswith( "seg_" ):
            with self.served_lock:
                self.served_counts[ letter ] = self.served_counts.get( letter, 0 ) + 1
                if len( self.served_counts ) == 2 and min( self.served_counts.values() ) >= 2:
                    self.served.set()

    def close( self ):
        self.server.shutdown()

class RecorderRunTest( unittest.TestCase ):
    # Runs recorder.py as the daemon does, against FakeHls and fake_ytdlp.py.

    def setUp( self ):
        self.cwd = tempfile.mkdtemp()
        os.makedirs( os.path.join(self.cwd, "configs") )
        with open( os.path.join(self.cwd, "configs", "config.json"), "w" ) as f:
            json.dump( { "youtube-dl_cmd": "{} {}".format(sys.executable, os.path.join(ROOT, "tests", "fake_ytdlp.py")),
                         "youtube-dl_config": "configs/unused.config" }, f )

    def tearDown( self ):
        shutil.rmtree( self.cwd )

    def run_recorder( self, env, timeout = 40 ):
        return subprocess.run( [ sys.executable, os.path.join(ROOT, "recorder.py"), "https://chaturbate.com/room/" ],
                               cwd=self.cwd, env=dict(os.environ, **env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout, text=True )

    def test_records_both_tracks_across_a_failed_segment( self ):
        hls = FakeHls( failing=[ ("v", 102) ] )
        try:
            result = self.run_recorder( { "FAKE_HLS_BASE": hls.base } )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Track 0 segment 102 failed: HTTP 500", result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        path = os.path.join( self.cwd, "videos", "room", "room 2026-01-01 00_00.ts" )
        codecs, container, duration = probe( path )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( container, "mpegts" )
        self.assertGreater( duration, 4 )

    def test_offline_room_exits_with_ytdlp_error( self ):
        result = self.run_recorder( { "FAKE_YTDLP_MODE": "offline" } )
        self.assertEqual( result.returncode, 1 )
        self.assertIn( "ERROR: [Chaturbate] room: Room is currently offline", result.stdout )
        self.assertFalse( os.path.exists(os.path.join(self.cwd, "videos")) )

    def test_recording_leaves_the_cookies_export_untouched( self ):
        # recorder.py run as the daemon runs it, with the user's export in its
        # working directory: yt-dlp rewrites the copy it is handed on exit, and
        # the export itself is never written.
        export = os.path.join( self.cwd, recorder.COOKIES_FILE )
        contents = "# Netscape HTTP Cookie File\n.chaturbate.com\tTRUE\t/\tTRUE\t0\tsessionid\tfrom-browser\n"
        with open( export, "w" ) as f:
            f.write( contents )
        log = os.path.join( self.cwd, "cookies-log" )
        hls = FakeHls()
        try:
            result = self.run_recorder( { "FAKE_HLS_BASE": hls.base, "FAKE_YTDLP_COOKIES_LOG": log } )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        with open( log ) as f:
            path, received = f.read().split( "\n", 1 )
        self.assertNotEqual( path, "" )
        self.assertNotEqual( os.path.realpath(path), os.path.realpath(export) )
        self.assertEqual( received, contents )
        self.assertFalse( os.path.exists(path) )
        with open( export ) as f:
            self.assertEqual( f.read(), contents )

    def test_sigint_stops_and_keeps_recording( self ):
        hls = FakeHls()
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), "https://chaturbate.com/room/" ],
                                     cwd=self.cwd, env=dict(os.environ, FAKE_HLS_BASE=hls.base),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            self.assertTrue( hls.served.wait( 15 ) )
            proc.send_signal( 2 )
            out, _ = proc.communicate( timeout=15 )
        finally:
            hls.close()
        self.assertIn( "Stopped recording room", out )
        path = os.path.join( self.cwd, "videos", "room", "room 2026-01-01 00_00.ts" )
        codecs, container, duration = probe( path )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( subprocess.run(["pgrep", "-P", str(proc.pid)], stdout=subprocess.PIPE).stdout, b"" )

    def test_outage_with_failed_reresolution_keeps_one_recording( self ):
        # Playlists answer 503 from 2s to 19s, so re-resolution runs and its
        # yt-dlp lookup fails with a network error; the recorder must keep
        # going and finish the stream in the same file.
        hls = FakeHls( outage=(2, 19) )
        try:
            result = self.run_recorder( { "FAKE_HLS_BASE": hls.base, "FAKE_YTDLP_MODE": "fail-after-first",
                                          "FAKE_YTDLP_COUNTER": os.path.join(self.cwd, "calls") }, timeout=60 )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Re-resolution failed", result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        self.assertEqual( os.listdir(os.path.join(self.cwd, "videos", "room")), [ "room 2026-01-01 00_00.ts" ] )

    def test_sigint_during_stalled_fetch_exits_within_15s( self ):
        hls = FakeHls( hang=True )
        ready_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), "https://chaturbate.com/room/", str(write_fd) ],
                                     cwd=self.cwd, env=dict(os.environ, FAKE_HLS_BASE=hls.base), pass_fds=(write_fd,),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            os.close( write_fd )
            # the room has resolved and the recorder is fetching from the hung server
            self.assertEqual( os.read( ready_fd, 16 ), b"ready\n" )
            os.close( ready_fd )
            sent = time.time()
            proc.send_signal( 2 )
            out, _ = proc.communicate( timeout=30 )
            elapsed = time.time() - sent
        finally:
            hls.close()
        self.assertLess( elapsed, 15, out )
        self.assertIn( "Stopped recording room", out )

class ExtractTest( unittest.TestCase ):
    # recorder.extract against fake_ytdlp.py in each of its modes.

    CONFIG = { "youtube-dl_cmd": "{} {}".format(sys.executable, os.path.join(ROOT, "tests", "fake_ytdlp.py")),
               "youtube-dl_config": "unused" }

    def extract( self, mode, stop = None ):
        with mock.patch.dict( os.environ, { "FAKE_YTDLP_MODE": mode, "FAKE_HLS_BASE": "http://h" } ), \
             mock.patch.object( recorder.sys, "stderr", new_callable=lambda: open(os.devnull, "w") ):
            return recorder.extract( "https://chaturbate.com/room/", self.CONFIG, stop or recorder.Stop() )

    def test_ok( self ):
        status, (title, urls, headers) = self.extract( "ok" )
        self.assertEqual( status, "ok" )
        self.assertEqual( title, "room 2026-01-01 00_00" )
        self.assertEqual( urls, [ "http://h/v/playlist.m3u8", "http://h/a/playlist.m3u8" ] )
        self.assertEqual( headers, { "User-Agent": "recordurbate-test" } )

    def test_room_state_is_unavailable( self ):
        self.assertEqual( self.extract("offline"), ("unavailable", "ERROR: [Chaturbate] room: Room is currently offline") )

    def test_network_error_is_an_error( self ):
        status, reason = self.extract( "neterror" )
        self.assertEqual( status, "error" )
        self.assertIn( "Network is unreachable", reason )

    def test_unreadable_output_is_an_error( self ):
        status, reason = self.extract( "garbage" )
        self.assertEqual( status, "error" )
        self.assertIn( "unreadable yt-dlp output", reason )

    def test_stop_interrupts_a_hanging_lookup( self ):
        # stop is set as the lookup starts waiting, so it provably lands
        # inside the wait; the lookup itself would hang until killed.
        stop = recorder.Stop()
        real_communicate = subprocess.Popen.communicate
        def stop_then_communicate( proc, *args, **kwargs ):
            stop.set()
            return real_communicate( proc, *args, **kwargs )
        with mock.patch.object( recorder.subprocess.Popen, "communicate", stop_then_communicate ):
            self.assertEqual( self.extract("hang", stop), ("error", "yt-dlp lookup interrupted") )

    def test_stop_set_before_the_lookup_starts_kills_it( self ):
        stop = recorder.Stop()
        stop.set()
        self.assertEqual( self.extract("hang", stop), ("error", "yt-dlp lookup interrupted") )

class ExtractCookiesTest( unittest.TestCase ):
    # yt-dlp rewrites its --cookies file on exit, as fake_ytdlp.py does;
    # extract must hand it a private copy so the user's export is never written.

    EXPORT = "# Netscape HTTP Cookie File\n.chaturbate.com\tTRUE\t/\tTRUE\t0\tsessionid\tfrom-browser\n"

    def setUp( self ):
        self.dir = tempfile.mkdtemp()
        self.addCleanup( shutil.rmtree, self.dir )
        self.export = os.path.join( self.dir, "chaturbate.com_cookies.txt" )
        self.log = os.path.join( self.dir, "cookies-log" )
        self.copies = []

    def extract( self, mode, stop = None, timeout = recorder.EXTRACT_TIMEOUT ):
        real_mkstemp = tempfile.mkstemp
        def recording_mkstemp( *args, **kwargs ):
            fd, path = real_mkstemp( *args, **kwargs )
            self.copies.append( path )
            return fd, path
        with mock.patch.object( recorder, "COOKIES_FILE", self.export ), \
             mock.patch.object( recorder, "EXTRACT_TIMEOUT", timeout ), \
             mock.patch.object( recorder.tempfile, "mkstemp", recording_mkstemp ), \
             mock.patch.dict( os.environ, { "FAKE_YTDLP_COOKIES_LOG": self.log } ):
            return ExtractTest.extract( self, mode, stop )

    def write_export( self ):
        with open( self.export, "w" ) as f:
            f.write( self.EXPORT )

    def received( self ):
        with open( self.log ) as f:
            path, contents = f.read().split( "\n", 1 )
        return path, contents

    def assert_export_untouched_and_copy_removed( self ):
        with open( self.export ) as f:
            self.assertEqual( f.read(), self.EXPORT )
        self.assertEqual( len(self.copies), 1 )
        self.assertFalse( os.path.exists(self.copies[0]) )

    CONFIG = ExtractTest.CONFIG

    def test_yt_dlp_gets_a_copy_of_the_export( self ):
        self.write_export()
        status, _ = self.extract( "ok" )
        self.assertEqual( status, "ok" )
        path, contents = self.received()
        self.assertEqual( path, self.copies[0] )
        self.assertNotEqual( os.path.realpath(path), os.path.realpath(self.export) )
        self.assertEqual( contents, self.EXPORT )
        self.assert_export_untouched_and_copy_removed()

    def test_failed_lookup_leaves_the_export_untouched( self ):
        self.write_export()
        self.assertEqual( self.extract("offline")[0], "unavailable" )
        self.assert_export_untouched_and_copy_removed()

    def test_timed_out_lookup_removes_the_copy( self ):
        self.write_export()
        self.assertEqual( self.extract("hang", timeout=1), ("error", "yt-dlp took over 1s") )
        self.assert_export_untouched_and_copy_removed()

    def test_stopped_lookup_removes_the_copy( self ):
        self.write_export()
        stop = recorder.Stop()
        stop.set()
        self.assertEqual( self.extract("hang", stop), ("error", "yt-dlp lookup interrupted") )
        self.assert_export_untouched_and_copy_removed()

    def test_no_export_passes_no_cookies( self ):
        status, _ = self.extract( "ok" )
        self.assertEqual( status, "ok" )
        self.assertEqual( self.received(), ("", "") )
        self.assertEqual( self.copies, [] )
        self.assertFalse( os.path.exists(self.export) )

class RunTest( unittest.TestCase ):
    # recorder.run in-process with main replaced, so only run's own routes run.

    def run_with( self, main ):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as cwd, \
             mock.patch.object( sys, "argv", [ "recorder.py", "https://chaturbate.com/alice/" ] ), \
             mock.patch.object( recorder.Config, "load_config", lambda: {} ), \
             mock.patch.object( recorder, "main", main ), \
             mock.patch.object( recorder.signal, "signal" ):
            os.chdir( cwd )
            try:
                with self.assertRaises( SystemExit ) as exited:
                    recorder.run()
                return exited.exception.code, os.path.exists( "configs/pid-alice" )
            finally:
                os.chdir( previous )

    def test_exits_with_the_recordings_code_and_removes_its_record( self ):
        seen = {}
        def main( url, config, ready_fd, stop, outcome ):
            with open( "configs/pid-alice" ) as f:
                seen["record"] = f.read()
            seen["stop"] = stop
            outcome["exit_code"] = 0
        code, record_left = self.run_with( main )
        self.assertEqual( code, 0 )
        self.assertEqual( seen["record"], "{}\n".format( os.getpid() ) )
        self.assertIsInstance( seen["stop"], recorder.Stop )
        self.assertFalse( record_left )

    def test_failed_recording_exits_1_and_removes_its_record( self ):
        def main( url, config, ready_fd, stop, outcome ):
            raise RuntimeError( "boom" )
        with mock.patch.object( recorder.threading, "excepthook", lambda args: None ):
            code, record_left = self.run_with( main )
        self.assertEqual( code, 1 )
        self.assertFalse( record_left )

class SignalReadyTest( unittest.TestCase ):

    def test_writes_ready_and_closes_its_end( self ):
        read_fd, write_fd = os.pipe()
        recorder.signal_ready( write_fd )
        self.assertEqual( os.read( read_fd, 16 ), b"ready\n" )
        self.assertEqual( os.read( read_fd, 16 ), b"" )
        os.close( read_fd )

    def test_pipe_closed_by_a_handed_off_daemon_is_not_an_error( self ):
        read_fd, write_fd = os.pipe()
        os.close( read_fd )
        recorder.signal_ready( write_fd )
        with self.assertRaises( OSError ):
            os.fstat( write_fd )

if __name__ == "__main__":
    unittest.main()
