# DAgent

基于 `Practice01/chat.py` 升级而来的本地工具型 Agent，同时支持通过 MCP Client 连接外部 MCP Server。业务逻辑与界面完全分离：
`core` 不依赖任何界面，`tui` 只负责渲染 Textual 界面。

## 目录结构

```text
DAgent/
├── core/                    # 与界面无关的业务层
│   ├── agent.py             # Agent 循环：调用 LLM -> 执行工具 -> 再调用 LLM
│   ├── bootstrap.py         # 依赖组装：配置 -> LLM 客户端 + 工具注册表 -> Agent
│   ├── config.py            # Settings 与 config.ini 读取
│   ├── context.py           # 上下文统计、压缩归档与 recallContext 全文检索
│   ├── errors.py            # 领域异常
│   ├── events.py            # UI 消费的 AgentEvent（文本、工具、完成、失败、上下文用量）
│   ├── llm.py               # OpenAI 兼容客户端，支持 Stream / 非 Stream 与 Token Usage
│   ├── skills.py            # Agent Skills 发现与按需加载
│   ├── mcp_config.py        # 读取当前项目的 .mcp.json
│   ├── mcp_client.py        # MCP 连接、list_tools 与工具调用转发
│   ├── types.py             # 消息、ToolCall、ToolResult 等类型
│   └── tools/
│       ├── registry.py      # 工具注册与安全执行
│       ├── time_tools.py    # 调用系统 date 命令
│       ├── calendar_tools.py# 闰年计算
│       ├── file_tools.py    # 10 个文件/目录工具
│       ├── web_tools.py     # fetch_web：curl 下载 + trafilatura 提取正文
│       ├── search_tools.py  # searchweb：百度千帆搜索 API
│       ├── skill_tools.py   # loadskill：加载技能；createskill：创建技能
│       ├── context_tools.py # recallContext：检索历史上下文归档
│       └── cli_tools.py     # Runcli：执行本地命令行程序
├── tui/                     # Textual 表现层
│   ├── app.py               # 聊天界面、流式渲染、工具状态展示
│   └── styles.tcss          # 界面样式
├── tests/                   # 使用标准库 unittest
├── main.py                  # TUI 组合根 / 启动入口
├── config.example.ini       # 配置模板
├── .mcp.json                # MCP Server 配置（示例）
└── requirements.txt
```

> 运行时配置文件 `config.ini`、`.mcp.json` 和 `.agents/` 都放在**启动 DAgent 时所在的项目目录**，而不是 DAgent 源码目录。

## 架构约束

- `core` 不允许导入 `tui`，也不包含任何 print/input/Textual 代码。
- `tui` 不直接访问 LLM 或工具，只消费 `Agent.run_turn()` 产生的 `AgentEvent`。
- 会话历史由 `Agent` 持有，一个 Agent 实例对应一个会话。
- 工具以 `Tool` 描述（名称、描述、JSON Schema、处理函数）注册到 `ToolRegistry`。
- 新增本地工具只需在 `core/tools` 中注册，不需要修改界面。
- MCP 工具在启动时发现后同样注册为 `Tool`，与本地工具共用同一条 Function Calling 执行链。

### 事件流

```python
for event in agent.run_turn("现在几点了？"):
    match event:
        case AssistantText(text):
            ...       # 增量文本
        case ToolCallStarted(name, arguments):
            ...       # 即将执行工具
        case ToolCallFinished(result):
            ...       # 工具结果
        case ContextUsageUpdated(used_tokens, window_tokens, percent, ...):
            ...       # 每次 LLM 响应后的上下文用量
        case TurnCompleted(content):
            ...       # 本轮结束
        case TurnFailed(message):
            ...       # 本轮失败
```

未来的 Web 或 Desktop 界面只需要：

1. 读取 `Settings`。
2. 调用 `core.bootstrap.create_agent(settings)`。
3. 消费 `Agent.run_turn()` 的事件。

这样 UI 可以替换，Agent、LLM 客户端和工具层不需要改动。

## 安装与运行

先安装依赖：

```powershell
cd D:\学习\wd\LLM\Agent\DAgent
python -m pip install -r requirements.txt
```

然后在**实际项目目录**启动 DAgent（该目录应包含 `.agents/`，可同时放置 `config.ini` 和 `.mcp.json`）：

```powershell
cd D:\学习\office
python D:\学习\wd\LLM\Agent\DAgent\main.py
```

开发时也可以直接在源码目录运行：

```powershell
cd D:\学习\wd\LLM\Agent\DAgent
python main.py
```

此时 `config.ini`、`.agents/`、`.mcp.json` 都从当前工作目录查找。也可以显式指定配置文件：

```powershell
python main.py --config D:\path\to\config.ini
```

TUI 操作：

- `Enter`：发送
- `/compact`：手动压缩当前会话上下文
- `Ctrl+L`：清空会话
- `Ctrl+Q`：退出

界面顶部会持续显示 `上下文：已用 k / 窗口 k · 已占用百分比`。无法从配置获得模型窗口时默认按 `128k` 计算，并标注「默认」。

## 配置

在**当前项目目录**（启动 DAgent 时的工作目录，与 `.agents/` 同级）创建 `config.ini`。可以从源码目录复制模板后修改：

```powershell
Copy-Item D:\学习\wd\LLM\Agent\DAgent\config.example.ini .\config.ini
```

`python main.py` 默认读取 `当前工作目录/config.ini`，不会读取 DAgent 源码目录中的 `config.ini`。

```ini
[llm]
base_url = https://text.pollinations.ai/openai
api_key = local
model = openai
stream = true
timeout = 120
max_tool_rounds = 10
# context_window = 128000

[search]
baidu_qianfan_api_key = bce-v3/ALTAK-...
```

- `stream = true`：逐段显示模型输出，并请求 Provider 在最后一个流式块中返回 Token Usage
- `stream = false`：等完整回复后一次性显示
- `max_tool_rounds`：单轮对话中允许的最大工具调用轮数
- `context_window`：可选，模型上下文窗口大小。支持 `128000`、`128k`、`1m` 等写法；不填时按 `128000` 处理，并在 TUI 中标注「默认」
- `baidu_qianfan_api_key`：百度千帆百度搜索 API Key，供 `searchweb` 工具使用；留空时工具会向 LLM 返回明确的配置错误

## MCP Client

DAgent 支持连接外部 MCP Server（Model Context Protocol），并把其工具注册为普通的 Function Calling 工具，与本地工具统一参与 Agent 循环。

### 配置文件位置

启动时读取**当前工作目录**（即启动 `python main.py` 时所在的目录，不是 `DAgent/` 源码目录）下的 `.mcp.json`。这与 `.agents/skills` 的查找位置一致。调用 `create_agent(settings, project_root=...)` 时则读取该项目根目录下的 `.mcp.json`。

- 文件不存在：MCP 功能静默关闭，本地工具照常使用。
- 文件存在但 JSON/字段有误：跳过有问题的 Server，并把原因加入启动警告，不影响其他 Server 和本地工具。

### 配置格式

```json
{
  "mcpServers": {
    "Bazi-MCP": {
      "type": "streamable_http",
      "url": "https://mcp.api-inference.modelscope.net/4a3222faa31a4e/mcp",
      "timeout": 60
    }
  }
}
```

支持的字段：

| 字段 | 适用传输 | 说明 |
|---|---|---|
| `type` | 全部 | `stdio` 或 `streamable_http`。省略时：有 `command` 推断为 stdio，有 `url` 推断为 streamable_http；`http`、`streamable-http` 是 streamable_http 的别名 |
| `command` | stdio | 要启动的 MCP Server 程序，必填 |
| `args` | stdio | 传给程序的字符串数组 |
| `env` | stdio | 追加到当前进程环境变量之上的键值对象 |
| `cwd` | stdio | 子进程工作目录；相对路径相对项目根目录解析 |
| `url` | streamable_http | MCP 端点，必须是 http/https |
| `headers` | streamable_http | 额外 HTTP 请求头，例如 Authorization |
| `timeout` | 全部 | 连接和单次工具调用超时（秒），默认 30，范围 1-3600 |
| `disabled` | 全部 | 设为 `true` 时跳过该 Server |

`url`、`args`、`env`、`headers` 中的值支持 `${VAR}` 环境变量展开。变量未设置时会保留原样并产生启动警告。

stdio 示例：

```json
{
  "mcpServers": {
    "weather": {
      "type": "stdio",
      "command": "python",
      "args": ["mcp_servers/weather.py"],
      "env": {"WEATHER_API_KEY": "${WEATHER_API_KEY}"}
    }
  }
}
```

### 工具发现、命名与调用

- 启动时按 MCP Python SDK 标准接口建立连接：`initialize()`，再调用 `list_tools()`（自动处理分页），读取每个工具的名称、描述和 `inputSchema`。
- 发现到的工具注册进现有的 `ToolRegistry`，因此 `LLM` 看到的 Schema、Agent 的执行路径、TUI 的工具状态展示都与本地工具完全一致。
- **命名映射**：内部维护 `暴露给 LLM 的名称 -> (MCP Server, 原始工具名)` 的 binding 表。工具名合法且不冲突时直接使用原名；与本地工具重名、多个 Server 之间重名或包含非法字符时，暴露为 `<server>__<tool>`；超过 64 字符会截断并附加哈希，保证符合 Function Calling 的名称限制。
- **调用转发**：LLM 发起调用后，Agent 仍走 `ToolRegistry.execute()`；MCP handler 根据 binding 找到 Server，使用 `ClientSession.call_tool(原始工具名, 参数)` 转发，不需要修改 Agent 循环或界面。
- **结果处理**：拼接所有文本内容；`structuredContent` 以格式化 JSON 追加；图片、音频和二进制资源返回可读摘要；服务器返回 `isError=true` 或调用抛出异常时，结果标记为失败（`ToolResult.ok=False`）并带上错误详情，LLM 可以据此调整参数重试。
- **错误隔离**：单个 Server 配置错误、连接超时、初始化失败或运行中断开，只影响该 Server。错误会写入 `Agent.startup_notes`，TUI 启动时显示为“启动警告”，`main.py` 同时输出到 stderr；其他 MCP Server 以及 `Runcli`、`fetch_web`、`loadskill` 等本地工具继续正常工作。
- MCP 会话运行在独立的后台事件循环线程中并保持长连接；`Agent.close()`（TUI 退出时自动调用）会关闭会话并回收 stdio 子进程。

### 依赖

`requirements.txt` 已包含 `mcp>=1.10,<2`。若环境未安装 MCP SDK，DAgent 仍能启动，MCP 工具会返回明确的安装提示。

## 上下文压缩与历史检索

### 上下文用量监控

- 每次 LLM 返回后，`Agent` 都会检查当前请求的上下文用量，包含 System Prompt、用户消息、对话历史、工具调用消息、工具返回结果以及工具定义 Schema。
- 优先使用 Provider 返回的 `usage.prompt_tokens / completion_tokens / total_tokens`；流式请求会带上 `stream_options.include_usage`。Provider 没有返回 Usage 时，使用本地估算：中文约 1 Token/字，英文约 4 字符/Token。
- 每次更新都会产生 `ContextUsageUpdated` 事件，TUI 顶部据此刷新上下文条。
- 用量达到窗口的 **60%** 时，TUI 显示警告并把提示词输入框切换为 `/compact` 提示；`Agent.run_turn()` 也会拒绝新的普通提示词，直到压缩成功。
- 上下文窗口来自 `[llm] context_window`；未配置时默认 `128k`，TUI 显示「默认」。

### `/compact` 手动压缩

1. 用户输入 `/compact` 后，Agent 进入专门的压缩状态，普通任务和 Function Calling 工具都会被阻止。
2. 在当前项目的 `.agents/context/` 下以原子方式写入完整原始上下文：
   ```text
   <项目根目录>/.agents/context/20261022-153000.jsonl
   ```
   文件名即归档编号；同一秒内重复压缩会自动追加 `-01`、`-02` 后缀。文件按消息顺序逐行保存，一行一个 JSON 对象，保留 `role`、`content`、`tool_calls`、`tool_call_id` 以及工具参数和结果。
3. 只有归档写入成功后，才向 LLM 发起**唯一一次**摘要请求；请求使用专用压缩 Prompt，`tools=[]`，即使模型返回工具调用也不会执行。
4. 摘要成功后，保留原有 System Prompt 和工具定义，把历史消息替换为一条带 `【历史上下文压缩摘要】` 前缀的摘要消息，并在末尾追加归档说明：
   ```text
   > 此前上下文已于 2026-10-22 15:30:00 完成压缩，原始上下文保存在编号为 20261022-153000 的归档文件中。如需查找历史信息，请使用 recallContext 工具。
   ```
5. TUI 会弹出浮动双栏面板：左侧为压缩前原文，右侧为压缩后摘要，两栏均可独立滚动，点击「关闭」按钮返回会话。
6. 压缩成功后解除输入限制；如果归档、LLM 请求或摘要生成失败，原有上下文保持不变，TUI 报告错误，用户可以再次输入 `/compact` 重试。

压缩目标约为 5,000 Tokens；实际摘要长度由模型根据压缩 Prompt 控制。

### `recallContext` 工具

压缩后的摘要可能省略部分历史细节，LLM 可以调用普通 Function Calling 工具 `recallContext` 重新检索原始归档：

| 参数 | 必需 | 说明 |
|---|---|---|
| `text` | 是 | 要查找的关键词或文本内容 |
| `id` | 否 | 指定归档编号，例如 `20261022-153000`；省略时检索 `.agents/context/` 下所有归档 |

检索使用 Python 本地全文关键词匹配，支持大小写不敏感的多关键词搜索，会同时搜索消息正文、工具调用参数和工具返回结果。结果包含归档编号、所在行号、消息角色和相关片段；默认最多返回 10 条、总长度约 6000 字符，避免再次占满上下文。

## 内置工具

| 工具 | 说明 |
|---|---|
| `get_local_time` | 通过 `subprocess` 调用系统 `date` 命令 |
| `is_leap_year` | 内部代码计算闰年 |
| `get_current_directory` | 获取当前工作目录 |
| `list_directory` | 列出目录内容 |
| `get_file_type` | 获取文件类型、扩展名、MIME、是否纯文本 |
| `read_text_file` | 读取纯文本文件 |
| `modify_text_file` | 覆盖纯文本文件内容 |
| `create_file` | 在指定目录创建文件 |
| `create_directory` | 创建目录 |
| `rename_path` | 重命名文件或目录 |
| `delete_directory` | 删除目录及其内容 |
| `delete_file` | 删除文件 |
| `fetch_web` | 用 `curl` 下载网页，由 `trafilatura` 提取正文并转为 Markdown |
| `searchweb` | 调用百度千帆搜索 API，返回搜索摘要、来源标题和 URL |
| `Runcli` | 以参数列表执行本地 CLI，返回 stdout、stderr 和退出码 |
| `loadskill` | 按需加载 Agent Skill 的 `SKILL.md` 全文或技能目录内的附件文件 |
| `createskill` | 根据 `name` 和 `skillContext` 在当前项目的 `.agents/skills/<name>/` 下创建 `SKILL.md`，自动补全 Frontmatter，已存在时拒绝覆盖 |
| `recallContext` | 在 `.agents/context/` 归档中按关键词检索原始上下文，返回归档编号、行号、角色和片段 |
| MCP 工具（动态） | 启动时从当前项目 `.mcp.json` 配置的 MCP Server 发现，名称、描述、参数 Schema 和调用方式与本地工具一致 |

不需要技能目录时，内置工具为 16 个；发现技能后会额外注册 `loadskill` 和 `createskill`，共 18 个。MCP 工具按当前项目配置动态追加。

删除目录会拒绝根目录、当前工作目录及其上级目录。删除操作不可恢复，生产使用时建议在工具层增加交互确认或沙箱目录限制。

### `fetch_web` 说明

- 只接受 `http` / `https` URL；未写协议时会自动补 `https://`。
- 通过 `subprocess.run([...])` 以参数列表调用本地 `curl`，不使用 `shell=True`，并限制超时、重定向次数和响应大小。
- 使用 `trafilatura` 的 Markdown 输出，保留标题、段落、列表、表格和链接，去除导航栏、广告、页脚；相对链接会转换为绝对链接。
- 返回内容包含原始 URL、最终 URL（发生跳转时）、页面标题、HTTP 状态码、内容类型和正文 Markdown。
- `text/plain`、JSON 等非 HTML 内容会原样返回（截断后），因此 IP 查询等纯文本接口也能直接使用。
- 正文默认截断到 12000 字符，可通过 `max_chars` 参数调整（500-50000），避免消耗过多上下文 Token。
- 网络失败、超时、HTTP 4xx/5xx、二进制内容、HTML 解析失败或正文为空时，返回以 `错误：` 开头的说明而不是抛出异常。

`fetch_web` 依赖系统 `curl`（Windows 10/11 自带 `C:\Windows\System32\curl.exe`）和 `trafilatura`（已在 `requirements.txt` 中）。

### `searchweb` 说明

- 调用百度千帆百度搜索接口 `POST https://qianfan.baidubce.com/v2/ai_search/web_search`。
- API Key 从 `config.ini` 的 `[search] baidu_qianfan_api_key` 读取，由 `Settings` 注入工具，**没有硬编码**；也支持填入带 `Bearer ` 前缀的值。
- 默认返回 5 条网页结果（`top_k` 可设为 1-10），每条包含标题、URL、发布时间和摘要；摘要会清理控制字符并截断到 400 字符。
- 缺少 API Key、网络失败、请求超时、HTTP 4xx/5xx、API 返回错误码、响应 JSON 无法解析或格式异常时，都会以 `错误：...` 形式作为工具结果返回给 LLM。
- 工具名为 `searchweb`，系统提示中会引导模型在需要实时信息时调用。

### `Runcli` 说明

- 参数：`command`（程序名或可执行文件路径）、`args`（字符串数组，可省略）。
- 通过 `subprocess.run([command, *args], shell=False, ...)` 执行，**不拼接 Shell 字符串**，因此不会把参数当作管道、重定向或通配符解释。
- 返回内容包含实际命令、退出码、完整 stdout 和 stderr；流为空时明确标注 `（空）`。
- 超时时间为 30 秒；超时会终止进程，并尽量返回已捕获的部分输出。
- stdout 和 stderr 分别截断到 6000 字符，并在标题中标注原始长度和截断情况，避免占用过多上下文。
- 程序不存在、无执行权限、启动失败、参数格式错误时返回 `错误：...`，不会中断 Agent 循环。
- 命令以非 0 退出码结束但 stderr 为空时，会提示模型改用 `-sS` 等会显示错误的参数，或换等价接口重试。
- 标准输入连接到空设备（`DEVNULL`），避免交互式命令一直等待输入。

> 注意：按教学要求，本工具**没有命令白名单、黑名单或审批机制**，命令以运行 Agent 的当前用户权限执行。请只在受控实验环境中使用。

## Agent Skills

DAgent 遵循 Agent Skills 的目录约定，在**程序启动时的工作目录**（不是 Python 安装目录，也不是 `DAgent/` 代码目录）下寻找技能：

```text
<当前工作目录>/
└── .agents/
    └── skills/
        └── <skill-name>/
            ├── SKILL.md          # 必需：YAML Frontmatter 声明 name 和 description
            └── references/...    # 可选：技能引用的附件
```

```markdown
---
name: weekly-report
description: 根据周报模板整理本周工作记录并生成周报。
---

# 周报技能

详细步骤……
```

- **发现与注册**：初始化时只扫描 `.agents/skills/*/SKILL.md` 的 Frontmatter，读取 `name` 和 `description` 两个必需字段；缺少任一字段、目录中没有 `SKILL.md` 或名称重复的技能会被跳过并记录到 `SkillCatalog.warnings`。
- **System Prompt**：发现的技能以 `name: description` 列表追加到系统提示，并告诉模型可以调用 `loadskill` 获取完整说明；**不读取正文**，因此不会提前占用上下文。
- **动态加载**：只有模型调用 `loadskill` 时，`SKILL.md` 正文或附件才进入对话上下文。工作流程、`Runcli`、`file_tools` 等其他工具保持不变。
- **创建技能**：`createskill(name, skillContext)` 在**当前项目**（`SkillCatalog.project_root`）的 `.agents/skills/<name>/` 下创建 `SKILL.md`。
  - `name` 会被规范化为小写英文字母、数字和连字符（例如 `Weekly Report` → `weekly-report`）；包含路径分隔符或无法转换为英文名称时返回错误。
  - `skillContext` 可以是完整的 `SKILL.md`（含 YAML Frontmatter），也可以只是正文。没有 Frontmatter 时，工具会自动生成 `name` 和 `description`，其中 `description` 优先取 Frontmatter 中的值，否则取正文第一段。
  - 如果名称对应的技能目录或技能名已经存在，工具会返回 `错误：...` 并拒绝写入，不会覆盖任何原有文件。
  - 创建成功后，新技能会立即加入当前 `SkillCatalog`，可以直接用 `loadskill` 加载。
  - 如需增加 `references/`、`scripts/` 等目录或辅助文件，模型应在创建 `SKILL.md` 后继续调用 `create_directory`、`create_file` 等文件工具。
- **`loadskill(name, filename=None)`**：
  - 只传 `name`：返回该技能的 `SKILL.md` 全文。
  - 同时传 `filename`：只读取该附件，不重复加载 `SKILL.md`。
  - 支持相对于技能目录的路径、相对于项目根目录的路径和绝对路径；只给文件名时会在技能目录内递归查找，匹配唯一才读取，重名时返回候选路径由模型自行选择。
  - 所有读取都会先解析为真实路径并限制在技能目录内部，符号链接或 `../` 跳出目录的请求会被拒绝。

## Agent 行为

- `core/bootstrap.py` 内置一段系统提示：需要实时或本地信息时优先调用工具，工具失败时换参数或等价接口重试，最后必须基于工具结果直接回答，不编造数据。
- 系统提示还会说明 MCP 工具与本地工具用法相同；MCP 工具描述末尾会标注来源 Server，便于模型区分同名工具。
- 模型返回空回复时自动追加一次纠正提示并重试；仍然为空则结束本轮并报告失败，避免界面无提示。
- 流式解析会合并被网关拆成“无名称片段”的工具调用参数，并清理 `<|channel|>` 之类的通道标记，避免合法工具被误判为未知工具。
- 上下文达到 60% 后，普通提示词会被阻止；`/compact` 是唯一可继续执行的操作。压缩期间 `Agent.compact()` 不接收工具 Schema，也不会执行任何工具调用。
- 压缩失败不会修改内存中的历史；压缩成功后才替换历史并保留 System Prompt 与工具注册表。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用 Fake LLM，不需要网络或 API Key。`tests/test_mcp_client.py` 会启动一个临时的本地 stdio MCP Server，验证连接、`list_tools()`、注册、调用和错误处理；需要安装 `mcp` 依赖（未安装时该集成测试自动跳过）。
