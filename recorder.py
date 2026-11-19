#!/usr/bin/env python3
"""Record one Chaturbate room into a growing MPEG-TS file.

    recorder.py https://chaturbate.com/<name>/

The room's LL-HLS stream carries video and audio as separate fMP4 playlists
with a window of a few seconds. This process fetches every full segment
itself, with short timeouts and quick retries, and feeds them to a local
ffmpeg that only muxes them into MPEG-TS. ffmpeg never touches the network,
so a dropped connection costs the segments it covered and nothing more: the
recording carries on in the same file across gaps, reconnects and fresh
stream URLs. It exits when yt-dlp reports the room unavailable, when no
segment arrives for GIVE_UP_AFTER seconds, or on SIGINT/SIGTERM.
"""
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin

import requests

import config as Config
from streamer import ROOM_STATES

POLL_INTERVAL = 1.0      # seconds between playlist reloads
HTTP_TIMEOUT = (3, 5)    # connect, read
SEGMENT_TRIES = 3
REEXTRACT_AFTER = 15     # seconds without every playlist loading before asking yt-dlp for fresh URLs
GIVE_UP_AFTER = 120      # seconds without a new segment before exiting
EXTRACT_TIMEOUT = 60     # seconds a yt-dlp lookup may take
CLOSE_TIMEOUT = 3        # seconds each muxer shutdown phase may take

def log( message ):
    print( "[recorder {}] {}".format( time.strftime("%Y-%m-%d %H:%M:%S"), message ), flush=True )

def signal_ready( fd ):
    # Tells the daemon that launched this recorder that the room resolved. The
    # daemon may have handed off and exited, so a closed pipe is not an error.
    try:
        os.write( fd, b"ready\n" )
    except OSError:
        pass
    os.close( fd )

def extract( url, config, stop ):
    # Resolves the room through yt-dlp. Returns ("ok", (title, urls, headers)),
    # ("unavailable", reason) when yt-dlp names a room state, or
    # ("error", reason) for anything else. yt-dlp's stderr is passed on to
    # ours, which the daemon appends to the capture log.
    cmd = config["youtube-dl_cmd"].split(" ") + [ "--config-location", config["youtube-dl_config"], "--dump-single-json", url ]
    try:
        proc = subprocess.Popen( cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
    except OSError as e:
        return "error", "could not run yt-dlp: {}".format( e )
    deadline = time.time() + EXTRACT_TIMEOUT
    while True:
        try:
            out, err = proc.communicate( timeout=0.5 )
            break
        except subprocess.TimeoutExpired:
            if stop.is_set() or time.time() > deadline:
                proc.kill()
                proc.communicate()
                if stop.is_set():
                    return "error", "yt-dlp lookup interrupted"
                return "error", "yt-dlp took over {}s".format( EXTRACT_TIMEOUT )
    text = err.decode( "utf-8", "replace" )
    sys.stderr.write( text )
    sys.stderr.flush()
    if proc.returncode != 0:
        errors = [ line.strip() for line in text.splitlines() if line.startswith("ERROR:") ]
        if errors and errors[-1].endswith( ROOM_STATES ):
            return "unavailable", errors[-1]
        return "error", errors[-1] if errors else "yt-dlp exited with {}".format( proc.returncode )
    try:
        info = json.loads( out )
        formats = info.get("requested_formats") or [ info ]
        urls = [ f["url"] for f in formats ]
        title = info["title"]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return "error", "unreadable yt-dlp output: {!r}".format( e )
    headers = info.get("http_headers") or formats[0].get("http_headers") or {}
    return "ok", (title, urls, headers)

def parse_playlist( text ):
    sequence = 0
    uris = []
    init_uri = None
    ended = False
    after_extinf = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int( line.split(":", 1)[1] )
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search( r'URI="([^"]+)"', line )
            init_uri = match.group(1) if match else None
        elif line.startswith("#EXTINF:"):
            after_extinf = True
        elif line == "#EXT-X-ENDLIST":
            ended = True
        elif line and not line.startswith("#") and after_extinf:
            uris.append( line )
            after_extinf = False
    return [ (sequence + i, uri) for i, uri in enumerate(uris) ], init_uri, ended

def fetch( session, url, tries = SEGMENT_TRIES, stop = None ):
    # Returns (bytes, None) or (None, reason). 403, 404 and 410 end the
    # attempts at once; a set stop event ends them before the next attempt.
    reason = None
    for attempt in range( tries ):
        if stop is not None and stop.is_set():
            return None, reason or "stopped"
        try:
            response = session.get( url, timeout=HTTP_TIMEOUT )
            if response.status_code == 200:
                return response.content, None
            reason = "HTTP {}".format( response.status_code )
            if response.status_code in (403, 404, 410):
                break
        except requests.RequestException as e:
            reason = "{}: {}".format( e.__class__.__name__, e )
        if attempt + 1 < tries:
            if stop is not None:
                stop.wait( 0.3 )
            else:
                time.sleep( 0.3 )
    return None, reason

class Track:

    def __init__( self, url ):
        self.url = url
        self.next_sequence = None
        self.init = None
        self.segments = []

class Muxer:
    # A local ffmpeg reading one fMP4 input per track from pipes and writing
    # MPEG-TS to stdout, which is appended to the recording.

    def __init__( self, path, inits ):
        self.out = open( path, "ab" )
        pipes = [ os.pipe() for _ in inits ]
        cmd = [ "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin" ]
        for read_fd, _ in pipes:
            cmd += [ "-f", "mov", "-i", "pipe:{}".format(read_fd) ]
        for index in range( len(pipes) ):
            cmd += [ "-map", str(index) ]
        cmd += [ "-c", "copy", "-f", "mpegts", "pipe:1" ]
        self.proc = subprocess.Popen( cmd, stdout=subprocess.PIPE, pass_fds=[ read_fd for read_fd, _ in pipes ] )
        for read_fd, _ in pipes:
            os.close( read_fd )
        # A queue per input, so a fetch never waits on ffmpeg reading.
        self.queues = []
        self.writers = []
        for (_, write_fd), init in zip( pipes, inits ):
            q = queue.Queue()
            q.put( init )
            thread = threading.Thread( target=self._write, args=(q, write_fd), daemon=True )
            thread.start()
            self.queues.append( q )
            self.writers.append( thread )
        self.reader = threading.Thread( target=self._read, daemon=True )
        self.reader.start()

    def _write( self, q, fd ):
        try:
            while True:
                data = q.get()
                if data is None:
                    break
                view = memoryview( data )
                while view:
                    view = view[ os.write( fd, view ): ]
        except OSError:
            pass # ffmpeg exited; the main loop notices and starts another
        finally:
            os.close( fd )

    def _read( self ):
        while True:
            chunk = self.proc.stdout.read1( 1 << 16 )
            if not chunk:
                break
            self.out.write( chunk )
            self.out.flush()

    def alive( self ):
        return self.proc.poll() is None

    def feed( self, index, data ):
        self.queues[ index ].put( data )

    def close( self ):
        # Ends each input so ffmpeg flushes and exits; an ffmpeg that has not
        # drained its inputs and exited within the budget is killed.
        for q in self.queues:
            q.put( None )
        deadline = time.time() + CLOSE_TIMEOUT
        for thread in self.writers:
            thread.join( max(0, deadline - time.time()) )
        self.reader.join( CLOSE_TIMEOUT )
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.reader.join()
        self.proc.stdout.close()
        self.out.close()

def main():
    url = sys.argv[1]
    name = url.rstrip("/").rsplit("/", 1)[-1]
    config = Config.load_config()

    stop = threading.Event()
    def on_signal( signum, frame ):
        stop.set()
    signal.signal( signal.SIGINT, on_signal )
    signal.signal( signal.SIGTERM, on_signal )

    status, result = extract( url, config, stop )
    if status != "ok":
        log( "Cannot record {}: {}".format(name, result) )
        sys.exit( 1 )
    title, urls, headers = result
    os.makedirs( os.path.join("videos", name), exist_ok=True )
    path = os.path.join( "videos", name, title + ".ts" )
    log( "Recording {} to {}".format(name, path) )
    if len( sys.argv ) > 2:
        signal_ready( int(sys.argv[2]) )

    session = requests.Session()
    session.headers.update( headers )
    tracks = [ Track(u) for u in urls ]
    muxer = None
    now = time.time()
    last_full_load = now
    last_extract = now
    last_segment = now

    try:
        while not stop.is_set():
            started = time.time()

            # Load every playlist and its init segment before any media
            # segment, so the muxer can start with all tracks.
            loaded = 0
            ended = False
            for track in tracks:
                if stop.is_set():
                    break
                track.segments = []
                data, reason = fetch( session, track.url, tries=1, stop=stop )
                if data is None or not data.startswith( b"#EXTM3U" ):
                    continue
                segments, init_uri, track_ended = parse_playlist( data.decode("utf-8", "replace") )
                ended = ended or track_ended
                if init_uri:
                    init, reason = fetch( session, urljoin(track.url, init_uri), tries=1, stop=stop )
                    if init is None and track.init is None:
                        log( "Init segment failed: {}".format(reason) )
                        continue
                    if init is not None and init != track.init:
                        if track.init is not None and muxer:
                            log( "Stream parameters changed; restarting muxer" )
                            muxer.close()
                            muxer = None
                        track.init = init
                if segments and track.next_sequence is not None and segments[-1][0] < track.next_sequence - 1:
                    log( "Segment numbering restarted ({} -> {})".format(track.next_sequence, segments[0][0]) )
                    track.next_sequence = None
                track.segments = segments
                loaded += 1
            if loaded == len( tracks ):
                last_full_load = time.time()

            if muxer and not muxer.alive():
                log( "ffmpeg exited with {}; restarting muxer".format(muxer.proc.returncode) )
                muxer.close()
                muxer = None
            if muxer is None and all( track.init for track in tracks ):
                muxer = Muxer( path, [ track.init for track in tracks ] )

            if muxer:
                for index, track in enumerate( tracks ):
                    for sequence, uri in track.segments:
                        if stop.is_set():
                            break
                        if track.next_sequence is None:
                            track.next_sequence = sequence
                        if sequence < track.next_sequence:
                            continue
                        if sequence > track.next_sequence:
                            log( "Track {} lost segments {}-{}: gone from the playlist before they could be fetched".format(index, track.next_sequence, sequence - 1) )
                        data, reason = fetch( session, urljoin(track.url, uri), stop=stop )
                        track.next_sequence = sequence + 1
                        if data is None:
                            log( "Track {} segment {} failed: {}".format(index, sequence, reason) )
                            continue
                        muxer.feed( index, data )
                        last_segment = time.time()

            if stop.is_set():
                break
            if ended:
                log( "Stream ended" )
                break
            if time.time() - last_segment > GIVE_UP_AFTER:
                log( "No new segment in {}s; giving up".format(GIVE_UP_AFTER) )
                break
            if time.time() - last_full_load > REEXTRACT_AFTER and time.time() - last_extract > REEXTRACT_AFTER:
                log( "Playlists unreachable for {}s; asking yt-dlp for fresh URLs".format(REEXTRACT_AFTER) )
                last_extract = time.time()
                status, result = extract( url, config, stop )
                if status == "unavailable":
                    log( "Room unavailable: {}".format(result) )
                    break
                if status == "error":
                    log( "Re-resolution failed: {}; still recording".format(result) )
                else:
                    _, urls, headers = result
                    session.headers.update( headers )
                    if len( urls ) != len( tracks ):
                        log( "Track count changed ({} -> {}); restarting muxer".format(len(tracks), len(urls)) )
                        if muxer:
                            muxer.close()
                            muxer = None
                        tracks = [ Track(u) for u in urls ]
                    else:
                        for track, new_url in zip( tracks, urls ):
                            track.url = new_url
                    last_full_load = time.time()

            stop.wait( max( 0, POLL_INTERVAL - (time.time() - started) ) )
    finally:
        if muxer:
            muxer.close()
        if os.path.exists( path ) and os.path.getsize( path ) == 0:
            os.remove( path )
        log( "Stopped recording {}".format(name) )

if __name__ == "__main__":
    main()
