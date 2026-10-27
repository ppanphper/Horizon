# 本地 Web 与 Profile 集成版

本实现位于隔离 worktree `/Users/pandy/ai-code/horizon-web-integrated`，分支 `integrate/local-web-profiles`。原工作区 `/Users/pandy/ai-code/horizon` 的 index、已修改及未跟踪文件均未写入。

## 功能

- 默认入口仍为 `python -m src.web.server`，保留 `--root`、`--host`、`--port`。
- 原概览、模型、信息源、规则、高级 JSON、运行/停止、日志以及日报排版/原文/下载全部保留。
- 原 `/api/state`、`/api/run`、`/api/run/stop`、`/api/config` POST、`/api/validate`、`/api/summaries` 合约保留。
- 新侧栏「Profile 管理」支持列表、查看、复制、新建、编辑定义与三种提示词、删除及引用检查。内置项只读，带 `.user-profile` 的用户项可编辑。
- 新 `/api/profiles` CRUD 和 `/api/profiles/{id}/copy` 使用原 `X-Horizon-Token`；所有写入仍受同源检查保护。
- 路由使用独立 `/api/profile-routing` GET/PUT，避免覆盖旧 `/api/config` 合约。支持全部来源的单 Profile、数组候选和自动路由。
- Profile、路由、旧配置保存与启动使用同一个进程内锁，运行中禁止修改；Profile 文件锁、revision 与引用保护保留。
- 保留 HTTP/2 抓取与 OpenAI Responses 协议，同时保留上游 ChatGPT OAuth、extra_body 等新增代码。
- 原功能分支独立控制台留在 `src.web.profile_server` 和 `profile_static/`，仅用于兼容原模块/回归测试；默认入口不是它。

## 隔离运行（须自行准备测试配置）

```sh
cd /Users/pandy/ai-code/horizon-web-integrated
uv sync --extra dev
# 不要直接使用生产 root；先准备独立配置、profiles 和 .env。
uv run python -m src.web.server --root /path/to/test-root --port 18765
```

本次没有复制生产 `.env`、配置、日报到此 worktree，没有推送、构建/启动部署容器或重启服务。测试只启动随机 loopback 端口的临时服务并结束它们。

Compose 示例保持本机端口发布；web 的 profiles 挂载改为可写以支持 Profile CRUD，CLI 仍只读。该部署路径尚未构建/重启验证。不要将无认证控制台公开到网络。

## 验证

```sh
uv run pytest -q
node --check src/web/static/app.js
git diff --check
```

测试覆盖原 API/UI、日报阅读、HTTP2、Responses、Profile CRUD、revision 冲突、路由、引用删除保护、运行锁、CSRF 与上游功能。另以真实 headless Chrome 在临时 root 验证旧界面加载、复制、编辑保存与磁盘读回、Responses 选择框且无 JS 异常。未进行真实付费 AI 请求、生产数据迁移或部署重启验证。

编辑 Profile 页后应使用该页专用保存按钮；路由当前采用带说明的 JSON 编辑器。内置 Profile 请先复制再编辑。
