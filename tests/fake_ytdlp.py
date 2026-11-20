#!/usr/bin/env python3
"""Stands in for yt-dlp --dump-single-json in the recorder tests.

FAKE_HLS_BASE names the local test server. FAKE_YTDLP_MODE picks the outcome:
ok (default), offline, neterror, garbage, hang, or fail-after-first, which
answers ok once and neterror afterwards, counting calls in FAKE_YTDLP_COUNTER.
FAKE_YTDLP_GATE names a FIFO; the outcome waits until a test opens and closes
its write end, so the test decides when yt-dlp answers.
Like yt-dlp, it rewrites the file named by --cookies when it exits.
FAKE_YTDLP_COOKIES_LOG names a file that receives the --cookies path on its
first line (empty when none was given) followed by that file's contents.
"""
import atexit
import json
import os
import sys
import time

args = sys.argv[1:]
cookies = args[args.index("--cookies") + 1] if "--cookies" in args else None
cookies_log = os.environ.get("FAKE_YTDLP_COOKIES_LOG")
if cookies_log:
    with open( cookies_log, "w" ) as f:
        f.write( (cookies or "") + "\n" )
        if cookies:
            with open( cookies ) as c:
                f.write( c.read() )
if cookies:
    def save_cookies():
        with open( cookies, "w" ) as f:
            f.write( "# Netscape HTTP Cookie File\n# rewritten by fake_ytdlp\n" )
    atexit.register( save_cookies )

mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
gate = os.environ.get("FAKE_YTDLP_GATE")
if gate:
    with open( gate ) as f:
        f.read()
if mode == "fail-after-first":
    counter = os.environ["FAKE_YTDLP_COUNTER"]
    calls = int( open(counter).read() ) if os.path.exists( counter ) else 0
    with open( counter, "w" ) as f:
        f.write( str(calls + 1) )
    mode = "ok" if calls == 0 else "neterror"

if mode == "offline":
    print( "ERROR: [Chaturbate] room: Room is currently offline", file=sys.stderr )
    sys.exit( 1 )
if mode == "neterror":
    print( "ERROR: [Chaturbate] room: Unable to download webpage: <urlopen error [Errno 51] Network is unreachable>", file=sys.stderr )
    sys.exit( 1 )
if mode == "garbage":
    print( "not json" )
    sys.exit( 0 )
if mode == "hang":
    time.sleep( 120 )

base = os.environ["FAKE_HLS_BASE"]
print( json.dumps({
    "title": "room 2026-01-01 00_00",
    "requested_formats": [
        {"url": base + "/v/playlist.m3u8"},
        {"url": base + "/a/playlist.m3u8"},
    ],
    "http_headers": {"User-Agent": "recordurbate-test"},
}) )
