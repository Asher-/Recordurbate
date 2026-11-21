import http.server
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
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

EDGE_API = os.path.join( ROOT, "tests", "fixtures", "edge_api" )
CAPTURED_HOST = "https://edge27-atl.live.mmcdn.com"

def captured( name ):
    # A response captured from the site; see tests/fixtures/edge_api/README.md
    with open( os.path.join(EDGE_API, name), encoding="utf-8" ) as f:
        return f.read()

class FakeHls:
    # Serves a live LL-HLS room from the fixtures: one segment per track
    # becomes available each second, the playlist window holds three, and
    # ENDLIST follows the last. Segments in `failing` answer 500.

    FIRST = 100

    def __init__( self, failing = (), outage = None, hang = False, slow = 0 ):
        # outage=(start, end) answers 503 to everything in that window of
        # seconds; hang=True never answers a media segment; slow delays each
        # media segment's answer by that many seconds.
        self.failing = set( failing )
        self.outage = outage
        self.hang = hang
        self.slow = slow
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
        if self.slow and name.startswith("seg_"):
            time.sleep( self.slow )
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


class FakeSite:
    # Stands in for chaturbate.com with responses captured from it, in front
    # of a FakeHls that serves the media. POST /get_edge_hls_url_ajax/
    # answers according to `api`; the public answer names the captured master
    # playlist, served verbatim, and its video and audio chunklists are
    # FakeHls's /v/ and /a/ playlists with init and segment URIs made
    # absolute on FakeHls, so FakeHls's failures, outages and hangs reach the
    # recorder unchanged. `api` is one of ok, offline, hidden (captured
    # answers), unknown or private (the captured offline answer with that
    # room_status), list (a JSON array), neterror (503), garbage (not JSON),
    # hang (never answers), or fail-after-first (ok once, then 503). `master`
    # is ok, neterror (503) or empty (the captured master cut before its
    # first variant). While `api_gate` is cleared, room API requests wait for
    # it. `api_requests` records each request's room_slug and Cookie header,
    # and `api_arrived` is set once one has arrived.

    def __init__( self, hls, api = "ok", master = "ok" ):
        self.hls = hls
        self.api = api
        self.master = master
        self.api_gate = threading.Event()
        self.api_gate.set()
        self.api_arrived = threading.Event()
        self.api_requests = []
        site = self
        class Handler( http.server.BaseHTTPRequestHandler ):
            def log_message( self, *args ):
                pass
            def do_GET( self ):
                site.handle( self )
            def do_POST( self ):
                site.handle_api( self )
        self.server = http.server.ThreadingHTTPServer( ("127.0.0.1", 0), Handler )
        self.base = "http://127.0.0.1:{}".format( self.server.server_address[1] )
        threading.Thread( target=self.server.serve_forever, daemon=True ).start()

    def room_url( self ):
        return self.base + "/room/"

    def stream_url( self, kind ):
        # the captured master playlist's chunklist for kind "video" or "audio"
        number = 3 if kind == "video" else 5
        return ( self.base + "/v1/edge/streams/origin.room.01ROOMSTREAM00000000000000/"
                 "chunklist_{}_{}_10000000000000000000_llhls.m3u8?session=00000000-0000-0000-0000-000000000000".format(number, kind) )

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
        self.api_arrived.set()
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
            self.respond( request, 200, b"<html>not json</html>", "text/html" )
            return
        if mode == "list":
            self.respond( request, 200, b"[]", "application/json" )
            return
        if mode == "ok":
            body = captured( "public.json" ).replace( CAPTURED_HOST, self.base )
        elif mode in ( "unknown", "private" ):
            info = json.loads( captured("offline.json") )
            info["room_status"] = mode
            body = json.dumps( info )
        else:
            body = captured( mode + ".json" )
        self.respond( request, 200, body.encode(), "application/json" )

    def handle( self, request ):
        name = request.path.split("?")[0].rsplit("/", 1)[-1]
        if name == "llhls.m3u8":
            if self.master == "neterror":
                self.respond( request, 503 )
                return
            text = captured( "master.m3u8" )
            if self.master == "empty":
                text = text.split( "#EXT-X-STREAM-INF" )[0]
            self.respond( request, 200, text.encode() )
            return
        letter = "v" if "_video_" in name else "a"
        try:
            with urllib.request.urlopen( "{}/{}/playlist.m3u8".format(self.hls.base, letter), timeout=5 ) as answer:
                text = answer.read().decode()
        except urllib.error.HTTPError as e:
            self.respond( request, e.code )
            return
        prefix = "{}/{}/".format( self.hls.base, letter )
        text = text.replace( 'URI="init.mp4"', 'URI="{}init.mp4"'.format(prefix) )
        text = "\n".join( prefix + line if line.startswith("seg_") else line for line in text.split("\n") )
        self.respond( request, 200, text.encode() )

    def close( self ):
        self.api_gate.set()
        self.server.shutdown()
        self.hls.close()

def fake_site( api = "ok", master = "ok", **media ):
    # A FakeSite in front of a new FakeHls built from media; closing the site
    # closes both.
    return FakeSite( FakeHls(**media), api, master )

TITLE = re.compile( r"room \d{4}-\d\d-\d\d \d\d_\d\d\.ts" )

class RecorderRunTest( unittest.TestCase ):
    # Runs recorder.py as the daemon does, against FakeSite as the site's room API
    # in front of FakeHls as the CDN.

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
        hls = fake_site( failing=[ ("v", 102) ] )
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

    def test_slow_segments_are_all_recorded( self ):
        # Each media segment takes 0.8s to answer while each track gains one
        # a second and the window holds three: fetched one after another the
        # two tracks need 1.6s a second and fall out of the window.
        hls = fake_site( slow=0.8 )
        try:
            result = self.run_recorder( hls, timeout=60 )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        self.assertNotIn( "lost segments", result.stdout )
        self.recording()

    def test_offline_room_exits_with_its_room_state( self ):
        hls = fake_site( api="offline" )
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
        hls = fake_site()
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
        hls = fake_site()
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), hls.room_url() ],
                                     cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            self.assertTrue( hls.hls.served.wait( 15 ) )
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
        hls = fake_site( outage=(2, 19), api="fail-after-first" )
        try:
            result = self.run_recorder( hls, timeout=60 )
        finally:
            hls.close()
        self.assertEqual( result.returncode, 0, result.stdout )
        self.assertIn( "Re-resolution failed", result.stdout )
        self.assertIn( "Stream ended", result.stdout )
        self.recording()

    def test_sigint_during_stalled_fetch_exits_within_15s( self ):
        hls = fake_site( hang=True )
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
        hls = fake_site( api="hang" )
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), hls.room_url() ],
                                     cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            self.assertTrue( hls.api_arrived.wait( 15 ) )
            sent = time.time()
            proc.send_signal( 2 )
            out, _ = proc.communicate( timeout=30 )
            elapsed = time.time() - sent
        finally:
            hls.close()
        self.assertLess( elapsed, 15, out )
        self.assertIn( "Cannot record room: lookup interrupted", out )

class StopTest( unittest.TestCase ):

    def test_set_wakes_a_registered_waiter( self ):
        stop = recorder.Stop()
        stop.waiter = queue.Queue()
        stop.set()
        self.assertTrue( stop.is_set() )
        self.assertIsNone( stop.waiter.get_nowait() )

    def test_set_without_a_waiter_only_sets( self ):
        stop = recorder.Stop()
        stop.set()
        self.assertTrue( stop.is_set() )

class LoadCookiesTest( unittest.TestCase ):

    def setUp( self ):
        self.cwd = tempfile.mkdtemp()
        self.addCleanup( shutil.rmtree, self.cwd )
        self.export = os.path.join( self.cwd, "chaturbate.com_cookies.txt" )
        patcher = mock.patch.object( recorder, "COOKIES_FILE", self.export )
        patcher.start()
        self.addCleanup( patcher.stop )

    def test_no_export_gives_an_empty_jar( self ):
        self.assertEqual( len(recorder.load_cookies()), 0 )
        self.assertFalse( os.path.exists(self.export) )

    def test_reads_cookie_and_httponly_lines_and_skips_comments_and_malformed_lines( self ):
        contents = ( "# Netscape HTTP Cookie File\n"
                     "#HttpOnly_.chaturbate.com\tTRUE\t/\tTRUE\t0\tsessionid\tfrom-browser\n"
                     "# a comment\n"
                     ".chaturbate.com\tTRUE\t/\tFALSE\t1795000000\tcsrftoken\tcsrf\r\n"
                     "malformed line\n" )
        with open( self.export, "w", newline="" ) as f:
            f.write( contents )
        jar = recorder.load_cookies()
        self.assertEqual( sorted( (c.domain, c.path, c.name, c.value) for c in jar ),
                          [ (".chaturbate.com", "/", "csrftoken", "csrf"), (".chaturbate.com", "/", "sessionid", "from-browser") ] )
        with open( self.export, newline="" ) as f:
            self.assertEqual( f.read(), contents )

class ParseMasterTest( unittest.TestCase ):

    BASE = "https://edge27-atl.live.mmcdn.com/v1/edge/streams/origin.room.01ROOMSTREAM00000000000000/llhls.m3u8?token=TOKEN"

    def test_captured_master_gives_the_highest_variant_and_its_groups_default_audio( self ):
        urls = recorder.parse_master( captured("master.m3u8"), self.BASE )
        self.assertEqual( [ u.split("?")[0].rsplit("/", 1)[1] for u in urls ],
                          [ "chunklist_3_video_10000000000000000000_llhls.m3u8", "chunklist_5_audio_10000000000000000000_llhls.m3u8" ] )
        self.assertTrue( urls[0].startswith("https://edge27-atl.live.mmcdn.com/v1/edge/streams/") )

    def test_variant_without_audio_group_gives_one_url( self ):
        text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlo.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\nhi.m3u8\n"
        self.assertEqual( recorder.parse_master(text, "http://h/s/master.m3u8?t=1"), [ "http://h/s/hi.m3u8" ] )

    def test_group_without_default_uses_its_first_rendition( self ):
        text = ( '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="g",DEFAULT=NO,URI="a1.m3u8"\n'
                 '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="g",DEFAULT=NO,URI="a2.m3u8"\n'
                 '#EXT-X-STREAM-INF:BANDWIDTH=5,AUDIO="g"\n/abs/v.m3u8\n' )
        self.assertEqual( recorder.parse_master(text, "http://h/s/master.m3u8"), [ "http://h/abs/v.m3u8", "http://h/s/a1.m3u8" ] )

    def test_rendition_without_uri_is_carried_in_the_variant( self ):
        text = ( '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="g",DEFAULT=YES,NAME="in-band"\n'
                 '#EXT-X-STREAM-INF:BANDWIDTH=5,AUDIO="g"\nv.m3u8\n' )
        self.assertEqual( recorder.parse_master(text, "http://h/s/master.m3u8"), [ "http://h/s/v.m3u8" ] )

    def test_undeclared_audio_group_or_no_variant_is_none( self ):
        self.assertIsNone( recorder.parse_master('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5,AUDIO="g"\nv.m3u8\n', "http://h/") )
        self.assertIsNone( recorder.parse_master(captured("master.m3u8").split("#EXT-X-STREAM-INF")[0], "http://h/") )

class LookupTest( unittest.TestCase ):
    # recorder.lookup with a real requests.Session against FakeSite serving the
    # site's captured answers.

    def setUp( self ):
        self.cwd = tempfile.mkdtemp()
        self.addCleanup( shutil.rmtree, self.cwd )
        patcher = mock.patch.object( recorder, "COOKIES_FILE", os.path.join(self.cwd, "chaturbate.com_cookies.txt") )
        patcher.start()
        self.addCleanup( patcher.stop )

    def lookup( self, **modes ):
        hls = fake_site( **modes )
        self.addCleanup( hls.close )
        with requests.Session() as session:
            return hls, recorder.lookup( hls.room_url(), session )

    def test_public_room_resolves_to_its_video_and_audio( self ):
        hls, (status, (title, urls, headers)) = self.lookup()
        self.assertEqual( status, "ok" )
        self.assertRegex( title + ".ts", TITLE )
        self.assertEqual( urls, [ hls.stream_url("video"), hls.stream_url("audio") ] )
        self.assertEqual( headers, { "User-Agent": recorder.USER_AGENT, "Referer": hls.base + "/room/" } )
        self.assertEqual( hls.api_requests, [ ("room", None) ] )

    def test_room_states_are_unavailable( self ):
        self.assertEqual( self.lookup(api="offline")[1], ("unavailable", "ERROR: [Chaturbate] room: Room is currently offline") )
        self.assertEqual( self.lookup(api="hidden")[1], ("unavailable", "ERROR: [Chaturbate] room: Hidden session in progress") )
        self.assertEqual( self.lookup(api="private")[1], ("unavailable", "ERROR: [Chaturbate] room: Room is currently in a private show") )

    def test_unknown_room_status_is_an_error( self ):
        self.assertEqual( self.lookup(api="unknown")[1],
                          ("error", 'ERROR: [Chaturbate] room: room API gave no stream for room status "unknown"') )

    def test_failed_api_request_is_an_error( self ):
        status, reason = self.lookup( api="neterror" )[1]
        self.assertEqual( status, "error" )
        self.assertTrue( reason.startswith("ERROR: [Chaturbate] room: room API request failed: 503"), reason )

    def test_unreadable_or_non_object_answer_is_an_error( self ):
        self.assertEqual( self.lookup(api="garbage")[1], ("error", "ERROR: [Chaturbate] room: unreadable room API response") )
        self.assertEqual( self.lookup(api="list")[1], ("error", "ERROR: [Chaturbate] room: unreadable room API response") )

    def test_failed_master_request_is_an_error( self ):
        status, reason = self.lookup( master="neterror" )[1]
        self.assertEqual( status, "error" )
        self.assertTrue( reason.startswith("ERROR: [Chaturbate] room: master playlist request failed: 503"), reason )

    def test_master_without_variant_is_an_error( self ):
        self.assertEqual( self.lookup(master="empty")[1], ("error", "ERROR: [Chaturbate] room: master playlist has no usable variant") )

    def test_cookies_come_from_the_export_which_is_left_untouched( self ):
        contents = ( "# Netscape HTTP Cookie File\n"
                     "#HttpOnly_127.0.0.1\tFALSE\t/\tFALSE\t0\tsessionid\tfrom-browser\n"
                     "127.0.0.1\tFALSE\t/\tFALSE\t0\tcsrftoken\tcsrf\n" )
        with open( recorder.COOKIES_FILE, "w" ) as f:
            f.write( contents )
        hls, (status, _) = self.lookup()
        self.assertEqual( status, "ok" )
        self.assertEqual( sorted( hls.api_requests[0][1].split("; ") ), [ "csrftoken=csrf", "sessionid=from-browser" ] )
        with open( recorder.COOKIES_FILE ) as f:
            self.assertEqual( f.read(), contents )

    def test_recordurbate_site_replaces_the_room_urls_site( self ):
        hls = fake_site()
        self.addCleanup( hls.close )
        with mock.patch.dict( os.environ, { "RECORDURBATE_SITE": hls.base } ), requests.Session() as session:
            status, (_, _, headers) = recorder.lookup( "https://chaturbate.com/room/", session )
        self.assertEqual( status, "ok" )
        self.assertEqual( hls.api_requests, [ ("room", None) ] )
        self.assertEqual( headers["Referer"], hls.base + "/room/" )

    def test_room_status_messages_are_the_ones_streamer_reads( self ):
        self.assertEqual( set( recorder.ROOM_STATUS_MESSAGES.values() ), set( recorder_streamer.ROOM_STATES ) )

class ExtractTest( unittest.TestCase ):
    # recorder.extract: its worker thread, its wait on stop, and its stderr.

    def setUp( self ):
        self.cwd = tempfile.mkdtemp()
        self.addCleanup( shutil.rmtree, self.cwd )
        patcher = mock.patch.object( recorder, "COOKIES_FILE", os.path.join(self.cwd, "chaturbate.com_cookies.txt") )
        patcher.start()
        self.addCleanup( patcher.stop )
        self.stderr = io.StringIO()
        patcher = mock.patch.object( recorder.sys, "stderr", self.stderr )
        patcher.start()
        self.addCleanup( patcher.stop )

    def fake( self, **modes ):
        hls = fake_site( **modes )
        self.addCleanup( hls.close )
        return hls

    def test_ok_returns_the_lookup_and_writes_nothing( self ):
        hls = self.fake()
        status, (_, urls, _) = recorder.extract( hls.room_url(), recorder.Stop() )
        self.assertEqual( status, "ok" )
        self.assertEqual( urls, [ hls.stream_url("video"), hls.stream_url("audio") ] )
        self.assertEqual( self.stderr.getvalue(), "" )

    def test_failure_writes_its_error_line_to_stderr( self ):
        hls = self.fake( api="offline" )
        self.assertEqual( recorder.extract( hls.room_url(), recorder.Stop() ),
                          ("unavailable", "ERROR: [Chaturbate] room: Room is currently offline") )
        self.assertEqual( self.stderr.getvalue(), "ERROR: [Chaturbate] room: Room is currently offline\n" )

    def test_fail_after_first_answers_then_fails( self ):
        hls = self.fake( api="fail-after-first" )
        self.assertEqual( recorder.extract( hls.room_url(), recorder.Stop() )[0], "ok" )
        status, reason = recorder.extract( hls.room_url(), recorder.Stop() )
        self.assertEqual( status, "error" )
        self.assertTrue( reason.startswith("ERROR: [Chaturbate] room: room API request failed: 503"), reason )

    def test_lookup_raising_is_an_error_line( self ):
        with mock.patch.object( recorder, "lookup", side_effect=RuntimeError("boom") ):
            self.assertEqual( recorder.extract( "https://chaturbate.com/room/", recorder.Stop() ),
                              ("error", "ERROR: [Chaturbate] lookup failed: RuntimeError('boom')") )
        self.assertEqual( self.stderr.getvalue(), "ERROR: [Chaturbate] lookup failed: RuntimeError('boom')\n" )

    def test_stop_during_a_hung_request_returns_before_the_lookup_and_within_1s( self ):
        hls = self.fake( api="hang" )
        stop = recorder.Stop()
        set_at = []
        finished = []
        real_lookup = recorder.lookup
        def recording_lookup( url, session ):
            try:
                return real_lookup( url, session )
            finally:
                finished.append( True )
        def stop_once_requested():
            hls.api_arrived.wait()
            set_at.append( time.time() )
            stop.set()
        threading.Thread( target=stop_once_requested, daemon=True ).start()
        with mock.patch.object( recorder, "lookup", recording_lookup ):
            self.assertEqual( recorder.extract( hls.room_url(), stop ), ("error", "lookup interrupted") )
            # extract returned while its worker was still inside the hung request
            self.assertEqual( finished, [] )
        self.assertLess( time.time() - set_at[0], 1 )
        self.assertIsNone( stop.waiter )

    def test_stop_set_before_the_lookup_returns_at_once( self ):
        hls = self.fake( api="hang" )
        stop = recorder.Stop()
        stop.set()
        self.assertEqual( recorder.extract( hls.room_url(), stop ), ("error", "lookup interrupted") )
        self.assertEqual( hls.api_requests, [] )

    def test_stop_set_as_the_answer_arrives_is_interrupted( self ):
        stop = recorder.Stop()
        def answer_then_stop( url, session ):
            stop.set()
            return ( "ok", ("room 2026-01-01 00_00", [], {}) )
        with mock.patch.object( recorder, "lookup", answer_then_stop ):
            self.assertEqual( recorder.extract( "https://chaturbate.com/room/", stop ), ("error", "lookup interrupted") )

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
