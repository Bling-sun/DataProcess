# Figure Raw 全量转换为 LeRobot v2.1

本文说明如何把一个日期目录下的 Figure/Tianji 双臂 + xHand 原始数据，全量转换为可供 NVIDIA GR00T 使用的 LeRobot v2.1 数据集。

适用入口：`scripts/start_lerobot_v21_conversion.sh`。它会自动完成环境检查、后台启动、断点续传、GPU 编码、watchdog、统计和最终全量校验。

> 如果需要逐个 episode 看视频、裁剪头尾或人工标记失败，请先阅读 [DATAPROCESS_USER_GUIDE.md](DATAPROCESS_USER_GUIDE.md)。无需人工审阅的整日数据，直接使用本文的批处理命令。

## 1. 最短操作流程

### 1.1 首次使用只需执行一次

```bash
ssh 4090
cd /mnt/sunbing/projects/DataProcess
./setup.sh
```

脚本需要以下系统能力：

- NVIDIA GPU 和 `nvidia-smi`
- FFmpeg、FFprobe 和 `h264_nvenc`
- Python、NumPy、PyArrow

`setup.sh` 负责 Python 依赖；启动脚本还会在真正转换前检查其余能力。

### 1.2 先做一次无副作用检查

把路径和任务描述替换成新数据的真实值：

```bash
cd /mnt/sunbing/projects/DataProcess

./scripts/start_lerobot_v21_conversion.sh \
  --source /mnt/pangyunyi/figure/raw/20260801 \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21 \
  --task "pick up the packaged item with both hands" \
  --dry-run
```

看到 `Preflight passed` 才进行下一步。`--dry-run` 不会创建转换进程。

### 1.3 一键后台启动

去掉最后的 `--dry-run`：

```bash
./scripts/start_lerobot_v21_conversion.sh \
  --source /mnt/pangyunyi/figure/raw/20260801 \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21 \
  --task "pick up the packaged item with both hands"
```

默认行为：

- 输出 20 Hz 数据；
- 使用服务器检测到的全部 GPU 做 8 路 NVENC 并行编码；
- 使用 8 个 Parquet worker；
- 坏 episode 自动跳过并记录；
- 每 30 分钟由 watchdog 检查一次；
- SSH 断开后任务仍继续；
- 进程意外退出时从隐藏断点目录恢复；
- 只有通过最终校验后才发布正式输出目录。

`--task` 必须描述这批数据中的真实任务。错误的任务文本会直接污染训练语言标签，不要机械复用示例。

## 2. 查看进度

随时运行：

```bash
cd /mnt/sunbing/projects/DataProcess
./scripts/lerobot_v21_status.sh \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21
```

实时查看转换日志：

```bash
tail -f /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21_conversion.log
```

查看 watchdog：

```bash
tail -f /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21_watchdog.log
```

相关文件都放在输出目录的同级目录，命名规则如下：

| 文件 | 用途 |
| --- | --- |
| `<输出名>_conversion.log` | 主转换进度 |
| `<输出名>_launcher.log` | Python traceback 和启动输出 |
| `<输出名>_conversion.pid` | 当前转换进程 PID |
| `<输出名>_watchdog.log` | 巡检、断点重启和完成记录 |
| `<输出名>_watchdog.pid` | watchdog PID |
| `.<输出名>.inprogress/` | 可恢复的隐藏断点目录 |

## 3. 怎样才算完成

不要仅凭 GPU 空闲或 Python 进程消失判断完成。以下条件应同时成立：

1. 正式输出目录存在；
2. 正式目录内有 `_SUCCESS`；
3. `meta/validation.json` 中 `status` 为 `pass`；
4. 状态脚本显示 `stage: complete`；
5. 隐藏的 `.inprogress` 目录已经被原子重命名，不再存在。

完成时状态脚本还会显示 episode、frame、video 和跳过数量。

## 4. 坏 episode 的处理

默认启用 `--skip-invalid`。以下情况会跳过整个源 episode：

- 必需文件缺失；
- manifest 未完成；
- 状态或动作帧不足；
- 相机 `rgb.raw` 小于索引声明的字节数；
- 不是预期的 38 维关节布局；
- 时间轴或关节名称不一致。

跳过不是静默发生的：

- 主日志会输出 `skipping invalid source episode ...`；
- 最终数据集中写入 `meta/skipped_episodes.json`；
- `meta/info.json` 记录源总数和跳过数；
- raw 源目录不会被修改或删除。

如果要求“任何坏 episode 都必须让整批失败”，增加 `--strict`：

```bash
./scripts/start_lerobot_v21_conversion.sh \
  --source /mnt/pangyunyi/figure/raw/20260801 \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21 \
  --task "真实任务描述" \
  --strict
```

## 5. 断点续传和意外中断

转换始终写入：

```text
<输出父目录>/.<输出名>.inprogress/
```

Parquet 和 MP4 都会在复用前检查结构、编码和帧数。进程重启后，已经验证通过的文件不会重新生成。

恢复有两种方式：

- 等待 watchdog 在下一个周期自动重启；
- 重新执行完全相同的一键启动命令，立即恢复。

不要手工改名或删除 `.inprogress`，否则会失去断点。

如果正式输出目录已经包含 `_SUCCESS`，重复执行启动命令只会显示完成状态，不会再次转换。

## 6. 常用参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--fps` | `20` | 输出采样频率 |
| `--gpus` | 所有检测到的 GPU | 例如 `--gpus 0,1,2,3` |
| `--parquet-workers` | `8` | 状态/动作处理并发数 |
| `--video-quality` | `20` | NVENC 质量参数，通常不需要修改 |
| `--watchdog-minutes` | `30` | 进程巡检和自动恢复周期 |
| `--skip-invalid` | 开启 | 跳过并记录坏 episode |
| `--strict` | 关闭 | 遇到一个坏 episode 就终止 |
| `--python` | 自动检测 | 显式指定 Python 解释器 |
| `--dry-run` | 关闭 | 只检查，不启动 |

如果服务器上有人训练，建议减少资源占用：

```bash
./scripts/start_lerobot_v21_conversion.sh \
  --source /mnt/pangyunyi/figure/raw/20260801 \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21 \
  --task "真实任务描述" \
  --gpus 6,7 \
  --parquet-workers 4 \
  --watchdog-minutes 30
```

共享 `/mnt` 是 NFS。即使 CPU/GPU 很空，大量 raw 读取仍可能与训练的数据加载和 checkpoint 保存竞争。

## 7. 转换规则

当前转换器面向 Tianji 双臂 + xHand 的 38 维数据：

- `observation.state`：38 维 `float32`；
- `action`：38 维 `float32`；
- 状态：按目标时间线做线性插值；
- 动作：因果零阶保持，不使用未来动作；
- 三路视频：Head、Left wrist、Right wrist；
- raw BGR24 视频按状态起点对齐，再采样到目标 FPS；
- 相机晚开始时复制首帧补齐，早结束时复制末帧直到精确目标帧数；
- 输出 H.264、`yuv420p`，由 `h264_nvenc` 编码；
- 相机键映射为 `ego_view`、`left_wrist`、`right_wrist`；
- episode 输出索引连续，源 ID 保留在 `raw_episode_id` 中。

## 8. 输出目录结构

```text
DATASET_ROOT/
├── _SUCCESS
├── data/chunk-000/
│   └── episode_000000.parquet
├── videos/chunk-000/
│   ├── observation.images.ego_view/episode_000000.mp4
│   ├── observation.images.left_wrist/episode_000000.mp4
│   └── observation.images.right_wrist/episode_000000.mp4
├── meta/
│   ├── info.json
│   ├── modality.json
│   ├── episodes.jsonl
│   ├── tasks.jsonl
│   ├── stats.json
│   ├── conversion.json
│   ├── skipped_episodes.json
│   └── validation.json
└── training/
    ├── figure_38d_config.py
    └── README.md
```

最终校验覆盖：

- 源 episode 结构；
- Parquet schema、行数和有限数值；
- 每个 MP4 的 H.264/`yuv420p` 属性；
- 所有 MP4 的实际解码帧数；
- 元数据中的 episode/frame/video 总数；
- LeRobot `v2.1` 标记。

## 9. 用于 GR00T 训练

输出自带 `training/figure_38d_config.py`。训练时使用 `NEW_EMBODIMENT`，不要冒用 Unitree G1 配置：

```bash
cd /path/to/Isaac-GR00T

uv run python gr00t/experiment/launch_finetune.py \
  --base-model-path nvidia/GR00T-N1.7-3B \
  --dataset-path /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21 \
  --embodiment-tag NEW_EMBODIMENT \
  --modality-config-path /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21/training/figure_38d_config.py \
  --num-gpus 8 \
  --output-dir /mnt/sunbing/outputs/groot-20260801 \
  --global-batch-size 32 \
  --dataloader-num-workers 8
```

正式训练前还应按所用 GR00T 版本检查命令行参数。参考：

- [GR00T Whole-Body Control VLA Workflow](https://nvlabs.github.io/GR00T-WholeBodyControl/tutorials/vla_workflow.html)
- [Isaac-GR00T Data Preparation](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/data_preparation.md)
- [Fine-tune a New Embodiment](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/finetune_new_embodiment.md)

## 10. 常见错误

### 输出目录已存在但没有 `_SUCCESS`

启动器会拒绝覆盖，防止误删已有数据。先检查目录内容；确认是废弃结果后，人工改名备份，再重新执行命令。

### `no Python with numpy and pyarrow`

```bash
cd /mnt/sunbing/projects/DataProcess
./setup.sh
```

也可传入已有环境：

```bash
--python /absolute/path/to/python
```

### `FFmpeg does not provide h264_nvenc`

当前批处理依赖 NVIDIA 编码器。检查 `ffmpeg -encoders | grep h264_nvenc`，或联系管理员修复 FFmpeg/CUDA 环境。

### 进度长时间不变化但进程仍在

先运行状态脚本。FFmpeg 处于 `D` 状态通常表示共享 NFS 正在等待 I/O，不要立即删除断点目录。

### 手工停止任务

先从状态脚本读取 PID，再对正确的转换进程发送 `TERM`。watchdog 仍在时会自动重启；如果确实要取消任务，还需停止对应 watchdog。不要使用模糊的 `pkill python`。

## 11. 代码入口

| 文件 | 作用 |
| --- | --- |
| `scripts/start_lerobot_v21_conversion.sh` | 一键预检、启动和恢复 |
| `scripts/batch_convert_lerobot_v21.py` | 实际转换、统计和全量校验 |
| `scripts/monitor_lerobot_conversion.sh` | 参数化 watchdog |
| `scripts/lerobot_v21_status.sh` | 统一查看状态和最终结果 |
