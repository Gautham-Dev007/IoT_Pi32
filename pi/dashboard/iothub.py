#!/usr/bin/env python3
import bisect
import collections
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formataddr
import csv
import io
import queue
import signal
import hmac
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import re
import shlex
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt
from flask import Flask, Response, g, jsonify, request, send_from_directory, session

HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".config", "iothub")
DATA_DIR = os.path.join(HOME, "iothub-data")
DB_PATH = os.path.join(DATA_DIR, "readings.db")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

HUB_VERSION = "2.6"
PI_ID = "pi"
PI_EVERY_S = 10
ID_RE = re.compile(r"^[a-z0-9_-]{1,24}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("iothub")


def load_env(name):
    out = {}
    path = os.path.join(CONF_DIR, name)
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            parts = shlex.split(v) if v.strip() else [""]
            out[k.strip()] = " ".join(parts)
    return out


cfg = {**load_env("mqtt.env"), **load_env("dashboard.env"),
       **load_env("alerts.env"), **load_env("storage.env"), **load_env("adafruit.env"), **load_env("email.env")}
MQTT_USER = cfg.get("MQTT_USER", "esp")
MQTT_PASS = cfg.get("MQTT_PASS", "")
DASH_PASSWORD = cfg.get("DASH_PASSWORD", "")
PORT = int(cfg.get("DASH_PORT", "8080"))
KEEP_DAYS = int(cfg.get("RAW_KEEP_DAYS", "30"))
FLUSH_S = int(float(cfg.get("FLUSH_MINUTES", "1")) * 60)
HEARTBEAT_PATH = os.path.join(DATA_DIR, "heartbeat.json")
FILL_S = int(float(cfg.get("FILL_MAX_MINUTES", "15")) * 60)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CONF_DIR, exist_ok=True)
db_recovered = None


def _open_db():
    global db_recovered
    try:
        con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        if ok == "ok":
            return con
        con.close()
        reason = ok
    except sqlite3.DatabaseError as e:
        reason = str(e)
    bad = f"{DB_PATH}.damaged-{time.strftime('%Y%m%d-%H%M%S')}"
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + ext):
            os.replace(DB_PATH + ext, bad + ext)
    db_recovered = f"Database was damaged ({reason}); moved to {os.path.basename(bad)} and a new one started"
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)


db = _open_db()
db_lock = threading.Lock()
with db_lock:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("CREATE TABLE IF NOT EXISTS readings (ts REAL, node TEXT, value REAL)")
    cols = [r[1] for r in db.execute("PRAGMA table_info(readings)")]
    if "metric" not in cols:
        db.execute("ALTER TABLE readings ADD COLUMN metric TEXT NOT NULL DEFAULT 'temp'")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ts ON readings(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_node_ts ON readings(node, metric, ts)")
    db.execute("CREATE TABLE IF NOT EXISTS outages (id INTEGER PRIMARY KEY, kind TEXT, detected REAL, "
               "last_alive REAL, boot_at REAL, hub_at REAL, down_s REAL, boot_s REAL, clock_s REAL, "
               "first_data_s REAL, lost INTEGER, recovered INTEGER, detail TEXT, analysed INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS node_boots (ts REAL, node TEXT, reason TEXT, boot INTEGER)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_nb ON node_boots(node, ts)")
    db.execute("CREATE TABLE IF NOT EXISTS daily (day TEXT, node TEXT, metric TEXT, min REAL, max REAL, "
               "avg REAL, n INTEGER, PRIMARY KEY (day, node, metric))")
    db.execute("CREATE TABLE IF NOT EXISTS events (ts REAL, node TEXT, kind TEXT, level TEXT, msg TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts)")
    ev_cols = [r[1] for r in db.execute("PRAGMA table_info(events)")]
    if "category" not in ev_cols:
        db.execute("ALTER TABLE events ADD COLUMN category TEXT NOT NULL DEFAULT 'system'")
        for kind, cat in {"power": "power", "boot": "power", "shutdown": "power", "pi_power": "power",
                          "offline": "connectivity", "mqtt": "connectivity", "sensor": "sensor",
                          "temp_high": "environment", "temp_low": "environment", "hum_high": "environment",
                          "hum_low": "environment", "ota": "firmware", "backlog": "data"}.items():
            db.execute("UPDATE events SET category = ? WHERE kind = ?", (cat, kind))
    db.commit()

METRICS = {"temp": (-55, 125), "hum": (0, 100)}
INSERT = "INSERT INTO readings (ts, node, value, metric) VALUES (?,?,?,?)"
pending = []
pend_lock = threading.Lock()
storage_state = {"last_flush": None, "rows_written": 0, "error": None}
MAX_PENDING = 500000


clock = {"ok": False, "since": None, "verified": False}
presync = []
presync_backlog = []
PROC_UP_AT_START = float(open("/proc/uptime").read().split()[0]) if os.path.exists("/proc/uptime") else 0.0
MONO_AT_START = time.monotonic()


def uptime_now():
    return PROC_UP_AT_START + (time.monotonic() - MONO_AT_START)


try:
    with open(HEARTBEAT_PATH) as _f:
        PREV_HEARTBEAT = json.load(_f)
except (OSError, ValueError):
    PREV_HEARTBEAT = None


def store(node, value, ts, metric="temp"):
    with pend_lock:
        if not clock["ok"]:
            presync.append((time.monotonic(), node, value, metric))
            return
        pending.append((ts, node, value, metric))


def clock_ready(verified=True):
    wall, mono = time.time(), time.monotonic()
    with pend_lock:
        for m, n, v, me in presync:
            pending.append((wall - (mono - m), n, v, me))
        held = len(presync)
        presync.clear()
        backlogs = presync_backlog[:]
        presync_backlog.clear()
        clock.update(ok=True, since=wall, verified=verified, up=uptime_now())
    for m, nid, payload in backlogs:
        count = store_backlog(nid, payload, wall - (mono - m))
        backlog_totals[nid] = backlog_totals.get(nid, 0) + count
        backlog_since_start[nid] = backlog_since_start.get(nid, 0) + count
    if held:
        log.info("clock %s: dated %d readings taken before it was set", "synced" if verified else "unverified", held)


def day_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def day_bounds(day):
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    nxt = time.localtime(start + 90000)
    end = time.mktime((nxt.tm_year, nxt.tm_mon, nxt.tm_mday, 0, 0, 0, 0, 0, -1))
    return start, end


def update_daily(day):
    start, end = day_bounds(day)
    db.execute("DELETE FROM daily WHERE day = ?", (day,))
    db.execute("INSERT INTO daily SELECT ?, node, metric, MIN(value), MAX(value), AVG(value), COUNT(*) "
               "FROM readings WHERE ts >= ? AND ts < ? GROUP BY node, metric", (day, start, end))


def flush():
    with pend_lock:
        rows = pending[:]
        pending.clear()
    if rows:
        cutoff = time.time() - KEEP_DAYS * 86400
        days = {day_of(r[0]) for r in rows}
        try:
            with db_lock:
                db.executemany(INSERT, rows)
                for d in days:
                    if day_bounds(d)[0] >= cutoff:
                        update_daily(d)
                db.commit()
        except sqlite3.Error as e:
            with db_lock:
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
            with pend_lock:
                pending[:0] = rows
                del pending[:-MAX_PENDING]
            storage_state["error"] = str(e)
            raise
        storage_state["rows_written"] += len(rows)
        storage_state["error"] = None
    storage_state["last_flush"] = time.time()
    write_heartbeat(clean=False)
    return len(rows)


def rebuild_daily():
    with db_lock:
        days = [r[0] for r in db.execute(
            "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') FROM readings "
            "EXCEPT SELECT DISTINCT day FROM daily")]
        for d in days:
            update_daily(d)
        db.commit()
    if days:
        log.info("Built daily summaries for %d day(s)", len(days))


def store_backlog(nid, payload, received):
    try:
        items = json.loads(payload).get("r", [])
    except (ValueError, AttributeError):
        return 0
    rows = []
    oldest = received - KEEP_DAYS * 86400
    for item in items:
        try:
            t, v = float(item[0]), float(item[1])
            h = float(item[2]) if len(item) > 2 and item[2] is not None else None
        except (TypeError, ValueError, IndexError):
            continue
        ts = t if t > 1e9 else received + t
        if not (oldest < ts <= received + 60 and -55 <= v <= 125):
            continue
        rows.append((ts, nid, v, "temp"))
        if h is not None and 0 <= h <= 100:
            rows.append((ts, nid, h, "hum"))
    rows = _drop_known(nid, rows)
    with pend_lock:
        pending.extend(rows)
    return sum(1 for r in rows if r[3] == "temp")


def _drop_known(nid, rows):
    if not rows:
        return rows
    iv = _node_interval(nid)
    tol = min(4.0, iv * 0.4)
    lo, hi = min(r[0] for r in rows) - tol, max(r[0] for r in rows) + tol
    have = collections.defaultdict(list)
    with db_lock:
        for ts, m in db.execute("SELECT ts, metric FROM readings WHERE node = ? AND metric IN ('temp','hum') "
                                "AND ts BETWEEN ? AND ?", (nid, lo, hi)):
            have[m].append(ts)
    with pend_lock:
        for ts, n, _, m in pending:
            if n == nid and lo <= ts <= hi:
                have[m].append(ts)
    for v in have.values():
        v.sort()
    out = []
    for r in sorted(rows):
        lst = have[r[3]]
        i = bisect.bisect_left(lst, r[0] - tol)
        if i < len(lst) and lst[i] <= r[0] + tol:
            continue
        bisect.insort(lst, r[0])
        out.append(r)
    return out


def pending_between(start, end, metric=None):
    with pend_lock:
        return [r for r in pending if start <= r[0] < end and (metric is None or r[3] == metric)]


def write_heartbeat(clean):
    if not clock["ok"]:
        return
    tmp = HEARTBEAT_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"alive": time.time(), "clean": clean}, f)
        os.replace(tmp, HEARTBEAT_PATH)
    except OSError as e:
        log.warning("heartbeat write failed: %s", e)


state_lock = threading.Lock()
nodes = {}


def node(nid):
    if nid not in nodes:
        nodes[nid] = {"id": nid, "status": "unknown", "temp": None, "temp_ts": None, "hum": None,
                      "info": {}, "led": None, "events": collections.deque(maxlen=15)}
    return nodes[nid]


def load_last_readings():
    with db_lock:
        rows = db.execute(
            "SELECT node, value, MAX(ts) FROM readings WHERE ts > ? AND metric = 'temp' GROUP BY node",
            (time.time() - 86400,)).fetchall()
    with state_lock:
        for nid, value, ts in rows:
            n = node(nid)
            n["temp"], n["temp_ts"] = value, ts


def on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code):
        log.error("MQTT connect failed: %s", reason_code)
        return
    log.info("MQTT connected")
    client.subscribe([("home/#", 0)] + [(t, 0) for t in SYS_TOPICS])


SYS_TOPICS = {
    "$SYS/broker/clients/connected": "clients_connected",
    "$SYS/broker/clients/total": "clients_total",
    "$SYS/broker/load/messages/received/1min": "msgs_in_1min",
    "$SYS/broker/load/messages/sent/1min": "msgs_out_1min",
    "$SYS/broker/uptime": "uptime",
    "$SYS/broker/version": "version",
}
broker = {}


def on_message(client, userdata, msg):
    if msg.topic in SYS_TOPICS:
        broker[SYS_TOPICS[msg.topic]] = msg.payload.decode("utf-8", "replace").strip()
        return
    parts = msg.topic.split("/")
    if len(parts) < 3 or parts[0] != "home" or parts[1] in ("all", PI_ID, "test", "hub"):
        return
    nid, sub = parts[1], "/".join(parts[2:])
    payload = msg.payload.decode("utf-8", "replace").strip()
    now = time.time()
    backlog = None
    with state_lock:
        if sub == "status" and payload == "":
            nodes.pop(nid, None)
            return
        if sub.endswith("/set") or sub == "cmd":
            return
        n = node(nid)
        n["seen"] = now
        if sub in METRICS:
            try:
                v = float(payload)
            except ValueError:
                return
            lo, hi = METRICS[sub]
            if not lo <= v <= hi:
                return
            if sub == "temp":
                n["temp"], n["temp_ts"] = v, now
            else:
                n["hum"] = v
        elif sub == "status":
            if payload == "online" and n["status"] != "online":
                publish_time()
            n["status"] = payload
        elif sub == "backlog":
            backlog = payload
        elif sub == "info":
            try:
                n["info"] = json.loads(payload) if payload else {}
            except ValueError:
                pass
        elif sub == "led/state":
            n["led"] = payload or None
        elif sub in ("ota/status", "log"):
            n["events"].appendleft({"ts": now, "kind": sub, "msg": payload})
            if sub == "ota/status" and payload.startswith(("failed", "new firmware")):
                log_event(nid, "ota", "info" if payload.startswith("new") else "warn", payload)
        else:
            return
    if sub in METRICS:
        if FIRST_DATA["up"] is None:
            FIRST_DATA["up"] = uptime_now()
        store(nid, float(payload), now, sub)
    elif sub == "info" and not msg.retain:
        try:
            note_node_boot(nid, json.loads(payload) if payload else {})
        except (ValueError, sqlite3.Error) as e:
            log.warning("node restart check failed: %s", e)
    elif backlog is not None:
        with pend_lock:
            if not clock["ok"]:
                presync_backlog.append((time.monotonic(), nid, backlog))
                return
        count = store_backlog(nid, backlog, now)
        backlog_totals[nid] = backlog_totals.get(nid, 0) + count
        backlog_since_start[nid] = backlog_since_start.get(nid, 0) + count
        if not count:
            return
        with state_lock:
            ev = node(nid)["events"]
            if ev and ev[0]["kind"] == "backlog":
                ev[0]["n"] += count
                ev[0]["msg"] = f"restored {ev[0]['n']} readings stored while the hub was away"
                ev[0]["ts"] = now
            else:
                ev.appendleft({"ts": now, "kind": "backlog", "n": count,
                               "msg": f"restored {count} readings stored while the hub was away"})


def make_mqtt():
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iothub-bridge")
    except AttributeError:
        c = mqtt.Client(client_id="iothub-bridge")
    c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.on_connect = on_connect
    c.on_message = on_message
    c.reconnect_delay_set(1, 30)
    c.connect_async("localhost", 1883, keepalive=30)
    c.loop_start()
    return c


mq = None


def publish_time():
    if mq is not None and mq.is_connected() and clock["ok"] and clock["verified"]:
        mq.publish("home/hub/time", str(int(time.time())))


def time_loop():
    while True:
        time.sleep(30)
        publish_time()


def publish(topic, payload):
    if mq is None or not mq.is_connected():
        return False
    return mq.publish(topic, payload).rc == mqtt.MQTT_ERR_SUCCESS


def pi_cpu_temp():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read().strip()) / 1000.0


_prev_cpu = None
_prev_net = None
_slow = {"ts": 0}
STARTED = time.time()


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _cpu_pct():
    global _prev_cpu
    vals = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
    idle, total = vals[3] + vals[4], sum(vals)
    pct = None
    if _prev_cpu:
        dt = total - _prev_cpu[1]
        pct = round(100 * (1 - (idle - _prev_cpu[0]) / dt), 1) if dt > 0 else 0.0
    _prev_cpu = (idle, total)
    return pct


def _default_iface():
    for line in (_read("/proc/net/route", "") or "").splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000":
            return f[0]
    return None


def _net_rates(iface):
    global _prev_net
    if not iface:
        return None, None
    rx = int(_read(f"/sys/class/net/{iface}/statistics/rx_bytes", "0"))
    tx = int(_read(f"/sys/class/net/{iface}/statistics/tx_bytes", "0"))
    now = time.time()
    rates = (None, None)
    if _prev_net and _prev_net[0] == iface and now > _prev_net[1]:
        dt = now - _prev_net[1]
        rates = (round((rx - _prev_net[2]) / dt / 1024, 1), round((tx - _prev_net[3]) / dt / 1024, 1))
    _prev_net = (iface, now, rx, tx)
    return rates


def _wifi_dbm(iface):
    for line in (_read("/proc/net/wireless", "") or "").splitlines()[2:]:
        f = line.split()
        if f and f[0].rstrip(":") == iface:
            try:
                return int(float(f[3]))
            except (IndexError, ValueError):
                return None
    return None


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("1.1.1.1", 80))
            return sk.getsockname()[0]
    except OSError:
        return None


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


THROTTLE_BITS = {0: "under-voltage", 1: "freq capped", 2: "throttled", 3: "soft temp limit"}


def _throttled():
    out = _run(["vcgencmd", "get_throttled"])
    if "=" not in out:
        return None
    v = int(out.split("=")[1], 16)
    now = [name for bit, name in THROTTLE_BITS.items() if v & (1 << bit)]
    past = [name for bit, name in THROTTLE_BITS.items() if v & (1 << (bit + 16))]
    if now:
        return "NOW: " + ", ".join(now)
    if past:
        return "OK (earlier: " + ", ".join(past) + ")"
    return "OK"


def _slow_stats():
    if time.time() - _slow["ts"] < 60:
        return
    os_name = "Linux"
    for line in (_read("/etc/os-release", "") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip('"')
    with db_lock:
        count = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    _slow.update(
        ts=time.time(),
        tailscale_ip=(_run(["tailscale", "ip", "-4"]).splitlines() or [None])[0],
        throttled=_throttled(),
        os=os_name, kernel=platform.release(), model=(_read("/proc/device-tree/model", "") or "").rstrip("\x00"),
        readings=count, db_mb=round(os.path.getsize(DB_PATH) / 1048576, 1),
    )


def pi_info():
    _slow_stats()
    up = float(_read("/proc/uptime").split()[0])
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0])
    mem_total, mem_avail = mem.get("MemTotal", 1), mem.get("MemAvailable", 0)
    disk = shutil.disk_usage("/")
    iface = _default_iface()
    rx, tx = _net_rates(iface)
    freq = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    with state_lock:
        esp = [n for k, n in nodes.items() if k != PI_ID]
        online = sum(1 for n in esp if n["status"] == "online")
    return {
        "id": PI_ID, "interval_s": PI_EVERY_S,
        "uptime_s": int(up), "service_uptime_s": int(time.monotonic() - MONO_AT_START),
        "cpu_pct": _cpu_pct(), "cpu_mhz": int(freq) // 1000 if freq else None,
        "load": [round(x, 2) for x in os.getloadavg()],
        "mem_used_mb": (mem_total - mem_avail) // 1024, "mem_total_mb": mem_total // 1024,
        "swap_used_mb": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) // 1024,
        "disk_used_gb": round(disk.used / 1e9, 1), "disk_total_gb": round(disk.total / 1e9, 1),
        "iface": iface, "wifi_dbm": _wifi_dbm(iface) if iface else None,
        "net_rx_kbs": rx, "net_tx_kbs": tx, "lan_ip": _lan_ip(),
        "nodes_online": online, "nodes_total": len(esp),
        "mqtt": dict(broker),
        **{k: v for k, v in _slow.items() if k != "ts"},
    }


def pi_loop():
    tick = 0
    while True:
        now = time.time()
        try:
            info = pi_info()
            with state_lock:
                node(PI_ID).update(status="online", info=info, seen=now)
        except Exception as e:
            log.warning("Pi stats failed: %s", e)
        try:
            t = round(pi_cpu_temp(), 2)
            with state_lock:
                node(PI_ID).update(temp=t, temp_ts=now)
            if tick % 2 == 0:
                store(PI_ID, t, now)
        except OSError as e:
            if tick == 0:
                log.warning("Pi temperature unavailable: %s", e)
        tick += 1
        time.sleep(PI_EVERY_S / 2)


AIO_USER = cfg.get("AIO_USERNAME", "")
AIO_KEY = cfg.get("AIO_KEY", "")
AIO_GROUP = cfg.get("AIO_GROUP", "iothub")
AIO_BASE = cfg.get("AIO_BASE", "https://io.adafruit.com/api/v2").rstrip("/")
AIO_MAX_FEEDS = int(cfg.get("AIO_MAX_FEEDS", "10"))
AIO_RATE = int(cfg.get("AIO_POINTS_PER_MIN", "24"))
AIO_FEEDS_PATH = os.path.join(CONF_DIR, "adafruit_feeds.json")
AIO_INCLUDE_PI = cfg.get("AIO_INCLUDE_PI", "no").lower() in ("1", "yes", "true")
aio_state = {"last_ok": None, "last_error": None, "feeds": [], "skipped": []}


def _aio(method, path, body=None):
    req = urllib.request.Request(f"{AIO_BASE}/{AIO_USER}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-AIO-Key": AIO_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


AIO_ERR = {401: "wrong username or key", 403: "key not allowed for this account",
           422: "feed limit reached (free plan: 10 feeds)", 429: "rate limit hit - slowing down"}


def _aio_load_cache():
    try:
        with open(AIO_FEEDS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):
        return {k.split(".", 1)[-1]: k for k in data if isinstance(k, str)}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _aio_save_cache(cache):
    try:
        with open(AIO_FEEDS_PATH, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def _aio_feed_key(name, cache):
    if name in cache:
        return cache[name]
    try:
        _aio("GET", f"/groups/{AIO_GROUP}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        _aio("POST", "/groups", {"group": {"name": AIO_GROUP}})
    full = f"{AIO_GROUP}.{name}"
    try:
        feed = _aio("GET", f"/feeds/{full}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        feed = _aio("POST", f"/groups/{AIO_GROUP}/feeds", {"feed": {"name": name}})
        log.info("Adafruit IO: created feed %s", (feed or {}).get("key", full))
    key = (feed or {}).get("key") or full
    if "." not in key:
        key = f"{AIO_GROUP}.{key}"
    cache[name] = key
    _aio_save_cache(cache)
    return key


def adafruit_loop():
    cache = _aio_load_cache()
    while True:
        values = []
        now = time.time()
        with state_lock:
            for nid, n in sorted(nodes.items(), key=lambda kv: (kv[0] == PI_ID, kv[0])):
                if nid == PI_ID and not AIO_INCLUDE_PI:
                    continue
                if n["temp"] is None or now - (n["temp_ts"] or 0) > 120:
                    continue
                safe = re.sub(r"[^a-z0-9-]", "-", nid.lower())
                values.append((f"{safe}-temp", round(n["temp"], 2)))
                if n.get("hum") is not None and nid != PI_ID:
                    values.append((f"{safe}-hum", round(n["hum"], 1)))
        allowed, skipped = [], []
        for name, v in values:
            if name in cache or len(cache) + sum(1 for a in allowed if a[0] not in cache) < AIO_MAX_FEEDS:
                allowed.append((name, v))
            else:
                skipped.append(name)
        cycle = max(20, 60 * max(len(allowed), 1) / AIO_RATE)
        if AIO_USER and AIO_KEY and allowed:
            sent, errors = [], []
            for name, v in allowed:
                try:
                    key = _aio_feed_key(name, cache)
                    _aio("POST", f"/feeds/{key}/data", {"value": v})
                    sent.append(name)
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        cache.pop(name, None)
                        _aio_save_cache(cache)
                    errors.append(f"{name}: {AIO_ERR.get(e.code, f'HTTP {e.code}')}")
                    if e.code in (401, 403, 429):
                        cycle = 90 if e.code == 429 else cycle
                        break
                except Exception as e:
                    errors.append(f"{type(e).__name__}: {e}"[:100])
                    break
            if sent:
                aio_state.update(last_ok=time.time(), feeds=sent)
            real = [e for e in errors if "HTTP 404" not in e] or (errors if not sent else [])
            aio_state["last_error"] = "; ".join(real)[:160] or None
            aio_state["skipped"] = skipped
            if errors:
                log.warning("Adafruit IO: %s", "; ".join(errors))
        time.sleep(cycle)


NTFY_TOPIC = cfg.get("NTFY_TOPIC", "")
NTFY_SERVER = cfg.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOKEN = cfg.get("NTFY_TOKEN", "")
TG_TOKEN = cfg.get("TELEGRAM_TOKEN", "")
TG_CHAT = cfg.get("TELEGRAM_CHAT_ID", "")


def _limit(key):
    v = cfg.get(key, "")
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


LIMITS = {"temp_high": _limit("TEMP_HIGH"), "temp_low": _limit("TEMP_LOW"),
          "hum_high": _limit("HUM_HIGH"), "hum_low": _limit("HUM_LOW")}
PI_TEMP_HIGH = _limit("PI_TEMP_HIGH") or 75.0
DISK_FULL_PCT = _limit("DISK_FULL_PCT") or 90.0
OFFLINE_AFTER = int((_limit("OFFLINE_AFTER_MIN") or 2) * 60)
STARTUP_GRACE = 90

notify_q = queue.Queue(maxsize=200)
notify_state = {"last_ok": None, "last_error": None, "channels": [c for c, on in
                (("ntfy", NTFY_TOPIC), ("telegram", TG_TOKEN and TG_CHAT)) if on]}
active_alerts = {}
backlog_totals = {}
backlog_since_start = {}
LEVEL_PRIO = {"crit": "urgent", "warn": "high", "ok": "default", "info": "low"}
LEVEL_NAME = {"crit": "CRITICAL", "warn": "WARNING", "ok": "RESOLVED", "info": "INFO"}
CATEGORIES = {
    "power": "power", "pi_power": "power", "boot": "power", "shutdown": "power", "login": "security",
    "offline": "connectivity", "mqtt": "connectivity",
    "sensor": "sensor",
    "temp_high": "environment", "temp_low": "environment", "hum_high": "environment", "hum_low": "environment",
    "pi_hot": "system", "pi_disk": "system", "crash": "system", "start": "system", "storage": "system",
    "ota": "firmware", "backlog": "data", "cloud": "integration", "node_boot": "power", "data_lost": "data",
}
ACTIVE_PATH = os.path.join(DATA_DIR, "active_alerts.json")
LEVEL_TAG = {"crit": "rotating_light", "warn": "warning", "ok": "white_check_mark", "info": "information_source"}


def _ascii(t):
    return t.encode("ascii", "replace").decode()


def _send(title, body, level):
    if NTFY_TOPIC:
        req = urllib.request.Request(f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}", data=body.encode(),
                                     headers={"Title": _ascii(title), "Priority": LEVEL_PRIO.get(level, "default"),
                                              "Tags": LEVEL_TAG.get(level, "")})
        if NTFY_TOKEN:
            req.add_header("Authorization", f"Bearer {NTFY_TOKEN}")
        urllib.request.urlopen(req, timeout=10).read()
    if TG_TOKEN and TG_CHAT:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": f"{title}\n{body}"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=10).read()


def notify_loop():
    while True:
        item = notify_q.get()
        delay = 5
        while True:
            try:
                _send(*item)
                notify_state.update(last_ok=time.time(), last_error=None)
                break
            except Exception as e:
                notify_state["last_error"] = str(e)[:120]
                time.sleep(delay)
                delay = min(delay * 2, 300)


def notify(title, body, level="warn", category=""):
    if not notify_state["channels"]:
        return
    head = f"[{LEVEL_NAME.get(level, level.upper())}] {title}"
    tail = f"\n\n{category.capitalize()} - {fmt_time(time.time())}" if category else ""
    try:
        notify_q.put_nowait((head, body + tail, level))
    except queue.Full:
        log.warning("notification queue full, dropped: %s", title)


def log_event(nid, kind, level, msg, push=False, title=None):
    now = time.time()
    category = CATEGORIES.get(kind, "system")
    try:
        with db_lock:
            db.execute("INSERT INTO events (ts, node, kind, level, msg, category) VALUES (?,?,?,?,?,?)",
                       (now, nid, kind, level, msg, category))
            db.commit()
    except sqlite3.Error as e:
        log.error("could not save event (%s): %s", e, msg)
    log.info("event [%s/%s] %s: %s", category, level, nid, msg)
    if push:
        notify(title or f"{nid} {kind}", msg, level, category)


def fmt_time(ts):
    return time.strftime("%d %b %H:%M", time.localtime(ts))


def fmt_dur(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    if sec < 172800:
        return f"{sec / 3600:.1f} h"
    return f"{sec / 86400:.1f} days"


alerts_lock = threading.RLock()


def save_active_alerts():
    tmp = ACTIVE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(active_alerts, f)
        os.replace(tmp, ACTIVE_PATH)
    except OSError as e:
        log.warning("could not save active alerts: %s", e)


def load_active_alerts():
    try:
        with open(ACTIVE_PATH) as f:
            data = json.load(f)
        for k, a in data.items():
            if isinstance(a, dict) and {"node", "level", "since", "msg"} <= set(a):
                active_alerts[k] = a
                Condition.pending_since[k] = a["since"]
    except (OSError, ValueError):
        pass


class Condition:
    pending_since = {}

    @classmethod
    def check(cls, key, bad, delay, nid, level, title, msg, clear_msg):
        now = time.time()
        kind = key.split(":")[0]
        with alerts_lock:
            if bad:
                first = cls.pending_since.setdefault(key, now)
                a = active_alerts.get(key)
                if a is None and now - first >= delay:
                    active_alerts[key] = {"key": key, "node": nid, "level": level, "since": first, "msg": msg,
                                          "title": title, "category": CATEGORIES.get(kind, "system"),
                                          "ack": False, "ack_by": None}
                    save_active_alerts()
                    log_event(nid, kind, level, msg, push=True, title=title)
                    if REPORT_ALERTS and level == "crit":
                        queue_email("alert", alert={"node": nid, "level": level, "title": title, "msg": msg})
                elif a is not None:
                    a["msg"] = msg
                    a["last"] = now
            else:
                cls.pending_since.pop(key, None)
                a = active_alerts.pop(key, None)
                if a:
                    save_active_alerts()
                    log_event(nid, kind, "ok", f"{clear_msg} (after {fmt_dur(now - a['since'])})",
                              push=True, title=f"{title}")
                    if REPORT_ALERTS and a.get("level") == "crit":
                        queue_email("alert", alert={"node": nid, "level": "ok", "title": f"{title} - resolved",
                                                    "msg": f"{clear_msg} (after {fmt_dur(now - a['since'])})"})


def check_alerts():
    now = time.time()
    with state_lock:
        snap = {k: dict(v, info=dict(v["info"] or {})) for k, v in nodes.items()}
    for nid, n in snap.items():
        i = n["info"]
        if nid == PI_ID:
            t = n["temp"]
            Condition.check("pi_hot:pi", t is not None and t > PI_TEMP_HIGH, 60, nid, "warn", "Pi overheating",
                            f"Pi CPU at {t} C (limit {PI_TEMP_HIGH:g} C)", "Pi temperature back to normal")
            thr = i.get("throttled") or ""
            Condition.check("pi_power:pi", thr.startswith("NOW") and "under-voltage" in thr, 30, nid, "crit",
                            "Pi power problem", f"Under-voltage detected ({thr}). Use a proper 5V 3A supply.",
                            "Pi power supply OK again")
            used, total = i.get("disk_used_gb"), i.get("disk_total_gb")
            pct = 100 * used / total if used and total else 0
            Condition.check("pi_disk:pi", pct > DISK_FULL_PCT, 0, nid, "warn", "SD card almost full",
                            f"SD card {pct:.0f}% full", "SD card space OK")
            continue
        interval = i.get("interval_s") or 10
        seen = max(n.get("seen") or 0, n["temp_ts"] or 0)
        offline = n["status"] != "online" or now - seen > max(3 * interval, 90)
        last = fmt_time(seen) if seen else "never"
        Condition.check(f"offline:{nid}", offline, OFFLINE_AFTER, nid, "crit", f"{nid} offline",
                        f"{nid} stopped reporting (last seen {last}). It keeps storing readings and "
                        f"will send them when it reconnects.", f"{nid} is back online")
        if offline:
            continue
        no_data = n["temp_ts"] is None or now - n["temp_ts"] > max(3 * interval, 120)
        Condition.check(f"sensor:{nid}", i.get("sensor") == "missing" or no_data, 60, nid, "crit",
                        f"{nid}: temperature sensor not reading",
                        f"The temperature sensor on {nid} cannot be read, so no temperature or humidity is being "
                        f"recorded. The node itself is online. Check the sensor's power, the data wire to GPIO 1 "
                        f"and the 4.7 kOhm pull-up resistor.",
                        f"{nid}: temperature sensor reading normally again")
        for metric, unit, val in (("temp", "C", n["temp"]), ("hum", "%RH", n.get("hum"))):
            hi, lo = LIMITS[f"{metric}_high"], LIMITS[f"{metric}_low"]
            name = "Temperature" if metric == "temp" else "Humidity"
            k_hi, k_lo = f"{metric}_high:{nid}", f"{metric}_low:{nid}"
            if hi is not None and val is not None:
                bad = val > hi or (k_hi in active_alerts and val > hi - 0.5)
                Condition.check(k_hi, bad, 60, nid, "warn", f"{nid} {name.lower()} high",
                                f"{name} at {nid}: {val} {unit} (limit {hi:g})", f"{name} at {nid} back below {hi:g}")
            if lo is not None and val is not None:
                bad = val < lo or (k_lo in active_alerts and val < lo + 0.5)
                Condition.check(k_lo, bad, 60, nid, "warn", f"{nid} {name.lower()} low",
                                f"{name} at {nid}: {val} {unit} (limit {lo:g})", f"{name} at {nid} back above {lo:g}")
    with alerts_lock:
        gone = [k for k, a in active_alerts.items() if a["node"] not in snap and a["node"] != PI_ID]
        for key in gone:
            active_alerts.pop(key, None)
        if gone:
            save_active_alerts()
    if storage_state.get("error"):
        Condition.check("storage:pi", True, 0, PI_ID, "crit", "Data not being saved",
                        f"Writing readings to the SD card failed: {storage_state['error']}. "
                        f"{len(pending)} readings are held in RAM.", "Saving data to the SD card works again")
    else:
        Condition.check("storage:pi", False, 0, PI_ID, "crit", "Data not being saved", "", "Saving data works again")
    aio_on = bool(AIO_USER and AIO_KEY)
    Condition.check("cloud:pi", aio_on and bool(aio_state.get("last_error")), 600, PI_ID, "warn",
                    "Adafruit IO upload failing", f"Adafruit IO uploads failing: {aio_state.get('last_error')}",
                    "Adafruit IO uploads working again")
    Condition.check("mqtt:pi", not (mq and mq.is_connected()), 60, PI_ID, "crit", "MQTT broker down",
                    "The hub can't reach the Mosquitto broker - nodes can't deliver data",
                    "MQTT broker reachable again")
    for nid in list(backlog_totals):
        count = backlog_totals.pop(nid)
        if count:
            log_event(nid, "backlog", "info", f"{nid} delivered {count} readings it stored while offline")


def _ntp_synced():
    return _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) == "yes"


def startup_report():
    global CURRENT_OUTAGE
    if not clock["ok"]:
        for _ in range(36):
            if _ntp_synced():
                break
            time.sleep(5)
        clock_ready(verified=_ntp_synced())
    if db_recovered:
        log_event(PI_ID, "storage", "crit", db_recovered, push=True, title="Database recovered")
    hb = PREV_HEARTBEAT
    now = time.time()
    up = uptime_now()
    booted_at = now - up
    hub_at = now - (up - PROC_UP_AT_START)
    fresh_boot = PROC_UP_AT_START < 600
    if hb is None:
        kind = "first"
    elif not hb.get("clean"):
        kind = "power" if fresh_boot else "crash"
    else:
        kind = "reboot" if fresh_boot else "restart"
    last = hb.get("alive") if hb else None
    down = None if last is None else max(0.0, (booted_at if fresh_boot else hub_at) - last)
    boot_s = PROC_UP_AT_START if fresh_boot else None
    clock_s = clock.get("up") if fresh_boot else None
    with db_lock:
        cur = db.execute("INSERT INTO outages (kind, detected, last_alive, boot_at, hub_at, down_s, boot_s, clock_s) "
                         "VALUES (?,?,?,?,?,?,?,?)", (kind, now, last, booted_at if fresh_boot else None, hub_at,
                                                      down, boot_s, clock_s))
        db.commit()
        CURRENT_OUTAGE = cur.lastrowid
    boot_txt = f" Boot took {fmt_secs(boot_s)}." if boot_s else ""
    if kind == "first":
        log_event(PI_ID, "start", "info", "Hub service started for the first time", push=True, title="Hub online")
    elif kind == "crash":
        log_event(PI_ID, "crash", "warn", f"Hub service crashed and was restarted (last write {fmt_time(last)}, "
                  f"down about {fmt_dur(down)})", push=True, title="Hub service restarted")
    elif kind == "power":
        log_event(PI_ID, "power", "crit",
                  f"Pi restarted after a power cut or crash. Last seen alive {fmt_time(last)}, "
                  f"back since {fmt_time(booted_at)} (down about {fmt_dur(down)}).{boot_txt} "
                  f"Nodes will resend readings they stored meanwhile.", push=True, title="Pi recovered from power loss")
    elif kind == "reboot":
        log_event(PI_ID, "boot", "info", f"Pi rebooted (shut down {fmt_time(last)}, back {fmt_time(booted_at)}, "
                  f"down {fmt_dur(down)}).{boot_txt}", push=True, title="Pi rebooted")
    else:
        log_event(PI_ID, "start", "info", "Hub service restarted")
    write_heartbeat(clean=False)


def alert_loop():
    startup_report()
    time.sleep(STARTUP_GRACE)
    while True:
        try:
            check_alerts()
        except Exception as e:
            log.error("alert check failed: %s", e)
        time.sleep(15)


def on_shutdown(signum, frame):
    try:
        stopping = _run(["systemctl", "is-system-running"]) == "stopping"
        if stopping and notify_state["channels"]:
            try:
                _send("IoT Hub: Pi shutting down", f"Clean shutdown/reboot at {fmt_time(time.time())}", "info")
            except Exception:
                pass
        if stopping:
            log_event(PI_ID, "shutdown", "info", "Pi shutting down / rebooting")
        flush()
    finally:
        write_heartbeat(clean=True)
        os._exit(0)


SMTP_HOST = cfg.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(cfg.get("SMTP_PORT", "587") or 587)
SMTP_USER = cfg.get("SMTP_USER", "")
SMTP_PASS = cfg.get("SMTP_PASS", "").replace(" ", "")
REPORT_TO = [a.strip() for a in cfg.get("REPORT_TO", "").split(",") if "@" in a]
REPORT_TIME = cfg.get("REPORT_TIME", "")
REPORT_ALERTS = cfg.get("REPORT_ALERTS", "yes").lower() in ("1", "yes", "true")
SITE_NAME = cfg.get("SITE_NAME", "IoT Hub")
PUBLIC_URL = cfg.get("PUBLIC_URL", "")
REPORT_REQ = os.path.join(DATA_DIR, "report.request")
REPORT_STATE = os.path.join(DATA_DIR, "report_state.json")
email_q = queue.Queue(maxsize=50)
email_state = {"enabled": bool(SMTP_USER and SMTP_PASS and REPORT_TO), "last_ok": None, "last_error": None,
               "last_subject": None}
SERIES_COLORS = ["#0d7480", "#c0362c", "#6b4fbb", "#b26a00", "#2a7fd4", "#8a5a2b", "#c2417d", "#4d7c0f"]


def _period_rows(start, end, node_ids=None):
    with db_lock:
        rows = db.execute("SELECT ts, node, value, metric FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                          (start, end)).fetchall()
    rows += pending_between(start, end)
    rows.sort()
    return [r for r in rows if r[1] != PI_ID and (not node_ids or r[1] in node_ids)]


def _render_chart(rows, start, end):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime
    except ImportError:
        log.warning("matplotlib not installed - emails go out without a graph (sudo apt install python3-matplotlib)")
        return None
    series = collections.defaultdict(list)
    for ts, nid, v, m in rows:
        series[(nid, m)].append((ts, v))
    ids = sorted({k[0] for k in series})
    if not ids:
        return None
    fig, axes = plt.subplots(2, 1, figsize=(8, 5.2), dpi=110, sharex=True)
    for ax, metric, label in ((axes[0], "temp", "Temperature (°C)"), (axes[1], "hum", "Humidity (%RH)")):
        for i, nid in enumerate(ids):
            pts = series.get((nid, metric))
            if not pts:
                continue
            xs, ys, prev = [], [], None
            gap = max(600, (end - start) / 100)
            for t, v in pts:
                if prev is not None and t - prev > gap:
                    xs.append(datetime.fromtimestamp(prev + 1)); ys.append(float("nan"))
                xs.append(datetime.fromtimestamp(t)); ys.append(v); prev = t
            ax.plot(xs, ys, color=SERIES_COLORS[i % len(SERIES_COLORS)], linewidth=1.8, label=nid)
        ax.set_ylabel(label, fontsize=9, color="#4c5a68")
        ax.grid(True, color="#dde3e9", linewidth=.8)
        ax.tick_params(labelsize=8, colors="#4c5a68")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c9d1d9")
    axes[0].legend(fontsize=8, frameon=False, ncol=min(len(ids), 6), loc="upper left", bbox_to_anchor=(0, 1.22))
    span = end - start
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if span <= 2 * 86400 else "%d %b"))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _stats(rows):
    out = collections.defaultdict(dict)
    acc = collections.defaultdict(list)
    for _, nid, v, m in rows:
        acc[(nid, m)].append(v)
    for (nid, m), vals in acc.items():
        out[nid][m] = (min(vals), sum(vals) / len(vals), max(vals), len(vals))
    return out


def build_report(hours=24, node_ids=None, title=None, intro=None):
    end = time.time()
    start = end - hours * 3600
    rows = _period_rows(start, end, node_ids)
    stats = _stats(rows)
    png = _render_chart(rows, start, end)
    with state_lock:
        snap = {k: (v["status"], v["temp"], v.get("hum"), v["temp_ts"]) for k, v in nodes.items() if k != PI_ID}
    if node_ids:
        snap = {k: v for k, v in snap.items() if k in node_ids}
    with db_lock:
        evs = db.execute("SELECT ts, node, level, msg FROM events WHERE ts >= ? AND level IN ('crit','warn','ok') "
                         "AND category NOT IN ('security', 'system') ORDER BY ts DESC LIMIT 15", (start,)).fetchall()
        outs = db.execute("SELECT kind, last_alive, down_s, boot_s, lost, recovered FROM outages WHERE detected >= ? "
                          "AND kind IN ('power','reboot','crash') ORDER BY detected", (start,)).fetchall()
        n_boots = db.execute("SELECT COUNT(*) FROM node_boots WHERE ts >= ?", (start,)).fetchone()[0]
    with alerts_lock:
        open_alerts = [a for a in active_alerts.values() if not node_ids or a["node"] in node_ids]
    intr = [f"{OUTAGE_LABEL.get(k, k)} at {fmt_time(la) if la else '?'}: down {fmt_dur(d) if d else '-'}"
            + (f", boot {fmt_secs(b)}" if b else "") + (f", {lo} readings lost" if lo else ", no readings lost" if lo == 0 else "")
            + (f", {rc} restored from nodes" if rc else "") for k, la, d, b, lo, rc in outs]
    if n_boots:
        intr.append(f"{n_boots} node restart{'s' if n_boots > 1 else ''}")
    period = f"{fmt_time(start)} to {fmt_time(end)}"
    title = title or f"{SITE_NAME} report"
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    f1 = lambda v: "-" if v is None else f"{v:.1f}"

    rows_html, rows_txt = [], []
    for nid in sorted(set(snap) | set(stats)):
        st, t, h, tts = snap.get(nid, ("unknown", None, None, None))
        live = st == "online" and tts and end - tts < 300
        s_t, s_h = stats.get(nid, {}).get("temp"), stats.get(nid, {}).get("hum")
        now_txt = f"{f1(t)} °C, {f1(h)} %RH" if live else ("offline" if st != "online" else "sensor not reading")
        rng_t = f"{s_t[0]:.1f} / {s_t[1]:.1f} / {s_t[2]:.1f} °C" if s_t else "no data"
        rng_h = f"{s_h[0]:.0f} / {s_h[1]:.0f} / {s_h[2]:.0f} %RH" if s_h else "no data"
        colour = "#22804a" if live else "#c0362c"
        rows_html.append(f"<tr><td style='padding:8px 10px;border-bottom:1px solid #dde3e9'><b>{esc(nid)}</b></td>"
                         f"<td style='padding:8px 10px;border-bottom:1px solid #dde3e9;color:{colour}'>{esc(now_txt)}</td>"
                         f"<td style='padding:8px 10px;border-bottom:1px solid #dde3e9'>{rng_t}</td>"
                         f"<td style='padding:8px 10px;border-bottom:1px solid #dde3e9'>{rng_h}</td></tr>")
        rows_txt.append(f"{nid}: now {now_txt}; temperature low/avg/high {rng_t}; humidity {rng_h}")

    alert_html = "".join(f"<li style='margin:4px 0'><b style='color:{'#c0362c' if a['level'] == 'crit' else '#b26a00'}'>"
                         f"{'Problem' if a['level'] == 'crit' else 'Warning'}:</b> {esc(a['msg'])} "
                         f"<span style='color:#7b8794'>(since {fmt_time(a['since'])})</span></li>" for a in open_alerts)
    ev_html = "".join(f"<tr><td style='padding:4px 10px 4px 0;color:#7b8794;white-space:nowrap'>{fmt_time(t)}</td>"
                      f"<td style='padding:4px 0'>{esc(m)}</td></tr>" for t, _, _, m in evs)
    link = f"<p><a href='{esc(PUBLIC_URL)}' style='color:#0d7480'>Open the live dashboard</a></p>" if PUBLIC_URL else ""
    html = f"""<div style="font-family:Segoe UI,Roboto,Arial,sans-serif;color:#18232e;max-width:720px">
<h2 style="margin:0 0 4px;font-weight:600">{esc(title)}</h2>
<p style="margin:0 0 16px;color:#7b8794">{period}</p>
{f'<p style="margin:0 0 16px">{esc(intro)}</p>' if intro else ''}
{f'<div style="background:#fbe9e7;border-left:4px solid #c0362c;padding:10px 14px;margin:0 0 16px"><b>Open problems</b><ul style="margin:6px 0 0;padding-left:18px">{alert_html}</ul></div>' if open_alerts else '<p style="margin:0 0 16px;color:#22804a">No open problems.</p>'}
{'<img src="cid:chart" alt="Temperature and humidity chart" style="width:100%;max-width:720px;border:1px solid #dde3e9;border-radius:8px">' if png else ''}
<table style="border-collapse:collapse;width:100%;margin:16px 0;font-size:14px">
<tr style="text-align:left;color:#7b8794;font-size:12.5px"><th style="padding:6px 10px">Node</th><th style="padding:6px 10px">Now</th>
<th style="padding:6px 10px">Temperature low / avg / high</th><th style="padding:6px 10px">Humidity low / avg / high</th></tr>
{''.join(rows_html) or '<tr><td style="padding:8px 10px" colspan="4">No readings in this period.</td></tr>'}</table>
{f'<h3 style="font-size:15px;margin:18px 0 6px">Interruptions</h3><ul style="margin:0;padding-left:18px;font-size:13.5px">' + ''.join(f'<li style="margin:3px 0">{esc(x)}</li>' for x in intr) + '</ul>' if intr else '<p style="margin:12px 0;color:#22804a">No power cuts or restarts in this period.</p>'}
{f'<h3 style="font-size:15px;margin:18px 0 6px">Events</h3><table style="font-size:13px">{ev_html}</table>' if evs else ''}
{link}
<p style="color:#7b8794;font-size:12px;margin-top:20px">Sent by {esc(SITE_NAME)} (Raspberry Pi hub). All readings are attached as a CSV file.</p></div>"""
    text = "\n".join([title, period, ""] + (["OPEN PROBLEMS: " + "; ".join(a["msg"] for a in open_alerts), ""] if open_alerts else [])
                     + rows_txt + (["", "Interruptions: " + "; ".join(intr)] if intr else [])
                     + (["", f"Dashboard: {PUBLIC_URL}"] if PUBLIC_URL else []))
    csv_buf = io.StringIO()
    w = csv.writer(csv_buf)
    w.writerow(["time", "node", "metric", "value"])
    w.writerows((time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), nid, m, round(v, 2)) for t, nid, v, m in rows)
    return html, text, png, csv_buf.getvalue().encode()


def send_email(to, subject, html, text, png=None, csv_bytes=None, csv_name="readings.csv"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SITE_NAME, SMTP_USER))
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    cid = make_msgid()
    msg.add_alternative(html.replace("cid:chart", f"cid:{cid[1:-1]}"), subtype="html")
    if png:
        msg.get_payload()[1].add_related(png, "image", "png", cid=cid, filename="chart.png")
    if csv_bytes:
        msg.add_attachment(csv_bytes, maintype="text", subtype="csv", filename=csv_name)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def queue_email(kind, **kw):
    if not email_state["enabled"] and not kw.get("to"):
        return False
    try:
        email_q.put_nowait((kind, kw))
        return True
    except queue.Full:
        return False


def email_loop():
    while True:
        kind, kw = email_q.get()
        to = kw.get("to") or REPORT_TO
        if kind == "report":
            hours = kw.get("hours", 24)
            subject = f"{SITE_NAME}: {'daily ' if kw.get('daily') else ''}report for {time.strftime('%d %b %Y')}"
            html, text, png, csvb = build_report(hours)
        else:
            a = kw["alert"]
            word = {"crit": "PROBLEM", "warn": "Warning", "ok": "Resolved"}.get(a["level"], "Notice")
            subject = f"{SITE_NAME} {word}: {a['title']}"
            html, text, png, csvb = build_report(6, [a["node"]] if a["node"] != PI_ID else None,
                                                 title=f"{word}: {a['title']}", intro=a["msg"])
        delay = 30
        for _ in range(12):
            try:
                send_email(to, subject, html, text, png, csvb, f"readings-{time.strftime('%Y%m%d-%H%M')}.csv")
                email_state.update(last_ok=time.time(), last_error=None, last_subject=subject)
                log.info("emailed '%s' to %s", subject, ", ".join(to))
                break
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused) as e:
                email_state["last_error"] = f"rejected: {e}"[:160]
                log.error("email rejected: %s", e)
                break
            except Exception as e:
                email_state["last_error"] = str(e)[:160]
                time.sleep(delay)
                delay = min(delay * 2, 600)


def _report_state():
    try:
        with open(REPORT_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def report_loop():
    while True:
        time.sleep(20)
        if os.path.exists(REPORT_REQ):
            try:
                with open(REPORT_REQ) as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = {}
            try:
                os.remove(REPORT_REQ)
            except OSError:
                pass
            to = [a for a in req.get("to", []) if "@" in a] or None
            if email_state["enabled"] or (to and SMTP_USER and SMTP_PASS):
                queue_email("report", hours=float(req.get("hours", 24)), to=to)
        if REPORT_TIME and email_state["enabled"]:
            today = time.strftime("%Y-%m-%d")
            if time.strftime("%H:%M") >= REPORT_TIME and _report_state().get("last_daily") != today:
                queue_email("report", hours=24, daily=True)
                try:
                    with open(REPORT_STATE, "w") as f:
                        json.dump({"last_daily": today}, f)
                except OSError:
                    pass


def flush_loop():
    while True:
        time.sleep(FLUSH_S)
        try:
            n = flush()
            if n:
                log.info("Wrote %d readings to disk", n)
        except Exception as e:
            log.error("flush failed: %s", e)


def cleanup_loop():
    while True:
        time.sleep(3600)
        with db_lock:
            db.execute("DELETE FROM readings WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
            db.execute("DELETE FROM events WHERE ts < ?", (time.time() - 365 * 86400,))
            db.commit()


FIRST_DATA = {"up": None}
CURRENT_OUTAGE = None
ANALYSE_AFTER_S = 600
OUTAGE_LABEL = {"power": "Power cut", "reboot": "Reboot", "crash": "Hub service crash",
                "restart": "Service restart", "first": "First start"}
NODE_REASON = {"power_on": "power came back on", "brownout": "supply voltage dipped (brownout)",
               "software": "restarted by a command or update", "panic": "firmware crash",
               "watchdog": "watchdog reset (froze)", "ext": "reset button", "deepsleep": "woke from sleep"}
node_boot_seen = {}


def fmt_secs(s):
    if s is None:
        return "-"
    return f"{s:.0f} s" if s < 90 else fmt_dur(s)


def note_node_boot(nid, info):
    up = info.get("uptime_s")
    if not clock["ok"] or not clock["verified"] or not isinstance(up, (int, float)) or up < 0:
        return
    boot_at = time.time() - up
    boot_no = info.get("boot") if isinstance(info.get("boot"), int) else None
    seen = node_boot_seen.get(nid)
    if seen is None:
        with db_lock:
            row = db.execute("SELECT ts, boot FROM node_boots WHERE node = ? ORDER BY ts DESC LIMIT 1", (nid,)).fetchone()
        seen = tuple(row) if row else None
    prev = seen[0] if seen else None
    if seen is not None:
        same = boot_no == seen[1] if boot_no is not None and seen[1] is not None else abs(boot_at - prev) < 120
        if same:
            node_boot_seen[nid] = seen
            return
    node_boot_seen[nid] = (boot_at, boot_no)
    reason = info.get("reset") or None
    with db_lock:
        db.execute("INSERT INTO node_boots (ts, node, reason, boot) VALUES (?,?,?,?)",
                   (boot_at, nid, reason, info.get("boot")))
        db.commit()
    if prev is not None:
        why = NODE_REASON.get(reason, "reason not reported (firmware older than 1.4.1)")
        log_event(nid, "node_boot", "info" if reason == "software" else "warn",
                  f"{nid} restarted at {fmt_time(boot_at)}: {why}")


def _node_interval(nid):
    if nid == PI_ID:
        return PI_EVERY_S
    with state_lock:
        iv = (nodes.get(nid) or {}).get("info", {}).get("interval_s")
    return float(iv) if isinstance(iv, (int, float)) and iv > 0 else 10.0


def _gaps(nid, start, end=None, min_missing=1):
    iv = _node_interval(nid)
    thr = max(2.5 * iv, iv + 20)
    q = ("SELECT prev, ts FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev FROM readings "
         "WHERE node = ? AND metric = 'temp' AND ts >= ?" + (" AND ts <= ?" if end else "") + ") WHERE ts - prev > ?")
    args = (nid, start) + ((end,) if end else ()) + (thr,)
    with db_lock:
        rows = db.execute(q, args).fetchall()
    out = []
    for a, b in rows:
        miss = int(round((b - a) / iv)) - 1
        if miss >= min_missing:
            out.append((a, b, miss))
    return out, iv


def analyse_outage(oid):
    with db_lock:
        r = db.execute("SELECT kind, last_alive, hub_at FROM outages WHERE id = ?", (oid,)).fetchone()
    if not r:
        return
    kind, last, hub_at = r
    current = oid == CURRENT_OUTAGE
    detail, lost, recovered = {"nodes": {}}, 0, 0
    if last is not None:
        try:
            flush()
        except Exception:
            pass
        lo, hi = last - 1800, hub_at + ANALYSE_AFTER_S
        with db_lock:
            ids = [x[0] for x in db.execute("SELECT DISTINCT node FROM readings WHERE metric = 'temp' AND ts >= ? "
                                            "AND ts <= ? AND node != ?", (lo, hi, PI_ID))]
        for nid in ids:
            gaps, iv = _gaps(nid, lo, hi)
            gaps = [g for g in gaps if g[1] > last - iv and g[0] < hub_at + 600]
            miss = sum(g[2] for g in gaps)
            rec = backlog_since_start.get(nid, 0) if current else None
            detail["nodes"][nid] = {"missing": miss, "gap_s": round(sum(g[1] - g[0] - iv for g in gaps)),
                                    "from": gaps[0][0] if gaps else None, "to": gaps[-1][1] if gaps else None,
                                    "interval_s": iv, "recovered": rec}
            lost += miss
            recovered += rec or 0
    if current and kind in ("power", "reboot"):
        sa = _run(["systemd-analyze"]) or ""
        m = re.search(r"Startup finished in (.*?)(?:\n|$)", sa)
        if m:
            detail["systemd"] = m.group(1).strip()
    first = FIRST_DATA["up"] if current and kind in ("power", "reboot") else None
    with db_lock:
        db.execute("UPDATE outages SET lost = ?, recovered = ?, detail = ?, first_data_s = ?, analysed = 1 WHERE id = ?",
                   (lost, recovered if current else None, json.dumps(detail), first, oid))
        db.commit()
    _intr_cache.clear()
    if lost:
        per = ", ".join(f"{k} {v['missing']} ({fmt_dur(v['gap_s'])})" for k, v in detail["nodes"].items() if v["missing"])
        extra = f" {recovered} readings were restored from node memory." if recovered else ""
        log_event(PI_ID, "data_lost", "warn", f"{lost} readings missing after the {OUTAGE_LABEL.get(kind, kind).lower()} "
                  f"of {fmt_time(last)}: {per}.{extra}")


def outage_loop():
    while True:
        time.sleep(60)
        with db_lock:
            todo = db.execute("SELECT id, detected FROM outages WHERE analysed = 0").fetchall()
        for oid, detected in todo:
            if time.time() - detected >= ANALYSE_AFTER_S or oid != CURRENT_OUTAGE:
                analyse_outage(oid)


_intr_cache = {}


def interruptions(days):
    hit = _intr_cache.get(days)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    now = time.time()
    start = now - days * 86400
    raw_start = max(start, now - KEEP_DAYS * 86400)
    with db_lock:
        outs = db.execute("SELECT id, kind, detected, last_alive, boot_at, hub_at, down_s, boot_s, clock_s, first_data_s, "
                          "lost, recovered, detail, analysed FROM outages WHERE detected >= ? ORDER BY detected DESC",
                          (start,)).fetchall()
        boots = db.execute("SELECT ts, node, reason FROM node_boots WHERE ts >= ? ORDER BY ts DESC", (start,)).fetchall()
        pi_ev = db.execute("SELECT ts, kind FROM events WHERE node = ? AND kind IN ('power','boot','crash','start') "
                           "AND ts >= ?", (PI_ID, start - 86400)).fetchall()
        counts = dict(db.execute("SELECT kind, COUNT(*) FROM events WHERE node = ? AND kind IN ('power','boot','crash') "
                                 "AND ts >= ? GROUP BY kind", (PI_ID, start)).fetchall())
        ids = {x[0] for x in db.execute("SELECT DISTINCT node FROM daily WHERE day >= ?", (day_of(raw_start),))}
    with state_lock:
        ids |= set(nodes)
        live = {k: (v.get("status"), v.get("temp_ts")) for k, v in nodes.items()}
    outages = []
    for o in outs:
        d = json.loads(o[12]) if o[12] else {}
        outages.append({"id": o[0], "kind": o[1], "label": OUTAGE_LABEL.get(o[1], o[1]), "detected": o[2],
                        "last_alive": o[3], "boot_at": o[4], "hub_at": o[5], "down_s": o[6], "boot_s": o[7],
                        "clock_s": o[8], "first_data_s": o[9], "lost": o[10], "recovered": o[11],
                        "nodes": d.get("nodes", {}), "systemd": d.get("systemd"), "analysed": bool(o[13])})

    def cause(nid, a, b):
        why = []
        for o in outages:
            la = o["last_alive"]
            if o["kind"] != "first" and la is not None and la - 120 <= b and o["hub_at"] + 900 >= a:
                why.append(f"Pi {o['label'].lower()}")
                break
        else:
            if any(a <= t <= b + 900 for t, _ in pi_ev):
                why.append("Pi restarted")
        if nid != PI_ID:
            nb = [r for t, n, r in boots if n == nid and a - 60 <= t <= b + 60]
            if nb:
                why.append(f"node restarted ({NODE_REASON.get(nb[0], 'reason unknown')})")
            if not why:
                why.append("node not heard from (Wi-Fi, power or sensor)")
        return "; ".join(why) or "hub not recording"

    gaps, per_node = [], []
    for nid in sorted(ids, key=lambda x: (x == PI_ID, x)):
        g, iv = _gaps(nid, raw_start)
        with db_lock:
            got, last_ts = db.execute("SELECT COUNT(*), MAX(ts) FROM readings WHERE node = ? AND metric = 'temp' "
                                      "AND ts >= ?", (nid, raw_start)).fetchone()
        st, tts = live.get(nid, (None, None))
        last_ts = max(filter(None, [last_ts, tts]), default=None)
        missing = sum(x[2] for x in g)
        for a, b, m in g:
            gaps.append({"node": nid, "from": a, "to": b, "missing": m, "cause": cause(nid, a, b),
                         "estimated": b - a <= FILL_S})
        if nid != PI_ID and last_ts and st != "online" and now - last_ts > max(2.5 * iv, 60):
            m = int((now - last_ts) / iv)
            missing += m
            gaps.append({"node": nid, "from": last_ts, "to": None, "missing": m, "cause": "node offline now"})
        exp = got + missing
        per_node.append({"node": nid, "received": got, "missing": missing, "interval_s": iv,
                         "complete_pct": round(100 * got / exp, 2) if exp else None,
                         "gaps": sum(1 for x in gaps if x["node"] == nid),
                         "restarts": sum(1 for _, n, _ in boots if n == nid)})
    gaps.sort(key=lambda x: x["from"], reverse=True)
    boot_times = [o["boot_s"] for o in outages if o["boot_s"]]
    down = [o["down_s"] for o in outages if o["down_s"] and o["kind"] in ("power", "reboot", "crash")]
    esp = [p for p in per_node if p["node"] != PI_ID]
    summary = {
        "power_cuts": counts.get("power", 0), "reboots": counts.get("boot", 0), "crashes": counts.get("crash", 0),
        "node_restarts": len(boots),
        "node_power_restarts": sum(1 for _, _, r in boots if r in ("power_on", "brownout", None)),
        "last_boot_s": boot_times[0] if boot_times else None,
        "avg_boot_s": round(sum(boot_times) / len(boot_times), 1) if boot_times else None,
        "fastest_boot_s": min(boot_times) if boot_times else None,
        "downtime_s": round(sum(down)) if down else 0,
        "missing": sum(p["missing"] for p in esp), "received": sum(p["received"] for p in esp),
        "recovered": sum(o["recovered"] or 0 for o in outages),
        "uptime_s": round(uptime_now()), "hub_running_s": round(time.monotonic() - MONO_AT_START),
        "clock_verified": clock["verified"], "raw_from": raw_start,
    }
    exp = summary["received"] + summary["missing"]
    summary["complete_pct"] = round(100 * summary["received"] / exp, 2) if exp else None
    res = {"days": days, "now": now, "summary": summary, "outages": outages, "per_node": per_node,
           "gaps": gaps[:300], "node_boots": [{"ts": t, "node": n, "reason": r,
                                               "reason_txt": NODE_REASON.get(r, "not reported")} for t, n, r in boots[:200]]}
    _intr_cache[days] = (time.time(), res)
    return res


app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False


class BadRequest(Exception):
    pass


def arg_num(name, default, lo, hi, cast=int):
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        v = cast(raw)
    except ValueError:
        raise BadRequest(f"'{name}' must be a number")
    return min(max(v, lo), hi)


@app.errorhandler(BadRequest)
def _bad_request(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def _server_error(e):
    code = getattr(e, "code", 500)
    if isinstance(code, int) and 400 <= code < 500:
        return jsonify(error=getattr(e, "description", str(e))), code
    log.exception("API error on %s", request.path)
    return jsonify(error="internal error - see the hub log (journalctl -u iothub)"), 500


@app.after_request
def _no_cache(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


def _same(a, b):
    return hmac.compare_digest(str(a or "").encode(), str(b or "").encode())


def _session_key():
    path = os.path.join(CONF_DIR, "session.key")
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


app.secret_key = _session_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  SESSION_COOKIE_NAME="iothub_session", PERMANENT_SESSION_LIFETIME=30 * 86400)
PW_TAG = hmac.new(app.secret_key, DASH_PASSWORD.encode(), "sha256").hexdigest()[:16] if DASH_PASSWORD else ""
login_failures = collections.deque(maxlen=200)


@app.before_request
def auth():
    if not DASH_PASSWORD:
        g.role = "admin"
    else:
        g.role = "admin" if session.get("admin") == PW_TAG else "guest"
    if request.method == "POST" and request.path not in ("/api/login", "/api/logout") and g.role != "admin":
        return jsonify(error="Sign in as admin to do this", login=True), 403
    return None


@app.post("/api/login")
def api_login():
    now = time.time()
    recent = [t for t in login_failures if now - t < 600]
    if len(recent) >= 10:
        return jsonify(error="Too many wrong passwords. Try again in 10 minutes."), 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if not DASH_PASSWORD or not _same(pw, DASH_PASSWORD):
        login_failures.append(now)
        time.sleep(1)
        if DASH_PASSWORD and len(recent) + 1 in (3, 10):
            log_event(PI_ID, "login", "warn", f"Wrong admin password entered {len(recent) + 1} times in 10 min"
                      + (" - sign-in locked for 10 min" if len(recent) + 1 == 10 else ""))
        return jsonify(error="Wrong password" if DASH_PASSWORD else "No admin password is set on the hub"), 401
    session.clear()
    session.permanent = True
    session["admin"] = PW_TAG
    log_event(PI_ID, "login", "info", "Admin signed in to the dashboard")
    return jsonify(ok=True, role="admin")


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True, role="guest" if DASH_PASSWORD else "admin")


@app.get("/api/me")
def api_me():
    return jsonify(role=g.role, password_set=bool(DASH_PASSWORD))


GUEST_HIDDEN = ("ip", "lan_ip", "tailscale_ip", "hub", "wifi")


def _mask(addr):
    user, _, dom = addr.partition("@")
    return (user[:2] + "***@" + dom) if dom else "***"


def _for_guest(info):
    if g.role == "admin" or not isinstance(info, dict):
        return info
    return {k: v for k, v in info.items() if k not in GUEST_HIDDEN}


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


APP_FILES = {"manifest.webmanifest": "application/manifest+json", "sw.js": "text/javascript",
             "icon-192.png": "image/png", "icon-512.png": "image/png", "icon-maskable.png": "image/png",
             "apple-touch-icon.png": "image/png"}


@app.get("/<name>")
def app_file(name):
    if name not in APP_FILES:
        return jsonify(error="not found"), 404
    resp = send_from_directory(STATIC, name, mimetype=APP_FILES[name])
    if name == "sw.js":
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
    return resp


LEVEL_ORDER = {"crit": 0, "warn": 1, "info": 2}


def _alerts_list():
    with alerts_lock:
        items = [dict(a, key=k) for k, a in active_alerts.items()]
    return sorted(items, key=lambda a: (a.get("ack", False), LEVEL_ORDER.get(a["level"], 3), a["since"]))


@app.get("/api/nodes")
def api_nodes():
    now = time.time()
    out = []
    with state_lock:
        for n in nodes.values():
            interval = (n["info"] or {}).get("interval_s") or 10
            seen = max(n.get("seen") or 0, n["temp_ts"] or 0)
            stale = now - seen > max(3 * interval, 90)
            out.append({
                "id": n["id"], "is_pi": n["id"] == PI_ID,
                "status": "offline" if (n["status"] == "online" and stale) else n["status"],
                "temp": n["temp"], "temp_ts": n["temp_ts"], "hum": n.get("hum"), "info": _for_guest(n["info"]), "led": n["led"],
                "events": list(n["events"]),
            })
    out.sort(key=lambda x: (not x["is_pi"], x["id"]))
    return jsonify({
        "now": now, "role": g.role, "nodes": out, "mqtt": bool(mq and mq.is_connected()),
        "adafruit": {"enabled": bool(AIO_USER and AIO_KEY), "group": AIO_GROUP, **aio_state,
                     "user": AIO_USER if g.role == "admin" else None},
        "alerts": _alerts_list(),
        "notify": {k: v for k, v in notify_state.items()},
        "email": {**email_state, "daily_at": REPORT_TIME or None, "alerts": REPORT_ALERTS,
                  "to": REPORT_TO if g.role == "admin" else [_mask(a) for a in REPORT_TO]},
        "storage": {**storage_state, "pending": len(pending), "flush_s": FLUSH_S, "keep_days": KEEP_DAYS},
    })


def _metric():
    m = request.args.get("metric", "temp")
    return m if m in METRICS else "temp"


def _range():
    now = time.time()
    if "start" in request.args:
        start = arg_num("start", now - 86400, 0, now, float)
        end = arg_num("end", now, start, now + 60, float)
    else:
        hours = arg_num("hours", 6, 0.25, 24 * 3650, float)
        start, end = now - hours * 3600, now
    return start, max(end, start + 60)


@app.get("/api/history")
def api_history():
    start, end = _range()
    metric = _metric()
    step = max(10, int((end - start) / 360))
    with db_lock:
        rows = db.execute(
            "SELECT node, CAST(ts / ? AS INTEGER) * ? AS b, SUM(value), COUNT(*) FROM readings "
            "WHERE ts >= ? AND ts < ? AND metric = ? GROUP BY node, b",
            (step, step, start, end, metric)).fetchall()
    acc = {(nid, b): [sv, c] for nid, b, sv, c in rows}
    for ts, nid, v, _ in pending_between(start, end, metric):
        a = acc.setdefault((nid, int(ts // step) * step), [0.0, 0])
        a[0] += v
        a[1] += 1
    series = collections.defaultdict(list)
    for (nid, b), (sv, c) in sorted(acc.items(), key=lambda kv: kv[0][1]):
        series[nid].append([b + step / 2, round(sv / c, 2)])
    return jsonify({"step": step, "metric": metric, "start": start, "end": end, "series": series})


@app.get("/api/daily")
def api_daily():
    metric = _metric()
    days = arg_num("days", 30, 1, 3650)
    first = day_of(time.time() - (days - 1) * 86400)
    with db_lock:
        rows = db.execute("SELECT day, node, min, avg, max, n FROM daily WHERE metric = ? AND day >= ? "
                          "ORDER BY day", (metric, first)).fetchall()
    series = collections.defaultdict(list)
    for day, nid, mn, av, mx, n in rows:
        series[nid].append([day, round(mn, 2), round(av, 2), round(mx, 2), n])
    return jsonify({"metric": metric, "first": first, "series": series})


def _csv_response(header, rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([[("'" + c) if isinstance(c, str) and c and c[0] in "=+-@" else c for c in r] for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export.csv")
def api_export():
    start, end = _range()
    end = min(end, start + 31 * 86400)
    with db_lock:
        rows = db.execute("SELECT ts, node, metric, value FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                          (start, end)).fetchall()
    rows += [(r[0], r[1], r[3], r[2]) for r in pending_between(start, end)]
    rows.sort()
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    if request.args.get("fill") != "1":
        out = [(fmt(t), nid, m, round(v, 2)) for t, nid, m, v in rows]
        return _csv_response(["time", "node", "metric", "value"], out, f"readings_{day_of(start)}.csv")
    out, last = [], {}
    for t, nid, m, v in rows:
        p = last.get((nid, m))
        if p:
            iv = _node_interval(nid)
            if 2.5 * iv < t - p[0] <= FILL_S:
                n = int(round((t - p[0]) / iv)) - 1
                for k in range(1, n + 1):
                    te = p[0] + (t - p[0]) * k / (n + 1)
                    out.append((te, fmt(te), nid, m, round(p[1] + (v - p[1]) * k / (n + 1), 2), "estimated"))
        out.append((t, fmt(t), nid, m, round(v, 2), "measured"))
        last[(nid, m)] = (t, v)
    out.sort(key=lambda r: r[0])
    return _csv_response(["time", "node", "metric", "value", "source"], [r[1:] for r in out],
                         f"readings_{day_of(start)}_filled.csv")


@app.get("/api/daily.csv")
def api_daily_csv():
    days = arg_num("days", 365, 1, 3650)
    with db_lock:
        rows = db.execute("SELECT day, node, metric, ROUND(min,2), ROUND(avg,2), ROUND(max,2), n FROM daily "
                          "WHERE day >= ? ORDER BY day, node, metric",
                          (day_of(time.time() - (days - 1) * 86400),)).fetchall()
    return _csv_response(["day", "node", "metric", "min", "avg", "max", "readings"], rows, "daily_summary.csv")


def _event_filter():
    where, params = [], []
    for col, name in (("category", "category"), ("level", "level"), ("node", "node")):
        v = request.args.get(name, "")
        if v:
            vals = [x for x in v.split(",") if re.fullmatch(r"[a-z0-9_-]{1,24}", x)]
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                params += vals
    q = request.args.get("q", "").strip()[:60]
    if q:
        where.append("msg LIKE ?")
        params.append(f"%{q}%")
    since = request.args.get("since")
    if since:
        where.append("ts >= ?")
        params.append(arg_num("since", 0, 0, time.time(), float))
    return (" WHERE " + " AND ".join(where)) if where else "", params


@app.get("/api/events")
def api_events():
    limit = arg_num("limit", 50, 1, 500)
    offset = arg_num("offset", 0, 0, 10**7)
    where, params = _event_filter()
    with db_lock:
        total = db.execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()[0]
        rows = db.execute(f"SELECT ts, node, kind, level, msg, category FROM events{where} "
                          "ORDER BY ts DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        counts = dict(db.execute("SELECT category, COUNT(*) FROM events WHERE ts > ? GROUP BY category",
                                 (time.time() - 7 * 86400,)).fetchall())
    return jsonify({"total": total, "offset": offset, "counts_7d": counts,
                    "events": [{"ts": t, "node": n, "kind": k, "level": lv, "msg": m, "category": c}
                               for t, n, k, lv, m, c in rows]})


@app.get("/api/events.csv")
def api_events_csv():
    where, params = _event_filter()
    with db_lock:
        rows = db.execute(f"SELECT ts, node, category, level, msg FROM events{where} ORDER BY ts DESC LIMIT 20000",
                          params).fetchall()
    out = [(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), n, c, LEVEL_NAME.get(lv, lv), m)
           for t, n, c, lv, m in rows]
    return _csv_response(["time", "node", "category", "severity", "message"], out, "events.csv")


@app.post("/api/alerts/<path:key>/ack")
def api_ack(key):
    with alerts_lock:
        a = active_alerts.get(key)
        if a is None:
            return jsonify(error="alert is no longer active"), 404
        a["ack"] = not a.get("ack", False)
        a["ack_at"] = time.time() if a["ack"] else None
        save_active_alerts()
    log_event(a["node"], key.split(":")[0], "info",
              f"Alert {'acknowledged' if a['ack'] else 'un-acknowledged'}: {a['msg']}")
    return jsonify(ok=True, ack=a["ack"])


@app.get("/api/interruptions")
def api_interruptions():
    return jsonify(interruptions(arg_num("days", 30, 1, 365)))


@app.get("/api/interruptions.csv")
def api_interruptions_csv():
    d = interruptions(arg_num("days", 30, 1, 365))
    t = lambda x: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x)) if x else ""
    rows = []
    for o in d["outages"]:
        nodes_txt = "; ".join(f"{k}: {v['missing']} missing" for k, v in o["nodes"].items())
        rows.append(["pi", o["label"], t(o["last_alive"]), t(o["boot_at"] or o["hub_at"]),
                     round(o["down_s"]) if o["down_s"] is not None else "", o["lost"] if o["lost"] is not None else "",
                     f"boot {round(o['boot_s'])} s" if o["boot_s"] else "", nodes_txt])
    for b in d["node_boots"]:
        rows.append([b["node"], "Node restart", t(b["ts"]), "", "", "", b["reason_txt"], ""])
    for g in d["gaps"]:
        rows.append([g["node"], "Data gap", t(g["from"]), t(g["to"]) if g["to"] else "ongoing",
                     round((g["to"] or d["now"]) - g["from"]), g["missing"], g["cause"], ""])
    rows.sort(key=lambda r: r[2], reverse=True)
    return _csv_response(["node", "type", "from", "to", "duration_s", "readings_missing", "detail", "per_node"],
                         rows, f"interruptions_{d['days']}d.csv")


@app.get("/api/health")
def api_health():
    now = time.time()
    threads = {name: {"alive": t.is_alive(), "restarts": workers_restarts.get(name, 0),
                      "last_error": workers_errors.get(name)} for name, t in workers.items()}
    healthy = all(v["alive"] for v in threads.values()) and not storage_state.get("error")
    return jsonify({
        "ok": healthy, "version": HUB_VERSION, "uptime_s": int(time.monotonic() - MONO_AT_START),
        "mqtt": bool(mq and mq.is_connected()), "threads": threads,
        "storage": {**storage_state, "pending": len(pending), "db_recovered": db_recovered},
        "notify_queue": notify_q.qsize(),
    }), (200 if healthy else 503)


@app.get("/api/config")
def api_config():
    return jsonify({
        "version": HUB_VERSION,
        "limits": {k: v for k, v in LIMITS.items()}, "pi_temp_high": PI_TEMP_HIGH,
        "disk_full_pct": DISK_FULL_PCT, "offline_after_s": OFFLINE_AFTER,
        "flush_s": FLUSH_S, "keep_days": KEEP_DAYS, "fill_s": FILL_S,
        "notify": notify_state["channels"],
        "adafruit": {"enabled": bool(AIO_USER and AIO_KEY), "user": AIO_USER if g.role == "admin" else None,
                     "group": AIO_GROUP},
        "timezone": time.strftime("%Z (UTC%z)"),
    })


EMAIL_RE = re.compile(r"^[^@\s,;]{1,64}@[^@\s,;]{1,190}\.[a-z]{2,}$", re.I)


@app.post("/api/report/send")
def api_report_send():
    body = request.get_json(silent=True) or {}
    to = [a.strip() for a in str(body.get("to", "")).replace(";", ",").split(",") if a.strip()]
    bad = [a for a in to if not EMAIL_RE.match(a)]
    if bad:
        return jsonify(error=f"Not a valid email address: {bad[0]}"), 400
    if not (SMTP_USER and SMTP_PASS):
        return jsonify(error="Email sending is not set up. Run setup-email.sh on the Pi."), 400
    if not to and not REPORT_TO:
        return jsonify(error="Enter an email address (no default recipients are set)"), 400
    hours = min(max(float(body.get("hours", 24) or 24), 1), 24 * 31)
    if not queue_email("report", hours=hours, to=to or None):
        return jsonify(error="Too many emails waiting - try again shortly"), 503
    return jsonify(ok=True, to=to or REPORT_TO)


@app.post("/api/alerts/test")
def api_alert_test():
    if not notify_state["channels"]:
        return jsonify(error="no notification channel set up (alerts.env)"), 400
    try:
        _send("IoT Hub: test notification", f"Alerts are working. Sent {fmt_time(time.time())}.", "info")
    except Exception as e:
        return jsonify(error=f"sending failed: {e}"), 502
    return jsonify(ok=True)


def valid_target(nid):
    return nid == "all" or (ID_RE.match(nid) and nid != PI_ID)


ALLOWED_CMDS = re.compile(r"^(reboot|info|identify|brightness \d{1,3}|interval \d{1,4})$")


@app.post("/api/nodes/<nid>/cmd")
def api_cmd(nid):
    if not valid_target(nid):
        return jsonify(error="bad node id"), 400
    cmd = str((request.get_json(silent=True) or {}).get("cmd", "")).strip()
    if not ALLOWED_CMDS.match(cmd):
        return jsonify(error="command not allowed from the dashboard"), 400
    ok = publish(f"home/{nid}/cmd", cmd)
    return (jsonify(ok=True), 200) if ok else (jsonify(error="MQTT not connected"), 503)


@app.post("/api/nodes/<nid>/forget")
def api_forget(nid):
    if not ID_RE.match(nid) or nid == PI_ID:
        return jsonify(error="bad node id"), 400
    for t in ("status", "info", "led/state"):
        if mq is not None:
            mq.publish(f"home/{nid}/{t}", "", retain=True)
    with state_lock:
        nodes.pop(nid, None)
    return jsonify(ok=True)


workers, workers_restarts, workers_errors = {}, {}, {}


def spawn(fn):
    name = fn.__name__

    def runner():
        while True:
            try:
                fn()
                return
            except Exception as e:
                workers_restarts[name] = workers_restarts.get(name, 0) + 1
                workers_errors[name] = f"{type(e).__name__}: {e}"[:200]
                log.exception("background job %s crashed - restarting in 10 s", name)
                time.sleep(10)

    t = threading.Thread(target=runner, name=name, daemon=True)
    workers[name] = t
    t.start()


def main():
    global mq
    load_last_readings()
    rebuild_daily()
    signal.signal(signal.SIGTERM, on_shutdown)
    signal.signal(signal.SIGINT, on_shutdown)
    load_active_alerts()
    mq = make_mqtt()
    if _ntp_synced() or PROC_UP_AT_START > 600:
        clock_ready(verified=True)
    for fn in (pi_loop, adafruit_loop, flush_loop, cleanup_loop, time_loop, notify_loop, alert_loop, email_loop,
               report_loop, outage_loop):
        spawn(fn)
    log.info("Dashboard on http://0.0.0.0:%d  (Adafruit IO %s, alerts via %s, disk writes every %d s)", PORT,
             "on" if AIO_USER and AIO_KEY else "off", ", ".join(notify_state["channels"]) or "nothing", FLUSH_S)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=6)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
