# Recordurbate

Automatically records Chaturbate live streams. Runs as a background daemon that
monitors a list of streamers and records any that go live using yt-dlp and
ffmpeg.

This is a full rewrite of the original
[oliverjrose99/Recordurbate](https://github.com/oliverjrose99/Recordurbate).
The daemon, process management, and IPC layers have been rebuilt from scratch.

## How It Works

The daemon runs in the background (via UNIX double-fork) and loops every 60
seconds over a list of streamers. For each streamer that isn't already being
recorded, it launches a yt-dlp subprocess targeting their Chaturbate page.
yt-dlp handles stream detection and spawns ffmpeg to download the video.

Each yt-dlp process runs in its own session (`setsid`) so it is isolated from
the daemon's signal environment. The daemon monitors each process in a
dedicated thread, validates that both yt-dlp and ffmpeg are alive, and cleans
up orphaned processes when streams end or crash.

Runtime management (add/remove streamers, start/stop, list status) is done via
a TCP IPC interface. The daemon binds to a dynamic port and registers itself
via Zeroconf (mDNS), so the CLI client can discover it without a PID file or
fixed port.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full technical breakdown
(process hierarchy, thread model, signal handling, IPC protocol, etc.).

## Requirements

- **macOS** or **Linux**
- **Python 3.10+**
- **yt-dlp**
- **ffmpeg**
- **Python packages**: `psutil`, `zeroconf`

### macOS

```bash
brew install python ffmpeg
pip3 install yt-dlp psutil zeroconf
```

### Linux

```bash
sudo apt update && sudo apt install python3 ffmpeg
pip3 install yt-dlp psutil zeroconf
```

## Installation

```bash
git clone https://github.com/oliverjrose99/Recordurbate.git
cd Recordurbate
python3 -m venv venv
source venv/bin/activate
pip install yt-dlp psutil zeroconf
```

Streams are saved to `videos/<streamer_name>/` by default. This can be changed
in `configs/youtube-dl.config`.

## Usage

```
python cli.py start                   # start the daemon
python cli.py stop                    # stop the daemon
python cli.py restart                 # restart the daemon

python cli.py add <username>          # add a streamer
python cli.py del <username>          # remove a streamer (stops recording)

python cli.py list                    # list all streamers (* = recording, - = offline)
python cli.py list online             # list currently recording
python cli.py list offline            # list currently offline

python cli.py import <file>           # import streamer names from file (one per line)
python cli.py export [file]           # export streamer names to file

python cli.py help                    # show usage
```

## Configuration

Two config files in the `configs/` directory:

### config.json

| Key                      | Type     | Default                       | Description                                                                 |
|:-------------------------|:---------|:------------------------------|:----------------------------------------------------------------------------|
| `youtube-dl_cmd`         | `string` | `"yt-dlp"`                    | Command to invoke yt-dlp. Can be an absolute path.                          |
| `youtube-dl_config`      | `string` | `"configs/youtube-dl.config"` | Path to yt-dlp config, passed via `--config-location`.                      |
| `auto_reload_config`     | `bool`   | `true`                        | Reload config every loop iteration (60s). Allows live streamer list edits.  |
| `rate_limit`             | `bool`   | `true`                        | Whether to rate-limit between launching streamers.                          |
| `rate_limit_time`        | `number` | `0`                           | Seconds to sleep between streamer launches. `0` = no delay.                 |
| `process_poll_wait_time` | `number` | `15`                          | Seconds to poll (1s intervals) after launching yt-dlp before validation.    |
| `default_export_location`| `string` | `"./list.txt"`                | Default file path for the `export` command.                                 |
| `streamers`              | `array`  | `[]`                          | Chaturbate usernames to record.                                             |

### youtube-dl.config

Passed to yt-dlp via `--config-location`. Controls output format, quality, and
yt-dlp behavior. Default settings:

```
-o "videos/%(id)s/%(title)s.%(ext)s"
--quiet
--hls-use-mpegts
--cookies ./chaturbate.com_cookies.txt
```

Quality can be limited to reduce file size and bandwidth:

```
-f 'best[height<1080][fps<?60]' -o "videos/%(id)s/%(title)s.%(ext)s"
```

See the [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp#usage-and-options)
for all available options.

## Monitoring

yt-dlp processes run in their own sessions and won't appear in `ps -u`. Use:

```bash
# all yt-dlp processes
ps aux | grep yt-dlp

# detailed: PID, parent PID, status, uptime, command
ps -eo pid,ppid,stat,etime,command | grep yt-dlp

# check for zombies
ps aux | awk '$8 ~ /Z/'
```

Daemon log: `configs/rb.log`

## macOS launchd

To run as a user agent that starts at login:

```bash
cp launchd.plist ~/Library/LaunchAgents/com.recordurbate.daemon.plist
launchctl load ~/Library/LaunchAgents/com.recordurbate.daemon.plist
```

To remove:

```bash
launchctl unload ~/Library/LaunchAgents/com.recordurbate.daemon.plist
rm ~/Library/LaunchAgents/com.recordurbate.daemon.plist
```

## Notes

### Large files and bandwidth

Streams are captured at the source quality (up to 4K/60fps). This produces
large files and heavy bandwidth usage. Use yt-dlp config options to limit
quality if needed.

### Recordings lag or freeze

Usually caused by outdated yt-dlp or ffmpeg. Update both:

```bash
pip3 install -U yt-dlp
brew upgrade ffmpeg    # macOS
```

### No files appearing

Check that yt-dlp, ffmpeg, and cookies are configured correctly. Check
`configs/rb.log` for errors. Ensure the `videos/` directory is writable.
