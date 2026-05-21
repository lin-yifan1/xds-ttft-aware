from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import os
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import DEFAULT_LATENCY_CONFIG, LatencyDetectorConfig
from latency_detector import (
    _build_metric_matrices,
    _build_system_series,
    _prepare_optional_latency_frame,
    detect_latency_anomalies,
    detect_latency_anomalies_with_history,
)


DEFAULT_API_PATH = "/maas/monitor/v1/data/query"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_PAGE_SIZE = 2000
DEFAULT_RATE_LIMIT_SECONDS = 60.0

BASE_URL_ENV = "MAAS_MONITOR_BASE_URL"
APPCODE_ENV = "MAAS_APP_CODE"

QUERY_DIMENSIONS = [
    {"name": "domain_id"},
    {"name": "timestamp", "granularity": "minute"},
]

QUERY_METRICS = [
    {"name": "rpm", "func": "sum"},
    {"name": "tpm", "func": "sum"},
    {"name": "ttft", "func": "avg"},
    {"name": "tpot", "func": "avg"},
    {"name": "prompt_tokens", "func": "avg"},
    {"name": "completion_tokens", "func": "avg"},
]


class MaasMonitorApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MaasMonitorClient:
    def __init__(
        self,
        base_url: str,
        appcode: str,
        appcode_header: str = "appcode",
        api_path: str = DEFAULT_API_PATH,
        page_size: int = DEFAULT_PAGE_SIZE,
        timeout_seconds: float = 30.0,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        urlopen_func: Callable[..., Any] = urlopen,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not appcode:
            raise ValueError("appcode is required")
        self.base_url = base_url.rstrip("/") + "/"
        self.appcode = appcode
        self.appcode_header = appcode_header or "appcode"
        self.api_path = api_path.lstrip("/")
        self.page_size = min(max(int(page_size), 1), DEFAULT_PAGE_SIZE)
        self.timeout_seconds = float(timeout_seconds)
        self.rate_limit_seconds = max(float(rate_limit_seconds), 0.0)
        self._urlopen = urlopen_func
        self._sleep = sleep_func
        self.http_request_count = 0
        self._last_request_at: float | None = None

    @property
    def url(self) -> str:
        return urljoin(self.base_url, self.api_path)

    def query_metrics(
        self,
        start: datetime,
        end: datetime,
        infer_service_id: str,
        model_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        rows: list[dict[str, Any]] = []
        page_num = 1
        request_count = 0

        while True:
            payload = self._build_payload(start, end, infer_service_id, model_name, page_num)
            response = self._post(payload)
            request_count += 1

            code = int(response.get("code", 0))
            if code != 200:
                raise MaasMonitorApiError(
                    f"MaaS monitor API returned code={code} msg={response.get('msg', '')}",
                    status=code,
                )

            data = response.get("data") or {}
            page_rows = data.get("list") or []
            if not isinstance(page_rows, list):
                raise MaasMonitorApiError("MaaS monitor API data.list is not a list")
            rows.extend(page_rows)

            pages = int(data.get("pages") or 1)
            current_page = int(data.get("pageNum") or page_num)
            if current_page >= pages or not page_rows:
                break
            page_num = current_page + 1

        return rows, request_count

    def _build_payload(
        self,
        start: datetime,
        end: datetime,
        infer_service_id: str,
        model_name: str | None,
        page_num: int,
    ) -> dict[str, Any]:
        filters = [
            {"name": "infer_service_id", "operator": "=", "value": infer_service_id},
            {"name": "timestamp", "operator": ">=", "value": str(int(start.timestamp()))},
            {"name": "timestamp", "operator": "<", "value": str(int(end.timestamp()))},
        ]
        if model_name:
            filters.append({"name": "model_name", "operator": "=", "value": model_name})
        return {
            "dimensions": QUERY_DIMENSIONS,
            "metrics": QUERY_METRICS,
            "filters": filters,
            "page": {
                "pageNum": int(page_num),
                "pageSize": self.page_size,
            },
        }

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._respect_rate_limit()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                self.appcode_header: self.appcode,
            },
            method="POST",
        )
        try:
            self._last_request_at = time.monotonic()
            with self._urlopen(req, timeout=self.timeout_seconds) as resp:
                self.http_request_count += 1
                raw = resp.read()
        except HTTPError as exc:
            raise MaasMonitorApiError(f"MaaS monitor API HTTP error: {exc.code}", status=exc.code) from exc
        except URLError as exc:
            raise MaasMonitorApiError(f"MaaS monitor API request failed: {exc.reason}") from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise MaasMonitorApiError("MaaS monitor API returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise MaasMonitorApiError("MaaS monitor API response is not an object")
        return parsed

    def _respect_rate_limit(self) -> None:
        if self.rate_limit_seconds <= 0 or self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.rate_limit_seconds - elapsed
        if remaining > 0:
            self._sleep(remaining)


def parse_reported_at(value: str, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=tz).replace(second=0, microsecond=0)

    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    else:
        parsed = parsed.astimezone(tz)
    return parsed.replace(second=0, microsecond=0)


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
        return datetime.fromtimestamp(int(float(value)), tz=tz).replace(second=0, microsecond=0, tzinfo=None)
    except Exception:
        return None


def rows_to_latency_frame(rows: list[dict[str, Any]], timezone_name: str = DEFAULT_TIMEZONE) -> pd.DataFrame:
    tz = ZoneInfo(timezone_name)
    records: list[dict[str, Any]] = []
    for row in rows:
        ts = _row_timestamp_to_local(row.get("timestamp"), tz)
        if ts is None:
            continue
        records.append(
            {
                "domain_id": str(row.get("domain_id", "")).strip(),
                "rpm": _to_float(row.get("rpm")),
                "tpm": _to_float(row.get("tpm")),
                "ttft_avg": _to_float(row.get("ttft")),
                "tpot_avg": _to_float(row.get("tpot")),
                "prompt_tokens": _to_float(row.get("prompt_tokens")),
                "completion_tokens": _to_float(row.get("completion_tokens")),
                "collect_time_std": ts.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return pd.DataFrame.from_records(records)


def summarize_latency_frame(df: pd.DataFrame, reported_at: datetime) -> dict[str, Any]:
    if df.empty:
        return {
            "row_count": 0,
            "system_rpm": 0.0,
            "system_tpm": 0.0,
            "system_ttft": 0.0,
            "system_tpot": 0.0,
        }

    prepared = _prepare_optional_latency_frame(df)
    if prepared.empty:
        return {
            "row_count": 0,
            "system_rpm": 0.0,
            "system_tpm": 0.0,
            "system_ttft": 0.0,
            "system_tpot": 0.0,
        }
    reported_naive = reported_at.replace(tzinfo=None)
    time_index = pd.DatetimeIndex([pd.Timestamp(reported_naive).floor("min")])
    user_ids = sorted(prepared["domain_id"].astype(str).unique().tolist())
    matrices = _build_metric_matrices(prepared, user_ids, time_index)
    system = _build_system_series(matrices, 1)
    return {
        "row_count": int(len(prepared)),
        "system_rpm": float(system["system_rpm"][0]),
        "system_tpm": float(system["system_tpm"][0]),
        "system_ttft": float(system["system_ttft"][0]),
        "system_tpot": float(system["system_tpot"][0]),
    }


def split_same_time_windows(
    df: pd.DataFrame,
    reported_at: datetime,
    window_before_minutes: int = 10,
    window_after_minutes: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        return df.copy(), df.copy()

    out = df.copy()
    parsed = pd.to_datetime(out["collect_time_std"], errors="coerce").dt.floor("min")
    out = out.loc[parsed.notna()].copy()
    parsed = parsed.loc[parsed.notna()]

    reported = pd.Timestamp(reported_at.replace(tzinfo=None)).floor("min")
    current_start = reported - pd.Timedelta(minutes=window_before_minutes)
    current_end = reported + pd.Timedelta(minutes=window_after_minutes)

    current_mask = (parsed >= current_start) & (parsed < current_end)

    anchor_seconds = reported.hour * 3600 + reported.minute * 60 + reported.second
    anchors = parsed.dt.normalize() + pd.to_timedelta(anchor_seconds, unit="s")
    offsets = np.floor((parsed - anchors).dt.total_seconds().to_numpy(dtype=float) / 60.0).astype(int)
    history_mask = (
        (parsed < current_start)
        & (offsets >= -int(window_before_minutes))
        & (offsets < int(window_after_minutes))
    )

    return out.loc[current_mask].copy(), out.loc[history_mask].copy()


def _empty_detection_result(cfg: LatencyDetectorConfig) -> dict[str, Any]:
    return {
        "events": [],
        "event_reports": [],
        "records": [],
        "system_stats": {
            "hours": 0,
            "event_count": 0,
            "event_hours_count": 0,
            "system_anom_hours_count": 0,
            "ttft": {
                "system_avg": 0.0,
                "system_p95": 0.0,
                "system_max": 0.0,
                "sla": float(cfg.ttft_sla),
                "severe_threshold": float(cfg.ttft_sla * cfg.severe_ratio),
            },
            "tpot": {
                "system_avg": 0.0,
                "system_p95": 0.0,
                "system_max": 0.0,
                "sla": float(cfg.tpot_sla),
                "severe_threshold": float(cfg.tpot_sla * cfg.severe_ratio),
            },
        },
        "config_echo": asdict(cfg),
    }


def analyze_cluster(
    client: MaasMonitorClient,
    reported_at: datetime,
    infer_service_id: str,
    model_name: str | None,
    cfg: LatencyDetectorConfig,
    timezone_name: str = DEFAULT_TIMEZONE,
    history_days: int = 14,
    window_before_minutes: int = 10,
    window_after_minutes: int = 10,
) -> dict[str, Any]:
    first_start = reported_at
    first_end = first_start + timedelta(minutes=1)
    first_rows, first_http_requests = client.query_metrics(first_start, first_end, infer_service_id, model_name)
    first_df = rows_to_latency_frame(first_rows, timezone_name)
    first_summary = summarize_latency_frame(first_df, reported_at)

    first_window = {
        "start": first_start.isoformat(timespec="minutes"),
        "end": first_end.isoformat(timespec="minutes"),
        **first_summary,
    }

    severe = (
        first_summary["system_ttft"] >= cfg.ttft_sla * cfg.severe_ratio
        or first_summary["system_tpot"] >= cfg.tpot_sla * cfg.severe_ratio
    )
    sla_clear = first_summary["system_ttft"] <= cfg.ttft_sla and first_summary["system_tpot"] <= cfg.tpot_sla

    logical_query_count = 1
    http_request_count = first_http_requests

    if severe:
        detection = detect_latency_anomalies(cfg, first_df) if not first_df.empty else _empty_detection_result(cfg)
        return _format_output(
            status="anomaly",
            decision_source="first_window_severe",
            query_count=logical_query_count,
            http_request_count=http_request_count,
            first_window=first_window,
            detection=detection,
            cfg=cfg,
        )

    if sla_clear:
        detection = detect_latency_anomalies(cfg, first_df) if not first_df.empty else _empty_detection_result(cfg)
        return _format_output(
            status="normal",
            decision_source="first_window_sla_clear",
            query_count=logical_query_count,
            http_request_count=http_request_count,
            first_window=first_window,
            detection=detection,
            cfg=cfg,
        )

    second_start = reported_at - timedelta(days=int(history_days), minutes=int(window_before_minutes))
    second_end = reported_at + timedelta(minutes=int(window_after_minutes))
    second_rows, second_http_requests = client.query_metrics(second_start, second_end, infer_service_id, model_name)
    logical_query_count += 1
    http_request_count += second_http_requests

    second_df = rows_to_latency_frame(second_rows, timezone_name)
    current_df, history_df = split_same_time_windows(
        second_df,
        reported_at,
        window_before_minutes=window_before_minutes,
        window_after_minutes=window_after_minutes,
    )

    if current_df.empty:
        detection = _empty_detection_result(cfg)
    else:
        detection = detect_latency_anomalies_with_history(
            cfg,
            current_df,
            history_df,
            reported_at.replace(tzinfo=None),
            window_before_minutes=window_before_minutes,
            window_after_minutes=window_after_minutes,
        )

    status = "anomaly" if detection.get("events") else "normal"
    output = _format_output(
        status=status,
        decision_source="second_window_history",
        query_count=logical_query_count,
        http_request_count=http_request_count,
        first_window=first_window,
        detection=detection,
        cfg=cfg,
    )
    output["second_window"] = {
        "query_start": second_start.isoformat(timespec="minutes"),
        "query_end": second_end.isoformat(timespec="minutes"),
        "current_rows": int(len(current_df)),
        "history_rows": int(len(history_df)),
        "window_before_minutes": int(window_before_minutes),
        "window_after_minutes": int(window_after_minutes),
        "history_days": int(history_days),
    }
    return output


def _format_output(
    status: str,
    decision_source: str,
    query_count: int,
    http_request_count: int,
    first_window: dict[str, Any],
    detection: dict[str, Any],
    cfg: LatencyDetectorConfig,
) -> dict[str, Any]:
    records = detection.get("records", [])
    if isinstance(records, pd.DataFrame):
        records_out = records.to_dict(orient="records")
    else:
        records_out = records
    return {
        "status": status,
        "decision_source": decision_source,
        "query_count": int(query_count),
        "http_request_count": int(http_request_count),
        "first_window": first_window,
        "events": detection.get("events", []),
        "event_reports": detection.get("event_reports", []),
        "records": records_out,
        "system_stats": detection.get("system_stats", {}),
        "config": detection.get("config_echo", asdict(cfg)),
        "time_index": detection.get("time_index", []),
        "time_step_minutes": detection.get("time_step_minutes"),
        "history_baseline": detection.get("history_baseline"),
    }


def to_jsonable(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        return value.item()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items() if v is not None}
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query MaaS monitor API and detect latency anomalies.")
    parser.add_argument("--reported-at", required=True, help="Alert/report time, ISO datetime or epoch seconds.")
    parser.add_argument("--infer-service-id", required=True, help="MaaS infer_service_id filter.")
    parser.add_argument("--model-name", default=None, help="Optional model_name filter.")
    parser.add_argument("--base-url", default=os.environ.get(BASE_URL_ENV, ""), help=f"API base URL. Defaults to ${BASE_URL_ENV}.")
    parser.add_argument("--appcode", default=os.environ.get(APPCODE_ENV, ""), help=f"API appcode. Defaults to ${APPCODE_ENV}.")
    parser.add_argument("--appcode-header", default="appcode", help="HTTP header name for appcode auth.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Timezone for reported-at and same-time windows.")
    parser.add_argument("--history-days", type=int, default=14, help="Historical days for same-time comparison.")
    parser.add_argument("--window-before-minutes", type=int, default=10, help="Minutes before reported-at in the second window.")
    parser.add_argument("--window-after-minutes", type=int, default=10, help="Minutes after reported-at in the second window.")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="API page size, capped at 2000.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0, help="HTTP request timeout.")
    parser.add_argument("--rate-limit-seconds", type=float, default=DEFAULT_RATE_LIMIT_SECONDS, help="Sleep between paged API requests.")
    parser.add_argument("--ttft-sla", type=float, default=DEFAULT_LATENCY_CONFIG.ttft_sla, help="TTFT SLA in ms.")
    parser.add_argument("--tpot-sla", type=float, default=DEFAULT_LATENCY_CONFIG.tpot_sla, help="TPOT SLA in ms.")
    parser.add_argument("--severe-ratio", type=float, default=DEFAULT_LATENCY_CONFIG.severe_ratio, help="Severe threshold ratio.")
    parser.add_argument(
        "--mild-consecutive-windows",
        type=int,
        default=DEFAULT_LATENCY_CONFIG.mild_consecutive_windows,
        help="Consecutive over-SLA minute windows required for mild anomaly.",
    )
    parser.add_argument("--max-events", type=int, default=DEFAULT_LATENCY_CONFIG.max_events, help="Max event windows to keep; 0 means no cap.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = LatencyDetectorConfig(
        ttft_sla=args.ttft_sla,
        tpot_sla=args.tpot_sla,
        severe_ratio=args.severe_ratio,
        mild_consecutive_windows=args.mild_consecutive_windows,
        max_events=args.max_events,
    )
    try:
        reported_at = parse_reported_at(args.reported_at, args.timezone)
        client = MaasMonitorClient(
            base_url=args.base_url,
            appcode=args.appcode,
            appcode_header=args.appcode_header,
            page_size=args.page_size,
            timeout_seconds=args.timeout_seconds,
            rate_limit_seconds=args.rate_limit_seconds,
        )
        result = analyze_cluster(
            client,
            reported_at,
            args.infer_service_id,
            args.model_name,
            cfg,
            timezone_name=args.timezone,
            history_days=args.history_days,
            window_before_minutes=args.window_before_minutes,
            window_after_minutes=args.window_after_minutes,
        )
    except Exception as exc:
        error = {
            "status": "error",
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }
        print(json.dumps(to_jsonable(error), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
