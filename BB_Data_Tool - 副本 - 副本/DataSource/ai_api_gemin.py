import openai
import json
import os

def generate_default_config(config_path="gemini_config.json"):
    """生成默认配置文件"""
    default_config = {
        "api_key": "",
        "base_url": "",
        "default_headers": {
            "x-foo": "true"
        },
        "model": "gemini-2.0-flash",
        "prompts": {
            "analyze_site": "Please analyze the website by combining its title and description to comprehensively extract information on its main product categories, outputting only the core conclusions without any introductory remarks or transitional phrases. The response should be in Chinese.",
            "summary_tags": "Please visit the website and extract its primary product categories. Consolidate related detailed subcategories into broader, high-level labels. For example, categories such as suspension systems, body protection, lighting, accessories, and tires should be grouped as 'Auto Repair Accessories'. Output only the generalized tags (with a maximum of 5) and avoid any irrelevant information. The final result must strictly adhere to the following JSON format with all text in Chinese:\n{\n    \"产品类目\": [\"标签1\", \"标签2\", \"标签3\", ...]\n}",
            "reserve": "预留提示词字段",
            "reserve1": "预留提示词字段",
            "reserve2": "预留提示词字段",
            "reserve3": "预留提示词字段"
        }
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_config, f, ensure_ascii=False, indent=4)
    print(f"谷歌Gemini-API默认配置文件已生成：{config_path}")

def load_config(config_path="gemini_config.json"):
    """加载 JSON 配置文件，如果不存在则生成一份默认配置文件"""
    if not os.path.exists(config_path):
        print(f"配置文件不存在：{config_path}，正在生成默认配置文件...")
        generate_default_config(config_path)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config

def configure_gemini(config):
    """根据配置文件设置 openai 参数"""
    openai.api_key = config.get("api_key")
    openai.base_url = config.get("base_url")
    openai.default_headers = config.get("default_headers", {})

def build_input(content, prompt):
    """构造输入内容"""
    return f"{content}\n\n{prompt}"

def gemini_call(content, prompt_key="analyze_site", config_path="gemini_config.json"):
    """
    调用 Gemini 接口的封装函数（非流式调用），通过配置文件中的提示词集合，根据传入的 prompt_key
    选择相应的提示词。调用时只需传入 content 和提示词字段名。
    
    参数:
        content: 要分析的内容（例如 URL）
        prompt_key: 提示词在配置文件中的键，默认使用 "analyze_site"
        config_path: 配置文件路径，默认 "gemini_config.json"
    
    返回:
        模型返回的回答文本
    """
    # 加载配置并设置 openai
    config = load_config(config_path)
    configure_gemini(config)
    
    # 从配置文件中获取模型参数
    model = config.get("model", "gemini-2.0-flash")
    
    # 从 prompts 字段中获取对应的提示词
    prompts = config.get("prompts", {})
    prompt = prompts.get(prompt_key)
    if not prompt:
        raise ValueError(f"配置文件中不存在提示词键: {prompt_key}")
    
    full_input = build_input(content, prompt)
    
    completion = openai.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": full_input}],
    )
    
    return completion.choices[0].message.content

if __name__ == "__main__":
    # 示例调用，只需输入 content 和提示词字段名
    content = "https://www.royalretros.com"
    result = gemini_call(content, prompt_key="analyze_site")
    print(result)
