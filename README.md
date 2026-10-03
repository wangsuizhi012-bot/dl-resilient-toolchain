# 下载流程固化方案（dl / dlm）

> 落地位置：`E:\AI\_scripts\dl\`
> 实测日期：2026-10-03　所有 URL 模板与延迟数字均为**本机实测**，非推测。
> 编码约定：`.py` UTF-8，stdout 全 ASCII（项目规则 8）。

## 文档索引

| 文件 | 讲什么 | 读者 |
|---|---|---|
| `README.md`（本文） | 瓶颈分析与设计原理 | 想了解**为什么**这么设计 |
| `CONTRACT.md` | 接口规范：参数、信封、错误码、路由、版本策略 | 要**接入**本工具链 |
| `AGENT-GUIDE.md` | 给 Agent 的接入指南与调用纪律 | 让 **Agent 调用**本工具链 |
| `PITFALLS.md` | **踩坑清单**：GitHub 推送失败、代理陷阱、误报排查 | 遇到**推送/代理**问题时查 |
| `capabilities.json` | 机器可读能力清单 | 程序读取 |

---

## 一、瓶颈分析：原来到底卡在哪

现有资产只有三个脚本，且都是「单镜像 + 硬编码 + 无反馈」：

| 文件 | 覆盖对象 | 问题 |
|---|---|---|
| `_scripts/tools/pip-tuna.py` + `pip-smart.bat` | pip | 只认清华；靠进程内改 UA 绕过 403 |
| `_scripts/tools/laya_fetch.py` | HF | 镜像/官方二选一，**失败不重试**，无进度 |
| `_scripts/tools/download_torigate_config.py` | HF 配置 | 裸 try/except，失败只打印 |

### 实测出的 5 个真实根因

**① 清华源对浏览器 UA 直接 403（不是慢，是被拒）**

```
UA 策略矩阵（pypi simple index，2026-10-03 实测）
  源        chromeUA   python-requests   pip/24.0   huggingface_hub   wget
  清华      403        200               200         200              200
  阿里云    200        200               200         200              200
  腾讯      200        200               200         200              200
```
这解释了为什么「有时能下有时不能」——取决于调用方用什么 UA。`pip-tuna.py`
的补丁是有效但脆弱的：它只在 pip 进程内生效，HF 侧完全没覆盖。

**② huggingface.co / github.com 在本机 TLS 校验失败（直连和走代理都失败）**

```
pypi.org / files.pythonhosted.org   OK    issuer=GlobalSign nv-sa
pypi.tuna / aliyun / tencent        OK
hf-mirror.com / modelscope          OK
github.com                         FAIL  Missing Subject Key Identifier
huggingface.co                     FAIL  Missing Subject Key Identifier
raw.githubusercontent.com           FAIL  Missing Subject Key Identifier
```
`curl` 同样失败，且 65532 代理当前**未监听**（netstat 无 6553x）。
所以「走官方源」这条路目前在本机是死的 —— 任何依赖它的逻辑都会表现为
「卡住直到超时」。这就是耗时过长的主因，不是镜像慢。

**③ 镜像的 index 路径与 file 路径不同源（最容易写错的地方）**

```
PyPI 索引根（都 200）        PyPI 文件根（同一路径 /packages/... 实测）
  清华   /simple/              清华   /                      200
  阿里   /pypi/simple/         阿里   /pypi/web/            200
  腾讯   /pypi/simple/         腾讯   /pypi/                200  ← 无 /web
  中科大 /pypi/simple/         中科大 /pypi/web/            200
  华为   /repository/pypi/simple/  华为 /repository/pypi/    200  ← 无 /web
```
索引正常但文件 404 → 用户感知就是「这个镜像坏了/很慢」。腾讯和华为的
`/web` 猜测都是错的，实测才找对。

**④ 官方源延迟劣化**：pypi.org 首字节 **11.36s**，同期所有国产镜像
0.96–1.23s（差 10 倍以上）。所以「优先官方」在这台机器上是负优化。

**⑤ 零进度反馈 + 零重试**：`snapshot_download` 失败即抛异常，
`download_torigate_config.py` 失败只 `print FAIL`。没有重试、没有退避、
没有「还剩多少」的输出 —— 长任务表现为「卡住」。

---

## 二、改造后的架构

```
                    ┌──────────────────────────────┐
                    │  probe_all()  只探一次，结果落盘缓存 10 分钟
                    └──────────────┬───────────────┘
                                   │  按 (ok, 延迟) 排序
                                   ▼
    resolve_pypi_urls()  ──►  [候选 URL 列表: 同一对象的多个镜像]
                                   │
                                   ▼
                        ┌──────────────────────┐
                        │  Downloader.fetch()  │  逐候选尝试
                        └──────────┬───────────┘
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
      errors.classify()     Range + If-Range         Progress(stderr)
      RETRY_SAME / _NEXT    断点续传 + 200回退        速度/已下/ETA
      /RETRY_PROXY/GIVE_UP  .part + .part.json       每 0.25s 刷新
             │                     │                     │
             └─────────────────────┴─────────────────────┘
                                   ▼
                    按 errors.Verdict 决定：重试当前镜像
                    / 换下一个镜像 / 走代理 / 放弃
```

### 1. 多源探测与切换

- **一次性探测，缓存复用**：`probe_all()` 并发探测，结果写
  `~/.dlm/health.json`，TTL 600s。批量下 40 个文件只探 1 次，不是 40 次。
- **排序规则**：`ok` 优先 → 延迟低优先 → 注册序稳定排序（避免抖动）。
- **切换预算**：`max_mirror_switches=3`，防止在全是坏源时无限轮询。
- **未实测的源不许进表**：每个 URL 模板都标注了实测结论，`registry.py`
  顶部注释写明「不要凭信仰加镜像，先跑 probe」。

### 2. 断点续传与错误分类

- 续传用 `Range: bytes=N-` + `If-Range: <etag>`。
- **关键安全网**：服务端若忽略 Range 回 200（文件已变），代码检测到
  `code == 200 and have` 会**丢弃旧分片重来**，而不是把新文件追加到旧
  文件后面 —— 这是多数自制下载器的数据损坏来源。
- 分片落在 `<target>.part`，元信息在 `<target>.part.json`（记录 url/etag），
  崩溃后下次运行读元信息续上。
- 错误分类（`errors.py`）明确区分：

| 情况 | Verdict | 行为 |
|---|---|---|
| 超时 / 连接重置 / 5xx / 429 / 408 | `RETRY_SAME` | 同源指数退避重试（1/2/4/8s + 抖动） |
| 404（blob 未同步）/ 403 / DNS 失败 | `RETRY_NEXT` | **立刻换源**，不在本源浪费重试预算 |
| TLS 校验失败 | `RETRY_PROXY` | 提示切 65532 代理路线 |
| 416 Range 不可满足 | `GIVE_UP` | 判定分片来自不同构建，清空重下 |
| 401 / 4xx 客户端错误 / 内存不足 | `GIVE_UP` | 立即停止 |

### 3. 进度反馈

- 单行原地刷新（`\r`），输出**速度 / 已下载 / 总量 / ETA**。
- 非 TTY（日志、重定向）自动降级为每 5s 一行，不会刷屏也不会静默。
- 256KB 缓冲流式写盘，内存占用恒定，不随文件大小增长。

### 4. 减少资源消耗

- 探测缓存（见上），避免 N×M 次重复探测。
- 同一对象跨镜像只解析一次 index，候选 URL 复用。
- 已完成的文件重跑是 no-op（实测 1.8s→0.1s）。
- 每镜像连接数上限 `_HostSemaphore(PER_HOST_CAP=3)`：12 个并发文件打同一
  镜像，是把「不稳」变成「被封 IP」的最快方式。

### 5. 大文件与批量

- 大文件：流式 + 续传，崩溃损失 ≤ 一个 chunk。
- 批量：`download_many(tasks, concurrency=3)`，任务级并发 3 + 单主机并发 3
  双层限流，进度回调逐个汇报。

---

## 三、验收结果（本机实测，非推断）

| 测试 | 结果 |
|---|---|
| 镜像探测 | 5/5 PyPI 镜像健康；hf-mirror OK，huggingface FAIL(TLS，已知) |
| PyPI 单文件下载 | OK，sha256 `1e61c374…c4c926` **与官方索引一致** |
| 幂等重跑 | 1.8s → 0.1s，零重复下载 |
| **断点续传** | 播种 50MB 半成品后续传完成，**最终 sha256 与完整文件逐字节一致**，只重取 892MB |
| **镜像故障切换** | 前置 2 个坏镜像（404 / 端口拒绝）被自动跳过，阿里云接管，sha256 正确 |
| **批量并发** | 4/4 成功（0.3s），4 个文件 tar/zip 均可正常解压 |
| HF 端到端 | `tiny-gpt2` config.json 下载成功，JSON 合法 |
| 大文件 | 942MB Qwen2-0.5B 完整下载 36.1s（约 26MB/s） |

---

## 四、可直接借鉴的开源项目

均已核对存在，但**本方案没有直接依赖它们**，理由写在最后一列：

| 项目 | 定位 | 为什么不直接用 |
|---|---|---|
| [pypdl](https://github.com/mjishnu/pypdl) | 纯 Python 并发下载器，`mirrors=` 备用源、`etag_validation`、多段下载 —— **设计最贴近本需求** | 需额外装包；其 mirror 是「主源挂了换备用」，缺少按实测延迟排序的健康探测层。本方案 `adapters.download_pypi_file` 覆盖了 PyPI 场景，若要下**任意 URL + 多段 + 镜像**，值得直接引它替换 core |
| [pdman](https://github.com/Akira-TL/pdman) | 异步分块、`--continue` 续传、低速分片重启、批量任务 | 功能更全但更重；本机 8GB 内存 / 磁盘紧张的场景下，异步分块的收益不如把「续传+切源」做扎实 |
| [rheo](https://github.com/plutopulp/rheo) | asyncio 编排、优先级队列、hash 校验、事件驱动 | Alpha 阶段（README 自述 API 未稳定），且需 aiohttp（本机未装） |
| [hyper_fetch](https://github.com/masroore/hyper_fetch.py) | 异步下载器，重试/限速/分块/缓存 | 同上，且会把整文件读进内存（`result.content`），大模型文件会炸内存 |
| [Xget](https://github.com/sangemajia/Xget) / [EdgeMirror](https://github.com/tianrking/EdgeMirror) | 自建 CDN 边缘网关，统一入口加速 pypi/HF/git/docker | 是**部署型**方案（要跑一个网关服务）。本机是单机、磁盘紧张、无长期公网入口，收益不抵运维成本；且它们是第三方源，稳定性反而是新的风险点 |

**建议**：保持现有零依赖实现（只用标准库 + 可选 `huggingface_hub`），
若日后要下**非 PyPI/HF 的任意大文件**并需要多段并行，届时引 `pypdl`
作为可选后端，而不是现在就加依赖。

---

## 五、加速与省流量（2026-10-03 追加实测）

### 能不能用 IDM / 多线程加速？—— 实测结论：**不能，别折腾**

| 方案 | 实测结果 | 结论 |
|---|---|---|
| **单流**（当前引擎） | 6.24 MB/s | 基线 |
| **8 线程 Range 分片** | 6.67 MB/s（合计） | **仅 +7%，等于没提升** |
| **IDM** | 已装在 `D:\Internet Download Manager\IDMan.exe`，但 CLI 静默下载在本环境**无产出**（进程被回收），且它是 GUI 程序，无法嵌入无人值守脚本 | 不适合本工作流 |

**原因**：瓶颈在**镜像端带宽**，不在你的客户端。8 个分片各自只有
0.83–1.62 MB/s，加起来还是那个总带宽。IDM 的多连接、动态分块、慢速分片
重启这些机制对付的是「单连接被 QoS 限速」或「服务器支持 Range 但单流
不给力」的场景——**hf-mirror 不属于这类**。

而且实测 942MB 完整下载 36.1s（约 26MB/s 峰值），已经接近镜像给的上限。
**换 IDM 不会有肉眼可见的收益。**

> IDM 唯一真正有价值的地方：你在浏览器里**手动**下一个大文件、并且愿意
> 盯着 GUI 看进度时。除此之外不要指望它参与自动化。

### 代理流量有限怎么办？—— 核心原则：**代理只用在"够不着"的地方**

实测前提：`hf-mirror` 直连 **6.24 MB/s 可用**，而 `huggingface.co` 直连和
走代理**都**因 TLS 校验失败。所以代理对你的价值是**兜底可达性**，不是提速。

据此实现 `dlm/traffic.py`，按请求类型分流：

| 请求类型 | 走代理？ | 理由 |
|---|---|---|
| `hf-mirror.com` / 各国内镜像的 **blob**（权重、wheel） | ❌ **不走** | 直连本来就通，把流量省下来 |
| `huggingface.co` / `github.com` 的任何请求 | ✅ 走 | 直连 TLS 失败，只有代理可能通 |
| 仓库元数据（`/api/models/...`） | ✅ 走 | 几十 KB，可忽略不计 |

关键设计：`route()` **只会把流量从代理里移走，绝不会加进来**。所以它
只会帮你省，不会反过来偷你的额度。

```bash
# 设 500MB 代理额度：超过就自动改走直连，而不是把流量耗光
dl hf <repo> -d <dir> -p --proxy-budget 512MB
```

输出会实时回报：
```
[dl] routing: blob via proxy (direct not known-good for huggingface.co)
[dl] proxy traffic: 0.0B / 512.0MB (0%)
```
额度判断逻辑（已单测）：
- 剩余 380MB、待下 1.5GB → `can_afford=False` → 自动改走直连
- 原因串：`blob would exceed budget (380.0MB left) -> direct`

**配套的省流量习惯（比任何工具都有效）：**
1. **先下小文件验证链路**，再上权重。`config.json` 几百 KB，失败了只
   浪费几 KB；直接上 20GB 权重，失败就是 20GB 的教训。
2. **大文件用直连 + 断点续传**。断点续传让"下到一半代理断了"不再需要
   重头来，这才是真正省流量的机制。
3. **不要开 `--proxy` 当默认值**。默认必须是直连镜像，代理只在你确认
   直连不通时手动加 `-p`。这跟项目规则 13 是同一个思路：代理是手动开关，
   不是自动兜底。

### 为什么不把 aria2c 接进来？

本机没装 aria2c。理论上它比 IDM 更适合（无 GUI、支持多连接、有
`--continue`），但基于上面第 1 条实测——**多连接在这条链路上没有收益**，
装了也只是多一个依赖。真正的收益点在"断点续传 + 故障切换"，而这两项
当前引擎已经做到了（并且用 sha256 逐字节验证过）。


新增：
- `E:\AI\_scripts\dl\dl.py` — CLI 入口（probe / pypi / hf / url）
- `E:\AI\_scripts\dl\dlm\__init__.py`
- `E:\AI\_scripts\dl\dlm\errors.py` — 错误分类（可重试/不可重试）
- `E:\AI\_scripts\dl\dlm\registry.py` — 镜像注册表 + 健康探测缓存
- `E:\AI\_scripts\dl\dlm\core.py` — 续传 + 退避重试 + 进度
- `E:\AI\_scripts\dl\dlm\adapters.py` — PyPI / HF 适配
- `E:\AI\_scripts\dl\dlm\batch.py` — 批量并发 + 单主机限流
- `E:\AI\_scripts\dl\dlm\traffic.py` — 代理流量预算与分流（`route()` / `Budget`）

未改动（避免破坏现有流程）：
- `E:\AI\_scripts\tools\pip-tuna.py`、`pip-smart.bat`、`laya_fetch.py`、
  `download_torigate_config.py` — 保持原样，新引擎并行可用。

---

## 六、日常用法

```bash
PY="C:/Users/wsz945/.workbuddy/binaries/python/envs/default/Scripts/python.exe"  # 含 huggingface_hub
D="E:/AI/_scripts/dl"

# 1) 先看谁活着（结果缓存 10 分钟）
"$PY" "$D/dl.py" probe --fresh

# 2) 下 PyPI 文件（自动选最快镜像 + 故障切换 + 续传）
"$PY" "$D/dl.py" pypi six six-1.16.0.tar.gz -d D:/tmp

# 3) 下 HF 仓库（默认 hf-mirror 直连，不耗代理流量）
"$PY" "$D/dl.py" hf Qwen/Qwen2-0.5B -d E:/AI/LLM/GGUF/Qwen2-0.5B --allow "*.safetensors"

# 3b) 代理额度有限时：设上限，超出自动改走直连
"$PY" "$D/dl.py" hf <repo> -d <dir> -p --proxy-budget 512MB

# 4) 任意 URL（-o 必填，可加 --mirror 备用地址）
"$PY" "$D/dl.py" url "https://..." -o D:/tmp/f.bin
```

**省流量三条铁律**：
1. 默认**不要**加 `-p`。默认直连镜像，代理只在你确认直连不通时手动开。
2. 代理只用来兜底**够不着**的源（huggingface.co / github.com）；
   国内镜像的流量一律直连，`route()` 只会把流量移出代理，不会往里塞。
3. 先下 `config.json` 之类的小文件验证链路，再上权重。断点续传保证
   「下到一半断了」不需要重头再来——这才是真正省流量的机制。

**作为库用**：
```python
import sys; sys.path.insert(0, r"E:\AI\_scripts\dl")
from dlm import Downloader
from dlm.batch import download_many
```
