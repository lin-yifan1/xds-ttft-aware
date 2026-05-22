# tests/scenarios

CSV 驱动的异常检测场景。每个子目录是一个独立的场景，由两份 CSV + 一份 JSON 元数据组成，可以由 `build.py` 代码生成，也可以完全手写。

## 目录约定

每个场景一个子目录，固定文件名：

```
tests/scenarios/<scenario_name>/
  scenario.json     # 必填。场景元数据（reported_at、infer_service_id、cfg 等）
  history.csv       # 必填。历史同时段数据（即使内容为空，header 必须存在）
  current.csv       # 必填。上报日时间窗内数据
  build.py          # 可选。代码驱动场景的生成器；手写场景可以没有
  README.md         # 可选。场景设计意图
```

## CSV schema

两份 CSV 共用同一份 schema，列顺序：

| 列名 | 类型 | 说明 |
|---|---|---|
| `collect_time_std` | string | `YYYY-MM-DD HH:MM:SS` naive，约定时区 `Asia/Shanghai` |
| `domain_id` | string | 用户/租户 ID |
| `rpm` | number | 每分钟请求数 |
| `tpm` | number | 每分钟 token 数 |
| `ttft` | number | TTFT 平均值，毫秒 |
| `tpot` | number | TPOT 平均值，毫秒 |
| `prompt_tokens` | number | 平均 prompt token 长度 |
| `completion_tokens` | number | 平均 completion token 长度 |

约定：
- **排序**：`(collect_time_std ASC, domain_id ASC)`。
- **时区**：所有 `collect_time_std` 字符串以 `Asia/Shanghai` 解释，文件里不写偏移。
- **数值**：原样写字符串，整数写 `100`、小数写 `30.5`，下游会按 float 解析。
- **行内容**：`history.csv` 放过去 N 天同时段的样本；`current.csv` 放上报日时间窗内的样本。两份合并后由 `analyze_cluster` 按时间窗过滤。

## scenario.json schema

```json
{
  "reported_at": "2026-05-15 10:00:00",
  "infer_service_id": "svc-test-001",
  "model_name": null,
  "history_days": 14,
  "window_before_minutes": 10,
  "window_after_minutes": 10,
  "config": {
    "ttft_sla": 15000,
    "tpot_sla": 50,
    "severe_ratio": 7,
    "mild_consecutive_windows": 10
  }
}
```

- `reported_at`：与 CSV 同格式同时区。
- `config`：可选；支持 `LatencyDetectorConfig` 任意子集，缺的用默认值（见 `code/config.py:LatencyDetectorConfig`）。
- 其他字段必填。

## 跑场景

```powershell
uv run python tests/run_scenario.py tests/scenarios/<scenario_name>/
```

`run_scenario.py` 读取三份文件，调 `analyze_cluster` 并把关键字段以 JSON 打印到 stdout。不做断言。

## 手写一个新场景

1. 复制最简单的现有场景为模板：`Copy-Item -Recurse tests/scenarios/ttft_only_new_user tests/scenarios/my_case`。
2. 编辑 `my_case/scenario.json`、`history.csv`、`current.csv`。
3. 删掉 `build.py`（如果不需要代码生成）。
4. 跑：`uv run python tests/run_scenario.py tests/scenarios/my_case/`。

## 写一个代码驱动的新场景

复制 `ttft_only_new_user/build.py` 改顶层常量与 `build_history_rows`/`build_current_rows`，CSV 与 JSON 由 `build.py` 一次产出，末尾自动调 `run_scenario` 自验证。
