"""LightGBM ranker — Phase 4 of priority-ml-system.

Trains a regression model that learns to predict per-post engagement
score from the features built by feature_extractor. Persists models
under data/ml/ranker_v{N}.pkl with `ranker_current.pkl` symlink.

Why LightGBM and not sklearn GBR
- 5-10× faster training and prediction on CPU-only Docker.
- Native categorical handling means we can later drop the manual
  one-hot in feature_extractor.
- Scales to the eventual 10000+ training rows we expect after a year
  of data.

Model lifecycle
- `train_ranker_weekly()` — Saturday 03:00 cron. Pulls last 30 days of
  matured (window=168h) PostEngagement rows + features, runs 5-fold CV,
  trains final model on full set, saves new version.
- `load_current_ranker()` — lazy-loaded singleton used by
  `predict_scores()` for the daily score-back cron (Phase 5) and the
  /ml/score-candidate endpoint.
- Models are forward-compatible: predict() will fall back to a
  uniform 0.0 score if the model can't be loaded (no LightGBM, no
  trained file yet, version mismatch). Score-back cron then writes 0
  → backend formula's COALESCE keeps the rule-based ranking intact.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = pathlib.Path("data/ml")
CURRENT_SYMLINK = "ranker_current.pkl"
META_SUFFIX = ".meta.json"


@dataclass
class RankerMeta:
    """Per-version training metadata persisted alongside .pkl."""
    version: int
    trained_at: str
    train_rows: int
    train_window_days: int
    label_window_hours: int
    cv_rmse_mean: float
    cv_rmse_std: float
    cv_r2_mean: float
    feature_columns: list[str]


class Ranker:
    """Wrapper around the trained LightGBM model + metadata."""

    def __init__(self, model: Any, meta: RankerMeta):
        self.model = model
        self.meta = meta

    def predict(self, X) -> list[float]:
        """Predict engagement scores for a feature DataFrame.

        Reorders columns to match training, fills missing with 0.0.
        """
        import pandas as pd
        cols = self.meta.feature_columns
        for c in cols:
            if c not in X.columns:
                X[c] = 0
        X_aligned = X[cols]
        return [float(v) for v in self.model.predict(X_aligned)]


def _model_dir() -> pathlib.Path:
    p = pathlib.Path(os.environ.get("ML_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _next_version(model_dir: pathlib.Path) -> int:
    versions = []
    for f in model_dir.glob("ranker_v*.pkl"):
        m = re.match(r"ranker_v(\d+)\.pkl$", f.name)
        if m:
            versions.append(int(m.group(1)))
    return max(versions, default=0) + 1


def _save_ranker(ranker: Ranker, model_dir: pathlib.Path) -> pathlib.Path:
    """Persist .pkl + .meta.json + update ranker_current.pkl symlink."""
    import joblib
    pkl_path = model_dir / f"ranker_v{ranker.meta.version}.pkl"
    meta_path = pkl_path.with_suffix(".pkl" + META_SUFFIX)
    joblib.dump(ranker.model, pkl_path)
    with meta_path.open("w") as f:
        json.dump(asdict(ranker.meta), f, indent=2)

    current = model_dir / CURRENT_SYMLINK
    if current.exists() or current.is_symlink():
        current.unlink()
    try:
        current.symlink_to(pkl_path.name)
    except OSError:
        # On platforms without symlink permission (Windows w/o admin),
        # fall back to a small marker file pointing at the latest version.
        current.write_text(pkl_path.name)
    logger.info("ml.ranker: saved v%d → %s", ranker.meta.version, pkl_path)
    return pkl_path


_cached_ranker: Ranker | None = None
_cached_mtime: float = 0.0


def load_current_ranker(model_dir: pathlib.Path | None = None) -> Ranker | None:
    """Lazy singleton loader. Reloads if the symlink target changed."""
    global _cached_ranker, _cached_mtime
    md = model_dir or _model_dir()
    current = md / CURRENT_SYMLINK
    if not current.exists():
        return None
    try:
        target = current.resolve() if current.is_symlink() else (md / current.read_text().strip())
    except OSError:
        return None
    if not target.exists():
        return None
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    if _cached_ranker is not None and mtime == _cached_mtime:
        return _cached_ranker
    try:
        import joblib
        model = joblib.load(target)
        meta_path = target.with_suffix(".pkl" + META_SUFFIX)
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        ranker = Ranker(
            model=model,
            meta=RankerMeta(
                version=int(meta.get("version", 0)),
                trained_at=meta.get("trained_at", ""),
                train_rows=int(meta.get("train_rows", 0)),
                train_window_days=int(meta.get("train_window_days", 0)),
                label_window_hours=int(meta.get("label_window_hours", 168)),
                cv_rmse_mean=float(meta.get("cv_rmse_mean", 0.0)),
                cv_rmse_std=float(meta.get("cv_rmse_std", 0.0)),
                cv_r2_mean=float(meta.get("cv_r2_mean", 0.0)),
                feature_columns=list(meta.get("feature_columns") or []),
            ),
        )
        _cached_ranker = ranker
        _cached_mtime = mtime
        logger.info("ml.ranker: loaded v%d (rmse=%.2f r²=%.3f rows=%d)",
                    ranker.meta.version, ranker.meta.cv_rmse_mean,
                    ranker.meta.cv_r2_mean, ranker.meta.train_rows)
        return ranker
    except Exception:
        logger.warning("ml.ranker: failed to load %s", target, exc_info=True)
        return None


def predict_scores(features) -> list[float]:
    """Predict scores for a feature DataFrame, returning [] if no model."""
    ranker = load_current_ranker()
    if ranker is None:
        return [0.0] * len(features)
    try:
        return ranker.predict(features)
    except Exception:
        logger.warning("ml.ranker: predict failed", exc_info=True)
        return [0.0] * len(features)


async def train_ranker_weekly(
    *,
    label_window_hours: int = 168,
    train_window_days: int = 30,
    n_splits: int = 5,
    model_dir: pathlib.Path | None = None,
) -> Ranker | None:
    """Saturday 03:00 cron entrypoint. Returns the new Ranker or None.

    Steps:
      1. Pull post_engagement rows at the chosen label window from the
         last `train_window_days` days.
      2. Build feature DataFrame via feature_extractor.
      3. 5-fold CV: report mean RMSE / R² in logs.
      4. Train final model on full set; save under ranker_v{N+1}.pkl.

    Skips silently if dependencies aren't installed (LightGBM not
    on the host) or if there are <30 training rows (model would be
    junk and over-fit).
    """
    md = model_dir or _model_dir()
    try:
        import lightgbm as lgb
        import numpy as np
        from sklearn.model_selection import KFold
        from sklearn.metrics import mean_squared_error, r2_score
    except ImportError as e:
        logger.warning("ml.ranker: missing ML dependency (%s) — skipping training", e)
        return None

    from .feature_extractor import build_feature_frame, FEATURE_COLUMNS

    X, y = await build_feature_frame(min_window_hours=label_window_hours)

    if len(X) < 30:
        logger.info("ml.ranker: %d training rows < 30, skipping (need more data)",
                    len(X))
        return None

    rmses, r2s = [], []
    kf = KFold(n_splits=min(n_splits, max(2, len(X) // 5)),
               shuffle=True, random_state=42)
    for fold, (train_idx, test_idx) in enumerate(kf.split(X)):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
        booster = lgb.LGBMRegressor(
            n_estimators=200,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=5,
            random_state=42,
            verbose=-1,
        )
        booster.fit(X_train, y_train)
        preds = booster.predict(X_test)
        rmse = float(np.sqrt(mean_squared_error(y_test, preds)))
        r2 = float(r2_score(y_test, preds))
        rmses.append(rmse)
        r2s.append(r2)
        logger.info("ml.ranker fold %d: rmse=%.2f r²=%.3f", fold, rmse, r2)

    cv_rmse_mean = float(np.mean(rmses))
    cv_rmse_std = float(np.std(rmses))
    cv_r2_mean = float(np.mean(r2s))

    final = lgb.LGBMRegressor(
        n_estimators=300,
        learning_rate=0.04,
        num_leaves=15,
        min_child_samples=5,
        random_state=42,
        verbose=-1,
    )
    final.fit(X, y)

    version = _next_version(md)
    meta = RankerMeta(
        version=version,
        trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        train_rows=int(len(X)),
        train_window_days=train_window_days,
        label_window_hours=label_window_hours,
        cv_rmse_mean=cv_rmse_mean,
        cv_rmse_std=cv_rmse_std,
        cv_r2_mean=cv_r2_mean,
        feature_columns=list(FEATURE_COLUMNS),
    )
    ranker = Ranker(model=final, meta=meta)
    _save_ranker(ranker, md)

    # Bust the in-process cache so the next predict() picks up the new model.
    global _cached_ranker, _cached_mtime
    _cached_ranker = None
    _cached_mtime = 0.0

    logger.info(
        "ml.ranker: trained v%d on %d rows, cv RMSE=%.2f±%.2f cv R²=%.3f",
        version, len(X), cv_rmse_mean, cv_rmse_std, cv_r2_mean,
    )
    return ranker
