<p align="center"><img src="assets/logo.png" width="96"></p>
<h1 align="center">SoberStrap</h1>
<p align="center">A Bloxstrap-style launcher for <a href="https://sober.vinegarhq.org">Sober</a> — Roblox on Linux.</p>

## Features
- **Play** — one-click launch with three modes:
  - **Auto** — uses Fleasion's proxy only if it's actually running, otherwise launches without it
    (fixes `CONNECT tunnel failed, response 502` / "Could not connect" when Fleasion is closed)
  - **With Fleasion** — starts Fleasion, waits for its proxy, then launches
  - **No proxy** — plain Roblox
- FPS cap buttons, force close, live status (Sober version, Roblox session timer, Fleasion proxy, flag count)
- **FastFlags editor** — search, inline edit with type checking, performance preset,
  import (Bloxstrap / `ClientAppSettings.json`, drag & drop; crash/debug flags stripped automatically),
  export to Bloxstrap format, backup + restore
- **Tools** — update Sober, clear cache, open config / logs
- **Logs** — errors from the latest Sober launch, cleaned up and copyable
- Custom titlebar, animations, keyboard shortcuts (Ctrl+1–5, Ctrl+Enter, Ctrl+F, Ctrl+S, F5)

FastFlags are written to Sober's own config (`~/.var/app/org.vinegarhq.Sober/config/sober/config.json`).

## Build & install
```sh
./build.sh      # -> dist/soberstrap (single binary)
./install.sh    # -> ~/.local/bin/sober-strap + app-menu entry
```
Requires Python 3 and Sober installed from Flathub.

## CLI
```sh
sober-strap              # GUI
sober-strap --launch     # launch (auto proxy)
sober-strap --fleasion   # GUI, start Fleasion, then launch
sober-strap --no-proxy   # launch without proxy
```

## Run from source
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python soberstrap.py
```
