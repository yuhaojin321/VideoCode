import inspect
import json
import os
import re
from string import Template
from typing import List, Callable, Tuple

import click
from dotenv import load_dotenv
from openai import OpenAI
import platform

from prompt_template import react_system_prompt_template


class ReActAgent:
    def __init__(self, tools: List[Callable], model: str, project_directory: str):
        self.tools = { func.__name__: func for func in tools }
        self.model = model
        self.project_directory = project_directory
        self.client = OpenAI(
            base_url="https://api.deepseek.com",
            api_key=ReActAgent.get_api_key(),
        )

    def run(self, user_input: str):
        messages = [
            {"role": "system", "content": self.render_system_prompt(react_system_prompt_template)},
            {"role": "user", "content": f"<question>{user_input}</question>"}
        ]

        while True:

            # 请求模型
            content = self.call_model(messages)

            # 检测 Thought
            thought_match = re.search(r"<thought>(.*?)</thought>", content, re.DOTALL)
            if thought_match:
                thought = thought_match.group(1)
                print(f"\n\n💭 Thought: {thought}")

            # 检测模型是否输出 Final Answer，如果是的话，直接返回
            if "<final_answer>" in content:
                final_answer = re.search(r"<final_answer>(.*?)</final_answer>", content, re.DOTALL)
                if final_answer:
                    return final_answer.group(1).strip()
                # 模型输出缺少闭合标签（常因被 max_tokens 截断）：取 <final_answer> 之后的内容
                return content.split("<final_answer>", 1)[1].strip()

            # 检测 Action
            action_match = re.search(r"<action>(.*?)</action>", content, re.DOTALL)
            if not action_match:
                print(f"\n\n⚠️ 模型本轮原始输出：\n{content}")
                raise RuntimeError("模型未输出 <action>")
            action = action_match.group(1)
            tool_name, kwargs = self.parse_action(action)

            print(f"\n\n🔧 Action: {tool_name}({kwargs})")
            # 只有终端命令才需要询问用户，其他的工具直接执行
            should_continue = input(f"\n\n是否继续？（Y/N）") if tool_name == "run_terminal_command" else "y"
            if should_continue.lower() != 'y':
                print("\n\n操作已取消。")
                return "操作被用户取消"

            try:
                observation = self.tools[tool_name](**kwargs)
            except Exception as e:
                observation = f"工具执行错误：{str(e)}"
            print(f"\n\n🔍 Observation：{observation}")
            obs_msg = f"<observation>{observation}</observation>"
            messages.append({"role": "user", "content": obs_msg})


    def get_tool_list(self) -> str:
        """生成工具列表字符串，包含函数签名和简要说明"""
        tool_descriptions = []
        for func in self.tools.values():
            name = func.__name__
            signature = str(inspect.signature(func))
            doc = inspect.getdoc(func)
            tool_descriptions.append(f"- {name}{signature}: {doc}")
        return "\n".join(tool_descriptions)

    def render_system_prompt(self, system_prompt_template: str) -> str:
        """渲染系统提示模板，替换变量"""
        tool_list = self.get_tool_list()
        file_list = ", ".join(
            os.path.abspath(os.path.join(self.project_directory, f))
            for f in os.listdir(self.project_directory)
        )
        return Template(system_prompt_template).substitute(
            operating_system=self.get_operating_system_name(),
            tool_list=tool_list,
            file_list=file_list
        )

    @staticmethod
    def get_api_key() -> str:
        """Load the API key from an environment variable."""
        load_dotenv()
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("未找到 DEEPSEEK_API_KEY 环境变量，请在 .env 文件中设置。")
        return api_key

    def call_model(self, messages):
        print("\n\n正在请求模型，请稍等...")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=8192,
        )
        content = response.choices[0].message.content
        messages.append({"role": "assistant", "content": content})
        return content

    def parse_action(self, action_str: str) -> Tuple[str, dict]:
        """将 <action> 内的 JSON 解析为 (工具名, 参数字典)。

        <action> 的内容形如：
            {"tool": "write_to_file", "args": {"file_path": "...", "content": "..."}}
        使用标准 JSON 解析器，能正确处理多行文本、引号、逗号等特殊字符。
        """
        action_str = action_str.strip()
        # 去掉模型可能多包裹的 Markdown 代码块标记（如 ```json ... ```）
        action_str = re.sub(r"^```(?:json)?\s*|\s*```$", "", action_str, flags=re.DOTALL).strip()
        try:
            data = json.loads(action_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"<action> 中的 JSON 解析失败：{e}\n原始内容：\n{action_str}")
        return data["tool"], data["args"]

    def get_operating_system_name(self):
        os_map = {
            "Darwin": "macOS",
            "Windows": "Windows",
            "Linux": "Linux"
        }

        return os_map.get(platform.system(), "Unknown")


def read_file(file_path):
    """用于读取文件内容"""
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()

def write_to_file(file_path, content):
    """将指定内容写入指定文件"""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content.replace("\\n", "\n"))
    return "写入成功"

def run_terminal_command(command):
    """用于执行终端命令"""
    import subprocess
    run_result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return "执行成功" if run_result.returncode == 0 else run_result.stderr


def fix_input_encoding(s: str) -> str:
    """修复终端输入编码问题。

    当终端以非 UTF-8 编码（如 Windows 中文系统的 GBK）发送中文时，Python 的
    input() 会把这些字节按 utf-8 + surrogateescape 误读成「代理字符」
    （U+DC80~U+DCFF），后续 JSON 序列化时报 "surrogates not allowed"。
    这里把误读的原始字节还原，再用常见编码重新解码出正确文本。
    """
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        return s
    try:
        raw = s.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        # 无法还原原始字节时，退化为替换非法字符
        return s.encode("utf-8", "replace").decode("utf-8")
    for enc in ("gb18030", "gbk", "big5", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


@click.command()
@click.argument('project_directory',
                type=click.Path(exists=True, file_okay=False, dir_okay=True))
def main(project_directory):
    project_dir = os.path.abspath(project_directory)

    tools = [read_file, write_to_file, run_terminal_command]
    agent = ReActAgent(tools=tools, model="deepseek-chat", project_directory=project_dir)

    task = fix_input_encoding(input("请输入任务："))

    final_answer = agent.run(task)

    print(f"\n\n✅ Final Answer：{final_answer}")

if __name__ == "__main__":
    main()
