"""
CogNet-SDN | backend/app.py

Flask REST API + SocketIO for the dashboard.
Port: 5050

Run (Terminal 4):
    source ~/cognet-env/bin/activate
    python3 ~/cognet-sdn/backend/app.py

Endpoints:
  GET  /api/status           → controller + training status
  GET  /api/topology         → network graph (nodes + edges)
  GET  /api/traffic_matrix   → live 6×6 TM from Ryu
  GET  /api/link_stats       → per-link BW, delay, loss
  GET  /api/training/history → all episode rewards so far
  POST /api/training         → receive episode update from train_sdn.py
  GET  /api/lstm/predict     → LSTM prediction of next TM
  GET  /api/reward           → latest DQN reward from Ryu
"""

import os
import sys
import json
import time
import threading
import requests
import numpy as np

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit

try:
    from ml.lstm_predictor import LSTMPredictor, WINDOW_SIZE
    LSTM_AVAILABLE = True
except Exception:
    LSTM_AVAILABLE = False
    WINDOW_SIZE    = 5

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Config ─────────────────────────────────────────────────────────────────
RYU_BASE   = "http://127.0.0.1:8181"
LSTM_PATH  = os.path.join(PROJECT_DIR, "models", "lstm_predictor.pt")
PUSH_INTERVAL = 5   # seconds between live pushes to dashboard

# ── In-memory state ────────────────────────────────────────────────────────
training_history = []         # list of {episode, reward}
tm_history       = []         # last N traffic matrices (for LSTM window)
lstm_model       = None
_lock            = threading.Lock()


def _load_lstm():
    global lstm_model
    if LSTM_AVAILABLE and os.path.exists(LSTM_PATH):
        try:
            lstm_model = LSTMPredictor.load(LSTM_PATH)
            print(f"[backend] LSTM loaded from {LSTM_PATH}")
        except Exception as e:
            print(f"[backend] LSTM load failed: {e}")


def _ryu_get(endpoint: str, timeout: int = 2):
    try:
        r = requests.get(f"{RYU_BASE}{endpoint}", timeout=timeout)
        return r.json()
    except Exception:
        return None


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    ryu_ok = _ryu_get("/cognet/stats/switches") is not None
    sw     = _ryu_get("/cognet/stats/switches") or {}
    return jsonify({
        "controller"  : "online" if ryu_ok else "offline",
        "switches"    : sw.get("count", 0),
        "lstm_loaded" : lstm_model is not None,
        "episodes_done": len(training_history),
        "timestamp"   : time.time(),
    })


@app.route("/api/topology")
def topology():
    topo = _ryu_get("/cognet/topo") or {}
    sw   = _ryu_get("/cognet/stats/switches") or {}

    nodes = [{"id": f"s{i}", "label": f"s{i}", "type": "switch"}
             for i in range(1, 7)]
    # Add hosts
    host_map = {2: [1,2], 3: [3,4], 4: [5,6], 5: [7,8]}
    for sw_id, hosts in host_map.items():
        for h in hosts:
            nodes.append({"id": f"h{h}", "label": f"h{h}", "type": "host"})

    edges = []
    for link in topo.get("links", []):
        src = int(link["src"]) & 0xFF
        dst = int(link["dst"]) & 0xFF
        edges.append({"source": f"s{src}", "target": f"s{dst}",
                      "src_port": link.get("src_port"),
                      "dst_port": link.get("dst_port")})

    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/api/traffic_matrix")
def traffic_matrix():
    data = _ryu_get("/cognet/stats/traffic_matrix") or {}
    tm   = data.get("matrix", [0.0] * 36)

    # Store for LSTM window
    with _lock:
        tm_history.append(tm)
        if len(tm_history) > 100:
            tm_history.pop(0)

    return jsonify({
        "matrix"   : tm,
        "shape"    : [6, 6],
        "reward"   : data.get("reward", 0.0),
        "timestamp": time.time(),
    })


@app.route("/api/link_stats")
def link_stats():
    data = _ryu_get("/cognet/stats/links") or {}
    # Flatten to list of {switch, port, bw_mbps, delay_ms, loss_ratio}
    result = []
    for dpid_str, ports in data.items():
        sw_num = int(dpid_str) & 0xFF
        for port_str, stats in ports.items():
            result.append({
                "switch"    : f"s{sw_num}",
                "port"      : int(port_str),
                "bw_mbps"   : stats.get("bw_mbps",    0.0),
                "delay_ms"  : stats.get("delay_ms",   0.0),
                "loss_ratio": stats.get("loss_ratio",  0.0),
            })
    return jsonify({"links": result, "count": len(result)})


@app.route("/api/training/history")
def training_history_ep():
    with _lock:
        hist = list(training_history)
    return jsonify({"history": hist, "total": len(hist)})


@app.route("/api/training", methods=["POST"])
def training_update():
    """Receive episode update from train_sdn.py."""
    body = request.get_json(silent=True) or {}
    ep   = body.get("episode", len(training_history))
    rew  = body.get("reward",  0.0)
    with _lock:
        training_history.append({"episode": ep, "reward": rew})
    socketio.emit("training_update", {"episode": ep, "reward": rew})
    return jsonify({"status": "ok"})


@app.route("/api/lstm/predict")
def lstm_predict():
    with _lock:
        recent = list(tm_history[-WINDOW_SIZE:])

    if lstm_model is None:
        return jsonify({"error": "LSTM model not loaded", "predicted_tm": None}), 503

    if len(recent) < WINDOW_SIZE:
        pad    = [[0.0] * 36] * (WINDOW_SIZE - len(recent))
        recent = pad + recent

    result = lstm_model.predict_congestion(recent, threshold=80.0)
    result["timestamp"] = time.time()
    return jsonify(result)


@app.route("/api/reward")
def reward():
    data = _ryu_get("/cognet/stats/reward") or {}
    return jsonify(data)


# ── SocketIO: push live stats every PUSH_INTERVAL seconds ─────────────────
def _live_push_loop():
    while True:
        time.sleep(PUSH_INTERVAL)
        try:
            tm   = _ryu_get("/cognet/stats/traffic_matrix") or {}
            lnks = _ryu_get("/cognet/stats/links") or {}
            socketio.emit("live_stats", {
                "traffic_matrix": tm.get("matrix", []),
                "reward"        : tm.get("reward", 0.0),
                "link_count"    : sum(len(v) for v in lnks.values()),
                "timestamp"     : time.time(),
            })
        except Exception:
            pass


# ── Startup ────────────────────────────────────────────────────────────────
def create_app():
    _load_lstm()
    push_thread = threading.Thread(target=_live_push_loop, daemon=True)
    push_thread.start()
    return app


if __name__ == "__main__":
    print("=" * 50)
    print("  CogNet-SDN Flask Backend  (port 5050)")
    print("=" * 50)
    create_app()
    socketio.run(app, host="0.0.0.0", port=5050, debug=False)
    
@app.route("/api/stats")
def stats():
    tm   = _ryu_get("/cognet/stats/traffic_matrix") or {}
    lnks = _ryu_get("/cognet/stats/links") or {}
    sw   = _ryu_get("/cognet/stats/switches") or {}
    return jsonify({
        "traffic_matrix": tm.get("matrix", []),
        "reward"        : tm.get("reward", 0.0),
        "switches"      : sw.get("count", 0),
        "links"         : lnks,
        "timestamp"     : time.time(),
    })
