# 给 Agent 的接入指南

> 面向要调用 `dl` 工具链的 Agent / 脚本 / 外部工具。
> 完整接口规范见 `CONTRACT.md`；本文件只讲**怎么接进去**。

---

## 30 秒上手

```bash
# 1. 先问它有什么、怎么调（会返回一份 JSON 清单）
python E:/AI/_scripts/dl/dl.py caps --json

# 2. 拿不准某个目标走哪条路？先问，别猜（不下载任何东西）
python E:/AI/_scripts/dl/dl.py route Qwen/Qwen2-0.5B

# 3. 下东西
python E:/AI/_scripts/dl/dl.py hf Qwen/Qwen2-0.5B -d E:/models/qwen --allow "*.safetensors"
```

```python
# Python 内嵌
import sys; sys.path.insert(0, r"E:\AI\_scripts\dl")
from dlm.api import run
env = run("dl.hf", repo="Qwen/Qwen2-0.5B", dest="E:/models/qwen")
```

---

## 能不能给其他 Agent 用？

**能，实测通过。** 下面是验证过的结论和边界。

### 1. 跨目录调用 —— ✅

从任意工作目录调用都正常，不需要先 `cd`：

```bash
cd C:/Users/任意目录
python E:/AI/_scripts/dl/dl.py caps        # OK
```

### 2. 退出码 —— ✅（Agent 判断成败的可靠依据）

| 场景 | 退出码 |
|---|---|
| 成功 | `0` |
| 参数错 | `10` |
| 源全挂 | `11` |
| 找不到 | `12` |
| 需 token | `13` |
| 代理不通 | `15` |
| 未知/已废弃工具 | `19` |

**注意**：Windows CMD / Git Bash 里**只有 0 和非 0 可区分**。
要拿具体错误码，读 JSON 的 `error.code`，别依赖 `$?`。

### 3. 冷启动自举 —— ✅（这是给 Agent 用的关键能力）

实测：只给它 `capabilities.json` 路径，一个完全不了解 `dl` 的 Agent 能自行
完成推理并下载成功。它走的路径是：

```
读 capabilities.json
  → 从 entrypoints 学到调用方式
  → 从 tools[] 找到 dl.hf
  → 从 params[].required + desc 知道要填什么
  → 抄 examples[0].cli 作为命令模板
  → 执行 --json 读信封
```
**这就是为什么每个参数都必须有 `desc`、每个工具都必须有 `examples`。**

### 4. 并发 —— ✅

| 场景 | 结果 |
|---|---|
| 4 个 Agent 并发下不同文件 | 4/4 成功，1.3s 墙钟，文件均可解压 |
| 3 个 Agent 并发下**同一**文件 | sha256 正确，**无残留 `.part`**，数据无损 |

原子落盘（`.part` → `os.replace`）保证同文件并发不会写坏数据。

### 5. Python 导入 —— ✅

```bash
# 方式 A：PYTHONPATH（更干净，无需 sys.path 技巧）
set PYTHONPATH=E:/AI/_scripts/dl
python -c "from dlm.api import run; print(run('dl.caps')['ok'])"

# 方式 B：sys.path.insert（不依赖环境变量）
python -c "import sys; sys.path.insert(0,r'E:\AI\_scripts\dl'); from dlm.api import run"
```

> ⚠️ `dl.py` 和 `dlm/` 必须在**同一目录**，两者是配套的。

---

## 边界与注意事项（Agent 必读）

### ❌ 不要给命令加 `2>&1 | head -N`

管道会截断输出。信封走 stdout，截断后 JSON 不完整 → 解析失败。
需要限量用 `head` 时，改用 `--json` 之外的人读模式。

### ❌ 不要匹配 `error.message`

它是人类可读文本，**会变**。只匹配 `error.code`（稳定契约）。

### ❌ 不要在没探测端口时就传 `proxy=True`

本项目规则 13：代理是**手动开关**。虽然 `dl` 有降级保护（代理没开会自动
走直连 + warning，不会失败），但你**应该先确认代理已开**再传 `-p`。
`dl route <target> -p` 会直接告诉你结果。

### ✅ 失败了就看 `retryable`

```json
{"code":"E_NO_HEALTHY_SOURCE","retryable":true}   → 稍后重试
{"code":"E_NOT_FOUND","retryable":false}          → 改名/换源，不要重试
```
重试前**先 `dl probe`**，确认是不是镜像全挂了。

### ✅ 大文件先验链路

先下 `config.json`（几百 KB）确认链路通，再上权重。这比任何重试策略都省。

---

## 推荐的 Agent 调用模板

```python
import sys, json
sys.path.insert(0, r"E:\AI\_scripts\dl")
from dlm.api import run, exit_code_for

def safe_download(tool, **params):
    """带路由预检和错误分类的下载封装。"""
    env = run(tool, **params)
    if env["ok"]:
        return env["result"]

    err = env["error"]
    code, retryable = err["code"], err["retryable"]
    if code == "E_PROXY_UNREACHABLE":
        # 代理没开：降级直连重试一次（hf-mirror 直连可用）
        env = run(tool, **{**params, "proxy": False})
        if env["ok"]:
            return env["result"]
    if retryable:
        raise RuntimeError("retryable failure: %s" % code)
    raise ValueError("permanent failure: %s - %s" % (code, err["message"]))

# 用
r = safe_download("dl.hf", repo="Qwen/Qwen2-0.5B",
                  dest="E:/models/qwen", allow=["*.safetensors"])
print(r["path"], len(r["files"]), "files")
```

### Shell 版（关心退出码时）

```bash
if python E:/AI/_scripts/dl/dl.py hf "$REPO" -d "$DEST" --json > out.json; then
  echo "OK: $(jq -r .result.path out.json)"
else
  rc=$?
  case $rc in
    13) echo "需要 HF token" ;;
    15) echo "代理不通，检查 65532" ;;
    19) echo "工具 id 错了或已废弃" ;;
    *)  echo "失败 rc=$rc" ;;
  esac
fi
```

---

## 快速命令速查

| 命令 | 用途 | 网络 |
|---|---|---|
| `dl caps --json` | 能力清单 | 否 |
| `dl route <target>` | 看会走哪条路 | 仅一次端口探测 |
| `dl probe --fresh` | 强制重探镜像 | 只读探测 |
| `dl health` | 读缓存健康数据 | 否 |
| `dl pypi <pkg> <file> -d <dir>` | 下 PyPI 文件 | 是 |
| `dl hf <repo> -d <dir> [--allow ...]` | HF 仓库快照 | 是 |
| `dl url <url> -o <path> [--mirror <url>]` | 任意 URL | 是 |

**加 `--json` 到任何命令** → 拿完整信封。

---

## 已验证清单（2026-10-03 本机实测）

| 能力 | 状态 |
|---|---|
| 跨目录调用 | ✅ |
| 退出码语义 | ✅ 0/10/11/12/13/15/19 |
| Python `PYTHONPATH` 导入 | ✅ |
| 冷启动自举（只给清单） | ✅ Agent 自行推理并下载成功 |
| 4 路并发不同文件 | ✅ 4/4 |
| 3 路并发同一文件 | ✅ sha256 正确，无残留 |
| API/CLI 信封一致 | ✅ 逐字节比对 |
| 错误码分类 | ✅ 无 `E_INTERNAL` 泄漏 |
| API 不抛异常 | ✅ 4 类畸形输入全返回信封 |
