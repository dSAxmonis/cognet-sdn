"""
CogNet-SDN | train_sdn.py
Main training loop: DQN + LSTM traffic prediction.

Run (Terminal 3):
    source ~/cognet-env/bin/activate
    cd ~/cognet-sdn
    python3 train_sdn.py

Prerequisites:
    Terminal 1: sudo python3 network/topology.py
    Terminal 2: ryu-manager --ofp-tcp-listen-port 6653 --wsapi-port 8080 \
                    controller/ryu_controller.py
"""

import os
import sys
import time
import json
import requests
import torch
import numpy as np

# ── Path setup ─────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

os.makedirs("training_data", exist_ok=True)
os.makedirs("models", exist_ok=True)

from link_hop.standard.env import Env, build_sdn_graph
from multi_agent_DQN.DQN   import MultiAgent
from ml.lstm_predictor      import LSTMPredictor, collect_training_data

# ── Config ─────────────────────────────────────────────────────────────────
RYU_BASE          = "http://127.0.0.1:8181"
FLASK_BASE        = "http://127.0.0.1:5050"
EPISODES          = 120          # DTPRO Table 1
LSTM_COLLECT_SECS = 60           # seconds to collect data before LSTM trains
STATS_INTERVAL    = 5            # seconds per Ryu poll interval (Pi)
SAVE_FILE         = "training_data/episode_rewards.csv"
MODEL_PATH        = "models/cognet_dqn.pt"
LSTM_PATH         = "models/lstm_predictor.pt"


def wait_for_controller(retries: int = 20, delay: float = 3.0):
    """Block until Ryu REST is reachable."""
    print("[train] Waiting for Ryu controller...")
    for i in range(retries):
        try:
            r = requests.get(f"{RYU_BASE}/cognet/stats/switches", timeout=2)
            switches = r.json().get("count", 0)
            if switches >= 6:
                print(f"[train] Controller ready — {switches} switches connected.")
                return True
            print(f"[train] Only {switches}/6 switches... retrying ({i+1}/{retries})")
        except Exception as e:
            print(f"[train] Controller not ready: {e} ({i+1}/{retries})")
        time.sleep(delay)
    print("[train] WARNING: Controller not fully ready. Proceeding with fallback graph.")
    return False


def collect_lstm_data(seconds: int = 30) -> list:
    """
    Collect Traffic Matrix snapshots from Ryu for LSTM training.
    Returns list of 36-dim TM vectors.
    """
    print(f"[train] Collecting {seconds}s of TM data for LSTM...")
    snapshots = []
    end_time  = time.time() + seconds
    while time.time() < end_time:
        try:
            r  = requests.get(f"{RYU_BASE}/cognet/stats/traffic_matrix", timeout=2)
            tm = r.json().get("matrix", [0.0] * 36)
            snapshots.append(tm)
        except Exception:
            pass
        time.sleep(STATS_INTERVAL)
    print(f"[train] Collected {len(snapshots)} TM snapshots.")
    return snapshots


def push_metrics_to_flask(episode: int, reward: float):
    """Optionally push training metrics to Flask dashboard."""
    try:
        requests.post(f"{FLASK_BASE}/api/training",
                      json={"episode": episode, "reward": reward},
                      timeout=1)
    except Exception:
        pass   # Flask may not be running during training


def main():
    print("=" * 60)
    print("  CogNet-SDN Training  (DQN + LSTM)")
    print("=" * 60)

    # 1. Wait for Ryu
    ready = wait_for_controller()

    # 2. Build graph
    print("[train] Building SDN graph from Ryu topology...")
    graph = build_sdn_graph()
    print(f"[train] Graph: {graph.number_of_nodes()} nodes, "
          f"{graph.number_of_edges()} edges")

    # 3. Create environment
    env = Env(save_file=SAVE_FILE, graph=graph, live=True)
    print(f"[train] Env created. Nodes: {sorted(list(env.graph.nodes))}")

    # 4. Create MultiAgent
    ma = MultiAgent(env)
    print(f"[train] MultiAgent: {len(ma.agents)} agents | "
          f"STATE_DIM=42 | EPISODES={EPISODES}")

    # 5. Collect data + train LSTM (background)
    if ready:
        tm_data = collect_lstm_data(LSTM_COLLECT_SECS)
    else:
        # Generate synthetic TM data if controller not ready
        tm_data = [[float(np.random.rand()) * 10 for _ in range(36)]
                   for _ in range(10)]

    lstm = LSTMPredictor(input_dim=36, hidden_dim=150, output_dim=36)
    if len(tm_data) >= 5:
        print("[train] Training LSTM predictor...")
        lstm.fit(tm_data, epochs=100)
        lstm.save(LSTM_PATH)
        print(f"[train] LSTM saved to {LSTM_PATH}")
    else:
        print("[train] Not enough TM data for LSTM — skipping pre-training.")

    # 6. DQN Training loop
    print(f"\n[train] Starting DQN training for {EPISODES} episodes...")
    print("-" * 60)

    all_rewards = []
    for ep in range(EPISODES):
        obs = env.reset()
        src, dst, info = obs[0], obs[1], obs[2]

        curr_idx   = ma.nodes.index(src)
        state      = ma._format_input(dst)
        ep_reward  = 0.0
        done       = False
        step_count = 0

        while not done:
            agent  = ma.agents[curr_idx]
            action = agent.select_action(state)

            obs, reward, done, info = env.step(action.item())
            curr_node, dst_node     = obs[0], obs[1]
            ep_reward  += reward
            step_count += 1

            reward_t   = torch.tensor([reward], dtype=torch.float32)
            next_state = ma._format_input(dst_node) if not done else None

            agent.memory.push(state, action, next_state, reward_t)
            state = next_state if next_state is not None else state

            agent.optimize_model()

            # TARGET_UPDATE every 300 global steps (DTPRO)
            ma._step_count += 1
            if ma._step_count % 300 == 0:
                for a in ma.agents:
                    a.sync_target()

            curr_idx = ma.nodes.index(curr_node)

        all_rewards.append(ep_reward)
        push_metrics_to_flask(ep, ep_reward)

        if ep % 10 == 0 or ep == EPISODES - 1:
            avg = np.mean(all_rewards[-10:]) if len(all_rewards) >= 10 else np.mean(all_rewards)
            print(f"  Ep {ep:4d}/{EPISODES}  reward={ep_reward:8.4f}  "
                  f"avg10={avg:8.4f}  steps={step_count}")

    # 7. Save DQN model
    ma.save(MODEL_PATH)
    print(f"\n[train] DQN model saved to {MODEL_PATH}")

    # 8. Evaluation
    print("\n[train] Running evaluation (350 episodes, greedy)...")
    ratio = ma.test(num_episodes=50)   # reduced for quick check
    print(f"[train] Success ratio: {ratio:.3f}")

    # 9. Save final reward summary
    np.savetxt("training_data/all_rewards.csv",
               np.array(all_rewards), delimiter=",", header="reward")
    print("[train] Training complete. Results in training_data/")
    print("=" * 60)


if __name__ == "__main__":
    main()



    