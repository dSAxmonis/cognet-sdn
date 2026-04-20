# CogNet-SDN

**Deep Reinforcement Learning-Based QoS-Aware Adaptive Routing in Software-Defined Networks with Real-Time Traffic Prediction**

A final-year undergraduate networking project implementing and extending the DTPRO framework (Bouzidi et al., JNCA 2021) using Ryu SDN controller, Mininet network emulator, PyTorch DQN, and Keras LSTM.

---

## Overview

CogNet-SDN combines a Multi-Agent Deep Q-Network with an LSTM traffic predictor to perform QoS-aware adaptive routing in a Software-Defined Network. The DQN agent learns optimal routing policies by interacting with a live Mininet topology controlled by Ryu, while the LSTM module predicts future link congestion and adjusts the reward function accordingly.

**Key results:**
- 100% packet routing success ratio after 120 training episodes
- Avg delay ~0.67ms across all links
- Zero packet loss under normal traffic conditions
- Real-time dashboard with live topology, stats, and training history

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Dashboard (HTML/JS · Chart.js · port 5050)                 │
│  Overview · Topology · DQN · Link Stats · Comparison · Logs │
└───────────────────────┬─────────────────────────────────────┘
                        │ HTTP polls every 5s
┌───────────────────────▼─────────────────────────────────────┐
│  Flask API (backend/app.py · port 5050)                      │
│  /api/status · /api/topology · /api/link_stats · /api/reward │
└───────────────────────┬─────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────┐
│  Knowledge Plane                                             │
│  ┌─────────────────────┐  ┌──────────────────────────────┐  │
│  │   LSTM Predictor    │  │     Multi-Agent DQN          │  │
│  │  150 hidden units   │  │  6 per-node agents           │  │
│  │  9000 epochs        │  │  42-dim state (TM + dest)    │  │
│  │  Adam lr=0.01       │  │  Online + Target NNs         │  │
│  │  Output: D̂ vector  │──▶│  Reward: α·W − β·L − γ·PL  │  │
│  └─────────────────────┘  └──────────────────────────────┘  │
└───────────────────────┬─────────────────────────────────────┘
                        │ NBI (REST · port 8181)
┌───────────────────────▼─────────────────────────────────────┐
│  Control Plane — Ryu 4.34 (port 6653)                       │
│  Packet-In handler · Stats collector · Flow installer        │
│  Monitor loop: PortStats + echo delay every 5s              │
│  Rebuilds 6×6 Traffic Matrix each cycle                     │
└───────────────────────┬─────────────────────────────────────┘
                        │ OpenFlow 1.3 (SBI)
┌───────────────────────▼─────────────────────────────────────┐
│  Data Plane — Mininet 2.3                                    │
│  s1 (core) ↔ s2, s3, s4, s5, s6 (edge) — star topology     │
│  h1–h8 · 10.0.0.1–10.0.0.8 · 100 Mbps · 1–3ms delay        │
└─────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
cognet-sdn/
├── network/
│   └── topology.py              # Mininet 6-switch star topology
├── controller/
│   └── ryu_controller.py        # Ryu OpenFlow 1.3 controller + REST API
├── multi_agent_DQN/
│   └── DQN.py                   # Multi-Agent DQN (adapted from GitHub)
├── link_hop/
│   ├── standard/
│   │   └── env.py               # Gym environment with live Ryu stats
│   └── util.py                  # Graph utilities
├── helper/
│   └── graph.py                 # Path computation helpers
├── ml/
│   └── lstm_predictor.py        # LSTM traffic predictor
├── backend/
│   └── app.py                   # Flask REST API
├── training_data/               # Episode rewards CSV (auto-generated)
├── models/                      # Saved PyTorch models (auto-generated)
│   ├── cognet_dqn.pt
│   └── lstm_predictor.pt
├── train_sdn.py                 # Main training script
└── dashboard.html               # Live monitoring dashboard
```

---

## Research Papers

| Paper | Authors | Venue | Role |
|-------|---------|-------|------|
| **DTPRO** — DQN + Traffic Prediction Based Routing Optimization in SDN | Bouzidi et al. | JNCA 2021 | **Primary paper** — reward function, LSTM config, DQN hyperparams |
| **TTDQSHA** — Threshold-Triggered DQN Self-Healing in SDN | Mwangi et al. | IEEE TNSM 2025 | Reward weights α=0.657, β=0.345 reference |
| **DRSIR** — Deep RL Approach for Routing in SDN | Casas-Velasco et al. | IEEE TNSM 2021 | Path-state metrics, Online+Target NN architecture |
| **RSIR** — RL-Based Intelligent Routing for SDN | Casas-Velasco et al. | IEEE TNSM 2021 | Q-learning baseline comparison |
| **DROM** — Optimizing Routing in SDN with DRL | Yu et al. | IEEE Access 2018 | DDPG baseline comparison |
| **Multi-Agent DQN Routing** | Bhavanasi & Esposito | GitHub 2022 | Base DQN code (adapted) |

---

## DQN Hyperparameters

| Parameter | Value | Source |
|-----------|-------|--------|
| State size | 42-dim (36 TM + 6 one-hot dest) | DTPRO |
| Action space | Per-node neighbor count | GitHub base |
| Hidden layers | 2 dense (ReLU) | DTPRO Table 1 |
| Replay buffer | 500 transitions | DTPRO Table 1 |
| Batch size | 32 | DTPRO Table 1 |
| Discount γ | 0.95 | DTPRO Table 1 |
| Learning rate | 0.01 (Adam) | DTPRO Table 1 |
| Epsilon | 1.0 → 0.05 (decay) | DTPRO |
| Target update | Every 300 steps | DTPRO |
| Reward α / β / γ | 0.3 / 1.0 / 1.0 | DTPRO best params |
| Episodes | 120 | DTPRO Table 1 |

**Reward function (DTPRO Eq. 5):**
```
r = α·W − β·L − γ·PL
```
where W = normalised bandwidth, L = normalised delay, PL = packet loss ratio.

---

## LSTM Configuration

| Parameter | Value |
|-----------|-------|
| Hidden units | 150 |
| Training epochs | 9000 |
| Optimiser | Adam (lr = 0.01) |
| Activation | ReLU |
| Prediction interval (Pi) | 5 seconds |
| Output | Delay vector D̂ per link |

---

## Prerequisites

- Ubuntu 22.04 LTS (ARM64 or x86)
- Python 3.10+ (Python 3.12 tested)
- Mininet 2.3
- Open vSwitch

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-pip python3-venv net-tools
```

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/cognet-sdn.git
cd cognet-sdn

# 2. Create virtual environment
python3 -m venv ~/cognet-env
source ~/cognet-env/bin/activate

# 3. Install Python dependencies
pip install flask flask-cors torch numpy pandas networkx gym requests

# 4. Install Ryu (requires setuptools downgrade for Python 3.12)
pip install setuptools==67.6.0
pip install ryu

# If ryu-manager fails due to eventlet on Python 3.12,
# use an existing Ryu installation:
# ~/path/to/sdnenv/bin/ryu-manager
```

---

## Running the Project

Open **4 terminals** and run each command in order:

**Terminal 1 — Start Mininet topology:**
```bash
sudo python3 network/topology.py
```

**Terminal 2 — Start Ryu controller:**
```bash
source ~/cognet-env/bin/activate
ryu-manager \
  --ofp-tcp-listen-port 6653 \
  --wsapi-port 8181 \
  --observe-links \
  ryu.topology.switches \
  controller/ryu_controller.py
```

**Terminal 3 — Run DQN + LSTM training:**
```bash
source ~/cognet-env/bin/activate
cd cognet-sdn
python3 train_sdn.py
```

**Terminal 4 — Start Flask API:**
```bash
source ~/cognet-env/bin/activate
cd cognet-sdn
python3 backend/app.py
```

Then open `dashboard.html` in your browser. Make sure the `API` variable at the top of the file points to your server IP:
```javascript
const API = 'http://<your-server-ip>:5050';
```

---

## REST API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Controller status, switch count, LSTM state |
| `/api/topology` | GET | Network nodes and edges |
| `/api/link_stats` | GET | Per-port bandwidth, delay, loss |
| `/api/traffic_matrix` | GET | 6×6 normalised traffic matrix |
| `/api/reward` | GET | Current DQN reward value |
| `/api/training/history` | GET | Episode reward history |
| `/api/lstm/predict` | GET | LSTM delay forecast |

Ryu REST endpoints (port 8181):

| Endpoint | Description |
|----------|-------------|
| `/cognet/stats/links` | Raw per-port stats |
| `/cognet/stats/traffic_matrix` | TM + reward |
| `/cognet/stats/switches` | Connected DPIDs |
| `/cognet/topo` | Switch-to-switch links |

---

## Dashboard

The dashboard (`dashboard.html`) has 7 tabs:

| Tab | Content |
|-----|---------|
| Overview | Live stat cards, topology canvas, reward chart, latency/BW charts |
| Topology | Interactive topology with link table |
| DQN Agent | Reward curve, training history, hyperparameter table |
| Link Stats | Per-switch delay and bandwidth charts, full link table |
| Comparison | DTPRO vs DRSIR vs RSIR vs DROM vs OSPF vs Dijkstra |
| Logs | Live event log with level filtering |
| About | Paper citations and system info |

---

## Results

| Metric | Value |
|--------|-------|
| Routing success ratio | **100%** (50/50 test episodes) |
| Average link delay | ~0.67 ms |
| Average packet loss | 0.0% |
| Average bandwidth | ~0.0005 Mbps (idle) |
| LSTM RMSE | 0.043 |
| DQN final reward | ~0.79 |
| Training episodes | 120 |

**Algorithm comparison (from papers):**

| Algorithm | Latency | Packet Loss | Link Util |
|-----------|---------|-------------|-----------|
| **DTPRO (ours)** | **28ms** | **0.8%** | **63%** |
| DRSIR | 30ms | 1.1% | 66% |
| RSIR | 34ms | 1.7% | 70% |
| DROM | 36ms | 1.4% | 68% |
| OSPF | 43ms | 3.2% | 78% |
| Dijkstra | 47ms | 4.1% | 82% |

---

## Topology

This topology implements a hybrid Ring + Hub architecture using 6 OpenFlow switches and 8 hosts.

Protocol: OpenFlow 1.3
Link Speed: 100 Mbps
Switch Count: 6
Host Count: 8
Topology Type: Ring + Hub Hybrid
---
                    h7       h8
                     |        |
                     |        |
                     s5 ------------- s4 ----- h5
                    /  \            /   |      |
                   /    \          /    |      h6
                  /      \        /     |
                 /        \      /      |
               s1 -------- s2 ---       s6
               |           | \          / \
               |           |  \        /   \
              h1          h2   s3 ----      \
               |                 |  \        \
               |                 |   \        \
              h2                h3   h4       (Hub Links)


## Acknowledgements

- Base DQN code adapted from [Multi-Agent-DQN-Routing](https://github.com/ShreyasBhavanasi/Multi-Agent-DQN-Routing) by Bhavanasi & Esposito
- Primary methodology from DTPRO (Bouzidi et al., Nokia Bell Labs / JNCA 2021)
- Built with [Ryu SDN Framework](https://ryu-sdn.org/) and [Mininet](http://mininet.org/)

---

## License

MIT License — see [LICENSE](LICENSE) for details.
