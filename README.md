# ai-proxy-lab

个人自用的 AI 账号统一代理 + 数据落盘管道。

架构（三层）：

```
你的客户端 (Claude Code / Codex / 任意 SDK)
        │  http://127.0.0.1:8400
   logger-proxy（本仓库）—— 转发 + 每个请求完整落盘 JSONL
        │  http://cliproxyapi:8317
   CLIProxyAPI —— 订阅账号池（Gemini / Claude / Codex / Qwen OAuth）轮换
        │
   各家官方服务
```

## 启动

```bash
docker compose up -d
```

- 客户端统一连 `http://127.0.0.1:8400`，API key 用 `cliproxy/config.yaml` 里
  的 `api-keys`（默认 `sk-local-change-me`，务必改掉）。
- 两个服务都只绑定 127.0.0.1，不要改成公网监听。

## 登录你的账号（每个账号执行一次）

在容器里跑 CLIProxyAPI 的官方登录命令，按提示在浏览器完成 OAuth：

```bash
docker compose run --rm cliproxyapi ./CLIProxyAPI --login          # Gemini
docker compose run --rm cliproxyapi ./CLIProxyAPI --claude-login   # Claude
docker compose run --rm cliproxyapi ./CLIProxyAPI --codex-login    # Codex / ChatGPT
```

登录态保存在 `cliproxy/auths/`（已在 .gitignore 里，绝不要提交或外传）。
多个账号重复登录即可，代理会 round-robin 轮换，配额耗尽自动切换。

## 数据管道

原始日志：`data/raw_logs/YYYY-MM-DD.jsonl`，每行一个请求，含完整请求体
（对话历史）、响应（流式已拼回完整文本）、模型、延迟、token 用量。

清洗成 SFT 数据集（ShareGPT 风格）：

```bash
pip install -r requirements.txt   # 仅清洗脚本需要时可跳过依赖
python3 scripts/clean_dataset.py --out data/dataset/train.jsonl
```

清洗规则：只留 HTTP 200 的对话请求 → 抽取完整多轮对话 → 按对话哈希去重 →
丢弃总长度 < 200 字符或没有用户轮次的条目。参数 `--min-chars / --min-turns` 可调。

注意：原始日志包含你自己的全部 prompt 和回复内容，是敏感数据：
只存本地、不要进 Git（.gitignore 已排除 data/）、不要上传到任何网盘公开链接。

## 不用 Docker 的跑法（只跑日志代理）

```bash
pip install -r requirements.txt
UPSTREAM=http://127.0.0.1:8317 python3 logger_proxy.py   # 监听 8400
```

## 风险备忘

- 本项目仅供个人自用自己的账号。多账号轮换聚合额度仍可能违反各家服务条款，
  存在封号风险，不要对外提供服务、不要转售。
- 用模型输出做训练数据在多数厂商条款下受限，自用研究与对外发布性质不同。
