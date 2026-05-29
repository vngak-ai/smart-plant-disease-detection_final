#!/usr/bin/env python3
"""
SIT210 Smart Plant — Flask REST API (Pi side).

Reads sensor / event / status data from SQLite (written by mqtt_logger.py),
and publishes control commands via MQTT for the Arduino to act on.

Run:
  python3 flask_api.py
Listens on 0.0.0.0:5000

Endpoints:
  GET  /                          — serve dashboard.html
  GET  /api/health                — service heartbeat
  GET  /api/latest                — most recent sensor row
  GET  /api/sensors?n=20          — last N sensor rows (max 500)
  GET  /api/events?n=10           — last N events     (max 200)
  GET  /api/status                — latest online/offline state
  GET  /api/disease               — latest disease diagnosis
  GET  /api/disease/history?n=10  — last N disease diagnoses (max 100)
  POST /api/water                 — trigger manual watering
  POST /api/check_disease         — trigger manual disease check
  POST /api/plant_type            — set current plant   body: {"plant":"tomato"}
"""

import sqlite3
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
import paho.mqtt.client as mqtt

# ============================================================
# CONFIG
# ============================================================
DB_PATH       = Path.home() / "smart_plant" / "plant.db"
STATIC_DIR    = Path.home() / "smart_plant" / "static"
MQTT_BROKER   = "localhost"
MQTT_PORT     = 1883
HTTP_PORT     = 5000

# ============================================================
# APP + MQTT
# ============================================================
app = Flask(__name__)

# Permissive CORS for the dashboard (Pi LAN only, low-stakes)
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

_mqtt = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="plant-api-01",
)
_mqtt.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
_mqtt.loop_start()

# ============================================================
# DB HELPERS  (per-request connection — SQLite is happy with this)
# ============================================================
def db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def to_list(rows):
    return [dict(r) for r in rows]

# ============================================================
# READ ENDPOINTS
# ============================================================
@app.get("/api/health")
def health():
    return jsonify(status="ok", ts=time.time())

@app.get("/api/latest")
def latest():
    with db() as conn:
        row = conn.execute(
            "SELECT ts_unix, temp_c, humid, soil_temp, soil_moisture, lux, plant "
            "FROM sensors ORDER BY ts_unix DESC LIMIT 1"
        ).fetchone()
    if not row:
        return jsonify(error="no data yet"), 404
    return jsonify(dict(row))

@app.get("/api/sensors")
def sensors_history():
    try:
        n = min(max(int(request.args.get("n", 20)), 1), 500)
    except ValueError:
        return jsonify(error="bad n"), 400
    with db() as conn:
        rows = conn.execute(
            "SELECT ts_unix, temp_c, humid, soil_temp, soil_moisture, lux "
            "FROM sensors ORDER BY ts_unix DESC LIMIT ?", (n,)
        ).fetchall()
    return jsonify(to_list(rows))

@app.get("/api/events")
def events_history():
    try:
        n = min(max(int(request.args.get("n", 10)), 1), 200)
    except ValueError:
        return jsonify(error="bad n"), 400
    with db() as conn:
        rows = conn.execute(
            "SELECT ts_unix, type, detail FROM events ORDER BY ts_unix DESC LIMIT ?", (n,)
        ).fetchall()
    return jsonify(to_list(rows))

@app.get("/api/status")
def device_status():
    with db() as conn:
        row = conn.execute(
            "SELECT ts_unix, state FROM status ORDER BY ts_unix DESC LIMIT 1"
        ).fetchone()
    if not row:
        return jsonify(state="unknown")
    return jsonify(dict(row))

@app.get("/api/disease")
def latest_disease():
    with db() as conn:
        row = conn.execute(
            "SELECT ts_unix, declared_plant, top1_class, top1_confidence, "
            "top2_class, top2_confidence, verdict, disease, plant_family, notes "
            "FROM disease_results ORDER BY ts_unix DESC LIMIT 1"
        ).fetchone()
    if not row:
        return jsonify(error="no diagnosis yet"), 404
    return jsonify(dict(row))

@app.get("/api/disease/history")
def disease_history():
    try:
        n = min(max(int(request.args.get("n", 10)), 1), 100)
    except ValueError:
        return jsonify(error="bad n"), 400
    with db() as conn:
        rows = conn.execute(
            "SELECT ts_unix, declared_plant, top1_class, top1_confidence, "
            "verdict, disease, notes FROM disease_results "
            "ORDER BY ts_unix DESC LIMIT ?", (n,)
        ).fetchall()
    return jsonify(to_list(rows))

# ============================================================
# DASHBOARD (static file)
# ============================================================
@app.get("/")
def dashboard():
    if not (STATIC_DIR / "dashboard.html").exists():
        return ("<h1>Dashboard not deployed yet</h1>"
                f"<p>Expected at: {STATIC_DIR}/dashboard.html</p>"), 404
    return send_from_directory(STATIC_DIR, "dashboard.html")

# ============================================================
# CONTROL ENDPOINTS  (publish to MQTT)
# ============================================================
@app.post("/api/water")
def trigger_water():
    _mqtt.publish("plant/control/water", "1", qos=1)
    return jsonify(ok=True, action="water")

@app.post("/api/check_disease")
def trigger_disease_check():
    _mqtt.publish("plant/control/check_disease", "1", qos=1)
    return jsonify(ok=True, action="check_disease")

@app.post("/api/plant_type")
def set_plant_type():
    body  = request.get_json(silent=True) or {}
    plant = body.get("plant", "tomato")
    _mqtt.publish("plant/control/plant_type", plant, qos=1, retain=True)
    return jsonify(ok=True, plant=plant)

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print(f"[API] DB={DB_PATH}")
    print(f"[API] MQTT={MQTT_BROKER}:{MQTT_PORT}")
    print(f"[API] listening on 0.0.0.0:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False)
