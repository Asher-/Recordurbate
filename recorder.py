#!/usr/bin/env python3
"""Record one Chaturbate room into a growing MPEG-TS file.

    recorder.py https://chaturbate.com/<name>/

The room's LL-HLS stream carries video and audio as separate fMP4 playlists
with a window of a few seconds. This process fetches every full segment
itself, with short timeouts and quick retries, and feeds them to a local
ffmpeg that only muxes them into MPEG-TS. ffmpeg never touches the network,
so a dropped connection costs the segments it covered and nothing more: the
recording carries on in the same file across gaps, reconnects and fresh
stream URLs. When the site reports the room unavailable it waits up to
room_return_wait_minutes (configs/config.json) for the room to come back and
records on into the same file; it exits when the room stays away that long,
when no segment arrives for GIVE_UP_AFTER seconds, or on SIGINT/SIGTERM.
As it exits it starts a process that remuxes the finished .ts into an .mp4 and
removes the .ts.
"""
import fcntl
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import requests

from pid import record_recorder, forget_recorder

POLL_INTERVAL = 1.0      # seconds between playlist reloads
HTTP_TIMEOUT = (3, 5)    # connect, read
SEGMENT_TRIES = 3
FETCH_WORKERS = 8        # segment fetches in flight at once, across all tracks
PLAYLIST_TIMEOUT = (3, 1.5)  # connect, read: a playlist is a few KB and answers in ~0.05s
PLAYLIST_TRIES = 3
BACKFILL_LIMIT = 10      # most segments fetched directly after a gap; the CDN keeps one ~10s past the window
SEGMENT_TIMEOUT = (3, 2) # connect, read: seconds without a byte before a segment request is abandoned
SEGMENT_DEADLINE = 12    # seconds a segment is asked for again after its first request; the CDN keeps one ~11-16s
SLOW_RELOAD = 3          # seconds a playlist reload may take before its timing is logged
RETURN_POLL = 3          # seconds between room checks while waiting for a room to come back
DEFAULT_RETURN_WAIT_MINUTES = 10
CONFIG_FILE = os.path.join( "configs", "config.json" )
REEXTRACT_AFTER = 15     # seconds without every playlist loading before asking the site for fresh URLs
GIVE_UP_AFTER = 120      # seconds without a new segment before exiting
LOOKUP_TIMEOUT = (3, 5)  # connect, read, for each request of a room lookup
WRITE_RETRY = 1          # seconds between tries at a write the disk refused
AUDIO_GAP = 0.1          # seconds between audio packets that make a gap; AAC's are ~0.02s apart
MP4_TOLERANCE = 1      # seconds an MP4 may differ in length from the .ts it replaces
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
    # A log that cannot be written, as on a full disk, must not end a recording.
    try:
        print( "[recorder {}] {}".format( time.strftime("%Y-%m-%d %H:%M:%S"), message ), flush=True )
    except OSError:
        pass

def signal_ready( fd ):
    # Tells the daemon that launched this recorder that the room resolved. The
    # daemon may have handed off and exited, so a closed pipe is not an error.
    try:
        os.write( fd, b"ready\n" )
    except OSError:
        pass
    os.close( fd )

class Stop( threading.Event ):
    # Set on SIGINT or SIGTERM. Setting it also wakes a lookup waiting in
    # extract, through the queue that lookup registered as waiter.
    waiter = None

    def set( self ):
        super().set()
        waiter = self.waiter
        if waiter is not None:
            waiter.put( None )

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
    # an audio group, that group's DEFAULT=YES rendition (else its first); a
    # rendition with no URI is carried in the variant itself, so the variant
    # is the only URL. None when the playlist has no usable variant or names
    # an audio group it does not declare.
    lines = text.splitlines()
    audio = {}
    best = None
    for i, line in enumerate( lines ):
        if line.startswith( "#EXT-X-MEDIA:" ) and "TYPE=AUDIO" in line:
            group = re.search( r'GROUP-ID="([^"]+)"', line )
            uri = re.search( r'URI="([^"]+)"', line )
            if group:
                audio.setdefault( group.group(1), [] ).append( ("DEFAULT=YES" in line, uri.group(1) if uri else None) )
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
        chosen = defaults[0] if defaults else renditions[0][1]
        if chosen is not None:
            urls.append( urljoin(base, chosen) )
    return urls

def lookup( url, session ):
    # One edge-API request for the room through session and, when it is live,
    # one request for its master playlist. Returns ("ok", (title, urls,
    # headers)), ("unavailable", line) when the site names a room state, or
    # ("error", line); each line is an "ERROR: [Chaturbate] <room>: ..." line.
    parts = urlsplit( url )
    room = parts.path.strip( "/" ).split( "/" )[0]
    # RECORDURBATE_SITE stands in for the room URL's site in tests
    site = os.environ.get( "RECORDURBATE_SITE" ) or "{}://{}".format( parts.scheme, parts.netloc )
    headers = { "User-Agent": USER_AGENT, "Referer": "{}/{}/".format(site, room) }
    def line( message ):
        return "ERROR: [Chaturbate] {}: {}".format( room, message )
    try:
        response = session.post( site + "/get_edge_hls_url_ajax/", data={ "room_slug": room },
                                 headers=dict( headers, **{ "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" } ),
                                 cookies=load_cookies(), timeout=LOOKUP_TIMEOUT )
        response.raise_for_status()
    except requests.RequestException as e:
        return "error", line( "room API request failed: {}".format(e) )
    try:
        info = response.json()
    except ValueError:
        return "error", line( "unreadable room API response" )
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

def return_wait_minutes():
    # configs/config.json's room_return_wait_minutes: how long a room that
    # went unavailable mid-recording is waited for. Read when a room goes
    # away, so an edit reaches running recorders; DEFAULT_RETURN_WAIT_MINUTES
    # when the file or the value is unreadable or negative.
    try:
        with open( CONFIG_FILE ) as f:
            minutes = float( json.load( f ).get( "room_return_wait_minutes", DEFAULT_RETURN_WAIT_MINUTES ) )
    except ( OSError, ValueError, TypeError, AttributeError ):
        return DEFAULT_RETURN_WAIT_MINUTES
    return minutes if minutes >= 0 else DEFAULT_RETURN_WAIT_MINUTES

def await_return( url, stop, minutes ):
    # Asks the site for the room every RETURN_POLL seconds until it is public
    # again, `minutes` have passed, or stop is set. Returns extract's last
    # result, or ("error", "lookup interrupted") when stopped.
    deadline = time.time() + minutes * 60
    while True:
        stop.wait( RETURN_POLL )
        if stop.is_set():
            return "error", "lookup interrupted"
        status, result = extract( url, stop, report=False )
        if status == "ok":
            return status, result
        if time.time() >= deadline:
            log( "Room did not come back within {:g} min; last answer: {}".format(minutes, result) )
            return status, result

def extract( url, stop, report = True ):
    # Resolves the room through lookup on a worker thread and blocks until the
    # lookup answers or stop is set: Stop.set puts None on the queue this call
    # registers as stop's waiter. A stopped lookup returns ("error",
    # "lookup interrupted") at once; its worker is a daemon thread that ends
    # when its request times out (LOOKUP_TIMEOUT), and stop is set only as the
    # recorder exits. Otherwise returns lookup's result, writing its ERROR:
    # line to stderr, which the daemon appends to the capture log for
    # Streamer.launch_error.
    results = queue.Queue()
    stop.waiter = results
    try:
        if stop.is_set():
            return "error", "lookup interrupted"
        def work():
            try:
                with requests.Session() as session:
                    results.put( lookup(url, session) )
            except Exception as e:
                results.put( ("error", "ERROR: [Chaturbate] lookup failed: {!r}".format(e)) )
        threading.Thread( target=work, daemon=True ).start()
        answer = results.get()
    finally:
        stop.waiter = None
    if answer is None or stop.is_set():
        return "error", "lookup interrupted"
    status, result = answer
    if status != "ok" and report:
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

def sibling_uri( uri, sequence, wanted ):
    # The URI of segment `wanted` of the track whose segment `sequence` is at
    # uri: the one run of digits in uri's file name that is sequence, replaced
    # by wanted. None when the file name does not hold sequence exactly once.
    path, mark, query = uri.partition( "?" )
    head, slash, name = path.rpartition( "/" )
    runs = [ m for m in re.finditer( r"\d+", name ) if m.group() == str(sequence) ]
    if len( runs ) != 1:
        return None
    run = runs[0]
    return head + slash + name[:run.start()] + str(wanted) + name[run.end():] + mark + query

def fetch( session, url, tries = SEGMENT_TRIES, stop = None, timeout = HTTP_TIMEOUT, failures = None ):
    # Returns (bytes, None) or (None, reason). 403, 404 and 410 end the
    # attempts at once; a set stop event ends them before the next attempt.
    # When failures is a list, each failed attempt's reason is appended to it
    # with its start time relative to the first, whatever the outcome.
    reason = None
    started = time.time()
    for attempt in range( tries ):
        if stop is not None and stop.is_set():
            return None, reason or "stopped"
        offset = time.time() - started
        try:
            response = session.get( url, timeout=timeout )
            if response.status_code == 200:
                return response.content, None
            reason = "HTTP {}".format( response.status_code )
            if response.status_code in (403, 404, 410):
                break
        except requests.RequestException as e:
            reason = "{}: {}".format( e.__class__.__name__, e )
        if failures is not None:
            failures.append( "+{:.1f}s {}".format(offset, reason) )
        if attempt + 1 < tries:
            if stop is not None:
                stop.wait( 0.3 )
            else:
                time.sleep( 0.3 )
    return None, reason

def timed( function, *args, **kwargs ):
    # function's (data, reason) result with the seconds it took appended
    started = time.time()
    return function( *args, **kwargs ) + ( time.time() - started, )

def fetch_segment( session, url, stop ):
    # Asks for a segment until it answers, the CDN says it is gone (403, 404
    # or 410), stop is set, or SEGMENT_DEADLINE has passed since the first
    # request. Returns (bytes, None) or (None, reason) where reason names
    # every attempt with its start time relative to the first.
    started = time.time()
    reasons = []
    while True:
        offset = time.time() - started
        data, reason = fetch( session, url, tries=1, stop=stop, timeout=SEGMENT_TIMEOUT )
        if data is not None:
            return data, None
        reasons.append( "+{:.1f}s {}".format(offset, reason) )
        if reason in ( "HTTP 403", "HTTP 404", "HTTP 410" ) or stop.is_set() or time.time() - started >= SEGMENT_DEADLINE:
            return None, "; ".join( reasons )
        stop.wait( 0.3 )

class Track:

    def __init__( self, url ):
        self.url = url
        self.next_sequence = None
        self.init = None
        self.init_uri = None
        self.segments = []
        # digest of the last segment fed, to drop one that repeats it
        self.last_digest = None
        # (sequence, future of fetch's result) in sequence order, not yet fed
        self.pending = deque()

def feed_fetched( tracks, muxer, wait = False ):
    # Feeds each track's fetched segments to muxer in sequence order, stopping
    # at the first still in flight unless wait is set. Returns whether any
    # segment was fed.
    fed = False
    for index, track in enumerate( tracks ):
        while track.pending and ( wait or track.pending[0][1].done() ):
            sequence, future = track.pending.popleft()
            data, reason = future.result()
            if data is None:
                log( "Track {} segment {} failed: {}".format(index, sequence, reason) )
                continue
            digest = hashlib.sha256( data ).digest()
            if digest == track.last_digest:
                log( "Track {} segment {} repeats the segment before it; dropped".format(index, sequence) )
                continue
            track.last_digest = digest
            muxer.feed( index, data )
            fed = True
    return fed

TS_PACKET = 188
TS_TAIL_BYTES = 4 << 20

def pes_pts( packet ):
    # The PTS, in 90 kHz ticks, of a TS packet that starts a PES packet
    # carrying one, else None.
    if not packet[1] & 0x40:
        return None
    control = ( packet[3] >> 4 ) & 3
    if not control & 1:
        return None
    offset = 4 + ( 1 + packet[4] if control & 2 else 0 )
    p = packet[ offset: ]
    if len( p ) < 14 or p[:3] != b"\0\0\1" or not p[7] & 0x80:
        return None
    return ( (p[9] >> 1) & 7 ) << 30 | p[10] << 22 | ( p[11] >> 1 ) << 15 | p[12] << 7 | p[13] >> 1

def ts_end_seconds( path ):
    # The highest PTS among the last TS_TAIL_BYTES of an MPEG-TS file, in
    # seconds; 0 when the file is missing or holds none. Packets are found by
    # their sync byte, so a torn packet from a killed ffmpeg is skipped.
    try:
        with open( path, "rb" ) as f:
            f.seek( 0, os.SEEK_END )
            f.seek( max(0, f.tell() - TS_TAIL_BYTES) )
            data = f.read()
    except FileNotFoundError:
        return 0.0
    end = 0
    i = data.find( b"\x47" )
    while 0 <= i and i + TS_PACKET <= len( data ):
        if i + TS_PACKET < len( data ) and data[ i + TS_PACKET ] != 0x47:
            i = data.find( b"\x47", i + 1 )
            continue
        pts = pes_pts( data[ i:i + TS_PACKET ] )
        if pts is not None and pts > end:
            end = pts
        i += TS_PACKET
    return end / 90000

class Muxer:
    # A local ffmpeg reading one fMP4 input per track from pipes and writing
    # MPEG-TS to stdout, which is appended to the recording. Every ffmpeg
    # starts its output clock near zero, so a muxer appending to a recording
    # starts its timestamps after the file's last one; otherwise the clock
    # jumps back at each restart and players see only the last stretch.

    def __init__( self, path, inits ):
        offset = ts_end_seconds( path )
        self.out = open( path, "ab", buffering=0 ) # nothing held back for close() to fail on
        self.closing = threading.Event()
        pipes = [ os.pipe() for _ in inits ]
        cmd = [ "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin" ]
        for read_fd, _ in pipes:
            cmd += [ "-f", "mov", "-i", "pipe:{}".format(read_fd) ]
        for index in range( len(pipes) ):
            cmd += [ "-map", str(index) ]
        if offset:
            cmd += [ "-output_ts_offset", "{:.3f}".format(offset) ]
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
            if not self._save( chunk ):
                break

    def _save( self, chunk ):
        # Writes chunk whole, trying again while the disk refuses it: ffmpeg
        # waits on its full pipe and the segments on their queues, so a disk
        # that was full loses nothing once it has space. False when the muxer
        # closes first.
        view = memoryview( chunk )
        failed = False
        while view:
            try:
                view = view[ self.out.write( view ): ]
            except OSError as e:
                if not failed:
                    log( "Cannot write the recording ({}); trying until it can be".format(e) )
                failed = True
                if self.closing.wait( WRITE_RETRY ):
                    log( "Recording closed while it could not be written; its last stretch is lost" )
                    return False
        if failed:
            log( "Writing the recording again" )
        return True

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
        self.closing.set()
        self.reader.join()
        self.proc.stdout.close()
        self.out.close()

def sample_descriptions( init ):
    # The stsd boxes of an fMP4 init segment: a track's codec and parameters.
    found = b""
    at = init.find( b"stsd" )
    while at >= 4:
        found += init[ at - 4 : at - 4 + int.from_bytes(init[at - 4:at], "big") ]
        at = init.find( b"stsd", at + 4 )
    return found

def to_mp4( path, keep = False ):
    # Remuxes the finished MPEG-TS recording at path, its video as it is, into an MP4 beside it, which iMovie opens and MPEG-TS it does not, then
    # removes the .ts if each track of the MP4 has the same packets over the
    # same time. keep says the stream's parameters changed as it was recorded:
    # Apple's decoder stops at such a change in the MP4, so the .ts stays. The
    # MP4 is written under a temporary name and renamed into place, so a remux
    # that fails or is cut short leaves the .ts as it was and no .mp4. An MP4
    # that does not match is kept beside its .ts. Returns the path of the MP4,
    # or of the .ts when there is none.
    target = os.path.splitext( path )[0] + ".mp4"
    temporary = target + ".tmp"
    original = media_info( path )
    # Apple's decoders take HEVC under the tag hvc1, not ffmpeg's hev1.
    tag = [ "-tag:v", "hvc1" ] if any( track[0] == "hevc" for track in original or [] ) else []
    # Apple's players close up a gap in an MP4's audio and play the rest out of
    # step with the video, so the gaps are filled with silence, which takes
    # encoding the audio again.
    gaps = has_audio_gaps( path )
    audio = [ "-c:a", "aac", "-af", "aresample=async=1:min_hard_comp=0.1" ] if gaps else [ "-c:a", "copy", "-bsf:a", "aac_adtstoasc" ]
    # By default ffmpeg closes up a gap of over 10s in MPEG-TS timestamps.
    cmd = [ "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y", "-dts_delta_threshold", "1000000", "-i", path,
            "-c:v", "copy" ] + audio + tag + [ "-f", "mp4", temporary ]
    result = subprocess.run( cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True )
    if result.returncode != 0:
        if os.path.exists( temporary ):
            os.remove( temporary )
        log( "Converting {} to MP4 failed ({}); keeping the .ts: {}".format(path, result.returncode, result.stderr.strip()) )
        return path
    converted = media_info( temporary )
    # on disk before the .ts goes, so a power cut cannot take both
    # (macOS's fsync leaves data in the drive's cache; F_FULLFSYNC does not)
    with open( temporary, "rb" ) as f:
        if hasattr( fcntl, "F_FULLFSYNC" ):
            fcntl.fcntl( f.fileno(), fcntl.F_FULLFSYNC )
        else:
            os.fsync( f.fileno() )
    os.replace( temporary, target )
    if keep:
        log( "{} changes stream parameters partway; keeping the .ts too".format(target) )
        return target
    if gaps:
        log( "{} has silence in place of gaps in its audio; keeping the .ts too".format(target) )
        return target
    if "corrupt" in result.stderr.lower():
        log( "{} has damaged packets; keeping the .ts too".format(target) )
        return target
    if original is None or converted is None or len( original ) != len( converted ) or any(
            a[:2] != b[:2] or abs( a[2] - b[2] ) > MP4_TOLERANCE for a, b in zip( original, converted ) ):
        log( "{} does not match its .ts ({} against {}); keeping both".format(target, converted, original) )
        return target
    os.remove( path )
    log( "Saved {}".format(target) )
    return target

def has_audio_gaps( path ):
    # Whether any two audio packets in a row are over AUDIO_GAP seconds apart.
    try:
        probe = subprocess.Popen( [ "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", path ],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True )
    except OSError:
        return False # without ffprobe to_mp4 keeps the .ts whatever this says
    found = False
    last = None
    with probe:
        for line in probe.stdout:
            try:
                pts = float( line.split(",")[0] )
            except ValueError:
                continue
            found = found or ( last is not None and pts - last > AUDIO_GAP )
            last = pts
    return found

def media_info( path ):
    # [(codec, packets, seconds), ...], one per track of a media file; None
    # when ffprobe cannot tell.
    try:
        result = subprocess.run( [ "ffprobe", "-v", "error", "-count_packets", "-show_entries", "stream=codec_name,nb_read_packets,duration", "-of", "json", path ],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL )
        info = json.loads( result.stdout )
        return sorted( (s["codec_name"], int(s["nb_read_packets"]), float(s["duration"])) for s in info["streams"] ) or None
    except (ValueError, KeyError, OSError):
        return None

def start_to_mp4( path, keep ):
    # Converts in a process of its own session: a long recording takes minutes,
    # and the daemon launches no recorder for the room until this one exits.
    subprocess.Popen( [ sys.executable, os.path.abspath(__file__), "--to-mp4", path ] + ( ["--keep"] if keep else [] ), stdin=subprocess.DEVNULL, start_new_session=True )

def recording_path( name, title ):
    # A path no earlier recording has, as a .ts or as the .mp4 made from it:
    # a room recorded twice in a minute gets the same title twice.
    stem = os.path.join( "videos", name, title )
    number = 1
    candidate = stem
    while os.path.exists( candidate + ".ts" ) or os.path.exists( candidate + ".mp4" ):
        number += 1
        candidate = "{} ({})".format( stem, number )
    return candidate + ".ts"

def run():
    # Entry point: recorder.py https://chaturbate.com/<name>/ [<ready fd>]
    # or, for a finished recording, recorder.py --to-mp4 <path> [--keep]
    if sys.argv[1] == "--to-mp4":
        to_mp4( sys.argv[2], "--keep" in sys.argv[3:] )
        return
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
    path = recording_path( name, title )
    log( "Recording {} to {}".format(name, path) )
    if ready_fd is not None:
        signal_ready( ready_fd )

    session = requests.Session()
    session.headers.update( headers )
    tracks = [ Track(u) for u in urls ]
    formats = set() # the stream parameters of every muxer started
    muxer = None
    # Segments are fetched on the pool as soon as a playlist lists them, so a
    # slow fetch holds up neither the next playlist reload nor the other
    # track; the window is a few seconds long.
    pool = ThreadPoolExecutor( FETCH_WORKERS )
    # Playlists load on a pool of their own, all tracks at once, so they never
    # queue behind segment fetches or behind each other.
    list_pool = ThreadPoolExecutor( 4 )
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
            failures = [ [] for track in tracks ]
            lists = [ list_pool.submit( timed, fetch, session, track.url, PLAYLIST_TRIES, stop, PLAYLIST_TIMEOUT, failed ) for track, failed in zip( tracks, failures ) ]
            notes = []
            for index, ( track, listed ) in enumerate( zip( tracks, lists ) ):
                if stop.is_set():
                    break
                track.segments = []
                data, reason, took = listed.result()
                notes.append( "track {} playlist {} in {:.1f}s{}".format(index, reason or "ok", took,
                                                                       " after " + "; ".join(failures[index]) if data is not None and failures[index] else "") )
                if data is None or not data.startswith( b"#EXTM3U" ):
                    continue
                segments, init_uri, track_ended = parse_playlist( data.decode("utf-8", "replace") )
                ended = ended or track_ended
                # The init segment is fetched again only when its URI changes.
                if init_uri and ( init_uri != track.init_uri or track.init is None ):
                    init, reason, took = timed( fetch, session, urljoin(track.url, init_uri), tries=PLAYLIST_TRIES, stop=stop )
                    notes.append( "track {} init {} in {:.1f}s".format(index, reason or "ok", took) )
                    if init is None and track.init is None:
                        log( "Init segment failed: {}".format(reason) )
                        continue
                    if init is not None:
                        track.init_uri = init_uri
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
            if time.time() - started > SLOW_RELOAD:
                log( "Playlist reload took {:.1f}s: {}".format(time.time() - started, ", ".join(notes)) )

            if muxer and not muxer.alive():
                log( "ffmpeg exited with {}; restarting muxer".format(muxer.proc.returncode) )
                muxer.close()
                muxer = None
            if muxer is None and all( track.init for track in tracks ):
                muxer = Muxer( path, [ track.init for track in tracks ] )
                formats.add( tuple(sample_descriptions(track.init) for track in tracks) )

            if muxer:
                for index, track in enumerate( tracks ):
                    for sequence, uri in track.segments:
                        if track.next_sequence is None:
                            track.next_sequence = sequence
                        if sequence < track.next_sequence:
                            continue
                        if sequence > track.next_sequence:
                            # Segments that left the playlist before it was
                            # read are fetched directly, their URIs built from
                            # this one's, while the CDN still serves them.
                            first = max( track.next_sequence, sequence - BACKFILL_LIMIT )
                            if sibling_uri( uri, sequence, first ) is None:
                                first = sequence
                            if first > track.next_sequence:
                                log( "Track {} lost segments {}-{}: gone from the playlist before they could be fetched".format(index, track.next_sequence, first - 1) )
                            if first < sequence:
                                log( "Track {} segments {}-{} left the playlist before it was read; fetching them directly".format(index, first, sequence - 1) )
                            for missing in range( first, sequence ):
                                track.pending.append( ( missing, pool.submit( fetch_segment, session, urljoin(track.url, sibling_uri(uri, sequence, missing)), stop ) ) )
                        track.pending.append( ( sequence, pool.submit( fetch_segment, session, urljoin(track.url, uri), stop ) ) )
                        track.next_sequence = sequence + 1
                if feed_fetched( tracks, muxer ):
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
                    # A broadcaster who drops and reconnects comes back as a
                    # new stream: wait for it and record it into this file
                    # through a new muxer, rather than leave the gap until
                    # the daemon's next pass relaunches the room.
                    minutes = return_wait_minutes()
                    log( "Room unavailable: {}; waiting up to {:g} min for it to come back".format(result, minutes) )
                    status, result = await_return( url, stop, minutes )
                    if status != "ok":
                        break
                    log( "Room is back; recording on in the same file" )
                    if muxer:
                        feed_fetched( tracks, muxer, wait=True )
                        muxer.close()
                        muxer = None
                    _, urls, headers = result
                    session.headers.update( headers )
                    tracks = [ Track(u) for u in urls ]
                    last_full_load = last_segment = time.time()
                elif status == "error":
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
        # A stream that ended or went quiet keeps the segments still in
        # flight; a stopped recorder does not wait for them.
        if muxer and not stop.is_set():
            feed_fetched( tracks, muxer, wait=True )
        pool.shutdown( wait=False, cancel_futures=True )
        list_pool.shutdown( wait=False, cancel_futures=True )
        if muxer:
            muxer.close()
        if os.path.exists( path ) and os.path.getsize( path ) == 0:
            os.remove( path )
        log( "Stopped recording {}".format(name) )
        if os.path.exists( path ):
            start_to_mp4( path, len(formats) > 1 )
    outcome["exit_code"] = 0

if __name__ == "__main__":
    run()
