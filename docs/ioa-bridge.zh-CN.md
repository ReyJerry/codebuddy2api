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
- **未实现**：音频/文件输入、远程图片 URL 自动获取、托管工具、`previous_response_id` 服务端检索、
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

## 大上下文支持：1M Token 模型与本地 HTTP 上限

为允许恢复长 Codex Session，桥接器现在默认接收 **128 MiB HTTP 请求体**，且转换后
Prompt 的本地上限为 **16,000,000 字符**（旧版分别约 2 MB 和 250,000 字符）。
这两个阈值只是桥接器的容量保护，**不是模型的 1M Token 上下文承诺**。
Token 与字符、UTF-8/JSON 字节并不相同；SDK 会另外注入系统上下文，实际窗口由
所用模型和服务端决定。桥接器绝不因为大小问题静默裁剪历史。

如需更大，可以在**启动桥接的那个终端**设置以下环境变量并重启（合法范围：
HTTP 1–256 MiB；Prompt 100,000–32,000,000 字符）：

```bash
export IOA_BRIDGE_MAX_BODY_MB=256
export IOA_BRIDGE_MAX_PROMPT_CHARS=32000000
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

默认值可以不设置；较大的上限只改变本机接收能力，不会绕过 Magpie 自身的限制或
模型实际 1M Token 窗口。如果 Magpie 或 SDK 上游另有上限，仍可能收到不同的错误。
对于极长历史先估算实际 Token、检查 AI TOKEN 扣费，避免重复发送大 Prompt。
内部使用 `request.stream()` 逐块接收并在超出阈值时立即拒绝，避免无限制缓冲。


### Codex 工具声明大小（不同于历史请求大小）

Codex App 与 MCP 可能提供超过 64 个工具。旧版的 `MAX_TOOLS=64` 把工具数量超限
错误地混在“工具列表格式错误或过长”的 422 中。**现在不设固定工具数量上限**；
对合法的 function/custom 工具逐个验证，并按序列化字符预算累计检查，
不默默移除工具、说明文字或参数 Schema。格式不正确会返回具体的 422 错误代码。

工具声明默认最大 **8,000,000 字符**（原来 512,000），配置范围为
1,000–32,000,000。若模型和机器资源允许，可在启动终端中进一步调整：

```bash
export IOA_BRIDGE_MAX_TOOL_SPEC_CHARS=16000000
export IOA_BRIDGE_EXPERIMENTAL_TOOLS=1
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

HTTP 请求体仍默认 128 MiB、最大可配置 256 MiB；总 Prompt 默认 16,000,000
字符、最大可配置 32,000,000 字符。两项额度各自独立，工具 Schema 也计入总 Prompt。
这些是避免进程耗尽内存的安全边界，而**不是无限长度**、也不保证上游模型接受。
超限明确返回 `tool_spec_too_large`（413）或 `input_too_large`（413）。
`/compact` 不会减少工具 Schema 的大小。超大工具列表还会增加 SDK Prompt Token、
延迟和实际 AI TOKEN 扣费；只在非敏感临时工程中测试。

默认仍不允许工具执行；开启实验模式需要 `IOA_BRIDGE_EXPERIMENTAL_TOOLS=1`。
工具模式采用 JSON Prompt 适配，**不是原生 Function Calling**。

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

### Codex App 默认模式与 SDK 规划模式

SDK `permission_mode` 使用 `default`，避免将 SDK 的“仅规划、禁止修改”限制注入模型对 Codex 工具的决策。桥接仍保持 `tools=[]`、`allowed_tools=[]` 以及一律拒绝 SDK 本地工具执行的回调；**实际文件操作和审批仍由 Codex App 负责**。

在 Codex App Default 模式中，`request_user_input` 工具可能不可用；如需确认权限，请让模型通过普通文本提问。测试编辑操作时，只授权非敏感临时工作区，不要直接在真实项目上试验。


## Codex 新式工具声明（2026-11，实验兼容）

Codex 现在可能在 Responses `tools` 中同时提交 `function`、`custom`、
`namespace`、`tool_search`（`execution=client`）及 `web_search`。
新版桥接区分它们，不再把 `tools[4]` 一概视为未知工具：

- **function/custom**：维持已验证的 Prompt JSON → Responses 工具调用协议。
- **namespace**：保留工具所属命名空间，发出带 `namespace` 的 `function_call`/
  `custom_tool_call`，支持同名但不同命名空间的工具。
- **tool_search（client）**：产生真正类型为 `tool_search_call`、`execution=client`、
  `arguments` 为对象的 Responses 项目，交给 Codex 客户端执行工具发现。
  `tool_search_output` 会在下一轮进入模型上下文。`defer_loading` 的工具在被发现之前
  不允许直接调用（仍属实验性 Prompt 适配；不是 OpenAI 原生工具搜索后端）。
- **web_search/web_search_preview**：这是由服务商执行的托管联网搜索，
  目前 CodeBuddy SDK 桥接**没有实现联网搜索**。为了不阻断普通编码请求，
  它们作为明确的**不可用能力**记录在 `metadata.ioa_unavailable_hosted_tools`，
  并明确告知模型不得编造联网结果或引用。若 `tool_choice` 强制搜索，会返回
  `422 hosted_tool_unavailable`。这不是支持联网搜索；需要实际联网搜索时应使用
  经授权的其他工具或支持该托管功能的后端。
- **其他托管或新工具类型**：显式返回 `422 unsupported_tool_type`，消息包含工具
  位置和类型，不会假装执行、丢弃或任意转成 Shell。

`tools[4]` 只是 JSON 数组的第 5 个工具位置，**不等于某一种固定类型**。
如仍发生类型问题，先记录不含密钥/参数的工具 `type`，再决定是否能安全兼容。
上述修改不能保证所有新版本 Codex App/Magpie 的协议完全一致；在用于实际项目之前，
应测试 `tool_search_call`、命名空间调用、工具返回和 Shell/Patch 全闭环。


## Codex App 图片输入（`input_image`）

Bridge 现已支持 **Responses `input_image` 的 Base64 `data:image/...;base64,...`**，
通过官方 SDK `query(prompt=AsyncIterable[dict])` 发送为真正的 `image` 内容块，
与原始文字保持相同顺序；**不会把图片当作文本描述，也不会静默丢弃**。
支持 PNG、JPEG、WebP、GIF，单张最多 12 MiB，单次累计最多 32 MiB、
最多 128 张（主要为了防止长期历史或异常输入耗尽本机内存）。
这些限制属于本地保护，模型本身的图片/视觉能力和上下文窗口可能更小。

示例（图片内容只在本机演示，真实截图请确认不含机密）：

```json
{"model":"gpt-6-astra","input":[{"role":"user","content":[
  {"type":"input_text","text":"Describe this image"},
  {"type":"input_image","image_url":"data:image/png;base64,<real-base64>"}
]}],"stream":false}
```

- 仅支持安全的内嵌 Base64 data URL。`https://`、`file://` 或 `file_id`
  会明确返回 `422 unsupported_image_reference`，**不会主动抓取外部 URL，避免 SSRF
  或读取不该读取的本地文件**。
- 损坏 Base64、不匹配的图片签名、超出体积上限会在调用收费 SDK 前报错。
- 本桥接仍然把 Codex 的 Shell/Patch 工具交还 Codex 执行。是否真的看见像素，
  取决于当前 iOA 模型的视觉支持；尤其当 SDK 把图片转成 ViewImage 路径提示，
  `tools=[]` 模式无法自行执行 ViewImage，不能误认为模型一定成功识图。
- 已完成不扣费的离线单元测试，**真实 iOA 多模态请求仍需用户在获授权环境下
  自行确认**。升级代码后必须重启 8791 服务。


## 1M 上下文设置：CodeBuddy 模型档位与自动压缩预算的区别

本桥接**默认配置 `IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000`**，由
`CodeBuddyAgentOptions.env["CODEBUDDY_AUTO_COMPACT_WINDOW"]` 传给官方 CLI。
该值是**自动压缩阈值的基准**，不是实际模型上下文窗口的强制开关，也不代表
`gpt-6-astra` 的 iOA API 已按 1M Token 接受请求。CodeBuddy CLI 会将此值
与模型的有效上下文预算取较小值；本桥接仍将完整的 Codex 历史交给 SDK，
不会因为该设置擅自截断。

```bash
# 可选：设置自动压缩预算（默认已经是 1M）
export IOA_BRIDGE_AUTOCOMPACT_TOKENS=1000000
uv run --locked --with codebuddy-agent-sdk python -m ioa_bridge
```

支持设置 100000–1000000 的整数；需重启 8791 才生效。
如使用较小窗口的模型，可设置 `200000`；未明确验证模型支持 1M 时，
不应仅因本地配置就声称已获得 1M 上下文。

**真正的 200K / 1M 模型档位**：CodeBuddy 官方目前通过 ACP 的
`getConfigOptions` / `setSessionConfigOption('context_window', value)` 提供，
并对模型声明的 `supportedLengths` 做校验。当前 Python SDK 的
`CodeBuddyAgentOptions` 没有公开 `context_window` 参数，桥接使用的是
Python SDK `query()` 而非 ACP，因此**尚不能直接切换或验证这个真实档位**。
不要把 CLI 的 `--autocompact 1m` 等同于选中 1M 档。

Codex App / CLI 还有独立的本地模型上下文设定：
`~/.codex/config.toml` 中的 `model_context_window = 1000000`。
**只有确认当前 iOA 模型支持 1M 时**才应设置它，因为它只改变 Codex 的
窗口估计和压缩策略，不会把服务器模型从 200K 扩容到 1M。
不要自动改写或覆盖用户现有 Magpie/Codex 配置。

参考：[官方 SDK Python API](https://www.codebuddy.ai/docs/cli/sdk-python)、
[CodeBuddy ACP 档位](https://www.codebuddy.cn/docs/cli/acp)、
[CodeBuddy 自动压缩设置](https://www.codebuddy.ai/docs/cli/settings)。

## Codex App 新版 Responses 消息兼容（2026-11-01）

`agent_message` 是 Codex MultiAgentV2 的实际子智能体委派信息，不是普通聊天字段：
桥接器会将**明文**任务/回复按原顺序映射成 SDK 的用户消息，并保留 `author/recipient`。
同时兼容 `agent_reasoning`、Shell/Patch/MCP 等工具历史、`context_compaction`
的明文摘要和常见控制事件；未来的纯文本历史消息也可以保留原类型标签进行兼容转换。

**不是所有内容都能无损迁移**：OpenAI/Codex 专有的 `encrypted_content`、
只有远端 ID 的 `item_reference`、无明文摘要的加密压缩状态仍无法通过本地
CodeBuddy SDK 解码。遇到这些情况会给出明确错误，且不会收费调用上游。
模型私有 reasoning 不转发，数量通过 `metadata.ioa_compat_omitted_private_reasoning` 报告。
其他托管工具声明只作「不可用」标记，不能冒充真实搜索或文件操作结果。

详细项目类型、回归测试及兼容边界：
[Responses 兼容矩阵](ioa-responses-compatibility.md)。

**无需消耗 AI TOKEN 的兼容性检查：**

```bash
curl -sS http://127.0.0.1:8791/v1/compatibility \
  -H "Authorization: Bearer $IOA_BRIDGE_KEY" | python3 -m json.tool
```

该只读接口会显示当前适配器版本、支持的输入项目类别、仍不支持的加密状态与实际本地容量限制。
`verified_upstream_context_tokens: null` 表明 **尚未验证 iOA 上游真正的 1M 上下文档位**。


### 工具搜索提示词与 SSE 错误（2026-11-01 v3）

如果出现 `未声明客户端 tool_search，禁止请求工具搜索` 或 Magpie 的
`stream disconnected before completion`，这不是请求体或 1M 上下文限制。
旧版会无条件向模型提供 `tool_search_call` 的 JSON 示例，即使 Codex
本轮只声明了 `exec_command`、`apply_patch` 等工具，导致模型在推理后
提出未经授权的工具搜索调用。

新版只有在 Codex 显式声明 `tool_search` 且 `execution=client` 时才提供
工具发现指令。未声明时只能选择本轮已声明的工具，或正常回答。
若 Astra 仍生成未授权搜索，`tool_choice=auto` 下不再自动重试、不会伪造
工具结果，而是返回清晰的说明并设置
`metadata.ioa_tool_protocol_notice=unadvertised_tool_search`。
**该说明表示本轮没有执行工具，不表示编码任务已完成。**
强制工具调用时仍拒绝未授权的请求。更新代码后需重启 8791 服务。
