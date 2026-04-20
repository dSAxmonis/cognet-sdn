# """
# CogNet-SDN | link_hop/standard/env.py

# Gym environment adapted from the GitHub Multi-Agent-DQN-Routing repo.
# Changes vs. original:
#   - compute_reward() replaced by live Ryu REST stats
#     (bandwidth, delay, packet-loss → DTPRO reward formula)
#   - Graph is the 6-node SDN topology (built from Ryu topo endpoint)
#   - Fallback to graph-based reward when controller is unreachable
#   - save_file CSV gains bw_mbps / delay_ms / loss_ratio columns
# """

# import time
# import requests
# import networkx as nx
# import gymnasium as gym
# import pandas as pd
# from gymnasium.spaces import MultiDiscrete, Discrete
# from typing import Tuple
# from copy import deepcopy

# from link_hop.util import get_new_route
# from helper.graph import get_neighbors, get_max_neighbors, compute_path_length, compute_flow_value

# # ── DTPRO reward weights (best params from paper) ────────────────────────
# ALPHA    = 0.3    # throughput weight
# BETA     = 1.0    # latency weight
# GAMMA_R  = 1.0    # packet-loss weight

# # ── Ryu REST base URL ─────────────────────────────────────────────────────
RYU_BASE = "http://127.0.0.1:8181"
# TIMEOUT  = 2      # seconds — if controller unreachable, use fallback

# NUM_SWITCHES = 6


# # ── Helper: build NetworkX graph from Ryu topology ─────────────────────────
# def build_sdn_graph() -> nx.Graph:
#     """
#     Pull topology from Ryu REST and return a NetworkX graph.
#     Nodes: 1…6 (switch numbers)
#     Edges: from /cognet/topo, weights from /cognet/stats/links
#     Falls back to a fully-connected 6-node ring if controller is down.
#     """
#     g = nx.Graph()
#     for i in range(1, NUM_SWITCHES + 1):
#         g.add_node(i)

#     try:
#         topo = requests.get(f"{RYU_BASE}/cognet/topo", timeout=TIMEOUT).json()
#         stats = requests.get(f"{RYU_BASE}/cognet/stats/links", timeout=TIMEOUT).json()

#         for link in topo.get("links", []):
#             src_dpid = int(link["src"])
#             dst_dpid = int(link["dst"])
#             src_port = link["src_port"]

#             si = (src_dpid & 0xFF)   # switch number 1-6
#             sj = (dst_dpid & 0xFF)

#             # Pull live bandwidth as edge weight; fall back to 1.0
#             bw = (stats
#                   .get(str(src_dpid), {})
#                   .get(str(src_port), {})
#                   .get("bw_mbps", 1.0))
#             delay = (stats
#                      .get(str(src_dpid), {})
#                      .get(str(src_port), {})
#                      .get("delay_ms", 1.0))
#             loss = (stats
#                     .get(str(src_dpid), {})
#                     .get(str(src_port), {})
#                     .get("loss_ratio", 0.0))

#             # Use delay as edge weight (lower = better), bw as capacity
#             weight   = max(delay, 0.001)
#             capacity = max(bw / 100.0, 0.001)   # normalise to [0,1] range

#             if not g.has_edge(si, sj):
#                 g.add_edge(si, sj,
#                            weight=weight,
#                            capacity=capacity,
#                            delay_ms=delay,
#                            bw_mbps=bw,
#                            loss_ratio=loss)
#     except Exception as e:
#         print(f"[env] Controller unreachable ({e}). Using fallback ring topology.")
#         # Fallback: 6-node ring + centre star (ensures connectivity)
#         edges = [(1,2),(2,3),(3,4),(4,5),(5,6),(6,1),(1,3),(1,4),(2,5),(3,6)]
#         for u, v in edges:
#             g.add_edge(u, v, weight=1.0, capacity=0.5,
#                        delay_ms=1.0, bw_mbps=50.0, loss_ratio=0.0)
#     return g


# # ── Live reward from Ryu REST ──────────────────────────────────────────────
# def _fetch_link_metrics(path: list) -> dict:
#     """
#     For each hop in path, pull per-port stats from Ryu.
#     Returns averaged {bw_mbps, delay_ms, loss_ratio}.
#     """
#     try:
#         stats = requests.get(f"{RYU_BASE}/cognet/stats/links",
#                              timeout=TIMEOUT).json()
#         bws, delays, losses = [], [], []
#         for i in range(len(path) - 1):
#             src_sw = path[i]         # switch number = dpid's last byte
#             # Find the dpid key whose last byte matches src_sw
#             for dpid_str, ports in stats.items():
#                 if (int(dpid_str) & 0xFF) == src_sw:
#                     for port_str, pstat in ports.items():
#                         bws.append(pstat.get("bw_mbps", 0.0))
#                         delays.append(pstat.get("delay_ms", 0.0))
#                         losses.append(pstat.get("loss_ratio", 0.0))
#                     break

#         if not bws:
#             return None

#         return {
#             "bw_mbps"   : sum(bws)    / len(bws),
#             "delay_ms"  : sum(delays) / len(delays),
#             "loss_ratio": sum(losses) / len(losses),
#         }
#     except Exception:
#         return None


# def compute_sdn_reward(graph: nx.Graph, target: int,
#                        path: list) -> Tuple[list, bool]:
#     """
#     DTPRO reward: r = α·W̄ − β·L̄ − γ·PL̄
#     Falls back to graph-weight reward if controller is unreachable.
#     """
#     done = (path[-1] == target)

#     # Guard: path too long (loop detection)
#     max_hops = 3 * NUM_SWITCHES
#     if len(path) > max_hops and not done:
#         return [-1.0, 0.0, 0.0, 0.0], True

#     metrics = _fetch_link_metrics(path)

#     if metrics is not None:
#         W  = metrics["bw_mbps"]
#         L  = metrics["delay_ms"]
#         PL = metrics["loss_ratio"]
#         r  = ALPHA * W - BETA * L - GAMMA_R * PL
#         r  = round(r, 6)
#         return [r, W, L, PL], done
#     else:
#         # ── Fallback: graph-based reward ─────────────────────────────────
#         if done:
#             path_len  = compute_path_length(graph, tuple(path))
#             flow_val  = compute_flow_value(graph, tuple(path))
#             # mimic DTPRO sign convention
#             r = flow_val - path_len
#             return [round(r, 6), path_len, flow_val, 0.0], True
#         else:
#             # Intermediate step: encourage progress toward target
#             try:
#                 dist_now  = nx.astar_path_length(graph, path[-1],  target, weight="weight")
#                 dist_prev = nx.astar_path_length(graph, path[-2], target, weight="weight")
#                 r = dist_prev - dist_now   # positive if getting closer
#             except Exception:
#                 r = -1.0
#             return [round(r, 6)], False


# # ── Gym Environment ────────────────────────────────────────────────────────
# class Env(gym.Env):
#     """
#     SDN-aware Gym environment for CogNet-SDN.
#     State : (current_node, destination_node)
#     Action: index into sorted neighbour list of current_node
#     Reward: DTPRO formula using live Ryu stats (fallback: graph weights)
#     """

#     def __init__(self, save_file: str, graph: nx.Graph = None,
#                  live: bool = True) -> None:
#         """
#         Parameters
#         ----------
#         save_file : path to training CSV
#         graph     : pre-built NetworkX graph (None → fetch from Ryu)
#         live      : if True, refresh graph from Ryu on each reset()
#         """
#         self.live = live
#         self.save_file = save_file

#         # Build initial graph
#         if graph is not None:
#             self.graph = deepcopy(graph)
#         else:
#             self.graph = build_sdn_graph()

#         self.max_neighbors = get_max_neighbors(self.graph)

#         # Gym spaces
#         self.observation_space = MultiDiscrete([self.num_nodes(),
#                                                 self.num_nodes()])
#         self.action_space      = Discrete(self.max_neighbors)
#         self.valid_actions     = [1] * self.max_neighbors

#         # Episode state
#         self.source       = -1
#         self.target       = -1
#         self.current_node = -1
#         self.path         = []
#         self.neighbors    = []
#         self.steps        = 0
#         self.eps          = 0
#         self.episode_reward = 0.0

#         # Initialise CSV
#         with open(self.save_file, "w") as f:
#             f.write("episode,steps,reward,bw_mbps,delay_ms,loss_ratio\n")

#         with open("training_data/step_data.csv", "w") as f:
#             f.write("steps,reward\n")

#     # ── Gym API ────────────────────────────────────────────────────────────
#     def step(self, action: int, train_mode: bool = True):
#         # Resolve action → next node
#         try:
#             next_node = self.neighbors[action]
#         except IndexError:
#             # Invalid action — stay, penalise
#             self._refresh_neighbors()
#             return ([self.current_node, self.target],
#                     -1.0, False,
#                     {'valid_actions': self.valid_actions})

#         self.path.append(next_node)
#         self.current_node = next_node
#         self.steps += 1

#         rewards, done = compute_sdn_reward(self.graph, self.target, self.path)
#         self.episode_reward += round(rewards[0], 3)

#         self._record(rewards, done, train_mode)
#         self._refresh_neighbors()

#         return ([self.current_node, self.target],
#                 rewards[0], done,
#                 {'valid_actions': self.valid_actions})

#     def reset(self) -> Tuple:
#         return self._reset()

#     def render(self, mode: str = 'human', close: bool = False) -> None:
#         pass

#     # ── Internal helpers ───────────────────────────────────────────────────
#     def _reset(self) -> Tuple:
#         # Optionally refresh live topology
#         if self.live:
#             try:
#                 self.graph = build_sdn_graph()
#                 self.max_neighbors = get_max_neighbors(self.graph)
#                 self.valid_actions = [1] * self.max_neighbors
#             except Exception:
#                 pass   # keep old graph

#         self.source, self.target = get_new_route(self.graph)
#         self.current_node = self.source
#         self.path         = [self.source]
#         self.episode_reward = 0.0
#         self._refresh_neighbors()

#         return (self.source, self.target,
#                 {'valid_actions': self.valid_actions})

#     def _refresh_neighbors(self):
#         self.neighbors = sorted(list(self.graph.neighbors(self.current_node)))
#         for i in range(len(self.valid_actions)):
#             self.valid_actions[i] = 1 if i < len(self.neighbors) else 0

#     # CORRECT — always log step reward, plus episode summary when done
# def _record(self, rewards, done, train_mode):
#     with open("training_data/step_data.csv", "a") as f:
#         f.write(f"{self.steps},{rewards[0]}\n")
    
#     if not done:
#         return

#     self.eps += 1
#     bw    = rewards[1] if len(rewards) > 1 else 0.0
#     delay = rewards[2] if len(rewards) > 2 else 0.0
#     loss  = rewards[3] if len(rewards) > 3 else 0.0

#     with open(self.save_file, "a") as f:
#         f.write(f"{self.eps},{self.steps},"
#                 f"{round(self.episode_reward, 4)},"
#                 f"{round(bw, 4)},{round(delay, 4)},{round(loss, 6)}\n")
#         self.eps += 1
#         bw    = rewards[1] if len(rewards) > 1 else 0.0
#         delay = rewards[2] if len(rewards) > 2 else 0.0
#         loss  = rewards[3] if len(rewards) > 3 else 0.0

#         with open(self.save_file, "a") as f:
#             f.write(f"{self.eps},{self.steps},"
#                     f"{round(self.episode_reward, 4)},"
#                     f"{round(bw, 4)},{round(delay, 4)},{round(loss, 6)}\n")

#     def num_nodes(self) -> int:
#         return len(self.graph.nodes)


"""
CogNet-SDN | link_hop/standard/env.py
Adapted from Multi-Agent-DQN-Routing-master.
Changes: compute_reward replaced with live Ryu REST stats (DTPRO formula).
"""

import time
import requests
import networkx as nx
import gymnasium as gym
import pandas as pd
from gymnasium.spaces import MultiDiscrete, Discrete
from typing import Tuple
from copy import deepcopy
from random import choice

from link_hop.util import get_new_route
from helper.graph import get_neighbors, get_max_neighbors, compute_path_length, compute_flow_value

ALPHA   = 0.3
BETA    = 1.0
GAMMA_R = 1.0

RYU_BASE     = "http://127.0.0.1:8181"
TIMEOUT      = 2
NUM_SWITCHES = 6


def build_sdn_graph() -> nx.Graph:
    g = nx.Graph()
    for i in range(1, NUM_SWITCHES + 1):
        g.add_node(i)
    try:
        topo  = requests.get(f"{RYU_BASE}/cognet/topo",        timeout=TIMEOUT).json()
        stats = requests.get(f"{RYU_BASE}/cognet/stats/links", timeout=TIMEOUT).json()
        for link in topo.get("links", []):
            src_dpid = int(link["src"])
            dst_dpid = int(link["dst"])
            src_port = link["src_port"]
            si = src_dpid & 0xFF
            sj = dst_dpid & 0xFF
            bw    = stats.get(str(src_dpid), {}).get(str(src_port), {}).get("bw_mbps",    1.0)
            delay = stats.get(str(src_dpid), {}).get(str(src_port), {}).get("delay_ms",   1.0)
            loss  = stats.get(str(src_dpid), {}).get(str(src_port), {}).get("loss_ratio", 0.0)
            weight   = max(delay, 0.001)
            capacity = max(bw / 100.0, 0.001)
            if not g.has_edge(si, sj):
                g.add_edge(si, sj, weight=weight, capacity=capacity,
                           delay_ms=delay, bw_mbps=bw, loss_ratio=loss)
    except Exception as e:
        print(f"[env] Controller unreachable ({e}). Using fallback star topology.")
        edges = [(1,2),(1,3),(1,4),(1,5),(1,6)]
        for u, v in edges:
            g.add_edge(u, v, weight=1.0, capacity=0.5,
                       delay_ms=1.0, bw_mbps=50.0, loss_ratio=0.0)
    return g


def _fetch_link_metrics(path: list) -> dict:
    try:
        stats = requests.get(f"{RYU_BASE}/cognet/stats/links", timeout=TIMEOUT).json()
        bws, delays, losses = [], [], []
        for i in range(len(path) - 1):
            src_sw = path[i]
            for dpid_str, ports in stats.items():
                if (int(dpid_str) & 0xFF) == src_sw:
                    for port_str, pstat in ports.items():
                        bws.append(pstat.get("bw_mbps",    0.0))
                        delays.append(pstat.get("delay_ms",   0.0))
                        losses.append(pstat.get("loss_ratio", 0.0))
                    break
        if not bws:
            return None
        return {
            "bw_mbps"   : sum(bws)    / len(bws),
            "delay_ms"  : sum(delays) / len(delays),
            "loss_ratio": sum(losses) / len(losses),
        }
    except Exception:
        return None


def compute_sdn_reward(graph: nx.Graph, target: int, path: list) -> Tuple[list, bool]:
    done     = (path[-1] == target)
    max_hops = 3 * NUM_SWITCHES
    if len(path) > max_hops and not done:
        return [-1.0, 0.0, 0.0, 0.0], True

    metrics = _fetch_link_metrics(path)
    if metrics is not None:
        W  = metrics["bw_mbps"] / 100.0        # normalise 0-1
        L  = metrics["delay_ms"] / 10.0         # normalise (10ms = 1.0)
        PL = metrics["loss_ratio"]               # already 0-1
        r  = ALPHA * W - BETA * L - GAMMA_R * PL
        if done:
            r += 1.0                             # bonus for reaching target
        return [round(r, 6), W, L, PL], done
    else:
        # Fallback: graph-based
        if done:
            path_len = compute_path_length(graph, tuple(path))
            flow_val = compute_flow_value(graph, tuple(path))
            r = flow_val - path_len
            return [round(r, 6), path_len, flow_val, 0.0], True
        else:
            try:
                dist_now  = nx.astar_path_length(graph, path[-1], target, weight="weight")
                dist_prev = nx.astar_path_length(graph, path[-2], target, weight="weight")
                r = dist_prev - dist_now
            except Exception:
                r = -1.0
            return [round(r, 6), 0.0, 0.0, 0.0], False


class Env(gym.Env):
    def __init__(self, save_file: str, graph: nx.Graph = None, live: bool = True) -> None:
        self.live      = live
        self.save_file = save_file

        self.graph = deepcopy(graph) if graph is not None else build_sdn_graph()
        self.max_neighbors = get_max_neighbors(self.graph)

        self.observation_space = MultiDiscrete([self.num_nodes(), self.num_nodes()])
        self.action_space      = Discrete(self.max_neighbors)
        self.valid_actions     = [1] * self.max_neighbors

        self.source       = -1
        self.target       = -1
        self.current_node = -1
        self.path         = []
        self.neighbors    = []
        self.steps        = 0
        self.eps          = 0
        self.episode_reward = 0.0

        with open(self.save_file, "w") as f:
            f.write("episode,steps,reward,bw_mbps,delay_ms,loss_ratio\n")
        with open("training_data/step_data.csv", "w") as f:
            f.write("steps,reward\n")

    def step(self, action: int, train_mode: bool = True):
        try:
            next_node = self.neighbors[action]
        except IndexError:
            self._refresh_neighbors()
            return ([self.current_node, self.target], -1.0, False,
                    {'valid_actions': self.valid_actions})

        self.path.append(next_node)
        self.current_node = next_node
        self.steps += 1

        rewards, done = compute_sdn_reward(self.graph, self.target, self.path)
        self.episode_reward += round(rewards[0], 3)

        self._record(rewards, done, train_mode)
        self._refresh_neighbors()

        return ([self.current_node, self.target], rewards[0], done,
                {'valid_actions': self.valid_actions})

    def reset(self) -> Tuple:
        if self.live:
            try:
                self.graph = build_sdn_graph()
                self.max_neighbors = get_max_neighbors(self.graph)
                self.valid_actions = [1] * self.max_neighbors
            except Exception:
                pass

        self.source, self.target = get_new_route(self.graph)
        self.current_node = self.source
        self.path         = [self.source]
        self.episode_reward = 0.0
        self._refresh_neighbors()
        return (self.source, self.target, {'valid_actions': self.valid_actions})

    def render(self, mode: str = 'human', close: bool = False) -> None:
        pass

    def _refresh_neighbors(self):
        self.neighbors = sorted(list(self.graph.neighbors(self.current_node)))
        for i in range(len(self.valid_actions)):
            self.valid_actions[i] = 1 if i < len(self.neighbors) else 0

    def _record(self, rewards, done, train_mode):
        with open("training_data/step_data.csv", "a") as f:
            f.write(f"{self.steps},{rewards[0]}\n")
        if not done:
            return
        self.eps += 1
        bw    = rewards[1] if len(rewards) > 1 else 0.0
        delay = rewards[2] if len(rewards) > 2 else 0.0
        loss  = rewards[3] if len(rewards) > 3 else 0.0
        with open(self.save_file, "a") as f:
            f.write(f"{self.eps},{self.steps},"
                    f"{round(self.episode_reward, 4)},"
                    f"{round(bw, 4)},{round(delay, 4)},{round(loss, 6)}\n")

    def num_nodes(self) -> int:
        return len(self.graph.nodes)