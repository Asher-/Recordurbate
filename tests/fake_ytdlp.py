#!/usr/bin/env python3
"""Stands in for yt-dlp --dump-single-json in the recorder tests.

FAKE_HLS_BASE names the local test server. FAKE_YTDLP_MODE picks the outcome:
ok (default), offline, neterror, garbage, hang, or fail-after-first, which
answers ok once and neterror afterwards, counting calls in FAKE_YTDLP_COUNTER.
FAKE_YTDLP_GATE names a FIFO; the outcome waits until a test opens and closes
its write end, so the test decides when yt-dlp answers.
"""
import json
import os
import sys
import time

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
