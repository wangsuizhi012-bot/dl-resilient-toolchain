# 可视化面板（VIZ）

> 对应项目硬规则（2026-09-21）：**长任务不许黑箱**。
> 本文件讲怎么用、怎么接进你已有的 `taskviz-panel`、以及它现在能看什么。

---

## 一句话

```bash
python E:/AI/_scripts/dl/dl.py url <url> -o <path> --panel
```

然后浏览器打开 **http://127.0.0.1:8790**。

---

## 为什么需要它

之前的下载只有一行终端进度条。对三个场景来说这是**不够**的：

| 场景 | 只有终端进度条时你看不到 |
|---|---|
| 20GB 权重 | 还剩多少时间、当前速度是不是在掉 |
| 镜像挂了换源 | **为什么换**、换了哪个、值不值得等 |
| 断点续传 | 从哪里续的、这次是重下了还是接着下的 |

面板把这些**全部显式化**，这正是"不黑盒"的含义。

---

## 能看到什么

```
dl — download progress                          updated 11:27:06 · auto-refresh 1s

Qwen2-0.5B / model.safetensors
█████████████████████████████████████████░░░░░░░░░░░░░░░░░
DONE      TOTAL     SPEED      ETA   SOURCE
829.2MB   942.3MB   28.4MB/s   0:03  hf-mirror

llama-3.1-8b / weights
█░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
DONE      TOTAL     SPEED      ETA   SOURCE
10.0MB    14.9GB    6.1MB/s    43:29 modelscope
│ resuming from 512.0MB                          ← 断点续传
│ tls verify failed (needs proxy route) -> switch  ← 换源原因
│ switching to modelscope                        ← 换到哪
```

**三类信息分层**：

- **进度区**（每次 tick）：进度条 / 已下载 / 总量 / 速度 / ETA / 当前源
- **事件日志**（里程碑）：
  - `attempt` — 第几次尝试、在哪个源
  - `resume` — 从多少字节续传
  - `note` — 失败原因 + 路由判定（`RETRY_SAME` / `RETRY_NEXT` / `GIVE_UP`）
  - `source` — 换源
  - `done` / `fail`
- **不做的事**：不把每个 progress tick 写进日志（会淹没关键行）

---

## 三个参数

| 参数 | 作用 | 何时用 |
|---|---|---|
| `--panel` | 起内置面板（默认 `127.0.0.1:8790`） | 默认选择 |
| `--panel-port 9000` | 换端口 | 8790 被占 |
| `--panel-url http://127.0.0.1:8125/event` | 喂给**已有**面板 | 想让下载进度出现在 ComfyUI 那块面板上 |

支持的命令：`dl.url`、`dl.pypi`。

```bash
# 换端口
dl url <url> -o <path> --panel --panel-port 9123

# 喂给 taskviz-panel（常驻）
dl url <url> -o <path> --panel-url http://127.0.0.1:8125/event
```

---

## 重要限制（先知道再用）

### 1. 面板是**进程内**的后台线程

`--panel` 起的服务随下载进程一起结束。**下载完就没了**，想回看就来不及。

需要常驻面板就用 `--panel-url` 喂给一个长期运行的面板。

### 2. `dl.hf` **没有**接面板

`dl.hf` 走 `huggingface_hub`，它自带 tqdm 进度条（终端可见），但**没接事件总线**。

要可视化 HF 权重，有两条路：

```bash
# 路线 1（推荐）：用 dl.url 下单个权重文件
dl route Qwen/Qwen2-0.5B          # 先确认走 hf-mirror
dl url "https://hf-mirror.com/Qwen/Qwen2-0.5B/resolve/main/model.safetensors" \
   -o E:/models/qwen2-0.5b.safetensors --panel
```

先 `dl route` 拿到端点，再拼 `resolve` 链接——这正是本工具链路由可解释的用处。

### 3. 只监听 127.0.0.1

不对外网开放，局域网也访问不到。这是刻意的（避免下载进度暴露主机信息）。

---

## 作为库用（自定义面板）

`EventBus` 的 sink 是可组合的，且**fail-soft**——面板挂了不会影响下载。

```python
import sys; sys.path.insert(0, r"E:\AI\_scripts\dl")
from dlm.viz import EventBus
from dlm.core import Downloader

bus = EventBus("my-job")

# 1) 自己收事件
def on_event(evt):
    print(evt["type"], evt.get("message") or evt.get("speed_h"))
bus.add_callback(on_event)

# 2) 或喂给 HTTP 面板
bus.add_panel("http://127.0.0.1:8125/event")

# 3) 或两个都要（终端 + 面板）
bus.add_printer(quiet=False)

dl = Downloader(bus=bus)
res = dl.fetch([("hf-mirror", url)], target)
```

### 事件契约（`type` 字段，稳定）

| type | 关键字段 | 时机 |
|---|---|---|
| `attempt` | `n`, `source`, `message` | 每次尝试 |
| `source` | `source`, `message` | 换镜像 |
| `resume` | `from_bytes`, `from_h` | 检测到分片 |
| `start` | `total`, `total_h` | 开始传输 |
| `progress` | `done`, `total`, `speed_h`, `eta_h`, `pct` | 流动中（4Hz 节流） |
| `note` | `message` | 失败原因 + 路由判定 |
| `done` | `done_h`, `elapsed`, `source` | 成功 |
| `fail` | `message` | 致命失败 |

所有事件都含 `job` 与 `t`（相对开始秒数）。**只用 `type` 分支，
不要匹配 `message` 文本。**

---

## 嵌进 taskviz-panel

`taskviz-panel`（`http://127.0.0.1:8125`）目前的下载监控是
**轮询 BITS + 读文件大小**，看不到 dl 的换源/续传过程。

要合并，两种做法：

**A. 只取进度（零改动）**
```bash
dl url <url> -o <path> --panel --panel-port 8790
# 另开 taskviz-panel 看 ComfyUI，下载看 8790
```

**B. 加一个 `/event` 端点**（推荐，需要改 `taskviz-panel/server.py`）
在它的 `do_POST` 里加一个分支，把事件写进 `STATE['downloads']`，
然后 `dl url ... --panel-url http://127.0.0.1:8125/event`。
面板本身已有下载区，能直接复用渲染。

> 我**没有**改 `taskviz-panel`——它不在本仓库，且改了会影响你现有的
> ComfyUI 流程。要合并时告诉我，我按 B 方案加。

---

## 依赖

**零新增依赖**。面板是单个 HTML 字符串 + `http.server`，
用的是 Python 标准库。任何能跑 `dl.py` 的解释器都能起。

实测：`versions/3.13.12`（无 huggingface_hub）也能起面板下 pypi 包。

---

## 实测记录（2026-10-05）

| 项 | 结果 |
|---|---|
| 942MB 真实下载 | 面板收到 **121 个事件**（2 attempt / 2 start / 114 progress / 1 note / 1 resume / 1 done） |
| 浏览器侧观察 | 31% → 99.4%，速度 13.5 → 39.0 MB/s，ETA 同步递减 |
| 面板 HTML | 3062 字节，含进度条，`Accept-Ranges: bytes` 正常 |
| 回归 | 7/7 通过（caps/route/probe/pypi/url/panel/json） |
| 截图 | 已确认视觉效果符合预期 |
