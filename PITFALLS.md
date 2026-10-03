# 踩坑清单：GitHub 推送失败（代理相关）

> **实测日期**：2026-10-03　**环境**：Win11 25H2 / Git 2.55.0 / 神舟 CN-H5S
> **结论先行**：GitHub 直连**完全可用**。所有 push 失败都源于
> `~/.gitconfig` 里硬编码了一个**当时没在监听的代理端口**。
> 修复只需一条命令，**不需要开代理**。

---

## 坑 1（主坑）：`.gitconfig` 硬编码死代理端口，push 全部失败

### 失败时的**具体报错**

```
fatal: unable to access 'https://github.com/wangsuizhi012-bot/x.git/':
Failed to connect to github.com:443 over proxy 127.0.0.1 after 2113 ms:
Could not connect to server
```

关键特征（决定了排查方向）：
- 报的是 `over proxy 127.0.0.1` —— **注意没有端口号**
- `2113 ms` 后失败，是**连接被拒**而非超时
- `Could not connect to server` = 端口没人监听，不是网络慢

### 根本原因

`C:\Users\wsz945\.gitconfig` 里有两条**全局**配置：

```ini
[http]
    proxy = http://127.0.0.1:65532
[https]
    proxy = http://127.0.0.1:65532
```

`65532` 是 Nano 代理的端口（项目规则 13 里的那个「手动开关」）。
**但当时它没有在监听**：

```bash
netstat -ano | grep LISTENING | grep ":65532 "
#  (无输出 —— 端口未监听)
```

于是 git 拿着一个「指向不存在服务的代理」去连 GitHub，必然失败。

### 为什么排查花了这么久（关键教训）

**因为代理有三个来源，git 只认其中一个：**

| 来源 | 本次实测值 | git 认吗 |
|---|---|---|
| 环境变量 `https_proxy` | `http://127.0.0.1:12741`（**在监听，活着**） | ❌ 不认 |
| `~/.gitconfig` 的 `http.proxy` | `http://127.0.0.1:65532`（**死的**） | ✅ **只认这个** |
| Windows 系统代理设置 | 未启用 | ❌ 不认 |

所以：

- `env | grep proxy` 显示的是 `12741`（**活的**）→ 误以为代理没问题
- 实际上 git **完全绕过了它**，去连 `65532`（死的）
- **「环境变量里的代理」和「git 实际用的代理」不是同一个东西**

> 这次的大坑就是：用 `env` 去判断 git 的代理配置。**要查 git 的，只能用 git 自己的命令。**

### 排查思路（按这个顺序走，5 分钟内定位）

```bash
# ① 让 git 自己说话 —— 唯一权威来源，能看到它实际连的端口
GIT_TRACE_CURL=1 git push <url> HEAD:main 2>&1 | grep -i proxy
#   输出：Trying 127.0.0.1:65532...        <- 真相在这里
#         connect to 127.0.0.1 port 65532 ... failed: Connection refused

# ② 查 git 的代理配置（注意 --show-origin 能看出是哪个文件）
git config --list --show-origin | grep -i proxy
#   file:C:/Users/wsz945/.gitconfig  http.proxy=http://127.0.0.1:65532

# ③ 确认那个端口到底活没活
netstat -ano | grep LISTENING | grep ":65532 "
#   没输出 = 死代理 = 找到根因

# ④ 对比：环境变量里的代理是另一个端口（很可能活着，容易误导）
env | grep -i proxy
```

**判定口诀**：报错里出现 `over proxy` → 直接去 `git config` 查，
**别看 `env`**。

### 最终解决方法

**不改任何配置**，单次命令临时绕过（最稳，不影响其他项目）：

```bash
git -c http.proxy= -c https.proxy= push -u origin main
```

若你的 `~/.gitconfig` 里已有这个别名（**本机确实已存在**）：

```ini
[alias]
    p = !git -c http.proxy= -c https.proxy= push
```

则直接用：

```bash
git p            # 等价于上面那条临时绕过命令
```

**长期修复（推荐，让 git 彻底不再被死代理拖累）**：

```bash
# 删掉硬编码的代理，git 自动回退到环境变量
git config --global --unset http.proxy
git config --global --unset https.proxy
```

> ⚠️ 删之前先确认你确实不需要「只让 git 走代理」。
> 如果需要，就改成**条件式**：代理开时才用，代理关时自动直连。
> 见文末「如何避免复发」。

### 实测验证

```bash
$ git -c http.proxy= -c https.proxy= ls-remote https://github.com/wangsuizhi012-bot/voice-mindwit.git
3a3c57c78b5c77e4a93c0800b28e0cb991d92888	HEAD
3a3c57c78b5c77e4a93c0800b28e0cb991d92888	refs/heads/main
9ea151b241aca5b2fb1df1fb2add194c312bf357	refs/tags/v1.0
...                                        ✅ 成功
```

---

## 坑 2：MCP GitHub 连接器没有建仓权限

### 报错

```
failed to create repository: POST https://api.github.com/user/repos:
403 Resource not accessible by integration []
```

### 根本原因

WorkBuddy 内置的 GitHub 连接器用的是 **GitHub App 安装令牌**，
其 scope 里**没有 `public_repo` / ` administration`**，只能操作已存在的仓库，
**不能新建**。而 `gh` CLI 用的是你的 **OAuth 令牌**（scopes: `gist`,
`read:org`, `repo`, `workflow`），权限够。

### 排查思路

看到 `403 Resource not accessible by integration` 就该意识到
**这是令牌类型问题，不是网络问题**——因为同一个时刻别的 GitHub 操作
（`get_me`）是成功的。

### 解决方法

**新建仓库用 `gh` CLI，已有的仓库操作可用 MCP 连接器。**

```bash
gh repo create <owner>/<name> --public --description "..." --disable-issues --disable-wiki
```

已实测建成：`https://github.com/wangsuizhi012-bot/dl-resilient-toolchain`

> 顺带一提：MCP 连接器的令牌**权限更小但更安全**（只读+已有仓库）。
> 分工反而合理：建仓用 `gh`（本地令牌），日常读写用 MCP。

---

## 坑 3：curl 能通 ≠ git 能通（TLS 吊销检查）

### 现象

`curl` 走代理时，CONNECT 隧道**建立成功**，但紧接着失败：

```
* CONNECT tunnel established, response 200
* schannel: next InitializeSecurityContext failed:
  CRYPT_E_NO_REVOCATION_CHECK (0x80092012) - 无法检查证书吊销
```

### 根本原因

Windows 的 schannel 引擎默认会检查**证书吊销列表（CRL/OCSP）**，
这需要访问额外的网络端点。在代理/中间人环境下这一步经常失败，
于是**握手在握手后段崩掉**，报错信息与真实原因（吊销检查）看起来毫不相关。

### 排查思路

`CONNECT` 返回 200 说明**代理本身是好的**，问题在 TLS 层。
看到 `schannel` + `CRYPT_E_*` 就该往证书链方向想。

### 试过但**无效**的方案（记录下来省得再试）

| 方案 | 结果 | 为什么无效 |
|---|---|---|
| `git -c http.schannelCheckRevoke=false push` | ❌ 仍失败 | 连**代理都连不上**，走不到 TLS 那一步 |
| 取消环境变量直连 | ❌ 仍失败 | `~/.gitconfig` 里的死代理优先级更高 |
| 换 SSH（`git@github.com:...`） | ❌ `port 22: Connection refused` | 本机未配 SSH，且 22 端口被封 |

### 最终解决方法

**绕开整个问题**：`curl` 的这个失败只影响**用 schannel 的程序**。
`git` 走的是自己的 curl 构建 + OpenSSL，**不受此影响**——
只要代理配置对了（坑 1），git 就能直连成功。

所以：**先修坑 1，再评估是否还需要处理 TLS 问题。**
本机实测修完坑 1 后，git 完全正常，不需要任何 TLS 绕过。

---

## 坑 4：SSH 端口 22 被封

### 报错

```
ssh: connect to host github.com port 22: Connection refused
fatal: Could not read from remote repository.
```

### 根本原因

国内网络对 GitHub 的 **22 端口**普遍封禁（只放行 443）。

### 解决方法（如果确实要用 SSH）

改用 443 端口的 SSH：

```bash
# ~/.ssh/config
Host github.com
    Hostname ssh.github.com
    Port 443
    User git
```

> 本项目最终**没有用 SSH**，因为 HTTPS 直连已经通了，没必要增加复杂度。

---

## 坑 5：`git push` 默认推 `main`，但本地是 `master`

### 报错

```
error: src refspec main does not match any
error: failed to push some refs to '...'
```

### 根本原因

`git init` 在本机默认创建 **`master`** 分支（除非有全局
`init.defaultBranch` 配置），而 GitHub 的默认分支是 **`main`**。
直接 `git push -u origin main` 会因为**本地根本没有 `main` 这个 ref** 而失败。

### 排查思路（1 秒）

```bash
git branch --show-current     # 看清本地到底叫什么
```

> 报错里的 `src refspec main does not match any` 措辞已经提示了：
> **源（src）** 引用 `main` **匹配不到任何东西** —— 是本地没有这个分支。

### 解决方法

两种都行：

```bash
# 方案 A：推送时改名（不影响本地历史）
git push -u origin master:main

# 方案 B：先把本地分支改名成 main（更干净，以后 push 不用带参数）
git branch -M main
git push -u origin main
```

> 本项目最终用方案 A 推送（远程分支 `main`），本地保留 `master`。
> 若希望本地也叫 `main`，执行方案 B 即可。

---

## 📌 如何避免复发

### 0. 新仓库第一件事：`git init` 后立刻确认分支名

```bash
git init && git branch --show-current
# 想要 main 就立刻改名，避免每次 push 都要带 refspec
git branch -M main
```

### 1. 把代理从「硬编码」改成「跟随环境变量」

**问题本质**：`~/.gitconfig` 写死了端口，代理开关状态一变就变死配置。

```bash
# 一次性移除死配置
git config --global --unset http.proxy
git config --global --unset https.proxy
```

移除后 git 会**自动回退到读环境变量**——你开关代理时无需再动 git 配置。

如果确实需要「有时走代理」，用**条件式脚本**而不是静态配置：

```bash
#!/bin/bash
# git-proxy-on / git-proxy-off
PORT=65532
if nc -z 127.0.0.1 $PORT 2>/dev/null; then
  export https_proxy="http://127.0.0.1:$PORT"
  export http_proxy="http://127.0.0.1:$PORT"
  echo "[git] proxy ON  ($PORT is alive)"
else
  unset https_proxy http_proxy
  echo "[git] proxy OFF (port $PORT not listening -> direct)"
fi
git "$@"
```

用：`git-proxy-on push` / `git-proxy-off push`
**它先探活再决定，不会拿死代理去打**——这正是坑 1 的根因。

### 2. 排查代理问题，只用 git 自己的命令

```bash
git config --list --show-origin | grep -i proxy     # git 认的
GIT_TRACE_CURL=1 git ls-remote <url> 2>&1 | grep -i proxy   # git 实际做的
env | grep -i proxy                                   # 只是参考，可能无关
```

**`env` 显示的代理不代表 git 在用。** 这是本次最大的误导。

### 3. 报 `over proxy` 就直奔 git 配置

```
fatal: ... over proxy 127.0.0.1 after NNNN ms: Could not connect to server
    ↑ 关键词：over proxy          ↑ 没端口号说明来自 gitconfig 而非环境变量
```

→ `git config --global --get-regexp proxy` → 查端口是否监听 → 修配置。

### 4. 先验证再提交（30 秒习惯）

```bash
git -c http.proxy= -c https.proxy= ls-remote <url>   # 能列出来 = 网络没问题
```

比直接 `push` 失败再排查快得多，**且不产生任何本地副作用**。

---

## 📋 速查表

| 症状 | 根因 | 一行解决 |
|---|---|---|
| `over proxy 127.0.0.1 after NNNN ms: Could not connect` | `.gitconfig` 死代理 | `git -c http.proxy= -c https.proxy= push` |
| `403 Resource not accessible by integration` | MCP 令牌无建仓权 | 改用 `gh repo create` |
| `CRYPT_E_NO_REVOCATION_CHECK (0x80092012)` | schannel 吊销检查 | 先修代理；git 用 OpenSSL 不受影响 |
| `port 22: Connection refused` | SSH 22 端口被封 | `Hostname ssh.github.com` + `Port 443` |
| `src refspec main does not match any` | 本地是 `master` | `git push -u origin master:main` 或 `git branch -M main` |
| `netstat` 查不到代理端口 | 代理没开 | 开代理，或用直连方案 |

---

## 最终生效的推送方式（已实测跑通，可直接复制）

```bash
# 1) 建仓（用 gh：本地 OAuth 令牌有建仓权，MCP 连接器没有）
gh repo create wangsuizhi012-bot/dl-resilient-toolchain \
  --public --description "Resilient download toolchain..."

# 2) 提交（用 -c 只对本次生效，不污染全局配置）
cd E:/AI/_scripts/dl
git init
git add .
git -c user.name="wangsuizhi012-bot" \
    -c user.email="295518665+wangsuizhi012-bot@users.noreply.github.com" \
    -c commit.gpgsign=false \
    commit -m "feat: resilient download toolchain v1.1.0"

# 3) 推送（-c http.proxy= 绕过硬编码死代理；master:main 处理分支名差异）
git remote add origin https://github.com/wangsuizhi012-bot/dl-resilient-toolchain.git
git -c http.proxy= -c https.proxy= push -u origin master:main
```

**两条 `-c http.proxy= -c https.proxy=` 是本机 push 成功的关键**，
它让本次 push 不走 `~/.gitconfig` 里那个指向 65532 的死代理。

### 一次性根治（可选，会改动你的全局配置）

```bash
git config --global --unset http.proxy
git config --global --unset https.proxy
```

移除后 git 自动回退到读环境变量，此后代理开关无需再动 git 配置。
**前提**：你不再需要「git 固定走某个代理」。

---

## ✅ 实测结果（2026-10-03）

```
$ git -c http.proxy= -c https.proxy= push -u origin master:main
To https://github.com/wangsuizhi012-bot/dl-resilient-toolchain.git
 * [new branch]      master -> main
branch 'master' set up to track 'origin/main'.

$ git ls-remote origin
5a5e8dc9ba84eae2abc55e82919aecf43cb8dc08	HEAD
5a5e8dc9ba84eae2abc55e82919aecf43cb8dc08	refs/heads/main
```

仓库：<https://github.com/wangsuizhi012-bot/dl-resilient-toolchain>
19 个文件 / 3874 行，全部推送成功。

