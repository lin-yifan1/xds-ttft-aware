# 本地 Web：池子时延异常检测（Flask + Plotly）

当前版本的主流程是 SQLite-only：

1. 从原始指标 CSV 清洗生成明细 SQLite
2. 从明细 SQLite 按时间粒度聚合生成聚合 SQLite
3. 在网页中上传聚合 SQLite，选择池子 / 服务并分析 TTFT / TPOT 异常

## 1. 安装依赖

安装 uv 后，在项目根目录执行：

```powershell
uv sync
```

`uv sync` 会按 `.python-version` 创建本地虚拟环境 `.venv`，并根据 `pyproject.toml` / `uv.lock` 安装依赖。依赖声明以 `pyproject.toml` 为准，锁定版本记录在 `uv.lock`。

## 2. 准备输入数据

如果手里已经有符合要求的聚合 SQLite，可以直接跳到第 3 步。

如果手里是原始 CSV，默认处理链路如下：

```powershell
uv run python code/process_data2_to_excel.py
uv run python code/aggregate_processed_metrics.py --granularity 1h
```

`process_data2_to_excel.py` 会读取 `data2/*.csv`，输出明细库：

- `result/new_data_processed.sqlite`

`aggregate_processed_metrics.py` 会读取明细库，输出聚合库：

- `result/new_data_aggregated.sqlite`

`--granularity` 支持 `1h`，也支持任意正整数分钟粒度，例如 `1min`、`5min`、`10min`、`30min`。

也可以显式指定输入输出路径：

```powershell
uv run python code/process_data2_to_excel.py --output result/processed_rows.sqlite
uv run python code/aggregate_processed_metrics.py --input result/processed_rows.sqlite --output result/aggregated_metrics.sqlite --granularity 1min
```

如果需要本地造一份演示数据，可以先执行：

```powershell
uv run python code/generate_fake_data2.py
```

再运行上面的两个处理脚本。

## 3. 启动网页

```powershell
uv run python code/webapp.py
```

浏览器访问：

- `http://127.0.0.1:5000`

网页只接收聚合 SQLite，例如默认产物 `result/new_data_aggregated.sqlite`。

## 4. 网页使用流程

1. 上传聚合后的 SQLite 文件
2. 选择要分析的池子 / 服务分组
3. 选择灵敏度和保留事件数
4. 进入结果页查看系统异常、事件根因和用户明细

结果页包含：

- 系统 TTFT / TPOT 与 RPM / TPM 时序图
- 异常事件列表
- 根因用户列表
- 单用户详情页
- 事件详情页
- CSV / JSON 导出

## 5. SQLite 输入格式要求

上传到网页的文件必须包含表 `aggregated_metrics`，每行是一条已经按时间粒度聚合后的用户指标。

表至少需要这些列：

- `infer_service_id`
- `service_name`
- `domain_id`
- `rpm`
- `tpm`
- `ttft_avg`
- `tpot_avg`
- `prompt_tokens`
- `completion_tokens`
- `collect_time_std`

说明：

- `infer_service_id + service_name` 用于在网页中组织池子 / 服务选择项
- `domain_id` 会作为用户维度进入异常检测和根因定位
- `collect_time_std` 会在后端自动解析并补齐时间轴
- 同一 `domain_id + collect_time_std` 的重复记录会先合并
- 系统层指标会按 `rpm` 加权计算 TTFT / TPOT / token 长度均值

## 6. 核心脚本

- `code/webapp.py`：Web 入口，上传并分析聚合 SQLite
- `code/latency_detector.py`：延迟异常检测与根因定位
- `code/process_data2_to_excel.py`：把 `data2/*.csv` 清洗为明细 SQLite
- `code/aggregate_processed_metrics.py`：从明细 SQLite 按 `1h` / 正整数分钟粒度聚合为结果 SQLite
