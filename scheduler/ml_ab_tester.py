"""A/B tester — Phase 6 of priority-ml-system.

Lets the bot run two LightGBM rankers in parallel — `current` (production)
and `challenger` (experiment). Every Post is assigned to one of the two
cohorts via a deterministic hash of post_id, so the same Post always
gets the same cohort if re-scored.

Promotion logic
- Every 14 days, compare mean 7-day engagement_score of posts assigned
  to `current` vs `challenger`.
- If challenger mean is ≥5% higher AND a two-sided independent t-test
  rejects "no difference" at p < 0.05, promote: rename
  challenger_current.pkl → ranker_current.pkl, then train a fresh
  challenger from updated data.
- If neither cohort has ≥30 published posts in the window, defer
  evaluation (not enough power).

Dashboard exposure
- `summarise_ab_state()` returns a small dict the daily-report HTML
  can render: active model version, challenger version, cohort sizes,
  cohort means, p-value, last decision.
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CHALLENGER_SYMLINK = "ranker_challenger.pkl"
DECISION_LOG = "ab_decisions.json"

# Promotion thresholds. 5% lift floor + p<0.05 keeps us from chasing
# noise on small samples. Adjust after we have ~3 cycles of evidence.
PROMOTION_LIFT_FLOOR = 0.05
PROMOTION_P_THRESHOLD = 0.05
MIN_COHORT_SIZE_FOR_PROMOTION = 30
EVAL_WINDOW_DAYS = 14
LABEL_WINDOW_HOURS = 168  # 7-day engagement → promotion label


@dataclass
class ABState:
    """Snapshot for the daily report."""
    active_version: int | None
    challenger_version: int | None
    current_cohort: int
    challenger_cohort: int
    current_mean: float
    challenger_mean: float
    p_value: float | None
    lift_pct: float | None
    last_decision_at: str | None
    last_decision: str | None


DEFAULT_MODEL_DIR = pathlib.Path("data/ml")


def _ml_dir() -> pathlib.Path:
    """Locate the data/ml model directory.

    Phase 4 (`ml/ranker.py`) owns the canonical helper; we duplicate
    a minimal version here so the A/B tester is importable even on a
    bot deploy where Phase 4 hasn't shipped yet (cron logs "skipping"
    in that case rather than ImportError-crashing the whole scheduler).
    """
    import os
    p = pathlib.Path(os.environ.get("ML_MODEL_DIR", str(DEFAULT_MODEL_DIR)))
    p.mkdir(parents=True, exist_ok=True)
    return p


def cohort_for(post_id: int) -> str:
    """Deterministic 50/50 split by post_id parity.

    Hash-based so re-scoring the same post always lands in the same
    cohort (no leakage between current and challenger windows).
    """
    return "challenger" if (post_id % 2 == 1) else "current"


def _load_decision_log() -> list[dict[str, Any]]:
    path = _ml_dir() / DECISION_LOG
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return []


def _append_decision(entry: dict[str, Any]) -> None:
    log = _load_decision_log()
    log.append(entry)
    path = _ml_dir() / DECISION_LOG
    path.write_text(json.dumps(log[-50:], indent=2))  # keep last 50 only


async def _cohort_scores(window_days: int) -> dict[str, list[float]]:
    """Pull engagement scores grouped by cohort for the eval window."""
    from sqlalchemy import select

    from db.database import async_session
    from db.models import PostEngagement

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=window_days)
    by_cohort: dict[str, list[float]] = {"current": [], "challenger": []}

    async with async_session() as session:
        rows = (await session.execute(
            select(PostEngagement.post_id, PostEngagement.score)
            .where(
                PostEngagement.window_hours == LABEL_WINDOW_HOURS,
                PostEngagement.collected_at >= cutoff,
            )
        )).all()
    for row in rows:
        by_cohort[cohort_for(int(row.post_id))].append(float(row.score))
    return by_cohort


def _compare_cohorts(a: list[float], b: list[float]) -> tuple[float, float]:
    """Return (lift_pct, p_value). p_value is None if scipy missing."""
    import statistics
    if not a or not b:
        return 0.0, None  # type: ignore[return-value]
    mean_a = statistics.fmean(a)
    mean_b = statistics.fmean(b)
    if mean_a == 0:
        lift = 0.0
    else:
        lift = (mean_b - mean_a) / mean_a
    try:
        from scipy import stats as _stats
        _, p = _stats.ttest_ind(a, b, equal_var=False)
        return lift, float(p)
    except ImportError:
        return lift, None  # type: ignore[return-value]


def _promote_challenger() -> int | None:
    """Rename challenger_current.pkl → ranker_current.pkl. Return new version."""
    md = _ml_dir()
    challenger = md / CHALLENGER_SYMLINK
    if not challenger.exists():
        return None
    try:
        target_name = (challenger.resolve().name
                       if challenger.is_symlink()
                       else challenger.read_text().strip())
    except OSError:
        return None
    current_link = md / "ranker_current.pkl"
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    try:
        current_link.symlink_to(target_name)
    except OSError:
        current_link.write_text(target_name)
    # Bust the in-process model cache so next predict() loads the
    # newly-promoted model.
    try:
        from ml import ranker as _ranker_mod
        _ranker_mod._cached_ranker = None  # type: ignore[attr-defined]
        _ranker_mod._cached_mtime = 0.0  # type: ignore[attr-defined]
    except ImportError:
        pass
    import re
    m = re.match(r"ranker_v(\d+)\.pkl$", target_name)
    return int(m.group(1)) if m else None


async def train_challenger() -> int | None:
    """Train a new challenger model. Same algorithm as the production
    weekly trainer, written under data/ml/ranker_v{N+1}.pkl, then a
    `ranker_challenger.pkl` symlink is set to point at it.

    Returns the new version number or None if training was skipped
    (insufficient data, missing dependency).
    """
    try:
        from ml.ranker import train_ranker_weekly, _model_dir, _save_ranker  # type: ignore
    except ImportError:
        logger.info("[ab.train_challenger] ml package missing, skipping")
        return None

    ranker = await train_ranker_weekly()
    if ranker is None:
        return None
    md = _model_dir()
    pkl_name = f"ranker_v{ranker.meta.version}.pkl"
    challenger_link = md / CHALLENGER_SYMLINK
    if challenger_link.exists() or challenger_link.is_symlink():
        challenger_link.unlink()
    try:
        challenger_link.symlink_to(pkl_name)
    except OSError:
        challenger_link.write_text(pkl_name)
    logger.info("[ab.train_challenger] new challenger v%d", ranker.meta.version)
    return ranker.meta.version


async def evaluate_promotion(window_days: int = EVAL_WINDOW_DAYS) -> ABState:
    """Run the 14-day cohort comparison and promote if challenger wins.

    Returns the ABState snapshot regardless of decision so the daily
    report can render it. Decision history is persisted under
    data/ml/ab_decisions.json (last 50 entries).
    """
    cohorts = await _cohort_scores(window_days)
    n_cur = len(cohorts["current"])
    n_chal = len(cohorts["challenger"])
    lift, p = _compare_cohorts(cohorts["current"], cohorts["challenger"])

    decision = "no_decision"
    decision_reason = ""
    promoted_version: int | None = None
    new_challenger_version: int | None = None

    if min(n_cur, n_chal) < MIN_COHORT_SIZE_FOR_PROMOTION:
        decision_reason = (
            f"insufficient power: current={n_cur} challenger={n_chal} "
            f"(need ≥{MIN_COHORT_SIZE_FOR_PROMOTION} each)"
        )
    elif lift < PROMOTION_LIFT_FLOOR:
        decision_reason = (
            f"challenger lift {lift*100:.1f}% < floor {PROMOTION_LIFT_FLOOR*100:.0f}%"
        )
    elif p is None:
        decision_reason = "scipy missing — cannot run t-test"
    elif p > PROMOTION_P_THRESHOLD:
        decision_reason = f"p={p:.3f} > {PROMOTION_P_THRESHOLD} (not significant)"
    else:
        promoted_version = _promote_challenger()
        if promoted_version is not None:
            decision = "promoted"
            decision_reason = (
                f"promoted v{promoted_version}: lift={lift*100:.1f}% p={p:.4f}"
            )
            new_challenger_version = await train_challenger()
        else:
            decision_reason = "no challenger to promote"

    md = _ml_dir()
    active_v = _read_version(md / "ranker_current.pkl")
    challenger_v = _read_version(md / CHALLENGER_SYMLINK)
    import statistics
    state = ABState(
        active_version=active_v,
        challenger_version=challenger_v,
        current_cohort=n_cur,
        challenger_cohort=n_chal,
        current_mean=float(statistics.fmean(cohorts["current"])) if cohorts["current"] else 0.0,
        challenger_mean=float(statistics.fmean(cohorts["challenger"])) if cohorts["challenger"] else 0.0,
        p_value=p,
        lift_pct=lift * 100 if lift else 0.0,
        last_decision_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        last_decision=f"{decision}: {decision_reason}",
    )
    _append_decision({**asdict(state),
                      "promoted_version": promoted_version,
                      "new_challenger_version": new_challenger_version})
    logger.info(
        "[ab.evaluate] %s — current(n=%d, mean=%.2f) vs challenger(n=%d, mean=%.2f) "
        "lift=%.1f%% p=%s",
        decision, n_cur, state.current_mean, n_chal, state.challenger_mean,
        state.lift_pct, "n/a" if p is None else f"{p:.4f}",
    )
    return state


def _read_version(symlink: pathlib.Path) -> int | None:
    if not symlink.exists():
        return None
    try:
        target = symlink.resolve().name if symlink.is_symlink() else symlink.read_text().strip()
    except OSError:
        return None
    import re
    m = re.match(r"ranker_v(\d+)\.pkl$", target)
    return int(m.group(1)) if m else None


def summarise_ab_state() -> ABState | None:
    """Latest decision (or None if log empty). Used by the daily report."""
    log = _load_decision_log()
    if not log:
        return None
    last = log[-1]
    return ABState(
        active_version=last.get("active_version"),
        challenger_version=last.get("challenger_version"),
        current_cohort=int(last.get("current_cohort") or 0),
        challenger_cohort=int(last.get("challenger_cohort") or 0),
        current_mean=float(last.get("current_mean") or 0.0),
        challenger_mean=float(last.get("challenger_mean") or 0.0),
        p_value=last.get("p_value"),
        lift_pct=last.get("lift_pct"),
        last_decision_at=last.get("last_decision_at"),
        last_decision=last.get("last_decision"),
    )


async def ab_evaluate_and_train(_when: float | None = None) -> dict[str, Any]:
    """Combined cron: evaluate promotion → train challenger if promoted.

    Hooked from main.py at Saturday 03:30 (just after Phase 4
    train_ranker_weekly at 03:00 finishes).
    """
    state = await evaluate_promotion()
    return asdict(state)
