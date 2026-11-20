import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import requests

ROOT = os.path.dirname( os.path.dirname( os.path.abspath(__file__) ) )
sys.path.insert( 0, ROOT )
import recorder
import streamer as recorder_streamer

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
    #
    # It also stands in for the site: POST /get_edge_hls_url_ajax/ answers
    # according to `api` and GET /master.m3u8 lists two video variants, the
    # higher-bandwidth one being v/playlist.m3u8, whose audio group's default
    # rendition is a/playlist.m3u8. `api` is one of ok, offline, private,
    # unknown (a room_status with no message), neterror (503), garbage (not
    # JSON), hang (never answers), or fail-after-first (ok once, then 503).
    # While `api_gate` is cleared, room API requests wait for it to be set.
    # `api_requests` records each API request's room_slug and Cookie header.

    FIRST = 100
    MASTER = "\n".join( [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="other",DEFAULT=NO,URI="x/playlist.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="main",DEFAULT=YES,URI="a/playlist.m3u8"',
        '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=9000000,BANDWIDTH=896000,RESOLUTION=640x360,AUDIO="aac"',
        "lo/playlist.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=5128000,RESOLUTION=1920x1080,AUDIO="aac"',
        "v/playlist.m3u8",
        "" ] )

    def __init__( self, failing = (), outage = None, hang = False, api = "ok" ):
        # outage=(start, end) answers 503 to everything in that window of
        # seconds; hang=True never answers a media segment.
        self.failing = set( failing )
        self.outage = outage
        self.hang = hang
        self.api = api
        self.api_gate = threading.Event()
        self.api_gate.set()
        self.api_requests = []
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
            def do_POST( self ):
                fake.handle_api( self )
        self.server = http.server.ThreadingHTTPServer( ("127.0.0.1", 0), Handler )
        self.base = "http://127.0.0.1:{}".format( self.server.server_address[1] )
        threading.Thread( target=self.server.serve_forever, daemon=True ).start()

    def track( self, letter ):
        return Fixtures.video if letter == "v" else Fixtures.audio

    def respond( self, request, status, body = b"", content_type = None ):
        request.send_response( status )
        if content_type:
            request.send_header( "Content-Type", content_type )
        request.send_header( "Content-Length", str(len(body)) )
        request.end_headers()
        request.wfile.write( body )

    def handle_api( self, request ):
        length = int( request.headers.get("Content-Length") or 0 )
        form = urllib.parse.parse_qs( request.rfile.read( length ).decode() )
        self.api_requests.append( ( form.get("room_slug", [""])[0], request.headers.get("Cookie") ) )
        self.api_gate.wait()
        mode = self.api
        if mode == "fail-after-first":
            mode = "ok" if len( self.api_requests ) == 1 else "neterror"
        if mode == "hang":
            time.sleep( 30 )
            return
        if mode == "neterror":
            self.respond( request, 503 )
            return
        if mode == "garbage":
            self.respond( request, 200, b"not json", "text/html" )
            return
        status = { "ok": "public", "offline": "offline", "private": "private", "unknown": "unknown" }[ mode ]
        info = { "success": True, "url": self.base + "/master.m3u8?token=t" if mode == "ok" else "",
                 "room_status": status }
        self.respond( request, 200, json.dumps( info ).encode(), "application/json" )

    def handle( self, request ):
        path = request.path.split("?")[0]
        elapsed = time.time() - self.started
        if self.outage and self.outage[0] <= elapsed < self.outage[1]:
            request.send_response( 503 )
            request.end_headers()
            return
        if path == "/master.m3u8":
            self.respond( request, 200, self.MASTER.encode() )
            return
        letter, name = path.strip("/").split("/")
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

    def room_url( self ):
        return self.base + "/room/"

    def close( self ):
        self.api_gate.set()
        self.server.shutdown()

TITLE = re.compile( r"room \d{4}-\d\d-\d\d \d\d_\d\d\.ts" )

class RecorderRunTest( unittest.TestCase ):
    # Runs recorder.py as the daemon does, against FakeHls as both the site's
    # room API and the CDN.

    def setUp( self ):
        self.cwd = tempfile.mkdtemp()
        os.makedirs( os.path.join(self.cwd, "configs") )

    def tearDown( self ):
        shutil.rmtree( self.cwd )

    def run_recorder( self, hls, timeout = 40 ):
        return subprocess.run( [ sys.executable, os.path.join(ROOT, "recorder.py"), hls.room_url() ],
                               cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout, text=True )

    def recording( self ):
        # the one file recorded for the room, named "room <date> <time>.ts"
        names = os.listdir( os.path.join(self.cwd, "videos", "room") )
        self.assertEqual( len(names), 1, names )
        self.assertRegex( names[0], TITLE )
        return os.path.join( self.cwd, "videos", "room", names[0] )

    def test_records_both_tracks_across_a_failed_segment( self ):
        hls = FakeHls( failing=[ ("v", 102) ] )
        try:
            result = self.run_recorder( hls )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Track 0 segment 102 failed: HTTP 500", result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        codecs, container, duration = probe( self.recording() )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( container, "mpegts" )
        self.assertGreater( duration, 4 )

    def test_offline_room_exits_with_its_room_state( self ):
        hls = FakeHls( api="offline" )
        try:
            result = self.run_recorder( hls )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 1 )
        self.assertIn( "ERROR: [Chaturbate] room: Room is currently offline", result.stdout )
        self.assertFalse( os.path.exists(os.path.join(self.cwd, "videos")) )

    def test_recording_sends_the_cookies_export_and_leaves_it_untouched( self ):
        # recorder.py run as the daemon runs it, with the user's export in its
        # working directory: the room API receives its cookies, including one
        # marked #HttpOnly_, and the export is never written.
        export = os.path.join( self.cwd, recorder.COOKIES_FILE )
        contents = ( "# Netscape HTTP Cookie File\n"
                     "#HttpOnly_127.0.0.1\tFALSE\t/\tFALSE\t0\tsessionid\tfrom-browser\n"
                     "127.0.0.1\tFALSE\t/\tFALSE\t0\tcsrftoken\tcsrf\n" )
        with open( export, "w" ) as f:
            f.write( contents )
        before = os.stat( export ).st_mtime_ns
        hls = FakeHls()
        try:
            result = self.run_recorder( hls )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        room, cookie = hls.api_requests[0]
        self.assertEqual( room, "room" )
        self.assertEqual( sorted( cookie.split("; ") ), [ "csrftoken=csrf", "sessionid=from-browser" ] )
        with open( export ) as f:
            self.assertEqual( f.read(), contents )
        self.assertEqual( os.stat( export ).st_mtime_ns, before )

    def test_sigint_stops_and_keeps_recording( self ):
        hls = FakeHls()
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), hls.room_url() ],
                                     cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            self.assertTrue( hls.served.wait( 15 ) )
            proc.send_signal( 2 )
            out, _ = proc.communicate( timeout=15 )
        finally:
            hls.close()
        self.assertIn( "Stopped recording room", out )
        codecs, container, duration = probe( self.recording() )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( subprocess.run(["pgrep", "-P", str(proc.pid)], stdout=subprocess.PIPE).stdout, b"" )

    def test_outage_with_failed_reresolution_keeps_one_recording( self ):
        # Playlists answer 503 from 2s to 19s, so re-resolution runs and its
        # room API request fails; the recorder must keep going and finish the
        # stream in the same file.
        hls = FakeHls( outage=(2, 19), api="fail-after-first" )
        try:
            result = self.run_recorder( hls, timeout=60 )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Re-resolution failed", result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        self.recording()

    def test_sigint_during_stalled_fetch_exits_within_15s( self ):
        hls = FakeHls( hang=True )
        ready_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), hls.room_url(), str(write_fd) ],
                                     cwd=self.cwd, pass_fds=(write_fd,),
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

    def test_sigint_during_a_hung_lookup_exits_within_15s( self ):
        hls = FakeHls( api="hang" )
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), hls.room_url() ],
                                     cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            deadline = time.time() + 15
            while not hls.api_requests and time.time() < deadline:
                time.sleep( 0.05 )
            self.assertTrue( hls.api_requests )
            sent = time.time()
            proc.send_signal( 2 )
            out, _ = proc.communicate( timeout=30 )
            elapsed = time.time() - sent
        finally:
            hls.close()
        self.assertLess( elapsed, 15, out )
        self.assertIn( "Cannot record room: lookup interrupted", out )

class ExtractTest( unittest.TestCase ):
    # recorder.extract against FakeHls's room API in each of its modes.

    def setUp( self ):
        self.cwd = tempfile.mkdtemp()
        self.addCleanup( shutil.rmtree, self.cwd )
        self.export = os.path.join( self.cwd, "chaturbate.com_cookies.txt" )

    def extract( self, hls, stop = None ):
        with mock.patch.object( recorder, "COOKIES_FILE", self.export ), \
             mock.patch.object( recorder.sys, "stderr", new_callable=lambda: open(os.devnull, "w") ):
            return recorder.extract( hls.room_url(), stop or recorder.Stop() )

    def extract_mode( self, mode ):
        hls = FakeHls( api=mode )
        self.addCleanup( hls.close )
        return self.extract( hls )

    def test_ok_picks_the_highest_variant_and_its_default_audio( self ):
        hls = FakeHls()
        self.addCleanup( hls.close )
        status, (title, urls, headers) = self.extract( hls )
        self.assertEqual( status, "ok" )
        self.assertRegex( title + ".ts", TITLE )
        self.assertEqual( urls, [ hls.base + "/v/playlist.m3u8", hls.base + "/a/playlist.m3u8" ] )
        self.assertEqual( headers, { "User-Agent": recorder.USER_AGENT, "Referer": hls.base + "/room/" } )
        self.assertEqual( hls.api_requests, [ ("room", None) ] )

    def test_room_states_are_unavailable( self ):
        self.assertEqual( self.extract_mode("offline"), ("unavailable", "ERROR: [Chaturbate] room: Room is currently offline") )
        self.assertEqual( self.extract_mode("private"), ("unavailable", "ERROR: [Chaturbate] room: Room is currently in a private show") )

    def test_room_state_messages_are_the_ones_streamer_reads( self ):
        self.assertEqual( set( recorder.ROOM_STATUS_MESSAGES.values() ), set( recorder_streamer.ROOM_STATES ) )

    def test_unknown_room_status_is_an_error( self ):
        self.assertEqual( self.extract_mode("unknown"),
                          ("error", 'ERROR: [Chaturbate] room: room API gave no stream for room status "unknown"') )

    def test_network_error_is_an_error( self ):
        status, reason = self.extract_mode( "neterror" )
        self.assertEqual( status, "error" )
        self.assertTrue( reason.startswith("ERROR: [Chaturbate] room: room API request failed: 503"), reason )

    def test_unreadable_response_is_an_error( self ):
        self.assertEqual( self.extract_mode("garbage"), ("error", "ERROR: [Chaturbate] room: unreadable room API response") )

    def test_error_line_reaches_stderr( self ):
        hls = FakeHls( api="offline" )
        self.addCleanup( hls.close )
        with mock.patch.object( recorder.sys, "stderr", new_callable=lambda: __import__("io").StringIO() ) as err:
            recorder.extract( hls.room_url(), recorder.Stop() )
        self.assertEqual( err.getvalue(), "ERROR: [Chaturbate] room: Room is currently offline\n" )

    def test_stop_interrupts_a_hanging_lookup_within_1s( self ):
        hls = FakeHls( api="hang" )
        self.addCleanup( hls.close )
        stop = recorder.Stop()
        set_at = []
        def stop_once_requested():
            while not hls.api_requests:
                time.sleep( 0.01 )
            set_at.append( time.time() )
            stop.set()
        threading.Thread( target=stop_once_requested, daemon=True ).start()
        self.assertEqual( self.extract(hls, stop), ("error", "lookup interrupted") )
        self.assertLess( time.time() - set_at[0], 1 )

    def test_stop_set_before_the_lookup_returns_at_once( self ):
        hls = FakeHls( api="hang" )
        self.addCleanup( hls.close )
        stop = recorder.Stop()
        stop.set()
        started = time.time()
        self.assertEqual( self.extract(hls, stop), ("error", "lookup interrupted") )
        self.assertLess( time.time() - started, 1 )

    def test_cookies_come_from_the_export_which_is_left_untouched( self ):
        contents = ( "# Netscape HTTP Cookie File\n"
                     "#HttpOnly_127.0.0.1\tFALSE\t/\tFALSE\t0\tsessionid\tfrom-browser\n"
                     "# a comment\n"
                     "127.0.0.1\tFALSE\t/\tFALSE\t0\tcsrftoken\tcsrf\n"
                     "malformed line\n" )
        with open( self.export, "w" ) as f:
            f.write( contents )
        hls = FakeHls()
        self.addCleanup( hls.close )
        self.assertEqual( self.extract(hls)[0], "ok" )
        self.assertEqual( sorted( hls.api_requests[0][1].split("; ") ), [ "csrftoken=csrf", "sessionid=from-browser" ] )
        with open( self.export ) as f:
            self.assertEqual( f.read(), contents )

    def test_no_export_sends_no_cookies( self ):
        hls = FakeHls()
        self.addCleanup( hls.close )
        self.assertEqual( self.extract(hls)[0], "ok" )
        self.assertEqual( hls.api_requests, [ ("room", None) ] )
        self.assertFalse( os.path.exists(self.export) )

class ParseMasterTest( unittest.TestCase ):

    def test_variant_without_audio_group_gives_one_url( self ):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlo.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nhi.m3u8\n"
        self.assertEqual( recorder.parse_master(text, "http://h/s/master.m3u8?t=1"), [ "http://h/s/hi.m3u8" ] )

    def test_group_without_default_uses_its_first_rendition( self ):
        text = ( '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="g",DEFAULT=NO,URI="a1.m3u8"\n'
                 '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="g",DEFAULT=NO,URI="a2.m3u8"\n'
                 '#EXT-X-STREAM-INF:BANDWIDTH=5,AUDIO="g"\n/abs/v.m3u8\n' )
        self.assertEqual( recorder.parse_master(text, "http://h/s/master.m3u8"), [ "http://h/abs/v.m3u8", "http://h/s/a1.m3u8" ] )

    def test_missing_audio_group_or_variant_is_none( self ):
        self.assertIsNone( recorder.parse_master('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5,AUDIO="g"\nv.m3u8\n', "http://h/") )
        self.assertIsNone( recorder.parse_master("#EXTM3U\n", "http://h/") )

class RunTest( unittest.TestCase ):
    # recorder.run in-process with main replaced, so only run's own routes run.

    def run_with( self, main ):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as cwd, \
             mock.patch.object( sys, "argv", [ "recorder.py", "https://chaturbate.com/alice/" ] ), \
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
        def main( url, ready_fd, stop, outcome ):
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
        def main( url, ready_fd, stop, outcome ):
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
