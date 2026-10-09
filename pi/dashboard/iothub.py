#!/usr/bin/env python3
import base64
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
import hashlib
import hmac
import secrets
from html import escape as html_escape
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import re
import random
import math
import shlex
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

import paho.mqtt.client as mqtt
from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory, session

HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".config", "iothub")
DATA_DIR = os.path.join(HOME, "iothub-data")
DB_PATH = os.path.join(DATA_DIR, "readings.db")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

HUB_VERSION = "2.16"
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
       **load_env("alerts.env"), **load_env("storage.env"), **load_env("adafruit.env"), **load_env("email.env"),
       **load_env("google.env"), **load_env("speaker.env"), **load_env("weather.env")}
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
    "ota": "firmware", "backlog": "data", "cloud": "integration", "google": "integration", "node_boot": "power", "data_lost": "data",
}
ACTIVE_PATH = os.path.join(DATA_DIR, "active_alerts.json")
LEVEL_TAG = {"crit": "rotating_light", "warn": "warning", "ok": "white_check_mark", "info": "information_source"}


def _ascii(t):
    return t.encode("ascii", "replace").decode()


def _send(title, body, level, actions=None):
    if NTFY_TOPIC:
        req = urllib.request.Request(f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}", data=body.encode(),
                                     headers={"Title": _ascii(title), "Priority": LEVEL_PRIO.get(level, "default"),
                                              "Tags": LEVEL_TAG.get(level, "")})
        if actions:
            req.add_header("Actions", _ascii(actions))
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


def notify(title, body, level="warn", category="", actions=None, push=None):
    try:
        webpush(title, body, level, category, **(push or {}))
    except Exception as e:
        log.warning("web push not queued: %s", e)
    if not notify_state["channels"]:
        return
    head = f"[{LEVEL_NAME.get(level, level.upper())}] {title}"
    tail = f"\n\n{category.capitalize()} - {fmt_time(time.time())}" if category else ""
    try:
        notify_q.put_nowait((head, body + tail, level, actions))
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
        notify(title or f"{nid} {kind}", msg, level, category,
               push={"tag": f"{nid}-{kind}", "url": "/#alerts" if level != "ok" else "/"})


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
    summ_lines = []
    if intro is None and hours >= 20:
        try:
            summ = build_summary(day_of(end - 60))
            summ_lines = [(sec["title"], ln) for sec in summ["sections"] for ln in sec["lines"]]
        except Exception as e:
            log.warning("summary for report failed: %s", e)
    period = f"{fmt_time(start)} to {fmt_time(end)}"
    title = title or f"{SITE_NAME} report"
    esc = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    f1 = lambda v: "-" if v is None else f"{v:.1f}"

    summ_html = ""
    if summ_lines:
        items = "".join("<li style='margin:3px 0" + (";color:#c0362c" if t == "Problems" and not ln.startswith("No problems") else ";color:#b26a00" if t == "Worth a look" else "")
                        + "'>" + esc(ln) + "</li>" for t, ln in summ_lines)
        summ_html = ("<h3 style='font-size:15px;margin:0 0 6px'>Summary</h3>"
                     "<ul style='margin:0 0 16px;padding-left:18px;font-size:14px'>" + items + "</ul>")
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
{summ_html}
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
                     + ([ln for _, ln in summ_lines] + [""] if summ_lines else [])
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
                  f"{node_name(nid)} restarted: {why}" + (f" (at {fmt_time(boot_at)})" if time.time() - boot_at > 300 else ""))


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
    if request.path.startswith("/google/"):
        return None
    if request.path == "/api/push/unsubscribe":
        return None
    m = re.fullmatch(r"/api/rain/(\d+)/answer", request.path)
    if m and _same(str(request.args.get("sig", "")), _rain_sig(int(m.group(1)))):
        return None
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
             "apple-touch-icon.png": "image/png", "qrcode.js": "text/javascript", "badge-72.png": "image/png"}


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
                "id": n["id"], "is_pi": n["id"] == PI_ID, "label": node_name(n["id"]), "named": n["id"] in profiles(),
                "place": "hub" if n["id"] == PI_ID else ("outdoor" if is_outdoor(n["id"]) else "indoor"),
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
        "push": {"enabled": WEBPUSH_OK, "devices": _push_devices(), "last_ok": push_state["last_ok"],
                 "last_error": push_state["last_error"]},
        "email": {**email_state, "daily_at": REPORT_TIME or None, "alerts": REPORT_ALERTS,
                  "to": REPORT_TO if g.role == "admin" else [_mask(a) for a in REPORT_TO]},
        "storage": {**storage_state, "pending": len(pending), "flush_s": FLUSH_S, "keep_days": KEEP_DAYS},
        "speaker": {"name": SPEAKER_NAME or None, "daily_at": SUMMARY_TIME or None, **speak_state},
        "google": {"enabled": GH_ENABLED, "linked": GH_ENABLED and _gh_linked(), **gh_state, "live": GH_REPORT,
                   "live_error": gh_sa["error"], "last_report": gh_sa["last_report"]},
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


GH_CLIENT_ID = cfg.get("GH_CLIENT_ID", "")
GH_CLIENT_SECRET = cfg.get("GH_CLIENT_SECRET", "")
GH_PROJECT_ID = cfg.get("GH_PROJECT_ID", "")
GH_INCLUDE_PI = cfg.get("GH_INCLUDE_PI", "yes").lower() in ("1", "yes", "true")
GH_ENABLED = bool(GH_CLIENT_ID and GH_CLIENT_SECRET)
GH_LED = cfg.get("GH_LED", "yes").lower() in ("1", "yes", "true")
GH_STATS = cfg.get("GH_STATS", "yes").lower() in ("1", "yes", "true")
GH_SA_PATH = cfg.get("GH_SERVICE_ACCOUNT", os.path.join(CONF_DIR, "google-service-account.json"))
GH_REPORT = GH_ENABLED and os.path.exists(GH_SA_PATH)
GH_AGENT = "iot-pi32-owner"
GH_REDIRECT = re.compile(r"^https://oauth-redirect(-sandbox)?\.googleusercontent\.com/r/([A-Za-z0-9_-]+)$")
GH_ACCESS_S = 3600
gh_codes = {}
gh_state = {"last_request": None, "last_intent": None, "linked_at": None}
with db_lock:
    db.execute("CREATE TABLE IF NOT EXISTS gh_tokens (hash TEXT PRIMARY KEY, kind TEXT, expires REAL, created REAL)")
    db.commit()


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _gh_issue(kind, ttl=None):
    tok = secrets.token_urlsafe(32)
    with db_lock:
        db.execute("INSERT INTO gh_tokens (hash, kind, expires, created) VALUES (?,?,?,?)",
                   (_h(tok), kind, time.time() + ttl if ttl else None, time.time()))
        db.execute("DELETE FROM gh_tokens WHERE expires IS NOT NULL AND expires < ?", (time.time(),))
        db.commit()
    return tok


def _gh_valid(tok, kind):
    if not tok:
        return False
    with db_lock:
        row = db.execute("SELECT expires FROM gh_tokens WHERE hash = ? AND kind = ?", (_h(tok), kind)).fetchone()
    return bool(row) and (row[0] is None or row[0] > time.time())


def _gh_linked():
    with db_lock:
        return db.execute("SELECT COUNT(*) FROM gh_tokens WHERE kind = 'refresh'").fetchone()[0] > 0


def _gh_client_ok():
    cid, sec = request.form.get("client_id", ""), request.form.get("client_secret", "")
    if request.authorization and request.authorization.username:
        cid, sec = request.authorization.username, request.authorization.password or ""
    return _same(cid, GH_CLIENT_ID) and _same(sec, GH_CLIENT_SECRET)


def _gh_redirect_ok(uri):
    m = GH_REDIRECT.match(uri or "")
    return bool(m) and (not GH_PROJECT_ID or m.group(2) == GH_PROJECT_ID)


GH_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link to Google Home</title><style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#eef1f4;color:#18232e;margin:0;display:grid;place-items:center;min-height:100vh}}
main{{background:#fff;border:1px solid #dde3e9;border-radius:12px;padding:26px;width:min(380px,90vw)}}
h1{{font-size:19px;margin:0 0 6px}} p{{color:#4c5a68;font-size:14px;margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #c9d1d9;border-radius:8px;font-size:15px;margin-bottom:12px}}
button{{width:100%;padding:10px;border:0;border-radius:8px;background:#0d7480;color:#fff;font-size:15px;cursor:pointer}}
.err{{color:#c0362c;font-size:14px;margin-bottom:10px}}
@media (prefers-color-scheme:dark){{body{{background:#0f1720;color:#e6edf3}}main{{background:#16202b;border-color:#2a3644}}p{{color:#9aa7b4}}input{{background:#0f1720;color:#e6edf3;border-color:#2a3644}}}}
</style></head><body><main>
<h1>Link {site} to Google Home</h1>
<p>Google Home will be able to read the temperature and humidity of your nodes. It can't change anything.</p>
{err}<form method="post" action="/google/authorize">
<input type="hidden" name="client_id" value="{client_id}"><input type="hidden" name="redirect_uri" value="{redirect_uri}">
<input type="hidden" name="state" value="{state}">
<input type="password" name="password" placeholder="Hub admin password" autocomplete="current-password" required autofocus>
<button type="submit">Allow</button></form></main></body></html>"""


def _gh_page(err=""):
    e = lambda v: html_escape(str(v or ""), quote=True)
    src = request.form if request.method == "POST" else request.args
    return GH_PAGE.format(site=e(SITE_NAME), err=f'<div class="err">{e(err)}</div>' if err else "",
                          client_id=e(src.get("client_id")), redirect_uri=e(src.get("redirect_uri")),
                          state=e(src.get("state")))


@app.route("/google/authorize", methods=["GET", "POST"])
def google_authorize():
    src = request.form if request.method == "POST" else request.args
    if not GH_ENABLED:
        return "Google Home is not set up on this hub (run setup-google.sh).", 404
    if not _same(src.get("client_id"), GH_CLIENT_ID) or not _gh_redirect_ok(src.get("redirect_uri")):
        return "Unknown client or redirect address.", 400
    if request.method == "GET":
        if request.args.get("response_type", "code") != "code":
            return "Unsupported response type.", 400
        return _gh_page()
    now = time.time()
    if len([t for t in login_failures if now - t < 600]) >= 10:
        return _gh_page("Too many wrong passwords. Try again in 10 minutes."), 429
    if not DASH_PASSWORD or not _same(src.get("password"), DASH_PASSWORD):
        login_failures.append(now)
        time.sleep(1)
        return _gh_page("Wrong password" if DASH_PASSWORD else "Set an admin password first (iothub password)."), 401
    code = secrets.token_urlsafe(24)
    gh_codes[code] = (src.get("redirect_uri"), now + 600)
    for c in [c for c, (_, exp) in gh_codes.items() if exp < now]:
        gh_codes.pop(c, None)
    sep = "&" if "?" in src.get("redirect_uri") else "?"
    return redirect(src.get("redirect_uri") + sep + urllib.parse.urlencode({"code": code, "state": src.get("state", "")}))


@app.post("/google/token")
def google_token():
    if not GH_ENABLED or not _gh_client_ok():
        return jsonify(error="invalid_client"), 401
    grant = request.form.get("grant_type")
    if grant == "authorization_code":
        entry = gh_codes.pop(request.form.get("code", ""), None)
        if not entry or entry[1] < time.time() or entry[0] != request.form.get("redirect_uri"):
            return jsonify(error="invalid_grant"), 400
        gh_state["linked_at"] = time.time()
        log_event(PI_ID, "google", "info", "Linked to Google Home")
        return jsonify(token_type="Bearer", access_token=_gh_issue("access", GH_ACCESS_S),
                       refresh_token=_gh_issue("refresh"), expires_in=GH_ACCESS_S)
    if grant == "refresh_token":
        if not _gh_valid(request.form.get("refresh_token"), "refresh"):
            return jsonify(error="invalid_grant"), 400
        return jsonify(token_type="Bearer", access_token=_gh_issue("access", GH_ACCESS_S), expires_in=GH_ACCESS_S)
    return jsonify(error="unsupported_grant_type"), 400


GH_SCENES = {"scene--summary": "Day summary", "scene--report": "Send hub report", "scene--findall": "Find all nodes"}
gh_led_last = {}
gh_sa = {"token": None, "exp": 0, "last_sync_sig": None, "last_states": {}, "error": None, "last_report": None}


def _gh_led(n):
    m = re.search(r"brightness=(\d+)", str(n.get("led") or ""))
    return int(m.group(1)) if m else 40


PROFILE_PATH = os.path.join(CONF_DIR, "nodes.env")
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '&.-]{0,23}$")
_prof = {"mtime": None, "map": {}}


def profiles():
    try:
        mt = os.stat(PROFILE_PATH).st_mtime
    except OSError:
        mt = None
    if mt != _prof["mtime"]:
        out = {}
        try:
            with open(PROFILE_PATH) as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    name, _, place = v.strip().strip('"').partition("|")
                    if ID_RE.match(k) and PROFILE_NAME.match(name.strip()):
                        out[k] = {"name": name.strip(), "place": "outdoor" if place.strip() == "outdoor" else "indoor"}
        except OSError:
            pass
        _prof.update(mtime=mt, map=out)
    return _prof["map"]


def save_profile(nid, name, place):
    cur = dict(profiles())
    if name:
        cur[nid] = {"name": name, "place": place}
    else:
        cur.pop(nid, None)
    tmp = PROFILE_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write("".join(f"{k}={v['name']}|{v['place']}\n" for k, v in sorted(cur.items())))
    os.replace(tmp, PROFILE_PATH)
    _prof["mtime"] = None


def is_outdoor(nid):
    return profiles().get(nid, {}).get("place") == "outdoor"


def node_name(nid):
    if nid == PI_ID:
        return "Hub"
    p = profiles().get(nid)
    if p:
        return p["name"]
    s = _id_words(nid)
    return s[:1].upper() + s[1:]


def _cap(s):
    return s[:1].upper() + s[1:]


def _gh_spoken(nid):
    p = profiles().get(nid)
    return p["name"] if p else _id_words(nid)


def _id_words(nid):
    s = re.sub(r"[-_]+", " ", nid)
    s = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _gh_today(nid):
    with db_lock:
        rows = db.execute("SELECT metric, min, max FROM daily WHERE day = ? AND node = ?",
                          (day_of(time.time()), nid)).fetchall()
    out = {m: (lo, hi) for m, lo, hi in rows}
    for ts, n, v, m in pending_between(day_bounds(day_of(time.time()))[0], time.time() + 60):
        if n == nid:
            lo, hi = out.get(m, (v, v))
            out[m] = (min(lo, v), max(hi, v))
    return out


def _gh_temp_attrs(lo=-40, hi=125):
    return {"temperatureRange": {"minThresholdCelsius": lo, "maxThresholdCelsius": hi},
            "temperatureUnitForUX": "C", "queryOnlyTemperatureControl": True}


def _gh_devices():
    with state_lock:
        snap = [(k, dict(v.get("info") or {}), v.get("hum")) for k, v in nodes.items()]
    devs = []
    for nid, info, hum in sorted(snap):
        if nid == PI_ID:
            if GH_INCLUDE_PI:
                devs.append({"id": PI_ID, "type": "action.devices.types.SENSOR",
                             "traits": ["action.devices.traits.TemperatureControl"],
                             "name": {"name": "hub", "defaultNames": [f"{SITE_NAME} hub CPU"], "nicknames": ["hub CPU", "raspberry pi"]},
                             "willReportState": GH_REPORT, "attributes": _gh_temp_attrs(0, 100),
                             "deviceInfo": {"manufacturer": "IoT_Pi32", "model": "Raspberry Pi 4", "swVersion": HUB_VERSION}})
            continue
        spoken = _gh_spoken(nid)
        has_hum = hum is not None or info.get("sensor") in ("am2305b", "dht22")
        traits = ["action.devices.traits.TemperatureControl"]
        attrs = _gh_temp_attrs()
        if has_hum:
            traits.append("action.devices.traits.HumiditySetting")
            attrs["queryOnlyHumiditySetting"] = True
        dinfo = {"manufacturer": "IoT_Pi32", "model": f"ESP32-S3 node ({str(info.get('sensor') or 'sensor').upper()})",
                 "swVersion": str(info.get("fw", ""))}
        nick = {nid, _id_words(nid), f"{spoken} sensor", f"{spoken} temperature"}
        if is_outdoor(nid):
            nick |= {"outside sensor", "outdoor sensor", f"{spoken.lower()} outside"}
        nick = sorted(nick - {spoken})
        devs.append({"id": nid, "type": "action.devices.types.SENSOR", "traits": traits,
                     "name": {"name": spoken, "defaultNames": [f"{SITE_NAME} {nid}"], "nicknames": nick},
                     "willReportState": GH_REPORT, "attributes": attrs, "deviceInfo": dinfo})
        if GH_LED:
            devs.append({"id": f"{nid}--led", "type": "action.devices.types.LIGHT",
                         "traits": ["action.devices.traits.OnOff", "action.devices.traits.Brightness"],
                         "name": {"name": f"{spoken} light", "defaultNames": [f"{SITE_NAME} {nid} status LED"],
                                  "nicknames": [f"{spoken} led", f"{nid} light"]},
                         "willReportState": GH_REPORT, "attributes": {}, "deviceInfo": dinfo})
        for verb in ("find", "restart"):
            devs.append({"id": f"scene--{verb}--{nid}", "type": "action.devices.types.SCENE",
                         "traits": ["action.devices.traits.Scene"], "name": {"name": f"{verb.capitalize()} {spoken}"},
                         "willReportState": False, "attributes": {"sceneReversible": False}})
        if GH_STATS:
            for kind, word in (("high", "today's highest"), ("low", "today's lowest")):
                st_traits = ["action.devices.traits.TemperatureControl"]
                st_attrs = _gh_temp_attrs()
                if has_hum:
                    st_traits.append("action.devices.traits.HumiditySetting")
                    st_attrs["queryOnlyHumiditySetting"] = True
                devs.append({"id": f"{nid}--{kind}", "type": "action.devices.types.SENSOR", "traits": st_traits,
                             "name": {"name": f"{spoken} {kind}", "defaultNames": [f"{spoken} {word}"],
                                      "nicknames": [f"{spoken} {word}", f"{spoken} today {kind}"]},
                             "willReportState": GH_REPORT, "attributes": st_attrs, "deviceInfo": dinfo})
    for sid, name in GH_SCENES.items():
        if sid == "scene--report" and not (SMTP_USER and SMTP_PASS and REPORT_TO):
            continue
        if sid == "scene--summary" and not (SPEAKER_NAME or (SUMMARY_PUSH and (notify_state["channels"] or _push_devices()))):
            continue
        devs.append({"id": sid, "type": "action.devices.types.SCENE", "traits": ["action.devices.traits.Scene"],
                     "name": {"name": name}, "willReportState": False, "attributes": {"sceneReversible": False}})
    return devs


def _gh_node(nid):
    now = time.time()
    with state_lock:
        n = nodes.get(nid)
        if not n:
            return None, False, False
        n = dict(n)
    interval = (n.get("info") or {}).get("interval_s") or 10
    seen = max(n.get("seen") or 0, n.get("temp_ts") or 0)
    reachable = nid == PI_ID or (n.get("status") == "online" and now - seen < max(3 * interval, 120))
    sensor_ok = bool(n.get("temp_ts")) and now - n["temp_ts"] < max(3 * interval, 120) \
        and (n.get("info") or {}).get("sensor") != "missing"
    return n, reachable, sensor_ok


def _gh_split(did):
    for suffix in ("--high", "--low", "--led"):
        if did.endswith(suffix):
            return did[:-len(suffix)], suffix[2:]
    return did, None


def _gh_state(did):
    if did.startswith("scene--"):
        return {"online": True, "status": "SUCCESS"}
    nid, stat = _gh_split(did)
    n, reachable, sensor_ok = _gh_node(nid)
    if n is None:
        return {"online": False, "status": "ERROR", "errorCode": "deviceNotFound"}
    if stat == "led":
        if not reachable:
            return {"online": False, "status": "OFFLINE", "errorCode": "deviceOffline"}
        b = _gh_led(n)
        return {"online": True, "status": "SUCCESS", "on": b > 0, "brightness": round(b * 100 / 255)}
    if stat:
        today = _gh_today(nid)
        if "temp" not in today:
            return {"online": True, "status": "ERROR", "errorCode": "deviceNotReady"}
        pick = 0 if stat == "low" else 1
        st = {"online": True, "status": "SUCCESS", "temperatureAmbientCelsius": round(float(today["temp"][pick]), 1)}
        if "hum" in today:
            st["humidityAmbientPercent"] = int(min(100, max(1, round(float(today["hum"][pick])))))
        return st
    if not reachable:
        return {"online": False, "status": "OFFLINE", "errorCode": "deviceOffline"}
    led = {}
    if not sensor_ok or n.get("temp") is None:
        return {"online": True, "status": "ERROR", "errorCode": "deviceNeedsRepair", **led}
    st = {"online": True, "status": "SUCCESS", "temperatureAmbientCelsius": round(float(n["temp"]), 1), **led}
    if n.get("hum") is not None and nid != PI_ID:
        st["humidityAmbientPercent"] = int(min(100, max(1, round(float(n["hum"])))))
    return st


def _gh_set_led(nid, value):
    if value > 0:
        gh_led_last[nid] = value
    if not publish(f"home/{nid}/cmd", f"brightness {value}"):
        return False
    with state_lock:
        if nid in nodes:
            nodes[nid]["led"] = f"brightness={value} mode=status"
    return True


def _gh_execute(did, ex):
    cmd, params = ex.get("command", ""), ex.get("params") or {}
    short = cmd.rsplit(".", 1)[-1]
    if did in GH_SCENES and short == "ActivateScene":
        if did == "scene--summary":
            summ = build_summary()
            if not queue_speak(summ["text"], push_title=f"{SITE_NAME}: today's summary"):
                return {"status": "ERROR", "errorCode": "deviceBusy"}
            log_event(PI_ID, "google", "info", "Google Home: day summary"
                      + (f" spoken on {SPEAKER_NAME}" if SPEAKER_NAME else " sent to the phone"))
            return {"status": "SUCCESS", "states": {"online": True}}
        if did == "scene--report":
            if not queue_email("report", hours=24):
                return {"status": "ERROR", "errorCode": "actionNotAvailable"}
            log_event(PI_ID, "google", "info", "Google Home: report email requested")
        else:
            if not publish("home/all/cmd", "identify"):
                return {"status": "ERROR", "errorCode": "deviceOffline"}
            log_event(PI_ID, "google", "info", "Google Home: find all nodes")
        return {"status": "SUCCESS", "states": {"online": True}}
    if did.startswith(("scene--find--", "scene--restart--")) and short == "ActivateScene":
        verb, nid = did.split("--")[1], did.split("--", 2)[2]
        did, short = (nid, "Locate") if verb == "find" else (nid, "Reboot")
        params = {}
    nid, stat = _gh_split(did)
    n, reachable, _ = _gh_node(nid)
    if n is None:
        return {"status": "ERROR", "errorCode": "deviceNotFound"}
    if stat in ("high", "low") or nid == PI_ID:
        return {"status": "ERROR", "errorCode": "functionNotSupported"}
    if not reachable:
        return {"status": "ERROR", "errorCode": "deviceOffline"}
    if stat == "led" and short in ("OnOff", "BrightnessAbsolute"):
        if short == "OnOff":
            value = (gh_led_last.get(nid) or _gh_led(n) or 40) if params.get("on") else 0
        else:
            pct = params.get("brightness")
            if not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
                return {"status": "ERROR", "errorCode": "valueOutOfRange"}
            value = round(pct * 255 / 100)
        if _gh_led(n) > 0 and value == 0:
            gh_led_last[nid] = _gh_led(n)
        if not _gh_set_led(nid, value):
            return {"status": "ERROR", "errorCode": "deviceOffline"}
        log_event(nid, "google", "info", f"Google Home: {nid} LED {'off' if value == 0 else f'{round(value * 100 / 255)}%'}")
        return {"status": "SUCCESS", "states": {"online": True, "on": value > 0, "brightness": round(value * 100 / 255)}}
    if short == "Locate":
        if params.get("silence"):
            return {"status": "SUCCESS", "states": {"online": True}}
        if not publish(f"home/{nid}/cmd", "identify"):
            return {"status": "ERROR", "errorCode": "deviceOffline"}
        log_event(nid, "google", "info", f"Google Home: find {nid} (rainbow LED for 10 s)")
        return {"status": "SUCCESS", "states": {"online": True}}
    if short == "Reboot":
        if not publish(f"home/{nid}/cmd", "reboot"):
            return {"status": "ERROR", "errorCode": "deviceOffline"}
        log_event(nid, "google", "info", f"Google Home: restart {nid}")
        return {"status": "SUCCESS", "states": {"online": True}}
    return {"status": "ERROR", "errorCode": "functionNotSupported"}


@app.post("/google/fulfillment")
def google_fulfillment():
    if not GH_ENABLED:
        return jsonify(error="not set up"), 404
    auth_h = request.headers.get("Authorization", "")
    if not auth_h.startswith("Bearer ") or not _gh_valid(auth_h[7:].strip(), "access"):
        return jsonify(error="invalid_token"), 401
    body = request.get_json(silent=True) or {}
    rid = body.get("requestId", "")
    gh_state["last_request"] = time.time()
    for inp in body.get("inputs", []):
        intent = inp.get("intent", "")
        gh_state["last_intent"] = intent.rsplit(".", 1)[-1]
        if intent == "action.devices.SYNC":
            return jsonify(requestId=rid, payload={"agentUserId": GH_AGENT, "devices": _gh_devices()})
        if intent == "action.devices.QUERY":
            ids = [d.get("id") for d in inp.get("payload", {}).get("devices", [])]
            return jsonify(requestId=rid, payload={"devices": {i: _gh_state(i) for i in ids if i}})
        if intent == "action.devices.EXECUTE":
            results = []
            for c in inp.get("payload", {}).get("commands", []):
                for d in c.get("devices", []):
                    res = {"status": "ERROR", "errorCode": "functionNotSupported"}
                    for ex in c.get("execution", []):
                        res = _gh_execute(d.get("id", ""), ex)
                    results.append({"ids": [d.get("id", "")], **res})
            return jsonify(requestId=rid, payload={"commands": results})
        if intent == "action.devices.DISCONNECT":
            with db_lock:
                db.execute("DELETE FROM gh_tokens")
                db.commit()
            gh_sa.update(last_sync_sig=None, last_states={})
            log_event(PI_ID, "google", "info", "Unlinked from Google Home")
            return jsonify({})
    return jsonify(requestId=rid, payload={"errorCode": "notSupported"})


def _gh_b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _gh_sa_token():
    now = time.time()
    if gh_sa["token"] and gh_sa["exp"] > now + 120:
        return gh_sa["token"]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with open(GH_SA_PATH) as f:
        sa = json.load(f)
    head = {"alg": "RS256", "typ": "JWT", "kid": sa.get("private_key_id", "")}
    claims = {"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/homegraph",
              "aud": sa.get("token_uri", "https://oauth2.googleapis.com/token"), "iat": int(now), "exp": int(now) + 3600}
    msg = _gh_b64(json.dumps(head, separators=(",", ":")).encode()) + b"." + \
        _gh_b64(json.dumps(claims, separators=(",", ":")).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), None)
    jwt = msg + b"." + _gh_b64(key.sign(msg, padding.PKCS1v15(), hashes.SHA256()))
    data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": jwt.decode()}).encode()
    with urllib.request.urlopen(claims["aud"], data=data, timeout=15) as r:
        tok = json.load(r)
    gh_sa.update(token=tok["access_token"], exp=now + int(tok.get("expires_in", 3600)))
    return gh_sa["token"]


def _gh_homegraph(path, body):
    req = urllib.request.Request(f"https://homegraph.googleapis.com/v1/{path}", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {_gh_sa_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def gh_tick():
    if not (GH_REPORT and _gh_linked()):
        return
    try:
        devs = _gh_devices()
        sig = json.dumps(sorted((d["id"], d["traits"], d["name"].get("name")) for d in devs))
        if sig != gh_sa["last_sync_sig"]:
            _gh_homegraph("devices:requestSync", {"agentUserId": GH_AGENT, "async": True})
            gh_sa["last_sync_sig"] = sig
        states = {}
        for d in devs:
            if d["type"] == "action.devices.types.SCENE":
                continue
            st = {k: v for k, v in _gh_state(d["id"]).items() if k not in ("status", "errorCode")}
            if gh_sa["last_states"].get(d["id"]) != st:
                states[d["id"]] = st
        if states:
            _gh_homegraph("devices:reportStateAndNotification",
                          {"requestId": secrets.token_hex(8), "agentUserId": GH_AGENT,
                           "payload": {"devices": {"states": states}}})
            gh_sa["last_states"].update(states)
            gh_sa["last_report"] = time.time()
        gh_sa["error"] = None
    except urllib.error.HTTPError as e:
        gh_sa["error"] = f"Home Graph HTTP {e.code}"
        if e.code == 401:
            gh_sa["token"] = None
    except Exception as e:
        gh_sa["error"] = f"{type(e).__name__}: {e}"[:160]


def gh_loop():
    while True:
        time.sleep(30)
        gh_tick()


SPEAKER_NAME = cfg.get("SPEAKER_NAME", "") if cfg.get("SPEAKER_ENABLED", "no").lower() in ("1", "yes", "true") else ""
SUMMARY_TIME_DEFAULT = "21:00"
SPEAK_LANG = cfg.get("SPEAK_LANG", "en")
SPEAK_TLD = cfg.get("SPEAK_TLD", "co.uk")
SPEAK_ENGINE = cfg.get("SPEAK_ENGINE", "piper")
PIPER_VOICE = cfg.get("PIPER_VOICE", "en_GB-jenny_dioco-medium")
PIPER_DIR = cfg.get("PIPER_DIR", os.path.join(HOME, ".local", "share", "piper"))
_piper = {}
SUMMARY_TIME = cfg.get("SUMMARY_TIME", SUMMARY_TIME_DEFAULT)
SUMMARY_PUSH = cfg.get("SUMMARY_PUSH", "yes").lower() in ("1", "yes", "true")
TTS_DIR = "/dev/shm/iothub-tts" if os.path.isdir("/dev/shm") else os.path.join(DATA_DIR, "tts")
speak_q = queue.Queue(maxsize=5)
speak_state = {"last_ok": None, "last_error": None, "speaker": SPEAKER_NAME or None}
UNITS = {"temp": ("degrees", 0.5), "hum": ("percent", 3.0)}


def _say_time(ts):
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0")


def _say_dur(sec):
    sec = int(max(0, sec))
    if sec < 90:
        return f"{sec} seconds"
    m = round(sec / 60)
    if m < 90:
        return f"{m} minute{'s' if m != 1 else ''}"
    h, m = divmod(m, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "")


def _human_time(ts, short=False):
    lt = time.localtime(ts)
    m = int(round(lt.tm_min / 15.0) * 15)
    h = lt.tm_hour + (1 if m == 60 else 0)
    m = 0 if m == 60 else m
    h12 = (h % 12) or 12
    clock = f"{h12}" + (f":{m:02d}" if m else "")
    if short:
        return f"at {clock} {'AM' if h % 24 < 12 else 'PM'}"
    hh = h % 24
    if hh == 12:
        return f"around {clock} PM"
    part = ("early in the morning" if 4 <= hh < 7 else "in the morning" if hh < 12 and hh >= 7 else
            "in the afternoon" if 12 <= hh < 17 else "in the evening" if 17 <= hh < 21 else "at night")
    if hh == 12 and m == 0:
        return "around midday"
    if hh == 0 and m == 0:
        return "around midnight"
    return f"around {clock} {part}"


def _say_num(v, metric):
    return f"{v:.1f}" if metric == "temp" else f"{v:.0f}"


def _series_stats(rows, metric):
    if not rows:
        return None
    hi = max(rows, key=lambda r: r[1])
    lo = min(rows, key=lambda r: r[1])
    avg = sum(v for _, v in rows) / len(rows)
    buckets = collections.OrderedDict()
    for ts, v in rows:
        b = int(ts // 300) * 300
        s = buckets.setdefault(b, [0.0, 0])
        s[0] += v
        s[1] += 1
    pts = [(b + 150, s[0] / s[1]) for b, s in buckets.items()]
    rise = fall = (0.0, None, None)
    for j in range(len(pts)):
        for i in range(j - 1, -1, -1):
            if pts[j][0] - pts[i][0] > 3600:
                break
            d = pts[j][1] - pts[i][1]
            if d > rise[0]:
                rise = (d, pts[i][0], pts[j][0])
            if -d > fall[0]:
                fall = (-d, pts[i][0], pts[j][0])
    first = [v for ts, v in rows if ts - rows[0][0] < 3600]
    last = [v for ts, v in rows if rows[-1][0] - ts < 3600]
    return {"hi": hi, "lo": lo, "avg": avg, "rise": rise, "fall": fall, "n": len(rows),
            "start": sum(first) / len(first), "end": sum(last) / len(last), "now": rows[-1]}


def _event_pairs(evs, start, end):
    out = []
    for i, (ts, nid, kind, level, msg) in enumerate(evs):
        if level not in ("crit", "warn") or not (start <= ts < end):
            continue
        ended = next((t2 for t2, n2, k2, l2, _ in evs[i + 1:] if n2 == nid and k2 == kind and l2 == "ok"), None)
        out.append({"ts": ts, "node": nid, "kind": kind, "level": level, "msg": msg,
                    "dur": (ended - ts) if ended else None})
    return out


def _problem_sentence(p):
    s = _problem_text(p)
    return s[:1].upper() + s[1:]


def _problem_text(p):
    who = "the hub" if p["node"] == PI_ID else node_name(p["node"])
    t = _say_time(p["ts"])
    lasted = f" for {_say_dur(p['dur'])}" if p["dur"] is not None else ", and it hasn't recovered yet"
    k = p["kind"]
    if k == "offline":
        return f"{who} went offline at {t}{lasted}."
    if k == "sensor":
        return f"{who}'s sensor stopped reading at {t}{lasted}."
    if k == "pi_power":
        return f"The Pi's power supply dropped too low at {t}{lasted}. A better power supply would fix this."
    if k == "pi_hot":
        return f"The Pi overheated at {t}{lasted}."
    if k == "pi_disk":
        return f"The SD card got nearly full at {t}."
    if k == "mqtt":
        return f"The message broker stopped at {t}{lasted}."
    if k in ("temp_high", "temp_low", "hum_high", "hum_low"):
        what = "temperature" if k.startswith("temp") else "humidity"
        way = "above" if k.endswith("high") else "below"
        return f"{who}'s {what} went {way} the limit at {t}{lasted}."
    if k == "storage":
        return f"The database needed repair at {t}."
    msg = re.sub(r"\s*\(.*?\)\s*", " ", str(p["msg"])).strip()
    msg = re.sub(r"\s+at \d{1,2} \w{3}( \d{4})? \d{1,2}:\d{2}(:\d{2})?", "", msg)
    return f"At {t}, {msg[0].lower() + msg[1:] if msg else 'there was a problem'}."


ANOM = {"temp": {"margin": 1.5, "spike": 1.5, "unit": "°C", "word": "degrees"},
        "hum": {"margin": 8.0, "spike": 8.0, "unit": "%", "word": "percent"}}


def _buckets(rows, size=300):
    acc = collections.OrderedDict()
    for ts, v in rows:
        b = int(ts // size) * size
        a = acc.setdefault(b, [0.0, 0])
        a[0] += v
        a[1] += 1
    return [(b + size / 2, a[0] / a[1]) for b, a in acc.items()]


def _baseline(nid, day):
    start = day_bounds(day)[0]
    days = [day_of(start - 86400 * k) for k in range(1, 8)]
    with db_lock:
        rows = db.execute(f"SELECT metric, min, max FROM daily WHERE node = ? AND day IN ({','.join('?' * len(days))})",
                          (nid, *days)).fetchall()
    out = {}
    for metric in ("temp", "hum"):
        lo = [r[1] for r in rows if r[0] == metric]
        hi = [r[2] for r in rows if r[0] == metric]
        if len(hi) >= 3:
            mh, ml = sum(hi) / len(hi), sum(lo) / len(lo)
            sh = (sum((x - mh) ** 2 for x in hi) / len(hi)) ** 0.5
            sl = (sum((x - ml) ** 2 for x in lo) / len(lo)) ** 0.5
            out[metric] = (mh, sh, ml, sl)
    return out


def _find_anomalies(nid, series, base, is_today):
    found = []
    who = node_name(nid)
    for metric in ("temp", "hum"):
        rows = series.get((nid, metric), [])
        if len(rows) < 12:
            continue
        cfg_ = ANOM[metric]
        what = "temperature" if metric == "temp" else "humidity"
        hi, lo = max(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[1])
        if metric in base:
            mh, sh, ml, sl = base[metric]
            if hi[1] > mh + max(2 * sh, cfg_["margin"]):
                found.append({"node": nid, "ts": hi[0], "level": "anomaly", "metric": metric,
                              "text": f"{who}'s {what} was unusually high: {_say_num(hi[1], metric)} at {_say_time(hi[0])}, "
                                      f"against a usual high of about {_say_num(mh, metric)} {cfg_['word']}."})
            if lo[1] < ml - max(2 * sl, cfg_["margin"]):
                found.append({"node": nid, "ts": lo[0], "level": "anomaly", "metric": metric,
                              "text": f"{who}'s {what} was unusually low: {_say_num(lo[1], metric)} at {_say_time(lo[0])}, "
                                      f"against a usual low of about {_say_num(ml, metric)} {cfg_['word']}."})
        pts = _buckets(rows)
        jump = None
        for (t1, v1), (t2, v2) in zip(pts, pts[1:]):
            if t2 - t1 <= 660 and abs(v2 - v1) >= cfg_["spike"] and (jump is None or abs(v2 - v1) > abs(jump[1])):
                jump = (t2, v2 - v1)
        if jump and is_outdoor(nid) and (metric == "hum" or 7 <= time.localtime(jump[0]).tm_hour < 19):
            jump = None
        if jump:
            found.append({"node": nid, "ts": jump[0], "level": "anomaly", "metric": metric,
                          "text": f"{who}'s {what} {'jumped' if jump[1] > 0 else 'dropped'} suddenly by "
                                  f"{_say_num(abs(jump[1]), metric)} {cfg_['word']} around {_say_time(jump[0])}."})
    t_rows, h_rows = series.get((nid, "temp"), []), series.get((nid, "hum"), [])
    if is_today and len(t_rows) > 60:
        run_start, last = t_rows[-1][0], t_rows[-1][1]
        for ts, v in reversed(t_rows):
            if v != last:
                break
            run_start = ts
        h_same = all(v == h_rows[-1][1] for ts, v in h_rows if ts >= run_start) if h_rows else True
        if t_rows[-1][0] - run_start >= 7200 and h_same:
            found.append({"node": nid, "ts": run_start, "level": "anomaly", "metric": "temp",
                          "text": f"{who}'s readings haven't changed since {_say_time(run_start)}. The sensor may be stuck."})
    for ts, v in h_rows[-1:]:
        if v >= 99.5 or v <= 0.5:
            found.append({"node": nid, "ts": ts, "level": "anomaly", "metric": "hum",
                          "text": f"{who}'s humidity is reading {v:.0f} percent, which usually means a sensor problem."})
    return found


def build_summary(day=None):
    now = time.time()
    day = day or day_of(now)
    start, end = day_bounds(day)
    end_eff = min(end, now)
    is_today = day == day_of(now)
    label = "today" if is_today else ("yesterday" if day == day_of(now - 86400) else
                                       time.strftime("%A %d %B", time.localtime(start)))
    with db_lock:
        rows = db.execute("SELECT ts, node, value, metric FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                          (start, end)).fetchall()
        evs = db.execute("SELECT ts, node, kind, level, msg FROM events WHERE ts >= ? AND ts < ? ORDER BY ts",
                         (start, end + 86400)).fetchall()
        outs = db.execute("SELECT kind, last_alive, boot_at, hub_at, down_s, boot_s, lost, recovered FROM outages "
                          "WHERE detected >= ? AND detected < ? AND kind != 'first' ORDER BY detected",
                          (start, end + 3600)).fetchall()
        boots = db.execute("SELECT ts, node, reason FROM node_boots WHERE ts >= ? AND ts < ? ORDER BY ts",
                           (start, end)).fetchall()
    rows += [(t, n, v, m) for t, n, v, m in pending_between(start, end)]
    rows.sort()
    series = collections.defaultdict(list)
    for t, n, v, m in rows:
        series[(n, m)].append((t, v))
    problems = _event_pairs(evs, start, end_eff)
    with alerts_lock:
        open_crit = [a for a in active_alerts.values() if a["level"] == "crit"]
    with state_lock:
        live = {k: (v.get("status"), v.get("temp_ts")) for k, v in nodes.items()}
    node_ids = sorted({n for (n, _m) in series if n != PI_ID} | {k for k in live if k != PI_ID})

    attention, marks = [], []
    for o in outs:
        kind, last, boot_at, hub_at, down, boot_s, lost, rec = o
        if kind == "power" and last:
            txt = f"The Pi lost power at {_say_time(last)} for about {_say_dur(down or 0)}"
            txt += (f"; {lost} readings were lost" + (f" and {rec} recovered." if rec else ".")) if lost else "."
            attention.append({"level": "crit", "ts": last, "text": txt, "short": f"The Pi lost power at {_say_time(last)} for {_say_dur(down or 0)}."})
            marks.append({"ts": last, "level": "crit", "label": "Pi power cut"})
    groups = collections.OrderedDict()
    for p in problems:
        if p["level"] == "crit" and p["kind"] != "power":
            groups.setdefault((p["node"], p["kind"]), []).append(p)
            marks.append({"ts": p["ts"], "level": "crit", "label": f"{p['node']} {p['kind']}"})
    for (g_node, g_kind), ps in groups.items():
        if len(ps) == 1:
            txt = _problem_sentence(ps[0])
        else:
            who = "The hub" if g_node == PI_ID else node_name(g_node)
            verb = {"offline": "went offline", "sensor": "had sensor faults"}.get(g_kind, "had problems")
            total = sum(x["dur"] or 0 for x in ps)
            txt = f"{who} {verb} {len(ps)} times, first at {_say_time(ps[0]['ts'])}, about {_say_dur(total)} in total"
            txt += ", and it hasn't recovered yet." if ps[-1]["dur"] is None else "."
        attention.append({"level": "crit", "ts": ps[0]["ts"], "text": txt, "short": txt})
    if is_today and open_crit and not any(a["level"] == "crit" for a in attention):
        for a in open_crit:
            attention.append({"level": "crit", "ts": a["since"], "text": f"Still open: {a.get('title') or a['msg']}.",
                              "short": f"Still open: {a.get('title') or a['msg']}."})
    anomalies = []
    for nid in node_ids:
        anomalies += _find_anomalies(nid, series, _baseline(nid, day), is_today)
    pi_rows = series.get((PI_ID, "temp"), [])
    pi_hi = max(pi_rows, key=lambda r: r[1]) if pi_rows else None
    if pi_hi and pi_hi[1] >= 70:
        anomalies.append({"node": PI_ID, "ts": pi_hi[0], "level": "anomaly", "metric": "temp",
                          "text": f"The Pi ran hot, reaching {pi_hi[1]:.0f} degrees at {_say_time(pi_hi[0])}."})
    for a in anomalies:
        attention.append({**a, "short": a["text"]})
    warns = [p for p in problems if p["level"] == "warn" and p["kind"] not in ("login", "data_lost", "node_boot")]
    for p in warns:
        attention.append({"level": "warn", "ts": p["ts"], "text": _problem_sentence(p), "short": _problem_sentence(p)})
        marks.append({"ts": p["ts"], "level": "warn", "label": p["kind"]})

    node_stats = []
    for nid in node_ids:
        entry = {"id": nid, "name": node_name(nid), "outdoor": is_outdoor(nid), "missing": 0, "restarts": 0,
                 "online": live.get(nid, (None, None))[0] == "online"}
        for metric in ("temp", "hum"):
            st = _series_stats(series.get((nid, metric), []), metric)
            if st:
                entry[metric] = {"hi": round(st["hi"][1], 1), "hi_t": st["hi"][0], "lo": round(st["lo"][1], 1),
                                 "lo_t": st["lo"][0], "avg": round(st["avg"], 1), "now": round(st["now"][1], 1),
                                 "rise": round(st["rise"][0], 1), "rise_from": st["rise"][1], "rise_to": st["rise"][2],
                                 "change": round(st["end"] - st["start"], 1)}
        try:
            gaps, _iv = _gaps(nid, start, end_eff)
        except sqlite3.Error:
            gaps = []
        entry["missing"] = sum(g[2] for g in gaps)
        entry["longest_gap_s"] = round(max((g[1] - g[0] for g in gaps), default=0))
        entry["restarts"] = sum(1 for _, n, _ in boots if n == nid)
        node_stats.append(entry)
    for g_nid in node_ids:
        try:
            for a, b, _m in _gaps(g_nid, start, end_eff)[0]:
                if b - a >= 300:
                    marks.append({"ts": a, "end": b, "level": "gap", "label": f"{g_nid}: no data"})
        except sqlite3.Error:
            pass

    wx = weather_rows(start, end_eff + 3600) if WX_ENABLED else []
    revs = rain_events(start, end_eff) if (WX_ENABLED or outdoor_nodes()) else []
    rain_starts = [(r["ts"], r["desc"]) for r in revs if r["verdict"] in ("confirmed", "likely", "forecast")]
    out_t = [r[1] for r in wx if r[1] is not None and r[0] <= end_eff]
    outdoor = {"hi": max(out_t), "lo": min(out_t), "avg": sum(out_t) / len(out_t)} if out_t else None
    insights = []
    for r in revs:
        nm = _subj(r["node"]) if r.get("node") else "The sensor"
        if r["verdict"] == "dry" and r["forecast"]:
            insights.append({"node": r.get("node"), "ts": r["ts"], "kind": "rain",
                             "text": f"The forecast had {r['desc']} {_human_time(r['ts'], short=True)}, but you said it stayed dry."})
        elif r["verdict"] == "confirmed" and not r["forecast"]:
            insights.append({"node": r.get("node"), "ts": r["ts"], "kind": "rain",
                             "text": f"It rained {_human_time(r['ts'])} without the forecast expecting it. "
                                     f"{nm} sensor caught it, and you confirmed."})
    for e in node_stats:
        h = e.get("hum")
        if h and h["rise"] >= 5 and h["rise_to"]:
            rain = next((r for r in revs if r["verdict"] != "dry" and h["rise_from"] - 2 * 3600 <= r["ts"] <= h["rise_to"] + 2 * 3600), None)
            if rain:
                rel, d = rain["ts"] - h["rise_from"], rain["desc"]
                nm = _subj(e["id"]) if e.get("outdoor") else e["name"]
                if rel > 900:
                    txt = (f"{nm}'s humidity started climbing {_human_time(h['rise_from'])}, about {_say_dur(rel)} "
                           f"before {d} began. That was the rain on its way.")
                elif rel >= -1200:
                    txt = f"{nm}'s humidity jumped {h['rise']:.0f}% as {d} set in, {_human_time(rain['ts'], short=True)}."
                else:
                    txt = (f"{nm}'s humidity climbed {h['rise']:.0f}% {_human_time(h['rise_from'])}, "
                           f"after {d} started {_human_time(rain['ts'], short=True)}.")
                txt += {"confirmed": " You confirmed it rained.", "maybe": " Did it really rain? There's a question for you on the dashboard."
                        if rain.get("id") and not rain.get("answer") else ""}.get(rain["verdict"], "")
                insights = [i for i in insights if not (i["kind"] == "rain" and abs(i["ts"] - rain["ts"]) < 3600)]
                insights.append({"node": e["id"], "ts": h["rise_to"], "kind": "rain", "text": txt})
                for a in list(attention):
                    if a.get("level") == "anomaly" and a.get("node") == e["id"] and a.get("metric") == "hum" \
                            and abs(a["ts"] - h["rise_to"]) < 3 * 3600:
                        attention.remove(a)
        t = e.get("temp")
        if t and e.get("outdoor") and wx:
            vs = _vs_forecast(series.get((e["id"], "temp"), []), wx)
            if vs:
                e["vs_forecast"] = vs["vs_forecast"]
                e["sun"] = [{"from": a, "to": b, "above": round(m, 1)} for a, b, m in vs["sun"]]
                nm = _subj(e["id"])
                place = WX_PLACE or "your area"
                if vs["sun"]:
                    a_, b_, m_ = max(vs["sun"], key=lambda x: x[1] - x[0])
                    e["sun_peak"] = any(a <= t["hi_t"] <= b for a, b, _m in vs["sun"])
                    insights.append({"node": e["id"], "ts": a_, "kind": "sun",
                                     "text": f"The sun was on {nm.lower() if nm.startswith('The ') else nm} sensor from about "
                                             f"{_clock(a_)} to {_clock(b_)}, so it read up to {m_:.0f}° above the air temperature."})
                    for a in list(attention):
                        if a.get("level") == "anomaly" and a.get("node") == e["id"] and a.get("metric") == "temp" \
                                and any(x - 3600 <= a["ts"] <= y + 3600 for x, y, _m in vs["sun"]):
                            attention.remove(a)
                if vs["vs_forecast"] is not None:
                    dv = vs["vs_forecast"]
                    lead = "Out of the sun, " if vs["sun"] else ""
                    nm2 = nm.lower() if lead and nm.startswith("The ") else nm
                    insights.append({"node": e["id"], "ts": t["hi_t"], "kind": "forecast",
                                     "text": f"{lead}{nm2} matched the online temperature for {place} to within a degree."
                                     if abs(dv) < 1 else
                                     f"{lead}{nm2} ran about {abs(dv):.0f}° {'warmer' if dv > 0 else 'cooler'} than the "
                                     f"online temperature for {place}."})
        elif t and outdoor and not e.get("outdoor"):
            diff = t["avg"] - outdoor["avg"]
            if abs(diff) >= 3:
                insights.append({"node": e["id"], "ts": t["hi_t"], "kind": "outside",
                                 "text": f"{e['name']} stayed about {abs(diff):.0f}° {'warmer' if diff > 0 else 'cooler'} "
                                         f"than outside on average."})
        if t and h:
            fl = feels_like(t["hi"], h["lo"] if t["hi"] >= 30 else h["avg"])
            c = comfort(t["avg"], h["avg"])
            e["comfort"] = c
            e["feels_hi"] = round(fl, 1) if fl is not None else None
    for r in revs:
        if r["verdict"] != "dry" and not r.get("pred_only"):
            marks.append({"ts": r["ts"], "level": "rain",
                          "label": r["desc"] + ("" if r["verdict"] in ("confirmed", "likely") else " (not confirmed)")})
    yday = day_of(start - 3600)
    with db_lock:
        prev = {(n, m): (lo, hi, avg) for n, m, lo, hi, avg in db.execute(
            "SELECT node, metric, min, max, avg FROM daily WHERE day = ?", (yday,))}
        week = {(n, m): hi for n, m, hi in db.execute(
            "SELECT node, metric, MAX(max) FROM daily WHERE day >= ? AND day < ? GROUP BY node, metric",
            (day_of(start - 7 * 86400), day))}
    for e in node_stats:
        t = e.get("temp")
        if not t:
            continue
        p = prev.get((e["id"], "temp"))
        e["vs_yesterday"] = round(t["avg"] - p[2], 1) if p else None
        w = week.get((e["id"], "temp"))
        e["week_high"] = bool(w is not None and t["hi"] > w + 0.3)
    attention.sort(key=lambda a: ({"crit": 0, "anomaly": 1, "warn": 2}[a["level"]], a["ts"]))
    tomorrow = next((d for d in wx_state.get("daily", []) if d["day"] == day_of(start + 86400 + 3600)), None)

    crit = [a for a in attention if a["level"] == "crit"]
    anom = [a for a in attention if a["level"] == "anomaly"]
    look = sum(1 for a in attention if a["level"] in ("anomaly", "warn"))
    if crit:
        headline = f"{len(crit)} problem{'s' if len(crit) != 1 else ''} {'today' if is_today else label}" + \
                   (f", and {look} thing{'s' if look != 1 else ''} worth a look." if look else ".")
    elif look:
        headline = f"No problems, {look} thing{'s' if look != 1 else ''} worth a look."
    else:
        bits = []
        if rain_starts:
            bits.append(f"{_cap(rain_starts[0][1])} {_human_time(rain_starts[0][0])}")
        rs = [e for e in node_stats if e["restarts"]]
        if rs:
            bits.append(f"{rs[0]['name']} restarted " + ("once" if rs[0]["restarts"] == 1 else f"{rs[0]['restarts']} times"))
        headline = "No problems. " + ("; ".join(bits) + "." if bits else "Everything ran normally.")
    questions = [r for r in revs if r.get("id") and not r.get("answer")]
    rnd = random.Random(day)
    hour = time.localtime(now).tm_hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    when = "today" if is_today else label
    brief = [f"{greet}! " + rnd.choice([f"Here's how {when} went.", f"Here's a quick look at {when}.",
                                        f"Here's your round-up for {when}."])]
    if crit:
        brief.append(rnd.choice(["First, the important bit.", "A couple of things needed attention.",
                                 "Let's start with what went wrong."]) if len(crit) > 1 else
                     rnd.choice(["First, one thing that went wrong.", "One problem first."]))
        brief += [a["short"] for a in crit[:2]]
        if len(crit) > 2:
            brief.append(f"There were {len(crit) - 2} more; they're on the dashboard.")
    else:
        brief.append(rnd.choice(["Nothing went wrong.", "No problems at all.", "A smooth day, no problems.",
                                 "Everything ran smoothly."]))
    has_out = any(e.get("outdoor") for e in node_stats)
    if outdoor and not has_out:
        rain_txt = f", with {rain_starts[0][1]} {_human_time(rain_starts[0][0], short=True)}" if rain_starts else ""
        brief.append(f"Outside: {outdoor['lo']:.0f} to {outdoor['hi']:.0f}°{rain_txt}.")
    elif rain_starts and not any(i["kind"] == "rain" for i in insights):
        r0 = next(r for r in revs if (r["ts"], r["desc"]) == rain_starts[0])
        brief.append(f"There was {r0['desc']} {_human_time(r0['ts'])}" + (", which you confirmed." if r0["verdict"] == "confirmed" else "."))
    _nm = lambda e: _subj(e["id"]) if e.get("outdoor") else e["name"]
    shown = [e for e in node_stats if e.get("temp")]
    shown = shown if len(shown) <= 4 else sorted(shown, key=lambda e: -e["temp"]["hi"])[:4]
    if len(shown) == 1:
        e = shown[0]
        t = e["temp"]
        brief.append(f"{_nm(e)} peaked at {t['hi']:.0f}° {_human_time(t['hi_t'])}"
                     + (" in direct sun" if e.get("sun_peak") else "")
                     + f" and dipped to {t['lo']:.0f}° {_human_time(t['lo_t'])}.")
    elif shown:
        parts = [f"{e['name']} {e['temp']['hi']:.0f}°" + (" in the sun" if e.get("sun_peak") else "") for e in shown]
        brief.append(("Highs: " if len(parts) > 1 else "") + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + ".")
    note = next((f"{_nm(e)} had its warmest day of the week." for e in shown if e.get("week_high")), None) or \
        next((f"{_nm(e)} was {'a bit' if abs(e['vs_yesterday']) < 1.5 else 'noticeably'} "
              f"{'warmer' if e['vs_yesterday'] > 0 else 'cooler'} than yesterday."
              for e in shown if e.get("vs_yesterday") is not None and abs(e["vs_yesterday"]) >= 0.8), None)
    if note:
        brief.append(note)
    damp = [e for e in shown if e.get("comfort") in ("muggy", "humid", "dry") and e.get("hum")]
    if damp:
        e = damp[0]
        brief.append(f"It was {e['comfort']} out there, around {e['hum']['avg']:.0f}% humidity." if e.get("outdoor") and len(damp) == 1
                     else f"{_nm(e)} felt {e['comfort']}, around {e['hum']['avg']:.0f}% humidity.")
    elif len(shown) == 1 and shown[0].get("hum"):
        h = shown[0]["hum"]
        brief.append(f"Humidity stayed between {h['lo']:.0f} and {h['hi']:.0f}%.")
    picks = [i["text"] for i in insights if i["kind"] == "rain"][:1] + [a["short"] for a in anom][:1]
    if len(picks) < 2:
        picks += [i["text"] for i in insights if i["kind"] == "sun"][:1]
    for i, ptxt in enumerate(picks):
        brief.append(("Also, " + (ptxt[0].lower() + ptxt[1:] if re.match(r"(The|It|There|A|An)\b", ptxt) else ptxt)) if i else ptxt)
    missed = sum(e["missing"] for e in node_stats)
    restarts = sum(e["restarts"] for e in node_stats)
    if missed >= 30 or restarts:
        rnodes = [e for e in node_stats if e["restarts"]]
        rtxt = (f"{rnodes[0]['name']} restarted " + ("once" if rnodes[0]["restarts"] == 1 else f"{rnodes[0]['restarts']} times")
                if len(rnodes) == 1 else f"nodes restarted {restarts} times")
        bits = ([f"{missed} readings went missing"] if missed >= 30 else []) + ([rtxt] if restarts else [])
        line = " and ".join(bits)
        brief.append(line[:1].upper() + line[1:] + ".")
    if is_today:
        off_ids = {e["id"] for e in node_stats if not e["online"]} | \
            {a["node"] for a in open_crit if str(a.get("key", "")).startswith("offline:")}
        off = [node_name(i) for i in sorted(off_ids)]
        if off:
            brief.append(f"{_cap(', '.join(off))} {'is' if len(off) == 1 else 'are'} still not reporting.")
        ol = rain_outlook() if has_out else None
        if ol and ol["p"] >= 0.5:
            brief.append(f"Heads up: I'd put rain {ol['where']} in the next two hours at {round(ol['p'] * 100)}%.")
        if questions:
            brief.append("I've got a question for you on the dashboard about whether it rained.")
        ob = next((o for o in (outlook()["nodes"] if WX_ENABLED else []) if o["place"] == "outdoor"), None)
        if tomorrow:
            rain_t = f", with a {tomorrow['rain_prob']:.0f}% chance of rain" if (tomorrow.get("rain_prob") or 0) >= 40 else ""
            verb = "brings" if tomorrow.get("code") in RAINY else "looks"
            tm = (ob or {}).get("tomorrow") or {}
            if tm.get("lo") is not None and tm.get("hi") is not None and tm.get("day") != day:
                brief.append(f"Tomorrow {verb} {tomorrow['desc'] or 'much the same'}{rain_t}; {ob['where']} I expect "
                             f"{tm['lo']:.0f} to {tm['hi']:.0f}°" + (", with the afternoon sun." if tm.get("sun") else "."))
            else:
                brief.append(f"Tomorrow {verb} {tomorrow['desc'] or 'much the same'}, {tomorrow['lo']:.0f} to {tomorrow['hi']:.0f}°{rain_t}.")
        if ob and ob.get("dew") and hour >= 18:
            brief.append(f"Dew is likely {ob['where']} around {_clock(ob['dew']['ts'])}.")
        brief.append(rnd.choice(["That's all for now.", "That's everything.", "That's the lot."]) if hour < 17
                     else rnd.choice(["That's all. Have a good evening!", "That's everything for today. Good night!",
                                      "That's it for today."]))
    text = " ".join(brief)

    sections = [{"key": "critical", "title": "Problems",
                 "lines": [a["text"] for a in crit] or ["No problems " + ("so far today." if is_today else f"{label}.")]}]
    if anom:
        sections.append({"key": "anomalies", "title": "Worth a look", "lines": [a["text"] for a in anom]})
    for metric, title in (("temp", "Temperature"), ("hum", "Humidity")):
        unit = "degrees" if metric == "temp" else "percent"
        lines = []
        for e in node_stats:
            m = e.get(metric)
            if not m:
                continue
            s = (f"{e['name']}: high {_say_num(m['hi'], metric)} at {_say_time(m['hi_t'])}, "
                 f"low {_say_num(m['lo'], metric)} at {_say_time(m['lo_t'])}, average {_say_num(m['avg'], metric)} {unit}.")
            if m["rise"] >= UNITS[metric][1] and m["rise_from"]:
                s += f" Fastest rise {_say_time(m['rise_from'])} to {_say_time(m['rise_to'])}, up {_say_num(m['rise'], metric)}."
            lines.append(s)
        if lines:
            sections.append({"key": metric, "title": title, "lines": lines})
    inter = []
    for o in outs:
        kind, last, boot_at, hub_at, down, boot_s, lost, rec = o
        if kind == "reboot":
            inter.append(f"The Pi rebooted at {_say_time(boot_at or hub_at)}" + (f" in {_say_dur(boot_s)}." if boot_s else "."))
        elif kind == "crash":
            inter.append(f"The hub software restarted itself at {_say_time(hub_at)}.")
        elif kind == "power" and boot_s:
            inter.append(f"After the power cut, the Pi took {_say_dur(boot_s)} to start.")
    for ts, nid, reason in boots:
        inter.append(f"{node_name(nid)} restarted at {_say_time(ts)}: {NODE_REASON.get(reason, 'reason unknown')}.")
    for e in node_stats:
        if e["missing"]:
            inter.append(f"{e['name']} missed {e['missing']} readings; longest gap {_say_dur(e['longest_gap_s'])}.")
    if inter:
        sections.append({"key": "outages", "title": "Outages and restarts", "lines": inter})
    if warns:
        sections.append({"key": "warnings", "title": "Warnings", "lines": [_problem_sentence(p) for p in warns][:8]})
    if pi_hi:
        sections.append({"key": "hub", "title": "Hub", "lines": [
            f"The Pi's processor peaked at {pi_hi[1]:.0f} degrees at {_say_time(pi_hi[0])}."]})
    if outdoor or insights or revs:
        wl = []
        if outdoor:
            wl.append(f"Online weather for {WX_PLACE or 'your area'}: {outdoor['lo']:.0f} to {outdoor['hi']:.0f} degrees"
                      + (", " + ", ".join(f"{d} {_human_time(t, short=True)}" for t, d in rain_starts[:3]) if rain_starts else "") + ".")
        wl += [i["text"] for i in insights]
        sc = rain_score() if has_out else None
        if sc and sc["hours"] >= 24:
            wl.append(f"Rain predictor, last 30 days: caught {sc['caught']} of {sc['rains']} rains with "
                      f"{sc['false_alarms']} false alarm{'s' if sc['false_alarms'] != 1 else ''}; the forecast alone caught "
                      f"{sc['fc_caught']} with {sc['fc_false_alarms']}." if sc["rains"] else
                      f"Rain predictor, last 30 days: no rain yet, {sc['false_alarms']} false alarms (forecast {sc['fc_false_alarms']}).")
        for o in (outlook()["nodes"] if is_today and WX_ENABLED else []):
            tn, tm = o.get("tonight"), o.get("tomorrow") or {}
            if o["place"] == "outdoor" and (tn or tm.get("hi") is not None):
                bits = ([f"tonight's low about {tn['lo']:.1f}° around {_clock(tn['lo_t'])} (forecast {tn['fc_lo']:.1f}°)"] if tn else []) + \
                       ([f"tomorrow's high about {tm['hi']:.1f}° around {_clock(tm['hi_t'])} (forecast {tm['fc_hi']:.1f}°)"]
                        if tm.get("hi") is not None else [])
                wl.append(f"{o['name']} outlook: " + "; ".join(bits) + ".")
            elif o["place"] == "indoor" and tm:
                wl.append(f"{o['name']} tomorrow: about {tm['lo']:.0f} to {tm['hi']:.0f}°.")
        for nid, sc in (temp_score().items() if is_today else []):
            if sc["calls"] >= 6:
                wl.append(f"Temperature calls for {node_name(nid)}, last 30 days: off by {sc['off_by']}° on average"
                          + (f" ({sc['base']}: {sc['base_off_by']}°)." if sc["base_off_by"] is not None else "."))
        if tomorrow:
            wl.append(f"Tomorrow: {tomorrow['desc']}, {tomorrow['lo']:.0f} to {tomorrow['hi']:.0f} degrees"
                      + (f", {tomorrow['rain_prob']:.0f}% chance of rain." if tomorrow.get("rain_prob") is not None else "."))
        sections.insert(1, {"key": "insights", "title": "Weather and insights", "lines": wl})
    detail = " ".join([f"Full summary for {label}."] + [ln for s in sections for ln in s["lines"]])
    return {"day": day, "label": label, "headline": headline, "intro": brief[0], "text": text, "detail_text": detail,
            "attention": sorted(attention, key=lambda a: ({"crit": 0, "anomaly": 1, "warn": 2}[a["level"]], a["ts"])),
            "nodes": node_stats, "marks": sorted(marks, key=lambda m: m["ts"]), "sections": sections,
            "critical": bool(crit), "anomalies": len(anom), "generated": now,
            "hub": {"cpu_hi": round(pi_hi[1], 1), "cpu_hi_t": pi_hi[0]} if pi_hi else None,
            "insights": insights, "outdoor": outdoor,
            "rain": [{"ts": r["ts"], "desc": r["desc"], "verdict": r["verdict"], "id": r.get("id")} for r in revs],
            "questions": len(questions), "outlook": rain_outlook() if is_today and has_out else None,
            "tomorrow": tomorrow, "live": live_insights() if is_today else []}


def _piper_wav(text, path):
    from piper import PiperVoice
    model = os.path.join(PIPER_DIR, PIPER_VOICE + ".onnx")
    if _piper.get("model") != model:
        _piper.update(model=model, voice=PiperVoice.load(model))
    voice = _piper["voice"]
    with wave.open(path, "wb") as wf:
        if hasattr(voice, "synthesize_wav"):
            try:
                from piper import SynthesisConfig
                voice.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=1.08))
            except ImportError:
                voice.synthesize_wav(text, wf)
        else:
            voice.synthesize(text, wf)


def _tts_file(text):
    os.makedirs(TTS_DIR, exist_ok=True)
    for old in os.listdir(TTS_DIR):
        p = os.path.join(TTS_DIR, old)
        if time.time() - os.path.getmtime(p) > 3600:
            os.remove(p)
    token = secrets.token_hex(12)
    if SPEAK_ENGINE == "piper":
        try:
            _piper_wav(text, os.path.join(TTS_DIR, token + ".wav"))
            speak_state["engine"] = f"piper {PIPER_VOICE}"
            return token + ".wav"
        except Exception as e:
            log.warning("piper voice failed (%s), using Google's voice instead", e)
    from gtts import gTTS
    gTTS(text, lang=SPEAK_LANG, tld=SPEAK_TLD).save(os.path.join(TTS_DIR, token + ".mp3"))
    speak_state["engine"] = f"gtts {SPEAK_LANG}-{SPEAK_TLD}"
    return token + ".mp3"


def _find_speaker(name, timeout=10):
    import pychromecast
    casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[name], discovery_timeout=timeout)
    try:
        browser.stop_discovery()
    except Exception:
        pass
    return casts[0] if casts else None


def list_speakers(timeout=8):
    import pychromecast
    casts, browser = pychromecast.get_chromecasts(timeout=timeout)
    try:
        browser.stop_discovery()
    except Exception:
        pass
    return sorted({c.cast_info.friendly_name for c in casts}) if casts else []


def speak(text, speaker=None):
    speaker = speaker or SPEAKER_NAME
    if not speaker:
        raise RuntimeError("no speaker chosen (run setup-speaker.sh)")
    name = _tts_file(text)
    cast = _find_speaker(speaker)
    if cast is None:
        raise RuntimeError(f"speaker '{speaker}' not found on this network")
    cast.wait(timeout=10)
    host = getattr(getattr(cast, "cast_info", None), "host", None) or cast.socket_client.host
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect((host, 9))
            src = sk.getsockname()[0]
    except OSError:
        raise RuntimeError(f"no route from the hub to {host}")
    url = f"http://{src}:{PORT}/tts/{name}"
    mc = cast.media_controller
    mc.play_media(url, "audio/wav" if name.endswith(".wav") else "audio/mpeg", title=f"{SITE_NAME} summary")
    mc.block_until_active(timeout=20)
    speak_state.update(last_ok=time.time(), last_error=None, speaker=speaker)


def queue_speak(text, push_title=None):
    if push_title and SUMMARY_PUSH:
        notify(push_title, text, "info", "summary", push={"tag": "summary", "url": "/#overview"})
    if SPEAKER_NAME:
        try:
            speak_q.put_nowait(text)
        except queue.Full:
            return False
    return True


def speak_loop():
    while True:
        text = speak_q.get()
        try:
            speak(text)
        except Exception as e:
            speak_state["last_error"] = f"{type(e).__name__}: {e}"[:160]
            log.warning("speaking failed: %s", e)


def summary_loop():
    state_path = os.path.join(DATA_DIR, "summary_state.json")
    req_path = os.path.join(DATA_DIR, "speak.request")
    while True:
        time.sleep(5)
        if os.path.exists(req_path):
            try:
                with open(req_path) as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = {}
            try:
                os.remove(req_path)
            except OSError:
                pass
            day = req.get("day") or None
            if day == "yesterday":
                day = day_of(time.time() - 86400)
            text = str(req.get("text") or "")[:4000] or build_summary(day)["text"]
            queue_speak(text, push_title=None if req.get("text") else f"{SITE_NAME}: summary")
        if not SUMMARY_TIME:
            continue
        today = day_of(time.time())
        if time.strftime("%H:%M") < SUMMARY_TIME:
            continue
        try:
            with open(state_path) as f:
                if json.load(f).get("last") == today:
                    continue
        except (OSError, ValueError):
            pass
        s = build_summary(today)
        queue_speak(s["text"], push_title=f"{SITE_NAME}: today's summary")
        try:
            with open(state_path, "w") as f:
                json.dump({"last": today}, f)
        except OSError:
            pass


TTS_NAME = re.compile(r"^[0-9a-f]{24}\.(mp3|wav)$")


@app.get("/tts/<name>")
def tts_file(name):
    if not TTS_NAME.match(name) or not os.path.exists(os.path.join(TTS_DIR, name)):
        return jsonify(error="not found"), 404
    return send_from_directory(TTS_DIR, name, mimetype="audio/wav" if name.endswith(".wav") else "audio/mpeg")


@app.get("/api/summary")
def api_summary():
    day = request.args.get("day", "")
    if day == "yesterday":
        day = day_of(time.time() - 86400)
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise BadRequest("day must be YYYY-MM-DD, 'yesterday' or empty for today")
    out = build_summary(day or None)
    out["speaker"] = {"name": SPEAKER_NAME or None, "last_ok": speak_state["last_ok"],
                      "last_error": speak_state["last_error"], "daily_at": SUMMARY_TIME or None}
    return jsonify(out)


@app.post("/api/summary/speak")
def api_summary_speak():
    body = request.get_json(silent=True) or {}
    day = str(body.get("day") or "")
    day = day_of(time.time() - 86400) if day == "yesterday" else day
    if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise BadRequest("day must be YYYY-MM-DD or 'yesterday'")
    text = str(body.get("text") or "")[:4000] or build_summary(day or None)["text"]
    if not SPEAKER_NAME:
        return jsonify(error="No speaker set up. Run setup-speaker.sh on the Pi."), 400
    if not queue_speak(text):
        return jsonify(error="Busy - try again in a moment"), 503
    return jsonify(ok=True, speaker=SPEAKER_NAME)


try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    WEBPUSH_OK = True
except ImportError:
    WEBPUSH_OK = False
VAPID_PATH = os.path.join(CONF_DIR, "vapid.pem")
PUSH_CATS = {"problems": True, "rain": True, "tips": False, "summary": True, "quiet": True}
push_q = queue.Queue(maxsize=200)
push_state = {"last_ok": None, "last_error": None, "sent": 0}
_vapid = {}
with db_lock:
    db.execute("CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, prefs TEXT, "
               "label TEXT, created REAL, last_ok REAL, fails INTEGER DEFAULT 0)")
    db.commit()


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _vapid_key():
    if "key" not in _vapid:
        try:
            with open(VAPID_PATH, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), None)
        except (OSError, ValueError):
            key = ec.generate_private_key(ec.SECP256R1())
            fd = os.open(VAPID_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        _vapid["key"] = key
        _vapid["pub"] = _b64u(key.public_key().public_bytes(serialization.Encoding.X962,
                                                            serialization.PublicFormat.UncompressedPoint))
    return _vapid["key"], _vapid["pub"]


def _vapid_header(endpoint):
    key, pub = _vapid_key()
    aud = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(endpoint))
    cached = _vapid.get(aud)
    if cached and cached[1] > time.time() + 3600:
        return cached[0]
    exp = int(time.time()) + 12 * 3600
    seg = lambda o: _b64u(json.dumps(o, separators=(",", ":")).encode())
    signing = f"{seg({'typ': 'JWT', 'alg': 'ES256'})}.{seg({'aud': aud, 'exp': exp, 'sub': 'https://github.com/Gautham-Dev007/IoT_Pi32'})}"
    r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{signing}.{_b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    hdr = f"vapid t={jwt}, k={pub}"
    _vapid[aud] = (hdr, exp)
    return hdr


def _hkdf(salt, ikm, info, n):
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def push_encrypt(payload, p256dh, auth):
    ua_pub = _b64u_d(p256dh)
    secret = _b64u_d(auth)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + body


def push_send(sub, msg, urgency="normal", ttl=86400):
    endpoint, p256dh, auth = sub
    data = push_encrypt(json.dumps(msg, separators=(",", ":")).encode(), p256dh, auth)
    req = urllib.request.Request(endpoint, data=data, method="POST", headers={
        "Authorization": _vapid_header(endpoint), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": urgency})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            with db_lock:
                db.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
                db.commit()
            return False
        raise RuntimeError(f"push service said {e.code}: {e.read()[:120]!r}") from None
    return True


def _push_devices():
    with db_lock:
        return db.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0]


def _push_cat(level, category, cat=None):
    if cat:
        return cat
    if category == "summary":
        return "summary"
    if category == "insight":
        return "tips"
    return "problems"


def _quiet_now():
    h = time.localtime().tm_hour
    return h >= 22 or h < 7


def webpush(title, body, level="info", category="", cat=None, tag=None, url="/", actions=None, answer=None):
    if not WEBPUSH_OK:
        return
    msg = {"title": title, "body": body, "level": level, "cat": _push_cat(level, category, cat), "tag": tag,
           "url": url, "ts": int(time.time())}
    if actions:
        msg["actions"] = actions
    if answer:
        msg["answer"] = answer
    try:
        push_q.put_nowait(msg)
    except queue.Full:
        log.warning("push queue full, dropped: %s", title)


def push_loop():
    while True:
        msg = push_q.get()
        with db_lock:
            subs = db.execute("SELECT endpoint, p256dh, auth, prefs FROM push_subs").fetchall()
        for endpoint, p256dh, auth, prefs in subs:
            try:
                pr = {**PUSH_CATS, **json.loads(prefs or "{}")}
            except ValueError:
                pr = dict(PUSH_CATS)
            if msg.get("only") and msg["only"] != endpoint:
                continue
            if not msg.get("only") and not pr.get(msg["cat"], False) and msg["level"] != "crit":
                continue
            m = dict(msg, silent=bool(pr.get("quiet") and _quiet_now() and msg["level"] != "crit"))
            m.pop("only", None)
            for attempt in range(3):
                try:
                    if push_send((endpoint, p256dh, auth), m, "high" if msg["level"] in ("crit", "warn") else "normal"):
                        with db_lock:
                            db.execute("UPDATE push_subs SET last_ok = ?, fails = 0 WHERE endpoint = ?", (time.time(), endpoint))
                            db.commit()
                        push_state.update(last_ok=time.time(), sent=push_state["sent"] + 1)
                    break
                except Exception as e:
                    push_state["last_error"] = str(e)[:160]
                    if attempt == 2:
                        with db_lock:
                            db.execute("UPDATE push_subs SET fails = fails + 1 WHERE endpoint = ?", (endpoint,))
                            db.execute("DELETE FROM push_subs WHERE endpoint = ? AND fails >= 50", (endpoint,))
                            db.commit()
                    else:
                        time.sleep(5 * (attempt + 1))


def _valid_sub(sub):
    try:
        ep, k = str(sub["endpoint"]), sub["keys"]
        if not ep.startswith("https://") or len(ep) > 1000:
            return None
        if len(_b64u_d(k["p256dh"])) != 65 or len(_b64u_d(k["auth"])) != 16:
            return None
        return ep, k["p256dh"], k["auth"]
    except (KeyError, TypeError, ValueError):
        return None


@app.get("/api/push")
def api_push():
    out = {"enabled": WEBPUSH_OK, "cats": PUSH_CATS, **push_state}
    if WEBPUSH_OK:
        out["key"] = _vapid_key()[1]
    with db_lock:
        out["devices"] = [{"label": r[0], "since": r[1], "last_ok": r[2]} for r in
                          db.execute("SELECT label, created, last_ok FROM push_subs ORDER BY created")]
    ep = request.args.get("endpoint")
    if ep:
        with db_lock:
            r = db.execute("SELECT prefs FROM push_subs WHERE endpoint = ?", (ep,)).fetchone()
        out["this"] = {**PUSH_CATS, **json.loads(r[0] or "{}")} if r else None
    if g.role != "admin":
        out["devices"] = len(out["devices"])
    return jsonify(out)


@app.post("/api/push/subscribe")
def api_push_subscribe():
    if not WEBPUSH_OK:
        return jsonify(error="Notifications need python3-cryptography on the Pi: sudo apt install python3-cryptography"), 503
    body = request.get_json(silent=True) or {}
    sub = _valid_sub(body.get("subscription") or {})
    if not sub:
        return jsonify(error="bad subscription"), 400
    old = None
    if body.get("replaces"):
        with db_lock:
            old = db.execute("SELECT prefs, label FROM push_subs WHERE endpoint = ?", (str(body["replaces"]),)).fetchone()
            db.execute("DELETE FROM push_subs WHERE endpoint = ?", (str(body["replaces"]),))
            db.commit()
    given = body.get("prefs") or (json.loads(old[0]) if old and old[0] else {})
    prefs = {k: bool(given.get(k, v)) for k, v in PUSH_CATS.items()}
    label = re.sub(r"[^\w .,()/-]", "", str(body.get("label") or (old[1] if old else "")))[:60] or "Device"
    with db_lock:
        db.execute("INSERT INTO push_subs (endpoint, p256dh, auth, prefs, label, created, fails) VALUES (?,?,?,?,?,?,0) "
                   "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth, "
                   "prefs = excluded.prefs, label = excluded.label", (*sub, json.dumps(prefs), label, time.time()))
        db.commit()
    return jsonify(ok=True, prefs=prefs)


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe():
    ep = str((request.get_json(silent=True) or {}).get("endpoint", ""))
    with db_lock:
        n = db.execute("DELETE FROM push_subs WHERE endpoint = ?", (ep,)).rowcount
        db.commit()
    return jsonify(ok=True, removed=n)


@app.post("/api/push/test")
def api_push_test():
    ep = str((request.get_json(silent=True) or {}).get("endpoint", ""))
    with db_lock:
        known = db.execute("SELECT 1 FROM push_subs WHERE endpoint = ?", (ep,)).fetchone()
    if not known:
        return jsonify(error="This device isn't subscribed"), 404
    msg = {"title": "IoT Hub", "body": f"Notifications are working on this device. Sent {fmt_time(time.time())}.",
           "level": "info", "cat": "problems", "tag": "test", "url": "/", "ts": int(time.time()), "only": ep}
    try:
        push_q.put_nowait(msg)
    except queue.Full:
        return jsonify(error="busy, try again"), 503
    return jsonify(ok=True)


WX_LAT = cfg.get("WEATHER_LAT", "")
WX_LON = cfg.get("WEATHER_LON", "")
WX_PLACE = cfg.get("WEATHER_PLACE", "")
WX_ENABLED = bool(WX_LAT and WX_LON)
WX_EVERY_S = 900
RAIN_ALERTS = cfg.get("RAIN_ALERTS", "yes").lower() in ("1", "yes", "true")
TIPS_ALERTS = cfg.get("TIP_ALERTS", "yes").lower() in ("1", "yes", "true")
wx_state = {"current": None, "daily": [], "updated": None, "error": None}
insight_sent = {}
with db_lock:
    db.execute("CREATE TABLE IF NOT EXISTS weather (ts INTEGER PRIMARY KEY, temp REAL, hum REAL, dew REAL, "
               "precip REAL, prob REAL, code INTEGER, pressure REAL, cloud REAL, wind REAL)")
    db.commit()

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "heavy showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
       99: "thunderstorms with hail"}
RAINY = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _wx_fetch():
    q = urllib.parse.urlencode({
        "latitude": WX_LAT, "longitude": WX_LON, "timezone": "auto", "timeformat": "unixtime",
        "past_days": 2, "forecast_days": 3,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,cloud_cover",
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation,precipitation_probability,"
                  "weather_code,surface_pressure,cloud_cover,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code,sunrise,sunset"})
    with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=20) as r:
        return json.load(r)


def _wx_store(d):
    h = d.get("hourly") or {}
    keys = ["temperature_2m", "relative_humidity_2m", "dew_point_2m", "precipitation", "precipitation_probability",
            "weather_code", "surface_pressure", "cloud_cover", "wind_speed_10m"]
    rows = [(int(t), *[(h.get(k) or [None] * len(h["time"]))[i] for k in keys]) for i, t in enumerate(h.get("time", []))]
    with db_lock:
        db.executemany("INSERT OR REPLACE INTO weather (ts, temp, hum, dew, precip, prob, code, pressure, cloud, wind) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("DELETE FROM weather WHERE ts < ?", (time.time() - 60 * 86400,))
        db.commit()
    c = d.get("current") or {}
    dl = d.get("daily") or {}
    wx_state["current"] = {"ts": c.get("time"), "temp": c.get("temperature_2m"), "hum": c.get("relative_humidity_2m"),
                           "feels": c.get("apparent_temperature"), "precip": c.get("precipitation"),
                           "code": c.get("weather_code"), "desc": WMO.get(c.get("weather_code"), ""),
                           "wind": c.get("wind_speed_10m"), "cloud": c.get("cloud_cover")}
    wx_state["daily"] = [{"day": time.strftime("%Y-%m-%d", time.localtime(t)), "hi": dl["temperature_2m_max"][i],
                          "lo": dl["temperature_2m_min"][i], "rain_mm": dl["precipitation_sum"][i],
                          "rain_prob": (dl.get("precipitation_probability_max") or [None] * 9)[i],
                          "code": dl["weather_code"][i], "desc": WMO.get(dl["weather_code"][i], ""),
                          "sunrise": (dl.get("sunrise") or [None] * 9)[i], "sunset": (dl.get("sunset") or [None] * 9)[i]}
                         for i, t in enumerate(dl.get("time", []))]
    wx_state.update(updated=time.time(), error=None)


def weather_rows(start, end):
    with db_lock:
        return db.execute("SELECT ts, temp, hum, dew, precip, prob, code, pressure, cloud, wind FROM weather "
                          "WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)).fetchall()


def weather_loop():
    while True:
        if WX_ENABLED:
            try:
                _wx_store(_wx_fetch())
            except Exception as e:
                wx_state["error"] = f"{type(e).__name__}: {e}"[:160]
            try:
                check_insights()
            except Exception as e:
                log.warning("insight check failed: %s", e)
        time.sleep(WX_EVERY_S)


def dew_point(t, rh):
    if t is None or rh is None or rh <= 0:
        return None
    a, b = 17.62, 243.12
    g = math.log(rh / 100.0) + a * t / (b + t)
    return b * g / (a - g)


def feels_like(t, rh):
    if t is None or rh is None:
        return None
    if t < 26.7 or rh < 40:
        return t
    f = t * 9 / 5 + 32
    hi = (-42.379 + 2.04901523 * f + 10.14333127 * rh - .22475541 * f * rh - .00683783 * f * f
          - .05481717 * rh * rh + .00122874 * f * f * rh + .00085282 * f * rh * rh - .00000199 * f * f * rh * rh)
    return (hi - 32) * 5 / 9


def comfort(t, rh):
    if t is None or rh is None:
        return None
    dp = dew_point(t, rh)
    if rh < 30:
        return "dry"
    if dp is not None and dp >= 21:
        return "muggy"
    if dp is not None and dp >= 18 or rh >= 70:
        return "humid"
    if t >= 30:
        return "hot"
    if t <= 18:
        return "cool"
    return "comfortable"


def _recent(nid, metric, seconds):
    now = time.time()
    with db_lock:
        rows = db.execute("SELECT ts, value FROM readings WHERE node = ? AND metric = ? AND ts >= ? ORDER BY ts",
                          (nid, metric, now - seconds)).fetchall()
    rows += [(t, v) for t, n, v, m in pending_between(now - seconds, now + 60, metric) if n == nid]
    return sorted(rows)


RAIN_RISE, RAIN_PEAK = 10.0, 85.0
RAIN_MODEL_PATH = os.path.join(DATA_DIR, "rain_model.json")
RAIN_ASK_PER_DAY = 3
RAIN_FEATURES = ["hum", "rise1h", "rise3h", "tchange1h", "spread", "hum_vs_city", "temp_vs_city",
                 "fc_prob", "fc_rain", "pressure3h", "cloud", "daytime"]
RAIN_PRIOR = [2.0, 2.5, 1.0, -1.5, -2.0, 0.5, -0.3, 3.0, 1.0, -0.8, 1.0, 0.0]
RAIN_PRIOR_B = -5.0
RAIN_REF = [0.65, 0, 0, 0, 1.0, 0, 0, 0.1, 0, 0, 0.4, 0.5]
RAIN_LIKE = [0.9, 0.5, 0.4, -0.5, 0.15, 0.2, -0.2, 0.7, 1.0, -0.5, 0.9, 0.5]
with db_lock:
    db.execute("CREATE TABLE IF NOT EXISTS rain_checks (id INTEGER PRIMARY KEY, ts REAL, node TEXT, forecast INTEGER, "
               "fc_desc TEXT, sensor INTEGER, rise REAL, peak REAL, pred REAL, asked REAL, answer TEXT, answered REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS rain_preds (ts REAL, node TEXT, p REAL, fc REAL)")
    db.commit()
rain_state = {"asked": [], "outlook": None, "outlook_at": 0, "error": None}
_rain_m = {"w": None}


def outdoor_nodes():
    with state_lock:
        ids = {k for k in nodes if k != PI_ID}
    return sorted(n for n in ids | set(profiles()) if is_outdoor(n))


def _where(nid):
    n = node_name(nid)
    low = n.lower()
    if re.search(r"\b(balcony|terrace|roof|rooftop|deck|porch|patio|veranda|verandah|sit-out|ledge|sill)\b", low):
        return f"on the {low}"
    if re.search(r"\b(garden|yard|backyard|courtyard|garage|shed|greenhouse|kitchen|bedroom|room|hall|office|lab)\b", low):
        return f"in the {low}"
    return f"at {n}"


def _subj(nid):
    w = _where(nid)
    return "The " + w.split("the ", 1)[1] if " the " in f" {w}" and not w.startswith("at ") else node_name(nid)


def _clock(ts):
    return _human_time(ts, short=True)[3:]


def _ctx(nid, t0, t1):
    with db_lock:
        rows = db.execute("SELECT CAST(ts / 300 AS INTEGER), metric, AVG(value) FROM readings "
                          "WHERE node = ? AND ts >= ? AND ts < ? GROUP BY 1, 2", (nid, t0, t1)).fetchall()
    acc = collections.defaultdict(list)
    for t, n, v, m in pending_between(t0, t1):
        if n == nid:
            acc[(int(t // 300), m)].append(v)
    hum, tmp = {}, {}
    for k, m, v in rows:
        (hum if m == "hum" else tmp)[k] = v
    for (k, m), vs in acc.items():
        (hum if m == "hum" else tmp)[k] = sum(vs) / len(vs)
    wx = {r[0]: r for r in weather_rows(t0 - 4 * 3600, t1 + 4 * 3600)} if WX_ENABLED else {}
    return {"node": nid, "hum": hum, "tmp": tmp, "wx": wx, "wxts": sorted(wx)}


def _wx_at(c, t):
    ts = c.get("wxts")
    if ts is None:
        ts = c["wxts"] = sorted(c["wx"])
    i = bisect.bisect_right(ts, t) - 1
    return c["wx"][ts[i]] if i >= 0 and t - ts[i] < 3600 else None


def _val(d, k, near=2):
    for j in range(near + 1):
        for kk in (k - j, k + j) if j else (k,):
            if kk in d:
                return d[kk]
    return None


def _rain_x(ctx, t):
    k = int(t // 300)
    hd, td = ctx["hum"], ctx["tmp"]
    h, tc = _val(hd, k), _val(td, k)
    if h is None or tc is None:
        return None, None
    p1 = [hd[j] for j in range(k - 12, k) if j in hd]
    p3 = [hd[j] for j in range(k - 36, k) if j in hd]
    t1 = [td[j] for j in range(k - 12, k) if j in td]
    rise1, rise3 = (h - min(p1)) if p1 else 0.0, (h - min(p3)) if p3 else 0.0
    tch = (tc - max(t1)) if t1 else 0.0
    dp = dew_point(tc, h)
    spread = max(0.0, tc - dp) if dp is not None else 5.0
    w = _wx_at(ctx, t) or _wx_at(ctx, t - 3600)
    ahead = [a for a in (_wx_at(ctx, t + 3600 * i) for i in (1, 2)) if a]
    w3 = _wx_at(ctx, t - 3 * 3600)
    raw = {"hum": h, "temp": tc, "rise1h": rise1, "rise3h": rise3, "tchange1h": tch, "spread": spread,
           "city_hum": w[2] if w else None, "city_temp": w[1] if w else None,
           "fc_prob": max((a[5] or 0) for a in ahead) if ahead else None,
           "fc_rain": any(a[6] in RAINY or (a[4] or 0) >= 0.2 for a in ahead) if ahead else False,
           "pressure3h": (w[7] - w3[7]) if w and w3 and w[7] and w3[7] else 0.0,
           "cloud": w[8] if w and w[8] is not None else None}
    lt = time.localtime(t)
    x = [h / 100, rise1 / 20, rise3 / 30, tch / 3, spread / 10,
         (h - raw["city_hum"]) / 20 if raw["city_hum"] is not None else 0.0,
         (tc - raw["city_temp"]) / 5 if raw["city_temp"] is not None else 0.0,
         (raw["fc_prob"] or 0) / 100, 1.0 if raw["fc_rain"] else 0.0, raw["pressure3h"] / 3,
         (raw["cloud"] if raw["cloud"] is not None else 40) / 100, 1.0 if 7 <= lt.tm_hour < 18 else 0.0]
    return x, raw


def _sig(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def _model():
    if _rain_m["w"] is None:
        try:
            with open(RAIN_MODEL_PATH) as f:
                _rain_m.update(json.load(f))
        except (OSError, ValueError):
            _rain_m.update(w=list(RAIN_PRIOR), b=RAIN_PRIOR_B, n=0, pos=0, neg=0, trained=None)
    return _rain_m


def rain_prob(x):
    m = _model()
    return _sig(m["b"] + sum(a * b for a, b in zip(m["w"], x)))


def rain_signature():
    with db_lock:
        rows = db.execute("SELECT rise, peak, answer FROM rain_checks WHERE answer IN ('yes', 'no') "
                          "AND rise IS NOT NULL").fetchall()
    yes, no = [r for r in rows if r[2] == "yes"], [r for r in rows if r[2] == "no"]
    rise, peak = RAIN_RISE, RAIN_PEAK
    if len(yes) >= 3 and len(no) >= 3:
        my, mn = sum(r[0] for r in yes) / len(yes), sum(r[0] for r in no) / len(no)
        if my > mn:
            rise = min(max((my + mn) / 2, 6.0), 25.0)
        py, pn = sum(r[1] for r in yes) / len(yes), sum(r[1] for r in no) / len(no)
        if py > pn:
            peak = min(max((py + pn) / 2, 80.0), 97.0)
    return {"rise": rise, "peak": peak}


def _sensor_rain(ctx, t0, t1, sig):
    eps = []
    hd = ctx["hum"]
    for k in sorted(hd):
        t = k * 300 + 150
        if not (t0 <= t < t1):
            continue
        past = [hd[j] for j in range(k - 12, k) if j in hd]
        if not past:
            continue
        rise, v = hd[k] - min(past), hd[k]
        if rise >= sig["rise"] and v >= sig["peak"]:
            if eps and t - eps[-1]["last"] < 7200:
                eps[-1].update(last=t, rise=max(eps[-1]["rise"], rise), peak=max(eps[-1]["peak"], v))
            elif t - 1800 >= t0:
                eps.append({"ts": t - 1800, "last": t, "rise": rise, "peak": v})
    return eps


def _forecast_rain(wx, t0, t1):
    eps, prev_wet = [], False
    for ts in sorted(wx):
        r = wx[ts]
        wet = (r[4] or 0) >= 0.2 or r[6] in RAINY
        if wet and not prev_wet and t0 <= ts < t1:
            if eps and ts - eps[-1]["ts"] < 2 * 3600:
                prev_wet = wet
                continue
            eps.append({"ts": ts, "desc": WMO.get(r[6], "rain") if r[6] in RAINY else "rain", "prob": r[5]})
        prev_wet = wet
    return eps


def rain_events(t0, t1, ctxs=None):
    outs = outdoor_nodes()
    ctxs = ctxs if ctxs is not None else [_ctx(n, t0 - 4 * 3600, t1) for n in outs]
    wx = ctxs[0]["wx"] if ctxs else ({r[0]: r for r in weather_rows(t0 - 4 * 3600, t1 + 4 * 3600)} if WX_ENABLED else {})
    out = [{"ts": f["ts"], "desc": f["desc"], "forecast": True, "sensor": None, "node": outs[0] if outs else None,
            "answer": None, "id": None} for f in _forecast_rain(wx, t0, t1)]
    sig = rain_signature()
    for c in ctxs:
        for e in _sensor_rain(c, t0, t1, sig):
            m = next((o for o in out if abs(o["ts"] - e["ts"]) <= 5400), None)
            if m:
                m.update(sensor=e, node=c["node"])
            else:
                out.append({"ts": e["ts"], "desc": "rain", "forecast": False, "sensor": e, "node": c["node"],
                            "answer": None, "id": None})
    with db_lock:
        checks = db.execute("SELECT id, ts, answer, node, forecast, sensor, fc_desc FROM rain_checks "
                            "WHERE ts >= ? AND ts < ?", (t0 - 7200, t1 + 7200)).fetchall()
    for c in checks:
        m = next((o for o in out if abs(o["ts"] - c[1]) <= 7200 and o["id"] is None), None)
        if m:
            m.update(id=c[0], answer=c[2])
        elif t0 <= c[1] < t1:
            out.append({"ts": c[1], "desc": c[6] or "rain", "forecast": bool(c[4]), "sensor": None if not c[5] else {},
                        "node": c[3], "answer": c[2], "id": c[0], "pred_only": not c[4] and not c[5]})
    for o in out:
        o["verdict"] = ("confirmed" if o["answer"] == "yes" else "dry" if o["answer"] == "no" else
                        "likely" if (o["forecast"] and o["sensor"] is not None) or o["answer"] == "agreed" else
                        "maybe" if o["sensor"] is not None or (o["forecast"] and outs) or o.get("pred_only") else "forecast")
    return sorted(out, key=lambda o: o["ts"])


def _labels(events):
    pos = [(e["ts"], 3.0 if e["verdict"] == "confirmed" else 1.0) for e in events if e["verdict"] in ("confirmed", "likely")]
    dry = [(e["ts"], 3.0) for e in events if e["verdict"] == "dry"]
    unsure = [e["ts"] for e in events if e["verdict"] in ("maybe", "forecast")]
    return pos, dry, unsure


def _label_at(t, pos, dry, unsure, c):
    for ts, w in pos:
        if t - 1800 <= ts <= t + 7200:
            return 1, w
    for ts, w in dry:
        if t - 1800 <= ts <= t + 7200:
            return 0, w
    if any(abs(ts - t) <= 3 * 3600 for ts, _ in pos) or any(t - 3600 <= ts <= t + 3 * 3600 for ts in unsure):
        return None, 0
    if any(((_wx_at(c, t + 3600 * i) or (0,) * 7)[4] or 0) >= 0.1 for i in (0, 1, 2)):
        return None, 0
    return 0, 0.5


def rain_train(days=60):
    outs = outdoor_nodes()
    if not outs:
        return None
    now = time.time()
    t0, t1 = now - days * 86400, now - 2.5 * 3600
    ctxs = [_ctx(n, t0 - 4 * 3600, now) for n in outs]
    pos, dry, unsure = _labels(rain_events(t0, now, ctxs))
    X, Y, W = [], [], []
    for c in ctxs:
        t = t0
        while t < t1:
            y, w = _label_at(t, pos, dry, unsure, c)
            if y is not None:
                x, _raw = _rain_x(c, t)
                if x:
                    X.append(x), Y.append(y), W.append(w)
            t += 1800
    npos = sum(w for y, w in zip(Y, W) if y)
    nneg = sum(w for y, w in zip(Y, W) if not y)
    w_, b_ = list(RAIN_PRIOR), RAIN_PRIOR_B
    if npos >= 1 and nneg >= 5:
        cw = {1: (npos + nneg) / (2 * npos), 0: (npos + nneg) / (2 * nneg)}
        W = [w * cw[y] for y, w in zip(Y, W)]
        tot = sum(W)
        lam = 6.0 / (npos + 6.0)
        lr = 0.5
        for _ in range(250):
            gw, gb = [0.0] * len(w_), 0.0
            for x, y, wt in zip(X, Y, W):
                e = (_sig(b_ + sum(a * b for a, b in zip(w_, x))) - y) * wt
                gb += e
                for i, xi in enumerate(x):
                    gw[i] += e * xi
            w_ = [wi - lr * (g / tot + lam * (wi - pi)) for wi, g, pi in zip(w_, gw, RAIN_PRIOR)]
            b_ -= lr * (gb / tot + lam * (b_ - RAIN_PRIOR_B))
    m = {"w": [round(v, 4) for v in w_], "b": round(b_, 4), "n": len(X), "pos": round(npos, 1), "neg": round(nneg, 1),
         "trained": now, "features": RAIN_FEATURES}
    tmp = RAIN_MODEL_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f)
    os.replace(tmp, RAIN_MODEL_PATH)
    _rain_m.update(m)
    return m


def rain_score(days=30):
    now = time.time()
    with db_lock:
        preds = db.execute("SELECT ts, p, fc FROM rain_preds WHERE ts >= ? AND ts < ? ORDER BY ts",
                           (now - days * 86400, now - 2.5 * 3600)).fetchall()
    if not preds:
        return None
    pos, dry, unsure = _labels(rain_events(now - days * 86400, now))
    wxc = {"wx": {r[0]: r for r in weather_rows(now - days * 86400, now)} if WX_ENABLED else {}}
    calls = {"me": [0, 0], "fc": [0, 0]}
    rain_hits = {"me": 0, "fc": 0, "n": 0, "me_false": 0, "fc_false": 0}
    seen = set()
    for ts, p, fc in preds:
        slot = int(ts // 3600)
        if slot in seen:
            continue
        seen.add(slot)
        y, _w = _label_at(ts, pos, dry, unsure, wxc)
        if y is None:
            continue
        for k, guess in (("me", p >= 0.5), ("fc", (fc or 0) >= 50)):
            calls[k][0] += int(guess == bool(y))
            calls[k][1] += 1
        if y:
            rain_hits["n"] += 1
            rain_hits["me"] += int(p >= 0.5)
            rain_hits["fc"] += int((fc or 0) >= 50)
        else:
            rain_hits["me_false"] += int(p >= 0.5)
            rain_hits["fc_false"] += int((fc or 0) >= 50)
    if not calls["me"][1]:
        return None
    return {"hours": calls["me"][1], "right": calls["me"][0], "fc_right": calls["fc"][0],
            "rains": rain_hits["n"], "caught": rain_hits["me"], "fc_caught": rain_hits["fc"],
            "false_alarms": rain_hits["me_false"], "fc_false_alarms": rain_hits["fc_false"]}


REASON = {
    "hum": lambda r: f"humidity is high ({r['hum']:.0f}%)",
    "rise1h": lambda r: f"humidity jumped {r['rise1h']:.0f}% in the last hour",
    "rise3h": lambda r: f"humidity is up {r['rise3h']:.0f}% over three hours",
    "tchange1h": lambda r: f"it cooled {abs(r['tchange1h']):.1f}° in the last hour",
    "spread": lambda r: f"the air is close to saturation (dew point only {r['spread']:.1f}° below)",
    "hum_vs_city": lambda r: f"it's {r['hum'] - (r['city_hum'] or 0):.0f}% more humid than the city reading",
    "temp_vs_city": lambda r: f"it's {(r['city_temp'] or 0) - r['temp']:.0f}° cooler than the city reading",
    "fc_prob": lambda r: f"the forecast gives {r['fc_prob'] or 0:.0f}%",
    "fc_rain": lambda r: "the forecast shows rain",
    "pressure3h": lambda r: f"air pressure fell {abs(r['pressure3h']):.1f} hPa in three hours",
    "cloud": lambda r: f"it's {r['cloud'] or 0:.0f}% cloudy",
}


AGAINST = {
    "hum": lambda r: f"humidity is only {r['hum']:.0f}%",
    "rise1h": lambda r: "humidity isn't climbing",
    "rise3h": lambda r: "humidity has been falling",
    "tchange1h": lambda r: "it's warming, not cooling",
    "spread": lambda r: f"the air is far from saturation (dew point {r['spread']:.0f}° below)",
    "hum_vs_city": lambda r: "it's drier here than the city reading",
    "fc_prob": lambda r: f"the forecast gives only {r['fc_prob'] or 0:.0f}%",
    "fc_rain": lambda r: "no rain in the forecast for the next two hours",
    "pressure3h": lambda r: "air pressure is steady or rising",
    "cloud": lambda r: f"skies are fairly clear ({r['cloud'] or 0:.0f}% cloud)",
}


def rain_outlook(force=False):
    now = time.time()
    if not force and rain_state["outlook"] is not None and now - rain_state["outlook_at"] < 300:
        return rain_state["outlook"]
    outs = outdoor_nodes()
    res = None
    for nid in outs:
        c = _ctx(nid, now - 4 * 3600, now + 60)
        x, raw = _rain_x(c, now)
        if not x or now - max(c["hum"] or [0]) * 300 > 1800:
            continue
        m = _model()
        p = rain_prob(x)
        contrib = sorted(((m["w"][i] * (x[i] - RAIN_REF[i]), RAIN_FEATURES[i]) for i in range(len(x))), reverse=True)
        why = [REASON[f](raw) for v, f in contrib if v > 0.35 and f in REASON][:3]
        against = [f for v, f in contrib if v < -0.5]
        gap = sorted((m["w"][i] * (x[i] - RAIN_LIKE[i]), RAIN_FEATURES[i]) for i in range(len(x)))
        why_not = [AGAINST[f](raw) for v, f in gap if v < -0.6 and f in AGAINST][:3]
        res = {"node": nid, "name": node_name(nid), "where": _where(nid), "p": round(p, 3),
               "forecast": raw["fc_prob"], "why": why, "raw": {k: (round(v, 1) if isinstance(v, float) else v)
                                                              for k, v in raw.items()},
               "dry_air": "spread" in against or "hum" in against, "why_not": why_not, "trained": m.get("trained"),
               "examples": m.get("n", 0), "rain_examples": m.get("pos", 0)}
        break
    rain_state.update(outlook=res, outlook_at=now)
    return res


def _rain_sig(cid):
    return hmac.new(app.secret_key, f"rain:{cid}".encode(), "sha256").hexdigest()[:24]


def _rain_question(row):
    cid, ts, nid, fc, fc_desc, sensor, rise, peak, pred = row[:9]
    where = _where(nid) if nid else "where you are"
    q = f"Did it rain {where} around {_clock(ts)}{'' if day_of(ts) == day_of(time.time()) else ' yesterday'}?"
    if fc and sensor:
        why = f"The weather service had {fc_desc}, and humidity jumped {rise:.0f}% to {peak:.0f}%."
    elif fc:
        why = f"The weather service had {fc_desc}, but the sensor barely noticed" + (f" (humidity up {rise:.0f}%)." if rise else ".")
    elif sensor:
        why = f"The forecast didn't expect rain, but humidity jumped {rise:.0f}% to {peak:.0f}%."
    else:
        pc = round((pred or 0) * 100)
        why = f"I gave it {'an' if str(pc).startswith(('8', '11', '18')) else 'a'} {pc}% chance from the graph, but nothing else confirmed it."
    return q, why


def _rain_thanks(answer):
    s = rain_score() or {}
    with db_lock:
        n = db.execute("SELECT COUNT(*) FROM rain_checks WHERE answer IN ('yes', 'no')").fetchone()[0]
    msg = {"yes": "Thanks! Noted that it rained.", "no": "Thanks! Noted that it stayed dry.",
           "unsure": "No problem, I'll leave that one out."}[answer]
    if answer != "unsure":
        msg += f" That's {n} answer{'s' if n != 1 else ''} so far; I retrain on them every few hours."
    if s.get("rains"):
        msg += (f" Last 30 days I caught {s['caught']} of {s['rains']} rains with {s['false_alarms']} false alarm"
                f"{'s' if s['false_alarms'] != 1 else ''} (the forecast alone: {s['fc_false_alarms']}).")
    return msg


def rain_check_tick():
    outs = outdoor_nodes()
    if not outs:
        return
    now = time.time()
    lt = time.localtime(now)
    t0, t1 = now - 14 * 3600, now - 5400
    evs = [e for e in rain_events(t0, t1) if e["id"] is None]
    with db_lock:
        preds = db.execute("SELECT ts, node, p FROM rain_preds WHERE ts >= ? AND ts < ? AND p >= 0.6 ORDER BY ts",
                           (t0, t1 - 7200)).fetchall()
        answered = db.execute("SELECT COUNT(*) FROM rain_checks WHERE answer IN ('yes', 'no')").fetchone()[0]
    all_evs = rain_events(t0 - 3 * 3600, now)
    for ts, nid, p in preds:
        if not any(abs(e["ts"] - ts) <= 3 * 3600 for e in all_evs) and \
                not any(abs(e["ts"] - ts) <= 3 * 3600 for e in evs):
            evs.append({"ts": ts + 3600, "desc": "rain", "forecast": False, "sensor": None, "node": nid,
                        "pred_only": True, "pred": p})
    rain_state["asked"] = [t for t in rain_state["asked"] if now - t < 86400]
    for e in sorted(evs, key=lambda e: e["ts"]):
        nid = e.get("node") or outs[0]
        c = _ctx(nid, e["ts"] - 2 * 3600, min(now, e["ts"] + 2 * 3600))
        hd = c["hum"]
        win = [k for k in sorted(hd) if e["ts"] - 1800 <= k * 300 <= e["ts"] + 7200]
        rise = max((hd[k] - min([hd[j] for j in range(k - 12, k) if j in hd] or [hd[k]]) for k in win), default=None)
        peak = max((hd[k] for k in win), default=None)
        if rise is None:
            continue
        x, _raw = _rain_x(c, e["ts"] - 3600)
        pred = e.get("pred") or (rain_prob(x) if x else None)
        sensor = e["sensor"] is not None
        agree = e["forecast"] and sensor
        ask = not agree or answered < 6
        if 7 <= lt.tm_hour < 22 or not ask:
            with db_lock:
                cur = db.execute("INSERT INTO rain_checks (ts, node, forecast, fc_desc, sensor, rise, peak, pred, asked, answer) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (e["ts"], nid, int(e["forecast"]), e["desc"], int(sensor), round(rise, 1), round(peak, 1),
                                  round(pred, 3) if pred is not None else None, now if ask else None,
                                  None if ask else "agreed"))
                cid = cur.lastrowid
                db.commit()
            if ask and len(rain_state["asked"]) < RAIN_ASK_PER_DAY and RAIN_ALERTS:
                rain_state["asked"].append(now)
                q, why = _rain_question((cid, e["ts"], nid, int(e["forecast"]), e["desc"], int(sensor), rise, peak, pred))
                base = _public_url()
                acts = None
                if base:
                    u = f"{base}/api/rain/{cid}/answer?sig={_rain_sig(cid)}&a="
                    acts = (f"http, Yes it rained, {u}yes, method=POST, clear=true; "
                            f"http, No it stayed dry, {u}no, method=POST, clear=true; view, Not sure, {base}/#overview")
                notify("Quick question", f"{q} {why} Your answer teaches the rain predictor."
                       + ("" if acts else " Answer on the dashboard."), "info", "insight", actions=acts,
                       push={"cat": "rain", "tag": f"rainq-{cid}", "url": "/#overview",
                             "actions": [{"action": "yes", "title": "Yes, it rained"}, {"action": "no", "title": "No, stayed dry"}],
                             "answer": {"id": cid, "sig": _rain_sig(cid)}})


def rain_loop():
    last_train = 0
    time.sleep(60)
    while True:
        try:
            if outdoor_nodes():
                o = rain_outlook(force=True)
                if o:
                    with db_lock:
                        db.execute("INSERT INTO rain_preds (ts, node, p, fc) VALUES (?,?,?,?)",
                                   (time.time(), o["node"], o["p"], o["forecast"]))
                        db.execute("DELETE FROM rain_preds WHERE ts < ?", (time.time() - 120 * 86400,))
                        db.commit()
                rain_check_tick()
                if time.time() - last_train > 6 * 3600:
                    rain_train()
                    last_train = time.time()
            rain_state["error"] = None
        except Exception as e:
            rain_state["error"] = f"{type(e).__name__}: {e}"[:200]
            log.warning("rain check failed: %s", e)
        time.sleep(900)


def _vs_forecast(rows, wx):
    if len(rows) < 30 or not wx:
        return None
    diffs = []
    for r in wx:
        if r[1] is None:
            continue
        near = [v for t, v in rows if abs(t - r[0]) <= 1800]
        if len(near) >= 3:
            diffs.append((r[0], sum(near) / len(near) - r[1]))
    if len(diffs) < 4:
        return None
    spans = []
    for ts, d in diffs:
        if 7 <= time.localtime(ts).tm_hour < 18 and d >= 4:
            if spans and ts - spans[-1][1] <= 3600:
                spans[-1] = (spans[-1][0], ts, max(spans[-1][2], d))
            else:
                spans.append((ts, ts, d))
    spans = [(a - 1800, b + 1800, m) for a, b, m in spans]
    rest = [d for ts, d in diffs if not any(a <= ts <= b for a, b, _ in spans)]
    return {"sun": spans, "vs_forecast": round(sum(rest) / len(rest), 1) if len(rest) >= 3 else None}


with db_lock:
    db.execute("CREATE TABLE IF NOT EXISTS temp_preds (day TEXT, node TEXT, kind TEXT, pred REAL, base REAL, made REAL, "
               "PRIMARY KEY (day, node, kind))")
    db.execute("CREATE TABLE IF NOT EXISTS disk_hist (day TEXT PRIMARY KEY, used REAL, total REAL)")
    db.commit()
_outl = {"at": 0, "data": None}


def _q15(nid, metric, t0, t1):
    with db_lock:
        rows = db.execute("SELECT CAST(ts / 900 AS INTEGER), AVG(value) FROM readings WHERE node = ? AND metric = ? "
                          "AND ts >= ? AND ts < ? GROUP BY 1", (nid, metric, t0, t1)).fetchall()
    return dict(rows)


def _near(q, ts):
    vals = [q[k] for k in range(int((ts - 1800) // 900), int((ts + 1800) // 900) + 1) if k in q]
    return sum(vals) / len(vals) if vals else None


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


def _sunny(r):
    h = time.localtime(r[0]).tm_hour
    return 8 <= h < 17 and (r[8] if r[8] is not None else 50) < 50


def _offsets(nid, days=14):
    now = time.time()
    wx = weather_rows(now - days * 86400, now)
    qt, qh = _q15(nid, "temp", now - days * 86400 - 3600, now), _q15(nid, "hum", now - days * 86400 - 3600, now)
    t_by, h_by, t_all, h_all = collections.defaultdict(list), collections.defaultdict(list), [], []
    for r in wx:
        hr = time.localtime(r[0]).tm_hour
        v = _near(qt, r[0])
        if v is not None and r[1] is not None:
            t_by[(hr, _sunny(r))].append(v - r[1])
            t_by[(hr, None)].append(v - r[1])
            t_all.append(v - r[1])
        v = _near(qh, r[0])
        if v is not None and r[2] is not None:
            h_by[hr].append(v - r[2])
            h_all.append(v - r[2])
    if len(t_all) < 24:
        return None
    return {"t": {k: _median(v) for k, v in t_by.items() if len(v) >= 3}, "t_all": _median(t_all),
            "h": {k: _median(v) for k, v in h_by.items() if len(v) >= 3}, "h_all": _median(h_all) if h_all else 0.0,
            "hours": len(t_all)}


def _next_9am(ts):
    lt = time.localtime(ts)
    nine = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))
    return nine if nine > ts + 3600 else nine + 86400


def _outdoor_outlook(nid, now):
    off = _offsets(nid)
    if not off:
        return None
    fut = weather_rows(now - 1800, now + 36 * 3600)
    series = []
    for r in fut:
        if r[1] is None:
            continue
        hr = time.localtime(r[0]).tm_hour
        o = off["t"].get((hr, _sunny(r)), off["t"].get((hr, None), off["t_all"]))
        ho = off["h"].get(hr, off["h_all"])
        series.append({"ts": r[0], "pred": round(r[1] + o, 1), "fc": r[1],
                       "hum": round(min(100.0, max(0.0, (r[2] or 0) + ho)), 0) if r[2] is not None else None,
                       "dew": r[3], "sun": _sunny(r) and o >= 3})
    if not series:
        return None
    morning = _next_9am(now)
    night = [p for p in series if now <= p["ts"] <= morning]
    tmr_day = day_of(morning)
    t0, t1 = day_bounds(tmr_day)
    tday = [p for p in series if t0 <= p["ts"] < t1]
    daytime = [p for p in tday if 9 <= time.localtime(p["ts"]).tm_hour < 18]
    out = {"node": nid, "name": node_name(nid), "where": _where(nid), "place": "outdoor", "learned_hours": off["hours"],
           "series": [[p["ts"], p["pred"], p["fc"]] for p in series if p["ts"] <= now + 24 * 3600]}
    if night:
        lo = min(night, key=lambda p: p["pred"])
        out["tonight"] = {"lo": lo["pred"], "lo_t": lo["ts"], "fc_lo": min(p["fc"] for p in night)}
        dew = next((p for p in night if (p["hum"] or 0) >= 94), None)
        if dew:
            out["dew"] = {"ts": dew["ts"], "hum": dew["hum"]}
    if daytime:
        hi = max(daytime, key=lambda p: p["pred"])
        out["tomorrow"] = {"day": tmr_day, "hi": hi["pred"], "hi_t": hi["ts"], "fc_hi": max(p["fc"] for p in daytime),
                           "sun": hi["sun"]}
        if len(tday) >= 20:
            out["tomorrow"].update(lo=min(p["pred"] for p in tday), fc_lo=min(p["fc"] for p in tday))
    return out


def _wx_daily(days=21):
    now = time.time()
    by = collections.defaultdict(list)
    for r in weather_rows(now - days * 86400, now + 3 * 86400):
        if r[1] is not None:
            by[day_of(r[0])].append(r[1])
    return {d: (min(v), max(v)) for d, v in by.items() if len(v) >= 20}


def _fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx > 1.0 else 0.0
    b = min(max(b, 0.0), 1.2)
    return my - b * mx, b


def _indoor_outlook(nid, now):
    wxd = _wx_daily()
    today = day_of(now)
    with db_lock:
        rows = db.execute("SELECT day, min, max FROM daily WHERE node = ? AND metric = 'temp' AND day >= ? AND day < ?",
                          (nid, day_of(now - 21 * 86400), today)).fetchall()
    pairs = [(wxd[d], (lo, hi)) for d, lo, hi in rows if d in wxd]
    tmr = day_of(_next_9am(now))
    if len(pairs) < 5 or tmr not in wxd:
        return None
    a_hi, b_hi = _fit([p[0][1] for p in pairs], [p[1][1] for p in pairs])
    a_lo, b_lo = _fit([p[0][0] for p in pairs], [p[1][0] for p in pairs])
    o_lo, o_hi = wxd[tmr]
    last = max(rows)
    return {"node": nid, "name": node_name(nid), "where": _where(nid), "place": "indoor", "learned_days": len(pairs),
            "tomorrow": {"day": tmr, "lo": round(a_lo + b_lo * o_lo, 1), "hi": round(a_hi + b_hi * o_hi, 1),
                         "base_lo": last[1], "base_hi": last[2], "follows": round(b_hi, 2)}}


def _disk_outlook():
    with db_lock:
        rows = db.execute("SELECT day, used, total FROM disk_hist ORDER BY day").fetchall()[-60:]
    if len(rows) < 7:
        return {"days_of_data": len(rows)}
    t = [time.mktime(time.strptime(d, "%Y-%m-%d")) / 86400 for d, _u, _t in rows]
    a, b = _fit_free(t, [u for _d, u, _t in rows])
    used, total = rows[-1][1], rows[-1][2]
    out = {"used_gb": used, "total_gb": total, "gb_per_month": round(b * 30, 2), "days_of_data": len(rows)}
    limit = total * DISK_FULL_PCT / 100
    if b > 0.002 and used < limit:
        out["full_in_days"] = int((limit - used) / b)
    return out


def _fit_free(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs) or 1.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx
    return my - b * mx, b


def temp_score(days=30):
    since = day_of(time.time() - days * 86400)
    with db_lock:
        rows = db.execute("SELECT p.node, p.kind, p.pred, p.base, d.min, d.max FROM temp_preds p JOIN daily d "
                          "ON d.day = p.day AND d.node = p.node AND d.metric = 'temp' WHERE p.day >= ? AND p.day < ?",
                          (since, day_of(time.time()))).fetchall()
    out = {}
    for nid, kind, pred, base, lo, hi in rows:
        real = hi if kind == "hi" else lo
        e = out.setdefault(nid, {"n": 0, "err": 0.0, "base_err": 0.0, "base_n": 0})
        e["n"] += 1
        e["err"] += abs(pred - real)
        if base is not None:
            e["base_n"] += 1
            e["base_err"] += abs(base - real)
    return {n: {"calls": e["n"], "off_by": round(e["err"] / e["n"], 1),
                "base_off_by": round(e["base_err"] / e["base_n"], 1) if e["base_n"] else None,
                "base": "forecast" if is_outdoor(n) else "same as the day before"} for n, e in out.items()}


def outlook(force=False):
    now = time.time()
    if not force and _outl["data"] is not None and now - _outl["at"] < 600:
        return _outl["data"]
    with state_lock:
        ids = sorted(k for k in nodes if k != PI_ID)
    res = []
    for nid in ids:
        try:
            o = (_outdoor_outlook if is_outdoor(nid) else _indoor_outlook)(nid, now) if WX_ENABLED else None
        except (sqlite3.Error, ValueError, ZeroDivisionError) as e:
            log.warning("outlook for %s failed: %s", nid, e)
            o = None
        if o:
            res.append(o)
    data = {"nodes": res, "score": temp_score(), "disk": _disk_outlook(), "generated": now}
    _outl.update(at=now, data=data)
    return data


def predict_loop():
    time.sleep(90)
    while True:
        try:
            now = time.time()
            data = outlook(force=True)
            if time.localtime(now).tm_hour >= 18:
                with db_lock:
                    for o in data["nodes"]:
                        tm = o.get("tomorrow") or {}
                        for kind in ("lo", "hi"):
                            if tm.get(kind) is None:
                                continue
                            base = tm.get("fc_" + kind) if o["place"] == "outdoor" else tm.get("base_" + kind)
                            db.execute("INSERT OR IGNORE INTO temp_preds VALUES (?,?,?,?,?,?)",
                                       (tm["day"], o["node"], kind, tm[kind], base, now))
                    db.commit()
            with state_lock:
                pi = dict((nodes.get(PI_ID) or {}).get("info") or {})
            if pi.get("disk_used_gb") is not None:
                with db_lock:
                    db.execute("INSERT OR REPLACE INTO disk_hist VALUES (?,?,?)",
                               (day_of(now), pi["disk_used_gb"], pi["disk_total_gb"]))
                    db.commit()
        except Exception as e:
            log.warning("prediction refresh failed: %s", e)
        time.sleep(1800)


@app.get("/api/outlook")
def api_outlook():
    return jsonify(outlook())


def live_insights():
    out = []
    now = time.time()
    cur = wx_state.get("current") or {}
    upcoming = weather_rows(now - 3600, now + 4 * 3600) if WX_ENABLED else []
    rain_next = next(((ts, prob, code) for ts, _t, _h, _d, pr, prob, code, *_r in upcoming
                      if ts >= now - 1800 and ((pr or 0) >= 0.3 or (code in RAINY and (prob or 0) >= 50))), None)
    dry_ahead = bool(upcoming) and all((pr or 0) < 0.2 and (prob or 0) < 35 and code not in RAINY
                                       for ts, _t, _h, _d, pr, prob, code, *_r in upcoming if ts >= now)
    daytime = 8 <= time.localtime(now).tm_hour < 17
    with state_lock:
        snap = {k: (v.get("temp"), v.get("hum"), v.get("status")) for k, v in nodes.items() if k != PI_ID}
    has_out = any(is_outdoor(k) for k in snap)
    look = rain_outlook() if has_out else None
    sig = rain_signature() if has_out else None
    for nid, (t, h, st) in snap.items():
        if st != "online" or t is None:
            continue
        who = node_name(nid)
        hum = _recent(nid, "hum", 7200)
        if is_outdoor(nid):
            where, subj = _where(nid), _subj(nid)
            hb = _buckets(hum)
            raining = False
            if len(hb) >= 6 and now - hb[-1][0] < 1200:
                past = [v for t2, v in hb if hb[-1][0] - t2 <= 3600]
                rise = hb[-1][1] - min(past)
                if rise >= sig["rise"] and hb[-1][1] >= sig["peak"]:
                    raining = True
                    agree = ("The forecast agrees." if cur.get("code") in RAINY or (rain_next and rain_next[0] <= now + 1800)
                             else "The forecast didn't see this coming.")
                    out.append({"key": f"rainnow:{nid}", "level": "info", "icon": "rain",
                                "text": f"Looks like it's raining {where}: humidity jumped {rise:.0f}% to {hb[-1][1]:.0f}% "
                                        f"in the last hour. {agree} Bring in anything that shouldn't get wet."})
            if look and look["node"] == nid and not raining and look["p"] >= 0.6:
                why = (" Why: " + "; ".join(look["why"]) + ".") if look["why"] else ""
                out.append({"key": f"rain:{nid}", "level": "info", "icon": "rain",
                            "text": f"Rain likely {where} in the next two hours ({round(look['p'] * 100)}%).{why}"
                                    + (" Good time to bring the washing in." if daytime else "")})
            if daytime and dry_ahead and h is not None and h <= 60 and t >= 24 and not raining:
                out.append({"key": f"drying:{nid}", "level": "tip", "icon": "sun",
                            "text": f"Good drying weather {where}: {t:.0f}°, {h:.0f}% humidity and no rain expected "
                                    f"for the next few hours."})
            if daytime and cur.get("temp") is not None and t - cur["temp"] >= 5:
                out.append({"key": f"sun:{nid}", "level": "info", "icon": "sun",
                            "text": f"{subj} sensor is probably in direct sun: it reads {t:.0f}°, about "
                                    f"{t - cur['temp']:.0f}° above the air temperature in {WX_PLACE or 'your area'}."})
            ol = next((o for o in outlook()["nodes"] if o["node"] == nid), None) if WX_ENABLED else None
            if ol and ol.get("dew") and time.localtime(now).tm_hour >= 18 and ol["dew"]["ts"] - now <= 12 * 3600:
                out.append({"key": f"dew:{nid}", "level": "tip", "icon": "drop",
                            "text": f"Dew likely {where} around {_clock(ol['dew']['ts'])}"
                                    + (f" (humidity heading to {ol['dew']['hum']:.0f}%)" if ol["dew"].get("hum") else "")
                                    + ". Bring in cushions, or cover anything that shouldn't get damp."})
            f = feels_like(t, h)
            if f is not None and f >= 38:
                out.append({"key": f"heat:{nid}", "level": "warn", "icon": "heat",
                            "text": f"It's very hot {where} (feels like {f:.0f}°). Plants and anything heat-sensitive "
                                    f"out there may need shade or water."})
            continue
        if len(hum) >= 20:
            first = sum(v for _, v in hum[:10]) / 10
            last = sum(v for _, v in hum[-10:]) / 10
            if last - first >= 6 and rain_next:
                when = "now" if rain_next[0] <= now + 900 else f"around {_say_time(rain_next[0])}"
                out.append({"key": f"rain:{nid}", "level": "info", "icon": "rain",
                            "text": f"Rain is likely {when}. {who}'s humidity has climbed {last - first:.0f}% in the "
                                    f"last two hours, and the forecast agrees."})
        if h is not None and h >= 70:
            long = _recent(nid, "hum", 6 * 3600)
            if len(long) > 50 and min(v for _, v in long) >= 68:
                out.append({"key": f"mould:{nid}", "level": "warn", "icon": "drop",
                            "text": f"{who} has been above 68% humidity for six hours. Damp air like this can lead to "
                                    f"mould; airing the room or a dehumidifier would help."})
        if cur.get("temp") is not None and t is not None:
            diff = t - cur["temp"]
            if diff >= 3 and t >= 27 and (cur.get("code") not in RAINY) and (cur.get("hum") or 100) < 85:
                out.append({"key": f"vent:{nid}", "level": "tip", "icon": "wind",
                            "text": f"It's {cur['temp']:.0f}° outside but {t:.0f}° in {who}. Opening a window "
                                    f"would cool things down."})
        f = feels_like(t, h)
        if f is not None and f >= 32:
            out.append({"key": f"heat:{nid}", "level": "warn", "icon": "heat",
                        "text": f"It feels like {f:.0f}° in {who} with the humidity. Worth keeping an eye on "
                                f"anything heat-sensitive."})
    vents = [o for o in out if o["key"].startswith("vent:")]
    if len(vents) > 1:
        names = [node_name(o["key"].split(":", 1)[1]) for o in vents]
        out = [o for o in out if not o["key"].startswith("vent:")]
        out.append({"key": "vent:all", "level": "tip", "icon": "wind",
                    "text": f"It's {cur['temp']:.0f}° outside, cooler than {', '.join(names[:-1])} and {names[-1]}. "
                            f"Opening a window would cool things down."})
    if rain_next and not any(o["icon"] == "rain" for o in out):
        out.append({"key": "rain:forecast", "level": "info", "icon": "rain",
                    "text": f"{_cap(WMO.get(rain_next[2], 'rain'))} expected around {_say_time(rain_next[0])}"
                            + (f" ({rain_next[1]:.0f}% chance)." if rain_next[1] is not None else ".")
                            + (f" {_subj(look['node'])} graph isn't showing it yet ({round(look['p'] * 100)}%)."
                               if look and look["p"] < 0.3 else "")})
    return out


def check_insights():
    now = time.time()
    for ins in live_insights():
        kind = ins["key"].split(":")[0]
        if kind in ("rain", "rainnow") and not RAIN_ALERTS or kind in ("vent", "mould", "heat", "dew") and not TIPS_ALERTS:
            continue
        if ins["key"] == "rain:forecast" or kind in ("drying", "sun"):
            continue
        if now - insight_sent.get(ins["key"], 0) < (3 if kind == "rainnow" else 6) * 3600:
            continue
        if kind == "rain" and now - insight_sent.get(ins["key"].replace("rain:", "rainnow:"), 0) < 3 * 3600:
            continue
        insight_sent[ins["key"]] = now
        notify({"rain": "Rain on the way", "rainnow": "Raining now", "mould": "Damp air", "vent": "Cooler outside",
                "heat": "Feels hot", "dew": "Dew tonight"}.get(kind, "Tip"), ins["text"], "info", "insight",
               push={"cat": "rain" if kind in ("rain", "rainnow") else "tips", "tag": ins["key"], "url": "/#overview"})


@app.get("/api/rain")
def api_rain():
    now = time.time()
    with db_lock:
        rows = db.execute("SELECT id, ts, node, forecast, fc_desc, sensor, rise, peak, pred, answer FROM rain_checks "
                          "WHERE ts >= ? ORDER BY ts DESC", (now - 14 * 86400,)).fetchall()
    open_q, recent = [], []
    for r in rows:
        q, why = _rain_question(r)
        item = {"id": r[0], "ts": r[1], "node": r[2], "question": q, "why": why, "answer": r[9],
                "forecast": bool(r[3]), "sensor": bool(r[5]), "pred": r[8]}
        if r[9] is None and now - r[1] < 2 * 86400:
            open_q.append(item)
        elif r[9]:
            recent.append(item)
    m = _model()
    return jsonify({"enabled": bool(outdoor_nodes()), "outlook": rain_outlook(), "open": open_q[:5], "recent": recent[:10],
                    "model": {"trained": m.get("trained"), "examples": m.get("n", 0), "rain_examples": m.get("pos", 0),
                              "dry_examples": m.get("neg", 0),
                              "weights": dict(zip(RAIN_FEATURES, m["w"])), "signature": rain_signature()},
                    "score": rain_score(), "error": rain_state["error"]})


@app.post("/api/rain/<int:cid>/answer")
def api_rain_answer(cid):
    body = request.get_json(silent=True) or {}
    a = str(body.get("answer") or request.args.get("a") or "").lower()
    if a not in ("yes", "no", "unsure"):
        return jsonify(error="answer must be yes, no or unsure"), 400
    with db_lock:
        n = db.execute("UPDATE rain_checks SET answer = ?, answered = ? WHERE id = ?", (a, time.time(), cid)).rowcount
        db.commit()
    if not n:
        return jsonify(error="no such question"), 404
    rain_state["outlook_at"] = 0
    return jsonify(ok=True, thanks=_rain_thanks(a))


@app.post("/api/rain/retrain")
def api_rain_retrain():
    m = rain_train()
    return jsonify(ok=bool(m), model={k: m[k] for k in ("n", "pos", "neg", "trained")} if m else None)


@app.get("/api/weather")
def api_weather():
    now = time.time()
    hours = arg_num("hours", 24, 1, 24 * 14)
    rows = weather_rows(now - hours * 3600, now + 48 * 3600) if WX_ENABLED else []
    return jsonify({"enabled": WX_ENABLED, "place": WX_PLACE or None, **wx_state,
                    "hourly": [{"ts": r[0], "temp": r[1], "hum": r[2], "precip": r[4], "prob": r[5], "code": r[6],
                                "desc": WMO.get(r[6], "")} for r in rows],
                    "insights": live_insights()})


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


_pub = {"url": None, "at": 0}


def _public_url():
    if PUBLIC_URL:
        return PUBLIC_URL
    if time.time() - _pub["at"] > 600:
        m = re.search(r"https://[^\s]+", _run(["tailscale", "funnel", "status"]) or "")
        _pub.update(url=m.group(0).rstrip("/") if m else None, at=time.time())
    return _pub["url"]


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
        "public_url": _public_url(),
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


@app.post("/api/nodes/<nid>/profile")
def api_profile(nid):
    if not ID_RE.match(nid) or nid == PI_ID:
        return jsonify(error="bad node id"), 400
    body = request.get_json(silent=True) or {}
    name = re.sub(r"\s+", " ", str(body.get("name", ""))).strip()
    place = "outdoor" if body.get("place") == "outdoor" else "indoor"
    if name and not PROFILE_NAME.match(name):
        return jsonify(error="Use up to 24 letters, numbers and spaces"), 400
    try:
        save_profile(nid, name, place)
    except OSError as e:
        return jsonify(error=f"could not save: {e}"), 500
    log_event(nid, "start", "info", f"Named {name or nid}, {place}")
    return jsonify(ok=True, label=node_name(nid), place=place)


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
               report_loop, outage_loop, gh_loop, speak_loop, summary_loop,
               weather_loop, rain_loop, push_loop, predict_loop):
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
