"""
CogNet-SDN | ml/lstm_predictor.py

LSTM-based traffic predictor.
Architecture matches DTPRO (Bouzidi 2021):
  - 150 hidden nodes
  - 9000 training epochs  (reduced to 500 here for speed; set epochs=9000 for paper)
  - Adam lr=0.01
  - ReLU activation
  - Input/Output: 36-dim Traffic Matrix (6×6)
  - Prediction interval Pi = 5 seconds
"""

import os
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

# ── DTPRO LSTM hyperparameters ─────────────────────────────────────────────
HIDDEN_DIM   = 150
LEARNING_RATE = 0.01
WINDOW_SIZE  = 5     # look-back window (time steps)
PRED_HORIZON = 1     # predict 1 step ahead (Pi = 5s interval)


class LSTMPredictor(nn.Module):
    """
    Single-layer LSTM + linear output head.
    Input:  [batch, seq_len, input_dim]
    Output: [batch, output_dim]
    """
    def __init__(self, input_dim: int = 36,
                 hidden_dim: int = HIDDEN_DIM,
                 output_dim: int = 36,
                 num_layers: int = 1):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers

        self.lstm = nn.LSTM(input_dim, hidden_dim,
                            num_layers=num_layers,
                            batch_first=True)
        self.relu = nn.ReLU()
        self.fc   = nn.Linear(hidden_dim, output_dim)

        self._scaler_min = None
        self._scaler_max = None

    def forward(self, x):
        # x: [batch, seq_len, input_dim]
        out, _ = self.lstm(x)
        # Use last time step
        out = self.relu(out[:, -1, :])
        return self.fc(out)

    # ── Normalisation ────────────────────────────────────────────────────
    def _fit_scaler(self, data: np.ndarray):
        self._scaler_min = data.min(axis=0)
        self._scaler_max = data.max(axis=0)
        # Avoid division by zero
        self._scaler_max = np.where(
            self._scaler_max == self._scaler_min,
            self._scaler_min + 1e-8,
            self._scaler_max)

    def _scale(self, data: np.ndarray) -> np.ndarray:
        return (data - self._scaler_min) / (self._scaler_max - self._scaler_min)

    def _unscale(self, data: np.ndarray) -> np.ndarray:
        return data * (self._scaler_max - self._scaler_min) + self._scaler_min

    # ── Training ─────────────────────────────────────────────────────────
    def fit(self, tm_snapshots: list,
            epochs: int = 500,
            window: int = WINDOW_SIZE,
            batch_size: int = 16,
            verbose: bool = True):
        """
        Train on a list of 36-dim TM vectors.

        Parameters
        ----------
        tm_snapshots : list of lists, shape (T, 36)
        epochs       : training epochs (DTPRO uses 9000)
        window       : look-back window size
        """
        data = np.array(tm_snapshots, dtype=np.float32)
        if len(data) < window + PRED_HORIZON + 1:
            print(f"[LSTM] Not enough data ({len(data)} samples). "
                  f"Need at least {window + PRED_HORIZON + 2}.")
            return

        self._fit_scaler(data)
        scaled = self._scale(data)

        # Build supervised sequences
        X, Y = [], []
        for i in range(len(scaled) - window - PRED_HORIZON + 1):
            X.append(scaled[i : i + window])
            Y.append(scaled[i + window])

        X = torch.tensor(np.array(X), dtype=torch.float32)
        Y = torch.tensor(np.array(Y), dtype=torch.float32)

        dataset = TensorDataset(X, Y)
        loader  = DataLoader(dataset, batch_size=min(batch_size, len(dataset)),
                             shuffle=True)

        optimizer = optim.Adam(self.parameters(), lr=LEARNING_RATE)
        criterion = nn.MSELoss()

        self.train()
        for ep in range(1, epochs + 1):
            total_loss = 0.0
            for xb, yb in loader:
                pred = self(xb)
                loss = criterion(pred, yb)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

            if verbose and ep % max(epochs // 10, 1) == 0:
                print(f"  [LSTM] epoch {ep:5d}/{epochs}  "
                      f"loss={total_loss/len(loader):.6f}")

        self.eval()
        print("[LSTM] Training complete.")

    # ── Prediction ───────────────────────────────────────────────────────
    def predict(self, recent_tms: list) -> np.ndarray:
        """
        Predict next TM given a window of recent TMs.

        Parameters
        ----------
        recent_tms : list of 36-dim vectors, length == WINDOW_SIZE

        Returns
        -------
        np.ndarray of shape (36,) — predicted next TM
        """
        if self._scaler_min is None:
            return np.zeros(36, dtype=np.float32)

        data   = np.array(recent_tms, dtype=np.float32)
        scaled = self._scale(data)
        x      = torch.tensor(scaled[np.newaxis], dtype=torch.float32)  # [1,W,36]

        self.eval()
        with torch.no_grad():
            pred_scaled = self(x).numpy()[0]

        return self._unscale(pred_scaled)

    def predict_congestion(self, recent_tms: list,
                           threshold: float = 80.0) -> dict:
        """
        Returns predicted TM and flags links above threshold Mbps.
        """
        pred = self.predict(recent_tms)
        congested = {}
        for idx, val in enumerate(pred):
            si = idx // 6 + 1
            sj = idx  % 6 + 1
            if val >= threshold:
                congested[f"s{si}->s{sj}"] = round(float(val), 3)
        return {"predicted_tm": pred.tolist(), "congested": congested}

    # ── Persistence ──────────────────────────────────────────────────────
    def save(self, path: str = "models/lstm_predictor.pt"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch.save({
            "state_dict" : self.state_dict(),
            "scaler_min" : self._scaler_min,
            "scaler_max" : self._scaler_max,
            "hidden_dim" : self.hidden_dim,
            "num_layers" : self.num_layers,
        }, path)
        print(f"[LSTM] Saved to {path}")

    @classmethod
    def load(cls, path: str = "models/lstm_predictor.pt",
             input_dim: int = 36, output_dim: int = 36):
        ckpt = torch.load(path, map_location="cpu")
        model = cls(input_dim=input_dim,
                    hidden_dim=ckpt.get("hidden_dim", HIDDEN_DIM),
                    output_dim=output_dim,
                    num_layers=ckpt.get("num_layers", 1))
        model.load_state_dict(ckpt["state_dict"])
        model._scaler_min = ckpt.get("scaler_min")
        model._scaler_max = ckpt.get("scaler_max")
        model.eval()
        print(f"[LSTM] Loaded from {path}")
        return model


# ── Standalone data collector (used in train_sdn.py) ──────────────────────
def collect_training_data(ryu_base: str = "http://127.0.0.1:8181",
                          duration: int = 60,
                          interval: int = 5) -> list:
    import time, requests
    snapshots = []
    end = time.time() + duration
    while time.time() < end:
        try:
            r  = requests.get(f"{ryu_base}/cognet/stats/traffic_matrix", timeout=2)
            tm = r.json().get("matrix", [0.0] * 36)
            snapshots.append(tm)
        except Exception:
            pass
        time.sleep(interval)
    return snapshots


if __name__ == "__main__":
    # Quick smoke test
    print("Testing LSTMPredictor...")
    import random
    fake_data = [[random.random() * 50 for _ in range(36)] for _ in range(30)]
    model = LSTMPredictor()
    model.fit(fake_data, epochs=50, verbose=True)
    pred  = model.predict(fake_data[-WINDOW_SIZE:])
    print(f"Predicted TM (first 6 values): {pred[:6].round(3)}")
    model.save("/tmp/test_lstm.pt")
    m2 = LSTMPredictor.load("/tmp/test_lstm.pt")
    print("Load OK:", m2.predict(fake_data[-WINDOW_SIZE:])[:3].round(3))