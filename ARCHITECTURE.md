# Recordurbate Architecture

## Table of Contents

- [Overview](#overview)
- [File Layout](#file-layout)
- [Process Hierarchy](#process-hierarchy)
- [Thread Model](#thread-model)
- [Module Reference](#module-reference)
  - [cli.py](#clipy)
  - [daemon.py](#daemonpy)
  - [streamer.py](#streamerpy)
  - [pid.py](#pidpy)
  - [config.py](#configpy)
  - [ipc/server.py](#ipcserverpy)
  - [ipc/client.py](#ipcclientpy)
  - [ipc/client\_response.py](#ipcclient_responsepy)
  - [ipc/terminator.py](#ipcterminatorpy)
  - [ipc/discover\_port.py](#ipcdiscover_portpy)
  - [ipc/zeroconf\_config.py](#ipczeroconf_configpy)
- [Daemonization](#daemonization)
- [Streamer Lifecycle](#streamer-lifecycle)
- [Stream Validation](#stream-validation)
- [IPC Protocol](#ipc-protocol)
  - [Service Discovery](#service-discovery)
  - [TCP Socket Protocol](#tcp-socket-protocol)
  - [Command Dispatch](#command-dispatch)
- [Signal Handling](#signal-handling)
  - [Daemon Signals](#daemon-signals)
  - [Child Process Isolation](#child-process-isolation)
- [Configuration Reference](#configuration-reference)
  - [config.json](#configjson)
  - [youtube-dl.config](#youtube-dlconfig)
- [Operational Guide](#operational-guide)
  - [Starting the Daemon](#starting-the-daemon)
  - [Stopping the Daemon](#stopping-the-daemon)
  - [Managing Streamers](#managing-streamers)
  - [Monitoring Processes](#monitoring-processes)
  - [Log Files](#log-files)
  - [launchd Integration](#launchd-integration)
- [Known Behaviors](#known-behaviors)
  - [yt-dlp Internal Deadlocks](#yt-dlp-internal-deadlocks)
  - [Session Isolation and ps Visibility](#session-isolation-and-ps-visibility)
  - [Offline Streamer Cycling](#offline-streamer-cycling)
- [Changes (Feb 22, 2026)](#changes-feb-22-2026)
  - [Zombie / Orphan Process Fixes](#zombie--orphan-process-fixes)
  - [IPC Reliability Fixes](#ipc-reliability-fixes)
  - [Timing Improvements](#timing-improvements)
- [Changes (Feb 25, 2026)](#changes-feb-25-2026)
  - [Stranded Sleeping Process Fixes](#stranded-sleeping-process-fixes)
- [In-Place Upgrade (Handoff)](#in-place-upgrade-handoff)
  - [Upgrade Lifecycle](#upgrade-lifecycle)
  - [AdoptedProcess](#adoptedprocess)
  - [New IPC Command](#new-ipc-command)
  - [New CLI Command](#new-cli-command)
  - [Files Changed](#files-changed)
- [Stale Stream Watchdog](#stale-stream-watchdog)
  - [Detection Mechanism](#detection-mechanism)
  - [Configuration](#configuration-1)
  - [Defence in Depth](#defence-in-depth)

---

## Overview

Recordurbate is a daemon that monitors a list of Chaturbate streamers and
automatically records their live streams using yt-dlp and ffmpeg. It runs as a
background process (via UNIX double-fork daemonization) and exposes an IPC
interface over TCP for runtime management (add/remove streamers, start/stop,
list status).

The daemon spawns one yt-dlp subprocess per online streamer. Each yt-dlp
process in turn spawns an ffmpeg child to handle the actual stream download.
The daemon monitors these process trees, validates stream health, and cleans up
orphaned processes when streams end or crash.

Service discovery uses Zeroconf (mDNS) so the CLI client can find the daemon's
dynamically-assigned TCP port without a PID file or fixed port.

---

## File Layout

```
Recordurbate/
├── cli.py                      # CLI entry point, argument parsing, IPC client
├── daemon.py                   # Daemon class: fork, run loop, streamer management
├── streamer.py                 # Streamer class: yt-dlp lifecycle, validation, cleanup
├── pid.py                      # Process utilities (active check, find by name/args)
├── config.py                   # Config load/save from JSON
├── ipc/
│   ├── __init__.py
│   ├── server.py               # TCP socket server (daemon side)
│   ├── client.py               # TCP socket client (CLI side)
│   ├── client_response.py      # Response writer for server→client communication
│   ├── terminator.py           # End-of-message sentinel (0xDEADBEEF)
│   ├── discover_port.py        # Zeroconf service browser for port discovery
│   └── zeroconf_config.py      # Shared Zeroconf service type/name constants
├── configs/
│   ├── config.json             # Main configuration (streamers list, options)
│   ├── youtube-dl.config       # yt-dlp command-line options
│   └── rb.log                  # Daemon log file
├── videos/                     # Recorded stream output directory
├── chaturbate.com_cookies.txt  # Browser cookies for authenticated access
├── launchd.plist               # macOS launchd service definition
└── venv/                       # Python virtual environment
```

---

## Process Hierarchy

When the daemon is running and recording a streamer, the process tree looks
like this:

```
launchd (PID 1)
└── python cli.py start          (daemon, double-forked, PPID=1)
    │
    │   [threads inside daemon process]
    │   ├── main thread           (waiting for run loop thread to finish)
    │   ├── run loop thread       (non-daemon, iterates streamers every 60s)
    │   ├── IPC server thread     (daemon thread, accept loop)
    │   ├── IPC handler threads   (daemon threads, one per active connection)
    │   └── streamer threads      (daemon threads, one per streamer, manage yt-dlp)
    │
    ├── yt-dlp <streamer_a>       (own session, PGID=own PID, Ss)
    │   └── ffmpeg                (child of yt-dlp, auto-reaped via SIGCHLD=SIG_IGN)
    ├── yt-dlp <streamer_b>
    │   └── ffmpeg
    └── ...
```

Key relationships:

- **Daemon → yt-dlp**: Parent-child. The daemon spawns yt-dlp via
  `subprocess.Popen`. Each yt-dlp runs in its own session
  (`start_new_session=True`), making it a session leader (stat `Ss`).
- **yt-dlp → ffmpeg**: Parent-child. yt-dlp spawns ffmpeg internally to handle
  stream download. The `preexec_fn` sets `SIGCHLD=SIG_IGN` in the yt-dlp
  process, so the kernel auto-reaps ffmpeg when it exits (no zombie).
- **Daemon threads → yt-dlp**: Each streamer thread holds a `Popen` reference
  to its yt-dlp process and is responsible for reaping it via `wait()`.

---

## Thread Model

The daemon process contains several categories of threads:

| Thread              | Daemon? | Count         | Purpose                                                     |
|:--------------------|:--------|:--------------|:------------------------------------------------------------|
| Main thread         | —       | 1             | Waits for run loop thread (keeps process alive)             |
| Run loop thread     | No      | 1             | Iterates streamers, launches missing streams, sleeps 60s    |
| IPC accept thread   | Yes     | 1             | Accepts TCP connections, dispatches to handler threads       |
| IPC handler threads | Yes     | 0–N           | One per active CLI connection, processes a single command    |
| Streamer threads    | Yes     | 0–N           | One per streamer, manages yt-dlp lifecycle for that streamer |

**Non-daemon** threads prevent the process from exiting. The run loop thread is
non-daemon, so the process stays alive as long as the run loop is active. All
other threads are daemon threads — they are killed when the last non-daemon
thread exits.

**Concurrency note**: There is no mutex protecting `self.streamers` or
individual `Streamer` fields. The run loop thread reads `streamer.stream` to
decide whether to call `start()`, while the streamer thread writes to
`streamer.stream` and `streamer.started`. This is safe in practice because:

- Only one streamer thread exists per streamer at a time.
- The run loop only checks `streamer.stream` (truthiness) and calls `start()`
  if it's `None`.
- The streamer thread sets `self.stream` at the beginning and clears it at the
  end.

---

## Module Reference

### cli.py

Entry point. Invoked as `python cli.py <command> [args]`.

**Flow for `start`**:

1. Attempts to register a Zeroconf service to check if the daemon is already
   running. If registration fails with `NonUniqueNameException`, the daemon is
   already running — exits with error.
2. Unregisters the temporary service.
3. Creates a `Daemon` instance and calls `daemon.start()`.
4. The parent and intermediate fork processes exit via `sys.exit(0)` inside
   `daemonize()`. Only the final child continues.

**Flow for all other commands** (`stop`, `add`, `del`, `list`, etc.):

1. Discovers the daemon's TCP port via Zeroconf (`DiscoverPort`).
2. Opens a `SocketClient` connection to the daemon.
3. Sends the command string over TCP.
4. Reads and prints the response until the terminator sentinel is received.

**Global state**: `socket_client` is lazily initialized on the first IPC call
and reused for the lifetime of the CLI process.

---

### daemon.py

The `Daemon` class manages the entire daemon lifecycle.

**`__init__`**: Initializes logger, loads config, creates `Streamer` objects
for each name in the config, registers SIGINT/SIGTERM handlers.

**`daemonize()`**: Classic UNIX double-fork:

1. Creates the `SocketServer` (but does not start it yet).
2. First `os.fork()` — parent exits, child continues.
3. Second `os.fork()` — intermediate child exits, grandchild continues.
4. Grandchild is the daemon: detached from terminal, PPID becomes 1.
5. Starts the IPC socket server.
6. Closes stdin/stdout/stderr to complete daemonization.

**`run()`**: Starts the run loop in a non-daemon thread:

```
while self.pid:
    reload_config()                    # pick up config changes
    for each streamer:
        if not streaming: start()      # launch yt-dlp
        rate_limit_sleep()
    sleep 60s (in 1s intervals, checking self.pid)
```

**`stop()`**: Sets `self.pid = None`, which causes the run loop to exit. The
run loop's cleanup phase calls `streamer.stop()` on all streamers. Then
`sys.exit(0)`.

**`reload_config()`**: Reloads `config.json` every loop iteration (when
`auto_reload_config` is true). Preserves existing `Streamer` objects for names
that still exist, creates new ones for additions, and drops removed ones.

**`ipc()`**: Command dispatcher. Parses the command string and routes to the
appropriate method (`stop`, `add_streamer`, `delete_streamer`,
`list_streamers`, etc.).

---

### streamer.py

The `Streamer` class manages a single streamer's yt-dlp process.

**Fields**:

| Field     | Type            | Description                                        |
|:----------|:----------------|:---------------------------------------------------|
| `name`    | `str`           | Chaturbate username                                |
| `daemon`  | `Daemon`        | Back-reference to parent daemon                    |
| `stream`  | `Popen or None` | Active yt-dlp subprocess, or `None` if not running |
| `started` | `bool`          | `True` if stream validated and actively recording  |

**`start()`**: Spawns a daemon thread (`stream_thread`) that:

1. Launches yt-dlp via `subprocess.Popen` with:
   - `start_new_session=True` — isolates child in its own session/process
     group so it doesn't receive signals meant for the daemon.
   - `preexec_fn=ignore_sigchld` — sets `SIGCHLD=SIG_IGN` in the child so
     ffmpeg (yt-dlp's child) is auto-reaped by the kernel.
   - `stdout=DEVNULL, stderr=DEVNULL` — suppresses output.
2. Polls every 1s for up to `process_poll_wait_time` (default 15s) to detect
   early exit (e.g., streamer offline). Breaks immediately if the process
   exits.
3. If the process is still alive, calls `ensure_valid_stream()` to verify both
   yt-dlp and ffmpeg are running.
4. If valid, sets `started = True` and blocks on `self.stream.wait()` until the
   stream ends.
5. On exit, calls `cleanup_ffmpeg()` to kill any orphaned ffmpeg, then
   `stop(signal_child=False)`.

**`ensure_valid_stream()`**: Validates that a healthy stream exists:

- If yt-dlp is alive and ffmpeg is found → `True` (valid stream).
- If yt-dlp is alive but no ffmpeg → waits 5s and retries (up to 5 times, 25s
  max). ffmpeg may not have started yet.
- If yt-dlp is dead but ffmpeg is alive → kills orphaned ffmpeg.
- If both are dead → `False`.

Uses `pid.process_active()` to check yt-dlp and
`pid.get_pid_by_name_and_args()` to find ffmpeg by matching
`/<streamer_name>/` in its command-line arguments.

**`cleanup_ffmpeg()`**: Called after `wait()` returns (yt-dlp exited). Finds
any ffmpeg process whose command line contains `/<streamer_name>/` and sends
`SIGTERM`.

**`stop(signal_child)`**:

- If `signal_child=True` and `started`: sends `SIGINT` to yt-dlp, then
  `wait()` for graceful shutdown.
- Regardless: checks `poll()` — if the child is still alive, sends `SIGTERM`
  via `terminate()` and `wait()`. This is the safety net for hung processes.
- Sets `self.stream = None`.

---

### pid.py

Process utility functions using `psutil`.

**`process_active(pid)`**: Returns `True` if the PID exists and is not in
`STATUS_DEAD` or `STATUS_ZOMBIE` state.

**`get_pid_by_name_and_args(process_name, args_substring, exe_path)`**: Iterates
all system processes to find one matching by name (case-insensitive), optionally
filtering by a substring in the command-line arguments and/or executable path.
Returns the first matching PID or `None`. Used to find ffmpeg processes
associated with a specific streamer.

---

### config.py

Simple JSON config loader/saver.

- **`load_config()`** — reads `configs/config.json`, returns dict. Exits on
  error.
- **`save_config(config)`** — writes dict to `configs/config.json` with
  4-space indent. Exits on error.
- **`find_in_config(username, config)`** — returns the index of `username` in
  `config["streamers"]`, or `None`.

---

### ipc/server.py

`SocketServer` — the daemon-side TCP server for IPC.

**Lifecycle**:

1. `__init__` — resolves hostname/IP, stores daemon reference.
2. `start()` — creates a TCP socket with `SO_REUSEADDR`, binds to an
   OS-assigned port (port 0), sets a 2s accept timeout, registers the service
   via Zeroconf, and starts the accept loop.
3. `loop()` — runs in a daemon thread. Calls `accept()` in a loop (with 2s
   timeout so it can check `self.daemon.pid` for shutdown). Each accepted
   connection is dispatched to `handle_connection()` in its own daemon thread.
4. `handle_connection()` — sets a 10s recv timeout on the client socket, reads
   the command, creates a `ClientResponse`, dispatches to `daemon.ipc()`.
   Uses `try/finally` to guarantee `client_response.close()` (sends
   terminator) and `connected_socket.close()` always execute, even if the
   command handler throws.

**Error handling**: Catches `socket.timeout`, `ConnectionResetError`, and all
other exceptions per-connection. A failing command never kills the accept loop.

---

### ipc/client.py

`SocketClient` — the CLI-side TCP client.

**`start()`**: Connects to the daemon with a 30s timeout.

**`ipc(command_string)`**: Sends the command, then enters a read loop:

- Uses `select.select()` with a 30s timeout to wait for data.
- If timeout expires, prints "Timed out waiting for response." and exits.
- If `recv()` returns empty bytes (server closed connection), exits.
- If the terminator sentinel (`0xDEADBEEF`) is found at the end of received
  data, strips it and exits after printing any remaining data.
- Catches `ValueError`/`OSError` from `select` for connection loss.

---

### ipc/client\_response.py

`ClientResponse` — wraps a connected socket for server→client communication.

- **`print(string)`** — encodes and sends a string (with trailing newline).
- **`close()`** — sends the terminator bytes (`0xDEADBEEF`), signaling end of
  response.

---

### ipc/terminator.py

`Terminator` — defines the end-of-message sentinel.

```python
bytes      = b'\xDE\xAD\xBE\xEF'   # 4-byte sentinel
byte_array = bytearray(bytes)        # for comparison
length     = 4                        # for slicing
```

The terminator is appended to every server response. The client scans the tail
of each `recv()` for this pattern to know when the response is complete.

---

### ipc/discover\_port.py

`DiscoverPort` — Zeroconf service browser.

Uses `ServiceBrowser` to listen for `_recordurbate._tcp.local.` services. When
a matching service is found, stores its port in `self.port`. The CLI polls
`self.port` in a loop (up to `port_timeout` seconds, default 10) until
discovery succeeds.

---

### ipc/zeroconf\_config.py

`ZeroconfConfig` — shared constants for Zeroconf registration/discovery.

```python
service_type = "_recordurbate._tcp.local."
service_name = "Recordurbate"
```

Both the server (registration) and client (discovery) reference these to ensure
they agree on the service identifier.

---

## Daemonization

The daemon uses the classic UNIX double-fork pattern:

```
cli.py start
  │
  ├── os.fork() ──► Parent (original process): sys.exit(0)
  │
  └── Child 1
        │
        ├── os.fork() ──► Child 1: sys.exit(0)
        │
        └── Child 2 (the daemon)
              │
              ├── self.pid = os.getpid()
              ├── ipc_client.start()        # start TCP server
              ├── close stdin/stdout/stderr
              └── return to start() → run()
```

The double fork ensures the daemon:

- Is not a session leader (can't accidentally acquire a controlling terminal).
- Has PPID=1 (adopted by init/launchd).
- Has no controlling terminal.

After daemonization, `start()` calls `run()`, which starts the non-daemon run
loop thread. The main thread then falls off the end of `cli.py` and enters
Python's shutdown sequence, where it waits for the non-daemon run loop thread
to finish (which only happens when `self.pid` is set to `None`).

---

## Streamer Lifecycle

```
                    ┌─────────────────────────────────────────────┐
                    │              Run Loop (60s cycle)            │
                    │                                             │
                    │  for each streamer:                         │
                    │      if streamer.stream is None:            │
                    │          streamer.start()                   │
                    └──────────────┬──────────────────────────────┘
                                   │
                           streamer.start()
                                   │
                    ┌──────────────▼──────────────────────────────┐
                    │          stream_thread (daemon thread)       │
                    │                                             │
                    │  1. Popen(yt-dlp, start_new_session=True)   │
                    │  2. Poll every 1s up to poll_wait_time      │
                    │     └─ break early if process exits         │
                    │  3. If still alive:                         │
                    │     └─ ensure_valid_stream()                │
                    │        ├─ yt-dlp + ffmpeg alive → True      │
                    │        ├─ yt-dlp alive, no ffmpeg → retry   │
                    │        ├─ yt-dlp dead, ffmpeg alive → kill  │
                    │        └─ both dead → False                 │
                    │  4. If valid:                               │
                    │     ├─ started = True                       │
                    │     ├─ stream.wait() (blocks until end)     │
                    │     └─ cleanup_ffmpeg()                     │
                    │  5. stop(signal_child=False)                │
                    │     ├─ If child still alive: terminate+wait │
                    │     └─ stream = None                        │
                    └─────────────────────────────────────────────┘
                                   │
                           (thread exits)
                                   │
                    ┌──────────────▼──────────────────────────────┐
                    │  Next run loop iteration sees               │
                    │  streamer.stream is None → start() again    │
                    └─────────────────────────────────────────────┘
```

**Timing breakdown** (worst case for a hung yt-dlp):

| Phase                        | Duration |
|:-----------------------------|:---------|
| Initial poll loop            | ≤15s     |
| ensure_valid_stream retries  | ≤25s     |
| terminate() + wait()         | immediate (SIGTERM) |
| **Total**                    | **≤40s** |

For offline streamers (yt-dlp exits in ~2s), the poll loop detects exit within
1s, and the streamer recycles in ~3s.

---

## Stream Validation

`ensure_valid_stream` verifies that a recording is healthy by checking for two
processes:

1. **yt-dlp** — identified by its PID (from `Popen.pid`), checked via
   `process_active()`.
2. **ffmpeg** — identified by scanning all system processes for one named
   `ffmpeg` whose command-line arguments contain `/<streamer_name>/`.

The ffmpeg detection relies on yt-dlp's output path containing the streamer
name. This comes from the youtube-dl.config output template:
`-o "videos/%(id)s/%(title)s.%(ext)s"` — the `%(id)s` component is the
streamer's Chaturbate username.

---

## IPC Protocol

### Service Discovery

The daemon binds to port 0 (OS-assigned) and registers itself via Zeroconf
(mDNS) under:

```
Service type: _recordurbate._tcp.local.
Service name: Recordurbate._recordurbate._tcp.local.
```

The CLI discovers the port by creating a `ServiceBrowser` that listens for this
service type. Once found, the port is extracted from the `ServiceInfo`.

This avoids the need for a PID file or hardcoded port. The Zeroconf
registration also serves as a singleton check — if the service name is already
registered, `NonUniqueNameException` is raised, preventing duplicate daemons.

### TCP Socket Protocol

The protocol is simple request-response over a single TCP connection:

1. **Client → Server**: Raw UTF-8 command string (≤512 bytes). Examples:
   `"stop"`, `"add username"`, `"list online"`.
2. **Server → Client**: Zero or more UTF-8 string chunks (each ≤512 bytes),
   terminated by the 4-byte sentinel `0xDEADBEEF`.

The terminator allows variable-length responses. The client reads until it sees
the sentinel at the end of a `recv()` chunk.

**Timeouts**:

| Component        | Timeout | Purpose                                      |
|:-----------------|:--------|:---------------------------------------------|
| Server accept    | 2s      | Allows shutdown check every 2s               |
| Server recv      | 10s     | Prevents misbehaving client from freezing    |
| Client connect   | 30s     | Prevents hang if daemon is dead              |
| Client select    | 30s     | Prevents hang if server dies mid-response    |
| Port discovery   | 10s     | Maximum wait for Zeroconf service to appear  |

### Command Dispatch

Commands are space-delimited strings. The first token is the command name:

| Command              | Handler                     | Description                    |
|:---------------------|:----------------------------|:-------------------------------|
| `stop`               | `daemon.stop()`             | Stop the daemon                |
| `restart`            | `daemon.restart()`          | Stop and restart               |
| `add <name>`         | `daemon.add_streamer()`     | Add a streamer to the list     |
| `del <name>`         | `daemon.delete_streamer()`  | Remove a streamer              |
| `list`               | `daemon.list_streamers()`   | List all streamers             |
| `list online`        | `daemon.list_streamers_online()`  | List currently recording |
| `list offline`       | `daemon.list_streamers_offline()` | List currently offline   |
| `handoff`            | `daemon.handoff()`          | Exit without killing children  |
| `import <path>`      | `daemon.import_streamers()` | Import streamers from file     |
| `export <path>`      | `daemon.export_streamers()` | Export streamers to file       |

Each handler receives a `ClientResponse` object for sending output back to the
CLI.

---

## Signal Handling

### Daemon Signals

The daemon registers handlers for:

- **SIGINT** — calls `daemon.stop()` (graceful shutdown).
- **SIGTERM** — calls `daemon.stop()` (graceful shutdown).

`stop()` sets `self.pid = None`, causing the run loop to exit. The run loop's
cleanup phase iterates all streamers and calls `streamer.stop()` with
`signal_child=True`, which sends SIGINT to each yt-dlp and waits for it to
exit.

### Child Process Isolation

Each yt-dlp child is isolated from the daemon's signal environment:

- **`start_new_session=True`** — creates a new session and process group. The
  child will not receive signals sent to the daemon's process group (e.g.,
  terminal SIGINT from Ctrl+C). The daemon can still send signals to specific
  PIDs via `Popen.send_signal()`.

- **`preexec_fn=ignore_sigchld`** — sets `SIGCHLD=SIG_IGN` in the child
  process before exec. This survives exec, so yt-dlp starts with SIGCHLD
  ignored. The effect: when ffmpeg (yt-dlp's child) exits, the kernel
  automatically reaps it instead of leaving a zombie. This is critical because
  yt-dlp may not explicitly wait on ffmpeg in all code paths.

- **SIGINT and SIGTERM are NOT ignored** — yt-dlp starts with default handling
  for these signals. The daemon can gracefully stop a recording by sending
  SIGINT (which yt-dlp handles as KeyboardInterrupt). If yt-dlp doesn't
  respond, `stop()` escalates to SIGTERM via `Popen.terminate()`.

---

## Configuration Reference

### config.json

Location: `configs/config.json`

| Key                      | Type     | Default                  | Description                                                                    |
|:-------------------------|:---------|:-------------------------|:-------------------------------------------------------------------------------|
| `youtube-dl_cmd`         | `string` | `"yt-dlp"`              | Command to invoke yt-dlp. Can be an absolute path.                             |
| `youtube-dl_config`      | `string` | `"configs/youtube-dl.config"` | Path to yt-dlp config file, passed via `--config-location`.             |
| `auto_reload_config`     | `bool`   | `true`                   | If true, config is reloaded every loop iteration (60s). Allows live changes.   |
| `rate_limit`             | `bool`   | `true`                   | Whether to rate-limit API calls (controls if `rate_limit_time` is used).       |
| `rate_limit_time`        | `number` | `0`                      | Seconds to sleep between launching streamers. `0` means no delay.              |
| `process_poll_wait_time` | `number` | `15`                     | Seconds to wait (polling every 1s) after launching yt-dlp before validation.   |
| `default_export_location`| `string` | `"./list.txt"`           | Default file path for the `export` command.                                    |
| `streamers`              | `array`  | `[]`                     | Array of Chaturbate usernames to record.                                       |

### youtube-dl.config

Location: `configs/youtube-dl.config`

Passed to yt-dlp via `--config-location`. Current settings:

| Option              | Value                                    | Description                              |
|:--------------------|:-----------------------------------------|:-----------------------------------------|
| `-o`                | `"videos/%(id)s/%(title)s.%(ext)s"`      | Output path template                     |
| `--quiet`           | —                                        | Suppress yt-dlp console output           |
| `--hls-use-mpegts`  | —                                        | Use MPEG-TS for HLS streams (resumable)  |
| `--cookies`         | `./chaturbate.com_cookies.txt`           | Browser cookies for authentication       |

The output template places recordings at `videos/<streamer_name>/`. This path
pattern is also used by `ensure_valid_stream()` to identify ffmpeg processes
belonging to a specific streamer.

---

## Operational Guide

### Starting the Daemon

```bash
python cli.py start
```

The process double-forks and returns immediately. The daemon runs in the
background. Check the log to confirm:

```bash
tail -f configs/rb.log
```

### Stopping the Daemon

```bash
python cli.py stop
```

Sends the `stop` command via IPC. The daemon gracefully shuts down: signals all
yt-dlp processes with SIGINT, waits for them to exit, then exits itself.

### Managing Streamers

```bash
python cli.py add <username>        # Add a streamer
python cli.py del <username>        # Remove a streamer (stops recording)
python cli.py list                  # List all streamers (* = recording, - = offline)
python cli.py list online           # List currently recording
python cli.py list offline          # List currently offline
python cli.py import <file>         # Import streamer names from file (one per line)
python cli.py export [file]         # Export streamer names to file
```

Changes made via `add`/`del`/`import` are persisted to `configs/config.json`
immediately.

### Monitoring Processes

Since yt-dlp processes run in their own sessions, they don't appear in `ps -u`.
Use:

```bash
# All yt-dlp processes
ps aux | grep yt-dlp

# Detailed view with PID, PPID, status, and command
ps -eo pid,ppid,stat,etime,command | grep yt-dlp

# Check for zombies (any process, not just yt-dlp)
ps aux | awk '$8 ~ /Z/'

# Process tree for the daemon
ps -eo pid,ppid,stat,command | grep -E 'recordurbate|yt-dlp|ffmpeg'

# Sample a hung process (macOS) for stack trace
sample <PID> 1
```

**Process status codes** (BSD `STAT` column):

| Code | Meaning                                         |
|:-----|:------------------------------------------------|
| `S`  | Interruptible sleep (waiting for I/O or event)  |
| `Ss` | Sleeping, session leader                        |
| `S+` | Sleeping, foreground process group              |
| `R`  | Running                                         |
| `Z`  | Zombie (exited but not reaped)                  |
| `T`  | Stopped (e.g., by SIGSTOP)                      |

### Log Files

| File                      | Contents                                     |
|:--------------------------|:---------------------------------------------|
| `configs/rb.log`          | Daemon log (start/stop, stream events, errors)|
| `recordurbate.log`        | stdout from launchd (if using plist)          |
| `recordurbate.error.log`  | stderr from launchd (if using plist)          |

Log format: `[timestamp filename:line - function()] - message`

### launchd Integration

The included `launchd.plist` runs the daemon as a macOS user agent:

```bash
# Install (run once)
cp launchd.plist ~/Library/LaunchAgents/com.recordurbate.daemon.plist
launchctl load ~/Library/LaunchAgents/com.recordurbate.daemon.plist

# Uninstall
launchctl unload ~/Library/LaunchAgents/com.recordurbate.daemon.plist
rm ~/Library/LaunchAgents/com.recordurbate.daemon.plist
```

The plist:

- Runs at load (`RunAtLoad: true`).
- Does not auto-restart (`KeepAlive: false`) — the daemon manages its own
  lifecycle via double-fork.
- Throttles restarts to 30s minimum (`ThrottleInterval: 30`).
- Removes stale PID files before starting.

---

## Known Behaviors

### yt-dlp Internal Deadlocks

yt-dlp occasionally deadlocks on internal Python threading locks (observed via
`sample` showing the main thread blocked on `lock_PyThread_acquire_lock` →
`_pthread_cond_wait`). This is a yt-dlp bug, not a Recordurbate bug.

When this happens:

1. yt-dlp appears alive (status `Ss`) but is not progressing.
2. No ffmpeg child is spawned (or the existing one finishes).
3. `ensure_valid_stream()` retries for up to 25s, fails to find ffmpeg, returns
   `False`.
4. `stop(signal_child=False)` detects the child is still alive via `poll()`,
   sends `SIGTERM` via `terminate()`, and `wait()` reaps it.
5. Total cleanup time: ≤40s from yt-dlp launch.

### Session Isolation and ps Visibility

yt-dlp processes run in their own sessions (`start_new_session=True`), which
means:

- They have **no controlling terminal**.
- They do **not** appear in `ps -u` (which filters by terminal).
- They **do** appear in `ps aux`, `ps -A`, and `ps -eo ...`.
- Their `STAT` column shows `Ss` (sleeping, session leader).

This isolation prevents yt-dlp from receiving terminal signals (Ctrl+C) that
are meant for the daemon. The daemon can still send signals to specific yt-dlp
PIDs.

### Offline Streamer Cycling

When a streamer is offline, yt-dlp exits within ~2 seconds. The polling loop
detects this within 1 additional second. The streamer thread cleans up and sets
`stream = None`. On the next run loop iteration (up to 60s later), the daemon
re-launches yt-dlp for that streamer.

With 96+ streamers and `rate_limit_time: 0`, all offline streamers are cycled
rapidly. Each cycle creates a short-lived yt-dlp process that exits
immediately. This is normal and expected.

---

## Changes (Feb 22, 2026)

### Zombie / Orphan Process Fixes

**Problem**: Processes were accumulating as unkillable sleeping orphans (status
`S`/`Ss`) and occasional zombies (status `Z`).

**Root causes and fixes**:

| #   | Root Cause                                                        | Fix                                                                                              |
|:----|:------------------------------------------------------------------|:-------------------------------------------------------------------------------------------------|
| 1   | SIGCHLD handler blocked for up to 100s (`ensure_valid_stream`     | Removed SIGCHLD handler entirely. Each streamer thread reaps its own child via `Popen.wait()`.   |
|     | with `time.sleep(10)` × 10 retries inside the signal handler).    |                                                                                                  |
|     | During this sleep, other dead children couldn't be reaped.        |                                                                                                  |
| 2   | Dual-reaping race between SIGCHLD handler (`os.waitpid(-1)`) and  | Eliminated by removing the SIGCHLD handler. Only `Popen.wait()`/`poll()` reap now.               |
|     | `Popen.wait()` in streamer threads. No locking.                   |                                                                                                  |
| 3   | `stop(signal_child=False)` dropped the `Popen` reference without  | `stop()` now checks `poll()` and calls `terminate()` + `wait()` if the child is still alive      |
|     | reaping. If the child was still alive or hadn't been reaped,      | before setting `self.stream = None`.                                                             |
|     | it became a zombie or orphan.                                     |                                                                                                  |
| 4   | `preexec_fn` set `SIGINT=SIG_IGN` and `SIGTERM=SIG_IGN` in the   | Replaced with `start_new_session=True` for signal isolation. `preexec_fn` now only sets          |
|     | child. `SIG_IGN` survives `exec`, so yt-dlp started with these    | `SIGCHLD=SIG_IGN` (for auto-reaping ffmpeg). yt-dlp now responds to SIGINT/SIGTERM.              |
|     | signals permanently ignored — unkillable.                         |                                                                                                  |

**Files changed**: `daemon.py`, `streamer.py`

**Removed from `daemon.py`**:

- `register_child_signal()` — the SIGCHLD handler.
- `clear_child()` — crash cleanup called from SIGCHLD handler.
- `import errno` — only used in the SIGCHLD handler.
- The call to `self.register_child_signal()` in `daemonize()`.

**Added to `streamer.py`**:

- `cleanup_ffmpeg()` — kills orphaned ffmpeg after `wait()` returns (replaces
  the crash handling that was in the SIGCHLD handler's `clear_child`).
- Safety net in `stop()` — `poll()` + `terminate()` + `wait()` before dropping
  the `Popen` reference.
- `start_new_session=True` on `Popen` — session isolation for yt-dlp.
- `preexec_fn` reduced to only `SIGCHLD=SIG_IGN`.

### IPC Reliability Fixes

**Problem**: The TCP IPC connection would hang and stop responding, requiring a
full daemon restart.

**Root causes and fixes**:

| #   | Root Cause                                                        | Fix                                                                                              |
|:----|:------------------------------------------------------------------|:-------------------------------------------------------------------------------------------------|
| 1   | Server loop was single-threaded: `accept()` → `recv()` →         | Each connection is now dispatched to its own daemon thread via `handle_connection()`.             |
|     | `dispatch()` → `close()` all in sequence. A slow command          | The accept loop only accepts and dispatches.                                                     |
|     | blocked all other connections.                                    |                                                                                                  |
| 2   | Only `ConnectionResetError` was caught. Any other exception       | All exceptions are caught per-connection. Logged but never kill the accept loop.                  |
|     | killed the loop thread silently. The socket stayed open but       |                                                                                                  |
|     | nobody called `accept()` — clients connected but hung forever.    |                                                                                                  |
| 3   | `client_response.close()` (terminator) was skipped if the         | `try/finally` guarantees `client_response.close()` runs even if the command handler throws.      |
|     | command handler threw. The client hung waiting for the terminator. | Outer `finally` guarantees `connected_socket.close()`.                                           |
| 4   | No timeouts anywhere. `accept()`, `recv()`, `sendall()` all       | Accept: 2s timeout (allows shutdown check). Recv: 10s timeout (server). Connect: 30s,            |
|     | blocked indefinitely.                                             | Select: 30s (client).                                                                            |

**Files changed**: `ipc/server.py`, `ipc/client.py`

**Server (`ipc/server.py`)**:

- Added `SO_REUSEADDR` to avoid "address already in use" after restart.
- Added 2s `settimeout` on the listening socket.
- Extracted `handle_connection()` method with full error handling and
  `try/finally` cleanup.
- Accept loop catches `socket.timeout` (continues) and all other exceptions
  (logs and continues).
- Each connection is handled in its own daemon thread.

**Client (`ipc/client.py`)**:

- Added 30s `settimeout` on the socket.
- Added 30s timeout to `select.select()`.
- Empty `recv()` (server closed connection) now breaks the loop.
- Catches `ValueError`/`OSError` on `select` for connection loss.

### Timing Improvements

**Problem**: Cleanup of hung or offline yt-dlp processes took up to 145 seconds.

| Parameter                      | Before | After | Location                  |
|:-------------------------------|:-------|:------|:--------------------------|
| `process_poll_wait_time`       | 45s    | 15s   | `configs/config.json`     |
| `ensure_valid_stream` retries  | 10     | 5     | `streamer.py` (hardcoded) |
| `ensure_valid_stream` sleep    | 10s    | 5s    | `streamer.py` (hardcoded) |
| Initial poll method            | blind `time.sleep()` | 1s polling loop | `streamer.py` |
| **Worst-case cleanup**         | **145s** | **40s** |                        |
| **Offline streamer recycle**   | **45s**  | **~3s** |                        |

The blind `time.sleep(poll_wait_time)` was replaced with a 1s polling loop
that checks `stream.poll()` each iteration and breaks immediately when the
process exits.

---

## Changes (Feb 25, 2026)

### Stranded Sleeping Process Fixes

**Problem**: yt-dlp processes were accumulating as stranded sleeping orphans
(status `Ss`, PPID=1) across daemon restarts and config reloads.

**Root causes and fixes**:

| #   | Root Cause                                                        | Fix                                                                                              |
|:----|:------------------------------------------------------------------|:-------------------------------------------------------------------------------------------------|
| 1   | `stream_thread` had no `try/finally`. Any exception after         | Wrapped post-`Popen` logic in `try/finally` so `self.stop(signal_child=False)` always runs,      |
|     | `Popen` (e.g., accessing `self.daemon.config` during a            | guaranteeing the child is terminated and reaped even if the thread crashes.                       |
|     | `reload_config()` race) killed the thread without cleanup,        |                                                                                                  |
|     | orphaning the yt-dlp process.                                     |                                                                                                  |
| 2   | `reload_config()` dropped removed streamers without calling       | Removed streamers now have `stop()` called before being dropped. Their yt-dlp processes are      |
|     | `stop()`. Their yt-dlp processes (in their own sessions)          | signaled and reaped.                                                                             |
|     | continued running indefinitely.                                   |                                                                                                  |
| 3   | `reload_config()` used `del self.config` followed by              | Replaced `del` + reassign with direct assignment (`self.config = new_config`). The old           |
|     | `self.config = new_config`. Between those two lines,              | reference is released by GC after reassignment. Same fix applied to `self.streamers`.            |
|     | `self.config` resolved to the class-level `None`, causing         |                                                                                                  |
|     | `TypeError` in concurrent `stream_thread` accesses (trigger       |                                                                                                  |
|     | for Bug 1).                                                       |                                                                                                  |
| 4   | `stop(signal_child=True)` called `wait()` with no timeout         | Added `timeout=10` to `wait()` after SIGINT. On `TimeoutExpired`, logs and falls through to      |
|     | after SIGINT. If yt-dlp was deadlocked, `wait()` blocked          | the existing `terminate()` + `wait()` safety net.                                                |
|     | forever, preventing the `terminate()` safety net from             |                                                                                                  |
|     | executing. During daemon shutdown this hung the run loop           |                                                                                                  |
|     | thread, eventually requiring kill -9 and orphaning all            |                                                                                                  |
|     | children.                                                         |                                                                                                  |

**Files changed**: `streamer.py`, `daemon.py`

**`streamer.py`**:

- `stream_thread()` — wrapped post-`Popen` body in `try/except/finally`.
  `except` logs the error; `finally` guarantees `self.stop(signal_child=False)`.
- `stop()` — `wait()` after SIGINT now uses `timeout=10`. `TimeoutExpired` is
  caught and logged; execution falls through to `terminate()` + `wait()`.

**`daemon.py`**:

- `reload_config()` — removed streamers now have `stop()` called. Removed
  unsafe `del self.config` / `del self.streamers` pattern; replaced with
  direct assignment. Removed unnecessary `del self.streamers[name]` mutation
  inside the preservation loop.

---

## In-Place Upgrade (Handoff)

The daemon supports in-place upgrades: a new daemon process takes over
monitoring of existing yt-dlp subprocesses from the old daemon, allowing code
updates without interrupting active recordings.

### Upgrade Lifecycle

```
CLI: python cli.py upgrade
  │
  ├── 1. Send "handoff" command to running daemon via IPC
  │
  ├── 2. Old daemon receives "handoff":
  │      ├── Sets _handoff = True
  │      ├── Sets self.pid = None (stops run loop)
  │      ├── Run loop exits WITHOUT calling streamer.stop()
  │      ├── yt-dlp children are reparented to PID 1 (launchd)
  │      └── Old daemon process exits
  │
  ├── 3. CLI polls Zeroconf until old service disappears (up to 15s)
  │
  ├── 4. CLI creates new Daemon() and calls daemon.start()
  │      ├── Double-fork daemonization
  │      └── Registers new Zeroconf service
  │
  └── 5. New daemon's run() calls adopt_existing():
         ├── find_running_ytdlp() scans all processes for yt-dlp
         │   with chaturbate.com URLs in their command lines
         ├── For each match in the streamer config:
         │   └── streamer.adopt(pid) wraps the PID in AdoptedProcess
         │       and starts a monitoring thread
         └── Unmatched yt-dlp processes are logged but ignored
```

### AdoptedProcess

`AdoptedProcess` (in `pid.py`) wraps a bare PID with the same interface as
`subprocess.Popen`, allowing `Streamer` to use either interchangeably:

| Method          | Popen                        | AdoptedProcess                    |
|:----------------|:-----------------------------|:----------------------------------|
| `poll()`        | Checks child exit status     | `psutil.Process.status()`         |
| `wait(timeout)` | `os.waitpid()` (child only)  | `psutil.Process.wait()` (any PID) |
| `send_signal(s)`| `os.kill(self.pid, s)`       | `os.kill(self.pid, s)`            |
| `terminate()`   | Sends SIGTERM                | Sends SIGTERM                     |

`wait(timeout)` translates `psutil.TimeoutExpired` into
`subprocess.TimeoutExpired` so `Streamer.stop()` exception handling works
unchanged.

### New IPC Command

| Command   | Handler              | Description                                     |
|:----------|:---------------------|:------------------------------------------------|
| `handoff` | `daemon.handoff()`   | Exit daemon without killing child processes     |

### New CLI Command

```bash
python cli.py upgrade
```

Performs a full in-place upgrade: handoff → wait → start. Active recordings
continue uninterrupted through the new daemon.

### Files Changed

**`pid.py`**:

- `AdoptedProcess` class — Popen-compatible wrapper using `psutil.Process`.
- `find_running_ytdlp()` — scans system processes for yt-dlp with chaturbate
  URLs, returns `{streamer_name: pid}` dict.

**`streamer.py`**:

- `adopt(pid)` — creates a daemon thread that wraps the PID in
  `AdoptedProcess`, validates the stream, and blocks on `wait()` until the
  yt-dlp process exits. On exit, cleans up ffmpeg and resets `self.stream`.

**`daemon.py`**:

- `_handoff` flag — when `True`, the run loop cleanup phase skips
  `streamer.stop()`, leaving children running.
- `handoff()` — sets `_handoff = True`, stops the run loop, exits.
- `adopt_existing()` — called at the start of `run()`. Scans for running
  yt-dlp processes and calls `streamer.adopt(pid)` for each match.
- IPC dispatch — routes `"handoff"` to `daemon.handoff()`.

**`cli.py`**:

- `upgrade()` — sends `"handoff"` via IPC, polls Zeroconf until the old
  service disappears, then starts a new daemon.

---

## Stale Stream Watchdog

ffmpeg can hang indefinitely when an HLS source disconnects. The remote CDN
sends FIN, the TCP sockets enter `CLOSE_WAIT`, but ffmpeg never closes its
end — leaving both ffmpeg and yt-dlp at 0% CPU with a stale output file.
The streamer thread blocks on `wait()` forever.

### Detection Mechanism

`Streamer.wait_with_watchdog(proc)` replaces the previous blocking
`proc.wait()` call in both `stream_thread` and `adopted_thread`:

```
loop:
  proc.wait(timeout=60s)
    └── process exited → return
    └── TimeoutExpired → check output file

  scan videos/{name}/ for .part files
  compare newest .part mtime to last check
    └── mtime advanced  → reset stale timer
    └── mtime unchanged → start/continue stale timer
        └── stale_since >= stale_stream_timeout
            ├── SIGINT → wait(10s)
            │   └── TimeoutExpired → SIGTERM
            └── return (cleanup_ffmpeg + stop follow)
```

After `wait_with_watchdog` returns (either normally or via kill), the
existing `cleanup_ffmpeg()` and `stop(signal_child=False)` chain runs
unchanged.

### Configuration

| Key                    | Default | Description                                    |
|:-----------------------|:--------|:-----------------------------------------------|
| `stale_stream_timeout` | `300`   | Seconds of no output growth before killing     |

Set in `config.json`. The check interval is fixed at 60 seconds, so the
effective resolution is ±60s around the configured threshold.

### Defence in Depth

Two independent layers protect against hung ffmpeg:

| Layer            | Where                       | Mechanism                                    |
|:-----------------|:----------------------------|:---------------------------------------------|
| ffmpeg timeouts  | `youtube-dl.config`         | `-rw_timeout` / `-timeout` (30s, µs units)   |
| Output watchdog  | `Streamer.wait_with_watchdog` | `.part` file mtime staleness (default 300s)  |

The ffmpeg timeouts address hangs where ffmpeg is blocked on a socket
read/write. The output watchdog catches all other causes — including the
`CLOSE_WAIT` bug where ffmpeg is not performing I/O at all.
