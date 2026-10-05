---
name: dl-resilient-download
display_name: "弹性下载"
description: 下载文件、PyPI 包、Hugging Face 模型或任意 URL，带镜像自动切换、断点续传、实时进度与代理流量控制。适用于任何「下载卡住、下载很慢、下载中断、超时、连接失败」的情况，或用户要求「下载模型 / 下载数据集 / 下载权重 / 下个包 / 下个文件 / 抓取文件 / 换个源下载 / 用代理下载 / 限流量下载」时。触发词：下载、下文件、下模型、下包、下载包、pip 下载、抓取文件、下载数据集、下载权重、下载卡住、下载失败、下载慢、下载中断、超时、连接失败、镜像、换源、换个源下载、断点续传、代理下载、用代理下载、限流量下载、hf-mirror、huggingface 下载、大文件下载、model download、download failed、download stuck。
description_zh: "带镜像自动切换、断点续传、实时进度和代理流量控制的下载工具。用于下载卡住、缓慢、超时，或用户要抓取模型/数据集/包/大文件时。触发词：下载、下模型、下包、换源、断点续传、代理下载。"
description_en: "Download with mirror failover, resume, live progress and proxy budget control."
display_name_en: "Resilient Download"
version: 1.1.1
agent_created: true
allowed-tools: Bash, Read
metadata:
  author: senior-developer
  entrypoint: "python E:/AI/_scripts/dl/dl.py"
  homepage: "https://github.com/wangsuizhi012-bot/dl-resilient-toolchain"
---

# 弹性下载工具链（dl）

**引擎位置**：`E:\AI\_scripts\dl\`
**不要**手写下载脚本、手动 `curl`/`wget` 大文件、或对同一个源反复重试——先按本 Skill 走。

---

## 0. 先自检（30 秒，只在报错或换环境时跑）

```bash
# 确认引擎在位 + 工具清单可读；缺依赖会直接告诉你该用哪个解释器
python E:/AI/_scripts/dl/dl.py caps
```

预期输出形如 `dl 1.1.0  schema=1.0.0  tools=7  errors=14`。

**若提示文件不存在**：引擎不在 `E:/AI/_scripts/dl/`，
从 <https://github.com/wangsuizhi012-bot/dl-resilient-toolchain> 克隆，
或改用本 Skill 里出现的其他路径（不要假设路径唯一）。

---

## 第一步：先问路由，别猜（不下载任何东西）

```bash
python E:/AI/_scripts/dl/dl.py route <目标>
```

它会告诉你走哪条路、为什么、以及有没有警告。**看到 `flow` 和 `reason`
再决定是否要传 `-p`。**

---

## 按目标类型选命令

| 目标长这样 | 用哪条 |
|---|---|
| `Qwen/Qwen2-0.5B`、`org/model` | `dl hf` |
| PyPI 包（要具体文件名） | `dl pypi` |
| 任意 http(s) 链接 | `dl url` |
| 想先看镜像状态 | `dl probe` |
| 不确定该走哪条 | `dl route` |

---

## 命令速查

```bash
# HF 仓库（默认直连 hf-mirror，不耗代理流量）
python E:/AI/_scripts/dl/dl.py hf <org/name> -d <目标目录> --allow "*.safetensors"

# PyPI 单个分发文件
python E:/AI/_scripts/dl/dl.py pypi <pkg> <文件名> -d <目录>

# 任意 URL（--mirror 是同一对象的备用地址）
python E:/AI/_scripts/dl/dl.py url <url> -o <文件路径> --mirror <备用url>

# 镜像健康（直连探测）
python E:/AI/_scripts/dl/dl.py probe --fresh

# 代理额度有限时：设上限，超出自动改走直连
python E:/AI/_scripts/dl/dl.py hf <org/name> -d <目录> -p --proxy-budget 512MB
```

**任何命令加 `--json`** → 拿机器可读信封（含 `error.code` 与 `metrics`）。

---

## 这台机器上的三条硬事实（别重新踩）

1. **`huggingface.co` / `github.com` 直连被 TLS 拦截**，走代理才可能通。
   → 优先级永远是 `hf-mirror`（国内镜像），别一上来就用官方源。
2. **`probe` 默认直连探测**。结果里带 `direct` / `(direct)` 标记。
   若此时有代理在跑，**加了 `--proxy` 才会显示官方源可达**——
   那回答的是另一个问题（"能否靠代理下"），别混为一谈。
3. **清华源对浏览器 UA 返回 403**，只认 `pip/xx.x`。工具链已钉死 UA，
   所以**不要自己另写脚本加浏览器 UA 的请求头**。

---

## 必须遵守的调用纪律

| ✅ 正确 | ❌ 错误（会出问题） |
|---|---|
| 判断成败看 **JSON 的 `error.code`** 或进程退出码 | 匹配 `error.message` 里的文字（会变） |
| 直接读完整 JSON | 加 `\| head -N` / `\| tail -N` **截断 stdout**（会把 JSON 弄坏） |
| 失败先看 `retryable`，`true` 才值得重试 | 对 `E_NOT_FOUND` / `E_USAGE` 反复重试 |
| 大文件**先下 `config.json` 之类小文件**验证链路 | 上来就下 20GB 权重，失败才发现链路不通 |
| 默认**不加 `-p`**（代理是手动开关） | 端口没确认就传 `-p`（虽然有降级，但语义会变） |
| 目标目录写明确 | 让默认落到当前工作目录 |

---

## 选哪个解释器（重要，先看这条）

`dl hf` 需要 `huggingface_hub`，**只有这个 venv 装了**：

```bash
C:/Users/wsz945/.workbuddy/binaries/python/envs/default/Scripts/python.exe
```

| 命令 | 需要 hf_hub | 建议解释器 |
|---|---|---|
| `hf` | ✅ 需要 | **必须用上面那个 venv** |
| `pypi` / `url` / `probe` / `route` / `caps` / `health` | ❌ 不需要 | 任意 python |

**踩坑提示**：直接敲 `python`（3.13.12）跑 `dl hf` 会报
`huggingface_hub is not installed`——工具链会直接告诉你该用哪个解释器，
别去手动 pip install。

---

## 排障顺序（卡住 / 失败时按这个走）

```bash
dl route <目标>        # 1. 该走哪条路？
dl probe --fresh       # 2. 镜像还活着吗？
dl health              # 3. 缓存里的健康数据
```

退出码速查：`0` 成功 · `10` 参数错 · `11` 源全挂 · `12` 找不到 ·
`13` 需 HF token · `15` 代理不通 / TLS 被拦 · `19` 工具 id 错或已废弃

> ⚠️ **CMD / PowerShell 里 `$?` 只能区分 0 和非 0**，拿不到具体错误码。
> 要精确判断必须读 `--json` 的 `error.code`。

---

## 给其他 Agent / 脚本用

能力清单（机器可读，含每个工具的参数与示例）：
`E:\AI\_scripts\dl\capabilities.json`
接口规范：`E:\AI\_scripts\dl\CONTRACT.md`

Python 内嵌：
```python
import sys; sys.path.insert(0, r"E:\AI\_scripts\dl")
from dlm.api import run, exit_code_for
env = run("dl.hf", repo="org/name", dest=r"E:/models/x")
if not env["ok"]:
    code = env["error"]["code"]      # 稳定契约，可分支
```

**API 永不抛异常**，失败也返回信封，不需要 try/except。
CLI 与 API 走同一条执行路径，行为和错误码完全一致。

---

## 局限（别指望它做这些）

- **不会让下载变快**：实测 8 线程分片只比单流快 7%，瓶颈在镜像端带宽。
  IDM / aria2 多连接在这条链路上没有收益。
- **不是常驻服务**，没有守护进程；每次调用是独立进程。
- 不做包管理器的依赖解析（`pip install` 请交给 pip）。
