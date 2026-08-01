# DataProcess

面向机器人遥操作原始数据的网页审阅与转换工具。它可以扫描 raw episode、识别不完整采集、裁剪静止的头尾、同步回放三路相机与关节轨迹，并导出可供 NVIDIA Isaac GR00T 训练的 LeRobot v2.1 数据集。

## 新用户从这里开始

- 网页审阅、裁剪和人工挑选：[DATAPROCESS_USER_GUIDE.md](DATAPROCESS_USER_GUIDE.md)
- 整日数据一键批量转换、GPU 加速、断点续传和 watchdog：[LEROBOT_V21_CONVERSION_GUIDE.md](LEROBOT_V21_CONVERSION_GUIDE.md)

最常用的批处理入口：

```bash
./scripts/start_lerobot_v21_conversion.sh \
  --source /绝对路径/raw/日期 \
  --output /绝对路径/日期_lerobot_v21 \
  --task "真实任务描述"
```

## 功能

- 自动检查 `manifest.json`、状态/动作 JSONL、三路相机索引和 `.raw` 文件完整性。
- 不完整 episode 自动标为失败；人工标记的失败 episode 从导出集中排除。
- 原始数据只读。审阅结果保存在本项目的 `runtime/reviews/`，不会删除 raw 文件。
- Head、Left wrist、Right wrist 三路视频同步回放，支持逐帧、倍速和任意定位。
- 同步显示 38 维 `observation.state` / `action` 关节轨迹。
- 手动双端裁剪，或依据关节运动强度自动裁剪并保留 0.6 秒缓冲。
- 状态、动作、视频统一重采样到指定 FPS，导出后校验 Parquet 行数和视频文件。
- 异步转换与进度显示；覆盖输出时先把旧目录重命名为带时间戳的备份。

## 启动

```bash
cd /mnt/sunbing/projects/DataProcess
./setup.sh
./start.sh
```

默认监听 `0.0.0.0:8088`。在自己的电脑上建立 SSH 隧道：

```bash
ssh -L 8088:127.0.0.1:8088 4090
```

然后打开 `http://127.0.0.1:8088`。

可以通过环境变量覆盖默认值：

```bash
DATAPROCESS_PORT=8090 \
DATAPROCESS_RAW_ROOT=/mnt/pangyunyi/figure/raw/20260730 \
./start.sh
```

查看日志或停止后台服务：

```bash
tail -f runtime/server.log
./stop.sh
```

网页审阅和 raw 视频预览只依赖 Python 标准库与系统 FFmpeg。写 Parquet 需要 `pyarrow`，由 `setup.sh` 安装。如果服务器没有 `python3-venv`，脚本会自动改用项目内 `.deps/`，不需要 sudo。

## 默认导出结构

默认使用 GR00T 官方建议的 episode/chunk 布局：

```text
dataset/
├── data/chunk-000/episode_000000.parquet
├── videos/chunk-000/
│   ├── observation.images.ego_view/episode_000000.mp4
│   ├── observation.images.left_wrist/episode_000000.mp4
│   └── observation.images.right_wrist/episode_000000.mp4
└── meta/
    ├── info.json
    ├── modality.json
    ├── episodes.jsonl
    ├── tasks.jsonl
    └── conversion.json
```

网页也提供“平铺兼容”布局，会生成 `data/train-00000.parquet` 和 `videos/<video_key>/episode_*.mp4`，用于兼容已有的内部流水线。训练 GR00T 时优先使用默认的 chunked 布局。

Parquet 每帧包含：

- `observation.state`: 38 维 `float32`
- `action`: 38 维 `float32`
- `timestamp`, `frame_index`, `episode_index`, `index`, `task_index`
- `annotation.human.task_description`
- `next.reward`, `next.done`

格式依据：[NVIDIA Isaac-GR00T Data Preparation Guide](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/data_preparation.md) 与 [Hugging Face LeRobot v2.1/v3 对照](https://huggingface.co/docs/lerobot/porting_datasets_v3)。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 设计说明

- 失败 episode 的“删除”指从导出数据集排除，不物理删除 raw 源文件。
- 首次回放一个 episode 时，服务端用 FFmpeg 将 BGR24 `.raw` 缓存成 H.264 MP4；后续打开直接读取缓存。
- 自动裁剪是建议区间，不替代人工复核。算法根据相邻 38 维关节状态的平均变化量寻找活动窗口。
- 导出先写入同级隐藏临时目录，完整校验后再原子重命名为目标目录，避免产生半成品训练集。
