# 致谢 / Acknowledgements

本项目在设计与实现过程中，参考、借鉴或受益于以下开源项目与资料。
特此致谢；若你使用了本工具，也请了解这些前置知识。

---

## 直接启发的开源项目

以下项目在调研阶段被逐一评估。最终**未引入任何运行时依赖**，
但其设计思路显著影响了本工具链的架构决策（详见 `README.md` 第四节）。

| 项目 | 借鉴了什么 | 为什么最终没用 |
|---|---|---|
| [pypdl](https://github.com/mjishnu/pypdl) | `mirrors=` 备用源列表、`etag_validation` 续传校验、多段并发 | 功能更全但需额外依赖。**本工具链的 `core.py` 是照着它的接口设计的**，若日后需要下任意 URL + 多段并行，它仍是最佳可选后端 |
| [pdman](https://github.com/Akira-TL/pdman) | `.pdman` 临时目录保存分片、低速分片自动重启 | 异步分块更重；本机 8GB 内存 / 磁盘紧张的场景下收益不抵成本 |
| [rheo](https://github.com/plutopulp/rheo) | 优先级队列、指数退避重试、事件驱动进度 | README 自述 Alpha 阶段、API 未稳定；且需 aiohttp（本机未装） |
| [hyper_fetch](https://github.com/masroore/hyper_fetch.py) | 可配置重试/限速/分块、进度回调 | 它把**整文件读进内存**（`result.content`），下模型文件会炸内存 |
| [EdgeMirror](https://github.com/tianrking/EdgeMirror) | 「多源统一入口 + 健康检查」的产品形态 | 是**部署型** CDN 网关（要跑常驻服务）。单机、无公网入口时不抵运维成本 |
| [Xget](https://github.com/sangemajia/Xget) | 源站分类组织方式（pypi/hf/git/docker 分域） | 同上是部署型方案；且第三方源本身引入新的稳定性风险 |

## 生态工具

| 项目 / 工具 | 关系 |
|---|---|
| [huggingface_hub](https://github.com/huggingface/huggingface_hub) | `dl.hf` 的底层实现。**其 Xet 通道在本网络下会 401**，本工具链强制 `HF_HUB_DISABLE_XET=1` |
| [PEP 503](https://peps.python.org/pep-0503/) 简单索引 API | `dl.pypi` 解析 index 页面的依据 |
| [HTTP Range / If-Range](https://developer.mozilla.org/docs/Web/HTTP/Range_requests) | 断点续传的标准依据。**服务端忽略 Range 回 200 时必须丢弃旧分片**，否则会写出损坏文件 |
| Git (schannel/OpenSSL 双后端) | Windows 上 schannel 的吊销检查问题记录于 `PITFALLS.md` 坑 3 |

## 镜像与数据源

本工具链的镜像表是**实测得出**的，向这些公开镜像的维护方致谢：

- 清华 TUNA · 阿里云 · 腾讯云 · 中科大 USTC · 华为云（PyPI 镜像）
- [hf-mirror.com](https://hf-mirror.com)（HF 镜像）
- [ModelScope](https://www.modelscope.cn)

> 特别感谢 **hf-mirror.com** —— 在本机 `huggingface.co` 与 `github.com`
> 均因 TLS 校验失败不可直连的条件下，它是唯一可用的 HF 源
> （实测 6.24 MB/s，支持 Range 断点续传）。

## 规范与工具

- **GitHub CLI (`gh`)** —— 唯一具备建仓权限的路径（MCP 连接器令牌无此权限）
- **WorkBuddy** —— 本项目在其上开发，并已封装为
  `dl-resilient-download` Skill

---

## 声明

本项目所有镜像 URL 模板与延迟数字均为 **2026-10-03 本机实测**。
镜像可用性会随时间变化，**使用前请先跑 `dl probe`** 复核，
不要凭本文档中的历史数字做判断。
