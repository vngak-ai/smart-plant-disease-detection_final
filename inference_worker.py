#!/usr/bin/env python3
"""
SIT210 Smart Plant — TFLite inference worker (Pi side).

Listens for manual disease-check triggers on MQTT, captures an image
from the Pi Camera, runs MobileNetV2 inference, and publishes a structured
diagnosis to plant/disease. Also writes results to SQLite.

Pipeline per check:
  1. Capture image (picamera2, 224x224 for MobileNetV2)
  2. TFLite inference (38 classes, unmasked first pass)
  3. Plant verification — does top-1 plant family match declared plant?
  4. Stage 1  binary Healthy / Unhealthy (>85% confidence)
  5. Stage 2  specific disease if conf >85% AND top-2 margin >20%
  6. Cross-validate Stage 2 result against latest sensor readings
  7. Publish JSON to plant/disease + insert row in disease_results table

Subscribes:
  plant/control/check_disease    trigger (payload ignored)
  plant/control/plant_type       update declared plant (retained)
  plant/sensors                  latest readings (for cross-validation)

Publishes:
  plant/disease                  diagnosis JSON

Run:
  python3 inference_worker.py
"""

import json
import signal
import sqlite3
import sys
import time
from pathlib import Path
from threading import Lock

import numpy as np
import paho.mqtt.client as mqtt
from PIL import Image

# TFLite runtime (Pi uses ai-edge-litert; expose same Interpreter API)
try:
    from ai_edge_litert.interpreter import Interpreter
except ImportError:
    from tflite_runtime.interpreter import Interpreter  # legacy fallback

from picamera2 import Picamera2

# ============================================================
# CONFIG
# ============================================================
HOME            = Path.home() / "smart_plant"
MODEL_PATH      = HOME / "models" / "plant_disease_model.tflite"
LABELS_PATH     = HOME / "models" / "class_names.json"
DB_PATH         = HOME / "plant.db"

MQTT_BROKER     = "localhost"
MQTT_PORT       = 1883
CLIENT_ID       = "plant-inference-01"

IMG_SIZE        = 224  # MobileNetV2 input

# Decision thresholds (per memory)
STAGE1_THRESHOLD       = 0.85  # binary Healthy/Unhealthy confidence floor
STAGE2_THRESHOLD       = 0.85  # specific disease confidence floor
STAGE2_MARGIN          = 0.20  # top-1 vs top-2 margin required
PLANT_MISMATCH_THRESH  = 0.70  # mismatch flagged only if confident
LOW_CONFIDENCE_THRESH  = 0.70  # below this  manual check

# ============================================================
# DATABASE
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS disease_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix         REAL    NOT NULL,
    declared_plant  TEXT,
    top1_class      TEXT,
    top1_confidence REAL,
    top2_class      TEXT,
    top2_confidence REAL,
    verdict         TEXT,    -- e.g. healthy, disease_detected, plant_mismatch, low_confidence, conflicting_signals
    disease         TEXT,    -- specific disease if Stage 2 passed
    plant_family    TEXT,
    notes           TEXT,    -- human-readable explanation
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_disease_ts ON disease_results(ts_unix DESC);
"""

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

# ============================================================
# LABELS
# ============================================================
def load_labels(path: Path):
    """Returns:
       labels  : dict[int -> str]   full 38-class names
       families: dict[int -> str]   plant family per class
    """
    raw = json.loads(path.read_text())
    # JSON keys are strings ("0".."37")
    labels = {int(k): v for k, v in raw.items()}
    families = {i: name.split("___")[0] for i, name in labels.items()}
    return labels, families

# ============================================================
# IMAGE CAPTURE
# ============================================================
class CameraCapture:
    """Wraps picamera2 to capture a 224x224 RGB image as numpy float32."""

    def __init__(self):
        self.cam = Picamera2()
        config  = self.cam.create_still_configuration(
            main={"size": (640, 640), "format": "RGB888"}
        )
        self.cam.configure(config)
        self.cam.start()
        time.sleep(1.5)  # let AE/AWB settle
        print("[Camera] started")

    def capture(self) -> np.ndarray:
        """Returns float32 array shape (1, 224, 224, 3) normalised to [0, 1]."""
        arr = self.cam.capture_array("main")   # (H, W, 3) uint8 RGB
        img = Image.fromarray(arr).resize((IMG_SIZE, IMG_SIZE), Image.BILINEAR)
        x   = np.asarray(img, dtype=np.float32) / 255.0
        return np.expand_dims(x, axis=0)

    def close(self):
        try:
            self.cam.stop()
            self.cam.close()
        except Exception:
            pass

# ============================================================
# TFLITE INFERENCE
# ============================================================
class DiseaseClassifier:
    def __init__(self, model_path: Path):
        self.interp = Interpreter(model_path=str(model_path))
        self.interp.allocate_tensors()
        self.in_d  = self.interp.get_input_details()[0]
        self.out_d = self.interp.get_output_details()[0]
        print(f"[Model] {model_path.name} | in={self.in_d['shape']} out={self.out_d['shape']}")

    def predict(self, x: np.ndarray):
        """Returns dense softmax-like probability vector (38,)."""
        # If model expects uint8 (full INT8 quant), rescale
        if self.in_d["dtype"] == np.uint8:
            x_in = (x * 255.0).astype(np.uint8)
        else:
            x_in = x.astype(self.in_d["dtype"])
        self.interp.set_tensor(self.in_d["index"], x_in)
        self.interp.invoke()
        out = self.interp.get_tensor(self.out_d["index"])[0]
        # Dequantize if needed
        if self.out_d["dtype"] == np.uint8:
            scale, zp = self.out_d["quantization"]
            out = (out.astype(np.float32) - zp) * scale
        # Normalise to probabilities (defensive: some exports skip softmax)
        if out.min() < 0 or out.sum() > 1.5 or out.sum() < 0.5:
            e = np.exp(out - out.max())
            out = e / e.sum()
        return out

# ============================================================
# DECISION LOGIC (the brains)
# ============================================================
def diagnose(probs: np.ndarray, labels: dict, families: dict,
             declared_plant: str, latest_sensors: dict | None) -> dict:
    """
    Apply the hierarchy:
      1. Plant verification (declared vs predicted family)
      2. Stage 1 binary Healthy / Unhealthy
      3. Stage 2 specific disease (with margin)
      4. Sensor cross-validation
    Returns a dict ready to publish & persist.
    """
    top_idx       = int(np.argsort(probs)[::-1][:2][0])
    top2_idx      = int(np.argsort(probs)[::-1][:2][1])
    top1_class    = labels[top_idx]
    top2_class    = labels[top2_idx]
    top1_conf     = float(probs[top_idx])
    top2_conf     = float(probs[top2_idx])
    pred_family   = families[top_idx]

    result = {
        "ts":              time.time(),
        "declared_plant":  declared_plant,
        "top1_class":      top1_class,
        "top1_confidence": round(top1_conf, 4),
        "top2_class":      top2_class,
        "top2_confidence": round(top2_conf, 4),
        "plant_family":    pred_family,
        "disease":         None,
        "verdict":         None,
        "notes":           "",
    }

    # ---- Step 1: plant verification ---------------------------------
    declared_norm = (declared_plant or "").strip().lower()
    pred_norm     = pred_family.lower()
    if declared_norm and declared_norm not in pred_norm and pred_norm not in declared_norm:
        if top1_conf >= PLANT_MISMATCH_THRESH:
            result["verdict"] = "plant_mismatch"
            result["notes"]   = (
                f"Declared plant '{declared_plant}' does not match predicted "
                f"family '{pred_family}' (conf {top1_conf:.0%}). Verify the plant."
            )
            return result

    # ---- Step 2: confidence floor (Stage 1 binary) ------------------
    if top1_conf < LOW_CONFIDENCE_THRESH:
        result["verdict"] = "low_confidence"
        result["notes"]   = (
            f"Top prediction '{top1_class}' only {top1_conf:.0%} confident — "
            f"manual inspection recommended."
        )
        return result

    is_healthy = top1_class.lower().endswith("___healthy")

    if top1_conf < STAGE1_THRESHOLD:
        # Stage 1 fail
        result["verdict"] = "unhealthy_unspecified" if not is_healthy else "low_confidence"
        result["notes"]   = (
            f"Stage 1 ({'healthy' if is_healthy else 'unhealthy'}) "
            f"only {top1_conf:.0%}, below {STAGE1_THRESHOLD:.0%} — manual check."
        )
        return result

    if is_healthy:
        result["verdict"] = "healthy"
        result["notes"]   = f"Plant appears healthy ({top1_conf:.0%} confidence)."
        return result

    # ---- Step 3: Stage 2 specific disease ---------------------------
    margin = top1_conf - top2_conf
    if top1_conf < STAGE2_THRESHOLD or margin < STAGE2_MARGIN:
        result["verdict"] = "unhealthy_unspecified"
        result["notes"]   = (
            f"Unhealthy detected but specific disease unclear "
            f"(top1 {top1_conf:.0%}, margin {margin:.0%}). Manual inspection."
        )
        return result

    disease = top1_class.split("___", 1)[1].replace("_", " ")
    result["disease"] = disease
    result["verdict"] = "disease_detected"
    result["notes"]   = f"Detected {disease} ({top1_conf:.0%} confidence)."

    # ---- Step 4: sensor cross-validation ----------------------------
    conflict = sensor_conflict(disease, latest_sensors)
    if conflict:
        result["verdict"] = "conflicting_signals"
        result["notes"]  += f" Conflicting signals: {conflict} — needs review."

    return result

def sensor_conflict(disease: str, sensors: dict | None) -> str | None:
    """Light-touch sanity checks: only flag obvious contradictions.
    Rules derived from plant pathology consensus on optimal disease conditions.
    Conservative thresholds  flag only when environment is clearly hostile to the
    detected pathogen, indicating possible misclassification or unusual case.
    """
    if not sensors:
        return None
    d        = disease.lower()
    humid    = sensors.get("humid")
    temp     = sensors.get("temp_c")
    if humid is None or temp is None:
        return None

    # Late Blight (Phytophthora infestans)  needs cool & wet
    if "late_blight" in d or "late blight" in d:
        if humid < 60 and temp > 25:
            return (f"Late Blight typically needs humidity >80% and temp <22C; "
                    f"got humid={humid}%, temp={temp}C")

    # Powdery Mildew  thrives in moderate humidity, dry leaf surface
    if "powdery_mildew" in d or "powdery mildew" in d:
        if humid > 90:
            return (f"Powdery Mildew unusual at humidity={humid}% "
                    f"(typically prefers <70%, dry leaves)")

    # Bacterial Spot  warm & wet for bacterial spread
    if "bacterial_spot" in d or "bacterial spot" in d:
        if humid < 50 and temp < 20:
            return (f"Bacterial Spot typically needs warm wet conditions (humid >70%, "
                    f"temp >24C); got humid={humid}%, temp={temp}C")

    # Early Blight  warm-season pathogen, cold environment unusual
    if "early_blight" in d or "early blight" in d:
        if temp < 15:
            return (f"Early Blight typically occurs in warm seasons (temp >20C); "
                    f"got temp={temp}C")

    # Leaf Scorch  fungal sporulation needs moisture
    if "leaf_scorch" in d or "leaf scorch" in d:
        if humid < 30:
            return (f"Leaf Scorch fungal sporulation typically needs moist conditions "
                    f"(humid >50%); got humid={humid}%")

    # Common Rust  cool moist conditions
    if "common_rust" in d or "common rust" in d:
        if humid < 40 and temp > 30:
            return (f"Common Rust thrives in cool moist conditions (humid >70%, "
                    f"temp 16-22C); got humid={humid}%, temp={temp}C")

    return None

# ============================================================
# MAIN APP
# ============================================================
class InferenceWorker:
    def __init__(self):
        self.db          = init_db()
        self.labels, self.families = load_labels(LABELS_PATH)
        print(f"[Labels] {len(self.labels)} classes, {len(set(self.families.values()))} plant families")

        self.classifier  = DiseaseClassifier(MODEL_PATH)
        self.camera      = CameraCapture()

        self.declared_plant = "tomato"   # default; updated via MQTT
        self.latest_sensors = {}         # cache from plant/sensors
        self.busy_lock      = Lock()     # serialize inferences

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
        )
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ---- MQTT ------------------------------------------------------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"[MQTT] connected")
            client.subscribe("plant/control/check_disease", qos=1)
            client.subscribe("plant/control/plant_type",    qos=1)
            client.subscribe("plant/sensors",               qos=0)
        else:
            print(f"[MQTT] connect failed rc={reason_code}", file=sys.stderr)

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            if topic == "plant/sensors":
                self.latest_sensors = json.loads(payload)
            elif topic == "plant/control/plant_type":
                self.declared_plant = payload.strip() or "tomato"
                print(f"[Plant] declared = {self.declared_plant}")
            elif topic == "plant/control/check_disease":
                self.handle_check()
        except Exception as e:
            print(f"[ERROR] on_message({topic}): {e}", file=sys.stderr)

    # ---- Inference flow -------------------------------------------
    def handle_check(self):
        if not self.busy_lock.acquire(blocking=False):
            print("[Check] already running, ignoring duplicate trigger")
            return
        try:
            t0 = time.time()
            print(f"[Check] start  declared={self.declared_plant}")

            x      = self.camera.capture()
            t_cap  = time.time() - t0
            probs  = self.classifier.predict(x)
            t_inf  = time.time() - t0 - t_cap

            result = diagnose(probs, self.labels, self.families,
                              self.declared_plant, self.latest_sensors)
            result["latency_ms"] = int((time.time() - t0) * 1000)
            result["t_capture_ms"]   = int(t_cap * 1000)
            result["t_inference_ms"] = int(t_inf * 1000)

            self.publish(result)
            self.persist(result)
            print(f"[Check] done  verdict={result['verdict']}  "
                  f"top1={result['top1_class']} ({result['top1_confidence']:.0%})  "
                  f"latency={result['latency_ms']}ms")
        except Exception as e:
            print(f"[ERROR] inference failed: {e}", file=sys.stderr)
        finally:
            self.busy_lock.release()

    def publish(self, result: dict):
        payload = json.dumps(result)
        self.client.publish("plant/disease", payload, qos=1, retain=True)

    def persist(self, result: dict):
        try:
            self.db.execute(
                "INSERT INTO disease_results "
                "(ts_unix, declared_plant, top1_class, top1_confidence, "
                " top2_class, top2_confidence, verdict, disease, plant_family, "
                " notes, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (result["ts"], result["declared_plant"],
                 result["top1_class"], result["top1_confidence"],
                 result["top2_class"], result["top2_confidence"],
                 result["verdict"], result["disease"], result["plant_family"],
                 result["notes"], json.dumps(result)),
            )
            self.db.commit()
        except sqlite3.Error as e:
            print(f"[ERROR] DB write failed: {e}", file=sys.stderr)

    # ---- Lifecycle -------------------------------------------------
    def run(self):
        def shutdown(sig, frame):
            print("\n[Shutdown] stopping...")
            try:
                self.client.disconnect()
                self.client.loop_stop()
                self.camera.close()
                self.db.close()
            except Exception:
                pass
            sys.exit(0)

        signal.signal(signal.SIGINT,  shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        print(f"[MQTT] connecting to {MQTT_BROKER}:{MQTT_PORT}")
        self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        print("[Worker] ready  waiting for triggers on plant/control/check_disease")
        self.client.loop_forever()

if __name__ == "__main__":
    InferenceWorker().run()
