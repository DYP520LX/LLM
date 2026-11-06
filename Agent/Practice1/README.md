# LLM-Course

极简 DeepSeek 命令行聊天程序。

## 文件

- `chat.py` — 单轮问答，流式逐字输出，回答前后用 `---` 分割
- `config.ini` — LLM 配置（已被 `.gitignore` 忽略）
- `config.ini.example` — 配置模板
- `.env` — API Key 环境变量（已被 `.gitignore` 忽略）

## 使用

1. 安装依赖：

```bash
py -m pip install -r requirements.txt
```

2. 配置 API Key（二选一）：

- 在 `.env` 中设置：

```env
DEEPSEEK_API_KEY=sk-your-deepseek-key-here
```

- 或复制 `config.ini.example` 为 `config.ini` 并填写 `api_key`。

3. 运行：

```bash
py chat.py
```

输入问题回车，回答逐字打印，程序输出结尾 `---` 后自动退出。

> 注意：API Key 只保存在本地 `.env` / `config.ini`，不要提交到仓库。
