# 本地 Web 配置台

这是本地新增的配置与运行界面。上游原有的 `docs/` 是 Jekyll 日报展示站点，配置流程原本使用终端向导和 JSON 文件。

## 打开与管理

在 Docker Desktop 运行的情况下：

```bash
cd /Users/pandy/ai-code/horizon
docker compose -f compose.local.yaml up -d web
```

浏览器访问 **http://127.0.0.1:18764**。端口只绑定本机，不开放到局域网。页面没有登录功能，请保持此绑定方式。

```bash
# 查看服务状态与日志
docker compose -f compose.local.yaml ps
docker compose -f compose.local.yaml logs --tail 100 web

# 停止 / 重启
docker compose -f compose.local.yaml stop web
docker compose -f compose.local.yaml restart web

# 修改代码后重新构建
docker compose -f compose.local.yaml up -d --build web
```

本机出网使用已有代理 `http://host.docker.internal:1087`。模型服务如运行在宿主机上，API 地址应填写 `http://host.docker.internal:端口/v1`。

## 配置与生成

1. **AI 模型**：选择协议、填写模型名称、API 根地址和密钥。OpenAI 兼容网关选择“OpenAI 兼容”，再在“调用协议”选择 Chat Completions 或 Responses。密钥留空会保留原值；勾选清除后保存可删除该密钥。Ollama 不要求密钥。
2. **信息源**：添加、修改、启用或移除 RSS/Atom、GitHub 来源，并调整 Hacker News 参数。阅读方式对应项目的 Profile。
3. **日报规则**：选择语言、抓取时间、条数上限和各 Profile 的评分阈值、主题去重。
4. **高级配置**：编辑完整 JSON，点击“应用到表单”，再点击右上角“保存配置”。界面没有单独表单的原有配置字段会保留。
5. 回到 **概览与日报**，点击“生成今日新闻”。任务在后台执行，可以看日志、停止任务，完成后点击日报，进入排版后的阅读页。

保存写入主目录的 `data/config.json`。模型名称和 API 地址保存到 JSON，密钥保存到 `.env`，且不会由 API 返回已保存的密钥。每次保存会将上一版 JSON 备份为 `data/config.json.web-backup`。不需要重启 Web 服务来应用配置。

只对 `.env` 中对应的密钥变量做替换，不会删除其他变量；变量名需以 `API_KEY` 或 `TOKEN` 结尾。Azure 的 endpoint 和 api_version、额外来源的凭证可以继续用高级 JSON 和本地 `.env` 设置。

多个标签页或直接编辑文件产生冲突时，页面拒绝覆盖并要求重新加载。任务运行时禁止保存，当前 Web 服务一次运行一个任务。不要同时在终端运行另一份采集任务。

日报位于 `data/summaries/`，概览显示最近 100 个文件。点击后进入网页阅读页，支持分类目录、标题跳转、评分、标签、来源链接和可折叠参考链接。原始 Markdown 保留，可在工具栏切换查看或下载。日志只保留本次 Web 服务启动后最近一次任务的末尾 600 行，重启后清空。

## 与原有 CLI 的关系

Web 复用 `python -m src.main`，没有改动原有新闻处理流程。CLI 仍可运行：

```bash
docker compose -f compose.local.yaml run --rm horizon --hours 24
```

Web 服务常驻；新闻生成任务按按钮执行。没有增加定时调度、云端发布或消息发送配置。原有 `config.json` 中如果启用了邮件或 Webhook，完整流水线仍按其配置投递。

## 实现与验证

- 标准库 HTTP 服务，前端使用本地 HTML/CSS/JavaScript，无外部字体或脚本。
- Docker 以非 root 用户运行，配置和日报挂载到主工作区。
- 同源检查、写请求校验、配置校验、并发运行拦截、日志密钥遮蔽、日报路径检查。
- API 与持久化测试见 `tests/test_web_console.py`；真实模型调用需你自行填写 API 后验证。

配置台是单用户、本机工具，不适合作为公网多用户服务。

## OpenAI 兼容协议

“OpenAI 兼容”表示服务类型；具体接口由“调用协议”控制：

| 页面选项 | 配置值 | 请求路径（API 根地址为 `/v1` 时） |
| --- | --- | --- |
| Chat Completions | `chat_completions` | `/v1/chat/completions` |
| Responses | `responses` | `/v1/responses` |

旧配置未设置 `api_protocol` 时继续使用 Chat Completions。Responses 需要你的服务商实际提供该接口；程序不会遇到错误后偷偷切回另一个协议。

```json
{
  "ai": {
    "provider": "openai",
    "api_protocol": "responses",
    "model": "你的模型名称",
    "base_url": "https://你的服务地址/v1",
    "api_key_env": "HORIZON_AI_API_KEY",
    "max_tokens": 4096,
    "languages": ["zh"]
  }
}
```

填写 API 根地址，不要包含 `/responses` 或 `/chat/completions`。Responses 模式使用 包含 system/user 消息的 `input`、`max_output_tokens` 和 `text.format`，聚合 `output_text` 并统计输入/输出 tokens。模型明确拒绝 temperature 时，会在同一协议下去掉该参数重试。当前是非流式文本/JSON 调用；不增加工具调用或会话状态。

参考：[OpenAI Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create)。

## 日报阅读页

从「概览与日报」点击任意日报即可阅读。页面地址包含文件名，例如：

```text
http://127.0.0.1:18764/#reader/horizon-2026-10-16-zh.md
```

- 桌面左侧显示目录，点击标题定位到正文；窄屏目录可折叠。
- 顶部「切换日报」可选择日期和语言；「返回概览」返回列表。
- 「阅读」显示排版后的内容，「Markdown 原文」显示原始内容。
- 「下载 Markdown」下载原始文件，保留原始 UTF-8 字节和换行。
- 来源和参考链接在新标签页打开；参考链接保持原项目的折叠结构。
- 阅读地址可直接收藏或刷新；浏览器前进/后退可返回列表或阅读页。

渲染复用现有 Markdown 和 BeautifulSoup 依赖，支持标题、列表、表格、代码块与 details。服务端仅允许文档排版标签、HTTP(S) 来源链接和经过命名隔离的文内锚点，移除脚本、事件属性、表单和外部媒体请求。既有日报无需重新生成；原文件不会因阅读或下载而被改写。
