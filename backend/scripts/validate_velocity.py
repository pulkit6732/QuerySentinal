"""
validate_velocity.py — Reproducible validation of the Semantic Velocity detector.

Answers the judge's killer question: "what's your false-positive rate?"

Method: pull real clean movie plots from sample_mflix, embed them, build many
synthetic 8-hour windows, and measure how the centroid-drift z-score behaves:
  - CLEAN windows   -> how often does it FALSELY alarm? (false-positive rate)
  - POISONED windows (last hour = crypto/spam) -> how often does it CORRECTLY
    alarm? (true-positive rate)

This mirrors detect.py compute_semantic_velocity() exactly (unit-normalized
hourly centroids, cosine distance, z-score vs prior-hours baseline, spike z>3.0).

Run: cd backend && python -m scripts.validate_velocity
"""
import sys, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
logging.disable(logging.CRITICAL)

import numpy as np
from agents.mongo_fn_tools import _embed_text_local
from db import client
from config import SOURCE_DB

SPIKE_Z = 3.0
PER_HOUR = 8
HOURS = 8
TRIALS = 200


def _cos_dist(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


def _window_z(centroids):
    dists = [_cos_dist(centroids[i], centroids[i + 1]) for i in range(len(centroids) - 1)]
    cur, hist = dists[-1], dists[:-1]
    return (cur - np.mean(hist)) / max(np.std(hist), 1e-7)


def _centroid(bucket):
    c = np.mean(np.stack(bucket), axis=0)
    n = np.linalg.norm(c)
    return c / n if n > 1e-9 else c


def main():
    print("Loading real clean embeddings from sample_mflix.movies...")
    docs = list(client[SOURCE_DB].movies.aggregate([
        {"$match": {"plot": {"$exists": True, "$ne": None}}},
        {"$sample": {"size": 800}},
        {"$project": {"plot": 1}},
    ]))
    clean = [np.array(_embed_text_local(d["plot"]), dtype=np.float64)
             for d in docs if isinstance(d.get("plot"), str) and len(d["plot"]) > 20][:600]
    print(f"  {len(clean)} clean embeddings ready")

    spam = [
        "URGENT INVESTMENT OPPORTUNITY cryptocurrency trading profits guaranteed",
        "FOREX TRADING SECRETS professional fund managers hidden strategy",
        "CRYPTO AIRDROP ALERT you have been selected to receive free tokens",
        "BINARY OPTIONS MASTERY COURSE learn professional trading",
        "PUMP AND DUMP ALERT NETWORK exclusive membership serious traders",
    ]
    poison = [np.array(_embed_text_local(s), dtype=np.float64) for s in spam]

    rng = np.random.default_rng(42)

    # CLEAN trials -> false positives
    fp = 0
    for _ in range(TRIALS):
        cents = []
        for _h in range(HOURS):
            bucket = [clean[rng.integers(len(clean))] for _ in range(PER_HOUR)]
            cents.append(_centroid(bucket))
        if _window_z(cents) > SPIKE_Z:
            fp += 1

    # POISONED trials -> true positives (last hour poisoned)
    tp = 0
    for _ in range(TRIALS):
        cents = []
        for h in range(HOURS):
            src = poison if h == HOURS - 1 else clean
            bucket = [src[rng.integers(len(src))] for _ in range(PER_HOUR)]
            cents.append(_centroid(bucket))
        if _window_z(cents) > SPIKE_Z:
            tp += 1

    fpr = fp / TRIALS * 100
    tpr = tp / TRIALS * 100
    print()
    print("=" * 52)
    print("  SEMANTIC VELOCITY DETECTOR — VALIDATION SCORECARD")
    print("=" * 52)
    print(f"  Windows tested:    {TRIALS} clean + {TRIALS} poisoned (8h each)")
    print(f"  Embeddings:        {len(clean)} real sample_mflix plots")
    print(f"  Spike threshold:   z > {SPIKE_Z}")
    print("  " + "-" * 48)
    print(f"  True-positive rate (poison caught):   {tpr:5.1f}%")
    print(f"  False-positive rate (clean alarmed):  {fpr:5.1f}%")
    print("  " + "-" * 48)
    verdict = "STRONG separation" if (tpr >= 95 and fpr <= 5) else "needs tuning"
    print(f"  Verdict: {verdict}")
    print("=" * 52)


if __name__ == "__main__":
    main()
