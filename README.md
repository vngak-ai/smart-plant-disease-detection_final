# Smart Plant Disease Detection System

An IoT plant monitor that waters automatically and uses an on-device AI model to catch leaf disease early, keeping a human in the loop for treatment.

This repository contains the full source code for a SIT210 Embedded Systems Development project at Deakin University.

## Architecture

The system uses two boards working together:

- **Arduino Nano 33 IoT** — sensor hub and local controller. Reads four sensors every five seconds, publishes data over MQTT, and runs local auto-watering logic with safety caps so the plant keeps being watered even if the network goes down.
- **Raspberry Pi 4** — the brain of the system. Runs the Mosquitto MQTT broker, a TensorFlow Lite inference worker with a two-stage disease classifier, a Flask dashboard for live monitoring, and SQLite for logging.

## Repository Structure

- `smart_plant_1/` — Arduino sketch
  - Sensor reading (DHT22, BH1750, capacitive soil moisture, DS18B20)
  - MQTT publish over Wi-Fi
  - Local auto-watering logic with rate limiting and safety caps
- `mqtt_logger.py` — Pi script that subscribes to MQTT topics and logs all sensor readings and events to SQLite
- `inference_worker.py` — Pi script that runs the TFLite disease detection model with two-stage classification, confidence thresholds, and sensor cross-validation
- `flask_api.py` — Pi script that serves the live dashboard and exposes a REST API for sensor history and disease diagnosis results

## Hardware

Raspberry Pi 4, Arduino Nano 33 IoT, DHT22 temperature and humidity sensor, BH1750 light sensor, capacitive soil moisture sensor, DS18B20 waterproof temperature sensor, Raspberry Pi Camera Module 3, Keyestudio relay module with transistor buffer, DC water pump, active buzzer, plus standard wiring (jumper wires, breadboard, pull-up resistors).

## Software

- Raspberry Pi OS (Debian 13 Trixie)
- Arduino IDE with libraries: WiFiNINA, PubSubClient, ArduinoJson, DHT sensor library, BH1750, OneWire, DallasTemperature
- Python libraries on the Pi: paho-mqtt, ai-edge-litert, flask, picamera2
- Eclipse Mosquitto MQTT broker
- Model trained on the PlantVillage dataset using TensorFlow on Google Colab

## Full Tutorial

For the complete how-to article including wiring, setup steps, screenshots, and explanations:

https://www.hackster.io/khoaanhnguyenvo2006/smart-plant-disease-detector-fbb6ea

## Setup Notes

Before running the Arduino sketch, replace the placeholder values for `WIFI_SSID`, `WIFI_PASSWORD`, and the MQTT broker IP address (the Pi's IP) in the sketch.

On the Pi, install Mosquitto and the Python libraries listed above, then run the three Python scripts in any order. The Flask dashboard listens on port 5000 by default.

---

This repository is part of an assignment submitted to Deakin University, School of IT, Unit SIT210/730 – Embedded Systems Development.
