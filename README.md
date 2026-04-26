# Shelly Manager

A lightweight Python web application that automatically discovers and manages **Shelly smart home devices** (Gen1, Gen2, and Gen3) on your local network.

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

## Installation

```bash
# Clone the repository
git clone https://github.com/BoriZ33/Shelly-Manager.git
cd Shelly-Manager

# Install dependencies
pip install flask requests
pip install zeroconf        # optional but recommended
```

---

## Usage

### Windows (recommended)
Double-click **`Start Shelly Manager.bat`** — it will:
1. Check that Python is installed
2. Install missing packages automatically
3. Start the server
4. Open `http://localhost:5000` in your browser automatically

### Manual start
```bash
python shelly_manager.py
# or on Windows:
py shelly_manager.py
```

Then open **http://localhost:5000** in your browser.

> Keep the terminal window open — closing it stops the server.

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

## License

MIT
