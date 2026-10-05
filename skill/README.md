# Skill 副本

`SKILL.md` 是本工具链的 **WorkBuddy Skill 定义**，仓库里这份是**副本**。

## 两份的区别与关系

| 位置 | 作用 | 谁在读 |
|---|---|---|
| `C:\Users\wsz945\.workbuddy\skills\dl-resilient-download\SKILL.md` | **生效的那份** | WorkBuddy 加载器 |
| 本目录 `skill/SKILL.md` | **备份 / 分发用** | 人、或换机时复制 |

> **实际生效的只有 `~/.workbuddy/skills/` 下那份。**
> 改仓库里的副本**不会**影响当前安装；反之亦然。

## 换机 / 重装时怎么用

```bash
# 复制到 Skill 目录即可生效
cp -r skill/ ~/.workbuddy/skills/dl-resilient-download/
#   目录名必须叫 dl-resilient-download，且与 frontmatter 的 name 字段一致

# 验证加载器能认出来
python E:/AI/_scripts/dl/dl.py caps    # 引擎本身要可用
```

## 同步规则（改了一边要记得另一边）

1. **先改生效的那份**：`~/.workbuddy/skills/dl-resilient-download/SKILL.md`
2. 再复制回仓库：`cp ~/.workbuddy/skills/dl-resilient-download/SKILL.md skill/SKILL.md`
3. 提交并推送

---

## 踩过的坑（**改 Skill 前必读**）

### 1. `description` 必须同时含**中文**和**英文**触发词

初版 `description` 写的是纯英文，结果**中文提问时不触发**。
模型是按语义相似度选 Skill 的——你用中文问「下载模型卡住了」，
拿纯英文描述去匹配，命中率很低。

现在中英混写，并显式列出触发词：
> 下载、下文件、下模型、下包、下载包、下载卡住、超时、换源、
> 断点续传、代理下载、hf-mirror、huggingface 下载、大文件下载、
> model download、download failed …

**这是本 Skill 最重要的字段**，不是可有可无的说明文字。

### 2. 必填字段（缺了可能不加载）

```yaml
name:            # 必须与目录名完全一致
description:     # 必须（触发靠它）
display_name:    # 建议，UI 显示用
version:         # 建议
agent_created:   # 建议，本机 57 个 skill 中 15 个有它
allowed-tools:   # 建议，限制可用工具
```

> `name` 与目录名不一致是最隐蔽的错误——文件在，但加载器找不到。

### 3. frontmatter 必须能被 YAML 解析

用 `yaml.safe_load()` 验一遍最稳。注意**中文冒号、全角引号**都可能让解析失败：

```python
import re, yaml
s = open("SKILL.md", encoding="utf-8").read()
fm = re.match(r"^---\n(.*?)\n---\n", s, re.S).group(1)
print(yaml.safe_load(fm))
```

（本机 `versions/3.13.12` 那个 python 没装 yaml，用
`envs/default` 那个，它有。）

### 4. 路径写死是隐患

Skill 正文里写的是 `E:/AI/_scripts/dl/`。**换机或移动目录后就会失效**，
且报错是「文件不存在」，很难联想到是 Skill 里的路径过期。

所以 Skill 开头放了**自检命令**：

```bash
python E:/AI/_scripts/dl/dl.py caps
```

跑不通就说明环境变了，**先别往下走**。
