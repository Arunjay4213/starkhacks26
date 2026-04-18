#!/usr/bin/env python3
"""
Mock hardware receiver for Person B development.

Matches the real /stimulate /stop /status /health contract exactly.
Every request is logged to stdout. No serial port, no Arduino needed.

Run with:
    python tests/mock_receiver.py
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MOCK] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/stimulate", methods=["POST"])
def stimulate():
    data = request.get_json(force=True, silent=True) or {}
    finger = data.get("finger", "UNKNOWN")
    action = data.get("action", "UNKNOWN")
    duration_ms = data.get("duration_ms", 0)
    logger.info("stimulate finger=%s action=%s duration_ms=%s", finger, action, duration_ms)
    ack = f"MOCK:OK:{finger}:{action}"
    return jsonify({"status": "ok", "ack": ack})


@app.route("/stop", methods=["POST"])
def stop():
    logger.info("STOP")
    return jsonify({"status": "ok"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "connected": True,
        "last_ack": "MOCK",
        "watchdog_remaining_ms": 3000,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"alive": True})


if __name__ == "__main__":
    logger.info("Mock receiver starting on port 5001")
    logger.info("Endpoints: POST /stimulate  POST /stop  GET /status  GET /health")
    app.run(host="0.0.0.0", port=5001, debug=False)
