import re

# 定义emoji替换映射
emoji_map = {
    "🔄": "[循环]",
    "🌈": "[彩虹]",
    "🎉": "[成功]",
    "❌": "[错误]",
    "⚠️": "[警告]",
    "🚫": "[禁止]",
    "❕": "[提示]",
    "🚀": "[启动]",
    "✅": "[OK]",
    "📁": "[文件]",
    "📐": "[压缩]",
    "🔍": "[搜索]",
    "🔴": "[红色]",
}

# 读取文件
import os; project_dir = os.path.dirname(os.path.abspath(__file__))
file_path = os.path.join(project_dir, "mango", "set.py")
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 替换所有emoji
for emoji, replacement in emoji_map.items():
    content = content.replace(emoji, replacement)

# 写回文件
with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Emoji替换完成！")
