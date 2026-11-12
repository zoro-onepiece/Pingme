import os
import time
import json
import signal
import subprocess
import glob
import sys
import pty
import re
import argparse
import fcntl

CONFIG_FILE = os.path.expanduser("~/PROJECTS/Pingme/config.json")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PING_FILE = os.path.join(BASE_DIR, "pings.log")
AGENT_DIR = os.path.join(BASE_DIR, ".agents")
LAST_PING_FILE = os.path.join(BASE_DIR, ".last_ping")
PING_COOLDOWN = 5  # seconds: same event detected by daemon + wrap = one ping only


def live_agents():
    """Marker files of agent sessions that are still running."""
    if not os.path.isdir(AGENT_DIR):
        return []
    alive = []
    for name in os.listdir(AGENT_DIR):
        if not name.endswith(".agent"):
            continue
        marker = os.path.join(AGENT_DIR, name)
        try:
            pid = int(name.split(".")[0])
        except ValueError:
            os.remove(marker)
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            os.remove(marker)  # stale marker, agent is gone
            continue
        alive.append(marker)
    return alive


def register_agent():
    os.makedirs(AGENT_DIR, exist_ok=True)
    marker = os.path.join(AGENT_DIR, f"{os.getpid()}.agent")
    with open(marker, "w") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    return marker


def unregister_agent(marker):
    try:
        os.remove(marker)
    except OSError:
        pass


def clear_pings():
    try:
        with open(PING_FILE, "w"):
            pass
    except OSError:
        pass


def _last_ping_times():
    """{tool_title: unix_time} of recent pings, shared across daemon and wrappers."""
    try:
        with open(LAST_PING_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _claim_ping(key):
    """Atomically decide-and-record a ping for `key`. True if we should ping.

    The daemon, terminal wrappers, and a tool's own hooks all run as separate
    processes. A plain read-check-write races when several fire at once (e.g.
    OpenCode emits back-to-back execution events), letting duplicate pings
    through. An exclusive file lock makes it exactly one ping per event, while
    still letting *different* keys (other tools/events) through.
    """
    key = key or ""
    try:
        lock = open(LAST_PING_FILE + ".lock", "w")
    except OSError:
        return True  # can't lock: better to ping than to stay silent
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _last_ping_times()
        now = time.time()
        try:
            on_cd = (now - float(data.get(key, 0))) < PING_COOLDOWN
        except (TypeError, ValueError):
            on_cd = False
        if on_cd:
            return False
        data[key] = now
        # Keep the file tiny: drop entries from long-finished sessions.
        for stale in [k for k, v in data.items()
                      if not isinstance(v, (int, float)) or now - v > 600]:
            data.pop(stale, None)
        try:
            with open(LAST_PING_FILE, "w") as f:
                json.dump(data, f)
        except OSError:
            pass
        return True
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


def send_ping(title, message, key=None):
    # One event = one ping, no matter how many watchers spotted it
    if not _claim_ping(key or title):
        return False

    # 1. Store in a single file for the agent to read.
    # Never written to terminals, so agent output stays clean.
    if live_agents():
        with open(PING_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {title}: {message}\n")
    else:
        # No agent listening, don't let the file pile up.
        clear_pings()

    # 2. Desktop notification (daemon's own sound suppressed,
    #    paplay below is the only sound that plays)
    try:
        # DBus handles notify-send in user systemd service.
        subprocess.run(
            ["notify-send", "-a", "pingme", "-u", "critical",
             "-h", "boolean:suppress-sound:true", title, message],
            check=False,
        )
    except Exception:
        pass

    # 3. Play the ping sound (same loud sound for every tool)
    sound_cmd = 'case "$(uname -s)" in Darwin) afplay /System/Library/Sounds/Glass.aiff ;; Linux) paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null || aplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null || printf \'\\a\' ;; *) printf \'\\a\' ;; esac'
    subprocess.run(sound_cmd, shell=True)
    return True


# A tool's own hook/plugin reports these; keep them stable so a log watcher and
# the tool's hook collapse into the same single ping.
PERMISSION_HINTS = (
    "permission", "allow", "proceed", "approval", "approve", "ask",
    "cleanbuild", "differences", "[y/n]", "(y/n)", "continue", "selection",
    "replace"
)
PASSWORD_HINTS = ("password", "passphrase")
COMPLETE_HINTS = ("complete", "completed", "done", "finished", "idle")


def event_kind(keyword):
    """Map a detected keyword to a stable event kind (permission/password/complete/...)."""
    lowered = keyword.lower()
    # Completion first: "task completed" contains "ask", so a permission-first
    # check would file every completion under the permission dedup key and let
    # it swallow a real permission ping that lands within the cooldown.
    if any(hint in lowered for hint in COMPLETE_HINTS):
        return "complete"
    if any(hint in lowered for hint in PASSWORD_HINTS):
        return "password"
    if any(hint in lowered for hint in PERMISSION_HINTS):
        return "permission"
    return lowered


def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (OSError, ValueError):
        # The daemon reloads this every cycle; a half-written or missing file
        # must not take the watcher down.
        return {}

def matching_files(pattern):
    try:
        return glob.glob(os.path.expanduser(pattern), recursive=True, include_hidden=True)
    except TypeError:
        return glob.glob(os.path.expanduser(pattern), recursive=True)


def describe_line(line):
    """Turn a matched log line into something readable in a notification.

    Most tools log a plain sentence, but Antigravity logs a glog line whose
    first ~60 chars are `I1012 19:28:24.305882    1070 file.go:226]` -- that
    prefix pushes the useful part out of view. Drop it, and for confirmation
    asks show the tool being asked about.
    """
    line = re.sub(r"^[IWEF]\d{4} [\d:.]+\s+\d+ [\w.]+\]\s*", "", line.strip())
    m = re.search(r'tool confirmation:\s*"([^"]+)"', line, re.IGNORECASE)
    if m:
        return f"{m.group(1)} needs your approval"
    return line[:100] + "..." if len(line) > 100 else line


def scan_and_ping(tool, keywords, new_content):
    lowered = new_content.lower()
    found_keyword = next((kw for kw in keywords if kw.lower() in lowered), None)
    if not found_keyword:
        return

    context_line = "Action required or task completed."
    for line in new_content.splitlines():
        if found_keyword.lower() in line.lower():
            context_line = describe_line(line)
            break
    send_ping(f"Pingme: {tool}", f"Keyword '{found_keyword}' found: {context_line}",
              key=f"{tool}:{event_kind(found_keyword)}")


GLOB_REFRESH = 10  # seconds between re-scanning a pattern for new log files


def monitor_logs():
    state = {}  # log file path -> byte offset already read
    found_files = {}  # pattern -> (checked_at, [paths]); globbing is expensive
    START_TIME = time.time()

    print("Pingme daemon is running and monitoring logs...")
    while True:
        # Reload every cycle so config edits take effect without a restart.
        config = load_config()
        now = time.time()
        for tool, settings in config.items():
            # A tool can expose more than one place where a prompt shows up
            # (Antigravity logs permission asks to its cli log, but task/user
            # events to its brain transcript).
            patterns = settings.get("log_patterns") or []
            if settings.get("log_pattern"):
                patterns.append(settings["log_pattern"])
            keywords = settings.get("keywords", [])

            for pattern in patterns:
                cached = found_files.get(pattern)
                if cached is None or now - cached[0] >= GLOB_REFRESH:
                    paths = matching_files(pattern)
                    found_files[pattern] = (now, paths)
                else:
                    paths = cached[1]

            # Watch *every* matching file, not just the newest one: a prompt
                # can be written to a different file than the one last touched.
                for path in paths:
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue

                    if path not in state:
                        # Never replay history that predates the daemon.
                        if os.path.getmtime(path) >= START_TIME:
                            state[path] = 0
                        else:
                            state[path] = size
                            continue

                    offset = state[path]
                    if size < offset:
                        offset = 0  # file was truncated or rewritten in place

                    if size > offset:
                        try:
                            with open(path, 'r', errors='ignore') as f:
                                f.seek(offset)
                                new_content = f.read()
                        except OSError:
                            continue
                        state[path] = size
                        scan_and_ping(tool, keywords, new_content)
        time.sleep(2)


def wrap_command(tool_name, cmd, keywords):
    import termios
    import tty

    buffer = b""
    last_ping_time = 0
    # Keywords currently visible on screen. A phrase fires once when it appears
    # and again only after it has scrolled away, so an unrelated keyword in the
    # output can never clear the buffer and swallow the prompt that follows.
    latched = set()
    keyword_bytes = [kw.lower().encode('utf-8') for kw in keywords]

    def master_read(fd):
        nonlocal buffer, last_ping_time, latched
        try:
            data = os.read(fd, 10240)
        except OSError:
            return b""

        if data:
            buffer += data
            # Keep enough context that a prompt split across two reads still matches.
            if len(buffer) > 4096:
                buffer = buffer[-4096:]

            buf_lower = buffer.lower()
            present = {kw for kw in keyword_bytes if kw in buf_lower}
            latched &= present  # a phrase that scrolled away may fire again

            for kw in keyword_bytes:
                if kw in present and kw not in latched:
                    now = time.time()
                    # Debounce the same prompt being redrawn, not new events.
                    if now - last_ping_time > 2:
                        found = kw.decode('utf-8')
                        kind = event_kind(found)
                        if kind == "password":
                            desc = f"Password required: {found}"
                        else:
                            desc = f"Action required ({found})"
                        send_ping(f"Pingme: {tool_name}",
                                  desc,
                                  key=f"{tool_name}:{kind}")
                        last_ping_time = now
                    latched.add(kw)
                    break
        return data

    marker = register_agent()

    pid, master_fd = pty.fork()
    if pid == pty.CHILD:
        try:
            os.execvp(cmd[0], cmd)
        except Exception as e:
            sys.stderr.write(f"Failed to execute {cmd[0]}: {e}\n")
            os._exit(1)

    # Synchronize initial terminal window size
    try:
        if sys.stdin.isatty():
            ws = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
    except Exception:
        pass

    # Propagate window resizing to the pty slave
    def handle_sigwinch(signum, frame):
        try:
            if sys.stdin.isatty():
                ws = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
        except Exception:
            pass

    old_sigwinch = signal.signal(signal.SIGWINCH, handle_sigwinch)

    restore_mode = None
    try:
        if sys.stdin.isatty():
            restore_mode = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
    except Exception:
        pass

    try:
        pty._copy(master_fd, master_read=master_read)
    except Exception:
        pass
    finally:
        if restore_mode is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, restore_mode)
            except Exception:
                pass
        try:
            signal.signal(signal.SIGWINCH, old_sigwinch)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
        unregister_agent(marker)

    try:
        _, status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
    except OSError:
        code = 0
    sys.exit(code)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "wrap":
        # Usage: pingme.py wrap <ToolName> -- <cmd> [args...]
        #    or: pingme.py wrap <cmd> [args...]
        if len(sys.argv) < 3:
            print("Usage: pingme.py wrap [ToolName --] <cmd> [args...]")
            sys.exit(1)

        if "--" in sys.argv:
            sep_idx = sys.argv.index("--")
            tool_name = sys.argv[2] if sep_idx > 2 else os.path.basename(sys.argv[sep_idx + 1]).capitalize()
            cmd = sys.argv[sep_idx + 1:]
        else:
            cmd = sys.argv[2:]
            tool_name = os.path.basename(cmd[0]).capitalize()

        config = load_config()
        # Default keywords if tool not in config. Permission & password phrases come first
        # because a missed permission prompt is the failure that actually hurts.
        keywords = config.get(tool_name, {}).get("keywords", [
            "permission required", "permission requested", "allow once",
            "always allow", "do you want to proceed", "waiting for user",
            "waiting for input", "action required", "task completed",
            "approval", "USER_INPUT",
            "[sudo] password for", "password for", "password:",
            "enter passphrase", "passphrase for",
            "proceed with installation?", "proceed with installation",
            "[y/n]", "(y/n)", "differences in pkgbuild",
            "packages to cleanbuild", "enter a selection", "enter a number",
            "do you want to continue", "replace"
        ])

        wrap_command(tool_name, cmd, keywords)

    elif len(sys.argv) > 1 and sys.argv[1] == "notify":
        # Usage: pingme.py notify <ToolName> <permission|password|complete|...>
        # Called from a tool's own hook or plugin. This is the reliable path:
        # the tool tells us exactly why it is waiting, so we only ping then.
        if len(sys.argv) < 4:
            print("Usage: pingme.py notify <ToolName> <event>")
            sys.exit(1)
        tool_name = sys.argv[2]
        kind = sys.argv[3]
        labels = {
            "permission": "Permission required",
            "password": "Password required",
            "complete": "Task completed",
        }
        send_ping(f"Pingme: {tool_name}", labels.get(kind, kind),
                  key=f"{tool_name}:{event_kind(kind)}")
        sys.exit(0)

    elif len(sys.argv) > 1 and sys.argv[1] not in ("-d", "--daemon"):
        # Convenience: `pingme <cmd> [args...]` wraps automatically
        cmd = sys.argv[1:]
        tool_name = os.path.basename(cmd[0]).capitalize()
        config = load_config()
        keywords = config.get(tool_name, {}).get("keywords", [
            "permission required", "permission requested", "allow once",
            "always allow", "do you want to proceed", "waiting for user",
            "waiting for input", "action required", "task completed",
            "approval", "USER_INPUT",
            "[sudo] password for", "password for", "password:",
            "enter passphrase", "passphrase for",
            "proceed with installation?", "proceed with installation",
            "[y/n]", "(y/n)", "differences in pkgbuild",
            "packages to cleanbuild", "enter a selection", "enter a number",
            "do you want to continue", "replace"
        ])
        wrap_command(tool_name, cmd, keywords)

    else:
        # Default daemon mode
        monitor_logs()

if __name__ == "__main__":
    main()
