#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""MaaS 过载溯源插件 - 主动巡检版（Mode B，单文件）

与 main.py（反应式 Mode A，告警驱动）平行的第二入口：由 maas-monitor 定时巡检
任务每 5 分钟逐服务调用，判断「此刻是否正在过载」，若是则定位根因租户并产出
按 (domain_id, resident_model_id, region) 维度的过载处理策略（strategies）。

入参（位置参数，顺序固定）：
    1. service_id         infer_service_id（被巡检的池子/服务实例）
    2. model_name         服务承载的模型名（用于 (P,M) 过滤 + SLA 选表 + 策略回填）
    3. time               巡检时刻，ISO 8601 字符串或数字时间戳（秒/毫秒自动判定）
    4. maasApiurl         MaaS 数据查询接口完整端点 URL
    5. appcode            -> X-Apig-AppCode header
    6. applydomainid      -> X-Apply-DomainID header
    7. applyprojectid     -> X-Apply-ProjectID header

可选环境变量（覆盖默认）：
    PLUGIN_TTFT_SLA                  覆盖 SLA 表的 TTFT 阈值 (ms)；不设则按模型表
    PLUGIN_TPOT_SLA                  覆盖 SLA 表的 TPOT 阈值 (ms)；不设则按模型表
    PLUGIN_ENABLE_TPOT               默认 0；置 1 时 TPOT 也参与事件检测（本轮公司范围仅 TTFT）
    PLUGIN_SEVERE_RATIO              默认 7
    PLUGIN_MILD_CONSECUTIVE_WINDOWS  默认 10
    PLUGIN_HISTORY_DAYS              默认 14
    PLUGIN_CANDIDATE_TOP_N           默认 6
    PLUGIN_CULPRIT_TOP_K             默认 3
    PLUGIN_SCENARIO_TRIGGER_FACTOR   默认 1.3  (ratio >= factor 才算触发)
    PLUGIN_DOMINANCE_MARGIN          默认 1.25 (多触发时最大 ratio >= 次大 × margin 才算 dominant)
    PLUGIN_TPM_CAP_FACTOR            默认 1.5  (tpm_limit 目标 = baseline_tpm × factor)
    PLUGIN_OUTPUT_CAP_FACTOR         默认 1.5  (compeletion_token_limit 值 = baseline_completion × factor)
    PLUGIN_RPM_SHRINK_FACTOR         默认 0.8  (rpm_limit 目标 = baseline_rpm × factor)
    PLUGIN_LOOKBACK_MINUTES          默认 60   (Round 1 回看窗口)
    PLUGIN_ACTIVE_RECENT_MINUTES     默认 5    (事件末端落在窗口最后 N 分钟内才算活跃)
    PLUGIN_DETECT_ONLY               默认 0；置 1 时 Step 1 判完即返回（恢复巡检用，省配额）
    PLUGIN_RETRY_MAX                 默认 3    (HTTP 429 退避重试次数)
    PLUGIN_RETRY_BASE_SECONDS        默认 2    (第 k 次重试 sleep base×k 秒)
    PLUGIN_TIMEZONE                  默认 Asia/Shanghai

输出契约：
    stdout 一段多行 JSON。顶层字段 status 取值：
        anomaly  巡检时刻存在活跃过载事件，伴随 culprits 与 strategies
        normal   无活跃事件（含「窗口内有事件但已结束」，此时附 inactive_event_count）
        no_data  当前窗口 API 返回空
        error    入参/API/解析错误，配合 exit code 1
    顶层另有 mode="proactive"；strategies 数组每行：
        { domain_id, resident_model_id, region, process_type, value,
          model_name, project_id, scenario }
    process_type 取值为协议原文：rpm_limit / tpm_limit / compeletion_token_limit
    （compeletion 为公司接口与 DB 表的既定拼写，不可“修正”）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
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
log = logging.getLogger("maas_proactive_plugin")


# ============================================================================
# 常量与配置
# ============================================================================

EPSILON = 1e-9

# culprit 评分固定使用 both 权重（rpm / input / output 三维全参与）。
# 主动巡检默认 TTFT-only 检测，scope 恒为 ttft_only；若沿用 scope 选权重会把
# 输出维清零，导致 output_shift_dominant 场景不可达，故权重不再随 scope 变化。
SCORE_WEIGHTS = (0.28125, 0.34375, 0.375)

# 模型化 SLA 表：模型名含 glm（不区分大小写）走 GLM 档，其余默认档。
SLA_TABLE = {
    "glm": (30000.0, 500.0),
    "default": (10000.0, 150.0),
}

# 策略类型为协议原文（含公司文档/接口/DB 一致的 compeletion 拼写），不可改。
PROCESS_RPM_LIMIT = "rpm_limit"
PROCESS_TPM_LIMIT = "tpm_limit"
PROCESS_COMPLETION_LIMIT = "compeletion_token_limit"

# 场景触发指标 -> (场景类型, process_type)。触发判定均为窗口均值对自身基线的 ratio。
TRIGGER_METRICS = ("rpm", "tpm", "completion_tokens")
SCENARIO_BY_METRIC = {
    "rpm": ("rpm_rise_dominant", PROCESS_RPM_LIMIT),
    "tpm": ("tpm_rise_dominant", PROCESS_TPM_LIMIT),
    "completion_tokens": ("output_shift_dominant", PROCESS_COMPLETION_LIMIT),
}
PROCESS_BY_METRIC = {m: SCENARIO_BY_METRIC[m][1] for m in TRIGGER_METRICS}
DEFAULT_SCENARIO_TYPE = "default"


@dataclass(frozen=True)
class PluginConfig:
    ttft_sla_override: float | None = None
    tpot_sla_override: float | None = None
    enable_tpot: bool = False
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
    dominance_margin: float = 1.25
    tpm_cap_factor: float = 1.5
    output_cap_factor: float = 1.5
    rpm_shrink_factor: float = 0.8
    lookback_minutes: int = 60
    active_recent_minutes: int = 5
    detect_only: bool = False
    retry_max: int = 3
    retry_base_seconds: float = 2.0
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


def _env_float_optional(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def load_config_from_env() -> PluginConfig:
    return PluginConfig(
        ttft_sla_override=_env_float_optional("PLUGIN_TTFT_SLA"),
        tpot_sla_override=_env_float_optional("PLUGIN_TPOT_SLA"),
        enable_tpot=_env_bool("PLUGIN_ENABLE_TPOT", False),
        severe_ratio=_env_float("PLUGIN_SEVERE_RATIO", 7.0),
        mild_consecutive_windows=_env_int("PLUGIN_MILD_CONSECUTIVE_WINDOWS", 10),
        history_days=_env_int("PLUGIN_HISTORY_DAYS", 14),
        candidate_top_n=_env_int("PLUGIN_CANDIDATE_TOP_N", 6),
        culprit_top_k=_env_int("PLUGIN_CULPRIT_TOP_K", 3),
        scenario_trigger_factor=_env_float("PLUGIN_SCENARIO_TRIGGER_FACTOR", 1.3),
        dominance_margin=_env_float("PLUGIN_DOMINANCE_MARGIN", 1.25),
        tpm_cap_factor=_env_float("PLUGIN_TPM_CAP_FACTOR", 1.5),
        output_cap_factor=_env_float("PLUGIN_OUTPUT_CAP_FACTOR", 1.5),
        rpm_shrink_factor=_env_float("PLUGIN_RPM_SHRINK_FACTOR", 0.8),
        lookback_minutes=_env_int("PLUGIN_LOOKBACK_MINUTES", 60),
        active_recent_minutes=_env_int("PLUGIN_ACTIVE_RECENT_MINUTES", 5),
        detect_only=_env_bool("PLUGIN_DETECT_ONLY", False),
        retry_max=_env_int("PLUGIN_RETRY_MAX", 3),
        retry_base_seconds=_env_float("PLUGIN_RETRY_BASE_SECONDS", 2.0),
        timezone=_env_str("PLUGIN_TIMEZONE", "Asia/Shanghai"),
    )


def resolve_sla(model_name: str, cfg: PluginConfig) -> tuple[float, float, str]:
    """按模型名选 SLA 档位；环境变量覆盖优先。返回 (ttft_sla, tpot_sla, source)。"""
    key = "glm" if "glm" in (model_name or "").lower() else "default"
    ttft_sla, tpot_sla = SLA_TABLE[key]
    source = f"model_table:{key}"
    if cfg.ttft_sla_override is not None:
        ttft_sla = cfg.ttft_sla_override
        source = "env_override"
    if cfg.tpot_sla_override is not None:
        tpot_sla = cfg.tpot_sla_override
        source = "env_override"
    return float(ttft_sla), float(tpot_sla), source


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

# Round 3：按 (租户, 项目, 常驻服务, region, 池子) 拆分，用于 fan-out 推导与区域放大。
R3_DIMENSIONS = [
    {"name": "timestamp", "granularity": "minute"},
    {"name": "domain_id"},
    {"name": "project_id"},
    {"name": "resident_model_id"},
    {"name": "region"},
    {"name": "infer_service_id"},
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
        retry_max: int = 3,
        retry_base_seconds: float = 2.0,
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
        self.retry_max = max(int(retry_max), 0)
        self.retry_base_seconds = max(float(retry_base_seconds), 0.0)
        self.http_call_count = 0

    def query(self, filters: list[dict], dimensions: list[dict] | None = None) -> list[dict]:
        rows: list[dict] = []
        page_num = 1
        while True:
            payload = {
                "dimensions": dimensions if dimensions is not None else QUERY_DIMENSIONS,
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
        attempt = 0
        while True:
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
            # appcode 配额为 10 次/分钟，巡检多轮查询易触顶；429 做有界递增退避。
            if resp.status_code == 429 and attempt < self.retry_max:
                attempt += 1
                sleep_seconds = self.retry_base_seconds * attempt
                log.info("[http] 429 rate limited, retry %d/%d after %.1fs",
                         attempt, self.retry_max, sleep_seconds)
                time.sleep(sleep_seconds)
                continue
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


def rows_to_dataframe(
    rows: list[dict], tz_name: str, extra_str_cols: tuple[str, ...] = ()
) -> pd.DataFrame:
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
        record: dict[str, Any] = {
            "domain_id": domain_id,
            "rpm": _to_float(row.get("rpm")),
            "tpm": _to_float(row.get("tpm")),
            "ttft_avg": _to_float(row.get("ttft_avg")),
            "tpot_avg": _to_float(row.get("tpot_avg")),
            "prompt_tokens": _to_float(row.get("prompt_tokens")),
            "completion_tokens": _to_float(row.get("completion_tokens")),
            "collect_time_std_parsed": pd.Timestamp(ts),
        }
        for col in extra_str_cols:
            record[col] = str(row.get(col) or "").strip()
        records.append(record)
    return pd.DataFrame.from_records(records)


# ============================================================================
# 算法核心（与 main.py 同源：聚合 / 矩阵 / 事件检测）
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
    ttft_sla: float,
    tpot_sla: float,
) -> list[tuple[int, int]]:
    if not events or max_events <= 0 or len(events) <= max_events:
        return events
    scored: list[tuple[tuple[int, int], float]] = []
    for window in events:
        a, b = window
        ttft_ratio = float(
            np.nanmax(system_ttft[a : b + 1] / max(ttft_sla, EPSILON))
        )
        tpot_ratio = float(
            np.nanmax(system_tpot[a : b + 1] / max(tpot_sla, EPSILON))
        )
        scored.append((window, max(ttft_ratio, tpot_ratio)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [window for window, _ in scored[:max_events]]


def _detect_system_events(
    cfg: PluginConfig,
    system_ttft: np.ndarray,
    system_tpot: np.ndarray,
    ttft_sla: float,
    tpot_sla: float,
) -> dict[str, Any]:
    ttft_heavy = system_ttft >= (ttft_sla * cfg.severe_ratio)
    ttft_mild = system_ttft > ttft_sla
    sys_anom_ttft = ttft_heavy | _mark_runs(ttft_mild, cfg.mild_consecutive_windows)
    if cfg.enable_tpot:
        tpot_heavy = system_tpot >= (tpot_sla * cfg.severe_ratio)
        tpot_mild = system_tpot > tpot_sla
        sys_anom_tpot = tpot_heavy | _mark_runs(tpot_mild, cfg.mild_consecutive_windows)
    else:
        # 本轮公司范围仅 TTFT 参与检测；TPOT 序列仍输出诊断统计。
        sys_anom_tpot = np.zeros_like(sys_anom_ttft, dtype=bool)
    sys_anom = sys_anom_ttft | sys_anom_tpot
    events = _mask_to_events(sys_anom, cfg.event_merge_gap)
    events = _cap_events(events, cfg.max_events, system_ttft, system_tpot, ttft_sla, tpot_sla)
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


def _select_active_event(
    cfg: PluginConfig,
    events: list[tuple[int, int]],
    point_count: int,
    system_ttft: np.ndarray,
    ttft_sla: float,
) -> tuple[int, int] | None:
    """活跃性规则：事件末端落在窗口最后 active_recent_minutes 分钟内才算「正在过载」。

    多个活跃事件时取末端最新者，再以 TTFT 峰值比破并列。已结束的历史事件
    不报告（避免每轮巡检重复上报、恢复巡检无法判 normal）。
    """
    threshold = point_count - max(int(cfg.active_recent_minutes), 1)
    active = [ev for ev in events if ev[1] >= threshold]
    if not active:
        return None

    def _peak_ratio(ev: tuple[int, int]) -> float:
        a, b = ev
        seg = system_ttft[a : b + 1]
        if seg.size == 0:
            return 0.0
        return float(np.nanmax(seg)) / max(ttft_sla, EPSILON)

    return max(active, key=lambda ev: (ev[1], _peak_ratio(ev)))


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


# ============================================================================
# 场景分类（dominant + margin）与池级杠杆
# ============================================================================

def classify_scenario(
    cfg: PluginConfig,
    current: dict[str, float],
    baseline: dict[str, float | None],
) -> tuple[dict[str, Any] | None, dict[str, float], list[str], list[str]]:
    """单一 dominant 场景裁决。

    三个触发指标 (rpm / tpm / completion_tokens) 各自对 baseline 求 ratio：
      - 触发 = ratio >= scenario_trigger_factor
      - 恰一个触发 -> 该场景 (decision=single)
      - 多个触发 -> 最大 ratio >= 次大 × dominance_margin 才算 dominant
        (decision=margin)，否则 default/mixed -> rpm_limit (decision=mixed)
      - 零触发 -> None（调用方打 note，不出策略）
    baseline 缺失的指标记入 suppressed，不参与触发。
    返回 (scenario | None, ratios, triggered, suppressed)。
    """
    ratios: dict[str, float] = {}
    suppressed: list[str] = []
    for metric in TRIGGER_METRICS:
        bl = baseline.get(metric)
        cur = float(current.get(metric, 0.0))
        if bl is None or bl <= 0:
            suppressed.append(metric)
            continue
        ratios[metric] = cur / bl
    triggered = [
        m for m in TRIGGER_METRICS
        if m in ratios and ratios[m] >= cfg.scenario_trigger_factor
    ]
    if not triggered:
        return None, ratios, triggered, suppressed

    if len(triggered) == 1:
        metric = triggered[0]
        stype, ptype = SCENARIO_BY_METRIC[metric]
        decision = "single"
    else:
        ordered = sorted(triggered, key=lambda m: ratios[m], reverse=True)
        top, runner = ordered[0], ordered[1]
        if ratios[top] >= ratios[runner] * cfg.dominance_margin:
            stype, ptype = SCENARIO_BY_METRIC[top]
            decision = "margin"
        else:
            stype, ptype = DEFAULT_SCENARIO_TYPE, PROCESS_RPM_LIMIT
            decision = "mixed"

    scenario = {
        "type": stype,
        "process_type": ptype,
        "decision": decision,
        "trigger_ratios": {m: float(r) for m, r in ratios.items()},
        "triggered": list(triggered),
    }
    return scenario, ratios, triggered, suppressed


def _metric_for_process(process_type: str) -> str:
    for metric, ptype in PROCESS_BY_METRIC.items():
        if ptype == process_type:
            return metric
    return "rpm"


def _lever_for_metric(
    cfg: PluginConfig,
    metric: str,
    current: dict[str, float],
    baseline: dict[str, float | None],
) -> dict[str, Any] | None:
    """单指标杠杆。rpm/tpm 为 rate_scale（须 s<1 才有效）；completion 为 length_cap。

    completion 的 cap 是单请求输出长度上限（max_token 语义），是「基线之上设顶」
    的预防性限制，不要求 cap < current，也不参与区域放大。
    """
    bl = baseline.get(metric)
    if bl is None or bl <= 0:
        return None
    if metric == "completion_tokens":
        cap = float(bl) * cfg.output_cap_factor
        return {
            "metric": metric,
            "kind": "length_cap",
            "baseline": float(bl),
            "factor": cfg.output_cap_factor,
            "cap": cap,
        }
    cur = float(current.get(metric, 0.0))
    if cur <= 0:
        return None
    factor = cfg.rpm_shrink_factor if metric == "rpm" else cfg.tpm_cap_factor
    target = float(bl) * factor
    s = min(target / cur, 1.0)
    if s >= 1.0:
        return None
    return {
        "metric": metric,
        "kind": "rate_scale",
        "current": cur,
        "baseline": float(bl),
        "factor": factor,
        "target": target,
        "s": s,
    }


def compute_pool_lever(
    cfg: PluginConfig,
    scenario: dict[str, Any],
    current: dict[str, float],
    baseline: dict[str, float | None],
    ratios: dict[str, float],
    triggered: list[str],
) -> tuple[dict[str, Any] | None, str, str | None, str | None]:
    """场景 -> 池级杠杆。返回 (lever | None, process_type_final, note, warning)。

    dominant 场景只评估自身杠杆，不可降（s>=1 / baseline 缺失）则不出策略。
    default(mixed) 按公司口径落 rpm_limit，但 rpm 不可降时沿触发指标按
    ratio 降序兜底到次优杠杆（process_type 随之切换并打 warning）。
    """
    if scenario["type"] == DEFAULT_SCENARIO_TYPE:
        chain = ["rpm"] + [
            m for m in sorted(triggered, key=lambda m: ratios.get(m, 0.0), reverse=True)
            if m != "rpm"
        ]
    else:
        chain = [_metric_for_process(scenario["process_type"])]

    attempts: list[str] = []
    for metric in chain:
        lever = _lever_for_metric(cfg, metric, current, baseline)
        if lever is not None:
            ptype = PROCESS_BY_METRIC[metric]
            warning = None
            if scenario["type"] == DEFAULT_SCENARIO_TYPE and metric != "rpm":
                warning = (
                    f"default_fallback_to_{ptype}: " + "; ".join(attempts)
                )
            return lever, ptype, None, warning
        attempts.append(f"{metric}_not_reducible_or_baseline_missing")
    note = "lever_not_computable: " + "; ".join(attempts)
    return None, scenario["process_type"], note, None


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
# 候选选择
# ============================================================================

def _pick_candidates(
    cfg: PluginConfig,
    user_ids: list[str],
    matrices: dict[str, np.ndarray],
    a: int,
    b: int,
    ttft_sla: float,
    tpot_sla: float,
) -> list[str]:
    """事件窗口内按 max(ttft/sla) + max(tpot/sla) 综合排序取前 N。

    主动巡检无告警上报者，不做强制并入。
    """
    ttft_window = matrices["ttft"][:, a : b + 1]
    tpot_window = matrices["tpot"][:, a : b + 1]
    user_ttft_max = (
        np.max(ttft_window, axis=1) / max(ttft_sla, EPSILON)
        if ttft_window.size
        else np.zeros(len(user_ids))
    )
    user_tpot_max = (
        np.max(tpot_window, axis=1) / max(tpot_sla, EPSILON)
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
    return chosen


# ============================================================================
# Round 3：region/常驻服务拆分与区域放大
# ============================================================================

def build_region_breakdown(
    r3df: pd.DataFrame,
    domain_id: str,
    service_id: str,
    lever: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """fan-out + 区域放大（v2 Step 4，拓扑由数据驱动）。

    fan-out 集合 G(T,P) = 事件窗口内把 T 的流量路由到过载池 P 的
    (resident_model_id, region)；region_total[g] = T 经 g 在「所有池子」上的
    逐分钟总量取非零均值；value = floor(region_total × s)，下限 1。
    length_cap（max_token 语义）不放大，各 region 行同值。
    """
    if r3df.empty:
        return [], "resident_breakdown_unavailable"
    sub = r3df[r3df["domain_id"] == domain_id]
    sub = sub[(sub["resident_model_id"] != "") & (sub["region"] != "")]
    if sub.empty:
        return [], "resident_breakdown_unavailable"
    on_pool = sub[sub["infer_service_id"] == service_id]
    fanout = sorted(set(zip(on_pool["resident_model_id"], on_pool["region"])))
    if not fanout:
        return [], "resident_breakdown_unavailable"

    rows: list[dict[str, Any]] = []
    for resident, region in fanout:
        grp = sub[(sub["resident_model_id"] == resident) & (sub["region"] == region)]
        entry: dict[str, Any] = {
            "resident_model_id": resident,
            "region": region,
        }
        if lever["kind"] == "rate_scale":
            per_minute = grp.groupby("collect_time_std_parsed")[lever["metric"]].sum()
            region_total = _window_mean(per_minute.to_numpy(dtype=float))
            if region_total <= 0:
                continue
            entry["region_total"] = float(region_total)
            entry["s"] = float(lever["s"])
            entry["value"] = max(int(region_total * lever["s"]), 1)
        else:
            entry["value"] = max(int(lever["cap"]), 1)
        proj_sums = grp.groupby("project_id")["rpm"].sum()
        entry["project_id"] = str(proj_sums.idxmax()) if len(proj_sums) else ""
        rows.append(entry)
    if not rows:
        return [], "resident_breakdown_unavailable"
    return rows, None


# ============================================================================
# 输出
# ============================================================================

def _emit(payload: dict, exit_code: int) -> int:
    log.info(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return exit_code


def _format_ts(time_index: pd.DatetimeIndex, idx: int, tz: ZoneInfo) -> str:
    ts = time_index[idx]
    aware = ts.to_pydatetime().replace(tzinfo=tz)
    return aware.isoformat(timespec="minutes")


def _system_stats(
    cfg: PluginConfig,
    system: dict[str, np.ndarray],
    sys_anom: np.ndarray,
    event_count: int,
    ttft_sla: float,
    tpot_sla: float,
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
        "minutes": int(system["system_ttft"].size),
        "event_count": int(event_count),
        "system_anom_minutes_count": int(np.sum(np.asarray(sys_anom, dtype=bool))),
        "ttft": _block(system["system_ttft"], ttft_sla),
        "tpot": _block(system["system_tpot"], tpot_sla),
        "rpm": _block(system["system_rpm"]),
        "tpm": _block(system["system_tpm"]),
        "prompt_tokens": _block(system["system_prompt"]),
        "completion_tokens": _block(system["system_completion"]),
    }


def _make_base_output(
    status: str,
    cfg: PluginConfig,
    service_id: str,
    model_name: str,
    checked_at: datetime,
    ttft_sla: float,
    tpot_sla: float,
    sla_source: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "mode": "proactive",
        "sweep": {
            "service_id": service_id,
            "model_name": model_name,
            "checked_at": checked_at.isoformat(timespec="minutes"),
            "lookback_minutes": int(cfg.lookback_minutes),
            "active_recent_minutes": int(cfg.active_recent_minutes),
            "detect_only": bool(cfg.detect_only),
        },
        "sla": {
            "ttft_sla": float(ttft_sla),
            "tpot_sla": float(tpot_sla),
            "tpot_detection_enabled": bool(cfg.enable_tpot),
            "source": sla_source,
        },
        "config_echo": asdict(cfg),
    }


# ============================================================================
# 主流程
# ============================================================================

def run_plugin(
    service_id: str,
    model_name: str,
    time_iso: str,
    maas_url: str,
    appcode: str,
    apply_domain_id: str,
    apply_project_id: str,
    cfg: PluginConfig | None = None,
) -> tuple[dict[str, Any], int]:
    cfg = cfg if cfg is not None else load_config_from_env()
    tz = ZoneInfo(cfg.timezone)
    checked_at = parse_iso_reported_at(time_iso, cfg.timezone)
    checked_naive = checked_at.replace(tzinfo=None)
    if not str(model_name or "").strip():
        raise ValueError("model_name is required")
    model_name = str(model_name).strip()
    ttft_sla, tpot_sla, sla_source = resolve_sla(model_name, cfg)

    client = MaasClient(
        url=maas_url,
        appcode=appcode,
        apply_domain_id=apply_domain_id,
        apply_project_id=apply_project_id,
        timeout_seconds=cfg.timeout_seconds,
        page_size=cfg.page_size,
        retry_max=cfg.retry_max,
        retry_base_seconds=cfg.retry_base_seconds,
    )

    # ---- Round 1: (P, M) 巡检时刻前 lookback 分钟 ----
    r1_start = checked_at - timedelta(minutes=cfg.lookback_minutes)
    r1_end_inclusive = checked_at - timedelta(minutes=1)
    log.info(
        "[round1] sweep service=%s model=%s window=%s~%s",
        service_id,
        model_name,
        r1_start.isoformat(timespec="minutes"),
        checked_at.isoformat(timespec="minutes"),
    )
    r1_filters = [
        {"name": "infer_service_id", "operator": "=", "value": service_id},
        {"name": "model_name", "operator": "=", "value": model_name},
        {"name": "timestamp", "operator": ">=", "value": str(to_epoch_ms(r1_start))},
        {"name": "timestamp", "operator": "<=", "value": str(to_epoch_ms(r1_end_inclusive))},
    ]
    r1_rows = client.query(r1_filters)
    log.info("[round1] rows=%d", len(r1_rows))
    r1_df = rows_to_dataframe(r1_rows, cfg.timezone)
    log.info("[round1] df_rows=%d after filter", len(r1_df))

    base = _make_base_output(
        "normal", cfg, service_id, model_name, checked_at, ttft_sla, tpot_sla, sla_source
    )

    if r1_df.empty:
        base["status"] = "no_data"
        base["events"] = []
        base["culprits"] = []
        base["strategies"] = []
        base["api_call_count"] = client.http_call_count
        return base, 0

    prepared = _prepare_frame(r1_df)
    user_ids = sorted(prepared["domain_id"].astype(str).unique().tolist())
    window_start_naive = checked_naive - timedelta(minutes=cfg.lookback_minutes)
    time_index = pd.date_range(
        start=pd.Timestamp(window_start_naive), periods=cfg.lookback_minutes, freq="1min"
    )
    point_count = len(time_index)

    matrices = _build_metric_matrices(prepared, user_ids, time_index)
    system = _build_system_series(matrices, point_count)
    event_info = _detect_system_events(
        cfg, system["system_ttft"], system["system_tpot"], ttft_sla, tpot_sla
    )
    events = event_info["events"]

    base["system_stats"] = _system_stats(
        cfg, system, event_info["sys_anom"], len(events), ttft_sla, tpot_sla
    )

    active_event = _select_active_event(
        cfg, events, point_count, system["system_ttft"], ttft_sla
    )
    if active_event is None:
        base["events"] = []
        base["culprits"] = []
        base["strategies"] = []
        base["inactive_event_count"] = len(events)
        base["api_call_count"] = client.http_call_count
        return base, 0

    a, b = active_event
    scope = _scope_for_window(
        event_info["sys_anom_ttft"], event_info["sys_anom_tpot"], a, b
    )
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

    # ---- detect-only：恢复巡检只要「还过载吗」，省掉 Round 2/3 ----
    if cfg.detect_only:
        base["culprits"] = []
        base["strategies"] = []
        base["note"] = "detect_only"
        base["api_call_count"] = client.http_call_count
        return base, 0

    # ---- 候选选择 ----
    candidates = _pick_candidates(cfg, user_ids, matrices, a, b, ttft_sla, tpot_sla)
    log.info(
        "[round1] active event=[%d,%d] scope=%s candidates=%d uids=%s",
        a, b, scope, len(candidates), candidates,
    )

    # ---- Round 2: 跨池 14d 同时刻偏移基线（范围止于当前窗口之前，天然不混入当前数据）----
    r2_start = r1_start - timedelta(days=cfg.history_days)
    r2_end_inclusive = r1_start - timedelta(minutes=1)
    log.info(
        "[round2] query history candidates=%d window=%s~%s",
        len(candidates),
        r2_start.isoformat(timespec="minutes"),
        r2_end_inclusive.isoformat(timespec="minutes"),
    )
    r2_filters = [
        {"name": "domain_id", "operator": "in", "value": candidates},
        {"name": "model_name", "operator": "=", "value": model_name},
        {"name": "timestamp", "operator": ">=", "value": str(to_epoch_ms(r2_start))},
        {"name": "timestamp", "operator": "<=", "value": str(to_epoch_ms(r2_end_inclusive))},
    ]
    r2_rows = client.query(r2_filters)
    r2_df = rows_to_dataframe(r2_rows, cfg.timezone)
    history_prepared = _prepare_frame(r2_df) if not r2_df.empty else r2_df

    cand_uid_set = set(candidates)
    cand_user_ids = [uid for uid in user_ids if uid in cand_uid_set]

    cand_matrices = _build_metric_matrices(prepared, cand_user_ids, time_index)
    baselines = _historical_baselines_by_offset(
        cfg, history_prepared, cand_user_ids, time_index, pd.Timestamp(checked_naive)
    )

    # ---- 评分（固定 both 权重：rpm / input / output 三维全参与）----
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

    rpm_ratio = _safe_ratio(rpm_excess_window.sum(axis=1))
    prompt_ratio = _safe_ratio(prompt_delta_window.sum(axis=1))
    completion_ratio = _safe_ratio(completion_delta_window.sum(axis=1))

    w_rpm, w_input, w_output = SCORE_WEIGHTS
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
            SCORE_WEIGHTS,
        )
        peak_offset = int(np.argmax(local_score)) if local_score.size else 0
        peak_idx = int(a + peak_offset)

        current_scalars = {
            "rpm": _window_mean(cand_matrices["rpm"][idx, a : b + 1]),
            "tpm": _window_mean(cand_matrices["tpm"][idx, a : b + 1]),
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
        scenario, ratios, triggered, suppressed = classify_scenario(
            cfg, current_scalars, baseline_scalars
        )

        uid = cand_user_ids[idx]
        culprit: dict[str, Any] = {
            "domain_id": uid,
            "score": float(scores[idx]),
            "score_ratio": ratio,
            "scenario": scenario,
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
        warnings: list[str] = []
        if suppressed:
            warnings.append("baseline_unavailable: " + ", ".join(suppressed))

        if scenario is None:
            culprit["note"] = "no_scenario_triggered"
        else:
            lever, process_type, note, lever_warning = compute_pool_lever(
                cfg, scenario, current_scalars, baseline_scalars, ratios, triggered
            )
            culprit["process_type"] = process_type
            if lever is not None:
                culprit["pool_lever"] = lever
            if note:
                culprit["note"] = note
            if lever_warning:
                warnings.append(lever_warning)
        if warnings:
            culprit["warning"] = "; ".join(warnings)
        culprits.append(culprit)
        cumulative += ratio
        if len(culprits) >= cfg.culprit_top_k or cumulative >= cfg.culprit_cum_ratio:
            break

    # ---- Round 3: region/常驻服务拆分（仅对有杠杆的 culprits，一次查询）----
    strategies: list[dict[str, Any]] = []
    lever_culprits = [c for c in culprits if c.get("pool_lever")]
    if lever_culprits:
        ev_start_ms = to_epoch_ms(time_index[a].to_pydatetime().replace(tzinfo=tz))
        ev_end_ms = to_epoch_ms(time_index[b].to_pydatetime().replace(tzinfo=tz))
        r3_filters = [
            {
                "name": "domain_id",
                "operator": "in",
                "value": [c["domain_id"] for c in lever_culprits],
            },
            {"name": "model_name", "operator": "=", "value": model_name},
            {"name": "timestamp", "operator": ">=", "value": str(ev_start_ms)},
            {"name": "timestamp", "operator": "<=", "value": str(ev_end_ms)},
        ]
        log.info("[round3] query region breakdown culprits=%d", len(lever_culprits))
        r3_rows = client.query(r3_filters, dimensions=R3_DIMENSIONS)
        r3_df = rows_to_dataframe(
            r3_rows,
            cfg.timezone,
            extra_str_cols=("project_id", "resident_model_id", "region", "infer_service_id"),
        )
        log.info("[round3] rows=%d df_rows=%d", len(r3_rows), len(r3_df))
        for culprit in lever_culprits:
            breakdown, note = build_region_breakdown(
                r3_df, culprit["domain_id"], service_id, culprit["pool_lever"]
            )
            if note:
                existing = culprit.get("note")
                culprit["note"] = f"{existing}; {note}" if existing else note
                continue
            culprit["region_breakdown"] = breakdown
            for row in breakdown:
                strategies.append(
                    {
                        "domain_id": culprit["domain_id"],
                        "resident_model_id": row["resident_model_id"],
                        "region": row["region"],
                        "process_type": culprit["process_type"],
                        "value": int(row["value"]),
                        "model_name": model_name,
                        "project_id": row.get("project_id", ""),
                        "scenario": culprit["scenario"]["type"],
                    }
                )

    base["culprits"] = culprits
    base["strategies"] = strategies
    base["api_call_count"] = client.http_call_count
    base["history_baseline"] = {
        "candidates": candidates,
        "history_rows": int(len(history_prepared)),
        "history_days": cfg.history_days,
        "history_window_end": r2_end_inclusive.isoformat(timespec="minutes"),
    }
    if not culprits:
        base["warning"] = "no_culprit_resolved_baseline_may_be_empty"
    return base, 0


# ============================================================================
# 入口
# ============================================================================

EXPECTED_ARG_COUNT = 7
ARG_NAMES = (
    "service_id",
    "model_name",
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
