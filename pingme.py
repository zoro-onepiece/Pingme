import os
import time
import json
import signal
import subprocess
import glob
import sys
import pty
import argparse

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


def on_cooldown():
    """True if a ping already fired moments ago (daemon and wrap can both see it)."""
    try:
        return (time.time() - os.path.getmtime(LAST_PING_FILE)) < PING_COOLDOWN
    except OSError:
        return False


def mark_pinged():
    try:
        with open(LAST_PING_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def send_ping(title, message):
    # One event = one ping, no matter how many watchers spotted it
    if on_cooldown():
        return
    mark_pinged()

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


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def get_latest_file(pattern):
    try:
        files = glob.glob(os.path.expanduser(pattern), recursive=True, include_hidden=True)
    except TypeError:
        files = glob.glob(os.path.expanduser(pattern), recursive=True)
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def monitor_logs():
    config = load_config()
    state = {}
    START_TIME = time.time()
    
    print("Pingme daemon is running and monitoring logs...")
    while True:
        for tool, settings in config.items():
            pattern = settings.get("log_pattern")
            keywords = settings.get("keywords", [])
            
            latest_file = get_latest_file(pattern)
            if not latest_file:
                continue
                
            if latest_file not in state:
                if os.path.getmtime(latest_file) >= START_TIME:
                    state[latest_file] = 0
                else:
                    state[latest_file] = os.path.getsize(latest_file)
                    continue
                
            try:
                current_size = os.path.getsize(latest_file)
                if current_size > state[latest_file]:
                    with open(latest_file, 'r', errors='ignore') as f:
                        f.seek(state[latest_file])
                        new_content = f.read()
                        
                        found_keyword = None
                        for keyword in keywords:
                            if keyword.lower() in new_content.lower():
                                found_keyword = keyword
                                break
                                
                        if found_keyword:
                            lines = new_content.splitlines()
                            context_line = "Action required or task completed."
                            for line in lines:
                                if found_keyword.lower() in line.lower():
                                    context_line = line.strip()
                                    if len(context_line) > 100:
                                        context_line = context_line[:97] + "..."
                                    break
                            send_ping(f"Pingme: {tool}", f"Keyword '{found_keyword}' found: {context_line}")
                            
                    state[latest_file] = current_size
            except Exception:
                pass
        time.sleep(2)


def wrap_command(tool_name, cmd, keywords):
    buffer = b""
    last_ping_time = 0
    keyword_bytes = [kw.lower().encode('utf-8') for kw in keywords]
    
    def master_read(fd):
        nonlocal buffer, last_ping_time
        try:
            data = os.read(fd, 10240)
        except OSError:
            return b""
            
        if data:
            buffer += data
            # Keep buffer size manageable (check last 4KB)
            if len(buffer) > 4096:
                buffer = buffer[-4096:]
                
            buf_lower = buffer.lower()
            found = None
            for kw in keyword_bytes:
                if kw in buf_lower:
                    found = kw.decode('utf-8')
                    break
                    
            if found:
                now = time.time()
                # Debounce notifications for 5 seconds
                if now - last_ping_time > 5:
                    send_ping(f"Pingme: {tool_name}", f"Action required ({found})")
                    last_ping_time = now
                buffer = b""
        return data
        
    marker = register_agent()

    def on_signal(signum, frame):
        unregister_agent(marker)
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)

    try:
        status = pty.spawn(cmd, master_read=master_read)
        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
        else:
            code = 1
    except OSError as e:
        print(f"Error wrapping command: {e}")
        code = 1
    finally:
        unregister_agent(marker)
    sys.exit(code)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "wrap":
        # Usage: pingme.py wrap <ToolName> -- <cmd> [args...]
        if len(sys.argv) < 4:
            print("Usage: pingme.py wrap <ToolName> -- <cmd> [args...]")
            sys.exit(1)
            
        tool_name = sys.argv[2]
        try:
            sep_idx = sys.argv.index("--")
            cmd = sys.argv[sep_idx+1:]
        except ValueError:
            cmd = sys.argv[3:]
            
        config = load_config()
        # Default keywords if tool not in config
        keywords = config.get(tool_name, {}).get("keywords", [
            "waiting for user", "action required", "permission required", 
            "task completed", "approval", "USER_INPUT"
        ])
        
        wrap_command(tool_name, cmd, keywords)
    else:
        # Default daemon mode
        monitor_logs()

if __name__ == "__main__":
    main()
