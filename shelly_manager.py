#!/usr/bin/env python3
"""
Shelly Network Manager
Scans the local network for Shelly devices (Gen1 + Gen2/Gen3) and provides
a web management interface with firmware update and WiFi configuration support.

Dependencies:
    pip install flask requests
    pip install zeroconf   # optional but recommended for faster mDNS discovery

Usage:
    python shelly_manager.py
    Then open http://localhost:5000 in your browser
"""

import datetime
import json
import os
import socket
import threading
import time
import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

try:
    from flask import Flask, jsonify, render_template_string, request
    import requests
    requests.packages.urllib3.disable_warnings()
except ImportError:
    print("ERROR: Missing dependencies. Run:  pip install flask requests")
    raise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Global state ──────────────────────────────────────────────────────────────

devices: dict = {}          # ip -> device_dict
devices_lock = threading.RLock()

scan_status = {
    "running": False,
    "progress": 0,
    "total": 0,
    "found": 0,
    "start_time": None,
}

try:
    from zeroconf import ServiceBrowser, Zeroconf, ServiceStateChange
    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.warning("zeroconf not installed – mDNS discovery disabled (pip install zeroconf)")

# ─── Settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shelly_settings.json")

settings: dict = {
    "auto_restart_enabled": False,  # bool
    "auto_restart_time":    "03:00", # "HH:MM"
    "auto_update_enabled":  False,  # bool
    "auto_update_hours":    24,      # int hours
    # internal timestamps (not persisted)
    "_last_restart_day":    None,
    "_last_auto_update":    0.0,
}
settings_lock = threading.Lock()


def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            with settings_lock:
                for k, v in saved.items():
                    if k in settings and not k.startswith("_"):
                        settings[k] = v
            logger.info("Settings loaded.")
    except Exception as e:
        logger.error(f"Could not load settings: {e}")


def save_settings():
    try:
        with settings_lock:
            data = {k: v for k, v in settings.items() if not k.startswith("_")}
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save settings: {e}")

# ─── Network helpers ────────────────────────────────────────────────────────────

def get_local_networks() -> list:
    """Return IPv4Network objects for all local /24 subnets."""
    networks = []
    seen = set()

    # Method 1: hostname resolution (may return multiple IPs on multi-homed hosts)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in seen:
                seen.add(ip)
                networks.append(ipaddress.IPv4Network(f"{ip}/24", strict=False))
    except Exception:
        pass

    # Method 2: route-based fallback (works even when hostname doesn't resolve)
    if not networks:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip not in seen:
                networks.append(ipaddress.IPv4Network(f"{ip}/24", strict=False))
        except Exception:
            networks.append(ipaddress.IPv4Network("192.168.1.0/24"))

    return networks

# ─── Shelly device detection ────────────────────────────────────────────────────

def _get(url: str, timeout: float = 2.0) -> Optional[dict]:
    """GET a URL and return parsed JSON, or None on any error."""
    try:
        r = requests.get(url, timeout=timeout, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _check_update_gen1(ip: str) -> tuple[bool, Optional[str]]:
    data = _get(f"http://{ip}/ota", timeout=2.0)
    if data:
        return bool(data.get("has_update")), data.get("new_version")
    return False, None


def _check_update_gen2(ip: str) -> tuple[bool, Optional[str]]:
    data = _get(f"http://{ip}/rpc/Shelly.CheckForUpdate", timeout=2.0)
    if data:
        stable = data.get("stable") or {}
        if stable.get("version"):
            return True, stable["version"]
    return False, None


def probe_gen1(ip: str, timeout: float = 2.0) -> Optional[dict]:
    """Try to identify a Gen1 Shelly device at the given IP."""
    data = _get(f"http://{ip}/shelly", timeout=timeout)
    if not data or ("mac" not in data and "type" not in data):
        return None

    has_upd, new_ver = _check_update_gen1(ip)
    name = data.get("name") or data.get("type") or "Shelly"

    return {
        "ip": ip,
        "gen": 1,
        "type": data.get("type", "Unknown"),
        "model": data.get("type", "Unknown"),
        "mac": data.get("mac", "Unknown"),
        "firmware": data.get("fw", "Unknown"),
        "auth": bool(data.get("auth", False)),
        "has_update": has_upd,
        "new_version": new_ver,
        "name": name,
        "last_seen": time.time(),
        "update_status": None,
    }


def probe_gen2(ip: str, timeout: float = 2.0) -> Optional[dict]:
    """Try to identify a Gen2/Gen3 Shelly device at the given IP."""
    data = _get(f"http://{ip}/rpc/Shelly.GetDeviceInfo", timeout=timeout)
    if not data or "mac" not in data or "model" not in data:
        return None

    has_upd, new_ver = _check_update_gen2(ip)
    gen = data.get("gen", 2)
    name = data.get("name") or data.get("app") or f"Shelly {data.get('model', '')}"

    return {
        "ip": ip,
        "gen": gen,
        "type": data.get("app", "Unknown"),
        "model": data.get("model", "Unknown"),
        "mac": data.get("mac", "Unknown"),
        "firmware": data.get("ver", "Unknown"),
        "auth": bool(data.get("auth_en", False)),
        "has_update": has_upd,
        "new_version": new_ver,
        "name": name,
        "last_seen": time.time(),
        "update_status": None,
    }


def probe_ip(ip: str) -> Optional[dict]:
    """Probe a single IP for any Shelly device (Gen2 first, then Gen1)."""
    return probe_gen2(ip, timeout=1.5) or probe_gen1(ip, timeout=1.5)

# ─── Scanner ────────────────────────────────────────────────────────────────────

def run_network_scan():
    """Background thread: sweep all IPs in local /24 subnets."""
    global scan_status

    networks = get_local_networks()
    all_ips = []
    for net in networks:
        logger.info(f"Scanning network: {net}")
        all_ips.extend(str(ip) for ip in net.hosts())

    # Deduplicate while preserving order
    seen_ips: set = set()
    unique_ips = []
    for ip in all_ips:
        if ip not in seen_ips:
            seen_ips.add(ip)
            unique_ips.append(ip)

    scan_status.update({
        "running": True,
        "progress": 0,
        "total": len(unique_ips),
        "found": 0,
        "start_time": time.time(),
    })

    logger.info(f"Scanning {len(unique_ips)} addresses with 50 threads …")

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(probe_ip, ip): ip for ip in unique_ips}
        for i, future in enumerate(as_completed(futures)):
            scan_status["progress"] = i + 1
            try:
                device = future.result()
                if device:
                    with devices_lock:
                        devices[device["ip"]] = device
                        scan_status["found"] = len(devices)
                    logger.info(
                        f"Found: {device['name']} ({device['model']}) "
                        f"Gen{device['gen']} @ {device['ip']}"
                    )
            except Exception as exc:
                logger.debug(f"Probe error: {exc}")

    scan_status["running"] = False
    elapsed = time.time() - scan_status["start_time"]
    logger.info(f"Scan complete: {scan_status['found']} device(s) in {elapsed:.1f}s")


def run_mdns_discovery():
    """Background thread: discover Shelly devices via mDNS (zeroconf)."""
    if not ZEROCONF_AVAILABLE:
        return

    logger.info("mDNS discovery started …")
    probed: set = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change != ServiceStateChange.Added:
            return
        info = zeroconf.get_service_info(service_type, name)
        if not info:
            return
        for addr_bytes in info.addresses:
            try:
                addr = socket.inet_ntoa(addr_bytes)
            except Exception:
                continue
            if addr and addr not in probed:
                probed.add(addr)
                device = probe_ip(addr)
                if device:
                    with devices_lock:
                        devices[addr] = device
                    logger.info(f"mDNS found: {device['name']} @ {addr}")

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_http._tcp.local.", handlers=[on_change])
        ServiceBrowser(zc, "_shelly._tcp.local.", handlers=[on_change])
        time.sleep(15)
    finally:
        zc.close()

    logger.info("mDNS discovery complete.")

# ─── Shelly actions ─────────────────────────────────────────────────────────────

def action_update(ip: str) -> dict:
    """Trigger firmware update on a device."""
    device = devices.get(ip)
    if not device:
        return {"success": False, "error": "Device not found"}
    try:
        if device["gen"] == 1:
            r = requests.get(f"http://{ip}/ota?update=1", timeout=10, verify=False)
            success = r.status_code == 200
            response_text = r.text[:300]
        else:
            payload = {"id": 1, "method": "Shelly.Update", "params": {"stage": "stable"}}
            r = requests.post(
                f"http://{ip}/rpc/Shelly.Update",
                json=payload,
                timeout=10,
                verify=False,
            )
            result = r.json()
            success = "error" not in result
            response_text = r.text[:300]

        if success:
            with devices_lock:
                if ip in devices:
                    devices[ip]["update_status"] = "updating"
        return {"success": success, "response": response_text}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def action_set_wifi(ip: str, ssid: str, password: str) -> dict:
    """Push new WiFi credentials to a device."""
    device = devices.get(ip)
    if not device:
        return {"success": False, "error": "Device not found"}
    try:
        if device["gen"] == 1:
            r = requests.post(
                f"http://{ip}/settings/sta",
                data={"ssid": ssid, "key": password, "enabled": 1},
                timeout=10,
                verify=False,
            )
            success = r.status_code == 200
            response_text = r.text[:300]
        else:
            payload = {
                "id": 1,
                "method": "WiFi.SetConfig",
                "params": {
                    "config": {
                        "sta": {"ssid": ssid, "pass": password, "enable": True}
                    }
                },
            }
            r = requests.post(
                f"http://{ip}/rpc/WiFi.SetConfig",
                json=payload,
                timeout=10,
                verify=False,
            )
            result = r.json()
            success = "error" not in result
            response_text = r.text[:300]

        return {"success": success, "response": response_text}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def action_refresh(ip: str) -> Optional[dict]:
    """Re-query a device to get fresh firmware/update info."""
    device = probe_ip(ip)
    if device:
        with devices_lock:
            prev = devices.get(ip, {})
            device["update_status"] = prev.get("update_status")
            devices[ip] = device
    return device


def action_reboot(ip: str) -> dict:
    """Send reboot command to a device."""
    device = devices.get(ip)
    if not device:
        return {"success": False, "error": "Device not found"}
    try:
        if device["gen"] == 1:
            r = requests.get(f"http://{ip}/reboot", timeout=10, verify=False)
            success = r.status_code == 200
        else:
            payload = {"id": 1, "method": "Shelly.Reboot", "params": {}}
            r = requests.post(
                f"http://{ip}/rpc/Shelly.Reboot",
                json=payload,
                timeout=10,
                verify=False,
            )
            result = r.json()
            success = "error" not in result
        return {"success": success}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

# ─── Scheduler ───────────────────────────────────────────────────────────────────

def scheduler_loop():
    """Background thread: runs auto-restart and auto-update on schedule."""
    logger.info("Scheduler started.")
    while True:
        try:
            now = datetime.datetime.now()

            with settings_lock:
                restart_en   = settings["auto_restart_enabled"]
                restart_time = settings["auto_restart_time"]
                update_en    = settings["auto_update_enabled"]
                update_hours = int(settings["auto_update_hours"])
                last_restart_day  = settings["_last_restart_day"]
                last_auto_update  = settings["_last_auto_update"]

            # ── Auto restart ────────────────────────────────────────────────────
            if restart_en and restart_time and ":" in restart_time:
                try:
                    h, m = map(int, restart_time.split(":"))
                    today = now.date()
                    if now.hour == h and now.minute == m and today != last_restart_day:
                        with settings_lock:
                            settings["_last_restart_day"] = today
                        with devices_lock:
                            ips = list(devices.keys())
                        logger.info(f"Auto-restart: rebooting {len(ips)} device(s) at {restart_time}")
                        for ip in ips:
                            result = action_reboot(ip)
                            logger.info(f"  Reboot {ip}: {result}")
                except (ValueError, AttributeError) as e:
                    logger.error(f"Auto-restart time parse error: {e}")

            # ── Auto update ─────────────────────────────────────────────────────
            if update_en and update_hours > 0:
                if time.time() - last_auto_update >= update_hours * 3600:
                    with settings_lock:
                        settings["_last_auto_update"] = time.time()
                    with devices_lock:
                        upd_ips = [ip for ip, d in devices.items() if d.get("has_update")]
                    if upd_ips:
                        logger.info(f"Auto-update: updating {len(upd_ips)} device(s)")
                        for ip in upd_ips:
                            result = action_update(ip)
                            logger.info(f"  Update {ip}: {result}")
                    else:
                        logger.info("Auto-update: no updates available, skipping.")

        except Exception as e:
            logger.error(f"Scheduler error: {e}")

        time.sleep(30)  # Check every 30 seconds

# ─── Flask API routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/devices")
def api_devices():
    with devices_lock:
        device_list = list(devices.values())
    device_list.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    return jsonify(device_list)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if scan_status["running"]:
        return jsonify({"success": False, "error": "Scan already running"})

    with devices_lock:
        devices.clear()

    if ZEROCONF_AVAILABLE:
        threading.Thread(target=run_mdns_discovery, daemon=True).start()

    threading.Thread(target=run_network_scan, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/scan/status")
def api_scan_status():
    status = dict(scan_status)
    if status["total"] > 0:
        status["percent"] = int(status["progress"] / status["total"] * 100)
    else:
        status["percent"] = 0
    return jsonify(status)


@app.route("/api/device/<ip>/update", methods=["POST"])
def api_update_device(ip):
    return jsonify(action_update(ip))


@app.route("/api/update/all", methods=["POST"])
def api_update_all():
    with devices_lock:
        update_ips = [ip for ip, d in devices.items() if d.get("has_update")]
    results = {ip: action_update(ip) for ip in update_ips}
    return jsonify({"results": results, "count": len(update_ips)})


@app.route("/api/device/<ip>/refresh", methods=["POST"])
def api_refresh_device(ip):
    device = action_refresh(ip)
    if device:
        return jsonify({"success": True, "device": device})
    return jsonify({"success": False, "error": "Device not reachable"})


@app.route("/api/wifi", methods=["POST"])
def api_set_wifi():
    data = request.get_json() or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    target_ips = data.get("ips")  # None → all devices

    if not ssid:
        return jsonify({"success": False, "error": "SSID required"})

    with devices_lock:
        ips = [ip for ip in (target_ips or list(devices.keys())) if ip in devices]

    results = {ip: action_set_wifi(ip, ssid, password) for ip in ips}
    return jsonify({"results": results, "count": len(ips)})


@app.route("/api/device/<ip>/reboot", methods=["POST"])
def api_reboot_device(ip):
    return jsonify(action_reboot(ip))


@app.route("/api/reboot/all", methods=["POST"])
def api_reboot_all():
    with devices_lock:
        ips = list(devices.keys())
    results = {ip: action_reboot(ip) for ip in ips}
    return jsonify({"results": results, "count": len(ips)})


@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    with settings_lock:
        data = {k: v for k, v in settings.items() if not k.startswith("_")}
    # Compute next auto-update timestamp for UI display
    with settings_lock:
        last_upd = settings["_last_auto_update"]
        upd_hours = int(settings["auto_update_hours"])
        upd_en = settings["auto_update_enabled"]
    if upd_en and upd_hours > 0 and last_upd > 0:
        next_ts = last_upd + upd_hours * 3600
        data["_next_auto_update"] = datetime.datetime.fromtimestamp(next_ts).strftime("%d.%m.%Y %H:%M")
    elif upd_en and upd_hours > 0:
        data["_next_auto_update"] = "—"
    else:
        data["_next_auto_update"] = None
    return jsonify(data)


@app.route("/api/settings", methods=["POST"])
def api_post_settings():
    data = request.get_json() or {}
    with settings_lock:
        if "auto_restart_enabled" in data:
            settings["auto_restart_enabled"] = bool(data["auto_restart_enabled"])
        if "auto_restart_time" in data:
            t_val = str(data["auto_restart_time"]).strip()
            # Validate HH:MM format
            try:
                h, m = map(int, t_val.split(":"))
                if 0 <= h <= 23 and 0 <= m <= 59:
                    settings["auto_restart_time"] = f"{h:02d}:{m:02d}"
            except (ValueError, AttributeError):
                pass
        if "auto_update_enabled" in data:
            settings["auto_update_enabled"] = bool(data["auto_update_enabled"])
        if "auto_update_hours" in data:
            try:
                settings["auto_update_hours"] = max(1, int(data["auto_update_hours"]))
            except (ValueError, TypeError):
                pass
    save_settings()
    return jsonify({"success": True})

# ─── HTML / CSS / JS ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shelly Manager</title>
<style>
/* ══════════════════════════════════════════
   DARK THEME  (default)
══════════════════════════════════════════ */
:root,
[data-theme="dark"] {
  --bg:        #0d1017;
  --card:      #161920;
  --card2:     #1c2030;
  --border:    #252932;
  --hover:     #1c2030;
  --accent:    #e8a000;
  --accent2:   #f0c040;
  --text:      #dce0ec;
  --dim:       #7880a0;
  --red:       #e84040;
  --green:     #38c870;
  --blue:      #4090e8;
  --purple:    #a060e8;
  --shadow:    0 4px 24px rgba(0,0,0,.45);
  --th-bg:     rgba(255,255,255,.03);
  --overlay:   rgba(0,0,0,.78);
  --btn-ghost-bg: #252932;
  --radius:    10px;
  --font:      system-ui, -apple-system, sans-serif;
}

/* ══════════════════════════════════════════
   LIGHT THEME
══════════════════════════════════════════ */
[data-theme="light"] {
  --bg:        #f0f3f9;
  --card:      #ffffff;
  --card2:     #f5f7fc;
  --border:    #dce1ed;
  --hover:     #f0f4fc;
  --accent:    #c47800;
  --accent2:   #e09000;
  --text:      #1a1d2e;
  --dim:       #5d6890;
  --red:       #d03030;
  --green:     #1fa855;
  --blue:      #2878d0;
  --purple:    #7040c8;
  --shadow:    0 4px 24px rgba(0,0,0,.12);
  --th-bg:     rgba(0,0,0,.03);
  --overlay:   rgba(80,90,120,.55);
  --btn-ghost-bg: #e4e8f2;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  min-height: 100vh;
  font-size: 14px;
  transition: background .25s, color .25s;
}

/* ── Header ────────────────────────────── */
header {
  background: var(--card);
  border-bottom: 1px solid var(--border);
  padding: 12px 22px;
  display: flex;
  align-items: center;
  gap: 12px;
  position: sticky;
  top: 0;
  z-index: 50;
  box-shadow: var(--shadow);
}
.logo { font-size: 1.25rem; font-weight: 800; color: var(--accent); letter-spacing: .4px; }
.logo em { color: var(--dim); font-style: normal; font-weight: 400; font-size: .88rem; margin-left: 3px; }

.header-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }

.device-count { color: var(--dim); font-size: .82rem; padding: 0 6px; }

.mdns-badge {
  font-size: .7rem; padding: 2px 8px; border-radius: 20px;
  background: rgba(56,200,112,.15); color: var(--green);
  border: 1px solid rgba(56,200,112,.3);
}

/* ── Icon buttons (theme / lang) ────────── */
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  padding: 6px 11px;
  background: var(--btn-ghost-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text);
  cursor: pointer;
  font-size: .8rem;
  font-weight: 600;
  transition: background .15s, border-color .15s, opacity .15s;
  white-space: nowrap;
}
.icon-btn:hover { opacity: .8; }
.icon-btn svg { flex-shrink: 0; }

/* ── Layout ─────────────────────────────── */
main { max-width: 1400px; margin: 0 auto; padding: 22px; }

/* ── Buttons ────────────────────────────── */
button {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 16px;
  border: none; border-radius: 8px;
  cursor: pointer; font-size: .875rem; font-weight: 600;
  transition: opacity .15s, transform .1s;
  white-space: nowrap;
}
button:hover  { opacity: .85; }
button:active { transform: scale(.97); }
button:disabled { opacity: .3; cursor: not-allowed; pointer-events: none; }

.btn-primary { background: var(--accent);  color: #fff; }
.btn-success { background: var(--green);   color: #fff; }
.btn-blue    { background: var(--blue);    color: #fff; }
.btn-ghost   { background: var(--btn-ghost-bg); color: var(--text); border: 1px solid var(--border); }
.btn-sm      { padding: 4px 10px; font-size: .78rem; border-radius: 6px; }

/* ── Toolbar ────────────────────────────── */
.toolbar {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  margin-bottom: 16px;
  padding: 14px 16px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.toolbar-sep { width: 1px; height: 28px; background: var(--border); margin: 0 4px; }

/* ── Progress ───────────────────────────── */
.progress-wrap { margin-bottom: 14px; }
.progress-bar  {
  height: 3px; background: var(--border);
  border-radius: 2px; overflow: hidden; display: none;
}
.progress-bar.visible { display: block; }
.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent2));
  transition: width .4s ease; border-radius: 2px;
}
.scan-info { color: var(--dim); font-size: .8rem; margin-top: 5px; min-height: 16px; }

/* ── Table ──────────────────────────────── */
.table-wrap {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.device-table { width: 100%; border-collapse: collapse; }
.device-table th {
  padding: 9px 14px;
  text-align: left;
  background: var(--th-bg);
  color: var(--dim);
  font-size: .7rem;
  text-transform: uppercase;
  letter-spacing: .07em;
  font-weight: 700;
  border-bottom: 1px solid var(--border);
}
.device-table td {
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  vertical-align: middle;
}
.device-table tbody tr:hover td { background: var(--hover); }

/* ── Badges ─────────────────────────────── */
.badge {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 20px;
  font-size: .7rem; font-weight: 700;
}
.b-gen1  { background: rgba(64,144,232,.18); color: var(--blue); }
.b-gen2  { background: rgba(56,200,112,.18); color: var(--green); }
.b-gen3  { background: rgba(160,96,232,.18); color: var(--purple); }
.b-upd   { background: rgba(232,160,0,.18);  color: var(--accent); }
.b-ok    { background: rgba(56,200,112,.13); color: var(--green); }
.b-auth  { background: rgba(232,64,64,.13);  color: var(--red); font-size: .68rem; }

/* ── IP link ────────────────────────────── */
.ip-link {
  color: var(--accent); text-decoration: none;
  font-family: monospace; font-weight: 700; font-size: .9rem;
}
.ip-link:hover { text-decoration: underline; }

/* ── Device name ────────────────────────── */
.dev-name { font-weight: 600; }
.dev-sub  { font-size: .76rem; color: var(--dim); margin-top: 2px; }

/* ── Update animation ───────────────────── */
.upd-anim { color: var(--accent); font-size: .76rem; margin-top: 3px; }

/* ── Empty state ────────────────────────── */
.empty { text-align: center; padding: 80px 24px; color: var(--dim); }
.empty svg { opacity: .22; margin-bottom: 14px; }
.empty h2  { font-size: 1.05rem; color: var(--text); margin-bottom: 6px; }
.empty p   { font-size: .86rem; }

/* ── Modal ──────────────────────────────── */
.overlay {
  position: fixed; inset: 0;
  background: var(--overlay);
  z-index: 200;
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
  opacity: 0; pointer-events: none;
  transition: opacity .2s;
}
.overlay.open { opacity: 1; pointer-events: all; }
.modal {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 26px 26px 22px;
  width: 100%; max-width: 460px;
  box-shadow: var(--shadow);
  transform: translateY(16px);
  transition: transform .2s;
}
.overlay.open .modal { transform: translateY(0); }
.modal h2    { font-size: 1.05rem; margin-bottom: 4px; }
.modal-sub   { color: var(--dim); font-size: .8rem; margin-bottom: 18px; min-height: 16px; }

.form-group { margin-bottom: 13px; }
.form-group label {
  display: block; margin-bottom: 5px;
  color: var(--dim); font-size: .8rem; font-weight: 500;
}
.form-group input {
  width: 100%; padding: 9px 13px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text); font-size: .9rem;
  outline: none; transition: border-color .15s;
}
.form-group input:focus { border-color: var(--accent); }

.warn-box {
  background: rgba(232,160,0,.1);
  border: 1px solid rgba(232,160,0,.3);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: .78rem; color: var(--accent2);
  margin-bottom: 14px; line-height: 1.5;
}

.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }

/* ── Toast ──────────────────────────────── */
.toast {
  position: fixed; bottom: 20px; right: 20px;
  padding: 10px 18px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: .875rem; z-index: 400;
  max-width: 360px;
  box-shadow: var(--shadow);
  transform: translateY(80px); opacity: 0;
  transition: transform .3s, opacity .3s;
  pointer-events: none;
}
.toast.show { transform: translateY(0); opacity: 1; }
.toast.ok   { border-color: var(--green); }
.toast.err  { border-color: var(--red);   }

/* ── Checkbox ───────────────────────────── */
input[type=checkbox] { accent-color: var(--accent); width: 14px; height: 14px; cursor: pointer; }

/* ── Settings card ──────────────────────── */
.settings-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 16px;
  overflow: hidden;
}
.settings-head {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px;
  cursor: pointer;
  font-weight: 600; font-size: .9rem;
  user-select: none;
  transition: background .15s;
}
.settings-head:hover { background: var(--hover); }
.settings-body { padding: 0 16px 18px; }

.settings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 14px;
}
@media (max-width: 700px) { .settings-grid { grid-template-columns: 1fr; } }

.settings-block {
  background: var(--card2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.settings-block-header {
  display: flex; align-items: flex-start; gap: 12px;
  margin-bottom: 12px;
}
.settings-block-title { font-weight: 600; font-size: .9rem; margin-bottom: 3px; }
.settings-block-desc  { font-size: .76rem; color: var(--dim); line-height: 1.4; }

.settings-row {
  display: flex; flex-direction: column; gap: 5px;
}
.settings-row label { font-size: .8rem; color: var(--dim); font-weight: 500; }
.settings-row input[type=time],
.settings-row input[type=number] {
  padding: 7px 10px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text);
  font-size: .9rem;
  outline: none;
  width: 100%;
  max-width: 160px;
  transition: border-color .15s;
}
.settings-row input:focus { border-color: var(--accent); }
.settings-row input:disabled { opacity: .4; cursor: not-allowed; }

.settings-next {
  font-size: .76rem; color: var(--dim); margin-top: 4px;
}

.settings-footer {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  display: flex; justify-content: flex-end;
}

/* ── Toggle switch ──────────────────────── */
.toggle-wrap {
  position: relative;
  display: inline-block;
  width: 36px; height: 20px;
  flex-shrink: 0;
  margin-top: 2px;
  cursor: pointer;
}
.toggle-wrap input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute; inset: 0;
  background: var(--border);
  border-radius: 20px;
  transition: background .2s;
}
.toggle-slider::before {
  content: '';
  position: absolute;
  width: 14px; height: 14px;
  left: 3px; bottom: 3px;
  background: #fff;
  border-radius: 50%;
  transition: transform .2s;
}
.toggle-wrap input:checked + .toggle-slider { background: var(--accent); }
.toggle-wrap input:checked + .toggle-slider::before { transform: translateX(16px); }

/* ── Animations ─────────────────────────── */
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 800px) {
  .device-table th:nth-child(4), .device-table td:nth-child(4),
  .device-table th:nth-child(5), .device-table td:nth-child(5) { display: none; }
  main { padding: 12px; }
  .toolbar-sep { display: none; }
}
</style>
</head>
<body>

<!-- ═══════════════════════════ HEADER ═══════════════════════════ -->
<header>
  <div class="logo">Shelly<em>Manager</em></div>
  {% if mdns_available %}<span class="mdns-badge" id="mdns-lbl">mDNS</span>{% endif %}

  <div class="header-right">
    <span class="device-count" id="hdr-count">–</span>

    <!-- Language toggle -->
    <button class="icon-btn" id="btn-lang" onclick="toggleLang()" title="Switch language">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path d="M12 2a10 10 0 1 0 0 20A10 10 0 0 0 12 2z"/>
        <path d="M2 12h20M12 2c-3 4-3 14 0 20M12 2c3 4 3 14 0 20"/>
      </svg>
      <span id="lang-label">EN</span>
    </button>

    <!-- Theme toggle -->
    <button class="icon-btn" id="btn-theme" onclick="toggleTheme()" title="Toggle theme">
      <svg id="theme-icon-dark" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
      <svg id="theme-icon-light" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" style="display:none">
        <circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
      </svg>
      <span id="theme-label">Hell</span>
    </button>
  </div>
</header>

<!-- ═══════════════════════════ MAIN ════════════════════════════ -->
<main>
  <!-- Toolbar -->
  <div class="toolbar">
    <button class="btn-primary" id="btn-scan" onclick="startScan()">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
      <span data-i18n="btnScan">Netzwerk scannen</span>
    </button>

    <div class="toolbar-sep"></div>

    <button class="btn-success" id="btn-upd-all" onclick="updateAll()">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
      <span id="upd-all-label" data-i18n="btnUpdateAll">Alle aktualisieren</span>
    </button>

    <div class="toolbar-sep"></div>

    <button class="btn-blue" id="btn-wifi-all" onclick="openWifi('all')" disabled>
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/></svg>
      <span data-i18n="btnWifi">WLAN konfigurieren</span>
    </button>
    <button class="btn-blue" id="btn-wifi-sel" onclick="openWifi('sel')" style="display:none">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/></svg>
      <span id="wifi-sel-label"></span>
    </button>

    <div class="toolbar-sep"></div>

    <button class="btn-ghost" id="btn-reboot-all" onclick="rebootAll()" disabled>
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
      <span data-i18n="btnRebootAll">Alle neustarten</span>
    </button>
  </div>

  <!-- ══════════════ SETTINGS CARD ══════════════ -->
  <div class="settings-card" id="settings-card">
    <div class="settings-head" onclick="toggleSettings()">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
      <span data-i18n="settingsTitle">Einstellungen</span>
      <svg id="s-chevron" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24" style="margin-left:auto;transition:transform .2s"><path d="m6 9 6 6 6-6"/></svg>
    </div>
    <div class="settings-body" id="settings-body" style="display:none">
      <div class="settings-grid">

        <!-- Auto-Neustart -->
        <div class="settings-block">
          <div class="settings-block-header">
            <label class="toggle-wrap">
              <input type="checkbox" id="s-restart-en" onchange="onToggleRestart(this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <div>
              <div class="settings-block-title" data-i18n="settingsRestart">Auto-Neustart</div>
              <div class="settings-block-desc" data-i18n="settingsRestartDesc">Alle Geräte täglich zur eingestellten Zeit neu starten</div>
            </div>
          </div>
          <div class="settings-row" id="s-restart-row">
            <label data-i18n="settingsRestartAt">Uhrzeit</label>
            <input type="time" id="s-restart-time" value="03:00">
          </div>
        </div>

        <!-- Auto-Update -->
        <div class="settings-block">
          <div class="settings-block-header">
            <label class="toggle-wrap">
              <input type="checkbox" id="s-update-en" onchange="onToggleUpdate(this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <div>
              <div class="settings-block-title" data-i18n="settingsUpdate">Auto-Update</div>
              <div class="settings-block-desc" data-i18n="settingsUpdateDesc">Geräte automatisch aktualisieren wenn ein Update verfügbar ist</div>
            </div>
          </div>
          <div class="settings-row" id="s-update-row">
            <label data-i18n="settingsInterval">Intervall (Stunden)</label>
            <div style="display:flex;align-items:center;gap:8px">
              <input type="number" id="s-update-hours" min="1" max="720" value="24" style="width:80px">
              <span style="color:var(--dim);font-size:.8rem">h</span>
            </div>
            <div class="settings-next" id="s-next-update"></div>
          </div>
        </div>

      </div>
      <div class="settings-footer">
        <button class="btn-primary btn-sm" onclick="saveSettings()">
          <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
          <span data-i18n="settingsSave">Speichern</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Progress -->
  <div class="progress-wrap">
    <div class="progress-bar" id="prog-bar">
      <div class="progress-fill" id="prog-fill" style="width:0"></div>
    </div>
    <div class="scan-info" id="scan-info"></div>
  </div>

  <!-- Device list -->
  <div id="device-wrap"></div>
</main>

<!-- ══════════════════════════ WIFI MODAL ═══════════════════════ -->
<div class="overlay" id="wifi-overlay" onclick="overlayClick(event)">
  <div class="modal">
    <h2 data-i18n="wifiTitle">WLAN-Zugangsdaten ändern</h2>
    <div class="modal-sub" id="wifi-scope"></div>
    <div class="warn-box" data-i18n="wifiWarn">
      ⚠ Das Gerät startet nach der Änderung neu und verbindet sich mit dem neuen Netzwerk. Stellen Sie sicher, dass die Zugangsdaten korrekt sind.
    </div>
    <div class="form-group">
      <label for="f-ssid" data-i18n="wifiLabelSSID">Netzwerkname (SSID)</label>
      <input id="f-ssid" type="text" autocomplete="off" spellcheck="false">
    </div>
    <div class="form-group">
      <label for="f-pass" data-i18n="wifiLabelPass">Passwort</label>
      <input id="f-pass" type="password" autocomplete="new-password">
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeWifi()" data-i18n="wifiBtnCancel">Abbrechen</button>
      <button class="btn-primary" onclick="applyWifi()" data-i18n="wifiBtnApply">Übernehmen</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ══════════════════════════════════════════════════════════════════
//  i18n
// ══════════════════════════════════════════════════════════════════
const STRINGS = {
  de: {
    btnScan:       'Netzwerk scannen',
    btnScanRun:    'Scanning…',
    btnUpdateAll:  'Alle aktualisieren',
    btnUpdateAllN: n => `Alle aktualisieren (${n})`,
    btnWifi:       'WLAN konfigurieren',
    btnWifiSel:    n => `WLAN (${n})`,
    deviceCount:   n => `${n} Gerät(e)`,
    mdns:          'mDNS aktiv',
    scanProg:      (p,t,pct,f) => `${p} / ${t} IPs geprüft (${pct}%) — ${f} Gerät(e) gefunden`,
    scanDone:      f => `Scan abgeschlossen — ${f} Gerät(e) gefunden`,
    thDevice:      'Gerät',
    thIP:          'IP-Adresse',
    thGen:         'Generation',
    thFw:          'Firmware',
    thUpdate:      'Update-Status',
    thActions:     'Aktionen',
    emptyTitle:    'Keine Geräte gefunden',
    emptyMsg:      'Klicken Sie auf <strong>Netzwerk scannen</strong>, um Shelly-Geräte zu suchen.',
    badgeOk:       '✓ Aktuell',
    badgeUpdating: '⟳ Aktualisierung…',
    btnUpdate:     'Update',
    btnRefreshTip: 'Status aktualisieren',
    btnWifiTip:    'WLAN konfigurieren',
    btnWifiRow:    '⊛ WLAN',
    wifiTitle:     'WLAN-Zugangsdaten ändern',
    wifiScopeOne:  (n,ip) => `Gerät: ${n} (${ip})`,
    wifiScopeSel:  n => `${n} ausgewählte(s) Gerät(e)`,
    wifiScopeAll:  n => `Alle ${n} Gerät(e)`,
    wifiWarn:      '⚠ Das Gerät startet nach der Änderung neu und verbindet sich mit dem neuen Netzwerk. Stellen Sie sicher, dass die Zugangsdaten korrekt sind.',
    wifiLabelSSID: 'Netzwerkname (SSID)',
    wifiPlSSID:    'MeinHeimnetz',
    wifiLabelPass: 'Passwort',
    wifiBtnCancel: 'Abbrechen',
    wifiBtnApply:  'Übernehmen',
    toastScanErr:  e => `Fehler: ${e}`,
    toastScanDone: f => `Scan abgeschlossen: ${f} Gerät(e) gefunden`,
    toastUpdStart: ip => `Update für ${ip} gestartet`,
    toastUpdErr:   (ip,e) => `Fehler bei ${ip}: ${e}`,
    toastUpdAll:   (ok,f) => f===0 ? `${ok} Update(s) gestartet` : `${ok} ok, ${f} fehlgeschlagen`,
    toastRefOk:    ip => `${ip} aktualisiert`,
    toastRefErr:   ip => `${ip} nicht erreichbar`,
    toastWifiOk:   n => `WLAN auf ${n} Gerät(en) gesetzt`,
    toastWifiErr:  (ok,f) => `${ok} ok, ${f} Fehler`,
    toastWifiSSID: 'Bitte SSID eingeben',
    // settings
    settingsTitle:       'Einstellungen',
    settingsRestart:     'Auto-Neustart',
    settingsRestartDesc: 'Alle Geräte täglich zur eingestellten Zeit neu starten',
    settingsRestartAt:   'Uhrzeit',
    settingsUpdate:      'Auto-Update',
    settingsUpdateDesc:  'Geräte automatisch aktualisieren wenn ein Update verfügbar ist',
    settingsInterval:    'Intervall (Stunden)',
    settingsSave:        'Speichern',
    settingsDisabled:    '0 = deaktiviert',
    settingsNextUpdate:  'Nächstes Update',
    settingsLastUpdate:  'Letztes Update',
    settingsNever:       'Noch nie',
    settingsSaved:       'Einstellungen gespeichert',
    // reboot
    btnRebootAll:        'Alle neustarten',
    btnReboot:           '↺ Neustart',
    btnRebootTip:        'Gerät neu starten',
    toastRebootOne:      ip => `Neustart gesendet an ${ip}`,
    toastRebootErr:      (ip, e) => `Neustart Fehler ${ip}: ${e}`,
    toastRebootAll:      (ok, f) => f === 0 ? `${ok} Gerät(e) neu gestartet` : `${ok} ok, ${f} Fehler`,
    themeSwitchTo: 'Hell',
    langSwitch:    'EN',
  },
  en: {
    btnScan:       'Scan Network',
    btnScanRun:    'Scanning…',
    btnUpdateAll:  'Update All',
    btnUpdateAllN: n => `Update All (${n})`,
    btnWifi:       'Configure WiFi',
    btnWifiSel:    n => `WiFi (${n})`,
    deviceCount:   n => `${n} Device(s)`,
    mdns:          'mDNS active',
    scanProg:      (p,t,pct,f) => `${p} / ${t} IPs checked (${pct}%) — ${f} device(s) found`,
    scanDone:      f => `Scan complete — ${f} device(s) found`,
    thDevice:      'Device',
    thIP:          'IP Address',
    thGen:         'Generation',
    thFw:          'Firmware',
    thUpdate:      'Update Status',
    thActions:     'Actions',
    emptyTitle:    'No devices found',
    emptyMsg:      'Click <strong>Scan Network</strong> to search for Shelly devices.',
    badgeOk:       '✓ Up to date',
    badgeUpdating: '⟳ Updating…',
    btnUpdate:     'Update',
    btnRefreshTip: 'Refresh status',
    btnWifiTip:    'Configure WiFi',
    btnWifiRow:    '⊛ WiFi',
    wifiTitle:     'Change WiFi Credentials',
    wifiScopeOne:  (n,ip) => `Device: ${n} (${ip})`,
    wifiScopeSel:  n => `${n} selected device(s)`,
    wifiScopeAll:  n => `All ${n} device(s)`,
    wifiWarn:      '⚠ The device will restart and connect to the new network. Make sure the credentials are correct.',
    wifiLabelSSID: 'Network name (SSID)',
    wifiPlSSID:    'MyHomeNetwork',
    wifiLabelPass: 'Password',
    wifiBtnCancel: 'Cancel',
    wifiBtnApply:  'Apply',
    toastScanErr:  e => `Error: ${e}`,
    toastScanDone: f => `Scan complete: ${f} device(s) found`,
    toastUpdStart: ip => `Update started for ${ip}`,
    toastUpdErr:   (ip,e) => `Error at ${ip}: ${e}`,
    toastUpdAll:   (ok,f) => f===0 ? `${ok} update(s) started` : `${ok} ok, ${f} failed`,
    toastRefOk:    ip => `${ip} refreshed`,
    toastRefErr:   ip => `${ip} not reachable`,
    toastWifiOk:   n => `WiFi set on ${n} device(s)`,
    toastWifiErr:  (ok,f) => `${ok} ok, ${f} error(s)`,
    toastWifiSSID: 'Please enter SSID',
    // settings
    settingsTitle:       'Settings',
    settingsRestart:     'Auto-Restart',
    settingsRestartDesc: 'Restart all devices daily at the configured time',
    settingsRestartAt:   'Time',
    settingsUpdate:      'Auto-Update',
    settingsUpdateDesc:  'Automatically update devices when an update is available',
    settingsInterval:    'Interval (hours)',
    settingsSave:        'Save',
    settingsDisabled:    '0 = disabled',
    settingsNextUpdate:  'Next update',
    settingsNever:       'Never',
    settingsSaved:       'Settings saved',
    // reboot
    btnRebootAll:   'Restart all',
    btnReboot:      '↺ Restart',
    btnRebootTip:   'Restart device',
    toastRebootOne: ip => `Restart sent to ${ip}`,
    toastRebootErr: (ip, e) => `Reboot error ${ip}: ${e}`,
    toastRebootAll: (ok, f) => f === 0 ? `${ok} device(s) restarted` : `${ok} ok, ${f} error(s)`,
    themeSwitchTo: 'Dark',
    langSwitch:    'DE',
  }
};

// ══════════════════════════════════════════════════════════════════
//  State
// ══════════════════════════════════════════════════════════════════
let lang       = localStorage.getItem('shelly-lang')  || 'de';
let theme      = localStorage.getItem('shelly-theme') || 'dark';
let allDevices = [];
let selected   = new Set();
let wifiTarget = null;
let pollTimer  = null;

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const t = key => STRINGS[lang][key];

// ══════════════════════════════════════════════════════════════════
//  Theme
// ══════════════════════════════════════════════════════════════════
function applyTheme(th) {
  theme = th;
  document.documentElement.setAttribute('data-theme', th);
  localStorage.setItem('shelly-theme', th);
  const isDark = th === 'dark';
  $('theme-icon-dark').style.display  = isDark ? 'block' : 'none';
  $('theme-icon-light').style.display = isDark ? 'none'  : 'block';
  $('theme-label').textContent = isDark ? t('themeSwitchTo') : t('themeSwitchTo');
}

function toggleTheme() {
  applyTheme(theme === 'dark' ? 'light' : 'dark');
  // update label after lang may have changed
  $('theme-label').textContent = t('themeSwitchTo');
}

// ══════════════════════════════════════════════════════════════════
//  Language
// ══════════════════════════════════════════════════════════════════
function applyLang(l) {
  lang = l;
  localStorage.setItem('shelly-lang', l);
  document.documentElement.lang = l;

  // Update static data-i18n elements
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.dataset.i18n;
    if (STRINGS[lang][key] && typeof STRINGS[lang][key] === 'string') {
      el.textContent = STRINGS[lang][key];
    }
  });

  // SSID placeholder
  const ssidInput = $('f-ssid');
  if (ssidInput) ssidInput.placeholder = t('wifiPlSSID');

  // Re-apply settings labels if open
  if (settingsOpen) loadSettings();

  // Header controls
  $('lang-label').textContent  = t('langSwitch');
  $('theme-label').textContent = t('themeSwitchTo');

  const mdnsEl = $('mdns-lbl');
  if (mdnsEl) mdnsEl.textContent = t('mdns');

  // Re-render dynamic content
  renderDevices();
}

function toggleLang() { applyLang(lang === 'de' ? 'en' : 'de'); }

// ══════════════════════════════════════════════════════════════════
//  Utilities
// ══════════════════════════════════════════════════════════════════
function showToast(msg, type = 'ok') {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 4500);
}

async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: {} };
  if (body) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  try { return await (await fetch(path, opts)).json(); }
  catch (e) { return { success: false, error: e.message }; }
}

// ══════════════════════════════════════════════════════════════════
//  Scan
// ══════════════════════════════════════════════════════════════════
async function startScan() {
  const btn = $('btn-scan');
  btn.disabled = true;
  btn.innerHTML = `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24" style="animation:spin .8s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> ${t('btnScanRun')}`;

  allDevices = []; selected.clear(); renderDevices();

  const res = await api('/api/scan', 'POST');
  if (!res.success) {
    showToast(t('toastScanErr')(res.error || '?'), 'err');
    btn.disabled = false; btn.innerHTML = scanBtnHTML(); return;
  }

  $('prog-bar').classList.add('visible');
  pollTimer = setInterval(pollScan, 700);
}

function scanBtnHTML() {
  return `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> ${t('btnScan')}`;
}

async function pollScan() {
  const s = await api('/api/scan/status');
  $('prog-fill').style.width = (s.percent || 0) + '%';
  $('scan-info').textContent = s.running
    ? t('scanProg')(s.progress, s.total, s.percent, s.found)
    : t('scanDone')(s.found);

  await loadDevices();

  if (!s.running) {
    clearInterval(pollTimer); pollTimer = null;
    $('prog-bar').classList.remove('visible');
    const btn = $('btn-scan');
    btn.disabled = false; btn.innerHTML = scanBtnHTML();
    showToast(t('toastScanDone')(s.found));
  }
}

async function loadDevices() {
  allDevices = await api('/api/devices');
  renderDevices();
}

// ══════════════════════════════════════════════════════════════════
//  Render
// ══════════════════════════════════════════════════════════════════
function renderDevices() {
  const updatable = allDevices.filter(d => d.has_update).length;

  // Header count
  $('hdr-count').textContent = t('deviceCount')(allDevices.length);

  // Update-All button — always visible, label shows count when > 0
  const updAllBtn = $('btn-upd-all');
  updAllBtn.disabled = updatable === 0;
  $('upd-all-label').textContent = updatable > 0
    ? t('btnUpdateAllN')(updatable)
    : t('btnUpdateAll');

  // WiFi + reboot buttons
  $('btn-wifi-all').disabled   = allDevices.length === 0;
  $('btn-reboot-all').disabled = allDevices.length === 0;

  // Device table
  const wrap = $('device-wrap');
  if (allDevices.length === 0) {
    wrap.innerHTML = `<div class="table-wrap"><div class="empty">
      <svg width="60" height="60" fill="none" stroke="currentColor" stroke-width="1.3" viewBox="0 0 24 24">
        <path d="M5 12.55a11 11 0 0 1 14.08 0M1.42 9a16 16 0 0 1 21.16 0M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/>
      </svg>
      <h2>${t('emptyTitle')}</h2>
      <p>${t('emptyMsg')}</p>
    </div></div>`;
    return;
  }

  const rows = allDevices.map(d => {
    const gc   = d.gen === 1 ? 'b-gen1' : d.gen === 3 ? 'b-gen3' : 'b-gen2';
    const updB = d.has_update
      ? `<span class="badge b-upd" title="${esc(d.new_version||'?')}">↑ ${esc(d.new_version||'Update')}</span>`
      : `<span class="badge b-ok">${t('badgeOk')}</span>`;
    const updBtn = d.has_update
      ? `<button class="btn-success btn-sm" onclick="updateOne('${d.ip}')">${t('btnUpdate')}</button>` : '';
    const updSt = d.update_status === 'updating'
      ? `<div class="upd-anim">${t('badgeUpdating')}</div>` : '';
    const auth = d.auth ? `<span class="badge b-auth" title="Auth">🔒</span>` : '';
    const chk  = selected.has(d.ip) ? 'checked' : '';

    return `<tr>
      <td style="width:38px"><input type="checkbox" ${chk} onchange="toggleSel('${d.ip}',this.checked)"></td>
      <td>
        <div class="dev-name">${esc(d.name)} ${auth}</div>
        <div class="dev-sub">${esc(d.model)} · ${esc(d.mac)}</div>
      </td>
      <td><a class="ip-link" href="http://${d.ip}" target="_blank">${d.ip}</a></td>
      <td><span class="badge ${gc}">Gen ${d.gen}</span></td>
      <td style="font-family:monospace;font-size:.82rem">${esc(d.firmware)}</td>
      <td>${updB}${updSt}</td>
      <td><div style="display:flex;gap:5px;flex-wrap:wrap">
        ${updBtn}
        <button class="btn-ghost btn-sm" onclick="refreshOne('${d.ip}')" title="${t('btnRefreshTip')}">↻</button>
        <button class="btn-blue btn-sm" onclick="openWifi('one','${d.ip}')" title="${t('btnWifiTip')}">${t('btnWifiRow')}</button>
        <button class="btn-ghost btn-sm" onclick="rebootOne('${d.ip}')" title="${t('btnRebootTip')}">${t('btnReboot')}</button>
      </div></td>
    </tr>`;
  }).join('');

  const allChk = allDevices.length > 0 && allDevices.every(d => selected.has(d.ip));
  wrap.innerHTML = `<div class="table-wrap"><table class="device-table">
    <thead><tr>
      <th><input type="checkbox" ${allChk?'checked':''} onchange="toggleAll(this.checked)"></th>
      <th>${t('thDevice')}</th><th>${t('thIP')}</th><th>${t('thGen')}</th>
      <th>${t('thFw')}</th><th>${t('thUpdate')}</th><th>${t('thActions')}</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;

  updateSelBtn();
}

function toggleSel(ip, on) {
  on ? selected.add(ip) : selected.delete(ip);
  updateSelBtn();
  const allChk = allDevices.length > 0 && allDevices.every(d => selected.has(d.ip));
  const hdr = document.querySelector('.device-table thead input[type=checkbox]');
  if (hdr) hdr.checked = allChk;
}

function toggleAll(on) {
  allDevices.forEach(d => on ? selected.add(d.ip) : selected.delete(d.ip));
  renderDevices();
}

function updateSelBtn() {
  const btn = $('btn-wifi-sel');
  if (selected.size > 0) {
    btn.style.display = 'inline-flex';
    $('wifi-sel-label').textContent = t('btnWifiSel')(selected.size);
  } else {
    btn.style.display = 'none';
  }
}

// ══════════════════════════════════════════════════════════════════
//  Actions
// ══════════════════════════════════════════════════════════════════
async function updateOne(ip) {
  const d = allDevices.find(x => x.ip === ip);
  if (d) { d.update_status = 'updating'; renderDevices(); }

  const res = await api(`/api/device/${ip}/update`, 'POST');
  if (res.success) {
    showToast(t('toastUpdStart')(ip));
  } else {
    showToast(t('toastUpdErr')(ip, res.error || res.response || '?'), 'err');
    if (d) { d.update_status = null; renderDevices(); }
  }
}

async function updateAll() {
  $('btn-upd-all').disabled = true;
  const res  = await api('/api/update/all', 'POST');
  const ok   = Object.values(res.results || {}).filter(r => r.success).length;
  const fail = res.count - ok;
  showToast(t('toastUpdAll')(ok, fail), fail ? 'err' : 'ok');
  await loadDevices();
}

async function refreshOne(ip) {
  const res = await api(`/api/device/${ip}/refresh`, 'POST');
  if (res.success) {
    const idx = allDevices.findIndex(d => d.ip === ip);
    if (idx >= 0) allDevices[idx] = res.device;
    renderDevices();
    showToast(t('toastRefOk')(ip));
  } else {
    showToast(t('toastRefErr')(ip), 'err');
  }
}

// ══════════════════════════════════════════════════════════════════
//  WiFi Modal
// ══════════════════════════════════════════════════════════════════
function openWifi(mode, ip = null) {
  if (mode === 'one' && ip) {
    wifiTarget = [ip];
    const d = allDevices.find(x => x.ip === ip);
    $('wifi-scope').textContent = t('wifiScopeOne')(d ? d.name : ip, ip);
  } else if (mode === 'sel') {
    wifiTarget = [...selected];
    $('wifi-scope').textContent = t('wifiScopeSel')(wifiTarget.length);
  } else {
    wifiTarget = null;
    $('wifi-scope').textContent = t('wifiScopeAll')(allDevices.length);
  }
  // Update warn-box text (may change with language)
  document.querySelector('.warn-box').textContent = t('wifiWarn');
  $('wifi-overlay').classList.add('open');
  setTimeout(() => $('f-ssid').focus(), 160);
}

function closeWifi() { $('wifi-overlay').classList.remove('open'); }

function overlayClick(e) { if (e.target === $('wifi-overlay')) closeWifi(); }

async function applyWifi() {
  const ssid = $('f-ssid').value.trim();
  const pass = $('f-pass').value;
  if (!ssid) { showToast(t('toastWifiSSID'), 'err'); return; }

  const res  = await api('/api/wifi', 'POST', { ssid, password: pass, ips: wifiTarget });
  const ok   = Object.values(res.results || {}).filter(r => r.success).length;
  const fail = res.count - ok;
  closeWifi();
  showToast(fail === 0 ? t('toastWifiOk')(ok) : t('toastWifiErr')(ok, fail), fail ? 'err' : 'ok');
}

// ══════════════════════════════════════════════════════════════════
//  Reboot
// ══════════════════════════════════════════════════════════════════
async function rebootOne(ip) {
  const res = await api(`/api/device/${ip}/reboot`, 'POST');
  if (res.success) {
    showToast(t('toastRebootOne')(ip));
  } else {
    showToast(t('toastRebootErr')(ip, res.error || '?'), 'err');
  }
}

async function rebootAll() {
  $('btn-reboot-all').disabled = true;
  const res  = await api('/api/reboot/all', 'POST');
  const ok   = Object.values(res.results || {}).filter(r => r.success).length;
  const fail = res.count - ok;
  showToast(t('toastRebootAll')(ok, fail), fail ? 'err' : 'ok');
  $('btn-reboot-all').disabled = allDevices.length === 0;
}

// ══════════════════════════════════════════════════════════════════
//  Settings
// ══════════════════════════════════════════════════════════════════
let settingsOpen = false;

function toggleSettings() {
  settingsOpen = !settingsOpen;
  $('settings-body').style.display = settingsOpen ? 'block' : 'none';
  $('s-chevron').style.transform   = settingsOpen ? 'rotate(180deg)' : 'rotate(0)';
  if (settingsOpen) loadSettings();
}

function onToggleRestart(on) {
  $('s-restart-row').style.opacity        = on ? '1' : '0.4';
  $('s-restart-time').disabled            = !on;
}

function onToggleUpdate(on) {
  $('s-update-row').style.opacity         = on ? '1' : '0.4';
  $('s-update-hours').disabled            = !on;
}

async function loadSettings() {
  const s = await api('/api/settings');

  $('s-restart-en').checked   = !!s.auto_restart_enabled;
  $('s-restart-time').value   = s.auto_restart_time || '03:00';
  $('s-update-en').checked    = !!s.auto_update_enabled;
  $('s-update-hours').value   = s.auto_update_hours  || 24;

  onToggleRestart(!!s.auto_restart_enabled);
  onToggleUpdate(!!s.auto_update_enabled);

  const nextEl = $('s-next-update');
  if (nextEl) {
    nextEl.textContent = s._next_auto_update
      ? `${t('settingsNextUpdate')}: ${s._next_auto_update}`
      : (s.auto_update_enabled ? t('settingsNever') : '');
  }
}

async function saveSettings() {
  const payload = {
    auto_restart_enabled: $('s-restart-en').checked,
    auto_restart_time:    $('s-restart-time').value,
    auto_update_enabled:  $('s-update-en').checked,
    auto_update_hours:    parseInt($('s-update-hours').value) || 24,
  };
  const res = await api('/api/settings', 'POST', payload);
  if (res.success) {
    showToast(t('settingsSaved'));
    await loadSettings();
  }
}

// ══════════════════════════════════════════════════════════════════
//  Keyboard
// ══════════════════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeWifi();
  if (e.key === 'Enter' && $('wifi-overlay').classList.contains('open')) applyWifi();
});

// ══════════════════════════════════════════════════════════════════
//  Init
// ══════════════════════════════════════════════════════════════════
applyTheme(theme);
applyLang(lang);
loadDevices();
</script>
</body>
</html>
"""

# Pass mDNS availability to template context
@app.context_processor
def inject_globals():
    return {"mdns_available": ZEROCONF_AVAILABLE}

# ─── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_settings()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║        Shelly Network Manager            ║")
    print("  ║  http://localhost:5000                   ║")
    print("  ╚══════════════════════════════════════════╝")
    print()
    if ZEROCONF_AVAILABLE:
        print("  [✓] mDNS discovery available (zeroconf)")
    else:
        print("  [!] mDNS not available — run: pip install zeroconf")
    print()

    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
