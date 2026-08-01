# DataProcess 使用手册

DataProcess 是 Figure/Tianji 机器人 raw episode 的网页审阅工具。它用于检查采集完整性、同步预览三路相机、查看 38 维状态/动作曲线、裁剪有效区间、标记失败数据，并把人工审阅后的 episode 导出为 LeRobot v2.1。

## 1. 先选对入口

| 需求 | 推荐入口 |
| --- | --- |
| 逐个看视频、人工裁剪、标记失败 | DataProcess 网页 |
| 已经确认整日数据可直接全量处理 | `scripts/start_lerobot_v21_conversion.sh` |
| 大批量 GPU 加速、断点续传、watchdog、全量解码校验 | 批处理脚本 |

网页导出只包含“已保存审阅、未标记失败、从未导出”的 episode；全量批处理则自动扫描整日数据并跳过/记录结构损坏的 episode。全量转换请同时阅读 [LEROBOT_V21_CONVERSION_GUIDE.md](LEROBOT_V21_CONVERSION_GUIDE.md)。

## 2. 60 秒启动网页

### 2.1 服务器首次安装

```bash
ssh 4090
cd /mnt/sunbing/projects/DataProcess
./setup.sh
```

### 2.2 启动服务

```bash
cd /mnt/sunbing/projects/DataProcess

DATAPROCESS_RAW_ROOT=/mnt/pangyunyi/figure/raw/20260801 \
./start.sh
```

默认端口是 `8088`。看到以下形式的输出即表示后台服务已启动：

```text
DataProcess started (PID ..., port 8088).
```

### 2.3 在自己的电脑建立 SSH 隧道

以下命令在本地电脑运行，不是在 4090 上运行：

```bash
ssh -N -L 8088:127.0.0.1:8088 4090
```

保持该终端不关闭，然后浏览器打开：

```text
http://127.0.0.1:8088
```

页面右上角应显示“服务在线”。

## 3. 日常审阅流程

### 第一步：选择原始数据目录

在左上角“原始数据目录”中输入日期目录，例如：

```text
/mnt/pangyunyi/figure/raw/20260801
```

按 Enter 或点击刷新按钮。扫描会检查：

- `manifest.json`；
- 状态、动作和事件 JSONL；
- Head、Left wrist、Right wrist 三路相机索引；
- BGR24 `rgb.raw` 的实际字节数；
- 状态/动作维度和同步时间区间。

左侧统计含义：

| 状态 | 含义 |
| --- | --- |
| 未处理 | 还没有保存人工审阅结果 |
| 待导出 | 已保存审阅且未标记失败 |
| 已导出 | 已成功写入过某个输出数据集 |
| 失败 | 源数据损坏或人工标记为失败 |

### 第二步：选择 episode

点击左侧 episode。首次打开时服务器会从 `.raw` 生成紧凑 H.264 预览，因此可能需要等待；后续会直接复用缓存。

页面同时显示：

- Head 主视角；
- Left wrist；
- Right wrist；
- 关节状态/动作曲线；
- 状态帧、动作帧、维度和 manifest ID。

播放器支持播放/暂停、逐帧、拖动时间和 0.25×–2× 倍速。三路视频使用各自时间偏移同步。

### 第三步：设置有效区间

有两种方式：

1. 点击“自动裁剪”，系统根据关节运动范围给出建议区间，并保留约 0.6 秒缓冲；
2. 手工拖动有效区间两端，或直接填写起止秒数。

自动裁剪只是建议，必须回放确认物体、双手和动作完整保留。

### 第四步：决定是否保留

- 正常数据：填写可选备注，然后点击“保存审阅”；
- 不应训练的数据：点击“标记为失败”，确认状态后仍需保存；
- 误标失败：再次点击按钮可恢复。

保存审阅是进入“待导出”的必要条件。只播放视频但不保存，不会被网页导出。

### 第五步：继续下一个 episode

页面会预加载后续 5 个可用 episode，并保留少量前序缓存。可用顶部筛选切换“未处理”“待导出”“已导出”“失败”。

## 4. 从网页导出

点击右上角“导出 GR00T 数据集”。

依次填写：

1. 输出目录：必须是安全的绝对路径；
2. 任务描述：必须准确描述数据中的真实动作；
3. FPS：通常选 20 Hz；
4. 布局：训练 GR00T 优先选择 `chunk-000`；
5. 视频机位：通常保留三路；
6. 是否允许覆盖：勾选后，旧输出会先重命名为时间戳备份。

弹窗中的 episode 数量是本次真正会导出的数量。点击“开始转换”后不要关闭服务进程；弹窗会显示异步任务进度和错误。

网页导出成功后，episode 会登记为“已导出”，后续导出不会重复包含。审阅历史中会保留输出目录、输出 episode 索引和导出时间。

> 对数百 GB 整日数据，优先使用批处理脚本。网页导出适合人工挑选和裁剪后的较小批次，不提供独立 watchdog。

## 5. 服务管理

### 查看是否正常

```bash
curl http://127.0.0.1:8088/api/health
```

### 查看日志

```bash
cd /mnt/sunbing/projects/DataProcess
tail -f runtime/server.log
```

### 停止服务

```bash
cd /mnt/sunbing/projects/DataProcess
./stop.sh
```

### 重启并切换默认数据目录

```bash
cd /mnt/sunbing/projects/DataProcess
./stop.sh

DATAPROCESS_RAW_ROOT=/mnt/pangyunyi/figure/raw/20260801 \
./start.sh
```

也可以不重启服务，直接在网页左上角输入新的 raw 路径后重新扫描。

### 修改端口

服务器：

```bash
DATAPROCESS_PORT=8090 \
DATAPROCESS_RAW_ROOT=/mnt/pangyunyi/figure/raw/20260801 \
./start.sh
```

本地隧道：

```bash
ssh -N -L 8090:127.0.0.1:8090 4090
```

浏览器改为 `http://127.0.0.1:8090`。

## 6. 数据和缓存放在哪里

DataProcess 不会修改或删除 raw 源数据。项目运行状态位于：

```text
runtime/
├── reviews/       # 审阅、裁剪、失败标记、导出历史
├── previews/      # 浏览器使用的 H.264 预览缓存
├── posters/       # 视频封面缓存
├── backups/       # 手工/维护过程中生成的审阅备份
├── tmp/           # 项目临时文件
├── server.pid     # 服务 PID
└── server.log     # 服务日志
```

重要提示：

- `runtime/reviews/` 很小但很重要，应定期备份；
- 删除 `runtime/previews/` 或 `runtime/posters/` 不会删除 raw，但下次打开要重新生成；
- 不要在服务运行时修改 review JSON；
- 浏览器 localStorage 只保存页面索引/选择状态，权威审阅结果在服务器 `runtime/reviews/`。

## 7. 网页导出的结构

默认 chunked 输出：

```text
DATASET_ROOT/
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

Flat 布局用于兼容内部旧流水线，会写 `data/train-00000.parquet`。新 GR00T 训练优先选择 chunked。

## 8. 批处理一键入口

整日数据不需要逐个人工审阅时：

```bash
cd /mnt/sunbing/projects/DataProcess

./scripts/start_lerobot_v21_conversion.sh \
  --source /mnt/pangyunyi/figure/raw/20260801 \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21 \
  --task "真实任务描述"
```

查看状态：

```bash
./scripts/lerobot_v21_status.sh \
  --output /mnt/sunbing/data/ExpressSorting/20260801_lerobot_v21
```

完整参数、断点恢复和校验说明见 [LEROBOT_V21_CONVERSION_GUIDE.md](LEROBOT_V21_CONVERSION_GUIDE.md)。

## 9. 常见问题

### 页面显示“服务不可用”

依次检查：

```bash
cd /mnt/sunbing/projects/DataProcess
cat runtime/server.pid
tail -n 100 runtime/server.log
curl http://127.0.0.1:8088/api/health
```

同时确认本地 SSH 隧道仍在运行。

### 首次打开视频很慢

这是 raw BGR24 转 H.264 预览的正常过程。共享 NFS 繁忙时 FFmpeg 可能等待 I/O。生成后会缓存，不需要每次重做。

### 某个 episode 自动显示失败

点开查看错误原因。常见原因是 manifest 未完成、相机 raw 字节数不足、状态/动作帧不足或同步区间过短。网页不会物理删除该源数据。

### 点击导出但显示“没有可导出的 episode”

至少需要一个 episode 满足：

- 已点击“保存审阅”；
- 未标记失败；
- 源结构检查通过；
- 以前没有成功导出过。

### 输出目录已存在

未勾选覆盖时会拒绝；勾选后旧目录会先改名备份。确认备份和新输出无误后，再决定是否删除旧目录。

### `./setup.sh` 安装失败

检查网络、pip 日志和 `runtime/pip-cache/`。服务器没有 `python3-venv` 时，脚本会自动使用项目内 `.deps/`，不需要 sudo。

### 停止网页会不会停止批处理转换

不会。`./stop.sh` 只停止网页服务及其预览子进程；通过 `start_lerobot_v21_conversion.sh` 启动的批任务和 watchdog 使用独立 PID 文件。

## 10. 测试

```bash
cd /mnt/sunbing/projects/DataProcess
python3 -m unittest discover -s tests -v
```

如果使用项目环境：

```bash
./.venv/bin/python -m unittest discover -s tests -v
```
