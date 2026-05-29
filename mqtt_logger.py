#!/usr/bin/env python3
"""
SIT210 Smart Plant — MQTT subscriber + SQLite logger (Pi side).

Subscribes to:
  plant/sensors  - sensor readings (JSON)
  plant/events   - watering / alert events
  plant/status   - online / offline (LWT)

Writes everything to SQLite at ~/smart_plant/plant.db

Run:
  python3 mqtt_logger.py

Stop:
  Ctrl+C  (graceful)
"""

import json
import signal
import sqlite3
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

# ============================================================
# CONFIG
# ============================================================
BROKER     = "localhost"           # Mosquitto on the Pi itself
PORT       = 1883
CLIENT_ID  = "plant-logger-01"
DB_PATH    = Path.home() / "smart_plant" / "plant.db"
TOPICS     = ["plant/sensors", "plant/events", "plant/status"]

# ============================================================
# DATABASE
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix       REAL    NOT NULL,
    ts_device     INTEGER,
    temp_c        REAL,
    humid         REAL,
    soil_temp     REAL,
    soil_moisture INTEGER,
    lux           INTEGER,
    plant         TEXT,
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix   REAL    NOT NULL,
    ts_device INTEGER,
    type      TEXT,
    detail    TEXT,
    raw_json  TEXT
);

CREATE TABLE IF NOT EXISTS status (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix REAL NOT NULL,
    state   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensors_ts ON sensors(ts_unix DESC);
CREATE INDEX IF NOT EXISTS idx_events_ts  ON events(ts_unix DESC);
"""

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

# ============================================================
# MQTT CALLBACKS (paho-mqtt v2 API)
# ============================================================
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"[MQTT] connected to {BROKER}:{PORT}")
        for t in TOPICS:
            client.subscribe(t, qos=1)
            print(f"[MQTT] subscribed {t}")
    else:
        print(f"[MQTT] connect FAILED rc={reason_code}", file=sys.stderr)

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print(f"[MQTT] disconnected rc={reason_code}")

def on_message(client, userdata, msg):
    db = userdata["db"]
    topic   = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace")
    now     = time.time()

    # Always echo to stdout for live debugging
    print(f"[{topic}] {payload}")

    try:
        if topic == "plant/sensors":
            d = json.loads(payload)
            db.execute(
                "INSERT INTO sensors "
                "(ts_unix, ts_device, temp_c, humid, soil_temp, soil_moisture, lux, plant, raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now, d.get("ts"), d.get("temp_c"), d.get("humid"),
                 d.get("soil_temp"), d.get("soil_moisture"), d.get("lux"),
                 d.get("plant"), payload),
            )
            db.commit()

        elif topic == "plant/events":
            d = json.loads(payload)
            db.execute(
                "INSERT INTO events "
                "(ts_unix, ts_device, type, detail, raw_json) VALUES (?,?,?,?,?)",
                (now, d.get("ts"), d.get("type"), d.get("detail"), payload),
            )
            db.commit()

        elif topic == "plant/status":
            db.execute(
                "INSERT INTO status (ts_unix, state) VALUES (?,?)",
                (now, payload.strip()),
            )
            db.commit()

    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON decode failed on {topic}: {e}", file=sys.stderr)
    except sqlite3.Error as e:
        print(f"[ERROR] DB write failed: {e}", file=sys.stderr)

# ============================================================
# MAIN
# ============================================================
def main():
    db = init_db(DB_PATH)
    print(f"[DB] {DB_PATH}")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
    )
    client.user_data_set({"db": db})
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    # Auto-reconnect with capped backoff
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    # Graceful shutdown
    def shutdown(sig, frame):
        print("\n[Shutdown] disconnecting...")
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass
        db.close()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[MQTT] connecting to {BROKER}:{PORT} ...")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_forever()

if __name__ == "__main__":
    main()
