import os
import time
import json
import subprocess
import glob

CONFIG_FILE = "config.json"

def send_ping(title, message):
    subprocess.run(["notify-send", "-u", "critical", title, message])
    sound_cmd = "case \"$(uname -s)\" in Darwin) afplay /System/Library/Sounds/Glass.aiff ;; Linux) paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null || aplay /usr/share/sounds/alsa/Front_Center.wav 2>/dev/null || printf '\\a' ;; MINGW*|MSYS*|CYGWIN*) powershell.exe -c \"[console]::beep(750,300)\" 2>/dev/null || printf '\\a' ;; *) printf '\\a' ;; esac"
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

def monitor():
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
            except Exception as e:
                pass
        time.sleep(2)

if __name__ == "__main__":
    monitor()
