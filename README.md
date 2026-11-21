# ai-proxy-lab

个人自用的 AI 账号统一代理 + 数据落盘管道。为 Mac mini 常开部署设计：
**除账号 OAuth 登录外，全部一键配置。**

架构（三层）：

```
你的客户端 (Claude Code / Codex / 任意 SDK)
        │  http://127.0.0.1:8400        （其他设备经 Tailscale 私网接入）
   logger-proxy（本仓库）—— 转发 + 每个请求完整落盘 JSONL + /healthz 健康检查
        │  http://cliproxyapi:8317
   CLIProxyAPI —— 订阅账号池（Gemini / Claude / Codex / Qwen OAuth）轮换
        │
   各家官方服务
```

## 一键部署（Mac mini / macOS）

```bash
git clone https://github.com/four1er/ai-proxy-lab.git
cd ai-proxy-lab
./proxy.sh setup
```

`setup` 自动完成：检查并拉起 Docker（缺失时经 Homebrew 安装 Docker Desktop）→
建目录与 `.env` → **在本机随机生成本地 API key 并写入配置**（同时把
`cliproxy/config.yaml` 标记为 skip-worktree，本机密钥不会被误提交）→
拉镜像、构建、启动 → 等健康检查通过 → 自检 `/v1/models`。

唯一需要你手动的，是每个账号一次的 OAuth 登录（必须在你自己的浏览器里完成）：

```bash
./proxy.sh login gemini     # Gemini
./proxy.sh login claude     # Claude
./proxy.sh login codex      # Codex / ChatGPT
```

同一提供商重复登录即可加号，代理会 round-robin 轮换、配额耗尽自动切换。
登录态保存在 `cliproxy/auths/`（已 gitignore，绝不要提交或外传）。

客户端统一连 `http://127.0.0.1:8400`，API key 用 `./proxy.sh key` 查看。
两个服务都只绑定 127.0.0.1，不要改成公网监听。

## 日常命令（全走 ./proxy.sh）

| 命令 | 作用 |
|---|---|
| `./proxy.sh status` | 容器状态 + 健康检查 + 数据/登录态/备份占用 |
| `./proxy.sh start / stop / restart` | 启停 |
| `./proxy.sh logs [服务名]` | 跟踪日志 |
| `./proxy.sh test` | 健康检查 + 模型列表自检 |
| `./proxy.sh key` | 查看本机生成的本地 API key |
| `./proxy.sh backup` | 打包 配置+登录态+原始日志 到 `backups/`（保留最近 14 份） |
| `./proxy.sh restore <备份文件>` | 从备份恢复（会先确认，再自动重启验证） |
| `./proxy.sh update` | 拉代码 + 更新镜像 + 重启验证（不动本机配置） |
| `./proxy.sh dataset` | 原始日志清洗成 ShareGPT 风格 SFT 数据集 |
| `./proxy.sh tailscale` | 把 8400 经 Tailscale 私网给你的其他设备用（不暴露公网） |

## Mac mini 常开设置（一次性，系统设置里）

- 节能：接电源时防止自动睡眠；开启「断电后自动启动」
- Docker Desktop / OrbStack：设为登录时启动
- 保持这台 Mac 处于登录状态；远程管理按需开「远程登录 / 屏幕共享」
- 其他设备接入：两边装 Tailscale 后跑 `./proxy.sh tailscale`，用它给出的
  https 地址作 base URL，key 不变

## 数据盘与备份（容灾）

默认数据在 `./data/`。有独立数据盘时，setup 前指定：

```bash
DATA_DIR=/Volumes/Data/ai-proxy-data ./proxy.sh setup
```

（原理：写入 `.env` 的 `AI_PROXY_DATA_DIR`，compose 把它挂到日志代理的 `/app/data`。）

原始日志：`data/raw_logs/YYYY-MM-DD.jsonl`，每行一个请求，含完整请求体
（对话历史）、响应（流式已拼回完整文本）、模型、延迟、token 用量。

备份不是可选项：`./proxy.sh backup` 建议定期跑（可加进 crontab 每天一次），
并把 `backups/` 再同步一份到另一块盘/加密存储。备份包含账号 token 和你的
全部 prompt，**按密钥级别保管**。单盘损坏且无备份 = 登录态重登 + 数据全丢。

清洗成 SFT 数据集（ShareGPT 风格）：`./proxy.sh dataset`，规则：只留 HTTP 200
的对话请求 → 抽取完整多轮对话 → 对话哈希去重 → 丢弃过短/无用户轮次条目。

## 稳定性设计

- 两个容器 `restart: unless-stopped` + logger-proxy 带 Docker healthcheck
- 日志写入失败（磁盘满等）只丢一条日志，不阻断转发；上游不可达返回明确的 502 并落一条错误记录
- CLIProxyAPI 侧：请求重试 3 次、配额耗尽自动切项目/模型、账号池轮换
- 容器日志已限大小（10MB × 3），不会把磁盘写爆

## 风险备忘

- 本项目仅供个人自用自己的账号。多账号轮换聚合额度仍可能违反各家服务条款，
  存在封号风险，不要对外提供服务、不要转售。
- 数据中心 IP 容易触发各家风控；这也是推荐家用 Mac mini（住宅网络出口）
  而非云 VPS 的原因之一。
- 用模型输出做训练数据在多数厂商条款下受限，自用研究与对外发布性质不同。
