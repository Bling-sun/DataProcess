# DataProcess 使用说明

DataProcess 用于检查机器人遥操作原始数据、同步回放三路相机、审阅并裁剪 episode，以及导出 GR00T 可用的 LeRobot v2.1 数据集。

项目提供两种使用方式：

| 场景 | 使用入口 |
| --- | --- |
| 逐个查看视频、裁剪有效区间、标记失败数据 | 网页工作台 |
| 整日数据全量转换、GPU 编码、断点续传 | `scripts/start_lerobot_v21_conversion.sh` |

> DataProcess 不会修改或删除 raw 源数据。网页审阅结果和预览缓存写在项目的 `runtime/` 中。

## 1. 环境要求

网页工作台需要：

- Linux；
- Python 3；
- 可读取的 raw 数据目录；
- FFmpeg（执行 `setup.sh` 后会在项目环境中提供）；
- NumPy、PyArrow 和 imageio-ffmpeg（由 `setup.sh` 安装）。

GPU 全量转换还需要：

- NVIDIA GPU 和 `nvidia-smi`；
- 系统 FFmpeg、FFprobe；
- FFmpeg 支持 `h264_nvenc`。

首次使用，在项目目录执行：

```bash
cd ~/Documents/DataProcess
./setup.sh
```

脚本优先创建 `.venv/`。如果系统没有 `python3-venv`，会改为把依赖安装到项目内的 `.deps/`，不需要 sudo。

## 2. 启动网页工作台

### 后台启动

```bash
cd ~/Documents/DataProcess
./start.sh
```

默认监听 `0.0.0.0:8088`，浏览器访问：

```text
http://服务器IP:8088
```

如果服务器不能直接访问，可在自己的电脑上建立 SSH 隧道：

```bash
ssh -N -L 8088:127.0.0.1:8088 用户名@服务器地址
```

然后打开 `http://127.0.0.1:8088`。

### 前台运行

调试时可直接执行：

```bash
./run.sh
```

按 `Ctrl+C` 停止。

### 修改默认目录、地址或端口

```bash
DATAPROCESS_RAW_ROOT=/绝对路径/raw/日期 \
DATAPROCESS_HOST=0.0.0.0 \
DATAPROCESS_PORT=8090 \
./start.sh
```

也可以把这些变量写入项目根目录的 `.env`：

```dotenv
DATAPROCESS_RAW_ROOT=/绝对路径/raw/日期
DATAPROCESS_HOST=0.0.0.0
DATAPROCESS_PORT=8088
DATAPROCESS_TMP_DIR=/绝对路径/临时目录
```

修改配置后需要先停止再启动服务。

### 服务管理

```bash
# 查看健康状态
curl http://127.0.0.1:8088/api/health

# 查看日志
tail -f runtime/server.log

# 停止后台服务
./stop.sh

# 重启
./stop.sh && ./start.sh
```

## 3. raw 数据目录要求

网页只扫描名称符合 `episode_XXXXXX` 的一级子目录，例如：

```text
RAW_ROOT/
└── episode_000000/
    ├── manifest.json
    ├── observation_state_frame.jsonl
    ├── applied_action_frame.jsonl
    ├── events.jsonl
    ├── head/
    │   ├── frames.jsonl
    │   └── rgb.raw
    ├── left_wrist/
    │   ├── frames.jsonl
    │   └── rgb.raw
    └── right_wrist/
        ├── frames.jsonl
        └── rgb.raw
```

扫描时会检查必需文件、manifest 状态、状态和动作帧、三路相机索引、raw 字节数以及可同步时间区间。缺文件、数据不完整、manifest 未完成或同步区间过短的 episode 会显示为“失败”，但源文件不会被删除。

## 4. 网页审阅流程

### 4.1 扫描数据

在页面左上角输入 raw 日期目录的绝对路径，按 Enter 或点击刷新按钮。

左侧状态含义：

| 状态 | 含义 |
| --- | --- |
| 未处理 | 数据检查通过，但尚未保存审阅结果 |
| 待导出 | 已保存或自动裁剪，且未标记失败 |
| 已导出 | 已成功同步到至少一个输出数据集 |
| 失败 | 源数据检查失败，或被人工排除 |

### 4.2 回放 episode

点击左侧 episode 后，页面会同步显示：

- Head 主视角；
- Left wrist 和 Right wrist；
- `observation.state` 与 `action` 关节轨迹；
- 状态帧、动作帧、维度和 manifest ID。

播放器支持播放/暂停、前后逐帧、拖动定位和 0.25×–2× 倍速。首次打开某个 episode 时，服务端会从 BGR24 `rgb.raw` 生成 H.264 预览，可能需要等待；生成后会复用 `runtime/previews/` 中的缓存，并预加载后续 episode。

### 4.3 裁剪和保存

设置有效区间有三种方式：

1. 拖动起点和终点；
2. 输入起止秒数，或把当前帧设为起点/终点；
3. 点击“自动裁剪”，根据关节运动范围生成建议区间，并在两端保留约 0.6 秒。

保留区间不能短于 0.25 秒。自动裁剪只是建议，保存前应回放确认任务动作完整。

- 正常数据：填写可选备注，点击“保存审阅”；
- 不用于训练的数据：点击“标记为失败”；
- 标错的数据：再次点击可恢复为正常。

“自动裁剪”和“标记为失败”都会立即保存当前处理状态。只观看视频、不执行这些操作，也不点击“保存审阅”的 episode 不会进入网页导出集。

## 5. 从网页导出 LeRobot v2.1

点击右上角“导出 GR00T 数据集”，填写：

- 输出目录：必须是安全的绝对路径，且不能等于 raw 目录或位于系统回收站；
- 任务描述：应准确描述这批数据中的真实动作；
- FPS：默认和通常推荐为 20 Hz；
- 目录布局：GR00T 训练优先选择 `chunk-000`；
- 视频机位：至少选择一路；
- 保留备份：同步更新已有输出时，是否保留旧的完整版本。

网页会同步当前 raw 目录中所有“已处理且未失败”的 episode，包括以前导出过但后来被重新裁剪的 episode：

- 输出内容完全未变化时直接跳过；
- 新 episode 会按原顺序追加；
- 审阅区间或源文件变化时会重建对应数据集，并保持已有 episode 的索引顺序；
- 如果目标目录不是本工具为当前 raw 数据集生成的历史输出，必须勾选保留/覆盖选项才允许替换；
- 写入和校验成功后才会替换正式输出目录。

导出在独立子进程中运行，即使 PyArrow 或原生库异常退出，网页服务仍会继续运行。导出期间不要停止网页服务。

默认 chunked 输出结构：

```text
DATASET_ROOT/
├── data/chunk-000/
│   └── episode_000000.parquet
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

“平铺兼容”布局会把 Parquet 写到 `data/train-00000.parquet`，视频写到 `videos/<video_key>/`。

## 6. GPU 全量批量转换

如果整日数据不需要逐个人工裁剪，可直接使用批处理入口。它不读取网页审阅结果，而是扫描 raw 根目录中的全部 episode。

先做预检，不启动任务：

```bash
cd ~/Documents/DataProcess

./scripts/start_lerobot_v21_conversion.sh \
  --source /绝对路径/raw/日期 \
  --output /绝对路径/日期_lerobot_v21 \
  --task "真实任务描述" \
  --dry-run
```

预检通过后，去掉 `--dry-run` 后台启动：

```bash
./scripts/start_lerobot_v21_conversion.sh \
  --source /绝对路径/raw/日期 \
  --output /绝对路径/日期_lerobot_v21 \
  --task "真实任务描述"
```

默认使用全部检测到的 GPU、20 Hz、8 个 Parquet worker，并跳过和记录结构无效的 episode。任务写入同级隐藏目录 `.<输出名>.inprogress/`，进程中断后可从已校验文件恢复；watchdog 默认每 30 分钟检查并重启异常退出的转换进程。

常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--fps` | `20` | 输出采样频率 |
| `--gpus` | 所有 GPU | 例如 `--gpus 0,1` |
| `--parquet-workers` | `8` | 状态/动作处理并发数 |
| `--video-quality` | `20` | NVENC 质量参数 |
| `--watchdog-minutes` | `30` | watchdog 检查周期 |
| `--skip-invalid` | 开启 | 跳过并记录坏 episode |
| `--strict` | 关闭 | 遇到坏 episode 立即终止 |
| `--python` | 自动检测 | 指定包含 NumPy、PyArrow 的 Python |
| `--dry-run` | 关闭 | 只执行环境和参数检查 |

查看状态：

```bash
./scripts/lerobot_v21_status.sh \
  --output /绝对路径/日期_lerobot_v21
```

也可以查看输出目录同级的日志：

```bash
tail -f /绝对路径/日期_lerobot_v21_conversion.log
tail -f /绝对路径/日期_lerobot_v21_watchdog.log
```

只有以下条件全部满足才算完成：

- 正式输出目录已生成；
- 目录中存在 `_SUCCESS`；
- `meta/validation.json` 的 `status` 为 `pass`；
- 状态脚本显示 `stage: complete`。

无效 episode 会记录在 `meta/skipped_episodes.json`，不会修改 raw 数据。若需要任何一个坏 episode 都使整批失败，请添加 `--strict`。

批处理转换器面向 38 维 Tianji 双臂 + xHand 数据：状态使用线性插值，动作使用因果零阶保持，三路视频由 NVENC 编码为 H.264。最终会校验 Parquet schema、行数、有限数值、视频编码属性和实际解码帧数。

## 7. 运行数据和备份

```text
runtime/
├── reviews/      # 审阅、裁剪、排除标记和导出历史
├── previews/     # H.264 预览缓存
├── posters/      # 视频封面缓存
├── tmp/          # 临时文件
├── pip-cache/    # pip 缓存
├── server.pid    # 网页服务 PID
└── server.log    # 网页服务日志
```

注意：

- `runtime/reviews/` 是审阅结果的权威记录，建议定期备份；
- 删除 `runtime/previews/` 或 `runtime/posters/` 不影响 raw 数据，但下次打开会重新生成；
- 浏览器 localStorage 只保存目录、筛选、选择和页面索引缓存；
- 不要在服务运行时手工编辑 review JSON；
- `./stop.sh` 只管理网页服务，不会停止由批处理脚本独立启动的转换任务和 watchdog。

## 8. 常见问题

### 页面显示“服务不可用”

```bash
cd ~/Documents/DataProcess
cat runtime/server.pid
tail -n 100 runtime/server.log
curl http://127.0.0.1:8088/api/health
```

远程使用时还要确认 SSH 隧道没有断开。

### 首次打开视频很慢

首次访问需要读取较大的 raw 文件并生成 MP4。共享存储繁忙时主要瓶颈可能是 I/O；缓存完成后再次打开会更快。

### episode 自动显示失败

点开查看页面中的原因和警告。常见原因包括必需文件缺失、manifest 未完成、状态/动作帧不足、相机 raw 数据不完整或时间轴不能同步。

### 网页提示“没有可导出的 episode”

至少需要一个 episode 同时满足：源检查通过、已保存/自动裁剪、未被标记失败。

### 输出目录已存在

同一 raw 数据集的历史输出可以安全同步。其他非空目录默认拒绝覆盖；确认目标无误后勾选保留旧版本备份再导出。

### 批处理长时间没有进度

先运行状态脚本并查看 conversion/watchdog 日志。进程处于 `D` 状态通常表示等待共享存储 I/O，不要直接删除 `.inprogress` 目录。

### 批处理提示没有 `h264_nvenc`

```bash
ffmpeg -hide_banner -encoders 2>/dev/null | grep h264_nvenc
nvidia-smi
```

网页预览可以使用项目提供的 CPU FFmpeg；GPU 全量转换必须使用带 NVENC 支持的系统 FFmpeg。

## 9. 测试

```bash
cd ~/Documents/DataProcess
PATH="$PWD/.venv/bin:$PATH" \
./.venv/bin/python -m unittest discover -s tests -v
```

如果没有使用 `.venv`：

```bash
python3 -m unittest discover -s tests -v
```
