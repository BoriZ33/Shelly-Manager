# Shelly Manager

A lightweight Python web application that automatically discovers and manages **Shelly smart home devices** (Gen1, Gen2, and Gen3) on your local network.

![Version](https://img.shields.io/badge/Version-1.0-brightgreen)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Flask](https://img.shields.io/badge/Flask-2.3%2B-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Features

### 🔍 Network Discovery
- **IP sweep** — scans the entire local `/24` subnet with 50 parallel threads (~5–15 seconds)
- **mDNS discovery** — instantly finds devices via `_shelly._tcp` and `_http._tcp` (requires `zeroconf`)
- Supports **Gen1**, **Gen2**, and **Gen3** Shelly devices simultaneously
- Displays device name, model, MAC address, firmware version, and generation badge

### 🌐 Web Interface
- Clean browser UI accessible at `http://localhost:5000`
- Clickable IP links open the device's own web interface in a new tab
- **Dark and Light theme** — toggle in the top-right corner, preference saved in browser
- **German and English language** — switch instantly, all UI text updates live

### ⬆️ Firmware Updates
- Checks for available updates on every scan and refresh
- **Update** button per device — triggers update immediately
- **Update All** button — updates every device with a pending update in one click
- Update count shown in the button label (e.g. `Update All (3)`)
- Live "Updating…" status indicator per device

### 📶 WiFi Configuration
- Change WiFi credentials on one device, selected devices, or all devices at once
- Checkbox selection per row for bulk operations
- Warning shown before applying (device will restart and reconnect)
- Supports Gen1 (`/settings/sta`) and Gen2/Gen3 (`WiFi.SetConfig` RPC)

### 🔄 Device Reboot
- **Restart** button per device row
- **Restart All** toolbar button to reboot every discovered device

### ⚙️ Scheduler (Auto-Restart & Auto-Update)
- Collapsible **Settings panel** below the toolbar
- **Auto-Restart** — enable a daily reboot of all devices at a configured time (e.g. `03:00`)
- **Auto-Update** — automatically update devices at a configurable interval (e.g. every `24` hours)
- Both features use toggle switches (on/off) — no need to type "0"
- Settings are **persisted** to `shelly_settings.json` and survive restarts
- Scheduler runs every 30 seconds in the background — no manual trigger needed

---

## Requirements

- **Python 3.10+**
- **pip packages:**

```
flask>=2.3
requests>=2.31
zeroconf>=0.131   # optional — enables faster mDNS discovery
```

---

## Installation & Usage

### Windows
Double-click **`Start Shelly Manager.bat`** — it will:
1. Check that Python is installed
2. Install missing packages automatically
3. Start the server
4. Open `http://localhost:5000` in your browser automatically

### Linux / macOS (including Debian/Ubuntu servers)

> Modern Debian/Ubuntu systems block system-wide `pip install` (PEP 668).  
> The start script handles this automatically using a **virtual environment**.

```bash
# Clone the repository
git clone https://github.com/BoriZ33/Shelly-Manager.git
cd Shelly-Manager

# Make the start script executable (only needed once)
chmod +x start.sh

# Start — creates venv and installs dependencies automatically
./start.sh
```

The server is then available at `http://<server-ip>:5000`.

#### Run as a background service (optional)

```bash
# Keep running after logout with nohup
nohup ./start.sh > shelly.log 2>&1 &
echo "PID: $!"

# Or use screen
screen -S shelly
./start.sh
# Detach: Ctrl+A then D
```

#### Run as a systemd service (auto-start on boot)

```bash
sudo nano /etc/systemd/system/shelly-manager.service
```

Paste:

```ini
[Unit]
Description=Shelly Network Manager
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/Shelly-Manager
ExecStart=/path/to/Shelly-Manager/.venv/bin/python shelly_manager.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable shelly-manager
sudo systemctl start shelly-manager
sudo systemctl status shelly-manager
```

### Manual start (any OS)
```bash
python3 -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows
pip install flask requests zeroconf
python shelly_manager.py
```

> Keep the terminal open — closing it stops the server (unless running as a service).

---

## How It Works

### Device Detection
| Generation | Detection endpoint | Update check | Update trigger | Reboot |
|---|---|---|---|---|
| Gen1 | `GET /shelly` | `GET /ota` | `GET /ota?update=1` | `GET /reboot` |
| Gen2 / Gen3 | `GET /rpc/Shelly.GetDeviceInfo` | `GET /rpc/Shelly.CheckForUpdate` | `POST /rpc/Shelly.Update` | `POST /rpc/Shelly.Reboot` |

### WiFi Change
| Generation | Endpoint |
|---|---|
| Gen1 | `POST /settings/sta` with `ssid` + `key` |
| Gen2 / Gen3 | `POST /rpc/WiFi.SetConfig` with JSON config |

> ⚠️ Changing WiFi credentials will cause the device to restart and reconnect to the new network. Make sure the credentials are correct before applying.

---

## REST API

The backend exposes a simple HTTP API used by the frontend:

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/devices` | List all discovered devices |
| `POST` | `/api/scan` | Start a new network scan |
| `GET` | `/api/scan/status` | Scan progress and found device count |
| `POST` | `/api/device/<ip>/update` | Trigger firmware update |
| `POST` | `/api/update/all` | Update all devices with pending updates |
| `POST` | `/api/device/<ip>/refresh` | Re-query a single device |
| `POST` | `/api/device/<ip>/reboot` | Reboot a single device |
| `POST` | `/api/reboot/all` | Reboot all devices |
| `POST` | `/api/wifi` | Set WiFi credentials (body: `{ssid, password, ips}`) |
| `GET` | `/api/settings` | Get current scheduler settings |
| `POST` | `/api/settings` | Save scheduler settings |

---

## File Structure

```
Shelly-Manager/
├── shelly_manager.py         # Main application (backend + embedded frontend)
├── Start Shelly Manager.bat  # Windows one-click launcher
├── requirements.txt          # Python dependencies
├── shelly_settings.json      # Auto-created on first save (gitignored)
└── .gitignore
```

---

## Notes

- Devices protected by a **password** (HTTP Digest Auth) are marked with a 🔒 badge. Authentication support can be added if needed.
- The scanner uses 50 parallel threads — scanning a /24 network typically takes 5–15 seconds.
- `shelly_settings.json` is excluded from git to avoid committing schedule configs.

---

## Changelog

### v1.0 — Initial Release
- Network scan via IP sweep (50 threads) and optional mDNS discovery
- Firmware update: per device, update all, live status indicator
- WiFi credential change: single device, selection, or all devices
- Device reboot: per device and global
- Auto-Restart scheduler: Daily or specific weekday + time
- Auto-Update scheduler: Daily or specific weekday + time
- Dark / Light theme (persisted in browser)
- German / English language (persisted in browser)
- Settings saved to `shelly_settings.json`
- Windows batch launcher + Linux `start.sh` with automatic virtualenv

---

## License

MIT
