import configparser
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

config = configparser.ConfigParser()
config.read(BASE_DIR / "config.ini", encoding="utf-8")

api_key = os.getenv("DEEPSEEK_API_KEY") or config.get("LLM", "api_key", fallback="")
if not api_key or api_key.startswith("sk-your"):
    raise SystemExit(
        "未配置 API Key：请在 .env 中设置 DEEPSEEK_API_KEY，"
        "或在 config.ini 中填写 api_key"
    )

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")

question = input("请输入问题: ")
print("---")
stream = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": question}],
    stream=True,
)
for chunk in stream:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
print()
print("---")
