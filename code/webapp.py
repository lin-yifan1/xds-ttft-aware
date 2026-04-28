from __future__ import annotations



import json

import os
import sqlite3

import uuid


from datetime import datetime, timezone, timedelta
from pathlib import Path

from time import perf_counter

from typing import Any



import plotly.graph_objects as go

import plotly.io as pio

from plotly.subplots import make_subplots

import numpy as np

import pandas as pd

from flask import (

    Flask,

    Response,

    flash,

    redirect,

    render_template,

    request,

    url_for,

)

from werkzeug.utils import secure_filename



from config import (
    AGGREGATED_SQLITE_PATH,
    CHART_MARGIN,
    CHART_TEMPLATE,
    CHART_WIDTH,
    DEFAULT_FLASK_SECRET_KEY,
    DEFAULT_LATENCY_CONFIG,
    DEFAULT_MAX_EVENTS_OPTION,
    DEFAULT_SENSITIVITY,
    FLASK_SECRET_ENV,
    LATENCY_SENSITIVITY_LABELS,
    LATENCY_SENSITIVITY_RATIOS,
    MAX_EVENTS_ALL_OPTION,
    MAX_EVENTS_OPTIONS,
    PLOTLY_INCLUDE_JS,
    POOL_ALL_MARKERS,
    POOL_ALL_SERVICE_LABEL,
    POOL_UNNAMED_SERVICE_LABEL,
    PROJECT_ROOT,
    SENSITIVITY_OPTIONS,
    SYSTEM_CHART_HEIGHT,
    TIMING_PIE_HEIGHT,
    TIMING_PIE_TOP_N,
    UPLOAD_DIR,
    USER_CHART_HEIGHT,
    WEB_DEBUG,
    WEB_HOST,
    WEB_PORT,
    LatencyDetectorConfig,
)
from aggregate_processed_metrics import AGGREGATED_COLUMNS, AGGREGATED_TABLE
from latency_detector import detect_latency_anomalies





UPLOAD_DIR.mkdir(exist_ok=True)


# Large one-minute windows can create very large Plotly payloads. Keep the
# visual trend readable while preserving anomaly and event boundary points.
CHART_MAX_RENDER_POINTS = 3500
EVENT_USER_CONTEXT_POINTS = 180





SQLITE_INPUT_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".s3db"}


def validate_aggregated_database(db_path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (AGGREGATED_TABLE,),
        ).fetchone()
        if not table_exists:
            raise ValueError(f"missing table {AGGREGATED_TABLE!r}")

        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({AGGREGATED_TABLE})")}
        missing = [col for col in AGGREGATED_COLUMNS if col not in columns]
        if missing:
            raise ValueError(f"missing required columns: {missing}")

        rows = conn.execute(
            f"""
            SELECT infer_service_id, service_name, COUNT(*) AS row_count
            FROM {AGGREGATED_TABLE}
            GROUP BY infer_service_id, service_name
            ORDER BY infer_service_id, service_name
            """
        ).fetchall()
        groups = [
            {
                "group_id": str(idx),
                "infer_service_id": str(infer_service_id),
                "service_name": str(service_name),
                "row_count": int(row_count),
            }
            for idx, (infer_service_id, service_name, row_count) in enumerate(rows)
        ]
        if not groups:
            raise ValueError("no service groups found")
        return groups
    finally:
        conn.close()


def build_pool_service_groups(service_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build grouped select options for the pool/service picker."""
    from collections import defaultdict

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in service_groups:
        buckets[str(group["infer_service_id"])].append(group)

    groups: list[dict[str, Any]] = []
    for pool_key in sorted(buckets.keys()):
        options: list[dict[str, Any]] = []
        for group in sorted(buckets[pool_key], key=lambda item: str(item["service_name"])):
            service_name = str(group["service_name"]).strip()
            if service_name in POOL_ALL_MARKERS or service_name == "":
                label = POOL_ALL_SERVICE_LABEL
            else:
                label = service_name or POOL_UNNAMED_SERVICE_LABEL
            options.append(
                {
                    "group_id": group["group_id"],
                    "label": label,
                    "row_count": int(group["row_count"]),
                }
            )
        options.sort(key=lambda o: (0 if o["label"] == POOL_ALL_SERVICE_LABEL else 1, o["label"]))
        groups.append({"pool_key": pool_key, "options": options})
    return groups


def find_service_group(service_groups: list[dict[str, Any]], group_id: str) -> dict[str, Any] | None:
    return next((group for group in service_groups if str(group["group_id"]) == str(group_id)), None)


def load_aggregated_group(db_path: str | os.PathLike[str], group: dict[str, Any]) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(
            f"""
            SELECT domain_id, rpm, tpm, ttft_avg, tpot_avg, prompt_tokens, completion_tokens, collect_time_std
            FROM {AGGREGATED_TABLE}
            WHERE infer_service_id = ? AND service_name = ?
            ORDER BY domain_id, collect_time_std
            """,
            conn,
            params=(group["infer_service_id"], group["service_name"]),
        )
    finally:
        conn.close()


def _pool_upload_display_name(info: dict[str, Any]) -> str:
    return str(info.get("file_name", ""))


def _display_project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _default_database_context() -> dict[str, Any]:
    path = AGGREGATED_SQLITE_PATH
    exists = path.is_file()
    stat = path.stat() if exists else None
    return {
        "path": str(path),
        "display_name": _display_project_path(path),
        "exists": exists,
        "size_mb": (stat.st_size / 1024 / 1024) if stat else None,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S") if stat else "",
    }


def _register_pool_database(app: Flask, db_path: Path, file_name: str, source: str) -> str:
    service_groups = validate_aggregated_database(db_path)
    upload_id = uuid.uuid4().hex
    app.config["POOL_UPLOADS"][upload_id] = {
        "file_path": str(db_path),
        "file_name": file_name,
        "service_groups": service_groups,
        "source": source,
    }
    return upload_id


def _latency_config_for_sensitivity(sensitivity: str, max_events: int) -> LatencyDetectorConfig:
    mode = (sensitivity or DEFAULT_SENSITIVITY).strip().lower()
    default_ratio = LATENCY_SENSITIVITY_RATIOS[DEFAULT_SENSITIVITY]
    return LatencyDetectorConfig(severe_ratio=float(LATENCY_SENSITIVITY_RATIOS.get(mode, default_ratio)), max_events=max_events)


def _latency_sensitivity_label(sensitivity: str) -> str:
    return LATENCY_SENSITIVITY_LABELS.get(
        (sensitivity or DEFAULT_SENSITIVITY).strip().lower(),
        LATENCY_SENSITIVITY_LABELS[DEFAULT_SENSITIVITY],
    )





def _format_seconds(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except Exception:
        return str(value)


def _timing_seconds(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if not np.isfinite(v) or v < 0:
        return 0.0
    return v


def _timing_breakdown(items: list[tuple[str, Any]], top_n: int = TIMING_PIE_TOP_N) -> tuple[list[dict[str, Any]], float]:
    rows = [
        {"label": label, "seconds_value": _timing_seconds(value)}
        for label, value in items
    ]
    rows = [row for row in rows if row["seconds_value"] > 0]
    rows.sort(key=lambda row: row["seconds_value"], reverse=True)

    if top_n > 0 and len(rows) > top_n:
        major = rows[:top_n]
        other_seconds = sum(row["seconds_value"] for row in rows[top_n:])
        if other_seconds > 0:
            major.append({"label": "其他", "seconds_value": other_seconds})
        rows = major

    total = sum(row["seconds_value"] for row in rows)
    for row in rows:
        row["seconds"] = _format_seconds(row["seconds_value"])
        row["percent"] = (row["seconds_value"] / total * 100.0) if total > 0 else 0.0
    return rows, total


def _event_ranges(events: Any) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for item in events or []:
        try:
            a = int(item[0])
            b = int(item[1])
        except Exception:
            continue
        if b < a:
            a, b = b, a
        ranges.append((a, b))
    return ranges


def _chart_sample_indices(length: int, events: Any = None, *masks: Any) -> np.ndarray:
    if length <= 0:
        return np.asarray([], dtype=int)
    if CHART_MAX_RENDER_POINTS <= 0 or length <= CHART_MAX_RENDER_POINTS:
        return np.arange(length, dtype=int)

    keep: list[int] = [0, length - 1]
    for mask in masks:
        idx = np.where(np.asarray(mask, dtype=bool))[0]
        keep.extend(int(i) for i in idx)
    for a, b in _event_ranges(events):
        keep.extend([a, b])

    base = np.linspace(0, length - 1, num=CHART_MAX_RENDER_POINTS, dtype=int)
    keep_arr = np.asarray([i for i in keep if 0 <= i < length], dtype=int)
    if keep_arr.size:
        return np.unique(np.concatenate([base, keep_arr]))
    return np.unique(base)


def _slice_event_ranges(events: Any, start_idx: int, end_idx: int) -> list[tuple[int, int]]:
    sliced: list[tuple[int, int]] = []
    for a, b in _event_ranges(events):
        if b < start_idx or a > end_idx:
            continue
        sliced.append((max(a, start_idx) - start_idx, min(b, end_idx) - start_idx))
    return sliced


def create_app() -> Flask:

    # webapp.py lives under code/, so point Flask at the project template/static dirs.

    app = Flask(

        __name__,

        template_folder=str(PROJECT_ROOT / "templates"),

        static_folder=str(PROJECT_ROOT / "static"),

    )

    app.secret_key = os.environ.get(FLASK_SECRET_ENV, DEFAULT_FLASK_SECRET_KEY)



    # In-memory session cache: session_id -> dict(result, meta, created_at)

    app.config["SESSIONS"] = {}

    # Pool analysis upload cache: upload_id -> { file_path, service_groups, file_name }.

    app.config["POOL_UPLOADS"] = {}



    @app.get("/")

    def index():

        return render_template("pool_upload.html", default_database=_default_database_context())



    @app.get("/pool")

    def pool_index():

        return render_template("pool_upload.html", default_database=_default_database_context())



    @app.post("/pool")

    def pool_upload():
        f_database = request.files.get("file_database")
        has_file = bool(f_database and f_database.filename)
        if not has_file:
            default_path = AGGREGATED_SQLITE_PATH
            if not default_path.is_file():
                flash("默认 SQLite 不存在，请手动选择一个聚合 SQLite 数据库文件", "danger")
                return redirect(url_for("pool_index"))

            try:
                upload_id = _register_pool_database(
                    app,
                    default_path,
                    _display_project_path(default_path),
                    "default",
                )
            except Exception as e:
                flash(f"读取默认 SQLite 失败：{e}", "danger")
                return redirect(url_for("pool_index"))

            return redirect(url_for("pool_select", upload_id=upload_id))

        suffix = Path(f_database.filename).suffix.lower()
        if suffix not in SQLITE_INPUT_SUFFIXES:
            flash("请上传 SQLite 文件（.sqlite / .sqlite3 / .db / .s3db）", "danger")
            return redirect(url_for("pool_index"))

        filename = secure_filename(f_database.filename)
        upload_id = uuid.uuid4().hex
        saved_path = UPLOAD_DIR / f"pool_{upload_id}__latency__{filename}"
        f_database.save(saved_path)

        try:
            upload_id = _register_pool_database(app, saved_path, filename, "upload")
        except Exception as e:
            flash(f"读取 SQLite 失败：{e}", "danger")
            return redirect(url_for("pool_index"))

        return redirect(url_for("pool_select", upload_id=upload_id))



    @app.get("/pool/<upload_id>/select")

    def pool_select(upload_id: str):

        info = app.config["POOL_UPLOADS"].get(upload_id)

        if not info:

            flash("上传信息已失效，请重新上传 SQLite", "warning")

            return redirect(url_for("pool_index"))

        return render_template(

            "pool_select.html",

            upload_id=upload_id,

            file_name=_pool_upload_display_name(info),

            service_group_count=len(info["service_groups"]),

            service_groups=build_pool_service_groups(info["service_groups"]),

            sensitivity_options=SENSITIVITY_OPTIONS,

            sensitivity_labels=LATENCY_SENSITIVITY_LABELS,

            sensitivity_ratios=LATENCY_SENSITIVITY_RATIOS,

            default_sensitivity=DEFAULT_SENSITIVITY,

            max_events_options=MAX_EVENTS_OPTIONS,

            max_events_all_option=MAX_EVENTS_ALL_OPTION,

            default_max_events_option=DEFAULT_MAX_EVENTS_OPTION,

            default_latency_config=DEFAULT_LATENCY_CONFIG,

        )



    @app.post("/pool/<upload_id>/analyze")

    def pool_analyze(upload_id: str):

        info = app.config["POOL_UPLOADS"].get(upload_id)

        if not info:

            flash("上传信息已失效，请重新上传 SQLite", "warning")

            return redirect(url_for("pool_index"))

        group_id = request.form.get("group_id", "").strip()
        service_group = find_service_group(info["service_groups"], group_id)

        if service_group is None:

            flash("请选择有效的池子 / 服务", "danger")

            return redirect(url_for("pool_select", upload_id=upload_id))

        analysis_request_started = perf_counter()
        try:
            load_started = perf_counter()
            df = load_aggregated_group(info["file_path"], service_group)
            file_load_seconds = perf_counter() - load_started
            if df.empty:
                raise ValueError("当前池子 / 服务没有可分析数据")

            config_started = perf_counter()
            sensitivity = request.form.get("sensitivity", DEFAULT_SENSITIVITY).strip() or DEFAULT_SENSITIVITY
            max_events_option = request.form.get("max_events_option", DEFAULT_MAX_EVENTS_OPTION).strip().lower()
            if max_events_option == MAX_EVENTS_ALL_OPTION:
                max_events = 0
            else:
                try:
                    max_events = int(max_events_option)
                except Exception:
                    max_events = int(DEFAULT_MAX_EVENTS_OPTION)

            cfg = _latency_config_for_sensitivity(sensitivity, max_events)
            config_seconds = perf_counter() - config_started
            detect_started = perf_counter()
            res = detect_latency_anomalies(cfg, df)
            detect_seconds = perf_counter() - detect_started
            t = res["time_index"]

            session_id = uuid.uuid4().hex
            session_build_started = perf_counter()
            session_payload = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "analysis_mode": "latency",
                "file_name": (
                    f"{info['file_name']} "
                    f"({service_group['infer_service_id']} / {service_group['service_name']})"
                ),
                "saved_path": info["file_path"],
                "upload_id": upload_id,
                "infer_service_id": service_group["infer_service_id"],
                "service_name": service_group["service_name"],
                "sensitivity_mode": sensitivity,
                "sensitivity_mode_label": _latency_sensitivity_label(sensitivity),
                "file_load_seconds": float(file_load_seconds),
                "detect_seconds": float(detect_seconds),
                "detection_timings": res.get("detection_timings", {}),
                "t": [dt.isoformat() for dt in t],
                "cfg": res["config_echo"],
                "user_ids": res["user_ids"],
                "system_rpm": np.asarray(res["system_rpm"], dtype=float).tolist(),
                "system_tpm": np.asarray(res["system_tpm"], dtype=float).tolist(),
                "system_ttft": np.asarray(res["system_ttft"], dtype=float).tolist(),
                "system_tpot": np.asarray(res["system_tpot"], dtype=float).tolist(),
                "system_prompt_tokens": np.asarray(res["system_prompt_tokens"], dtype=float).tolist(),
                "system_completion_tokens": np.asarray(res["system_completion_tokens"], dtype=float).tolist(),
                "sys_anom": np.asarray(res["sys_anom"], dtype=bool).tolist(),
                "sys_anom_ttft": np.asarray(res["sys_anom_ttft"], dtype=bool).tolist(),
                "sys_anom_tpot": np.asarray(res["sys_anom_tpot"], dtype=bool).tolist(),
                "sys_event_mask": np.asarray(res["sys_event_mask"], dtype=bool).tolist(),
                "events": res["events"],
                "event_reports": res["event_reports"],
                "records_json": res["records"].to_dict(orient="records"),
                "records_csv": res["records"].to_csv(index=False, encoding="utf-8"),
                "system_stats": res["system_stats"],
                "rpm": np.asarray(res["rpm"], dtype=float).tolist(),
                "tpm": np.asarray(res["tpm"], dtype=float).tolist(),
                "ttft": np.asarray(res["ttft"], dtype=float).tolist(),
                "tpot": np.asarray(res["tpot"], dtype=float).tolist(),
                "prompt_tokens": np.asarray(res["prompt_tokens"], dtype=float).tolist(),
                "completion_tokens": np.asarray(res["completion_tokens"], dtype=float).tolist(),
                "flags": np.asarray(res["flags"], dtype=bool).tolist(),
                "time_step_minutes": int(res["time_step_minutes"]),
            }
            session_build_seconds = perf_counter() - session_build_started
            session_payload["analysis_timings"] = {
                "file_load_seconds": float(file_load_seconds),
                "config_seconds": float(config_seconds),
                "detect_seconds": float(detect_seconds),
                "session_build_seconds": float(session_build_seconds),
                "analysis_total_seconds": float(perf_counter() - analysis_request_started),
            }
            app.config["SESSIONS"][session_id] = session_payload
            return redirect(url_for("results", session_id=session_id))
        except Exception as e:
            flash(f"分析失败：{e}", "danger")
            return redirect(url_for("pool_select", upload_id=upload_id))



    @app.get("/results/<session_id>")

    def results(session_id: str):

        s = _get_session(app, session_id)

        if s is None:

            flash("分析结果已失效，请重新分析", "warning")

            return redirect(url_for("index"))


        restore_started = perf_counter()
        t = [datetime.fromisoformat(x) for x in s["t"]]
        time_restore_seconds = perf_counter() - restore_started
        figure_build_started = perf_counter()
        system_fig = build_latency_system_figure(
            t,
            np_array(s["system_ttft"]),
            np_array(s["system_tpot"]),
            np_array(s["system_rpm"]),
            np_array(s["system_tpm"]),
            np_bool(s["sys_anom_ttft"]),
            np_bool(s["sys_anom_tpot"]),
            s.get("events", []),
            float(s["cfg"].get("ttft_sla", DEFAULT_LATENCY_CONFIG.ttft_sla)),
            float(s["cfg"].get("tpot_sla", DEFAULT_LATENCY_CONFIG.tpot_sla)),
            float(s["cfg"].get("ttft_sla", DEFAULT_LATENCY_CONFIG.ttft_sla))
            * float(s["cfg"].get("severe_ratio", DEFAULT_LATENCY_CONFIG.severe_ratio)),
            float(s["cfg"].get("tpot_sla", DEFAULT_LATENCY_CONFIG.tpot_sla))
            * float(s["cfg"].get("severe_ratio", DEFAULT_LATENCY_CONFIG.severe_ratio)),
        )
        system_figure_seconds = perf_counter() - figure_build_started
        figure_html_started = perf_counter()
        system_fig_html = pio.to_html(system_fig, full_html=False, include_plotlyjs=False)
        figure_html_seconds = perf_counter() - figure_html_started

        user_summary_started = perf_counter()
        user_ids = s["user_ids"]
        rpm = np_array(s["rpm"])
        tpm = np_array(s["tpm"])
        ttft = np_array(s["ttft"])
        tpot = np_array(s["tpot"])
        prompt = np_array(s["prompt_tokens"])
        completion = np_array(s["completion_tokens"])
        flags = np_bool_matrix(s["flags"])
        records_by_uid = {r["user_id"]: r for r in s.get("records_json", [])}
        all_users: list[dict[str, Any]] = []
        for idx, uid in enumerate(user_ids):
            rec = records_by_uid.get(uid, {})
            all_users.append(
                {
                    "user_id": uid,
                    "hit_count": int(flags[idx].sum()),
                    "avg_ttft": float(np.mean(ttft[idx])),
                    "avg_tpot": float(np.mean(tpot[idx])),
                    "avg_rpm": float(np.mean(rpm[idx])),
                    "avg_tpm": float(np.mean(tpm[idx])),
                    "avg_prompt_tokens": float(np.mean(prompt[idx])),
                    "avg_completion_tokens": float(np.mean(completion[idx])),
                    "reason": rec.get("reason", ""),
                }
            )
        all_users.sort(key=lambda u: (-u["hit_count"], -u["avg_ttft"], -u["avg_tpot"], u["user_id"]))
        user_summary_seconds = perf_counter() - user_summary_started

        event_format_started = perf_counter()
        event_reports_view = _format_latency_event_reports_with_time(s.get("event_reports", []), t)
        event_format_seconds = perf_counter() - event_format_started

        analysis_timings = s.get("analysis_timings", {})
        detection_timings = s.get("detection_timings", {})
        detection_timing_sources = [
            ("数据校验", detection_timings.get("prepare_input_seconds", 0.0)),
            ("去重排序", detection_timings.get("dedupe_seconds", 0.0)),
            ("时间索引", detection_timings.get("time_index_seconds", 0.0)),
            ("矩阵构建", detection_timings.get("matrix_build_seconds", 0.0)),
            ("系统序列", detection_timings.get("system_series_seconds", 0.0)),
            ("事件检测", detection_timings.get("event_detection_seconds", 0.0)),
            ("基线计算", detection_timings.get("baseline_seconds", 0.0)),
            ("根因定位", detection_timings.get("rootcause_seconds", 0.0)),
            ("结果记录", detection_timings.get("records_seconds", 0.0)),
            ("统计汇总", detection_timings.get("stats_seconds", 0.0)),
        ]
        if not any(_timing_seconds(value) > 0 for _, value in detection_timing_sources):
            detection_timing_sources = [
                ("异常检测", analysis_timings.get("detect_seconds", s.get("detect_seconds", 0.0)))
            ]
        timing_sources = [
            ("文件加载", analysis_timings.get("file_load_seconds", s.get("file_load_seconds", 0.0))),
            ("参数配置", analysis_timings.get("config_seconds", 0.0)),
            *detection_timing_sources,
            ("结果缓存", analysis_timings.get("session_build_seconds", 0.0)),
            ("时间恢复", time_restore_seconds),
            ("系统图构建", system_figure_seconds),
            ("图表 HTML 生成", figure_html_seconds),
            ("用户汇总", user_summary_seconds),
            ("事件格式化", event_format_seconds),
        ]
        timing_items, timing_total_seconds = _timing_breakdown(timing_sources)
        timing_fig = build_timing_pie_figure(timing_items)
        timing_fig_html = pio.to_html(timing_fig, full_html=False, include_plotlyjs=PLOTLY_INCLUDE_JS)

        rendered = render_template(
            "results.html",
            session_id=session_id,
            pool_select_url=(
                url_for("pool_select", upload_id=s["upload_id"])
                if s.get("upload_id") in app.config["POOL_UPLOADS"]
                else None
            ),
            file_name=s["file_name"],
            start_time=t[0].isoformat(timespec="minutes") if t else "",
            end_time=t[-1].isoformat(timespec="minutes") if t else "",
            cfg=s["cfg"],
            system_stats=s["system_stats"],
            records=s["records_json"][:200],
            event_reports=event_reports_view,
            all_users=all_users,
            system_fig_html=system_fig_html,
            timing_fig_html=timing_fig_html,
            timing_items=timing_items,
            timing_total_seconds=timing_total_seconds,
            sensitivity_mode_label=s.get("sensitivity_mode_label", LATENCY_SENSITIVITY_LABELS[DEFAULT_SENSITIVITY]),
            time_step_minutes=int(s.get("time_step_minutes", 60)),
        )
        return rendered



    @app.get("/user/<session_id>/<user_id>")
    def user_detail(session_id: str, user_id: str):

        s = _get_session(app, session_id)

        if s is None:

            flash("分析结果已失效，请重新分析", "warning")

            return redirect(url_for("index"))



        user_ids = s["user_ids"]

        if user_id not in user_ids:

            flash("用户不存在", "danger")

            return redirect(url_for("results", session_id=session_id))



        idx = user_ids.index(user_id)

        t_all = [datetime.fromisoformat(x) for x in s["t"]]
        events_all = s.get("events", [])
        chart_start_idx = 0
        chart_end_idx = len(t_all) - 1
        is_event_window = False
        event_idx_arg = request.args.get("event_idx")
        if event_idx_arg is not None and t_all:
            try:
                event_idx = int(event_idx_arg)
            except ValueError:
                event_idx = -1
            reports = s.get("event_reports", [])
            if 0 <= event_idx < len(reports):
                ev = reports[event_idx]
                a = int(ev.get("start_hour", 0))
                b = int(ev.get("end_hour", a))
                chart_start_idx = max(0, min(a, b) - EVENT_USER_CONTEXT_POINTS)
                chart_end_idx = min(len(t_all) - 1, max(a, b) + EVENT_USER_CONTEXT_POINTS)
                is_event_window = True

        chart_slice = slice(chart_start_idx, chart_end_idx + 1)
        t = t_all[chart_slice]
        chart_events = _slice_event_ranges(events_all, chart_start_idx, chart_end_idx) if is_event_window else events_all
        if is_event_window and t:
            view_window_label = (
                f"{t[0].strftime('%Y-%m-%d %H:%M')} ~ {t[-1].strftime('%Y-%m-%d %H:%M')}"
                f"（事件前后各 {EVENT_USER_CONTEXT_POINTS} 个时间点）"
            )
        else:
            view_window_label = "全量时间范围"

        ttft_all = np_array(s["ttft"])
        tpot_all = np_array(s["tpot"])
        rpm_all = np_array(s["rpm"])
        tpm_all = np_array(s["tpm"])
        prompt_all = np_array(s["prompt_tokens"])
        completion_all = np_array(s["completion_tokens"])
        flags_all = np_bool_matrix(s["flags"])
        system_ttft = np_array(s["system_ttft"])
        system_tpot = np_array(s["system_tpot"])
        system_rpm = np_array(s["system_rpm"])
        system_tpm = np_array(s["system_tpm"])
        sys_anom_ttft = np_bool(s["sys_anom_ttft"])
        sys_anom_tpot = np_bool(s["sys_anom_tpot"])
        ttft_sla = float(s["cfg"].get("ttft_sla", DEFAULT_LATENCY_CONFIG.ttft_sla))
        tpot_sla = float(s["cfg"].get("tpot_sla", DEFAULT_LATENCY_CONFIG.tpot_sla))
        severe_ratio = float(s["cfg"].get("severe_ratio", DEFAULT_LATENCY_CONFIG.severe_ratio))

        system_fig = build_latency_system_figure(
            t,
            system_ttft[chart_slice],
            system_tpot[chart_slice],
            system_rpm[chart_slice],
            system_tpm[chart_slice],
            sys_anom_ttft[chart_slice],
            sys_anom_tpot[chart_slice],
            chart_events,
            ttft_sla,
            tpot_sla,
            ttft_sla * severe_ratio,
            tpot_sla * severe_ratio,
        )
        system_fig_html = pio.to_html(system_fig, full_html=False, include_plotlyjs=PLOTLY_INCLUDE_JS)

        user_fig = build_latency_user_figure(
            t,
            ttft_all[idx][chart_slice],
            tpot_all[idx][chart_slice],
            rpm_all[idx][chart_slice],
            tpm_all[idx][chart_slice],
            prompt_all[idx][chart_slice],
            completion_all[idx][chart_slice],
            flags_all[idx][chart_slice],
            chart_events,
            ttft_sla,
            tpot_sla,
            ttft_sla * severe_ratio,
            tpot_sla * severe_ratio,
        )
        user_fig_html = pio.to_html(user_fig, full_html=False, include_plotlyjs=False)

        row = next((r for r in s["records_json"] if r["user_id"] == user_id), None)
        if row is None:
            row = {
                "user_id": user_id,
                "hit_count": int(np_bool_matrix(s["flags"])[idx].sum()),
                "hit_hours": "",
                "reason": "",
                "avg_rpm": float(np.mean(np_array(s["rpm"])[idx])),
                "p95_rpm": float(np.quantile(np_array(s["rpm"])[idx], 0.95)),
                "max_rpm": float(np.max(np_array(s["rpm"])[idx])),
                "avg_tpm": float(np.mean(np_array(s["tpm"])[idx])),
                "p95_tpm": float(np.quantile(np_array(s["tpm"])[idx], 0.95)),
                "max_tpm": float(np.max(np_array(s["tpm"])[idx])),
                "avg_ttft": float(np.mean(np_array(s["ttft"])[idx])),
                "p95_ttft": float(np.quantile(np_array(s["ttft"])[idx], 0.95)),
                "max_ttft": float(np.max(np_array(s["ttft"])[idx])),
                "avg_tpot": float(np.mean(np_array(s["tpot"])[idx])),
                "p95_tpot": float(np.quantile(np_array(s["tpot"])[idx], 0.95)),
                "max_tpot": float(np.max(np_array(s["tpot"])[idx])),
                "avg_prompt_tokens": float(np.mean(np_array(s["prompt_tokens"])[idx])),
                "avg_completion_tokens": float(np.mean(np_array(s["completion_tokens"])[idx])),
            }

        return render_template(
            "user.html",
            session_id=session_id,
            file_name=s["file_name"],
            user_id=user_id,
            row=row,
            user_fig_html=user_fig_html,
            system_fig_html=system_fig_html,
            view_window_label=view_window_label,
            is_event_window=is_event_window,
            cfg=s["cfg"],
        )



    @app.get("/event/<session_id>/<int:event_idx>")

    def event_detail(session_id: str, event_idx: int):

        s = _get_session(app, session_id)

        if s is None:

            flash("分析结果已失效，请重新分析", "warning")

            return redirect(url_for("index"))



        reports = s.get("event_reports", [])

        if event_idx < 0 or event_idx >= len(reports):

            flash("事件不存在", "danger")

            return redirect(url_for("results", session_id=session_id))



        ev = reports[event_idx]
        t = [datetime.fromisoformat(x) for x in s["t"]]
        a = int(ev["start_hour"])
        b = int(ev["end_hour"])
        start_dt = t[a].isoformat(timespec="minutes") if 0 <= a < len(t) else ""
        end_dt = t[b].isoformat(timespec="minutes") if 0 <= b < len(t) else ""
        event_view = _format_latency_event_reports_with_time([ev], t)[0]

        return render_template(
            "event.html",
            session_id=session_id,
            file_name=s["file_name"],
            event_idx=event_idx,
            ev=event_view,
            start_dt=start_dt,
            end_dt=end_dt,
            cfg=s["cfg"],
        )



    @app.get("/export/<session_id>")

    def export(session_id: str):

        s = _get_session(app, session_id)

        if s is None:

            return Response("session not found", status=404)



        fmt = request.args.get("format", "csv").lower()

        if fmt == "json":

            return Response(json.dumps(s["records_json"], ensure_ascii=False, indent=2), mimetype="application/json")

        return Response(s["records_csv"], mimetype="text/csv; charset=utf-8")



    return app





def _get_session(app: Flask, session_id: str) -> dict[str, Any] | None:

    return app.config["SESSIONS"].get(session_id)





def np_array(x: Any) -> Any:

    import numpy as np



    return np.asarray(x, dtype=float)





def np_bool(x: Any) -> Any:

    import numpy as np



    return np.asarray(x, dtype=bool)





def np_bool_matrix(x: Any) -> Any:

    import numpy as np



    return np.asarray(x, dtype=bool)


def _format_latency_event_reports_with_time(event_reports: list[dict[str, Any]], t: list[datetime]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = len(t)
    for ev in event_reports:
        e = dict(ev)
        a = int(e.get("start_hour", -1))
        b = int(e.get("end_hour", -1))
        peak_ttft = int(e.get("system_peak_hour_ttft", -1))
        peak_tpot = int(e.get("system_peak_hour_tpot", -1))
        e["start_time_label"] = t[a].strftime("%Y-%m-%d %H:%M") if 0 <= a < n else ""
        e["end_time_label"] = t[b].strftime("%Y-%m-%d %H:%M") if 0 <= b < n else ""
        e["system_peak_time_label_ttft"] = t[peak_ttft].strftime("%Y-%m-%d %H:%M") if 0 <= peak_ttft < n else ""
        e["system_peak_time_label_tpot"] = t[peak_tpot].strftime("%Y-%m-%d %H:%M") if 0 <= peak_tpot < n else ""
        culprits: list[dict[str, Any]] = []
        for culprit in e.get("culprits", []) or []:
            c = dict(culprit)
            peak_hour = int(c.get("peak_hour", -1))
            c["peak_time_label"] = t[peak_hour].strftime("%Y-%m-%d %H:%M") if 0 <= peak_hour < n else ""
            first_active_hour = int(c.get("first_active_hour", -1))
            c["first_active_time_label"] = t[first_active_hour].strftime("%Y-%m-%d %H:%M") if 0 <= first_active_hour < n else ""
            culprits.append(c)
        e["culprits"] = culprits
        new_join_users: list[dict[str, Any]] = []
        for item in e.get("new_join_users", []) or []:
            x = dict(item)
            first_active_hour = int(x.get("first_active_hour", -1))
            x["first_active_time_label"] = t[first_active_hour].strftime("%Y-%m-%d %H:%M") if 0 <= first_active_hour < n else ""
            new_join_users.append(x)
        e["new_join_users"] = new_join_users
        out.append(e)
    return out


def build_timing_pie_figure(timing_items: list[dict[str, Any]]) -> go.Figure:
    fig = go.Figure(
        data=[
            go.Pie(
                labels=[item["label"] for item in timing_items],
                values=[item["seconds_value"] for item in timing_items],
                hole=0.42,
                sort=False,
                textinfo="percent",
                textposition="inside",
                marker=dict(line=dict(color="#ffffff", width=2)),
            )
        ]
    )
    fig.update_layout(
        template=CHART_TEMPLATE,
        height=TIMING_PIE_HEIGHT,
        margin=dict(l=8, r=8, t=12, b=8),
        showlegend=False,
    )
    return fig


def build_latency_system_figure(
    t: list[datetime],
    system_ttft: Any,
    system_tpot: Any,
    system_rpm: Any,
    system_tpm: Any,
    sys_anom_ttft: Any,
    sys_anom_tpot: Any,
    events: Any,
    ttft_sla: float,
    tpot_sla: float,
    ttft_severe: float,
    tpot_severe: float,
) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.55, 0.45],
        specs=[[{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=("系统 TTFT / TPOT", "系统 RPM / TPM"),
    )

    system_ttft = np.asarray(system_ttft, dtype=float)
    system_tpot = np.asarray(system_tpot, dtype=float)
    system_rpm = np.asarray(system_rpm, dtype=float)
    system_tpm = np.asarray(system_tpm, dtype=float)
    sys_anom_ttft = np.asarray(sys_anom_ttft, dtype=bool)
    sys_anom_tpot = np.asarray(sys_anom_tpot, dtype=bool)
    sample_idx = _chart_sample_indices(len(t), events, sys_anom_ttft, sys_anom_tpot)
    sample_t = [t[int(i)] for i in sample_idx]

    fig.add_trace(
        go.Scattergl(x=sample_t, y=system_ttft[sample_idx], mode="lines", name="SystemTTFT", line=dict(color="#d63384")),
        row=1,
        col=1,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scattergl(x=sample_t, y=system_tpot[sample_idx], mode="lines", name="SystemTPOT", line=dict(color="#fd7e14")),
        row=1,
        col=1,
        secondary_y=True,
    )
    fig.add_hline(y=ttft_sla, line_dash="dash", line_color="rgba(214,51,132,0.55)", row=1, col=1, secondary_y=False)
    fig.add_hline(y=ttft_severe, line_dash="dot", line_color="rgba(214,51,132,0.85)", row=1, col=1, secondary_y=False)
    fig.add_hline(y=tpot_sla, line_dash="dash", line_color="rgba(253,126,20,0.55)", row=1, col=1, secondary_y=True)
    fig.add_hline(y=tpot_severe, line_dash="dot", line_color="rgba(253,126,20,0.85)", row=1, col=1, secondary_y=True)

    ttft_idx = np.where(sys_anom_ttft)[0]
    if ttft_idx.size:
        fig.add_trace(
            go.Scattergl(
                x=[t[i] for i in ttft_idx],
                y=[system_ttft[i] for i in ttft_idx],
                mode="markers",
                name="TTFTAnomaly",
                marker=dict(size=8, color="#d63384", symbol="diamond"),
            ),
            row=1,
            col=1,
            secondary_y=False,
        )
    tpot_idx = np.where(sys_anom_tpot)[0]
    if tpot_idx.size:
        fig.add_trace(
            go.Scattergl(
                x=[t[i] for i in tpot_idx],
                y=[system_tpot[i] for i in tpot_idx],
                mode="markers",
                name="TPOTAnomaly",
                marker=dict(size=7, color="#fd7e14", symbol="circle"),
            ),
            row=1,
            col=1,
            secondary_y=True,
        )

    fig.add_trace(
        go.Scattergl(x=sample_t, y=system_rpm[sample_idx], mode="lines", name="SystemRPM", line=dict(color="#0d6efd")),
        row=2,
        col=1,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scattergl(x=sample_t, y=system_tpm[sample_idx], mode="lines", name="SystemTPM", line=dict(color="#20c997")),
        row=2,
        col=1,
        secondary_y=True,
    )

    step = (t[1] - t[0]) if len(t) > 1 else timedelta(hours=1)
    for (a, b) in events or []:
        if a < 0 or b >= len(t):
            continue
        for row in (1, 2):
            fig.add_vrect(
                x0=t[a],
                x1=t[b] + step,
                fillcolor="rgba(255, 193, 7, 0.12)",
                line_width=0,
                row=row,
                col=1,
            )

    fig.update_yaxes(title_text="TTFT (ms)", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="TPOT (ms)", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="RPM", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="TPM", row=2, col=1, secondary_y=True)
    fig.update_xaxes(title_text="时间", row=2, col=1)
    fig.update_layout(
        template=CHART_TEMPLATE,
        width=CHART_WIDTH,
        height=SYSTEM_CHART_HEIGHT,
        margin={**CHART_MARGIN, "t": 72},
        legend=dict(orientation="h", yanchor="bottom", y=1.05, xanchor="left", x=0),
    )
    return fig


def build_latency_user_figure(
    t: list[datetime],
    ttft: Any,
    tpot: Any,
    rpm: Any,
    tpm: Any,
    prompt_tokens: Any,
    completion_tokens: Any,
    flags: Any,
    events: Any,
    ttft_sla: float,
    tpot_sla: float,
    ttft_severe: float,
    tpot_severe: float,
) -> go.Figure:
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.4, 0.3, 0.3],
        specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{"secondary_y": True}]],
        subplot_titles=("用户 TTFT / TPOT", "用户 RPM / TPM", "输入 / 输出 Tokens"),
    )

    ttft = np.asarray(ttft, dtype=float)
    tpot = np.asarray(tpot, dtype=float)
    rpm = np.asarray(rpm, dtype=float)
    tpm = np.asarray(tpm, dtype=float)
    prompt_tokens = np.asarray(prompt_tokens, dtype=float)
    completion_tokens = np.asarray(completion_tokens, dtype=float)
    flags = np.asarray(flags, dtype=bool)
    sample_idx = _chart_sample_indices(len(t), events, flags)
    sample_t = [t[int(i)] for i in sample_idx]

    fig.add_trace(
        go.Scattergl(x=sample_t, y=ttft[sample_idx], mode="lines", name="UserTTFT", line=dict(color="#d63384")),
        row=1,
        col=1,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scattergl(x=sample_t, y=tpot[sample_idx], mode="lines", name="UserTPOT", line=dict(color="#fd7e14")),
        row=1,
        col=1,
        secondary_y=True,
    )
    fig.add_hline(y=ttft_sla, line_dash="dash", line_color="rgba(214,51,132,0.55)", row=1, col=1, secondary_y=False)
    fig.add_hline(y=ttft_severe, line_dash="dot", line_color="rgba(214,51,132,0.85)", row=1, col=1, secondary_y=False)
    fig.add_hline(y=tpot_sla, line_dash="dash", line_color="rgba(253,126,20,0.55)", row=1, col=1, secondary_y=True)
    fig.add_hline(y=tpot_severe, line_dash="dot", line_color="rgba(253,126,20,0.85)", row=1, col=1, secondary_y=True)

    hit_idx = np.where(flags)[0]
    if hit_idx.size:
        fig.add_trace(
            go.Scattergl(
                x=[t[i] for i in hit_idx],
                y=[ttft[i] for i in hit_idx],
                mode="markers",
                name="RootCauseHit",
                marker=dict(size=9, color="#dc3545", symbol="diamond"),
            ),
            row=1,
            col=1,
            secondary_y=False,
        )

    fig.add_trace(
        go.Scattergl(x=sample_t, y=rpm[sample_idx], mode="lines", name="UserRPM", line=dict(color="#0d6efd")),
        row=2,
        col=1,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scattergl(x=sample_t, y=tpm[sample_idx], mode="lines", name="UserTPM", line=dict(color="#20c997")),
        row=2,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(
        go.Scattergl(x=sample_t, y=prompt_tokens[sample_idx], mode="lines", name="PromptTokens", line=dict(color="#6f42c1")),
        row=3,
        col=1,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scattergl(x=sample_t, y=completion_tokens[sample_idx], mode="lines", name="CompletionTokens", line=dict(color="#198754")),
        row=3,
        col=1,
        secondary_y=True,
    )

    step = (t[1] - t[0]) if len(t) > 1 else timedelta(hours=1)
    for (a, b) in events or []:
        if a < 0 or b >= len(t):
            continue
        for row in (1, 2, 3):
            fig.add_vrect(
                x0=t[a],
                x1=t[b] + step,
                fillcolor="rgba(255, 193, 7, 0.12)",
                line_width=0,
                row=row,
                col=1,
            )

    fig.update_yaxes(title_text="TTFT (ms)", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="TPOT (ms)", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="RPM", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="TPM", row=2, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Prompt", row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Completion", row=3, col=1, secondary_y=True)
    fig.update_xaxes(title_text="时间", row=3, col=1)
    fig.update_layout(
        title="用户延迟、流量与 Token 趋势",
        template=CHART_TEMPLATE,
        width=CHART_WIDTH,
        height=USER_CHART_HEIGHT,
        margin=CHART_MARGIN,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig





if __name__ == "__main__":

    app = create_app()

    app.run(host=WEB_HOST, port=WEB_PORT, debug=WEB_DEBUG)



