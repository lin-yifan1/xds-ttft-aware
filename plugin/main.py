#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""MaaS 过载溯源插件 - 单文件版本

入参（位置参数，顺序固定）：
    1. domain_id          告警上报租户 ID
    2. service_id         infer_service_id
    3. time               ISO 8601 字符串或数字时间戳（秒/毫秒自动判定），例如 2026-05-22T10:30:00+08:00 或 1779349646000
    4. maasApiurl         MaaS 数据查询接口完整端点 URL
    5. appcode            -> X-Apig-AppCode header
    6. applydomainid      -> X-Apply-DomainID header
    7. applyprojectid     -> X-Apply-ProjectID header

可选环境变量（覆盖默认）：
    PLUGIN_TTFT_SLA                  默认 15000 (ms)
    PLUGIN_TPOT_SLA                  默认 50 (ms)
    PLUGIN_SEVERE_RATIO              默认 7
    PLUGIN_MILD_CONSECUTIVE_WINDOWS  默认 10
    PLUGIN_HISTORY_DAYS              默认 14
    PLUGIN_CANDIDATE_TOP_N           默认 6
    PLUGIN_CULPRIT_TOP_K             默认 3
    PLUGIN_SCENARIO_TRIGGER_FACTOR   默认 1.3  (current >= baseline × factor 才点亮场景)
    PLUGIN_TPM_CAP_FACTOR            默认 1.5  (input_too_long: tpm_limit = baseline_tpm × factor)
    PLUGIN_OUTPUT_CAP_FACTOR         默认 1.5  (output_too_long: max_output = baseline_completion × factor)
    PLUGIN_RPM_SHRINK_FACTOR         默认 0.8  (rpm_increase: rpm_limit = baseline_rpm × factor)
    PLUGIN_TIMEZONE                  默认 Asia/Shanghai

输出契约：
    stdout 一段多行 JSON。顶层字段 status 取值：
        anomaly  本次告警时刻命中系统级事件，伴随 culprits
        normal   系统级序列未命中事件
        no_data  当前窗口 API 返回空
        error    入参/API/解析错误，配合 exit code 1
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("maas_plugin")


# ============================================================================
# 常量与配置
# ============================================================================

EPSILON = 1e-9

# 三维评分权重 (w_rpm, w_input, w_output)。被 scope 门控的维度权重为 0；
# 由原四维权重去掉 TPM 与被门控维度后按比例归一化得到。
SCORE_WEIGHTS_BY_SCOPE = {
    "ttft_only": (0.4667, 0.5333, 0.0),
    "tpot_only": (0.0, 0.0, 1.0),
    "both": (0.28125, 0.34375, 0.375),
}

# 每个 scope 下物理上允许点亮的场景（严格门控）。
ELIGIBLE_SCENARIOS_BY_SCOPE = {
    "ttft_only": ("rpm_increase", "input_too_long"),
    "tpot_only": ("output_too_long",),
    "both": ("rpm_increase", "input_too_long", "output_too_long"),
}


@dataclass(frozen=True)
class PluginConfig:
    ttft_sla: float = 15000.0
    tpot_sla: float = 50.0
    severe_ratio: float = 7.0
    mild_consecutive_windows: int = 10
    event_merge_gap: int = 0
    max_events: int = 5
    min_baseline_points: int = 6
    culprit_top_k: int = 3
    culprit_cum_ratio: float = 0.8
    culprit_min_ratio: float = 0.05
    history_days: int = 14
    candidate_top_n: int = 6
    scenario_trigger_factor: float = 1.3
    tpm_cap_factor: float = 1.5
    output_cap_factor: float = 1.5
    rpm_shrink_factor: float = 0.8
    window_before_minutes: int = 30
    window_after_minutes: int = 30
    history_same_time_minutes: int = 10
    page_size: int = 2000
    timeout_seconds: float = 30.0
    timezone: str = "Asia/Shanghai"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def load_config_from_env() -> PluginConfig:
    return PluginConfig(
        ttft_sla=_env_float("PLUGIN_TTFT_SLA", 15000.0),
        tpot_sla=_env_float("PLUGIN_TPOT_SLA", 50.0),
        severe_ratio=_env_float("PLUGIN_SEVERE_RATIO", 7.0),
        mild_consecutive_windows=_env_int("PLUGIN_MILD_CONSECUTIVE_WINDOWS", 10),
        history_days=_env_int("PLUGIN_HISTORY_DAYS", 14),
        candidate_top_n=_env_int("PLUGIN_CANDIDATE_TOP_N", 6),
        culprit_top_k=_env_int("PLUGIN_CULPRIT_TOP_K", 3),
        scenario_trigger_factor=_env_float("PLUGIN_SCENARIO_TRIGGER_FACTOR", 1.3),
        tpm_cap_factor=_env_float("PLUGIN_TPM_CAP_FACTOR", 1.5),
        output_cap_factor=_env_float("PLUGIN_OUTPUT_CAP_FACTOR", 1.5),
        rpm_shrink_factor=_env_float("PLUGIN_RPM_SHRINK_FACTOR", 0.8),
        timezone=_env_str("PLUGIN_TIMEZONE", "Asia/Shanghai"),
    )


# ============================================================================
# 时间解析
# ============================================================================

def parse_iso_reported_at(value: str, default_tz: str) -> datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("time argument is empty")
    # 优先尝试时间戳格式（纯数字，支持秒/毫秒）
    try:
        ts = float(text)
        if ts > 1e12:  # 毫秒级时间戳
            ts = ts / 1000.0
        parsed = datetime.fromtimestamp(ts, tz=ZoneInfo(default_tz))
        return parsed.replace(second=0, microsecond=0)
    except (ValueError, OverflowError, OSError):
        pass
    # 回退到 ISO 8601 字符串
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception as exc:
        raise ValueError(f"time argument is not valid ISO 8601 or timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(default_tz))
    return parsed.replace(second=0, microsecond=0)


def to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ============================================================================
# MaaS API 客户端
# ============================================================================

class MaasApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


QUERY_DIMENSIONS = [
    {"name": "timestamp", "granularity": "minute"},
    {"name": "domain_id"},
]

QUERY_METRICS = [
    {"name": "ttft_avg", "func": "avg"},
    {"name": "tpot_avg", "func": "avg"},
    {"name": "success_cnt", "func": "sum"},
    {"name": "error_cnt", "func": "sum"},
    {"name": "prompt_tokens", "func": "avg"},
    {"name": "completion_tokens", "func": "avg"},
    {"name": "rpm", "func": "sum"},
    {"name": "tpm", "func": "sum"},
]


class MaasClient:
    def __init__(
        self,
        url: str,
        appcode: str,
        apply_domain_id: str,
        apply_project_id: str,
        timeout_seconds: float = 30.0,
        page_size: int = 2000,
    ) -> None:
        if not url:
            raise ValueError("maasApiurl is required")
        if not appcode:
            raise ValueError("appcode is required")
        if not apply_domain_id:
            raise ValueError("applydomainid is required")
        if not apply_project_id:
            raise ValueError("applyprojectid is required")
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "X-Apig-AppCode": appcode,
            "X-Apply-DomainID": apply_domain_id,
            "X-Apply-ProjectID": apply_project_id,
        }
        self.timeout_seconds = float(timeout_seconds)
        self.page_size = min(max(int(page_size), 1), 2000)
        self.http_call_count = 0

    def query(self, filters: list[dict]) -> list[dict]:
        rows: list[dict] = []
        page_num = 1
        while True:
            payload = {
                "dimensions": QUERY_DIMENSIONS,
                "metrics": QUERY_METRICS,
                "filters": filters,
                "page": {"pageNum": int(page_num), "pageSize": self.page_size},
            }
            data = self._post(payload)
            page_rows = data.get("list") or []
            if not isinstance(page_rows, list):
                raise MaasApiError("MaaS API list is not a list")
            rows.extend(page_rows)
            pages = int(data.get("pages") or 1)
            current_page = int(data.get("pageNum") or page_num)
            if current_page >= pages or not page_rows:
                break
            page_num = current_page + 1
        return rows

    def _post(self, payload: dict) -> dict:
        try:
            resp = requests.post(
                self.url,
                headers=self.headers,
                json=payload,
                timeout=self.timeout_seconds,
                verify=False,
            )
        except requests.RequestException as exc:
            raise MaasApiError(f"MaaS API request failed: {exc}") from exc
        self.http_call_count += 1
        if resp.status_code != 200:
            raise MaasApiError(
                f"MaaS API HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            )
        try:
            body = resp.json()
        except Exception as exc:
            raise MaasApiError("MaaS API returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise MaasApiError("MaaS API response is not an object")
        code = int(body.get("code", 200))
        if code != 200:
            raise MaasApiError(
                f"MaaS API code={code} msg={body.get('msg', '')}", status=code
            )
        return body


# ============================================================================
# API rows -> DataFrame
# ============================================================================

def _to_float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return 0.0
    if not np.isfinite(out):
        return 0.0
    return out


def _row_timestamp_to_local(value: Any, tz: ZoneInfo) -> datetime | None:
    try:
        raw = float(value)
    except Exception:
        return None
    if raw > 1e12:  # heuristic: ms epoch
        raw = raw / 1000.0
    try:
        return (
            datetime.fromtimestamp(int(raw), tz=tz)
            .replace(second=0, microsecond=0, tzinfo=None)
        )
    except Exception:
        return None


def rows_to_dataframe(rows: list[dict], tz_name: str) -> pd.DataFrame:
    tz = ZoneInfo(tz_name)
    records: list[dict[str, Any]] = []
    for row in rows:
        domain_id = str(row.get("domain_id", "")).strip()
        if not domain_id:
            continue
        if "infer_service_id" in row and not str(row.get("infer_service_id") or "").strip():
            continue
        ts = _row_timestamp_to_local(row.get("timestamp"), tz)
        if ts is None:
            continue
        success_cnt = _to_float(row.get("success_cnt"))
        error_cnt = _to_float(row.get("error_cnt"))
        if success_cnt + error_cnt <= 0:
            continue
        records.append(
            {
                "domain_id": domain_id,
                "rpm": _to_float(row.get("rpm")),
                "tpm": _to_float(row.get("tpm")),
                "ttft_avg": _to_float(row.get("ttft_avg")),
                "tpot_avg": _to_float(row.get("tpot_avg")),
                "prompt_tokens": _to_float(row.get("prompt_tokens")),
                "completion_tokens": _to_float(row.get("completion_tokens")),
                "collect_time_std_parsed": pd.Timestamp(ts),
            }
        )
    return pd.DataFrame.from_records(records)


# ============================================================================
# 算法核心 (inlined from latency_detector.py)
# ============================================================================

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
    valid = (
        np.isfinite(values) & np.isfinite(weights) & (weights > 0) & (values != 0)
    )
    if not valid.any():
        return 0.0
    return float(np.sum(values[valid] * weights[valid]) / np.sum(weights[valid]))


def _collapse_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    """同 (domain_id, time) 的重复行做 RPM 加权折叠。"""
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
        grouped[out_col] = np.divide(
            numer, denom, out=np.zeros_like(numer, dtype=float), where=denom > 0
        )

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


def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    prepared = _collapse_duplicate_rows(df)
    return prepared.sort_values(["domain_id", "collect_time_std_parsed"]).reset_index(
        drop=True
    )


def _build_metric_matrix(
    df: pd.DataFrame,
    user_ids: list[str],
    time_index: pd.DatetimeIndex,
    column: str,
) -> np.ndarray:
    if df.empty:
        return np.zeros((len(user_ids), len(time_index)), dtype=float)
    pivot = (
        df.pivot_table(
            index="domain_id",
            columns="collect_time_std_parsed",
            values=column,
            aggfunc="first",
        )
        .reindex(index=user_ids, columns=time_index)
        .fillna(0.0)
    )
    return pivot.to_numpy(dtype=float)


def _build_metric_matrices(
    df: pd.DataFrame,
    user_ids: list[str],
    time_index: pd.DatetimeIndex,
) -> dict[str, np.ndarray]:
    return {
        "rpm": _build_metric_matrix(df, user_ids, time_index, "rpm"),
        "tpm": _build_metric_matrix(df, user_ids, time_index, "tpm"),
        "ttft": _build_metric_matrix(df, user_ids, time_index, "ttft_avg"),
        "tpot": _build_metric_matrix(df, user_ids, time_index, "tpot_avg"),
        "prompt_tokens": _build_metric_matrix(df, user_ids, time_index, "prompt_tokens"),
        "completion_tokens": _build_metric_matrix(
            df, user_ids, time_index, "completion_tokens"
        ),
    }


def _build_system_series(
    matrices: dict[str, np.ndarray], point_count: int
) -> dict[str, np.ndarray]:
    rpm_matrix = matrices["rpm"]
    ttft_matrix = matrices["ttft"]
    tpot_matrix = matrices["tpot"]
    prompt_matrix = matrices["prompt_tokens"]
    completion_matrix = matrices["completion_tokens"]
    return {
        "system_rpm": rpm_matrix.sum(axis=0),
        "system_tpm": matrices["tpm"].sum(axis=0),
        "system_ttft": np.array(
            [
                _weighted_average_ignore_zero_1d(ttft_matrix[:, i], rpm_matrix[:, i])
                for i in range(point_count)
            ],
            dtype=float,
        ),
        "system_tpot": np.array(
            [
                _weighted_average_ignore_zero_1d(tpot_matrix[:, i], rpm_matrix[:, i])
                for i in range(point_count)
            ],
            dtype=float,
        ),
        "system_prompt": np.array(
            [
                _weighted_average_1d(prompt_matrix[:, i], rpm_matrix[:, i])
                for i in range(point_count)
            ],
            dtype=float,
        ),
        "system_completion": np.array(
            [
                _weighted_average_1d(completion_matrix[:, i], rpm_matrix[:, i])
                for i in range(point_count)
            ],
            dtype=float,
        ),
    }


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


def _cap_events(
    events: list[tuple[int, int]],
    max_events: int,
    system_ttft: np.ndarray,
    system_tpot: np.ndarray,
    cfg: PluginConfig,
) -> list[tuple[int, int]]:
    if not events or max_events <= 0 or len(events) <= max_events:
        return events
    scored: list[tuple[tuple[int, int], float]] = []
    for window in events:
        a, b = window
        ttft_ratio = float(
            np.nanmax(system_ttft[a : b + 1] / max(cfg.ttft_sla, EPSILON))
        )
        tpot_ratio = float(
            np.nanmax(system_tpot[a : b + 1] / max(cfg.tpot_sla, EPSILON))
        )
        scored.append((window, max(ttft_ratio, tpot_ratio)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [window for window, _ in scored[:max_events]]


def _detect_system_events(
    cfg: PluginConfig,
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
    return {
        "sys_anom": sys_anom,
        "sys_anom_ttft": sys_anom_ttft,
        "sys_anom_tpot": sys_anom_tpot,
        "events": events,
    }


def _scope_for_window(
    sys_anom_ttft: np.ndarray,
    sys_anom_tpot: np.ndarray,
    a: int,
    b: int,
) -> str:
    ttft_active = bool(np.asarray(sys_anom_ttft, dtype=bool)[a : b + 1].any())
    tpot_active = bool(np.asarray(sys_anom_tpot, dtype=bool)[a : b + 1].any())
    if ttft_active and tpot_active:
        return "both"
    if ttft_active:
        return "ttft_only"
    return "tpot_only"


def _safe_ratio(values: np.ndarray) -> np.ndarray:
    total = float(np.sum(values))
    if total <= 0:
        return np.zeros_like(values, dtype=float)
    return np.asarray(values, dtype=float) / total


def _combined_local_score(
    rpm_excess: np.ndarray,
    prompt_delta_excess: np.ndarray,
    completion_delta_excess: np.ndarray,
    weights: tuple[float, float, float],
) -> np.ndarray:
    w_rpm, w_input, w_output = weights
    score = np.zeros_like(rpm_excess, dtype=float)
    totals = [
        float(np.sum(rpm_excess)),
        float(np.sum(prompt_delta_excess)),
        float(np.sum(completion_delta_excess)),
    ]
    if totals[0] > 0:
        score += w_rpm * (rpm_excess / totals[0])
    if totals[1] > 0:
        score += w_input * (prompt_delta_excess / totals[1])
    if totals[2] > 0:
        score += w_output * (completion_delta_excess / totals[2])
    return score


# 三场景定义：trigger 指标（决定是否点亮）+ remediation 杠杆（限流建议落在哪个指标）。
# 注意 input_too_long 的触发看 prompt_tokens，但限流杠杆是 tpm。
_SCENARIO_SPECS: dict[str, dict[str, str]] = {
    "rpm_increase": {
        "trigger_metric": "rpm",
        "action": "throttle_rpm",
        "target_metric": "rpm",
        "factor_attr": "rpm_shrink_factor",
    },
    "input_too_long": {
        "trigger_metric": "prompt_tokens",
        "action": "cap_tpm",
        "target_metric": "tpm",
        "factor_attr": "tpm_cap_factor",
    },
    "output_too_long": {
        "trigger_metric": "completion_tokens",
        "action": "cap_output_length",
        "target_metric": "completion_tokens",
        "factor_attr": "output_cap_factor",
    },
}


def _window_mean(values: np.ndarray) -> float:
    """窗口内非 0 有限值的均值；无有效点时返回 0.0。"""
    arr = np.asarray(values, dtype=float)
    valid = arr[np.isfinite(arr) & (arr > 0)]
    if valid.size == 0:
        return 0.0
    return float(np.mean(valid))


def _baseline_scalar(values: np.ndarray) -> float | None:
    """窗口内非 0 历史偏移基线的均值；全 0/空 视为不可用，返回 None。"""
    arr = np.asarray(values, dtype=float)
    valid = arr[np.isfinite(arr) & (arr > 0)]
    if valid.size == 0:
        return None
    return float(np.mean(valid))


def _build_scenarios_for_culprit(
    cfg: PluginConfig,
    scope: str,
    current: dict[str, float],
    baseline: dict[str, float | None],
) -> tuple[list[dict[str, Any]], list[str]]:
    """按 scope 门控枚举可点亮场景，返回 (fired_scenarios, suppressed_types)。

    点亮条件：trigger 指标当前值 >= 自身 baseline × scenario_trigger_factor。
    任一所需 baseline（trigger 或 remediation 目标）不可用 -> 抑制并计入告警。
    remediation 值 = 目标指标 baseline × 对应系数。
    """
    fired: list[dict[str, Any]] = []
    suppressed: list[str] = []
    for scenario_type in ELIGIBLE_SCENARIOS_BY_SCOPE.get(scope, ()):
        spec = _SCENARIO_SPECS[scenario_type]
        trigger_metric = spec["trigger_metric"]
        target_metric = spec["target_metric"]
        trigger_baseline = baseline.get(trigger_metric)
        target_baseline = baseline.get(target_metric)
        if trigger_baseline is None or target_baseline is None:
            suppressed.append(scenario_type)
            continue
        cur = float(current.get(trigger_metric, 0.0))
        if cur < trigger_baseline * cfg.scenario_trigger_factor:
            continue
        factor = float(getattr(cfg, spec["factor_attr"]))
        fired.append(
            {
                "type": scenario_type,
                "trigger": {
                    "metric": trigger_metric,
                    "current": cur,
                    "baseline": float(trigger_baseline),
                    "ratio": cur / trigger_baseline,
                },
                "remediation": {
                    "action": spec["action"],
                    "target_metric": target_metric,
                    "baseline": float(target_baseline),
                    "factor": factor,
                    "recommended_value": float(target_baseline) * factor,
                },
            }
        )
    fired.sort(key=lambda s: s["trigger"]["ratio"], reverse=True)
    return fired, suppressed


# ============================================================================
# 跨池 baseline (同时刻偏移均值)
# ============================================================================

def _minute_offsets_from_anchor(
    times: pd.Series | pd.DatetimeIndex, reported_at: pd.Timestamp
) -> np.ndarray:
    ts = pd.to_datetime(pd.Series(times), errors="coerce").dt.floor("min")
    anchor = pd.Timestamp(reported_at).floor("min")
    anchor_seconds = anchor.hour * 3600 + anchor.minute * 60 + anchor.second
    anchors = ts.dt.normalize() + pd.to_timedelta(anchor_seconds, unit="s")
    return np.floor((ts - anchors).dt.total_seconds().to_numpy(dtype=float) / 60.0).astype(int)


def _historical_baselines_by_offset(
    cfg: PluginConfig,
    history: pd.DataFrame,
    user_ids: list[str],
    time_index: pd.DatetimeIndex,
    reported_at: pd.Timestamp,
) -> dict[str, np.ndarray]:
    point_count = len(time_index)
    baselines = {
        "rpm": np.zeros((len(user_ids), point_count), dtype=float),
        "tpm": np.zeros((len(user_ids), point_count), dtype=float),
        "prompt_tokens": np.zeros((len(user_ids), point_count), dtype=float),
        "completion_tokens": np.zeros((len(user_ids), point_count), dtype=float),
    }
    if history.empty or not user_ids or point_count == 0:
        return baselines

    hist = history.copy()
    hist["_offset_min"] = _minute_offsets_from_anchor(
        hist["collect_time_std_parsed"], reported_at
    )
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
    return baselines


# ============================================================================
# 候选选择 + 主流程
# ============================================================================

def _pick_candidates(
    cfg: PluginConfig,
    user_ids: list[str],
    matrices: dict[str, np.ndarray],
    a: int,
    b: int,
    alert_reporter: str,
) -> list[str]:
    """事件窗口内按 max(ttft/sla, tpot/sla) 综合排序，取前 N + 强行加入告警上报者。"""
    ttft_window = matrices["ttft"][:, a : b + 1]
    tpot_window = matrices["tpot"][:, a : b + 1]
    user_ttft_max = (
        np.max(ttft_window, axis=1) / max(cfg.ttft_sla, EPSILON)
        if ttft_window.size
        else np.zeros(len(user_ids))
    )
    user_tpot_max = (
        np.max(tpot_window, axis=1) / max(cfg.tpot_sla, EPSILON)
        if tpot_window.size
        else np.zeros(len(user_ids))
    )
    cand_score = user_ttft_max + user_tpot_max
    order = np.argsort(cand_score)[::-1]
    chosen: list[str] = []
    seen: set[str] = set()
    for idx in order:
        if len(chosen) >= cfg.candidate_top_n:
            break
        uid = user_ids[int(idx)]
        if uid in seen:
            continue
        chosen.append(uid)
        seen.add(uid)
    if alert_reporter and alert_reporter not in seen:
        chosen.append(alert_reporter)
    return chosen


def _index_for_reported_at(
    time_index: pd.DatetimeIndex, reported_naive: datetime
) -> int:
    target = pd.Timestamp(reported_naive).floor("min")
    diffs = np.abs((time_index - target).total_seconds().to_numpy(dtype=float))
    if diffs.size == 0:
        return -1
    return int(np.argmin(diffs))


def _system_stats(
    cfg: PluginConfig,
    system: dict[str, np.ndarray],
    sys_anom: np.ndarray,
    event_count: int,
) -> dict[str, Any]:
    def _block(values: np.ndarray, sla: float | None = None) -> dict[str, float]:
        block: dict[str, float] = {
            "system_avg": float(np.mean(values)) if values.size else 0.0,
            "system_p95": float(np.quantile(values, 0.95)) if values.size else 0.0,
            "system_max": float(np.max(values)) if values.size else 0.0,
        }
        if sla is not None:
            block["sla"] = float(sla)
            block["severe_threshold"] = float(sla * cfg.severe_ratio)
        return block

    return {
        "hours": int(system["system_ttft"].size),
        "event_count": int(event_count),
        "system_anom_hours_count": int(np.sum(np.asarray(sys_anom, dtype=bool))),
        "ttft": _block(system["system_ttft"], cfg.ttft_sla),
        "tpot": _block(system["system_tpot"], cfg.tpot_sla),
        "rpm": _block(system["system_rpm"]),
        "tpm": _block(system["system_tpm"]),
        "prompt_tokens": _block(system["system_prompt"]),
        "completion_tokens": _block(system["system_completion"]),
    }


def _format_ts(time_index: pd.DatetimeIndex, idx: int, tz: ZoneInfo) -> str:
    ts = time_index[idx]
    aware = ts.to_pydatetime().replace(tzinfo=tz)
    return aware.isoformat(timespec="minutes")


def _split_history(
    df: pd.DataFrame,
    reported_naive: datetime,
    same_time_minutes: int,
) -> pd.DataFrame:
    """从 Round 2 数据里剔除当前窗口 (reported_at ±same_time_minutes) 的行，剩下作为 history。"""
    if df.empty:
        return df.copy()
    reported = pd.Timestamp(reported_naive).floor("min")
    cw_start = reported - pd.Timedelta(minutes=int(same_time_minutes))
    cw_end = reported + pd.Timedelta(minutes=int(same_time_minutes))
    ts = pd.to_datetime(df["collect_time_std_parsed"], errors="coerce")
    mask = ts.notna() & ((ts < cw_start) | (ts >= cw_end))
    return df.loc[mask].copy()


# ============================================================================
# 输出
# ============================================================================

def _emit(payload: dict, exit_code: int) -> int:
    log.info(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return exit_code


def _make_base_output(
    status: str,
    cfg: PluginConfig,
    domain_id: str,
    service_id: str,
    reported_at: datetime,
) -> dict[str, Any]:
    return {
        "status": status,
        "alert": {
            "domain_id": domain_id,
            "service_id": service_id,
            "reported_at": reported_at.isoformat(timespec="minutes"),
        },
        "config_echo": asdict(cfg),
    }


# ============================================================================
# 主流程
# ============================================================================

def run_plugin(
    domain_id: str,
    service_id: str,
    time_iso: str,
    maas_url: str,
    appcode: str,
    apply_domain_id: str,
    apply_project_id: str,
) -> tuple[dict[str, Any], int]:
    cfg = load_config_from_env()
    tz = ZoneInfo(cfg.timezone)
    reported_at = parse_iso_reported_at(time_iso, cfg.timezone)
    reported_naive = reported_at.replace(tzinfo=None)

    client = MaasClient(
        url=maas_url,
        appcode=appcode,
        apply_domain_id=apply_domain_id,
        apply_project_id=apply_project_id,
        timeout_seconds=cfg.timeout_seconds,
        page_size=cfg.page_size,
    )

    # ---- Round 1: service 当前 ±window 分 ----
    r1_start = reported_at - timedelta(minutes=cfg.window_before_minutes)
    r1_end = reported_at + timedelta(minutes=cfg.window_after_minutes)
    log.info(
        "[round1] query service=%s window=%s~%s",
        service_id,
        r1_start.isoformat(timespec="minutes"),
        r1_end.isoformat(timespec="minutes"),
    )
    r1_end_inclusive = r1_end - timedelta(minutes=1)
    r1_filters = [
        {"name": "infer_service_id", "operator": "=", "value": service_id},
        {"name": "timestamp", "operator": ">=", "value": str(to_epoch_ms(r1_start))},
        {"name": "timestamp", "operator": "<=", "value": str(to_epoch_ms(r1_end_inclusive))},
    ]
    r1_rows = client.query(r1_filters)
    log.info("[round1] rows=%d first_row=%s", len(r1_rows), r1_rows[0] if r1_rows else None)
    r1_df = rows_to_dataframe(r1_rows, cfg.timezone)
    log.info("[round1] df_rows=%d after filter", len(r1_df))

    if r1_df.empty:
        out = _make_base_output("no_data", cfg, domain_id, service_id, reported_at)
        out["api_call_count"] = client.http_call_count
        out["events"] = []
        out["culprits"] = []
        return out, 0

    prepared = _prepare_frame(r1_df)
    user_ids = sorted(prepared["domain_id"].astype(str).unique().tolist())
    window_points = cfg.window_before_minutes + cfg.window_after_minutes
    window_start_naive = reported_naive - timedelta(minutes=cfg.window_before_minutes)
    time_index = pd.date_range(
        start=pd.Timestamp(window_start_naive), periods=window_points, freq="1min"
    )

    matrices = _build_metric_matrices(prepared, user_ids, time_index)
    system = _build_system_series(matrices, len(time_index))
    event_info = _detect_system_events(cfg, system["system_ttft"], system["system_tpot"])
    events = event_info["events"]

    reported_idx = _index_for_reported_at(time_index, reported_naive)
    matching_event: tuple[int, int] | None = None
    for a, b in events:
        if a <= reported_idx <= b:
            matching_event = (a, b)
            break

    base = _make_base_output(
        "normal", cfg, domain_id, service_id, reported_at
    )
    base["system_stats"] = _system_stats(cfg, system, event_info["sys_anom"], len(events))

    if matching_event is None:
        base["events"] = []
        base["culprits"] = []
        base["api_call_count"] = client.http_call_count
        return base, 0

    a, b = matching_event
    scope = _scope_for_window(
        event_info["sys_anom_ttft"], event_info["sys_anom_tpot"], a, b
    )

    # ---- Round 1.5: 候选选择 ----
    candidates = _pick_candidates(cfg, user_ids, matrices, a, b, domain_id)
    log.info(
        "[round1] event scope=%s candidates=%d uids=%s",
        scope,
        len(candidates),
        candidates,
    )

    # ---- Round 2: 跨池 14d × ±same_time 分 ----
    r2_start = reported_at - timedelta(
        days=cfg.history_days, minutes=cfg.history_same_time_minutes
    )
    r2_end = reported_at + timedelta(minutes=cfg.history_same_time_minutes)
    log.info(
        "[round2] query history candidates=%d window=%s~%s",
        len(candidates),
        r2_start.isoformat(timespec="minutes"),
        r2_end.isoformat(timespec="minutes"),
    )
    r2_end_inclusive = r2_end - timedelta(minutes=1)
    r2_filters = [
        {"name": "domain_id", "operator": "in", "value": candidates},
        {"name": "timestamp", "operator": ">=", "value": str(to_epoch_ms(r2_start))},
        {"name": "timestamp", "operator": "<=", "value": str(to_epoch_ms(r2_end_inclusive))},
    ]
    r2_rows = client.query(r2_filters)
    r2_df = rows_to_dataframe(r2_rows, cfg.timezone)
    r2_history = _split_history(r2_df, reported_naive, cfg.history_same_time_minutes)
    history_prepared = _prepare_frame(r2_history) if not r2_history.empty else r2_history

    # 候选 user_ids（顺序与 candidates 一致），保留事件窗口内的数据矩阵子集
    cand_uid_set = set(candidates)
    cand_user_ids = [uid for uid in user_ids if uid in cand_uid_set]
    # 上报者若不在 service 当前窗口里出现，补一个全 0 行，保证后续矩阵索引正确
    for uid in candidates:
        if uid not in cand_user_ids:
            cand_user_ids.append(uid)

    cand_matrices = _build_metric_matrices(prepared, cand_user_ids, time_index)
    baselines = _historical_baselines_by_offset(
        cfg, history_prepared, cand_user_ids, time_index, pd.Timestamp(reported_naive)
    )

    # ---- 评分（三维：rpm / input / output；TPM 退出评分，仅保留 baseline 作杠杆）----
    rpm_excess_window = np.clip(
        cand_matrices["rpm"][:, a : b + 1] - baselines["rpm"][:, a : b + 1], 0.0, None
    )
    prompt_delta_window = np.clip(
        cand_matrices["prompt_tokens"][:, a : b + 1]
        - baselines["prompt_tokens"][:, a : b + 1],
        0.0,
        None,
    )
    completion_delta_window = np.clip(
        cand_matrices["completion_tokens"][:, a : b + 1]
        - baselines["completion_tokens"][:, a : b + 1],
        0.0,
        None,
    )

    rpm_excess_sum = rpm_excess_window.sum(axis=1)
    prompt_delta_sum = prompt_delta_window.sum(axis=1)
    completion_delta_sum = completion_delta_window.sum(axis=1)

    rpm_ratio = _safe_ratio(rpm_excess_sum)
    prompt_ratio = _safe_ratio(prompt_delta_sum)
    completion_ratio = _safe_ratio(completion_delta_sum)

    weights = SCORE_WEIGHTS_BY_SCOPE.get(scope, SCORE_WEIGHTS_BY_SCOPE["both"])
    w_rpm, w_input, w_output = weights
    scores = w_rpm * rpm_ratio + w_input * prompt_ratio + w_output * completion_ratio
    score_sum = float(np.sum(scores))
    score_ratio = scores / score_sum if score_sum > 0 else np.zeros_like(scores)

    order = np.argsort(scores)[::-1]
    culprits: list[dict[str, Any]] = []
    cumulative = 0.0
    for idx in order:
        idx = int(idx)
        if scores[idx] <= 0:
            break
        ratio = float(score_ratio[idx])
        if culprits and ratio < cfg.culprit_min_ratio:
            break
        local_score = _combined_local_score(
            rpm_excess_window[idx],
            prompt_delta_window[idx],
            completion_delta_window[idx],
            weights,
        )
        peak_offset = int(np.argmax(local_score)) if local_score.size else 0
        peak_idx = int(a + peak_offset)

        current_scalars = {
            "rpm": _window_mean(cand_matrices["rpm"][idx, a : b + 1]),
            "prompt_tokens": _window_mean(cand_matrices["prompt_tokens"][idx, a : b + 1]),
            "completion_tokens": _window_mean(
                cand_matrices["completion_tokens"][idx, a : b + 1]
            ),
        }
        baseline_scalars: dict[str, float | None] = {
            "rpm": _baseline_scalar(baselines["rpm"][idx, a : b + 1]),
            "tpm": _baseline_scalar(baselines["tpm"][idx, a : b + 1]),
            "prompt_tokens": _baseline_scalar(baselines["prompt_tokens"][idx, a : b + 1]),
            "completion_tokens": _baseline_scalar(
                baselines["completion_tokens"][idx, a : b + 1]
            ),
        }
        scenarios, suppressed = _build_scenarios_for_culprit(
            cfg, scope, current_scalars, baseline_scalars
        )

        uid = cand_user_ids[idx]
        culprit: dict[str, Any] = {
            "domain_id": uid,
            "is_alert_reporter": bool(uid == domain_id),
            "score": float(scores[idx]),
            "score_ratio": ratio,
            "scenarios": scenarios,
            "peak_time": _format_ts(time_index, peak_idx, tz),
            "peak_rpm": float(cand_matrices["rpm"][idx, peak_idx]),
            "peak_tpm": float(cand_matrices["tpm"][idx, peak_idx]),
            "peak_ttft": float(cand_matrices["ttft"][idx, peak_idx]),
            "peak_tpot": float(cand_matrices["tpot"][idx, peak_idx]),
            "peak_prompt_tokens": float(cand_matrices["prompt_tokens"][idx, peak_idx]),
            "peak_completion_tokens": float(
                cand_matrices["completion_tokens"][idx, peak_idx]
            ),
        }
        if suppressed:
            culprit["warning"] = "baseline_unavailable: " + ", ".join(suppressed)
        if not scenarios:
            culprit["note"] = "no_scenario_triggered"
        culprits.append(culprit)
        cumulative += ratio
        if len(culprits) >= cfg.culprit_top_k or cumulative >= cfg.culprit_cum_ratio:
            break

    system_peak_ttft_offset = int(np.argmax(system["system_ttft"][a : b + 1]))
    system_peak_tpot_offset = int(np.argmax(system["system_tpot"][a : b + 1]))
    event_payload = {
        "start": _format_ts(time_index, a, tz),
        "end": _format_ts(time_index, b, tz),
        "duration_minutes": int(b - a + 1),
        "scope": scope,
        "system_peak_ttft_time": _format_ts(time_index, a + system_peak_ttft_offset, tz),
        "system_peak_ttft": float(system["system_ttft"][a + system_peak_ttft_offset]),
        "system_peak_tpot_time": _format_ts(time_index, a + system_peak_tpot_offset, tz),
        "system_peak_tpot": float(system["system_tpot"][a + system_peak_tpot_offset]),
    }

    base["status"] = "anomaly"
    base["events"] = [event_payload]
    base["culprits"] = culprits
    base["api_call_count"] = client.http_call_count
    base["history_baseline"] = {
        "candidates": candidates,
        "history_rows": int(len(history_prepared)),
        "history_days": cfg.history_days,
        "history_same_time_minutes": cfg.history_same_time_minutes,
    }
    if not culprits:
        base["warning"] = "no_culprit_resolved_baseline_may_be_empty"
    return base, 0


# ============================================================================
# 入口
# ============================================================================

EXPECTED_ARG_COUNT = 7
ARG_NAMES = (
    "domain_id",
    "service_id",
    "time",
    "maasApiurl",
    "appcode",
    "applydomainid",
    "applyprojectid",
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != EXPECTED_ARG_COUNT:
        err = {
            "status": "error",
            "error_type": "InvalidArgs",
            "error_msg": (
                f"expected {EXPECTED_ARG_COUNT} positional args "
                f"({', '.join(ARG_NAMES)}), got {len(argv)}"
            ),
        }
        return _emit(err, 1)

    try:
        result, exit_code = run_plugin(*argv)
    except MaasApiError as exc:
        return _emit(
            {
                "status": "error",
                "error_type": "MaasApiError",
                "error_msg": str(exc),
                "api_status": exc.status,
            },
            1,
        )
    except ValueError as exc:
        return _emit(
            {"status": "error", "error_type": "ValueError", "error_msg": str(exc)},
            1,
        )
    except Exception as exc:
        return _emit(
            {
                "status": "error",
                "error_type": exc.__class__.__name__,
                "error_msg": str(exc),
            },
            1,
        )

    return _emit(result, exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
