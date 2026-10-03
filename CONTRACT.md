# 工具链规范（Toolchain Contract v1.1.0）

> 面向外部调用方（Agent / 脚本 / 其他工具）的接口约定。
> 上层设计见 `README.md`；本文件只讲**怎么调**。

---

## 0. 整体设计思路（一句话）

> **API 是唯一执行路径，CLI 只是它的薄壳。**

`dl.py` 里**没有任何下载逻辑**——它只做参数解析、把结果打印出来。
所有实际工作都在 `dlm/api.py::run()`。这带来一个结构性保证：

```
        ┌──────────────────────────────┐
        │  dlm/api.py :: run(tool, **p) │  ← 唯一执行路径
        │  · 路由（router.py）           │
        │  · 执行（core/adapters）        │
        │  · 归类（contract.py）          │
        └──────────────┬───────────────┘
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
   dl.py (CLI)                 import dlm.api (库)
   argparse + 打印              直接拿 dict
```

行为一致性不是靠"两边都写对了"，而是**物理上只有一份实现**，
所以不可能漂移。已用逐字节比对验证（见 §6）。

---

## 1. 接口层

### 1.1 两种调用方式

**CLI**
```bash
python E:/AI/_scripts/dl/dl.py <tool> [参数] [--json]
```

**API**
```python
import sys; sys.path.insert(0, r"E:\AI\_scripts\dl")
from dlm.api import run, exit_code_for
env = run("dl.hf", repo="Qwen/Qwen2-0.5B", dest="E:/models/qwen")
rc  = exit_code_for(env)     # 与 CLI 的进程退出码一致
```

### 1.2 统一响应信封（Envelope）

**所有**调用——成功或失败——返回同一个结构：

```json
{
  "schema_version": "1.0.0",
  "tool": "dl.hf",
  "tool_version": "1.1.0",
  "status": "ok",              // ok | error | partial
  "ok": true,
  "error": null,               // 失败时为对象，见 §1.3
  "result": { },               // 成功时的负载；失败时为 null
  "metrics": {
    "elapsed_s": 3.51,
    "attempts": 1,
    "bytes": 662
  },
  "warnings": [ ]
}
```

约定：
- `ok === true` ⟺ `status === "ok"` ⟺ `error === null` 且 `exit code === 0`
- **API 永不抛异常**。任何内部错误都被转成 `status:"error"` 的信封。
  调用方不需要 try/except。

### 1.3 错误对象

```json
"error": {
  "code": "E_NOT_FOUND",
  "message": "http 404 blob (mirror not synced)",
  "retryable": false,
  "hint": "check the filename spelling",
  "detail": { "attempts": 5 }
}
```

`code` 是**稳定契约**，`message` 是人类可读、可能变化，**不要匹配 message**。

### 1.4 错误码表

| code | exit | 可重试 | 含义 |
|---|---|---|---|
| `E_OK` | 0 | - | 成功 |
| `E_USAGE` | 10 | ✗ | 参数缺失/非法 |
| `E_NO_HEALTHY_SOURCE` | 11 | ✓ | 所有候选源都失败，稍后重试 |
| `E_NOT_FOUND` | 12 | ✗ | 所有源上都不存在该对象 |
| `E_AUTH_REQUIRED` | 13 | ✗ | 需要 HF token |
| `E_BUDGET_EXCEEDED` | 14 | ✗ | 会超出代理预算 |
| `E_PROXY_UNREACHABLE` | 15 | ✓ | 代理/目标端口未监听 |
| `E_TLS_BLOCKED` | 15 | ✓ | TLS 校验失败（链路被劫持） |
| `E_INTEGRITY` | 16 | ✓ | 校验和不符 / 传输损坏 |
| `E_TIMEOUT` | 17 | ✓ | 超时窗口内无数据 |
| `E_PARTIAL` | 18 | ✓ | 批量任务部分成功 |
| `E_UNSUPPORTED` | 19 | ✗ | 未知工具 id |
| `E_DEPRECATED` | 19 | ✗ | 工具已废弃 |
| `E_INTERNAL` | 20 | ✗ | 未预期错误（**不应出现**） |

**退出码分组规则**：调用方可以只看退出码区间分支，不必解析错误码。
`0` 成功 · `10-19` 客户端/网络可判定 · `20` 内部错误（需要人看）。

---

## 2. 可发现性

### 2.1 机器可读清单

```bash
python dl.py caps --json          # 运行时生成（永远最新）
```
磁盘副本：`E:\AI\_scripts\dl\capabilities.json`（UTF-8, LF, 2 空格缩进）

> ⚠️ **清单由 `dlm/api.py::TOOLS` 单一数据源生成**，
> 所以不存在"CLI 里有、清单里没有"的漂移。加工具只改一处。

### 2.2 清单结构

```jsonc
{
  "name": "dl",
  "version": "1.1.0",              // 工具链版本（semver）
  "schema_version": "1.0.0",       // 信封结构版本（见 §4.1）
  "description": "...",
  "entrypoints": {
    "cli": "python dl.py <tool> [params]",
    "api": "from dlm.api import run; run('dl.hf', repo=..., dest=...)"
  },
  "invariants": [ "调用方可以依赖的保证" ],

  "tools": [
    {
      "id": "dl.hf",               // 稳定标识，永不复用
      "version": "1.1.0",          // 该工具自身的版本
      "status": "stable",          // stable | experimental | deprecated
      "since": "1.0.0",            // 引入版本
      "summary": "...",
      "params": {
        "repo": {
          "type": "str", "required": true,
          "cli": "positional",     // 对应的 CLI 形式
          "desc": "e.g. Qwen/Qwen2-0.5B"
        },
        "allow": { "type": "list[str]", "default": [], "cli": "--allow" }
      },
      "returns": "path, files[], route{flow,reason}",
      "errors": ["E_AUTH_REQUIRED", "E_TLS_BLOCKED", "..."],
      "network": "yes"             // 供调用方判断是否可离线执行
    }
  ],

  "error_codes": {
    "E_NOT_FOUND": { "exit": 12, "retryable": false, "meaning": "..." }
  }
}
```

### 2.3 Agent 自动发现路径

推荐顺序（成本递增，信息递增）：

1. **读文件** `capabilities.json` —— 零进程开销，可直接塞进上下文
2. **`dl.py caps --json`** —— 运行时权威副本
3. **`dl.py --help`** —— 人类可读
4. **`dl.py route <target>`** —— 拿到"这个目标该走哪条路"的具体决策

第 4 步是本工具链特有的：**先解释再执行**，避免调用方猜参数。

---

## 3. 路由决策流程

### 3.1 判定顺序（先匹配先胜，这是契约）

```
调用 run(tool, params)
  │
  ├─1. 工具已废弃?        → E_DEPRECATED，附 removed_in / replaced_by
  ├─2. 工具 id 未知?      → E_UNSUPPORTED
  ├─3. 必填参数缺失?      → E_USAGE，附 missing[]
  ├─4. 本地工具?          → 直接执行（probe/caps/health 不需要路由）
  ├─5. 要求代理但端口不通 → 降级直连 + warning（★ 不致命）
  ├─6. 具体路由
  │    ├ dl.hf   无 -p → direct  国内镜像，0 代理流量
  │    ├ dl.hf   有 -p → proxy   huggingface.co 直连被 TLS 拦
  │    ├ dl.pypi       → direct  永不走代理
  │    └ dl.url   域名在 DIRECT_OK_HOSTS → direct
  │              域名在 PROXY_ONLY_HOSTS 且无代理 → E_TLS_BLOCKED
  └─7. 执行 → 归类 → 信封
```

### 3.2 第 5 步的设计取舍（重要）

**代理未监听 ≠ 致命错误。**

本机实测：`hf-mirror` 直连 **6.24 MB/s 可用**。所以当调用方要代理
但代理没开时，**拒绝服务比直连更糟**。因此：

```
dl route Qwen/Qwen2-0.5B -p
  flow    : proxy_fallback
  endpoint: https://hf-mirror.com
  proxy   : False
  reason  : proxy port 65532 is not listening -> falling back to direct
  warn    : proxy_unreachable:port_65532_not_listening
```

调用方拿到的是**能用的结果 + 一条警告**，而不是一个错误。

### 3.3 参数约定

| 约定 | 说明 |
|---|---|
| **默认直连** | 代理必须显式 `-p`/`proxy=True`。**永不自动开启**（项目规则 13） |
| **UA 钉死** | 内部固定 `pip/24.0`。清华对浏览器 UA 返回 403 |
| **不写死版本号** | 路径用环境变量/相对定位，工具清单里没有版本化路径 |
| **布尔即开关** | `proxy=False` 不只是"不用代理"，还会**清除**环境变量 |
| **size 字符串** | CLI 收 `512MB`/`1.5GB`/`2G`；API 收整数字节 |

### 3.4 API / CLI 行为一致性要求

这是**必须成立**的性质，靠测试保证：

1. CLI 每个子命令 = 构造 params dict + 调 `run()` + 打印信封
2. 同一请求，`run()` 与 `dl.py --json` 的信封**逐字节相同**（除计时字段）
3. 退出码由 `exit_code_for(envelope)` 统一计算，CLI 直接返回它
4. CLI **不得**新增任何 API 里没有的能力

> 违反第 1 条的典型反例：为了少一次网络请求，CLI 自己偷偷加了个 HEAD 探测。
> 这会让两条路径行为分叉。**不要这么优化。**

---

## 4. 版本与扩展

### 4.1 三个独立版本号

| 版本 | 位置 | 何时变 |
|---|---|---|
| 工具链 `version` | 清单根、`dl --version` | 新增工具、破坏性变更 → minor；移除/改语义 → major |
| 信封 `schema_version` | 信封内 | 信封字段增删 → minor；改含义/删字段 → major |
| 工具 `tool.version` | 清单每个 tool 内 | 该工具行为变化 |

信封带 `schema_version` 的意义：调用方能**在解析前**判断字段是否认识，
而不必靠 try/except 猜。

### 4.2 工具生命周期

```
   [新增] --status: experimental --> stable --> deprecated --> [移除]
              （可跳过 experimental）      │                      │
                                          └── replaced_by ──────┘
```

| status | 含义 | 调用方应做什么 |
|---|---|---|
| `experimental` | 接口可能变 | 可以试，别写进生产依赖 |
| `stable` | 接口冻结 | 正常使用 |
| `deprecated` | 仍可用但会删 | **改用 `replaced_by`**，在 `removed_in` 之前 |

### 4.3 怎么新增一个工具

**只改一个文件** `dlm/api.py`：

```python
TOOLS["dl.mytool"] = {
    "version": "1.0.0",
    "status": "experimental",        # 新工具一律先 experimental
    "since": "1.2.0",
    "summary": "一句话说明",
    "params": {
        "target": {"type": "str", "required": True,
                   "cli": "positional", "desc": "..."},
    },
    "returns": "...",
    "errors": [Code.TIMEOUT],
    "network": "yes",
}

def _t_mytool(p):        # 签名固定：params dict -> envelope
    return envelope("dl.mytool", "1.0.0", "ok", result={...})

DISPATCH["dl.mytool"] = _t_mytool
```

再在 `dl.py` 加一个 `elif args.cmd == "mytool":` 分支调 `run(...)`。

**检查清单**：
- [ ] `id` 命名空间唯一，**永不复用**旧 id
- [ ] 参数全部经 `contract.make_error` 归类，不要自造错误码
- [ ] 函数不抛异常（`run()` 兜底，但自己处理更清晰）
- [ ] 重新生成清单：`python -c "...json.dump(manifest(), ...)"` 或直接跑 `dl.py caps --json > capabilities.json`
- [ ] 在本文档 §2.2 的工具表登记

### 4.4 怎么废弃一个工具

```python
"dl.legacy": {
    "version": "0.9.0",
    "status": "deprecated",         # ← 关键
    "since": "1.0.0",
    "deprecated_since": "1.1.0",
    "removed_in": "1.3.0",          # 承诺的移除版本
    "replaced_by": "dl.url",
    "summary": "...",
    "params": {},
}
```

**只需改 `status`**，调用即返回：
```json
{"code": "E_DEPRECATED",
 "hint": "replaced_by=dl.url removed_in=1.3.0"}
```
且它**仍然出现在清单里**（带 `removed_in`），所以调用方能提前看到并迁移——
这是"废弃"和"消失"的区别。

> 实现注记：废弃检查必须在 dispatch 查找**之前**。否则工具被移出
> `DISPATCH` 后会被误报成 `E_UNSUPPORTED`，调用方看不到替代方案。

### 4.5 调用方如何感知变更

| 想知道 | 怎么做 |
|---|---|
| 现在有哪些工具 | 读 `capabilities.json` 或 `dl.py caps --json` |
| 某工具是否已废弃 | 清单里查 `status` / `removed_in` / `replaced_by` |
| 信封字段是否认识 | 先看 `schema_version`，与自己的期望比对 |
| 某工具失败了能不能重试 | 信封 `error.retryable`，或清单 `error_codes[code].retryable` |
| 有没有新工具 | 比对两次 `capabilities.json` 的 `version` 与 `tools[].id` |

**建议的调用方模式**：
```
启动时  → 读 capabilities.json，缓存 version + tools[].id
调用前  → 校验目标 id 在缓存里且 status != deprecated
收到响应 → 若 schema_version 不认识 → 走保守路径或告警
失败时  → 只按 error.code 分支；code.retryable 决定是否重试
```

---

## 5. 工具清单（7 项）

| id | v | 状态 | 网络 | 用途 |
|---|---|---|---|---|
| `dl.probe` | 1.0.0 | stable | 只读探测 | 镜像健康检查与延迟排序 |
| `dl.route` | 1.0.0 | stable | 否（一次端口探测） | 解释某目标会走哪条路，**不下载** |
| `dl.pypi` | 1.0.0 | stable | 是 | 下载单个 PyPI 分发文件 |
| `dl.hf` | 1.1.0 | stable | 是 | HF 仓库快照，默认直连省代理流量 |
| `dl.url` | 1.0.0 | stable | 是 | 任意 URL，支持续传与备用源 |
| `dl.caps` | 1.0.0 | stable | 否 | 返回本清单 |
| `dl.health` | 1.0.0 | stable | 否 | 读缓存的健康数据，不重探 |

---

## 6. 验收记录（本机实测）

| 测试 | 结果 |
|---|---|
| API vs CLI 信封**逐字节比对** | `dl.route`×2 + `dl.pypi`（真实下载）**全部 match=True** |
| 退出码一致 | API `exit_code_for()` == CLI 进程退出码，**全部一致** |
| 错误码分类 | 6 个用例全部命中预期码，**无 `E_INTERNAL` 泄漏** |
| API 不抛异常 | 4 个畸形输入（空 repo / 空串 / retries=1e9 / 空白 dest）全部返回信封 |
| 路由决策 | 4 种目标形态（pypi 包 / HF repo / HF URL / github URL）推断均正确 |
| 代理降级 | `-p` 且端口未监听 → `flow=proxy_fallback` + warning，**非致命** |
| 废弃生命周期 | `status: deprecated` → `E_DEPRECATED` + `replaced_by`/`removed_in`，且仍在清单中 |
| 全量回归 | 7 个工具 × 2 接口 + 真实网络下载，全部通过 |

### 过程中修掉的 3 个真实缺陷

1. **`errors.py` 的 `_TLS_ERRNOS` 用了 set 而非 tuple**
   → `isinstance()` 抛 `TypeError`，**导致所有传输层失败被吞成 `E_INTERNAL`**。
   已改为 tuple 并加注释防止"优化"回去。
2. **TLS 错误藏在 `URLError.reason` 里**
   → 新增 `_unwrap()` 穿透 urllib 包装，否则无法区分 TLS / 死端口 / DNS。
3. **废弃检查晚于 dispatch 查找**
   → 废弃工具被误报 `E_UNSUPPORTED`，调用方看不到替代方案。已调整顺序。
