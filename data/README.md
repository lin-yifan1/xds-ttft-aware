# data

该目录下的数据属于旧版本遗留样例。

当前版本默认从 `data2/` 读取原始 CSV，并通过：

- `code/process_data2_to_excel.py`
- `code/aggregate_processed_metrics.py`

生成可供网页上传分析的聚合 SQLite。
