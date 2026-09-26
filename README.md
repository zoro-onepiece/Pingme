# Pingme

Pingme is a lightweight Python daemon that monitors log files from various AI coding assistants (like Cursor, Claude Code, Antigravity, VS Code, Qoder, OpenCode, CommandCode) and alerts you with a desktop notification and sound when a specific keyword is detected. 

It is designed to let you know exactly when an AI agent is waiting for your input, needs permission, or has completed a task, so you don't have to constantly check back on long-running processes.

## Features

- **Multi-Tool Monitoring:** Monitor logs for multiple AI agents simultaneously.
- **Smart Tailing:** Efficiently tails active log files and automatically detects new log files as they are created to never miss an update.
- **Customizable Keywords:** Define specific keywords (e.g., "waiting for user", "action required") for each tool.
- **Rich Notifications:** Provides exact context in the desktop notification, showing the matched keyword and the relevant line from the log.
- **Cross-Platform Audio:** Plays an audio alert on Linux, macOS, and Windows.

## Prerequisites

- **Python 3.6+**
- **Linux:** Requires `notify-send` (usually pre-installed with `libnotify-bin`) and `paplay`/`aplay` for audio.
- **macOS / Windows:** Uses native audio players (`afplay` / `powershell`).

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/pingme.git
   cd pingme
   ```

2. Configure your monitoring settings (see Configuration below).

## Configuration

Pingme uses a `config.json` file to know which files to monitor and what keywords to look for. By default, it comes with configurations for popular AI tools:

```json
{
    "Antigravity": {
        "log_pattern": "~/.gemini/antigravity-cli/brain/**/*.jsonl",
        "keywords": ["waiting_for_input", "waiting for user", "action required", "USER_INPUT"]
    },
    "Cursor": {
        "log_pattern": "~/.config/Cursor/logs/**/*.log",
        "keywords": ["waiting for user", "permission required", "task completed"]
    }
}
```

- **`log_pattern`**: The path to the tool's log directory. Supports `~` for the home directory and `**` for recursive search across subdirectories.
- **`keywords`**: A list of case-insensitive strings that will trigger an alert if found in the new log entries.

## Usage

Run the script in your terminal or set it up as a background daemon:

```bash
python3 pingme.py
```

The script will stay running, silently watching your log files. When a keyword is found, you will hear a ping and see a desktop notification showing exactly what the AI agent needs!

## Automated AI Setup

If you prefer not to set up Pingme manually, you can simply provide the following prompt to your AI assistant (like Claude, Cursor, Antigravity, etc.). The AI agent will automatically configure and run Pingme on your system:

> **Prompt for AI Agent:**
> "Please set up the Pingme daemon on my system to monitor AI coding assistants. Ensure my system has `notify-send` and audio playback utilities installed. Check the `config.json` file for log paths and update them if needed based on my OS. Finally, run `python3 pingme.py` in the background so it can alert me when an AI agent needs my input or finishes a task."
