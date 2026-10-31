# CodeBuddy iOA 官方 SDK → Magpie / Codex：独立本地桥接（实验版）

> 这是 `ReyJerry/codebuddy2api` 中**独立启动**的辅助服务，不更改原有
> `converter.py`、WebUI、`.info`、Credits 账本或原有 `/v1/*` 路由。
> 不提供无限额度、伪造企业资源或替换计费身份的功能。

## 设计范围及现状

- **已实现**：官方 SDK iOA API Key 认证；稳定隔离 `cwd`；`tools=[]`；
  `/v1/models` 模型发现（从 SDK CLI 的 `--help` 解析或用户指定白名单）；
  `/v1/responses` 文本 JSON / SSE；SDK `StreamEvent` 增量文本；
  `Usage` dataclass/dict 计数；缓存读写 Token 计数；本机密钥鉴权；
  错误脱敏；增量会话的受控复用和轮换；仅本机监听。
- **实验性，默认关闭**：把 Responses 的 `function` / `custom` 工具声明写入
  SDK 提示词，要求模型返回严格 JSON `tool_call`，将其映射为 Responses
  `function_call` / `custom_tool_call`，由 **Codex**（不是 SDK）执行工具，
  下一次 Responses 请求把带 `call_id` 的工具结果送回模型。
  这是**提示词协议**，不是原生 Function Calling；在真实 Codex 上需要逐项验证，
  不保证所有模型、工具 Schema、并行调用及长时间任务都能稳定工作。
- **未实现**：图像/音频输入、托管工具、`previous_response_id` 服务端检索、
  强一致 Session 恢复、WebSocket Responses、`tool_search`、完整的
  MCP 挂起/恢复回调。遇到不支持的能力应明确报错，而非偷偷丢弃。

用户实测 `glm-5.2-ioa` 的 SDK 调用已在腾讯 AI TOKEN 平台对应扣费。
这不自动证明其他模型同池。SDK 的 `total_cost_usd=0` **不是人民币免费证明**；
本桥接只显示原始 Token 统计，不计算未经核实的成本。

## 原生 SDK vs 直接 stream-json 的选择

| 项目 | 官方 Python SDK | 直接控制 headless stream-json |
|---|---|---|
| iOA Key、模型选择、消息解码 | SDK 已封装 | 需要自己维护 |
| `tools=[]`、稳定 cwd | 支持 | 支持 |
| 增量流 | `include_partial_messages` / `StreamEvent` | 原生 partial 流 |
| Session / 中断 | SDK 支持 | 需管理进程与管道 |
| Codex 工具闭环 | 需要另外适配 | **同样需要**另外适配 |
| 本次选择 | **首版使用** | 可作为后续性能/工具路径研究 |

`tools=[]` 使 CodeBuddy 内置工具定义不进入 Prompt。使用 `disallowed_tools`
并不能实现相同的上下文节省。SDK 不会通过此桥接调用 Shell/Read/Write。
SDK 仍会注入一部分 Agent 系统上下文，无法把它当作零开销的原始模型 API。

## 在 macOS 上安装（无需 Docker）

从你的个人仓库获取此分支，进入 `codebuddy2api` 项目根目录：

```bash
uv sync --locked --no-build --python 3.12
# SDK 使用独立临时依赖层，不修改原项目的 uv.lock / requirements.txt
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

启动之前请在**同一终端**设置以下环境变量，不要把真实密钥保存到 Git：

```bash
export CODEBUDDY_INTERNET_ENVIRONMENT=ioa
export CODEBUDDY_API_KEY='通过官方 iOA 入口合法申请的 API Key'
export IOA_BRIDGE_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"

# 可选：若 CLI --help 未提供可解析的模型列表，仅添加你本人已确认授权的模型 ID
export IOA_BRIDGE_MODELS='APPROVED_MODEL_ID'

# 工具桥接仅供隔离的无敏感数据测试，默认关闭
# export IOA_BRIDGE_EXPERIMENTAL_TOOLS=1

uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

默认只监听 `127.0.0.1:8791`，与原网关的 `127.0.0.1:8787` **互不冲突**。
不读取原网关的 `.info`、Cookie、Token、余额或 SQLite 文件；
启动时会拒绝同时设置 `CODEBUDDY_AUTH_TOKEN`，防止它覆盖 API Key。

### 试运行

另开一个终端，把刚才生成的本地 `IOA_BRIDGE_KEY` 安全地传入：

```bash
export IOA_BRIDGE_KEY='本地桥接密钥（不是 iOA Key）'

curl -sS -H "Authorization: Bearer $IOA_BRIDGE_KEY" \
  http://127.0.0.1:8791/v1/models

curl -sS http://127.0.0.1:8791/v1/responses \
  -H "Authorization: Bearer $IOA_BRIDGE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"APPROVED_MODEL_ID","input":"Reply with OK","stream":false}'
```

`/health` 只检查本地服务是否运行，不保证模型可用。
查询 `/models` 只证明模型 ID 在本地候选列表中，**不等于该账号被授权调用**。
如果 CLI help 格式不同而返回空模型目录，通过 `IOA_BRIDGE_MODELS` 指定
自己已确认可用的 ID；这不更改服务端权限。

## Magpie 接入

在 Magpie 添加 **Custom / OpenAI Responses Provider**：

- Base URL：`http://127.0.0.1:8791/v1`
- API Key：`IOA_BRIDGE_KEY` 对应的**本地**密钥
- Model ID：`GET /v1/models` 列表中的实际模型 ID

随后在 Magpie 的 Codex Agent 中选择该 Provider。

Codex CLI 也可在 `~/.codex/config.toml` 直接配置：

```toml
[model_providers.ioa_sdk_local]
name = "iOA Agent SDK (local experimental)"
base_url = "http://127.0.0.1:8791/v1"
wire_api = "responses"
env_key = "IOA_BRIDGE_KEY"
supports_websockets = false
requires_openai_auth = false

[profiles.ioa_sdk_text]
model = "APPROVED_MODEL_ID"
model_provider = "ioa_sdk_local"
```

默认禁用工具调用。**如果 Codex 发送工具，服务会返回 HTTP 422
`tools_not_enabled`**，这是刻意的安全边界而非临时故障。
验证工具模式前，显式设置 `IOA_BRIDGE_EXPERIMENTAL_TOOLS=1` 并重启。
即使启用，仍应先通过虚构代码和非敏感仓库进行小型 Shell/Patch 用例验证。
本桥接**永不自行执行** Codex 的命令或补丁，Codex 自己负责审批与执行。

## Session 与成本

Codex 通常每次传递完整对话历史（常见 `store=false`），因此桥接默认
`persist_session=False`。**不能**在完整历史模式下再叠加 SDK 的累积会话，
否则可能重复发送上下文并不断变长。

只有你能保证每次请求仅含**一个增量用户消息**时，才可加请求头
`X-IOA-Delta-Session: conversation_001`。桥接会创建 SDK Session 并在
任一条件满足时换新：4 次请求、报告的 input token 达到 24K、空闲超过 15 分钟、
或切换模型。并发使用同一 Session 会返回 HTTP 409。
重启桥接后本地映射消失，会创建新的 Session；这不会自动恢复先前上下文。

缓存由官方模型服务决定，`cache_read_input_tokens` 等为 SDK 原始记录。
参考测试数据不代表定价倍率：`cache_read` 0.1x / `cache_creation` 1.25x
仅为假设，不能以此推算真实 AI TOKEN 金额。为避免重复扣费，桥接不自动重试模型请求。

## 后续要做的真·原生工具闭环

1. 用 SDK `create_sdk_mcp_server` 为每个 Codex 工具注册对应的 MCP 包装，**只发起请求，不执行工具**。
2. 后端建立保活的 SDK Client、`tool_use_id ↔ call_id` 和 `asyncio.Future`；
   当模型要求工具时，将调用转换成 `response.function_call` / `custom_tool_call`
   交还 Codex，保持 SDK 工具处理器等待。
3. Codex 回传 `function_call_output` 后，桥接验证 Session、工具名、
   调用 ID、时限、内容大小，再解除 Future，继续 SDK 消息流。
4. 解决跨 HTTP 请求的生命周期：并发、取消、超时、断线、进程重启、
   只执行一次以及**绝不重放已产生费用或有副作用的工具**。
5. 用真实 Codex 的 Shell、Patch、自定义 MCP 工具、并行调用和取消场景验证。

这是一个单独的重大改造，不能把当前的提示词 JSON 工具适配宣称为已完成。

## 离线测试

```bash
uv run --locked python -B -m pytest -q tests/test_ioa_bridge.py
```

测试仅使用合成模型响应，不调用内部服务、不消耗 AI TOKEN。

## 安全注意

- **只运行在授权的公司环境中**；外部第三方客户端是否允许处理公司代码和
  消耗内部额度，要遵循适用的企业规定。
- 不要提交 `.env`、真实 Key、认证文件、内部端点或含敏感信息的日志。
- 不要开放公网访问；独立服务强制绑定 loopback。
- 本项目不读取或修改原网关 Credits，也不伪造员工账单和内部身份。
- `IOA_BRIDGE_MODELS` 是你的本地模型白名单，不是访问权限证明。
