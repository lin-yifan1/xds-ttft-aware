from __future__ import annotations

from dataclasses import asdict
from time import perf_counter
from typing import Any

import numpy as np
import pandas as pd

from config import (
    DOMINANCE_RATIO,
    EPSILON,
    LENGTH_SIGNAL_JOINT_MIN_RATIO,
    LENGTH_SIGNAL_MIN_RATIO,
    SCORE_WEIGHTS_BY_SCOPE,
    LatencyDetectorConfig,
)


REQUIRED_COLUMNS = [
    "domain_id",
    "rpm",
    "tpm",
    "ttft_avg",
    "tpot_avg",
    "prompt_tokens",
    "completion_tokens",
    "collect_time_std",
]

NUMERIC_COLUMNS = [
    "rpm",
    "tpm",
    "ttft_avg",
    "tpot_avg",
    "prompt_tokens",
    "completion_tokens",
]


def validate_latency_input(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    out = df.copy()
    out["domain_id"] = out["domain_id"].fillna("").astype(str).str.strip()
    out = out[out["domain_id"] != ""].copy()

    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    s = out["collect_time_std"].astype(str).str.strip().str.replace(r"\s+", " ", regex=True)
    parsed = pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    fallback_mask = parsed.isna() & s.ne("") & s.notna()
    if fallback_mask.any():
        parsed.loc[fallback_mask] = pd.to_datetime(s.loc[fallback_mask], errors="coerce")
    out["collect_time_std_parsed"] = parsed
    out = out.dropna(subset=["collect_time_std_parsed"]).copy()
    if out.empty:
        raise ValueError("No valid rows remained after parsing collect_time_std.")
    return out


def _weighted_average_1d(values: np.ndarray, weights: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not valid.any():
        return 0.0
    return float(np.sum(values[valid] * weights[valid]) / np.sum(weights[valid]))


def _weighted_average_ignore_zero_1d(values: np.ndarray, weights: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    valid = np.isfinite(values) & np.isfinite(weights) & (weights > 0) & (values != 0)
    if not valid.any():
        return 0.0
    return float(np.sum(values[valid] * weights[valid]) / np.sum(weights[valid]))


def _collapse_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    weights = out["rpm"].where(np.isfinite(out["rpm"]) & (out["rpm"] > 0), 0.0)

    for col in ("ttft_avg", "tpot_avg"):
        valid = np.isfinite(out[col]) & (out[col] != 0) & (weights > 0)
        out[f"_{col}_weight"] = weights.where(valid, 0.0)
        out[f"_{col}_weighted"] = (out[col] * weights).where(valid, 0.0)

    for col in ("prompt_tokens", "completion_tokens"):
        valid = np.isfinite(out[col]) & (weights > 0)
        out[f"_{col}_weight"] = weights.where(valid, 0.0)
        out[f"_{col}_weighted"] = (out[col] * weights).where(valid, 0.0)

    grouped = (
        out.groupby(["domain_id", "collect_time_std_parsed"], sort=True)
        .agg(
            rpm=("rpm", "sum"),
            tpm=("tpm", "sum"),
            ttft_weight=("_ttft_avg_weight", "sum"),
            ttft_weighted=("_ttft_avg_weighted", "sum"),
            tpot_weight=("_tpot_avg_weight", "sum"),
            tpot_weighted=("_tpot_avg_weighted", "sum"),
            prompt_weight=("_prompt_tokens_weight", "sum"),
            prompt_weighted=("_prompt_tokens_weighted", "sum"),
            completion_weight=("_completion_tokens_weight", "sum"),
            completion_weighted=("_completion_tokens_weighted", "sum"),
        )
        .reset_index()
    )

    for out_col, weighted_col, weight_col in (
        ("ttft_avg", "ttft_weighted", "ttft_weight"),
        ("tpot_avg", "tpot_weighted", "tpot_weight"),
        ("prompt_tokens", "prompt_weighted", "prompt_weight"),
        ("completion_tokens", "completion_weighted", "completion_weight"),
    ):
        denom = grouped[weight_col].to_numpy(dtype=float)
        numer = grouped[weighted_col].to_numpy(dtype=float)
        grouped[out_col] = np.divide(numer, denom, out=np.zeros_like(numer, dtype=float), where=denom > 0)

    return grouped[
        [
            "domain_id",
            "collect_time_std_parsed",
            "rpm",
            "tpm",
            "ttft_avg",
            "tpot_avg",
            "prompt_tokens",
            "completion_tokens",
        ]
    ]


def _infer_time_freq(times: pd.Series) -> pd.Timedelta:
    ordered = pd.Series(sorted(pd.unique(times.dropna())))
    if len(ordered) < 2:
        return pd.Timedelta(hours=1)
    diffs = ordered.diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return pd.Timedelta(hours=1)
    return diffs.min()


def _build_metric_matrix(df: pd.DataFrame, user_ids: list[str], time_index: pd.DatetimeIndex, column: str) -> np.ndarray:
    pivot = (
        df.pivot_table(index="domain_id", columns="collect_time_std_parsed", values=column, aggfunc="first")
        .reindex(index=user_ids, columns=time_index)
        .fillna(0.0)
    )
    return pivot.to_numpy(dtype=float)


def _rolling_mean(arr: np.ndarray, window: int, min_periods: int) -> np.ndarray:
    s = pd.Series(arr.astype(float)).shift(1)
    return s.rolling(window=window, min_periods=min_periods).mean().to_numpy()


def _mask_to_events(mask: np.ndarray, merge_gap: int) -> list[tuple[int, int]]:
    idx = np.where(np.asarray(mask, dtype=bool))[0]
    if idx.size == 0:
        return []
    segments: list[tuple[int, int]] = []
    start = int(idx[0])
    prev = int(idx[0])
    for h in idx[1:]:
        h = int(h)
        if h <= prev + 1 + int(max(merge_gap, 0)):
            prev = h
            continue
        segments.append((start, prev))
        start = prev = h
    segments.append((start, prev))
    return segments


def _mark_runs(mask: np.ndarray, min_run: int) -> np.ndarray:
    mask = np.asarray(mask, dtype=bool)
    if min_run <= 1:
        return mask.copy()
    out = np.zeros_like(mask, dtype=bool)
    start: int | None = None
    for idx, val in enumerate(mask):
        if val and start is None:
            start = idx
        if (not val) and start is not None:
            if idx - start >= min_run:
                out[start:idx] = True
            start = None
    if start is not None and (len(mask) - start) >= min_run:
        out[start:] = True
    return out


def _cap_events(
    events: list[tuple[int, int]],
    max_events: int,
    system_ttft: np.ndarray,
    system_tpot: np.ndarray,
    cfg: LatencyDetectorConfig,
) -> list[tuple[int, int]]:
    if not events or max_events <= 0 or len(events) <= max_events:
        return events
    scored: list[tuple[tuple[int, int], float]] = []
    for window in events:
        a, b = window
        ttft_ratio = float(np.nanmax(system_ttft[a : b + 1] / max(cfg.ttft_sla, EPSILON)))
        tpot_ratio = float(np.nanmax(system_tpot[a : b + 1] / max(cfg.tpot_sla, EPSILON)))
        scored.append((window, max(ttft_ratio, tpot_ratio)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [window for window, _ in scored[:max_events]]


def _scope_for_window(
    sys_anom_ttft: np.ndarray,
    sys_anom_tpot: np.ndarray,
    start_hour: int,
    end_hour: int,
) -> str:
    ttft_active = bool(np.asarray(sys_anom_ttft, dtype=bool)[start_hour : end_hour + 1].any())
    tpot_active = bool(np.asarray(sys_anom_tpot, dtype=bool)[start_hour : end_hour + 1].any())
    if ttft_active and tpot_active:
        return "both"
    if ttft_active:
        return "ttft_only"
    return "tpot_only"


def _score_weights(scope: str) -> tuple[float, float, float, float]:
    return SCORE_WEIGHTS_BY_SCOPE.get(scope, SCORE_WEIGHTS_BY_SCOPE["both"])


def _safe_ratio(values: np.ndarray) -> np.ndarray:
    total = float(np.sum(values))
    if total <= 0:
        return np.zeros_like(values, dtype=float)
    return np.asarray(values, dtype=float) / total


def _combined_local_score(
    rpm_excess: np.ndarray,
    tpm_excess: np.ndarray,
    prompt_delta_excess: np.ndarray,
    completion_delta_excess: np.ndarray,
    weights: tuple[float, float, float, float],
) -> np.ndarray:
    w_rpm, w_tpm, w_prompt, w_completion = weights
    score = np.zeros_like(rpm_excess, dtype=float)
    totals = [
        float(np.sum(rpm_excess)),
        float(np.sum(tpm_excess)),
        float(np.sum(prompt_delta_excess)),
        float(np.sum(completion_delta_excess)),
    ]
    if totals[0] > 0:
        score += w_rpm * (rpm_excess / totals[0])
    if totals[1] > 0:
        score += w_tpm * (tpm_excess / totals[1])
    if totals[2] > 0:
        score += w_prompt * (prompt_delta_excess / totals[2])
    if totals[3] > 0:
        score += w_completion * (completion_delta_excess / totals[3])
    return score


def _length_signal(prompt_ratio: float, completion_ratio: float, rpm_ratio: float, tpm_ratio: float) -> str:
    prompt_ratio = float(prompt_ratio)
    completion_ratio = float(completion_ratio)
    traffic_ratio = float(max(rpm_ratio, tpm_ratio))
    if prompt_ratio <= 0 and completion_ratio <= 0:
        return "traffic_dominant"
    if (
        prompt_ratio >= LENGTH_SIGNAL_MIN_RATIO
        and completion_ratio >= LENGTH_SIGNAL_MIN_RATIO
        and (prompt_ratio + completion_ratio) >= max(traffic_ratio, LENGTH_SIGNAL_JOINT_MIN_RATIO)
    ):
        return "io_shift_joint"
    if prompt_ratio >= max(completion_ratio * DOMINANCE_RATIO, traffic_ratio):
        return "input_shift_dominant"
    if completion_ratio >= max(prompt_ratio * DOMINANCE_RATIO, traffic_ratio):
        return "output_shift_dominant"
    if max(prompt_ratio, completion_ratio) >= traffic_ratio:
        return "length_shift_mixed"
    return "traffic_dominant"


def _positive_shift_score(values: np.ndarray, baseline: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    baseline = np.asarray(baseline, dtype=float)
    delta = np.clip(values - baseline, 0.0, None)
    baseline_abs = np.abs(baseline)
    valid = np.isfinite(delta) & np.isfinite(baseline_abs)
    if not valid.any():
        return 0.0
    delta_sum = float(np.sum(delta[valid]))
    if delta_sum <= 0:
        return 0.0
    baseline_sum = float(np.sum(baseline_abs[valid]))
    return float(delta_sum / (baseline_sum + delta_sum + EPSILON))


def _dominance_label(primary_score: float, secondary_score: float, primary_label: str, secondary_label: str, mixed_label: str) -> str:
    primary_score = float(primary_score)
    secondary_score = float(secondary_score)
    if primary_score <= 0 and secondary_score <= 0:
        return "unclear"
    if primary_score >= secondary_score * DOMINANCE_RATIO:
        return primary_label
    if secondary_score >= primary_score * DOMINANCE_RATIO:
        return secondary_label
    return mixed_label


def _event_driver_diagnosis(
    scope: str,
    rpm_shift_score: float,
    tpm_shift_score: float,
    prompt_shift_score: float,
    completion_shift_score: float,
) -> dict[str, Any]:
    if scope == "ttft_only":
        traffic_score = float(rpm_shift_score)
        length_score = float(prompt_shift_score)
        signal = _dominance_label(
            traffic_score,
            length_score,
            "rpm_rise_dominant",
            "input_shift_dominant",
            "rpm_input_mixed",
        )
    elif scope == "tpot_only":
        traffic_score = float(tpm_shift_score)
        length_score = float(completion_shift_score)
        signal = _dominance_label(
            traffic_score,
            length_score,
            "tpm_rise_dominant",
            "output_shift_dominant",
            "tpm_output_mixed",
        )
    else:
        traffic_score = float(0.5 * rpm_shift_score + 0.5 * tpm_shift_score)
        length_score = float(0.5 * prompt_shift_score + 0.5 * completion_shift_score)
        signal = _dominance_label(
            traffic_score,
            length_score,
            "traffic_family_dominant",
            "length_family_dominant",
            "traffic_length_mixed",
        )

    total = traffic_score + length_score
    if total > 0:
        traffic_ratio = traffic_score / total
        length_ratio = length_score / total
    else:
        traffic_ratio = 0.0
        length_ratio = 0.0
    return {
        "driver_signal": signal,
        "traffic_driver_score": float(traffic_score),
        "length_driver_score": float(length_score),
        "traffic_driver_ratio": float(traffic_ratio),
        "length_driver_ratio": float(length_ratio),
        "rpm_shift_score": float(rpm_shift_score),
        "tpm_shift_score": float(tpm_shift_score),
        "prompt_shift_score": float(prompt_shift_score),
        "completion_shift_score": float(completion_shift_score),
    }


def _culprit_driver_signal(
    scope: str,
    rpm_ratio: float,
    tpm_ratio: float,
    prompt_ratio: float,
    completion_ratio: float,
) -> str:
    if scope == "ttft_only":
        return _dominance_label(float(rpm_ratio), float(prompt_ratio), "rpm_rise_dominant", "input_shift_dominant", "rpm_input_mixed")
    if scope == "tpot_only":
        return _dominance_label(float(tpm_ratio), float(completion_ratio), "tpm_rise_dominant", "output_shift_dominant", "tpm_output_mixed")
    traffic_ratio = float(0.5 * rpm_ratio + 0.5 * tpm_ratio)
    length_ratio = float(0.5 * prompt_ratio + 0.5 * completion_ratio)
    return _dominance_label(traffic_ratio, length_ratio, "traffic_family_dominant", "length_family_dominant", "traffic_length_mixed")


def _first_positive_index(values: np.ndarray) -> int:
    arr = np.asarray(values, dtype=float)
    idx = np.where(np.isfinite(arr) & (arr > 0))[0]
    if idx.size == 0:
        return -1
    return int(idx[0])


def _prepare_latency_frame(df: pd.DataFrame) -> pd.DataFrame:
    prepared = validate_latency_input(df)
    prepared = _collapse_duplicate_rows(prepared)
    return prepared.sort_values(["domain_id", "collect_time_std_parsed"]).reset_index(drop=True)


def _empty_prepared_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "domain_id",
            "collect_time_std_parsed",
            "rpm",
            "tpm",
            "ttft_avg",
            "tpot_avg",
            "prompt_tokens",
            "completion_tokens",
        ]
    )


def _prepare_optional_latency_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_prepared_frame()
    return _prepare_latency_frame(df)


def _build_metric_matrices(
    prepared: pd.DataFrame,
    user_ids: list[str],
    time_index: pd.DatetimeIndex,
) -> dict[str, np.ndarray]:
    return {
        "rpm": _build_metric_matrix(prepared, user_ids, time_index, "rpm"),
        "tpm": _build_metric_matrix(prepared, user_ids, time_index, "tpm"),
        "ttft": _build_metric_matrix(prepared, user_ids, time_index, "ttft_avg"),
        "tpot": _build_metric_matrix(prepared, user_ids, time_index, "tpot_avg"),
        "prompt_tokens": _build_metric_matrix(prepared, user_ids, time_index, "prompt_tokens"),
        "completion_tokens": _build_metric_matrix(prepared, user_ids, time_index, "completion_tokens"),
    }


def _build_system_series(matrices: dict[str, np.ndarray], point_count: int) -> dict[str, np.ndarray]:
    rpm_matrix = matrices["rpm"]
    tpm_matrix = matrices["tpm"]
    ttft_matrix = matrices["ttft"]
    tpot_matrix = matrices["tpot"]
    prompt_matrix = matrices["prompt_tokens"]
    completion_matrix = matrices["completion_tokens"]

    system_rpm = rpm_matrix.sum(axis=0)
    system_tpm = tpm_matrix.sum(axis=0)
    system_ttft = np.array(
        [_weighted_average_ignore_zero_1d(ttft_matrix[:, i], rpm_matrix[:, i]) for i in range(point_count)],
        dtype=float,
    )
    system_tpot = np.array(
        [_weighted_average_ignore_zero_1d(tpot_matrix[:, i], rpm_matrix[:, i]) for i in range(point_count)],
        dtype=float,
    )
    system_prompt = np.array(
        [_weighted_average_1d(prompt_matrix[:, i], rpm_matrix[:, i]) for i in range(point_count)],
        dtype=float,
    )
    system_completion = np.array(
        [_weighted_average_1d(completion_matrix[:, i], rpm_matrix[:, i]) for i in range(point_count)],
        dtype=float,
    )
    return {
        "system_rpm": system_rpm,
        "system_tpm": system_tpm,
        "system_ttft": system_ttft,
        "system_tpot": system_tpot,
        "system_prompt": system_prompt,
        "system_completion": system_completion,
    }


def _detect_system_events(
    cfg: LatencyDetectorConfig,
    system_ttft: np.ndarray,
    system_tpot: np.ndarray,
) -> dict[str, Any]:
    ttft_heavy = system_ttft >= (cfg.ttft_sla * cfg.severe_ratio)
    tpot_heavy = system_tpot >= (cfg.tpot_sla * cfg.severe_ratio)
    ttft_mild = system_ttft > cfg.ttft_sla
    tpot_mild = system_tpot > cfg.tpot_sla
    sys_anom_ttft = ttft_heavy | _mark_runs(ttft_mild, cfg.mild_consecutive_windows)
    sys_anom_tpot = tpot_heavy | _mark_runs(tpot_mild, cfg.mild_consecutive_windows)
    sys_anom = sys_anom_ttft | sys_anom_tpot

    events = _mask_to_events(sys_anom, cfg.event_merge_gap)
    events = _cap_events(events, cfg.max_events, system_ttft, system_tpot, cfg)
    events = sorted(events, key=lambda item: item[0])

    sys_event_mask = np.zeros_like(sys_anom, dtype=bool)
    for a, b in events:
        sys_event_mask[a : b + 1] = True
    return {
        "sys_anom": sys_anom,
        "sys_anom_ttft": sys_anom_ttft,
        "sys_anom_tpot": sys_anom_tpot,
        "sys_event_mask": sys_event_mask,
        "events": events,
    }


def _rolling_matrix_baseline(matrix: np.ndarray, cfg: LatencyDetectorConfig) -> np.ndarray:
    if matrix.shape[0] == 0:
        return np.zeros_like(matrix, dtype=float)
    baseline = np.vstack(
        [_rolling_mean(matrix[i], cfg.baseline_window_points, cfg.min_baseline_points) for i in range(matrix.shape[0])]
    )
    return np.nan_to_num(baseline, nan=0.0)


def _rolling_baselines(
    cfg: LatencyDetectorConfig,
    matrices: dict[str, np.ndarray],
    system: dict[str, np.ndarray],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    baselines = {
        "rpm": _rolling_matrix_baseline(matrices["rpm"], cfg),
        "tpm": _rolling_matrix_baseline(matrices["tpm"], cfg),
        "prompt_tokens": _rolling_matrix_baseline(matrices["prompt_tokens"], cfg),
        "completion_tokens": _rolling_matrix_baseline(matrices["completion_tokens"], cfg),
    }
    system_baselines = {
        "system_rpm": np.nan_to_num(_rolling_mean(system["system_rpm"], cfg.baseline_window_points, cfg.min_baseline_points), nan=0.0),
        "system_tpm": np.nan_to_num(_rolling_mean(system["system_tpm"], cfg.baseline_window_points, cfg.min_baseline_points), nan=0.0),
        "system_prompt": np.nan_to_num(
            _rolling_mean(system["system_prompt"], cfg.baseline_window_points, cfg.min_baseline_points),
            nan=0.0,
        ),
        "system_completion": np.nan_to_num(
            _rolling_mean(system["system_completion"], cfg.baseline_window_points, cfg.min_baseline_points),
            nan=0.0,
        ),
    }
    return baselines, system_baselines


def _minute_offsets_from_anchor(times: pd.Series | pd.DatetimeIndex, reported_at: Any) -> np.ndarray:
    ts = pd.to_datetime(pd.Series(times), errors="coerce").dt.floor("min")
    anchor = pd.Timestamp(reported_at).floor("min")
    anchor_seconds = anchor.hour * 3600 + anchor.minute * 60 + anchor.second
    anchors = ts.dt.normalize() + pd.to_timedelta(anchor_seconds, unit="s")
    return np.floor((ts - anchors).dt.total_seconds().to_numpy(dtype=float) / 60.0).astype(int)


def _historical_baselines_by_offset(
    cfg: LatencyDetectorConfig,
    history: pd.DataFrame,
    user_ids: list[str],
    time_index: pd.DatetimeIndex,
    reported_at: Any,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    point_count = len(time_index)
    baselines = {
        "rpm": np.zeros((len(user_ids), point_count), dtype=float),
        "tpm": np.zeros((len(user_ids), point_count), dtype=float),
        "prompt_tokens": np.zeros((len(user_ids), point_count), dtype=float),
        "completion_tokens": np.zeros((len(user_ids), point_count), dtype=float),
    }
    system_baselines = {
        "system_rpm": np.zeros(point_count, dtype=float),
        "system_tpm": np.zeros(point_count, dtype=float),
        "system_prompt": np.zeros(point_count, dtype=float),
        "system_completion": np.zeros(point_count, dtype=float),
    }
    if history.empty or not user_ids or point_count == 0:
        return baselines, system_baselines

    hist = history.copy()
    hist["_offset_min"] = _minute_offsets_from_anchor(hist["collect_time_std_parsed"], reported_at)
    current_offsets = _minute_offsets_from_anchor(time_index, reported_at)
    user_pos = {uid: idx for idx, uid in enumerate(user_ids)}

    for metric, column in (
        ("rpm", "rpm"),
        ("tpm", "tpm"),
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
    ):
        grouped = (
            hist.groupby(["domain_id", "_offset_min"], sort=False)[column]
            .agg(["mean", "count"])
            .reset_index()
        )
        for _, row in grouped.iterrows():
            if int(row["count"]) < cfg.min_baseline_points:
                continue
            uid = str(row["domain_id"])
            if uid not in user_pos:
                continue
            cols = np.where(current_offsets == int(row["_offset_min"]))[0]
            if cols.size:
                baselines[metric][user_pos[uid], cols] = float(row["mean"])

    time_grouped = (
        hist.groupby("collect_time_std_parsed", sort=True)
        .agg(
            rpm=("rpm", "sum"),
            tpm=("tpm", "sum"),
            prompt_tokens=("prompt_tokens", lambda s: _weighted_average_1d(s.to_numpy(dtype=float), hist.loc[s.index, "rpm"].to_numpy(dtype=float))),
            completion_tokens=(
                "completion_tokens",
                lambda s: _weighted_average_1d(s.to_numpy(dtype=float), hist.loc[s.index, "rpm"].to_numpy(dtype=float)),
            ),
        )
        .reset_index()
    )
    time_grouped["_offset_min"] = _minute_offsets_from_anchor(time_grouped["collect_time_std_parsed"], reported_at)
    for out_key, column in (
        ("system_rpm", "rpm"),
        ("system_tpm", "tpm"),
        ("system_prompt", "prompt_tokens"),
        ("system_completion", "completion_tokens"),
    ):
        grouped = time_grouped.groupby("_offset_min", sort=False)[column].agg(["mean", "count"]).reset_index()
        for _, row in grouped.iterrows():
            if int(row["count"]) < cfg.min_baseline_points:
                continue
            cols = np.where(current_offsets == int(row["_offset_min"]))[0]
            if cols.size:
                system_baselines[out_key][cols] = float(row["mean"])

    return baselines, system_baselines


def _first_active_indices(
    matrices: dict[str, np.ndarray],
    user_ids: list[str],
    history: pd.DataFrame | None = None,
) -> np.ndarray:
    active_before = set()
    if history is not None and not history.empty:
        activity = history.groupby("domain_id", sort=False).apply(lambda g: bool(((g["rpm"] + g["tpm"]) > 0).any()))
        active_before = {str(uid) for uid, is_active in activity.items() if is_active}

    out = []
    for idx, uid in enumerate(user_ids):
        if str(uid) in active_before:
            out.append(-1)
        else:
            out.append(_first_positive_index(matrices["rpm"][idx] + matrices["tpm"][idx]))
    return np.asarray(out, dtype=int)


def _run_latency_detection_core(
    cfg: LatencyDetectorConfig,
    time_index: pd.DatetimeIndex,
    freq: pd.Timedelta,
    user_ids: list[str],
    matrices: dict[str, np.ndarray],
    system: dict[str, np.ndarray],
    baselines: dict[str, np.ndarray],
    system_baselines: dict[str, np.ndarray],
    first_active_hours: np.ndarray,
    detection_started: float,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    timings = dict(timings or {})

    rpm_matrix = matrices["rpm"]
    tpm_matrix = matrices["tpm"]
    ttft_matrix = matrices["ttft"]
    tpot_matrix = matrices["tpot"]
    prompt_matrix = matrices["prompt_tokens"]
    completion_matrix = matrices["completion_tokens"]

    system_rpm = system["system_rpm"]
    system_tpm = system["system_tpm"]
    system_ttft = system["system_ttft"]
    system_tpot = system["system_tpot"]
    system_prompt = system["system_prompt"]
    system_completion = system["system_completion"]

    baseline_rpm = baselines["rpm"]
    baseline_tpm = baselines["tpm"]
    baseline_prompt = baselines["prompt_tokens"]
    baseline_completion = baselines["completion_tokens"]
    system_baseline_rpm = system_baselines["system_rpm"]
    system_baseline_tpm = system_baselines["system_tpm"]
    system_baseline_prompt = system_baselines["system_prompt"]
    system_baseline_completion = system_baselines["system_completion"]

    event_detection_started = perf_counter()
    event_info = _detect_system_events(cfg, system_ttft, system_tpot)
    sys_anom = event_info["sys_anom"]
    sys_anom_ttft = event_info["sys_anom_ttft"]
    sys_anom_tpot = event_info["sys_anom_tpot"]
    sys_event_mask = event_info["sys_event_mask"]
    events = event_info["events"]
    event_detection_seconds = perf_counter() - event_detection_started

    flags = np.zeros_like(rpm_matrix, dtype=bool)
    event_reports: list[dict[str, Any]] = []

    rootcause_started = perf_counter()
    for a, b in events:
        scope = _scope_for_window(sys_anom_ttft, sys_anom_tpot, a, b)
        weights = _score_weights(scope)

        rpm_excess_window = np.clip(rpm_matrix[:, a : b + 1] - baseline_rpm[:, a : b + 1], 0.0, None)
        tpm_excess_window = np.clip(tpm_matrix[:, a : b + 1] - baseline_tpm[:, a : b + 1], 0.0, None)
        prompt_delta_window = np.clip(prompt_matrix[:, a : b + 1] - baseline_prompt[:, a : b + 1], 0.0, None)
        completion_delta_window = np.clip(completion_matrix[:, a : b + 1] - baseline_completion[:, a : b + 1], 0.0, None)

        rpm_excess_sum = rpm_excess_window.sum(axis=1)
        tpm_excess_sum = tpm_excess_window.sum(axis=1)
        prompt_delta_sum = prompt_delta_window.sum(axis=1)
        completion_delta_sum = completion_delta_window.sum(axis=1)

        rpm_ratio = _safe_ratio(rpm_excess_sum)
        tpm_ratio = _safe_ratio(tpm_excess_sum)
        prompt_ratio = _safe_ratio(prompt_delta_sum)
        completion_ratio = _safe_ratio(completion_delta_sum)

        w_rpm, w_tpm, w_prompt, w_completion = weights
        scores = (
            w_rpm * rpm_ratio
            + w_tpm * tpm_ratio
            + w_prompt * prompt_ratio
            + w_completion * completion_ratio
        )
        score_sum = float(np.sum(scores))
        score_ratio = scores / score_sum if score_sum > 0 else np.zeros_like(scores)

        rpm_shift_score = _positive_shift_score(system_rpm[a : b + 1], system_baseline_rpm[a : b + 1])
        tpm_shift_score = _positive_shift_score(system_tpm[a : b + 1], system_baseline_tpm[a : b + 1])
        prompt_shift_score = _positive_shift_score(system_prompt[a : b + 1], system_baseline_prompt[a : b + 1])
        completion_shift_score = _positive_shift_score(
            system_completion[a : b + 1],
            system_baseline_completion[a : b + 1],
        )
        event_driver = _event_driver_diagnosis(
            scope,
            rpm_shift_score,
            tpm_shift_score,
            prompt_shift_score,
            completion_shift_score,
        )

        order = np.argsort(scores)[::-1]
        culprits: list[dict[str, Any]] = []
        new_join_users: list[dict[str, Any]] = []
        cumulative = 0.0
        for idx in order:
            if scores[idx] <= 0:
                break
            ratio = float(score_ratio[idx])
            if culprits and ratio < cfg.culprit_min_ratio:
                break

            local_score = _combined_local_score(
                rpm_excess_window[idx],
                tpm_excess_window[idx],
                prompt_delta_window[idx],
                completion_delta_window[idx],
                weights,
            )
            peak_offset = int(np.argmax(local_score)) if local_score.size else 0
            peak_hour = int(a + peak_offset)
            flags[idx, peak_hour] = True
            length_signal = _length_signal(prompt_ratio[idx], completion_ratio[idx], rpm_ratio[idx], tpm_ratio[idx])
            driver_signal = _culprit_driver_signal(
                scope,
                rpm_ratio[idx],
                tpm_ratio[idx],
                prompt_ratio[idx],
                completion_ratio[idx],
            )
            first_active_hour = int(first_active_hours[idx])
            is_new_user_join = first_active_hour >= a and first_active_hour <= b

            culprit_record = {
                "user_id": user_ids[idx],
                "score": float(scores[idx]),
                "score_ratio": ratio,
                "rpm_excess_ratio": float(rpm_ratio[idx]),
                "tpm_excess_ratio": float(tpm_ratio[idx]),
                "prompt_delta_ratio": float(prompt_ratio[idx]),
                "completion_delta_ratio": float(completion_ratio[idx]),
                "length_signal": length_signal,
                "driver_signal": driver_signal,
                "peak_hour": peak_hour,
                "peak_rpm": float(rpm_matrix[idx, peak_hour]),
                "peak_tpm": float(tpm_matrix[idx, peak_hour]),
                "peak_prompt_tokens": float(prompt_matrix[idx, peak_hour]),
                "peak_completion_tokens": float(completion_matrix[idx, peak_hour]),
                "peak_ttft": float(ttft_matrix[idx, peak_hour]),
                "peak_tpot": float(tpot_matrix[idx, peak_hour]),
                "first_active_hour": first_active_hour,
                "is_new_user_join": bool(is_new_user_join),
            }
            culprits.append(culprit_record)
            if is_new_user_join:
                new_join_users.append(
                    {
                        "user_id": user_ids[idx],
                        "first_active_hour": first_active_hour,
                        "score_ratio": ratio,
                    }
                )
            cumulative += ratio
            if len(culprits) >= cfg.culprit_top_k or cumulative >= cfg.culprit_cum_ratio:
                break

        system_peak_ttft_hour = int(a + np.argmax(system_ttft[a : b + 1])) if b >= a else int(a)
        system_peak_tpot_hour = int(a + np.argmax(system_tpot[a : b + 1])) if b >= a else int(a)

        event_reports.append(
            {
                "start_hour": int(a),
                "end_hour": int(b),
                "duration_hours": int(b - a + 1),
                "rootcause_scope": scope,
                "system_peak_hour_ttft": system_peak_ttft_hour,
                "system_peak_ttft": float(system_ttft[system_peak_ttft_hour]),
                "system_peak_hour_tpot": system_peak_tpot_hour,
                "system_peak_tpot": float(system_tpot[system_peak_tpot_hour]),
                "culprits": culprits,
                "is_new_user_join_event": bool(new_join_users),
                "event_variant": "new_user_join_event" if new_join_users else "traditional_latency_event",
                "new_join_users": new_join_users,
                "driver_signal": event_driver["driver_signal"],
                "traffic_driver_ratio": event_driver["traffic_driver_ratio"],
                "length_driver_ratio": event_driver["length_driver_ratio"],
                "traffic_driver_score": event_driver["traffic_driver_score"],
                "length_driver_score": event_driver["length_driver_score"],
                "driver_breakdown": {
                    "rpm_shift_score": event_driver["rpm_shift_score"],
                    "tpm_shift_score": event_driver["tpm_shift_score"],
                    "prompt_shift_score": event_driver["prompt_shift_score"],
                    "completion_shift_score": event_driver["completion_shift_score"],
                },
                "totals": {
                    "rpm_excess": float(np.sum(rpm_excess_sum)),
                    "tpm_excess": float(np.sum(tpm_excess_sum)),
                    "prompt_delta_excess": float(np.sum(prompt_delta_sum)),
                    "completion_delta_excess": float(np.sum(completion_delta_sum)),
                },
            }
        )
    rootcause_seconds = perf_counter() - rootcause_started

    records_started = perf_counter()
    reason_map: dict[str, set[str]] = {uid: set() for uid in user_ids}
    for report in event_reports:
        scope = str(report.get("rootcause_scope", "both"))
        for culprit in report.get("culprits", []) or []:
            reason_map[str(culprit["user_id"])].add(scope)
            if culprit.get("is_new_user_join"):
                reason_map[str(culprit["user_id"])].add("new_user_join")

    records: list[dict[str, Any]] = []
    for idx, uid in enumerate(user_ids):
        hit_idx = np.where(flags[idx])[0]
        if hit_idx.size == 0:
            continue
        reasons = sorted(reason_map.get(uid, set()))
        records.append(
            {
                "user_id": uid,
                "hit_count": int(hit_idx.size),
                "hit_hours": ",".join(map(str, hit_idx.tolist())),
                "reason": ",".join(reasons) if reasons else "latency_rootcause",
                "avg_rpm": float(np.mean(rpm_matrix[idx])),
                "p95_rpm": float(np.quantile(rpm_matrix[idx], 0.95)),
                "max_rpm": float(np.max(rpm_matrix[idx])),
                "avg_tpm": float(np.mean(tpm_matrix[idx])),
                "p95_tpm": float(np.quantile(tpm_matrix[idx], 0.95)),
                "max_tpm": float(np.max(tpm_matrix[idx])),
                "avg_ttft": float(np.mean(ttft_matrix[idx])),
                "p95_ttft": float(np.quantile(ttft_matrix[idx], 0.95)),
                "max_ttft": float(np.max(ttft_matrix[idx])),
                "avg_tpot": float(np.mean(tpot_matrix[idx])),
                "p95_tpot": float(np.quantile(tpot_matrix[idx], 0.95)),
                "max_tpot": float(np.max(tpot_matrix[idx])),
                "avg_prompt_tokens": float(np.mean(prompt_matrix[idx])),
                "avg_completion_tokens": float(np.mean(completion_matrix[idx])),
            }
        )

    records_df = pd.DataFrame.from_records(records)
    if not records_df.empty:
        records_df = records_df.sort_values(["hit_count", "max_ttft", "max_tpot", "user_id"], ascending=[False, False, False, True])
    records_seconds = perf_counter() - records_started

    stats_started = perf_counter()
    system_stats = {
        "hours": int(len(time_index)),
        "event_count": int(len(events)),
        "event_hours_count": int(np.sum(sys_event_mask)),
        "system_anom_hours_count": int(np.sum(sys_anom)),
        "ttft": {
            "system_avg": float(np.mean(system_ttft)) if len(system_ttft) else 0.0,
            "system_p95": float(np.quantile(system_ttft, 0.95)) if len(system_ttft) else 0.0,
            "system_max": float(np.max(system_ttft)) if len(system_ttft) else 0.0,
            "sla": float(cfg.ttft_sla),
            "severe_threshold": float(cfg.ttft_sla * cfg.severe_ratio),
        },
        "tpot": {
            "system_avg": float(np.mean(system_tpot)) if len(system_tpot) else 0.0,
            "system_p95": float(np.quantile(system_tpot, 0.95)) if len(system_tpot) else 0.0,
            "system_max": float(np.max(system_tpot)) if len(system_tpot) else 0.0,
            "sla": float(cfg.tpot_sla),
            "severe_threshold": float(cfg.tpot_sla * cfg.severe_ratio),
        },
        "rpm": {
            "system_avg": float(np.mean(system_rpm)) if len(system_rpm) else 0.0,
            "system_p95": float(np.quantile(system_rpm, 0.95)) if len(system_rpm) else 0.0,
            "system_max": float(np.max(system_rpm)) if len(system_rpm) else 0.0,
        },
        "tpm": {
            "system_avg": float(np.mean(system_tpm)) if len(system_tpm) else 0.0,
            "system_p95": float(np.quantile(system_tpm, 0.95)) if len(system_tpm) else 0.0,
            "system_max": float(np.max(system_tpm)) if len(system_tpm) else 0.0,
        },
        "prompt_tokens": {
            "system_avg": float(np.mean(system_prompt)) if len(system_prompt) else 0.0,
            "system_p95": float(np.quantile(system_prompt, 0.95)) if len(system_prompt) else 0.0,
            "system_max": float(np.max(system_prompt)) if len(system_prompt) else 0.0,
        },
        "completion_tokens": {
            "system_avg": float(np.mean(system_completion)) if len(system_completion) else 0.0,
            "system_p95": float(np.quantile(system_completion, 0.95)) if len(system_completion) else 0.0,
            "system_max": float(np.max(system_completion)) if len(system_completion) else 0.0,
        },
    }
    stats_seconds = perf_counter() - stats_started
    detection_timings = {
        "prepare_input_seconds": float(timings.get("prepare_input_seconds", 0.0)),
        "dedupe_seconds": float(timings.get("dedupe_seconds", 0.0)),
        "time_index_seconds": float(timings.get("time_index_seconds", 0.0)),
        "matrix_build_seconds": float(timings.get("matrix_build_seconds", 0.0)),
        "system_series_seconds": float(timings.get("system_series_seconds", 0.0)),
        "event_detection_seconds": float(event_detection_seconds),
        "baseline_seconds": float(timings.get("baseline_seconds", 0.0)),
        "rootcause_seconds": float(rootcause_seconds),
        "records_seconds": float(records_seconds),
        "stats_seconds": float(stats_seconds),
        "detection_total_seconds": float(perf_counter() - detection_started),
    }

    return {
        "time_index": [ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts for ts in time_index],
        "time_step_minutes": int(freq.total_seconds() // 60) if freq.total_seconds() > 0 else 60,
        "user_ids": user_ids,
        "rpm": rpm_matrix,
        "tpm": tpm_matrix,
        "ttft": ttft_matrix,
        "tpot": tpot_matrix,
        "prompt_tokens": prompt_matrix,
        "completion_tokens": completion_matrix,
        "system_rpm": system_rpm,
        "system_tpm": system_tpm,
        "system_ttft": system_ttft,
        "system_tpot": system_tpot,
        "system_prompt_tokens": system_prompt,
        "system_completion_tokens": system_completion,
        "sys_anom": sys_anom,
        "sys_anom_ttft": sys_anom_ttft,
        "sys_anom_tpot": sys_anom_tpot,
        "sys_event_mask": sys_event_mask,
        "events": events,
        "event_reports": event_reports,
        "flags": flags,
        "records": records_df,
        "system_stats": system_stats,
        "config_echo": asdict(cfg),
        "detection_timings": detection_timings,
    }


def detect_latency_anomalies(cfg: LatencyDetectorConfig, df: pd.DataFrame) -> dict[str, Any]:
    detection_started = perf_counter()

    prepare_started = perf_counter()
    prepared = validate_latency_input(df)
    prepare_input_seconds = perf_counter() - prepare_started

    dedupe_started = perf_counter()
    prepared = _collapse_duplicate_rows(prepared)
    prepared = prepared.sort_values(["domain_id", "collect_time_std_parsed"]).reset_index(drop=True)
    dedupe_seconds = perf_counter() - dedupe_started

    time_index_started = perf_counter()
    user_ids = sorted(prepared["domain_id"].astype(str).unique().tolist())
    base_time_index = pd.Series(sorted(pd.unique(prepared["collect_time_std_parsed"])))
    freq = _infer_time_freq(base_time_index)
    time_index = pd.date_range(start=base_time_index.min(), end=base_time_index.max(), freq=freq)
    time_index_seconds = perf_counter() - time_index_started

    matrix_started = perf_counter()
    matrices = _build_metric_matrices(prepared, user_ids, time_index)
    matrix_build_seconds = perf_counter() - matrix_started

    system_series_started = perf_counter()
    system = _build_system_series(matrices, len(time_index))
    system_series_seconds = perf_counter() - system_series_started

    baseline_started = perf_counter()
    baselines, system_baselines = _rolling_baselines(cfg, matrices, system)
    first_active_hours = _first_active_indices(matrices, user_ids)
    baseline_seconds = perf_counter() - baseline_started

    return _run_latency_detection_core(
        cfg,
        time_index,
        freq,
        user_ids,
        matrices,
        system,
        baselines,
        system_baselines,
        first_active_hours,
        detection_started,
        {
            "prepare_input_seconds": prepare_input_seconds,
            "dedupe_seconds": dedupe_seconds,
            "time_index_seconds": time_index_seconds,
            "matrix_build_seconds": matrix_build_seconds,
            "system_series_seconds": system_series_seconds,
            "baseline_seconds": baseline_seconds,
        },
    )


def detect_latency_anomalies_with_history(
    cfg: LatencyDetectorConfig,
    current_df: pd.DataFrame,
    history_df: pd.DataFrame,
    reported_at: Any,
    window_before_minutes: int = 10,
    window_after_minutes: int = 10,
) -> dict[str, Any]:
    detection_started = perf_counter()

    prepare_started = perf_counter()
    current_prepared = _prepare_latency_frame(current_df)
    history_prepared = _prepare_optional_latency_frame(history_df)
    prepare_input_seconds = perf_counter() - prepare_started

    time_index_started = perf_counter()
    reported = pd.Timestamp(reported_at).floor("min")
    window_start = reported - pd.Timedelta(minutes=int(window_before_minutes))
    window_points = int(window_before_minutes) + int(window_after_minutes)
    if window_points <= 0:
        raise ValueError("history comparison window must contain at least one minute")
    time_index = pd.date_range(start=window_start, periods=window_points, freq="1min")
    freq = pd.Timedelta(minutes=1)
    user_ids = sorted(
        set(current_prepared["domain_id"].astype(str).unique().tolist())
        | set(history_prepared["domain_id"].astype(str).unique().tolist())
    )
    if not user_ids:
        raise ValueError("No valid users remained after preparing current and history data.")
    time_index_seconds = perf_counter() - time_index_started

    matrix_started = perf_counter()
    matrices = _build_metric_matrices(current_prepared, user_ids, time_index)
    matrix_build_seconds = perf_counter() - matrix_started

    system_series_started = perf_counter()
    system = _build_system_series(matrices, len(time_index))
    system_series_seconds = perf_counter() - system_series_started

    baseline_started = perf_counter()
    baselines, system_baselines = _historical_baselines_by_offset(
        cfg,
        history_prepared,
        user_ids,
        time_index,
        reported,
    )
    first_active_hours = _first_active_indices(matrices, user_ids, history_prepared)
    baseline_seconds = perf_counter() - baseline_started

    result = _run_latency_detection_core(
        cfg,
        time_index,
        freq,
        user_ids,
        matrices,
        system,
        baselines,
        system_baselines,
        first_active_hours,
        detection_started,
        {
            "prepare_input_seconds": prepare_input_seconds,
            "dedupe_seconds": 0.0,
            "time_index_seconds": time_index_seconds,
            "matrix_build_seconds": matrix_build_seconds,
            "system_series_seconds": system_series_seconds,
            "baseline_seconds": baseline_seconds,
        },
    )
    result["history_baseline"] = {
        "history_rows": int(len(history_prepared)),
        "current_rows": int(len(current_prepared)),
        "window_before_minutes": int(window_before_minutes),
        "window_after_minutes": int(window_after_minutes),
        "reported_at": reported.to_pydatetime().isoformat(timespec="minutes"),
    }
    return result
