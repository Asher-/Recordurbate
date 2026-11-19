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

    def __init__( self, failing = () ):
        self.failing = set( failing )
        self.started = time.time()
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
        result = self.run_recorder( { "FAKE_YTDLP_OFFLINE": "1" } )
        self.assertEqual( result.returncode, 1 )
        self.assertIn( "ERROR: [Chaturbate] room: Room is currently offline", result.stdout )
        self.assertFalse( os.path.exists(os.path.join(self.cwd, "videos")) )

    def test_sigint_stops_and_keeps_recording( self ):
        hls = FakeHls()
        try:
            proc = subprocess.Popen( [ sys.executable, os.path.join(ROOT, "recorder.py"), "https://chaturbate.com/room/" ],
                                     cwd=self.cwd, env=dict(os.environ, FAKE_HLS_BASE=hls.base),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True )
            time.sleep( 4 )
            proc.send_signal( 2 )
            out, _ = proc.communicate( timeout=15 )
        finally:
            hls.close()
        self.assertIn( "Stopped recording room", out )
        path = os.path.join( self.cwd, "videos", "room", "room 2026-01-01 00_00.ts" )
        codecs, container, duration = probe( path )
        self.assertEqual( codecs, [ "aac", "h264" ] )
        self.assertEqual( subprocess.run(["pgrep", "-P", str(proc.pid)], stdout=subprocess.PIPE).stdout, b"" )

if __name__ == "__main__":
    unittest.main()
