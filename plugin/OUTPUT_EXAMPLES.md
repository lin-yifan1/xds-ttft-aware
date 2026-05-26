# `main.py` 完整输出示例

本文档对 `main.py` 在不同情况下产出的 stdout/stderr 给出**端到端的实际示例**（含进度日志 + 最终 JSON）。字段语义见 [README.md §5](README.md)。

> ⚠️ **输出通道**：`main.py` 通过 `logging.basicConfig` 把进度日志和最终 JSON 都打到 **stderr**（Python `logging` 默认行为）。stdout 实际上为空。
> 若需把最终 JSON 喂到下游，可重定向：`python main.py ... 2>&1 1>/dev/null | tail -n +N`，或在调用方修改 logging handler。
>
> 退出码：`anomaly / normal / no_data` 均为 `0`；`error` 为 `1`。

---

## 1. `status = anomaly` — 命中事件 + 给出 culprits

**典型场景**：上报租户 `d-001` 在 `2026-05-22T10:30+08:00` 触发系统级双异常（TTFT 单点重度突刺 + TPOT 持续轻度超标），算法识别出 3 个根因租户。

### 调用

```bash
python main.py "d-001" "s-abc" "2026-05-22T10:30:00+08:00" \
    "https://modelarts-test-internal.cn-north-7.myhuaweicloud.com/v1/maas/om/data/query" \
    "Ip2Ahr4...PYxw" \
    "a027d8f4e6cb4bfe88744c72a6d6d620" \
    "52ed5b89fd39497eaff88e3589d32d87"
```

### stderr

```
[round1] query service=s-abc window=2026-05-22T10:00+08:00~2026-05-22T11:00+08:00
[round1] rows=362 first_row={'timestamp': 1779327600000, 'domain_id': 'd-001', 'infer_service_id': 's-abc', 'ttft_avg': 18234.5, 'tpot_avg': 52.1, 'success_cnt': 124, 'error_cnt': 3, 'prompt_tokens': 812.0, 'completion_tokens': 240.5, 'rpm': 127, 'tpm': 130436}
[round1] df_rows=358 after filter
[round1] event scope=both candidates=6 uids=['d-003', 'd-001', 'd-005', 'd-002', 'd-004', 'd-006']
[round2] query history candidates=6 window=2026-05-08T10:20+08:00~2026-05-22T10:40+08:00
{
  "status": "anomaly",
  "alert": {
    "domain_id": "d-001",
    "service_id": "s-abc",
    "reported_at": "2026-05-22T10:30+08:00"
  },
  "config_echo": {
    "ttft_sla": 15000.0,
    "tpot_sla": 50.0,
    "severe_ratio": 7.0,
    "mild_consecutive_windows": 10,
    "event_merge_gap": 0,
    "max_events": 5,
    "min_baseline_points": 6,
    "culprit_top_k": 3,
    "culprit_cum_ratio": 0.8,
    "culprit_min_ratio": 0.05,
    "history_days": 14,
    "candidate_top_n": 6,
    "window_before_minutes": 30,
    "window_after_minutes": 30,
    "history_same_time_minutes": 10,
    "page_size": 2000,
    "timeout_seconds": 30.0,
    "timezone": "Asia/Shanghai"
  },
  "system_stats": {
    "hours": 60,
    "event_count": 1,
    "system_anom_hours_count": 10,
    "ttft": {
      "system_avg": 22148.6,
      "system_p95": 95412.3,
      "system_max": 151820.0,
      "sla": 15000.0,
      "severe_threshold": 105000.0
    },
    "tpot": {
      "system_avg": 64.8,
      "system_p95": 178.4,
      "system_max": 312.5,
      "sla": 50.0,
      "severe_threshold": 350.0
    },
    "rpm": {
      "system_avg": 1284.5,
      "system_p95": 2156.0,
      "system_max": 2487.0
    },
    "tpm": {
      "system_avg": 1318420.0,
      "system_p95": 2241680.0,
      "system_max": 2587340.0
    },
    "prompt_tokens": {
      "system_avg": 810.4,
      "system_p95": 1124.6,
      "system_max": 1342.0
    },
    "completion_tokens": {
      "system_avg": 248.7,
      "system_p95": 412.0,
      "system_max": 538.5
    }
  },
  "events": [
    {
      "start": "2026-05-22T10:25+08:00",
      "end": "2026-05-22T10:34+08:00",
      "duration_minutes": 10,
      "scope": "both",
      "system_peak_ttft_time": "2026-05-22T10:30+08:00",
      "system_peak_ttft": 151820.0,
      "system_peak_tpot_time": "2026-05-22T10:31+08:00",
      "system_peak_tpot": 312.5
    }
  ],
  "culprits": [
    {
      "domain_id": "d-003",
      "is_alert_reporter": false,
      "score": 0.4218,
      "score_ratio": 0.5512,
      "driver_signal": "traffic_family_dominant",
      "length_signal": "traffic_dominant",
      "rpm_excess_ratio": 0.6248,
      "tpm_excess_ratio": 0.5871,
      "prompt_delta_ratio": 0.1142,
      "completion_delta_ratio": 0.0987,
      "peak_time": "2026-05-22T10:30+08:00",
      "peak_rpm": 412.0,
      "peak_tpm": 463240.0,
      "peak_ttft": 168342.0,
      "peak_tpot": 287.4,
      "peak_prompt_tokens": 824.0,
      "peak_completion_tokens": 263.5
    },
    {
      "domain_id": "d-001",
      "is_alert_reporter": true,
      "score": 0.1842,
      "score_ratio": 0.2407,
      "driver_signal": "length_family_dominant",
      "length_signal": "io_shift_joint",
      "rpm_excess_ratio": 0.0824,
      "tpm_excess_ratio": 0.0961,
      "prompt_delta_ratio": 0.4128,
      "completion_delta_ratio": 0.4862,
      "peak_time": "2026-05-22T10:31+08:00",
      "peak_rpm": 142.0,
      "peak_tpm": 218430.0,
      "peak_ttft": 96284.0,
      "peak_tpot": 304.1,
      "peak_prompt_tokens": 1342.0,
      "peak_completion_tokens": 538.5
    },
    {
      "domain_id": "d-005",
      "is_alert_reporter": false,
      "score": 0.1246,
      "score_ratio": 0.1628,
      "driver_signal": "traffic_length_mixed",
      "length_signal": "length_shift_mixed",
      "rpm_excess_ratio": 0.1843,
      "tpm_excess_ratio": 0.2014,
      "prompt_delta_ratio": 0.2245,
      "completion_delta_ratio": 0.1632,
      "peak_time": "2026-05-22T10:29+08:00",
      "peak_rpm": 218.0,
      "peak_tpm": 240620.0,
      "peak_ttft": 78420.0,
      "peak_tpot": 198.2,
      "peak_prompt_tokens": 1042.0,
      "peak_completion_tokens": 312.0
    }
  ],
  "api_call_count": 2,
  "history_baseline": {
    "candidates": ["d-003", "d-001", "d-005", "d-002", "d-004", "d-006"],
    "history_rows": 1632,
    "history_days": 14,
    "history_same_time_minutes": 10
  }
}
```

### 读法速记

- `system_stats.system_anom_hours_count = 10` 与 `events[0].duration_minutes = 10` 对齐，说明事件窗口刚好覆盖所有异常分钟。
- `events[0].scope = "both"` 决定 `SCORE_WEIGHTS_BY_SCOPE["both"] = (0.225, 0.20, 0.275, 0.30)`，长度类权重整体高于流量类，因此 `d-001`（length 主导）排到 #2。
- `culprits[0].score_ratio + culprits[1].score_ratio = 0.79`，仍 `< culprit_cum_ratio (0.8)`，所以继续遍历到第 3 个；累计 0.95 后或达到 `culprit_top_k = 3` 时停止。
- `api_call_count = 2`：Round 1 + Round 2 各一次，未触发分页。

---

## 2. `status = normal` — 系统级有事件但不覆盖 `reported_at`，或完全无事件

**典型场景**：服务在 `reported_at` 前后 30 分钟内确实出现过短暂尖刺，但 `reported_at` 落在两段正常区间之间；或全程平稳。此时 **跳过 Round 2**，不计算 culprits。

```
[round1] query service=s-abc window=2026-05-22T10:00+08:00~2026-05-22T11:00+08:00
[round1] rows=358 first_row={'timestamp': 1779327600000, 'domain_id': 'd-001', ...}
[round1] df_rows=355 after filter
{
  "status": "normal",
  "alert": {
    "domain_id": "d-001",
    "service_id": "s-abc",
    "reported_at": "2026-05-22T10:30+08:00"
  },
  "config_echo": {
    "ttft_sla": 15000.0,
    "tpot_sla": 50.0,
    "severe_ratio": 7.0,
    "mild_consecutive_windows": 10,
    "event_merge_gap": 0,
    "max_events": 5,
    "min_baseline_points": 6,
    "culprit_top_k": 3,
    "culprit_cum_ratio": 0.8,
    "culprit_min_ratio": 0.05,
    "history_days": 14,
    "candidate_top_n": 6,
    "window_before_minutes": 30,
    "window_after_minutes": 30,
    "history_same_time_minutes": 10,
    "page_size": 2000,
    "timeout_seconds": 30.0,
    "timezone": "Asia/Shanghai"
  },
  "system_stats": {
    "hours": 60,
    "event_count": 0,
    "system_anom_hours_count": 0,
    "ttft": {
      "system_avg": 8420.4,
      "system_p95": 13280.0,
      "system_max": 14920.5,
      "sla": 15000.0,
      "severe_threshold": 105000.0
    },
    "tpot": {
      "system_avg": 32.4,
      "system_p95": 47.8,
      "system_max": 49.6,
      "sla": 50.0,
      "severe_threshold": 350.0
    },
    "rpm": {"system_avg": 1280.5, "system_p95": 1942.0, "system_max": 2106.0},
    "tpm": {"system_avg": 1284320.0, "system_p95": 1942680.0, "system_max": 2106340.0},
    "prompt_tokens": {"system_avg": 802.4, "system_p95": 1024.0, "system_max": 1142.0},
    "completion_tokens": {"system_avg": 246.3, "system_p95": 384.0, "system_max": 412.0}
  },
  "events": [],
  "culprits": [],
  "api_call_count": 1
}
```

- 注意 `events: []` 与 `culprits: []`，且**没有** `history_baseline` 字段（因为 Round 2 没跑）。
- `system_stats.event_count = 0`，TTFT/TPOT max 均未触及 SLA。
- `api_call_count = 1`：只跑了 Round 1。

---

## 3. `status = no_data` — Round 1 返回空

**典型场景**：传入了不存在的 `service_id`，或该 service 在 `reported_at ± 30min` 内确实没有任何分钟级数据上报。

```
[round1] query service=s-not-exist window=2026-05-22T10:00+08:00~2026-05-22T11:00+08:00
[round1] rows=0 first_row=None
[round1] df_rows=0 after filter
{
  "status": "no_data",
  "alert": {
    "domain_id": "d-001",
    "service_id": "s-not-exist",
    "reported_at": "2026-05-22T10:30+08:00"
  },
  "config_echo": {
    "ttft_sla": 15000.0,
    "tpot_sla": 50.0,
    "severe_ratio": 7.0,
    "mild_consecutive_windows": 10,
    "event_merge_gap": 0,
    "max_events": 5,
    "min_baseline_points": 6,
    "culprit_top_k": 3,
    "culprit_cum_ratio": 0.8,
    "culprit_min_ratio": 0.05,
    "history_days": 14,
    "candidate_top_n": 6,
    "window_before_minutes": 30,
    "window_after_minutes": 30,
    "history_same_time_minutes": 10,
    "page_size": 2000,
    "timeout_seconds": 30.0,
    "timezone": "Asia/Shanghai"
  },
  "api_call_count": 1,
  "events": [],
  "culprits": []
}
```

- 与 `normal` 的关键差异：**没有** `system_stats`（因为根本没有数据建系统级序列）。
- 同样跳过 Round 2，`api_call_count = 1`。

---

## 4. `status = error` — 入参/API/解析失败

四种 `error_type` 都来自 `main()` 里的异常分支（[main.py:1181-1204](main.py:1181)），均以 `exit code = 1` 退出。

### 4.1 `InvalidArgs` — 位置参数数量不对

```bash
python main.py "d-001" "s-abc"
```

```
{
  "status": "error",
  "error_type": "InvalidArgs",
  "error_msg": "expected 7 positional args (domain_id, service_id, time, maasApiurl, appcode, applydomainid, applyprojectid), got 2"
}
```

### 4.2 `MaasApiError` — API 网络错误 / 非 200 / `code != 200`

API 网关认证失败：

```bash
python main.py "d-001" "s-abc" "2026-05-22T10:30:00+08:00" \
    "https://modelarts-test-internal.cn-north-7.myhuaweicloud.com/v1/maas/om/data/query" \
    "wrong-appcode" "a027d8f4..." "52ed5b89..."
```

```
[round1] query service=s-abc window=2026-05-22T10:00+08:00~2026-05-22T11:00+08:00
{
  "status": "error",
  "error_type": "MaasApiError",
  "error_msg": "MaaS API HTTP 401: {\"error_code\":\"APIG.0301\",\"error_msg\":\"Incorrect AppCode\"}",
  "api_status": 401
}
```

- `api_status` 是 HTTP 状态码（或后端 `code` 字段）；连接超时等网络错误时 `api_status = null`。

### 4.3 `ValueError` — 时间参数无法解析

```bash
python main.py "d-001" "s-abc" "not-a-time" "https://..." "appcode" "domid" "projid"
```

```
{
  "status": "error",
  "error_type": "ValueError",
  "error_msg": "time argument is not valid ISO 8601 or timestamp: 'not-a-time'"
}
```

也覆盖 `MaasClient` 构造时缺失字段（`maasApiurl is required` 等）。

### 4.4 兜底 `Exception` — 其他未预期异常

例如 MaaS API 返回的 `list` 字段不是数组，或 JSON 解析失败：

```
[round1] query service=s-abc window=2026-05-22T10:00+08:00~2026-05-22T11:00+08:00
{
  "status": "error",
  "error_type": "RuntimeError",
  "error_msg": "MaaS API list is not a list"
}
```

`error_type` 直接取异常类名（`exc.__class__.__name__`），便于上层路由。

---

## 5. 字段速查表

| 字段 | anomaly | normal | no_data | error |
| --- | :---: | :---: | :---: | :---: |
| `status` | ✓ | ✓ | ✓ | ✓ |
| `alert` | ✓ | ✓ | ✓ | ✗ |
| `config_echo` | ✓ | ✓ | ✓ | ✗ |
| `system_stats` | ✓ | ✓ | ✗ | ✗ |
| `events` | 长度 1 | `[]` | `[]` | ✗ |
| `culprits` | 1~`culprit_top_k` | `[]` | `[]` | ✗ |
| `api_call_count` | ✓ (≥2) | ✓ (=1) | ✓ (=1) | ✗ |
| `history_baseline` | ✓ | ✗ | ✗ | ✗ |
| `warning` | 偶发 | ✗ | ✗ | ✗ |
| `error_type` / `error_msg` | ✗ | ✗ | ✗ | ✓ |
| `api_status` | ✗ | ✗ | ✗ | 仅 `MaasApiError` |
