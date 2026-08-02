"""验证 ASGI app 工具 schema：名称、描述、参数、annotations。

对齐 PDD 6.1 工具表，输出 JSON 供人工核对。
"""

import asyncio
import json

from mem_lake.main import mcp


async def main():
    tools = await mcp.list_tools()
    print(f"已注册 {len(tools)} 个工具\n")
    for t in tools:
        info = {
            "name": t.name,
            "description": (t.description or "").strip().split("\n")[0][:100],
            "annotations": t.annotations.model_dump() if t.annotations else None,
            "parameters": list(t.parameters.get("properties", {}).keys())
            if t.parameters
            else None,
            "required": t.parameters.get("required", []) if t.parameters else None,
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))
        print()


if __name__ == "__main__":
    asyncio.run(main())
