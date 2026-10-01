"""Multi-criteria decision making: weights (AHP / entropy / equal / manual), aggregation, classes, sensitivity."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Saaty random consistency index for n = 1..10
_RI = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}


def ahp_matrix(names: Sequence[str], judgements: Dict[str, float]) -> np.ndarray:
    """Build a reciprocal pairwise matrix from 'a>b': intensity judgements (missing pairs = 1)."""
    n = len(names)
    idx = {nm: i for i, nm in enumerate(names)}
    m = np.ones((n, n), dtype=float)
    for key, val in (judgements or {}).items():
        if ">" not in key:
            continue
        a, b = (s.strip() for s in key.split(">", 1))
        if a in idx and b in idx and a != b:
            v = float(val)
            if v <= 0:
                raise ValueError(f"AHP judgement must be positive: {key}={val}")
            m[idx[a], idx[b]] = v
            m[idx[b], idx[a]] = 1.0 / v
    return m


def ahp_weights(matrix: np.ndarray) -> Tuple[np.ndarray, float, float]:
    """Principal-eigenvector weights, lambda_max and consistency ratio (CR < 0.10 is acceptable)."""
    n = matrix.shape[0]
    if n == 1:
        return np.array([1.0]), 1.0, 0.0
    vals, vecs = np.linalg.eig(matrix)
    k = int(np.argmax(vals.real))
    w = np.abs(vecs[:, k].real)
    w = w / w.sum()
    lam = float(vals[k].real)
    ci = (lam - n) / (n - 1)
    ri = _RI.get(n, 1.49)
    cr = float(ci / ri) if ri > 0 else 0.0
    return w, lam, cr


def entropy_weights(stack: np.ndarray) -> np.ndarray:
    """Shannon-entropy objective weights. stack: (n_criteria, n_cells) in 0-1, NaN-free."""
    n, m = stack.shape
    eps = 1e-12
    p = stack + eps
    p = p / p.sum(axis=1, keepdims=True)
    e = -(p * np.log(p)).sum(axis=1) / np.log(m)
    d = 1.0 - e
    if d.sum() <= 0:
        return np.full(n, 1.0 / n)
    return d / d.sum()


def compute_weights(names: List[str], stack_valid: np.ndarray, wcfg: dict):
    """Return (weights dict, info dict)."""
    method = wcfg.get("method", "ahp")
    info: Dict[str, object] = {"method": method}
    if method == "equal":
        w = np.full(len(names), 1.0 / len(names))
    elif method == "manual":
        raw = np.array([float(wcfg.get("manual", {}).get(nm, 0.0)) for nm in names])
        if raw.sum() <= 0:
            raise ValueError("Manual weights must be positive for the active criteria: " + ", ".join(names))
        w = raw / raw.sum()
    elif method == "entropy":
        w = entropy_weights(stack_valid)
    elif method == "ahp":
        mat = ahp_matrix(names, wcfg.get("ahp_judgements", {}))
        w, lam, cr = ahp_weights(mat)
        info.update({"lambda_max": lam, "consistency_ratio": cr, "consistent": bool(cr < 0.10),
                     "matrix": mat.round(4).tolist()})
    else:
        raise ValueError(f"Unknown weighting method: {method}")
    return {nm: float(v) for nm, v in zip(names, w)}, info


def wlc(stack: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted linear combination. stack: (k, ...) -> (...)"""
    w = np.asarray(weights, dtype=np.float64).reshape((-1,) + (1,) * (stack.ndim - 1))
    return (stack * w).sum(axis=0)


def topsis(stack: np.ndarray, weights: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """TOPSIS relative closeness to the 'most hazardous' ideal point (0-1, higher = riskier).

    stack: (k, H, W) normalised 0-1 criteria, valid: (H, W) bool mask.
    """
    k = stack.shape[0]
    w = np.asarray(weights, dtype=np.float64)
    v = np.stack([stack[i] * w[i] for i in range(k)])
    ideal = np.array([v[i][valid].max() for i in range(k)])  # worst hazard
    anti = np.array([v[i][valid].min() for i in range(k)])  # best (least hazard)
    d_ideal = np.sqrt(sum((v[i] - ideal[i]) ** 2 for i in range(k)))
    d_anti = np.sqrt(sum((v[i] - anti[i]) ** 2 for i in range(k)))
    denom = d_ideal + d_anti
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(denom > 0, d_anti / denom, 0.0)
    return out


def class_breaks(index: np.ndarray, method: str, breaks: Sequence[float], quantiles: Sequence[float]) -> List[float]:
    """Four break values separating the five classes."""
    if method == "manual" or method == "equal":
        b = sorted(float(x) for x in breaks)
        if len(b) != 4:
            raise ValueError("classification.breaks must contain exactly 4 values")
        return b
    if method == "quantile":
        f = index[np.isfinite(index)]
        if f.size == 0:
            return [0.2, 0.4, 0.6, 0.8]
        return [float(v) for v in np.quantile(f, list(quantiles))]
    raise ValueError(f"Unknown classification method: {method}")


def classify(index: np.ndarray, breaks: Sequence[float]) -> np.ndarray:
    """uint8 classes 1..5 (0 = no data)."""
    cls = np.digitize(np.nan_to_num(index, nan=-1.0), bins=list(breaks)).astype(np.uint8) + 1
    cls[~np.isfinite(index)] = 0
    return cls


def monte_carlo_sensitivity(stack: np.ndarray, base_w: np.ndarray, valid: np.ndarray, breaks: Sequence[float],
                            high_min_class: int, aggregation: str = "wlc", runs: int = 40,
                            perturbation: float = 0.25, seed: int = 42):
    """Perturb weights +/- `perturbation` (relative), re-run the index, report stability.

    Returns (std_map, summary). Uses a streaming mean/variance so memory stays small.
    """
    rng = np.random.default_rng(seed)
    shape = stack.shape[1:]
    mean = np.zeros(shape, dtype=np.float64)
    m2 = np.zeros(shape, dtype=np.float64)
    high_count = np.zeros(shape, dtype=np.int32)
    base_idx = wlc(stack, base_w) if aggregation == "wlc" else topsis(stack, base_w, valid)
    base_high = classify(np.where(valid, base_idx, np.nan), breaks) >= high_min_class
    for r in range(1, runs + 1):
        factors = 1.0 + rng.uniform(-perturbation, perturbation, size=len(base_w))
        w = np.asarray(base_w) * factors
        w = w / w.sum()
        idx = wlc(stack, w) if aggregation == "wlc" else topsis(stack, w, valid)
        delta = idx - mean
        mean += delta / r
        m2 += delta * (idx - mean)
        high_count += (classify(np.where(valid, idx, np.nan), breaks) >= high_min_class)
    std = np.sqrt(m2 / max(runs - 1, 1)).astype(np.float32)
    std[~valid] = np.nan
    stable = None
    if base_high.any():
        frac = high_count[base_high] / runs
        stable = float((frac >= 0.8).mean() * 100.0)
    summary = {
        "runs": runs,
        "perturbation_pct": perturbation * 100,
        "mean_index_std": float(np.nanmean(std)) if valid.any() else None,
        "max_index_std": float(np.nanmax(std)) if valid.any() else None,
        "high_risk_cells_stable_pct": stable,
    }
    return std, summary
