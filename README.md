# Recordurbate

Automatically records Chaturbate live streams. Runs as a background daemon that
monitors a list of streamers and records any that go live.

This is a full rewrite of the original
[oliverjrose99/Recordurbate](https://github.com/oliverjrose99/Recordurbate).
The daemon, process management, and IPC layers have been rebuilt from scratch.

## How It Works

The daemon runs in the background (via UNIX double-fork) and loops every 60
seconds over a list of streamers. For each streamer that isn't already being
recorded, it launches `recorder.py` for their Chaturbate page.

The recorder asks yt-dlp for the room's video and audio playlists, downloads
every segment itself with short timeouts, and has a local ffmpeg mux them into
an MPEG-TS file. A segment that cannot be fetched becomes a gap in the same
file rather than ending the recording; unreachable playlists are re-resolved
within seconds.

Each recorder runs in its own session (`setsid`) so it is isolated from the
daemon's signal environment. The daemon monitors each recorder in a dedicated
thread and relaunches it when the stream ends.

Runtime management (add/remove streamers, start/stop, list status) is done via
a TCP IPC interface. The daemon listens on a fixed localhost port, which the
CLI connects to directly; while one daemon holds the port, a second cannot bind
it and exits with "Already started."

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical breakdown
(process hierarchy, thread model, signal handling, IPC protocol, etc.).

## Requirements

- **macOS** or **Linux**
- **Python 3.10+**
- **yt-dlp**
- **ffmpeg**
- **Python packages**: `psutil`

### macOS

```bash
brew install python ffmpeg
```

### Linux

```bash
sudo apt update && sudo apt install python3 ffmpeg
```

## Installation

```bash
git clone https://github.com/oliverjrose99/Recordurbate.git
cd Recordurbate
python3 -m venv venv
source venv/bin/activate
pip install yt-dlp psutil requests
```

Streams are saved to `videos/<streamer_name>/<title>.ts`. MPEG-TS plays while
it is still being written (VLC, mpv, IINA).

## Running Recordurbate

There are two ways to invoke commands: **`service.sh`** (recommended) and
**`cli.py`** (direct). Both accept the same commands — the difference is how
they handle the Python virtual environment.

### service.sh — recommended

`service.sh` activates the venv transparently before dispatching to `cli.py`.
No manual activation step, no risk of running against system Python. It also
provides `enable`/`disable`/`status` commands for macOS launchd integration
(see [macOS launchd](#macos-launchd) below).

```bash
./service.sh start                    # start the daemon
./service.sh stop                     # stop the daemon
./service.sh restart                  # restart the daemon
./service.sh upgrade                  # in-place upgrade (preserves recordings)

./service.sh add <username>           # add a streamer
./service.sh del <username>           # remove a streamer (stops recording)

./service.sh list                     # list all streamers (* = recording, - = offline)
./service.sh list online              # list currently recording
./service.sh list offline             # list currently offline

./service.sh import <file>            # import streamer names from file (one per line)
./service.sh export [file]            # export streamer names to file

./service.sh help                     # show usage
```

### cli.py — direct

`cli.py` is the Python entry point. It imports project dependencies (`psutil`)
directly, so when it is launched with any interpreter other than the
project's `venv/bin/python` it re-execs itself under `venv/bin/python` first. No
activation step is needed:

```bash
./cli.py start
./cli.py add <username>
# etc. — same commands as service.sh
```

`./cli.py enable`, `./cli.py disable` and `./cli.py status` run `service.sh`
with the same subcommand (see [macOS launchd](#macos-launchd) below).

Use `cli.py` directly from any shell, with or without the venv activated, or
when `service.sh` is not available (e.g. on Linux without bash).

### Foreground mode

The `--foreground` flag prevents the daemon from double-forking. The process
stays attached to the terminal instead of detaching into the background. This
is used by launchd (which expects to supervise the process directly) but is
also useful for debugging:

```bash
python cli.py start --foreground
```

### In-place upgrade

The `upgrade` command performs a graceful handoff: the running daemon stops
accepting new streams, hands its child process table to a new daemon instance,
and exits. Active recordings continue uninterrupted across the restart.

```bash
./service.sh upgrade
```

## Configuration

Two config files in the `configs/` directory:

### config.json

| Key                       | Type     | Default                       | Description                                                                |
| :------------------------ | :------- | :---------------------------- | :------------------------------------------------------------------------- |
| `youtube-dl_cmd`          | `string` | `"yt-dlp"`                    | Command `recorder.py` runs to resolve a room. Can be an absolute path.         |
| `youtube-dl_config`       | `string` | `"configs/youtube-dl.config"` | Path to yt-dlp config, passed via `--config-location`.                         |
| `auto_reload_config`      | `bool`   | `true`                        | Reload config every loop iteration (60s). Allows live streamer list edits.     |
| `rate_limit`              | `bool`   | `true`                        | Whether to rate-limit between launching streamers.                             |
| `rate_limit_time`         | `number` | `0`                           | Seconds to sleep between streamer launches. `0` = no delay.                    |
| `process_poll_wait_time`  | `number` | `15`                          | Seconds to poll (1s intervals) after launching recorder.py before validation.  |
| `stale_stream_timeout`    | `number` | `300`                         | Seconds of `.ts`/`.part` inactivity before the backstop kills a recorder.      |
| `default_export_location` | `string` | `"./list.txt"`                | Default file path for the `export` command.                                |
| `streamers`               | `array`  | `[]`                          | Chaturbate usernames to record.                                            |

### youtube-dl.config

Passed to yt-dlp via `--config-location` when `recorder.py` resolves a room.
yt-dlp only resolves the stream; `recorder.py` downloads it and writes
`videos/<streamer_name>/<title>.ts`, so output and downloader options have no
effect. The options that matter:

```
--cookies ./chaturbate.com_cookies.txt
```

Quality can be limited to reduce file size and bandwidth:

```
-f 'bv*[height<1080]+ba'
```

See the [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp#usage-and-options)
for format selection.

## Monitoring

Recorder processes run in their own sessions and won't appear in `ps -u`. Use:

```bash
# all recorders
ps aux | grep recorder.py

# detailed: PID, parent PID, status, uptime, command
ps -eo pid,ppid,stat,etime,command | grep recorder.py

# check for zombies
ps aux | awk '$8 ~ /Z/'
```

Daemon log: `configs/rb.log`

## macOS launchd

`service.sh` can install Recordurbate as a macOS user agent that starts
automatically at login:

```bash
./service.sh enable                   # generate plist, install, and load
./service.sh disable                  # unload and remove
./service.sh status                   # show loaded/running state, PID and last exit
```

When enabled, launchd starts the daemon in foreground mode (`--foreground`) so
it can supervise the process directly — the daemon skips the double-fork and
launchd handles restart-on-crash (throttled to 30s intervals).

Logs:
- stdout: `~/Library/Logs/Recordurbate/launchd.stdout.log`
- stderr: `~/Library/Logs/Recordurbate/launchd.stderr.log`

The generated plist is installed to
`~/Library/LaunchAgents/com.recordurbate.daemon.plist`.

## Notes

### Large files and bandwidth

Streams are captured at the source quality (up to 4K/60fps). This produces
large files and heavy bandwidth usage. Use a `-f` format filter in
`configs/youtube-dl.config` to limit quality if needed.

### Gaps in a recording

When the CDN drops connections, `recorder.py` skips the segments it could not
fetch and keeps writing the same file, so the recording has a gap instead of
ending. Each gap is logged in `configs/capture-<name>.log` as a lost or failed
segment.

### Rooms no longer resolve

yt-dlp resolves each room's playlists. If launches fail with extractor errors,
update it:

```bash
pip3 install -U yt-dlp
```

### No files appearing

Check that yt-dlp, ffmpeg, and cookies are configured correctly. Check
`configs/rb.log` for errors. Ensure the `videos/` directory is writable.

If launches stop reaching the site, `configs/rb.log` logs `No launch has reached
chaturbate.com` with yt-dlp's error. Each streamer's own output is in
`configs/capture-<name>.log`.
