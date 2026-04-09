# AI Agent System

A production AI operations system: web terminal, knowledge base, and meeting copilot. Run locally or on a VPS. Code from your phone.

## What You Get

- **Terminal** (port 4021) -- Web-based tmux terminal. Access your machine from any browser, including phone.
- **Wiki** (port 4090) -- Knowledge base with AI-powered article compilation. Ingest URLs, PDFs, files. Search everything.
- **Copilot** (port 4051) -- Real-time meeting transcription with AI suggestions. Uses browser Web Speech API (no API key needed).

## Quick Start

```bash
git clone https://github.com/nikitarogers333/ai-agent-system.git
cd ai-agent-system
./deploy/setup.sh
./start.sh
```

Open http://localhost:4021 in your browser. Done.

### Phone Access

On the same WiFi, open `http://YOUR_COMPUTER_IP:4021` on your phone. The setup script prints your LAN IP.

### VPS Install

```bash
./deploy/setup.sh --vps
```

Adds systemd services, opens firewall ports. Access from anywhere.

## Requirements

- Node.js 18+
- Python 3.10+
- tmux
- Claude CLI (`npm install -g @anthropic-ai/claude-code && claude login`)

## Configuration

Edit `.env` after setup:

```
TTY_PORT=4021
WIKI_PORT=4090
COPILOT_PORT=4051
```

## Architecture

```
Phone/Browser
     |
     v
[Terminal :4021] --- tmux sessions --- [Claude CLI]
     |
[Wiki :4090] --- SQLite FTS5 --- [Article Compiler]
     |
[Copilot :4051] --- Web Speech API --- [AI Suggestions]
```

Each service runs independently. Start what you need.

## Commands

```bash
./start.sh       # Start all services
./stop.sh        # Stop all services
./status.sh      # Check what's running
```

## License

MIT
