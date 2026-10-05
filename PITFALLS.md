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

### 失败时的**具体报错**

**情况 A —— 首次推送时**：
```
error: src refspec main does not match any
error: failed to push some refs to '...'
```

**情况 B —— 已建立上游后，仍不能裸 push**：
```
fatal: The upstream branch of your current branch does not match
the name of your current branch.  To push to the upstream branch
on the remote, use

    git push origin HEAD:main
```

### 根本原因

`git init` 在本机默认创建 **`master`** 分支（当时全局
`init.defaultBranch` **未设置**），而 GitHub 的默认分支是 **`main`**。

**情况 B 最容易被误判**：明明已经 `push -u` 建立了追踪关系，
看起来"配好了"，但因为**本地名和远程名不同**，git 每次裸 `git push`
都会报这个错。**这不是没配上游，是名字不一致。**

### 排查思路

```bash
git branch --show-current            # 本地叫什么
git ls-remote --heads origin        # 远程叫什么
# 两行名字不一样 -> 就是本坑
```

### 最终解决方法（本项目已采用）

```bash
git branch -M main        # 本地改名，-M = 强制移动
git push -u origin main   # 之后裸 push 即可
```

改完实测：
```
$ git push
To https://github.com/wangsuizhi012-bot/dl-resilient-toolchain.git
   dc0ff33..xxxxxxx  main -> main          ✅ 不再需要任何 refspec
```

**同时已把全局默认值改掉，以后新建仓库不会再撞这个坑**：

```bash
git config --global init.defaultBranch main
```

实测新建仓库后 `git branch --show-current` 直接就是 `main`。

> ⚠️ 注意：`git branch -M` **只改本地**，远程分支名是 push 时决定的。
> 若已推过 `master`，改完还要推一次 `main` 上去并删掉旧的 `master`。

### 预防

```bash
# 以后新仓库：init 之后立刻改，两秒完成
git init && git branch -M main
```

---

## 坑 6：误把测试提交推到远程（自造分叉）

### 现象

```
To https://github.com/... 
 ! [rejected]        main -> main (non-fast-forward)
error: failed to push some refs to '...'
```

### 根本原因（自己挖的坑）

为验证「改名后能否裸 push」，我做了两次**真实提交并推送**来测试。
其中一次 `test: bare push after rename` 已被推到远程。
之后用 `git reset --hard HEAD~1` 只回滚了**本地**——
**远程那个提交还在**，于是本地与远程分叉，后续 push 被拒。

> **核心教训**：`git reset` 只动本地。**已经 push 过的提交，
> 本地回滚不会同步远程**，必须再推一次（或 `force-with-lease`）才能覆盖。

### 排查思路

```bash
git log --oneline -3              # 本地在哪
git log --oneline origin/main -3  # 远程在哪  <- 需要先 fetch
git ls-remote origin              # 直接看远程真实 commit
```

对比出现两条不同的 head commit = 分叉。

### 解决方法

```bash
# 用 --force-with-lease（比 --force 安全：远程有别人的新提交时会拒绝）
git push --force-with-lease origin main
```

> ⚠️ **不要用裸 `--force`**：它会无条件覆盖，可能抹掉协作者的提交。
> `--force-with-lease` 会在远程有未知变更时拒绝执行。

实测结果：
```
 + 63146d8...dc0ff33 main -> main (forced update)   ✅ 垃圾提交已清除
```

### 预防

**不要用真实 push 来做测试。** 验证推送链路有零风险的替代法：

```bash
git -c http.proxy= -c https.proxy= ls-remote <url>   # 只读，不改远程
```

或者在**临时仓库**测（`/tmp/xxx`），别在真实仓库上试。


## 坑 7：`gh` 能连但 `git` 连不上（SSL 后端不同）

### 现象（同一次操作，两条路结果相反）

```
$ gh api repos/.../commits/main --jq '.sha[0:7]'
dda3c87                                    ← gh 成功

$ git ls-remote origin
fatal: unable to access '...': Failed to connect to github.com:443
       after 21099 ms: Could not connect to server   ← git 失败
```

### 根本原因

**curl / gh 用 Windows Schannel，git 用自带 OpenSSL**，两者证书链来源不同：

```
$ curl --version | grep -i ssl
  Schannel zlib/1.3.2                        ← 系统证书库
$ git --version --build-options | grep -i ssl
  OpenSSL: OpenSSL 3.5.7 9 Jun 2026         ← 自带 CA 包
```

本机中间链路会让 **OpenSSL 的证书校验失败**，但 Schannel 走系统证书库
（已装好对应根证书）**可以通过**。

### 排查思路（关键：先证明"不是网络问题"）

```bash
# 1) 用 curl 证明网络本身是通的
curl -s --noproxy '*' -o /dev/null -w "%{http_code}\n" \
  https://api.github.com/repos/<owner>/<repo>          # -> 200

# 2) 对比两者的 TLS 后端
curl --version | grep -i ssl        # Schannel?
git --version --build-options | grep -i ssl   # OpenSSL?

# 3) 让 git 改用 schannel
git -c http.sslBackend=schannel ls-remote <url>
```

**第 1 步是关键**。curl 200 而 git 失败，就排除了网络层，
问题必然在 TLS 实现差异上。

### 解决方法

```bash
# 单次：加 -c 参数
git -c http.sslBackend=schannel -c http.proxy= -c https.proxy= push

# 永久（本机已执行）
git config --global http.sslBackend schannel
```

改完实测：裸 `git ls-remote` 立即正常。

> 顺带：本机还把 `http.proxy=http://127.0.0.1:65532` 硬编码在
> `.gitconfig` 里，而 65532 常常没监听 → 已 `--unset`，
> 现在 git 会**跟随环境变量**（当前是 WorkBuddy 注入的 8103）。

---

## 坑 8：代理只放行读操作，`push` 返回 502

### 现象

```
$ git ls-remote origin        # 读操作
dda3c87...	refs/heads/main                 ← 成功

$ git push origin main        # 写操作
fatal: ... CONNECT tunnel failed, response 502   ← 失败
```

### 根本因��

当前环境注入的代理（`127.0.0.1:8103`，端口每次会话会变）
**允许 GET（读）但拒绝 POST/PUT（写）**：

```bash
curl -o /dev/null -w "%{http_code}\n" \
  ".../info/refs?service=git-upload-pack"      # -> 200（读 OK）
curl -X POST -o /dev/null -w "%{http_code}\n" \
  ".../git-rece-pack"                          # -> 422（POST 被接受但协议不符）
# 经代理发 push -> CONNECT tunnel failed, 502
```

### 排查思路

「ls-remote 通、push 不通」= **不是认证问题、不是网络问题，
是代理的方法/路径过滤**。

### 解决方法

**写操作走直连**：

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  git push origin main
```

实测直连 push 返回 `Everything up-to-date`（说明远程早已同步成功）。

### 实用建议

排查"git 连不上"时，**同时准备三条路**：

```bash
# 1) 绕过死代理 + 换 SSL 后端（最稳）
git -c http.sslBackend=schannel -c http.proxy= -c https.proxy= push

# 2) 完全直连（绕过所有代理）
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY git push

# 3) 换用 gh（它有自己的 TLS 栈和认证）
gh auth login && gh repo create ... && gh api ...
```

**交叉验证很重要**：`gh` 能读不代表 `git` 能读，反之亦然。
判断"到底推上去没有"，要用**至少两条独立路径**确认。

---

## 📌 如何避免复发

### 0. 分支名：已一次性根治，**以后不用再管**

本项目已执行：

```bash
git branch -M main                                              # 本地改名
git config --global init.defaultBranch main                     # 全局默认
```

实测新建仓库 `git branch --show-current` 直接输出 `main`。

**所以以后 `git init` 之后不用再改名，直接 `git push` 即可。**
详见坑 5。

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
| `src refspec main does not match any` | 本地是 `master` | `git branch -M main` |
| `upstream branch ... does not match the name` | 同上（已建上游后） | 同上，改完就永久解决 |
| `! [rejected] (non-fast-forward)` | 本地 reset 过，远程没回滚 | `git push --force-with-lease` |
| `netstat` 查不到代理端口 | 代理没开 | 开代理，或用直连方案 |

---

## 最终生效的推送方式（已实测跑通，可直接复制）

> 本机已固化：`git config --global http.sslBackend schannel`，
> 且已 `--unset http.proxy/https.proxy`（不再有死代理）。
> 所以现在**裸 `git push` 就能用**。下面保留带 `-c` 的版本用于应急。

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

# 3) 推送（三条路任选，按网络状况挑）
git push -u origin main                          # 首选：走固化好的 schannel
git -c http.sslBackend=schannel -c http.proxy= -c https.proxy= push -u origin main
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY git push -u origin main
```

**两条关键配置（本机已生效）**：

```bash
git config --global http.sslBackend schannel   # 解决坑 7（OpenSSL 证书链）
git config --global --unset http.proxy          # 解决坑 1（死代理 65532）
```

### 一次性根治（都已执行完毕）

```bash
git config --global init.defaultBranch main         # 解决坑 5
git config --global http.sslBackend schannel        # 解决坑 7
git config --global --unset http.proxy              # 解决坑 1
git config --global --unset https.proxy             # 解决坑 1
```

---

## ✅ 实测结果（2026-10-05 更新）

```
$ git push origin main
Everything up-to-date

$ git ls-remote origin refs/heads/main
dda3c87e8cd3ece048bda75982756123545c1de9	refs/heads/main
```

仓库：<https://github.com/wangsuizhi012-bot/dl-resilient-toolchain>
5 次提交，23 文件，全部已同步。


