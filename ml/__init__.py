"""ML ranker — Phase 4 of priority-ml-system.

Trains a LightGBM regression model that predicts a Post's expected
engagement_score from features extracted at queue-build time. The
trained model is later (Phase 5) called by the bot every day to write
ml_priority_score back to the backend (map_points / city_events).

Layout:
    feature_extractor.py — Post → numeric feature vector (Pandas DataFrame)
    ranker.py            — train / save / load / predict (LightGBM)
    ab_tester.py         — Phase 6, A/B challenger model evaluation
    __init__.py          — public re-exports

Convention: trained models live in `data/ml/ranker_v{N}.pkl`. The latest
symlink `data/ml/ranker_current.pkl` points at the active model. Bot
endpoints load `ranker_current` lazily so a fresh training cycle takes
effect on the next predict() call without a full restart.
"""
from __future__ import annotations

from .feature_extractor import build_feature_frame, FEATURE_COLUMNS
from .ranker import (
    train_ranker_weekly,
    load_current_ranker,
    predict_scores,
    Ranker,
)

__all__ = [
    "build_feature_frame",
    "FEATURE_COLUMNS",
    "train_ranker_weekly",
    "load_current_ranker",
    "predict_scores",
    "Ranker",
]
