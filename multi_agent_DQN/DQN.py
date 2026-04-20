"""
CogNet-SDN | multi_agent_DQN/DQN.py

Adapted from GitHub Multi-Agent-DQN-Routing-master.
Key changes vs original:
  - State = 36-dim Traffic Matrix (6×6) + 6-dim one-hot destination = 42-dim
    (DTPRO uses 48-dim TM; we approximate with TM+dest encoding)
  - DQN: 2 dense layers (DTPRO Table 1), not 4
  - Hyperparams match DTPRO best params:
      lr=0.01, gamma=0.95, batch=32, memory=500, target_update=300 steps
  - MultiAgent._format_input() fetches live TM from Ryu REST
  - BATCH_SIZE, GAMMA, etc. set to DTPRO values
"""

import math
import random
import numpy as np
import requests
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from collections import namedtuple

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ── DTPRO best hyperparameters (Bouzidi JNCA 2021, Table 1) ───────────────
BATCH_SIZE    = 32
GAMMA         = 0.95
EPS_START     = 1.0
EPS_END       = 0.2          # DTPRO final exploration rate
EPS_DECAY     = 1000
TARGET_UPDATE = 300          # steps between target network sync
LEARNING_RATE = 0.01         # DTPRO learning rate (Adam)
MEMORY_SIZE   = 500          # DTPRO replay memory size

NUM_SWITCHES  = 6
TM_DIM        = NUM_SWITCHES * NUM_SWITCHES   # 36
STATE_DIM     = TM_DIM + NUM_SWITCHES         # 42 (TM + one-hot dest)

RYU_BASE      = "http://127.0.0.1:8181"
TIMEOUT       = 2

Transition = namedtuple('Transition',
                        ('state', 'action', 'next_state', 'reward'))


# ── Replay Memory ──────────────────────────────────────────────────────────
class ReplayMemory:
    def __init__(self, capacity):
        self.capacity = capacity
        self.memory   = []
        self.position = 0

    def push(self, *args):
        if len(self.memory) < self.capacity:
            self.memory.append(None)
        self.memory[self.position] = Transition(*args)
        self.position = (self.position + 1) % self.capacity

    def sample(self, batch_size):
        return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)


# ── DQN Network — 2 dense layers per DTPRO Table 1 ────────────────────────
class DQN(nn.Module):
    """
    DTPRO architecture: 2 dense layers.
    Input:  STATE_DIM (42) = 36-dim TM + 6-dim one-hot destination
    Output: n_actions (number of neighbours for this switch node)
    """
    def __init__(self, input_dim: int, output_dim: int):
        super().__init__()
        hidden = max(output_dim * 2, 24)   # ≥24 neurons per TTDQSHA Table V
        self.fc1 = nn.Linear(input_dim, hidden)
        self.fc2 = nn.Linear(hidden, hidden)
        self.out = nn.Linear(hidden, output_dim)

    def forward(self, x):
        x = x.float()
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.out(x)          # raw Q-values (no softmax — standard DQN)


# ── Single Agent ───────────────────────────────────────────────────────────
class Agent:
    def __init__(self, input_dim: int, n_actions: int):
        self.n_actions   = n_actions
        self.steps_done  = 0

        self.policy_net  = DQN(input_dim, n_actions).to(device)
        self.target_net  = DQN(input_dim, n_actions).to(device)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()

        # DTPRO uses Adam with lr=0.01
        self.optimizer   = optim.Adam(self.policy_net.parameters(),
                                      lr=LEARNING_RATE)
        self.memory      = ReplayMemory(MEMORY_SIZE)
        self.episode_durations = []
        self.loss_history      = []

    def select_action(self, state):
        """Epsilon-greedy with linear decay."""
        eps = EPS_END + (EPS_START - EPS_END) * \
              math.exp(-1.0 * self.steps_done / EPS_DECAY)
        self.steps_done += 1

        if random.random() > eps:
            with torch.no_grad():
                return self.policy_net(state).max(1)[1].view(1, 1)
        return torch.tensor([[random.randrange(self.n_actions)]],
                            device=device, dtype=torch.long)

    def predict(self, state):
        """Greedy action (test-time)."""
        with torch.no_grad():
            return self.policy_net(state).max(1)[1].view(1, 1)

    def optimize_model(self):
        if len(self.memory) < BATCH_SIZE:
            return

        transitions = self.memory.sample(BATCH_SIZE)
        batch       = Transition(*zip(*transitions))

        non_final_mask = torch.tensor(
            [s is not None for s in batch.next_state],
            device=device, dtype=torch.bool)
        non_final_next = torch.cat(
            [s for s in batch.next_state if s is not None])

        state_batch  = torch.cat(batch.state)
        action_batch = torch.cat(batch.action)
        reward_batch = torch.cat(batch.reward)

        # Q(s, a) from online network
        state_action_values = self.policy_net(state_batch).gather(
            1, action_batch)

        # V(s') from target network
        next_state_values = torch.zeros(BATCH_SIZE, device=device)
        next_state_values[non_final_mask] = \
            self.target_net(non_final_next).max(1)[0].detach()

        # Bellman target
        expected_values = (next_state_values * GAMMA) + reward_batch

        # Huber loss
        loss = F.smooth_l1_loss(state_action_values,
                                expected_values.unsqueeze(1))

        self.optimizer.zero_grad()
        loss.backward()
        # Gradient clipping (standard DQN practice)
        for p in self.policy_net.parameters():
            p.grad.data.clamp_(-1, 1)
        self.optimizer.step()
        self.loss_history.append(loss.item())

    def sync_target(self):
        """Copy policy → target network (called every TARGET_UPDATE steps)."""
        self.target_net.load_state_dict(self.policy_net.state_dict())


# ── Traffic Matrix fetcher ─────────────────────────────────────────────────
def _fetch_traffic_matrix() -> list:
    """
    Fetch the 36-dim Traffic Matrix from Ryu REST.
    Returns a flat list of 36 floats (normalised to [0,1]).
    Falls back to zeros if controller is unreachable.
    """
    try:
        r = requests.get(f"{RYU_BASE}/cognet/stats/traffic_matrix",
                         timeout=TIMEOUT)
        data = r.json()
        tm   = data.get("matrix", [0.0] * TM_DIM)
        # Normalise: divide by max observed BW (100 Mbps ceiling)
        max_bw = max(max(tm), 1.0)
        return [v / max_bw for v in tm]
    except Exception:
        return [0.0] * TM_DIM


# ── Multi-Agent ────────────────────────────────────────────────────────────
class MultiAgent:
    """
    One DQN agent per switch node.
    State: 42-dim vector = 36-dim TM (live from Ryu) + 6-dim one-hot destination
    Action: index into sorted neighbour list of the current switch
    """

    def __init__(self, env):
        self.env        = env
        self.nodes      = sorted(list(env.graph.nodes))   # e.g. [1,2,3,4,5,6]
        self.num_nodes  = len(self.nodes)
        self.agents     = []
        self._step_count = 0     # global step counter for TARGET_UPDATE
        self._initialize()

        # Scale EPS_DECAY with number of agents (from original GitHub)
        global EPS_DECAY
        EPS_DECAY = EPS_DECAY * self.num_nodes

    def _initialize(self):
        for node in self.nodes:
            n_neighbours = len(list(self.env.graph.neighbors(node)))
            self.agents.append(
                Agent(input_dim=STATE_DIM, n_actions=n_neighbours))

    def _format_input(self, destination: int) -> torch.Tensor:
        """
        Build 42-dim state tensor:
          [0:36]  = normalised 6×6 Traffic Matrix (live from Ryu)
          [36:42] = one-hot encoding of destination switch
        """
        tm = _fetch_traffic_matrix()   # 36 floats

        one_hot = [0.0] * self.num_nodes
        if destination in self.nodes:
            one_hot[self.nodes.index(destination)] = 1.0

        state = torch.tensor([tm + one_hot], dtype=torch.float32,
                             device=device)
        return state

    def run(self, episodes: int = 120):
        """Training loop — 120 episodes per DTPRO Table 1."""
        for ep in range(episodes):
            obs  = self.env.reset()
            src, dst, info = obs[0], obs[1], obs[2]
            curr_agent_idx  = self.nodes.index(src)
            state           = self._format_input(dst)
            ep_reward       = 0.0
            done            = False

            while not done:
                agent  = self.agents[curr_agent_idx]
                action = agent.select_action(state)

                obs, reward, done, info = self.env.step(action.item())
                curr_node, dst_node     = obs[0], obs[1]
                ep_reward += reward

                reward_t    = torch.tensor([reward], device=device,
                                           dtype=torch.float32)
                next_state  = self._format_input(dst_node) if not done else None

                agent.memory.push(state, action, next_state, reward_t)
                state = next_state if next_state is not None else state

                agent.optimize_model()

                # TARGET_UPDATE every 300 global steps (DTPRO Table 1)
                self._step_count += 1
                if self._step_count % TARGET_UPDATE == 0:
                    for a in self.agents:
                        a.sync_target()

                curr_agent_idx = self.nodes.index(curr_node)

            if ep % 10 == 0:
                print(f"[Episode {ep:4d}] reward={ep_reward:.4f}  "
                      f"steps_done={self.agents[0].steps_done}")

    def test(self, num_episodes: int = 350) -> float:
        """Evaluation loop — greedy policy, no exploration."""
        good, bad = 0, 0

        for _ in range(num_episodes):
            obs  = self.env.reset()
            src, dst, info = obs[0], obs[1], obs[2]
            curr_node      = src
            curr_idx       = self.nodes.index(src)
            state          = self._format_input(dst)
            done           = False
            reward         = 0.0

            while not done:
                agent  = self.agents[curr_idx]
                action = agent.predict(state)

                # Safety clamp: ensure action is valid for this node
                n_nbrs = len(list(self.env.graph.neighbors(curr_node)))
                if action.item() >= n_nbrs:
                    action = torch.tensor(
                        [[random.randint(0, max(n_nbrs - 1, 0))]],
                        device=device)

                obs, reward, done, info = self.env.step(action.item())
                curr_node, dst_node     = obs[0], obs[1]
                curr_idx = self.nodes.index(curr_node)
                state    = self._format_input(dst_node)

            # Count success: reward == 1.01 → arrived on optimal path
            if reward > 0.0:
                good += 1
            else:
                bad += 1

        ratio = good / float(good + bad) if (good + bad) > 0 else 0.0
        print(f"[Test] Routed: {ratio:.3f}  good={good}  bad={bad}")
        return ratio

    def save(self, path: str = "models/cognet_dqn.pt"):
        """Save all agent policy networks."""
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch.save(
            {f"agent_{i}": self.agents[i].policy_net.state_dict()
             for i in range(len(self.agents))},
            path)
        print(f"[MultiAgent] Saved to {path}")

    def load(self, path: str = "models/cognet_dqn.pt"):
        """Load all agent policy networks."""
        ckpt = torch.load(path, map_location=device)
        for i, agent in enumerate(self.agents):
            key = f"agent_{i}"
            if key in ckpt:
                agent.policy_net.load_state_dict(ckpt[key])
                agent.target_net.load_state_dict(ckpt[key])
        print(f"[MultiAgent] Loaded from {path}")