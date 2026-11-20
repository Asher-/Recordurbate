#!/usr/bin/env python3
"""Record one Chaturbate room into a growing MPEG-TS file.

    recorder.py https://chaturbate.com/<name>/

The room's LL-HLS stream carries video and audio as separate fMP4 playlists
with a window of a few seconds. This process fetches every full segment
itself, with short timeouts and quick retries, and feeds them to a local
ffmpeg that only muxes them into MPEG-TS. ffmpeg never touches the network,
so a dropped connection costs the segments it covered and nothing more: the
recording carries on in the same file across gaps, reconnects and fresh
stream URLs. It exits when the site reports the room unavailable, when no
segment arrives for GIVE_UP_AFTER seconds, or on SIGINT/SIGTERM.
"""
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests

from pid import record_recorder, forget_recorder

POLL_INTERVAL = 1.0      # seconds between playlist reloads
HTTP_TIMEOUT = (3, 5)    # connect, read
SEGMENT_TRIES = 3
REEXTRACT_AFTER = 15     # seconds without every playlist loading before asking the site for fresh URLs
GIVE_UP_AFTER = 120      # seconds without a new segment before exiting
LOOKUP_TIMEOUT = (3, 5)  # connect, read, for each request of a room lookup
LOOKUP_POLL = 0.1        # seconds between checks of stop while a lookup runs
CLOSE_TIMEOUT = 3        # seconds each muxer shutdown phase may take
COOKIES_FILE = "chaturbate.com_cookies.txt"  # the user's browser export; read, never written
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
# The edge API's room_status values for a room that is up with no public
# stream, in yt-dlp's wording, which streamer.ROOM_STATES matches.
ROOM_STATUS_MESSAGES = {
    "offline": "Room is currently offline",
    "private": "Room is currently in a private show",
    "away": "Performer is currently away",
    "password protected": "Room is password protected",
    "hidden": "Hidden session in progress",
}

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

class Stop( threading.Event ):
    # Set on SIGINT or SIGTERM.
    pass

def load_cookies():
    # The user's browser export, read and never written. Lines marked
    # #HttpOnly_ are cookies too; other # lines are comments.
    jar = requests.cookies.RequestsCookieJar()
    try:
        export = open( COOKIES_FILE, encoding="utf-8", errors="replace" )
    except FileNotFoundError:
        return jar
    with export:
        for line in export:
            if line.startswith( "#HttpOnly_" ):
                line = line[ len("#HttpOnly_"): ]
            elif line.startswith( "#" ):
                continue
            fields = line.rstrip( "\r\n" ).split( "\t" )
            if len( fields ) == 7:
                domain, _, path, _, _, name, value = fields
                jar.set( name, value, domain=domain, path=path )
    return jar

def parse_master( text, base ):
    # The highest-BANDWIDTH variant's playlist URL and, when the variant names
    # an audio group, that group's DEFAULT=YES rendition (else its first).
    # None when the playlist has no usable variant.
    lines = text.splitlines()
    audio = {}
    best = None
    for i, line in enumerate( lines ):
        if line.startswith( "#EXT-X-MEDIA:" ) and "TYPE=AUDIO" in line:
            group = re.search( r'GROUP-ID="([^"]+)"', line )
            uri = re.search( r'URI="([^"]+)"', line )
            if group and uri:
                audio.setdefault( group.group(1), [] ).append( ("DEFAULT=YES" in line, uri.group(1)) )
        elif line.startswith( "#EXT-X-STREAM-INF:" ):
            bandwidth = re.search( r'[:,]BANDWIDTH=(\d+)', line )
            group = re.search( r'AUDIO="([^"]+)"', line )
            uri = lines[i + 1].strip() if i + 1 < len( lines ) else ""
            if bandwidth and uri and not uri.startswith( "#" ) and ( best is None or int(bandwidth.group(1)) > best[0] ):
                best = ( int(bandwidth.group(1)), uri, group.group(1) if group else None )
    if best is None:
        return None
    urls = [ urljoin(base, best[1]) ]
    if best[2] is not None:
        renditions = audio.get( best[2] )
        if not renditions:
            return None
        defaults = [ uri for default, uri in renditions if default ]
        urls.append( urljoin(base, defaults[0] if defaults else renditions[0][1]) )
    return urls

def lookup( url ):
    # One edge-API request for the room and, when it is live, one request for
    # its master playlist. Returns ("ok", (title, urls, headers)),
    # ("unavailable", line) when the site names a room state, or
    # ("error", line); each line is an "ERROR: [Chaturbate] <room>: ..." line.
    parts = urlsplit( url )
    room = parts.path.strip( "/" ).split( "/" )[0]
    # RECORDURBATE_SITE stands in for the room URL's site in tests
    site = os.environ.get( "RECORDURBATE_SITE" ) or "{}://{}".format( parts.scheme, parts.netloc )
    headers = { "User-Agent": USER_AGENT, "Referer": "{}/{}/".format(site, room) }
    def line( message ):
        return "ERROR: [Chaturbate] {}: {}".format( room, message )
    with requests.Session() as session:
        try:
            response = session.post( site + "/get_edge_hls_url_ajax/", data={ "room_slug": room },
                                     headers=dict( headers, **{ "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" } ),
                                     cookies=load_cookies(), timeout=LOOKUP_TIMEOUT )
            response.raise_for_status()
            info = response.json()
        except ValueError:
            return "error", line( "unreadable room API response" )
        except requests.RequestException as e:
            return "error", line( "room API request failed: {}".format(e) )
        if not isinstance( info, dict ):
            return "error", line( "unreadable room API response" )
        stream = info.get( "url" )
        if not stream:
            status = info.get( "room_status" )
            if status in ROOM_STATUS_MESSAGES:
                return "unavailable", line( ROOM_STATUS_MESSAGES[status] )
            return "error", line( 'room API gave no stream for room status "{}"'.format(status) )
        try:
            master = session.get( stream, headers=headers, timeout=LOOKUP_TIMEOUT )
            master.raise_for_status()
        except requests.RequestException as e:
            return "error", line( "master playlist request failed: {}".format(e) )
        urls = parse_master( master.text, master.url )
        if urls is None:
            return "error", line( "master playlist has no usable variant" )
    title = "{} {}".format( room, time.strftime("%Y-%m-%d %H_%M") )
    return "ok", (title, urls, headers)

def extract( url, stop ):
    # Resolves the room through the site's edge API in a worker thread, so a
    # set stop returns ("error", "lookup interrupted") within LOOKUP_POLL
    # seconds. Returns lookup's result otherwise, writing its ERROR: line to
    # stderr, which the daemon appends to the capture log for
    # Streamer.launch_error.
    results = queue.Queue()
    def work():
        try:
            results.put( lookup(url) )
        except Exception as e:
            results.put( ("error", "ERROR: [Chaturbate] lookup failed: {!r}".format(e)) )
    threading.Thread( target=work, daemon=True ).start()
    while True:
        if stop.is_set():
            return "error", "lookup interrupted"
        try:
            status, result = results.get( timeout=LOOKUP_POLL )
            break
        except queue.Empty:
            pass
    if stop.is_set():
        return "error", "lookup interrupted"
    if status != "ok":
        sys.stderr.write( result + "\n" )
        sys.stderr.flush()
    return status, result

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

def run():
    # Entry point: recorder.py https://chaturbate.com/<name>/ [<ready fd>]
    url = sys.argv[1]
    ready_fd = int( sys.argv[2] ) if len( sys.argv ) > 2 else None

    # The main thread only takes signals; everything else runs on a worker.
    stop = Stop()
    def on_signal( signum, frame ):
        stop.set()
    signal.signal( signal.SIGINT, on_signal )
    signal.signal( signal.SIGTERM, on_signal )
    outcome = { "exit_code": 1 }
    worker = threading.Thread( target=recording, args=(url, ready_fd, stop, outcome) )
    worker.start()
    worker.join()
    sys.exit( outcome["exit_code"] )

def recording( url, ready_fd, stop, outcome ):
    # On run()'s worker: records this process's PID before anything else, so
    # a daemon started after a handoff or a crash can adopt it, records the
    # room, and forgets the PID as it ends.
    name = url.rstrip("/").rsplit("/", 1)[-1]
    record_recorder( name, os.getpid() )
    try:
        main( url, ready_fd, stop, outcome )
    finally:
        forget_recorder( name, os.getpid() )

def main( url, ready_fd, stop, outcome ):
    # Records the room until it ends or stop is set. outcome["exit_code"]
    # becomes 0 once a recording has run to its end.
    name = url.rstrip("/").rsplit("/", 1)[-1]
    status, result = extract( url, stop )
    if status != "ok":
        log( "Cannot record {}: {}".format(name, result) )
        return
    title, urls, headers = result
    os.makedirs( os.path.join("videos", name), exist_ok=True )
    path = os.path.join( "videos", name, title + ".ts" )
    log( "Recording {} to {}".format(name, path) )
    if ready_fd is not None:
        signal_ready( ready_fd )

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
                log( "Playlists unreachable for {}s; asking the site for fresh URLs".format(REEXTRACT_AFTER) )
                last_extract = time.time()
                status, result = extract( url, stop )
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
    outcome["exit_code"] = 0

if __name__ == "__main__":
    run()
