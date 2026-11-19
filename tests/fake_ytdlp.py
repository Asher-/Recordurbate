#!/usr/bin/env python3
"""Stands in for yt-dlp --dump-single-json in tests/test_recorder.py.

FAKE_HLS_BASE names the local test server; FAKE_YTDLP_OFFLINE makes it report
an offline room the way yt-dlp does.
"""
import json
import os
import sys

if os.environ.get("FAKE_YTDLP_OFFLINE"):
    print("ERROR: [Chaturbate] room: Room is currently offline", file=sys.stderr)
    sys.exit(1)

base = os.environ["FAKE_HLS_BASE"]
print(json.dumps({
    "title": "room 2026-01-01 00_00",
    "requested_formats": [
        {"url": base + "/v/playlist.m3u8"},
        {"url": base + "/a/playlist.m3u8"},
    ],
    "http_headers": {"User-Agent": "recordurbate-test"},
}))
