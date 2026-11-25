# parse_msprof
解析ascend profile文件，支持并发和强制重解析

# 🔍 "已解析"如何判定

脚本按 `DEFAULT_MARKERS` 顺序检查每个 `PROF_*` 目录：

| 标志 | 判定条件 |
| :--- | :--- |
| `mindstudio_profiler_output/` | 目录存在**且非空** |
| `ASCEND_PROFILER_OUTPUT/` | 目录存在**且非空** |
| `op_statistic.csv` / `kernel_details.csv` / `op_summary.csv` | 文件存在**且大小 > 0** |

任一命中即视为已解析并跳过。**关键是"非空"**——解析中途失败留下的空壳目录不会被误跳过，下次仍会重跑。

如果你的环境输出的标志文件不同，用 `--marker` 追加，例如：
```bash
python3 parse_all_ranks.py --marker ascend_pytorch_profiler_0.db
```

# 🎯 并行 + 强制组合示例

```bash
# 强制并行重跑 rank_5,6,7 —— 未给 -j，自动 min(3,8)=3，3 个 rank 同时跑
python3 parse_all_ranks.py --rank 5,6,7

# 强制并行重跑 rank 0-15 —— 未给 -j，自动 min(16,8)=8，8 并发
python3 parse_all_ranks.py --rank 0-15

# 强制并行 + 显式并发控制
python3 parse_all_ranks.py --rank 5,6,7 -j 3
python3 parse_all_ranks.py --rank 0-15 -j 16

# 全部强制重跑，8 并发
python3 parse_all_ranks.py --force

# 全部强制重跑，但想串行观察
python3 parse_all_ranks.py --force -j 1

# 只跑 rank_5，跳过已解析（并行退化为单任务）
python3 parse_all_ranks.py --rank 5 --skip-parsed

# 并行8，跳过已经解析的
python3 parse_all_ranks.py -j 8
```

# 📊 行为矩阵（含并行）

| 参数组合 | 处理范围 | 已解析目录 | 默认并发 | 是否并行 |
| :--- | :--- | :--- | :--- | :--- |
| 无参数 | 全部 | 跳过 | `min(N,4)` | ✅ 是 |
| `--rank 5` | rank_5 | **强制重跑** | `min(1,8)=1` | ❌ 单任务退化 |
| `--rank 5,6,7` | 3 个 rank | **强制重跑** | `min(3,8)=3` | ✅ **并行** |
| `--rank 0-15` | 16 个 rank | **强制重跑** | `min(16,8)=8` | ✅ **并行** |
| `--rank 5,6,7 -j 6` | 3 个 rank | **强制重跑** | `min(3,6)=3` | ✅ 并行（受任务数限制） |
| `--rank 5 --skip-parsed` | rank_5 | 跳过 | `min(1,4)=1` | ❌ 单任务 |
| `--force` | 全部 | **强制重跑** | `min(N,8)` | ✅ **并行** |
| `--force -j 1` | 全部 | **强制重跑** | 1 | ❌ 串行 |
| `--rank 5,6,7 --force` | 3 个 rank | **强制重跑** | `min(3,8)=3` | ✅ **并行** |
