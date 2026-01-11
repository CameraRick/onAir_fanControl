# esp_fancontrol/app.py
import os, json, time, threading
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional

from flask import Flask, request, redirect, url_for, send_file, render_template, jsonify
import paho.mqtt.client as mqtt

APP_NAME = "onAir_fanControl"
# /config is the Volume Mount for user data.
CONFIG_DIR = "/config"
# Code is in current directory inside container (/app)
CODE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
TEMPLATE_DIR = CODE_DIR
STATIC_DIR = CODE_DIR
TEMPLATE_NAME = "index.html"
FAVICON_PATH = os.path.join(CODE_DIR, "favicon.ico")

TZ_NAME = os.getenv("TZ", "UTC")

DEFAULT_CONFIG: Dict[str, Any] = {
    "mqtt": {"host": "192.168.178.8", "port": 1883, "username": "", "password": ""},
    "topics": {
        "target_pwm": "unraid/hdds/target_pwm",          # output
        "min_pwm": "unraid/hdds/min_pwm",                # output (debug)
        "max_pwm": "unraid/hdds/max_pwm",                # output (debug)
        "max_temp": "unraid/hdds/max_temp",              # output (debug)
        "spinning_disks": "unraid/hdds/spinning_disks",  # output (debug)
        "updated_at": "unraid/hdds/updated_at",          # output (unix ts)
        "bias_limit": "unraid/hdds/bias_limit",          # output (debug)
    },
    "limits": {"min_pwm": 25, "max_pwm": 100, "bias_limit": 25},
    "hysteresis_up": 0,
    "hysteresis_down": 3,

    "curve": [
        {"temp_c": 0, "pwm": 25},
        {"temp_c": 40, "pwm": 50},
        {"temp_c": 43, "pwm": 75},
        {"temp_c": 50, "pwm": 100},
    ],
    "curve_mode": "linear",  # linear | steps
    "publish_interval_s": 10,
    "ui_refresh_s": 10,
    "esp_ip": "",

    "unraid_disks_ini": {"path": "/host/disks.ini", "poll_s": 15},

    # Optional behavior (keine "Failsafe"-Logik mehr hier)
    "all_spun_down": {"enabled": False, "after_pwm": 25},
}

DISKS_INI_DEFAULT_PATH = "/host/disks.ini"

app = Flask(
    __name__,
    template_folder=TEMPLATE_DIR,
    static_folder=STATIC_DIR,
    static_url_path="/static",
)

state_lock = threading.Lock()
state: Dict[str, Any] = {
    "max_temp": None,
    "spinning_disks": None,
    "temps_seen": None,

    "target_pwm": None,
    "mode": "boot",
    "source": "boot",

    "updated_at": None,
    "updated_age_s": None,
    "esp_online": False,
    "history": [], # list of {"ts": int, "temp": float, "pwm": int}
}

log_lock = threading.Lock()
log_lines: List[str] = []
_smart_permission_warned = False


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    with log_lock:
        log_lines.append(line)
        del log_lines[:-500]


def _deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _ensure_config_file() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, sort_keys=True)
            f.write("\n")


def load_config() -> Dict[str, Any]:
    _ensure_config_file()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}

    # Backward compat: topics.base_pwm -> topics.target_pwm
    if "topics" in raw and isinstance(raw["topics"], dict):
        if "base_pwm" in raw["topics"] and "target_pwm" not in raw["topics"]:
            raw["topics"]["target_pwm"] = raw["topics"].pop("base_pwm")

    # Backward compat: idle_when_all_spun_down -> all_spun_down
    if "idle_when_all_spun_down" in raw and "all_spun_down" not in raw:
        raw["all_spun_down"] = raw.pop("idle_when_all_spun_down")

    cfg = _deep_merge(DEFAULT_CONFIG, raw)

    # MQTT
    mq = cfg.get("mqtt", {})
    migrated = False
    if "user" in mq:
        if not mq.get("username"): mq["username"] = mq.pop("user")
        else: mq.pop("user")
        migrated = True
    if "pass" in mq:
        if not mq.get("password"): mq["password"] = mq.pop("pass")
        else: mq.pop("pass")
        migrated = True

    cfg["mqtt"] = {
        "host": str(mq.get("host", DEFAULT_CONFIG["mqtt"]["host"])),
        "port": int(mq.get("port", DEFAULT_CONFIG["mqtt"]["port"])),
        "username": str(mq.get("username", "") or ""),
        "password": str(mq.get("password", "") or ""),
    }
    
    # Falls wir im Speicher aufraeumen mussten, schreiben wir die Datei sofort sauber zurueck
    if migrated:
        atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        log("config.json migrated and cleaned (MQTT keys)")

    # Topics (ensure strings)
    for k in ("target_pwm", "min_pwm", "max_pwm", "max_temp", "spinning_disks", "updated_at", "bias_limit"):
        if "topics" not in cfg: cfg["topics"] = {}
        # Fallback auf Default, falls der Key fehlt
        cfg["topics"][k] = str(cfg["topics"].get(k, DEFAULT_CONFIG["topics"].get(k, f"unraid/hdds/{k}")))

    # Limits / general
    cfg["limits"]["min_pwm"] = int(cfg["limits"].get("min_pwm", 25))
    cfg["limits"]["max_pwm"] = int(cfg["limits"].get("max_pwm", 100))
    cfg["limits"]["bias_limit"] = int(cfg["limits"].get("bias_limit", 25))

    # Hysteresis migration/init
    if "hysteresis_c" in raw and "hysteresis_up" not in raw:
        cfg["hysteresis_up"] = int(raw["hysteresis_c"])
    else:
        cfg["hysteresis_up"] = int(cfg.get("hysteresis_up", 0))

    if "hysteresis_c" in raw and "hysteresis_down" not in raw:
        cfg["hysteresis_down"] = int(raw["hysteresis_c"])
    else:
        cfg["hysteresis_down"] = int(cfg.get("hysteresis_down", 3))

    cfg["publish_interval_s"] = int(cfg.get("publish_interval_s", 10))
    cfg["esp_ip"] = str(cfg.get("esp_ip", "")).strip()
    cfg["curve_mode"] = cfg.get("curve_mode", "linear") if cfg.get("curve_mode") in ("linear", "steps") else "linear"

    # disks.ini
    udi = cfg.get("unraid_disks_ini", {}) or {}
    udi["path"] = str(udi.get("path") or DISKS_INI_DEFAULT_PATH)
    udi["poll_s"] = int(udi.get("poll_s", 15))
    cfg["unraid_disks_ini"] = udi

    # all_spun_down
    asd = cfg.get("all_spun_down", {}) or {}
    asd["enabled"] = bool(asd.get("enabled", False))
    asd["after_pwm"] = int(asd.get("after_pwm", 25))
    cfg["all_spun_down"] = asd

    # curve points
    pts = []
    for p in cfg.get("curve", []) or []:
        try:
            t = float(p.get("temp_c"))
            pwm = int(float(p.get("pwm")))
            pts.append({"temp_c": t, "pwm": pwm})
        except Exception:
            continue
    if not pts:
        pts = list(DEFAULT_CONFIG["curve"])
    pts.sort(key=lambda x: x["temp_c"])
    cfg["curve"] = pts

    return cfg


def atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def human_ts(ts: Optional[int]) -> str:
    if not ts:
        return "None"
    try:
        dt = datetime.fromtimestamp(int(ts))
        # Get offset like +0100 or -0500
        offset = dt.astimezone().strftime('%z')
        # Convert to UTC+x format
        if offset:
            hours = int(offset[:3])
            tz_str = f"UTC{hours:+d}" if hours != 0 else "UTC"
        else:
            tz_str = TZ_NAME
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f" ({tz_str})"
    except Exception:
        return str(ts)


# disks.ini parsing: include diskN + parity*, exclude cache/flash/transfer_cache by name + non-rotational
# returns: (spinning_count, temps_seen, max_temp_or_None, file_mtime, source_string)
def parse_disks_ini(path: str) -> Tuple[int, int, Optional[float], float, str]:
    try:
        if not os.path.exists(path):
            log(f"disks.ini not found at {path}")
            return (0, 0, None, 0.0)
        
        mtime = os.path.getmtime(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except Exception as e:
        log(f"Error reading {path}: {e}")
        return (0, 0, None, 0.0)

    spinning = 0
    temps_seen = 0
    max_temp: Optional[float] = None
    source_flags = set() # {"SMART", "INI"}

    cur_name = None
    cur_rot = None
    cur_spundown = None
    cur_temp = None
    cur_dev = None

    def get_smart_temp(dev: str) -> Optional[float]:
        global _smart_permission_warned
        try:
            import subprocess
            cmd = ["smartctl", "-n", "standby", "-A", f"/dev/{dev}"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            
            if res.returncode != 0: 
                # Check for permission denied symptoms (including silent failure rc=2 in restricted envs)
                perm_issue = False
                if "Permission denied" in res.stderr or "Operation not permitted" in res.stderr:
                    perm_issue = True
                elif res.returncode == 2 and not res.stderr.strip():
                    # Silent failure with rc=2 often happens in unprivileged containers
                    perm_issue = True

                if perm_issue and not _smart_permission_warned:
                    log(f"SMART: Query failed (rc={res.returncode}, stderr='{res.stderr.strip()}'). If this persists for active drives, check Docker privileges (needs --privileged and /dev mapping).")
                    _smart_permission_warned = True
                return None
            
            for line in res.stdout.splitlines():
                if "Temperature_Celsius" in line or "Airflow_Temperature_Cel" in line:
                    parts = line.split()
                    if len(parts) >= 10:
                        try:
                            return float(parts[9])
                        except ValueError:
                            continue
            return None
        except Exception as e:
            log(f"SMART error for {dev}: {e}")
            return None

    def flush():
        nonlocal spinning, temps_seen, max_temp, cur_name, cur_rot, cur_spundown, cur_temp, cur_dev, source_flags
        if cur_name is None:
            return
        name = cur_name.lower()
        # Include parity, parity2, disk1, disk2...
        if not (name.startswith("disk") or name.startswith("parity")):
            return
        
        # Only rotational disks
        if str(cur_rot) != "1":
            return
            
        # spundown="0" means spun up
        if str(cur_spundown) == "0":
            spinning += 1
            t = None
            
            # 1. Try SMART (real-time) if disk is awake
            if cur_dev:
                t = get_smart_temp(cur_dev)
                if t is not None:
                    source_flags.add("SMART")

            # 2. Fallback to disks.ini if SMART failed or wasn't available
            if t is None and cur_temp is not None and cur_temp != "*" and cur_temp != "":
                try:
                    import re
                    match = re.search(r"(\d+\.?\d*)", str(cur_temp))
                    if match:
                        t = float(match.group(1))
                        source_flags.add("INI")  # Only add if we actually used it
                except Exception:
                    pass
            
            if t is not None:
                temps_seen += 1
                if (max_temp is None) or (t > max_temp):
                    max_temp = t

    for ln in lines:
        ln = ln.strip()
        if ln.startswith('["') and ln.endswith('"]'):
            flush()
            cur_name = ln[2:-2]
            cur_rot = cur_spundown = cur_temp = None
            continue
        if "=" not in ln:
            continue
        
        # Handling key = "value" or key="value"
        k, v = ln.split("=", 1)
        k = k.strip().lower()
        v = v.strip().strip('"')
        
        if k == "rotational":
            cur_rot = v
        elif k == "spundown":
            cur_spundown = v
        elif k in ("temp", "temperature"):
            cur_temp = v
        elif k == "device":
            cur_dev = v

    flush()
    src_str = "/".join(sorted(list(source_flags))) if source_flags else "None"
    return (spinning, temps_seen, max_temp, mtime, src_str)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def curve_pwm(temp_c: float, points: List[Dict[str, Any]], mode: str) -> float:
    pts = sorted(points, key=lambda x: x["temp_c"])
    if temp_c <= pts[0]["temp_c"]:
        return float(pts[0]["pwm"])
    if temp_c >= pts[-1]["temp_c"]:
        return float(pts[-1]["pwm"])
    for i in range(1, len(pts)):
        t0 = float(pts[i - 1]["temp_c"]); p0 = float(pts[i - 1]["pwm"])
        t1 = float(pts[i]["temp_c"]);     p1 = float(pts[i]["pwm"])
        if temp_c <= t1:
            if mode == "steps":
                return p0
            if t1 == t0:
                return p1
            f = (temp_c - t0) / (t1 - t0)
            return p0 + f * (p1 - p0)
    return float(pts[-1]["pwm"])


_last_target_pwm: Optional[int] = None
_last_temp_band: Optional[float] = None


def compute_target(cfg: Dict[str, Any], now: int, max_temp: Optional[float], spinning_disks: int) -> Tuple[int, str, str]:
    global _last_target_pwm, _last_temp_band

    min_pwm = int(cfg["limits"]["min_pwm"])
    max_pwm = int(cfg["limits"]["max_pwm"])

    # All spun down behavior (optional)
    asd = cfg.get("all_spun_down", {}) or {}
    if asd.get("enabled", False) and spinning_disks == 0:
        # Override min_pwm!
        return (int(asd.get("after_pwm", 0)), "idle(all_spun_down)", "all_spun_down")

    # No temp -> hold last / min
    if max_temp is None:
        if _last_target_pwm is not None:
            return (_last_target_pwm, "no_temp_hold", "hold_last")
        return (min_pwm, "no_temp_min", "min_pwm")

    target = curve_pwm(float(max_temp), cfg["curve"], cfg.get("curve_mode", "linear"))
    target = int(round(clamp(target, min_pwm, max_pwm)))

    if _last_temp_band is not None and _last_target_pwm is not None:
        diff = float(max_temp) - _last_temp_band
        if diff >= 0:
            h = float(cfg.get("hysteresis_up", 0))
        else:
            h = float(cfg.get("hysteresis_down", 0))

        if h > 0 and abs(diff) < h:
            return (_last_target_pwm, "hysteresis_hold", "hysteresis")

    _last_temp_band = float(max_temp)
    return (target, "normal", "curve")


mqtt_client: Optional[mqtt.Client] = None
mqtt_connected = False
stop_event = threading.Event()


def mqtt_on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        log("MQTT: Connected successfully")
    else:
        mqtt_connected = False
        log(f"MQTT: Connection failed with result code {rc}")


def mqtt_on_disconnect(client, userdata, rc):
    global mqtt_connected
    mqtt_connected = False
    log(f"MQTT: Disconnected (rc={rc})")


def mqtt_loop():
    global mqtt_client, _last_target_pwm
    last_disks_poll = 0
    last_history_sample = 0
    last_ini_mtime = 0.0

    while not stop_event.is_set():
        cfg = load_config()
        now = int(time.time())

        # 1. ALWAYS poll disks.ini and compute targets (so UI is live even if MQTT dies)
        poll_s = int(cfg["unraid_disks_ini"].get("poll_s", 15))
        if now - last_disks_poll >= max(1, poll_s):
            last_disks_poll = now
            spinning, temps_seen, max_temp, ini_mtime, src_str = parse_disks_ini(cfg["unraid_disks_ini"]["path"])
            
            last_ini_mtime = ini_mtime

            target_pwm, mode, source = compute_target(cfg, now, max_temp, spinning)

            with state_lock:
                # Format temperature for UI: omit .0 if it's a whole number
                if max_temp is not None:
                    if float(max_temp) == int(float(max_temp)):
                        state["max_temp"] = int(float(max_temp))
                    else:
                        state["max_temp"] = float(max_temp)
                else:
                    state["max_temp"] = None

                state["spinning_disks"] = spinning
                state["temps_seen"] = temps_seen
                state["target_pwm"] = int(target_pwm)
                state["mode"] = mode
                state["source"] = source
                state["updated_at"] = now
                state["updated_age_s"] = 0
                _last_target_pwm = int(target_pwm)
            
            # 2. Publish to MQTT (if available)
            if mqtt_client is not None and mqtt_connected:
                try:
                    t = cfg["topics"]
                    mqtt_client.publish(t["target_pwm"], str(int(target_pwm)), qos=0, retain=True)
                    mqtt_client.publish(t["min_pwm"], str(int(cfg.get("limits", {}).get("min_pwm", 0))), qos=0, retain=True)
                    mqtt_client.publish(t["max_pwm"], str(int(cfg.get("limits", {}).get("max_pwm", 100))), qos=0, retain=True)
                    mqtt_client.publish(t["bias_limit"], str(int(cfg.get("limits", {}).get("bias_limit", 25))), qos=0, retain=True)
                    mqtt_client.publish(t["spinning_disks"], str(int(spinning)), qos=0, retain=True)
                    mqtt_client.publish(t["updated_at"], str(int(now)), qos=0, retain=True)

                    if max_temp is None:
                        mqtt_client.publish(t["max_temp"], "", qos=0, retain=True)
                    else:
                        mqtt_client.publish(t["max_temp"], f"{float(max_temp):.1f}", qos=0, retain=True)
                except Exception as e:
                    log(f"MQTT publish failed: {e}")
                    # don't reset client here, keep trying next poll
            
            
            log(f"Update: target_pwm={int(target_pwm)} mode={mode} spinning={spinning} max_temp={max_temp} ({src_str})")

        # 2. Sample History (every 30s)
        if now - last_history_sample >= 30:
            last_history_sample = now
            with state_lock:
                if state["max_temp"] is not None:
                    state["history"].append({
                        "ts": now,
                        "temp": float(state["max_temp"]),
                        "pwm": int(state["target_pwm"] or 0)
                    })
                    # Keep max 60 samples (~30 min)
                    state["history"] = state["history"][-60:]

        # 3. Check ESP availability (via HTTP)
        if cfg.get("esp_ip"):
            try:
                import urllib.request
                # Set a very short timeout
                with urllib.request.urlopen(f"http://{cfg['esp_ip']}/", timeout=2) as response:
                    online = (response.status < 500) # Any response is good
            except Exception:
                online = False
            
            with state_lock:
                state["esp_online"] = online

        if mqtt_client is None:
            try:
                mc = mqtt.Client()
                if cfg["mqtt"].get("username"):
                    mc.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password", ""))
                mc.on_connect = mqtt_on_connect
                mc.on_disconnect = mqtt_on_disconnect
                mc.connect(cfg["mqtt"]["host"], int(cfg["mqtt"]["port"]), keepalive=30)
                mc.loop_start()
                mqtt_client = mc
            except Exception as e:
                log(f"MQTT connect failed: {e}")
                mqtt_client = None

        # 4. Update age (every second)
        with state_lock:
            if state["updated_at"] is not None:
                state["updated_age_s"] = int(now - state["updated_at"])

        time.sleep(1)


def svg_graph(cfg: Dict[str, Any], current_temp: Optional[float] = None, width: int = 640, height: int = 260) -> str:
    pts = cfg.get("curve", []) or []
    if not pts:
        return ""
    mode = cfg.get("curve_mode", "linear")

    x_min, x_max = 10.0, 60.0
    y_min, y_max = 0.0, 100.0

    left_m = 54
    right_m = 18
    top_m = 14
    bottom_m = 48

    plot_w = width - left_m - right_m
    plot_h = height - top_m - bottom_m
    if plot_w <= 10 or plot_h <= 10:
        return ""

    def sx(x: float) -> float:
        return left_m + (float(x) - x_min) / (x_max - x_min) * plot_w

    def sy(y: float) -> float:
        return top_m + (1 - (float(y) - y_min) / (y_max - y_min)) * plot_h

    # Grid lines
    grid = []
    # Vertical grid (every 5°C)
    for x in range(int(x_min), int(x_max) + 1, 5):
        pos = sx(x)
        grid.append(f'<line x1="{pos:.1f}" y1="{top_m:.1f}" x2="{pos:.1f}" y2="{top_m + plot_h:.1f}" class="grid-line" />')
    # Horizontal grid (every 5%)
    for y in range(int(y_min), int(y_max) + 1, 5):
        pos = sy(y)
        grid.append(f'<line x1="{left_m:.1f}" y1="{pos:.1f}" x2="{left_m + plot_w:.1f}" y2="{pos:.1f}" class="grid-line" />')

    samples: List[Tuple[float, float]] = []
    if mode == "linear":
        xs = [x_min + i * (x_max - x_min) / 160 for i in range(161)]
        for x in xs:
            y = curve_pwm(x, pts, "linear")
            samples.append((sx(x), sy(y)))
    else:
        spts = sorted(pts, key=lambda p: p["temp_c"])
        for i in range(len(spts) - 1):
            x0, y0 = float(spts[i]["temp_c"]), float(spts[i]["pwm"])
            x1 = float(spts[i + 1]["temp_c"])
            samples.append((sx(x0), sy(y0)))
            samples.append((sx(x1), sy(y0)))
        samples.append((sx(float(spts[-1]["temp_c"])), sy(float(spts[-1]["pwm"]))))

    pl = " ".join(f"{x:.1f},{y:.1f}" for x, y in samples)
    circles = "\n".join(
        f'<circle cx="{sx(float(p["temp_c"])):.1f}" cy="{sy(float(p["pwm"])):.1f}" r="6" class="point-node" data-idx="{i}" />'
        for i, p in enumerate(pts)
    )

    axis_y0 = top_m
    axis_y1 = top_m + plot_h
    axis_x0 = left_m
    axis_x1 = left_m + plot_w

    xtick_vals = (10, 20, 30, 40, 50, 60)
    ytick_vals = (0, 25, 50, 75, 100)

    xticks_y = axis_y1 + 18
    ytick_x = axis_x0 - 10

    xticks = "".join(
        f'<text x="{sx(t):.1f}" y="{xticks_y:.1f}" font-size="10" text-anchor="middle">{int(t)}</text>'
        for t in xtick_vals
    )
    yticks = "".join(
        f'<text x="{ytick_x:.1f}" y="{sy(t):.1f}" font-size="10" text-anchor="end" dominant-baseline="middle">{int(t)}</text>'
        for t in ytick_vals
    )

    xlabel_y = xticks_y + 16
    xlabel = f'<text x="{(axis_x0 + axis_x1) / 2:.1f}" y="{xlabel_y:.1f}" text-anchor="middle" font-size="10" font-weight="bold">Temperature (°C)</text>'
    
    ylab_x = ytick_x - 22
    ylab_y = (axis_y0 + axis_y1) / 2
    ylabel = f'<text x="{ylab_x:.1f}" y="{ylab_y:.1f}" text-anchor="middle" font-size="10" font-weight="bold" transform="rotate(-90 {ylab_x:.1f} {ylab_y:.1f})">PWM (%)</text>'

    current_line = ""
    if current_temp is not None:
        try:
            cx = sx(float(current_temp))
            if left_m <= cx <= axis_x1:
                current_line = f'<line x1="{cx:.1f}" y1="{top_m:.1f}" x2="{cx:.1f}" y2="{axis_y1:.1f}" class="current-temp-line" />'
        except Exception:
            pass

    return f'''
<svg id="curve-svg" viewBox="0 0 {width} {height}" width="100%" height="{height}" xmlns="http://www.w3.org/2000/svg" class="graph"
  data-xmin="{x_min}" data-xmax="{x_max}" data-ymin="{y_min}" data-ymax="{y_max}"
  data-left="{left_m}" data-right="{right_m}" data-top="{top_m}" data-bottom="{bottom_m}"
  data-width="{width}" data-height="{height}" data-mode="{mode}">
  <style>
    .grid-line {{ stroke: rgba(255,255,255,0.05); stroke-width: 1; }}
    .current-temp-line {{ stroke: rgba(231,76,60,0.5); stroke-width: 1.5; stroke-dasharray: 4 2; }}
    .point-node {{ fill: var(--mid); stroke: #fff; stroke-width: 1.5; cursor: grab; }}
    .point-node:hover {{ r: 8; stroke-width: 2; }}
    .point-node.dragging {{ cursor: grabbing; fill: #fff; stroke: var(--mid); }}
  </style>
  {" ".join(grid)}
  <line x1="{axis_x0:.1f}" y1="{axis_y0:.1f}" x2="{axis_x0:.1f}" y2="{axis_y1:.1f}" class="axis"/>
  <line x1="{axis_x0:.1f}" y1="{axis_y1:.1f}" x2="{axis_x1:.1f}" y2="{axis_y1:.1f}" class="axis"/>
  {xticks}
  {yticks}
  <polyline points="{pl}" fill="none" class="curve-line"/>
  {current_line}
  {circles}
  {xlabel}
  {ylabel}
</svg>
'''.strip()


def svg_history(history: List[Dict[str, Any]], width: int = 1200, height: int = 100) -> str:
    # History card styling: we want the right side to be the "now"
    padding_x = 80 # a lot of room for Y labels to clear the clipping edge
    padding_y = 12
    right_gutter = 60 # move the rightmost clock label far inward
    gw = width - padding_x - right_gutter
    gh = height - (padding_y * 2) - 8 # room for X labels (time)

    if not history:
        return f'<svg viewBox="0 0 {width} {height}" class="h-graph"><text x="50%" y="50%" text-anchor="middle" fill="#888" font-size="12">waiting for data...</text></svg>'

    # Internal coordinate system: 0-100 for data
    # 1°C = 2 units (so 50°C = 100 units)
    # 1% PWM = 1 unit
    
    def sy(val: float) -> float:
        return padding_y + gh - (clamp(val, 0, 100) / 100.0 * gh)

    # We always show 60 slots. If we have fewer, we shift them to the right.
    max_slots = 60
    dx = gw / (max_slots - 1)
    
    pts_temp = []
    pts_pwm = []
    labels_x = []
    
    # Calculate offset to align right (the last item in history is index 59)
    offset = max_slots - len(history)
    
    for i, d in enumerate(history):
        x = padding_x + (i + offset) * dx
        pts_temp.append(f"{x:.1f},{sy(d['temp'] * 2):.1f}")
        pts_pwm.append(f"{x:.1f},{sy(d['pwm']):.1f}")
        
        # X-Labels (Time) - every 15 samples (~7.5 min)
        if (i + offset) % 15 == 0 or i == len(history) - 1:
            dt = datetime.fromtimestamp(d['ts']).strftime('%H:%M')
            labels_x.append(f'<text x="{x:.1f}" y="{height - 2}" text-anchor="middle" class="h-label-x">{dt}</text>')

    # Y-Labels and Grid
    grid_y = []
    # (data_value, temp_label, pwm_label)
    y_marks = [
        (0, "0°C", "0%"),
        (50, "25°C", "50%"),
        (100, "50°C", "100%")
    ]
    for val, t_lab, p_lab in y_marks:
        y_pos = sy(val)
        grid_y.append(f'<line x1="{padding_x}" y1="{y_pos:.1f}" x2="{width-10}" y2="{y_pos:.1f}" class="h-grid" />')
        label_html = f'<text x="{padding_x-5}" y="{y_pos+3:.1f}" text-anchor="end" class="h-label-y">' \
                     f'<tspan fill="#e74c3c">{t_lab}</tspan> <tspan fill="rgba(255,255,255,0.2)">/</tspan> ' \
                     f'<tspan class="text-pwm">{p_lab}</tspan></text>'
        grid_y.append(label_html)

    return f'''
<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class="h-graph" xmlns="http://www.w3.org/2000/svg">
  <style>
    .h-temp {{ stroke: #e74c3c; stroke-width: 1.4; fill: none; }}
    .h-pwm {{ stroke: var(--mid); stroke-width: 1.4; fill: none; }}
    .text-pwm {{ fill: var(--mid); }}
    .h-label-x {{ fill: rgba(255,255,255,0.6); font-size: 8px; }}
    .h-label-y {{ fill: rgba(255,255,255,0.7); font-size: 9px; }}
    .h-bg {{ fill: none; }}
    .h-grid {{ stroke: rgba(255,255,255,0.15); stroke-width: 0.8; stroke-dasharray: 2 2; }}
  </style>
  {" ".join(grid_y)}
  <polyline points="{" ".join(pts_pwm)}" class="h-pwm" />
  <polyline points="{" ".join(pts_temp)}" class="h-temp" />
  {" ".join(labels_x)}
</svg>
'''.strip()


@app.route("/favicon.ico")
def favicon():
    if os.path.exists(FAVICON_PATH):
        return send_file(FAVICON_PATH, mimetype="image/x-icon")
    return ("", 204)


@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    now = int(time.time())
    with state_lock:
        st = dict(state)
        st["updated_at_human"] = human_ts(st.get("updated_at"))
        if st.get("updated_at"):
            st["updated_age_s"] = max(0, now - int(st["updated_at"]))
        else:
            st["updated_age_s"] = None

    with log_lock:
        log_text = "\n".join(log_lines[-120:])

    return render_template(
        TEMPLATE_NAME,
        app_name=APP_NAME,
        tz_name=TZ_NAME,
        status=st,
        cfg=cfg,
        mqtt_connected=mqtt_connected,
        svg=svg_graph(cfg, st.get("max_temp")),
        h_svg=svg_history(st.get("history", [])),
        log_text=log_text,
        log_count=min(len(log_lines), 120),
        config_json=json.dumps(cfg, indent=2, sort_keys=True),
    )
    

@app.route("/api/status", methods=["GET"])
def api_status():
    with state_lock:
        st = dict(state)
        # Add human timestamp for UI
        if st.get("updated_at"):
            st["updated_at_human"] = human_ts(st["updated_at"])
        else:
            st["updated_at_human"] = "None"
        history_list = list(state.get("history", []))

    with log_lock:
        log_text = "\n".join(log_lines[-120:])

    return jsonify({
        "status": st,
        "mqtt_connected": mqtt_connected,
        "h_svg": svg_history(history_list),
        "log_text": log_text,
    })


@app.route("/save_settings", methods=["POST"])
def save_settings():
    cfg = load_config()

    # Network / MQTT
    if "mqtt_host" in request.form:
        cfg["mqtt"]["host"] = request.form.get("mqtt_host", "").strip() or cfg["mqtt"]["host"]
    if "esp_ip" in request.form:
        cfg["esp_ip"] = request.form.get("esp_ip", "").strip()
    if "mqtt_port" in request.form:
        try:
            cfg["mqtt"]["port"] = int(float(request.form.get("mqtt_port")))
        except Exception:
            pass
    if "mqtt_username" in request.form:
        cfg["mqtt"]["username"] = request.form.get("mqtt_username", "").strip()
    if "mqtt_password" in request.form:
        new_pw = request.form.get("mqtt_password", "").strip()
        if new_pw and new_pw != "**********":
            cfg["mqtt"]["password"] = new_pw
        elif not new_pw:
            cfg["mqtt"]["password"] = ""

    # Topics (editable but low-priority)
    for k in ("target_pwm", "min_pwm", "max_pwm", "max_temp", "spinning_disks", "updated_at", "bias_limit"):
        form_k = f"topic_{k}"
        if form_k in request.form:
            v = request.form.get(form_k, "").strip()
            if v:
                cfg["topics"][k] = v

    # General
    if "publish_interval_s" in request.form:
        try:
            cfg["publish_interval_s"] = int(float(request.form.get("publish_interval_s")))
        except Exception:
            pass

    if "ui_refresh_s" in request.form:
        try:
            cfg["ui_refresh_s"] = int(float(request.form.get("ui_refresh_s")))
        except Exception:
            pass

    # disks.ini poll only (path stays hardcoded)
    if "disks_poll_s" in request.form:
        try:
            cfg["unraid_disks_ini"]["poll_s"] = int(float(request.form.get("disks_poll_s")))
        except Exception:
            pass

    # Curve
    mode = request.form.get("curve_mode", cfg.get("curve_mode", "linear"))
    cfg["curve_mode"] = mode if mode in ("linear", "steps") else "linear"

    temps = request.form.getlist("curve_temp")
    pwms = request.form.getlist("curve_pwm")
    pts = []
    for t, p in zip(temps, pwms):
        try:
            pts.append({"temp_c": float(t), "pwm": int(float(p))})
        except Exception:
            pass

    if request.form.get("add_point") == "1":
        pts.append({"temp_c": 55.0, "pwm": 100})

    del_idx = request.form.get("delete_idx")
    if del_idx is not None:
        try:
            di = int(del_idx)
            if 0 <= di < len(pts):
                pts.pop(di)
        except Exception:
            pass

    if pts:
        pts.sort(key=lambda x: x["temp_c"])
        cfg["curve"] = pts

    # Limits
    for k in ("min_pwm", "max_pwm", "bias_limit"):
        if k in request.form:
            try:
                cfg["limits"][k] = int(float(request.form.get(k)))
            except Exception:
                pass
    if "hysteresis_up" in request.form:
        try:
            cfg["hysteresis_up"] = int(float(request.form.get("hysteresis_up")))
        except Exception:
            pass
    if "hysteresis_down" in request.form:
        try:
            cfg["hysteresis_down"] = int(float(request.form.get("hysteresis_down")))
        except Exception:
            pass

    # All spun down
    cfg["all_spun_down"]["enabled"] = (request.form.get("all_spun_down_enabled") == "on")
    if "all_spun_down_after_pwm" in request.form:
        try:
            cfg["all_spun_down"]["after_pwm"] = int(float(request.form.get("all_spun_down_after_pwm")))
        except Exception:
            pass

    atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    log("config.json saved via UI (Save settings)")
    return redirect(url_for("index"))


@app.route("/save_json", methods=["POST"])
def save_json():
    txt = request.form.get("config_json", "")
    try:
        parsed = json.loads(txt)
        if not isinstance(parsed, dict):
            raise ValueError("Top-level JSON must be an object")
        # compat: topics.base_pwm -> topics.target_pwm
        if "topics" in parsed and isinstance(parsed["topics"], dict):
            if "base_pwm" in parsed["topics"] and "target_pwm" not in parsed["topics"]:
                parsed["topics"]["target_pwm"] = parsed["topics"].pop("base_pwm")
        atomic_write(CONFIG_PATH, json.dumps(parsed, indent=2, sort_keys=True) + "\n")
        log("config.json saved via raw JSON")
    except Exception as e:
        log(f"raw JSON save failed: {e}")
    return redirect(url_for("index"))


def start():
    t = threading.Thread(target=mqtt_loop, daemon=True)
    t.start()
    log(f"UI listening on 0.0.0.0:8088 (TZ={TZ_NAME})")


if __name__ == "__main__":
    start()
    app.run(host="0.0.0.0", port=8088, debug=False)
