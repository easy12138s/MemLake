#!/usr/bin/env python
"""MemLake 端到端 MCP 协议测试脚本。

通过 fastmcp.Client 连接运行中的 MemLake MCP 服务器，以三角色（admin/pm/dev）
真实协作场景为主线，覆盖 19 个工具的完整 MCP 协议链路（HTTP → 中间件 → 工具 → 响应）。

用法:
    python scripts/e2e_mcp_test.py --admin-key <admin_access_key> [--url http://localhost:8000/mcp]

前置条件:
    1. 三容器已启动（docker compose up --build -d）
    2. 已通过 memlake-bootstrap-admin 创建首个 admin Access Key
    3. 本地 Python 环境已安装 fastmcp（pip install fastmcp）
"""

import argparse
import asyncio
import json
import sys
import traceback
import uuid

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

# ========== 配置 ==========

DEFAULT_URL = "http://localhost:8000/mcp"


# ========== 辅助函数 ==========


def make_client(url: str, key: str | None = None) -> Client:
    """创建 MCP 客户端，可选带 X-MCP-Key header。"""
    headers: dict[str, str] = {}
    if key:
        headers["X-MCP-Key"] = key
    transport = StreamableHttpTransport(url=url, headers=headers or None)
    return Client(transport)


async def call_tool(client: Client, name: str, arguments: dict) -> dict:
    """调用 MCP 工具，返回解析后的 dict。失败时抛异常。"""
    result = await client.call_tool(name, arguments)

    # 检查是否为错误结果
    is_error = getattr(result, "is_error", False)
    if is_error:
        error_msg = ""
        content = getattr(result, "content", [])
        for item in content:
            if hasattr(item, "text"):
                error_msg = item.text
                break
        raise RuntimeError(f"工具返回错误: {error_msg or '未知错误'}")

    # 优先使用 structured_content
    sc = getattr(result, "structured_content", None)
    if sc:
        if isinstance(sc, dict):
            return sc
        return {"data": sc}

    # 回退到 text content 解析
    content = getattr(result, "content", [])
    for item in content:
        if hasattr(item, "text"):
            try:
                return json.loads(item.text)
            except (json.JSONDecodeError, TypeError):
                return {"text": item.text}

    return {}


async def call_tool_safe(
    client: Client, name: str, arguments: dict
) -> tuple[dict | None, str | None]:
    """调用工具，返回 (result, error)。不抛异常。"""
    try:
        result = await call_tool(client, name, arguments)
        return result, None
    except Exception as e:
        return None, str(e)


# ========== ScenarioResult ==========


class ScenarioResult:
    """记录单个场景的测试结果。"""

    def __init__(self, name: str):
        self.name = name
        self.steps: list[dict] = []
        self.passed = True

    def ok(self, step: str, detail: str = "") -> None:
        self.steps.append({"step": step, "passed": True, "detail": detail})
        print(f"  [PASS] {step}" + (f" — {detail}" if detail else ""))

    def fail(self, step: str, detail: str = "") -> None:
        self.steps.append({"step": step, "passed": False, "detail": detail})
        self.passed = False
        print(f"  [FAIL] {step}" + (f" — {detail}" if detail else ""))

    def check(self, condition: bool, step: str, detail: str = "") -> bool:
        if condition:
            self.ok(step, detail)
        else:
            self.fail(step, detail)
        return condition


# ========== 测试上下文 ==========


class TestContext:
    """场景间共享数据的容器。"""

    def __init__(self, url: str, admin_key: str):
        self.url = url
        self.admin_key = admin_key
        self.project_id: str = str(uuid.uuid4())
        self.pm_key: str | None = None
        self.dev_key: str | None = None
        self.batch_id_1: str | None = None  # PM 首次发布需求
        self.batch_id_2: str | None = None  # PM 冲突需求
        self.batch_id_3: str | None = None  # Dev 开发产物
        self.requirement_node_id: str | None = None  # 审批通过后的需求节点 ID
        self.operation_id: str = f"op-test-{uuid.uuid4().hex[:8]}"


# ========== 场景函数 ==========


async def scenario_1_bootstrap(ctx: TestContext) -> ScenarioResult:
    """场景 1：系统引导与密钥管理"""
    result = ScenarioResult("场景 1：系统引导与密钥管理")
    print(f"\n{'='*60}")
    print("场景 1：系统引导与密钥管理")
    print(f"  project_id = {ctx.project_id}")
    print(f"{'='*60}")

    async with make_client(ctx.url, ctx.admin_key) as admin:
        # 1.1 获取 admin skills
        r, err = await call_tool_safe(admin, "get_role_skills", {"role": "admin"})
        if result.check(err is None, "1.1 get_role_skills(admin)"):
            version = r.get("version", "")
            has_md = bool(r.get("skills_markdown"))
            # version 由 skills frontmatter 动态取 max，不硬编码具体值，避免 stale 断言
            result.check(
                bool(version) and "." in version and has_md,
                "1.1 验证 version 非空(形如 x.y.z) + markdown 非空",
                f"version={version}",
            )
        else:
            result.fail("1.1 验证 version", err or "未知错误")

        # 1.2 创建 PM key
        r, err = await call_tool_safe(
            admin,
            "create_access_key",
            {"role": "pm", "project_scope": [ctx.project_id]},
        )
        if result.check(err is None, "1.2 创建 PM Access Key"):
            ctx.pm_key = r.get("plaintext")
            result.check(
                bool(ctx.pm_key),
                "1.2 验证返回 plaintext",
                f"key_id={r.get('key_id', 'N/A')}",
            )
            mcp_cfg = r.get("mcp_config")
            prompt = r.get("onboarding_prompt")
            result.check(
                bool(mcp_cfg) and '"X-MCP-Key"' in mcp_cfg,
                "1.2 验证返回 mcp_config(JSON, 含 X-MCP-Key)",
            )
            result.check(
                bool(prompt)
                and "get_role_skills" in prompt
                and ctx.pm_key not in prompt,
                "1.2 验证返回 onboarding_prompt(含技能指引, 不含 Key)",
            )
        else:
            result.fail("1.2 创建 PM Key", err or "未知错误")

        # 1.3 创建 Dev key
        r, err = await call_tool_safe(
            admin,
            "create_access_key",
            {"role": "dev", "project_scope": [ctx.project_id]},
        )
        if result.check(err is None, "1.3 创建 Dev Access Key"):
            ctx.dev_key = r.get("plaintext")
            result.check(
                bool(ctx.dev_key),
                "1.3 验证返回 plaintext",
                f"key_id={r.get('key_id', 'N/A')}",
            )
            mcp_cfg = r.get("mcp_config")
            prompt = r.get("onboarding_prompt")
            result.check(
                bool(mcp_cfg) and '"X-MCP-Key"' in mcp_cfg,
                "1.3 验证返回 mcp_config(JSON, 含 X-MCP-Key)",
            )
            result.check(
                bool(prompt)
                and "get_role_skills" in prompt
                and ctx.dev_key not in prompt,
                "1.3 验证返回 onboarding_prompt(含技能指引, 不含 Key)",
            )
        else:
            result.fail("1.3 创建 Dev Key", err or "未知错误")

        # 1.4 列出 access keys
        r, err = await call_tool_safe(admin, "list_access_keys", {})
        if result.check(err is None, "1.4 列出 Access Keys"):
            listed = (r or {}).get("items", []) if isinstance(r, dict) else (r or [])
            result.check(
                len(listed) >= 3, "1.4 验证 >=3 个 key", f"count={len(listed)}"
            )
        else:
            result.fail("1.4 列出 Keys", err or "未知错误")

        # 1.5 创建 ProjectProfile
        r, err = await call_tool_safe(
            admin,
            "manage_project_profile",
            {
                "project_id": ctx.project_id,
                "action": "create",
                "profile": {
                    "title": "MemLake 测试项目",
                    "content": (
                        "用于端到端测试的项目，技术栈包括 "
                        "Python/FastMCP/PostgreSQL/Apache AGE/pgvector"
                    ),
                    "properties": {
                        "name": "MemLake 测试项目",
                        "description": "团队知识管理 MCP 服务端到端测试",
                        "tech_stack": [
                            "Python",
                            "FastMCP",
                            "PostgreSQL",
                            "Apache AGE",
                            "pgvector",
                        ],
                        "architecture": "MCP 网关 + 知识图谱 + 三引擎检索",
                    },
                    "tags": ["测试", "知识管理"],
                },
            },
        )
        if result.check(err is None, "1.5 创建 ProjectProfile"):
            result.check(
                bool(r.get("node_id")),
                "1.5 验证返回 node_id",
                f"node_id={r.get('node_id', 'N/A')}",
            )
        else:
            result.fail("1.5 创建 ProjectProfile", err or "未知错误")

        # 1.6 查询 ProjectProfile
        r, err = await call_tool_safe(
            admin, "get_project_profile", {"project_id": ctx.project_id}
        )
        if result.check(err is None, "1.6 查询 ProjectProfile"):
            profile = r.get("profile")
            title = profile.get("title", "N/A") if profile else "None"
            result.check(
                profile is not None, "1.6 验证 profile 非空", f"title={title}"
            )
        else:
            result.fail("1.6 查询 ProjectProfile", err or "未知错误")

    return result


async def scenario_2_pm_publish(ctx: TestContext) -> ScenarioResult:
    """场景 2：PM 发布需求"""
    result = ScenarioResult("场景 2：PM 发布需求")
    print(f"\n{'='*60}")
    print("场景 2：PM 发布需求")
    print(f"{'='*60}")

    if not ctx.pm_key:
        result.fail("前置条件", "PM key 未创建")
        return result

    async with make_client(ctx.url, ctx.pm_key) as pm:
        # 2.1 获取 PM skills
        r, err = await call_tool_safe(pm, "get_role_skills", {})
        if result.check(err is None, "2.1 get_role_skills(pm)"):
            result.check(
                bool(r.get("skills_markdown")),
                "2.1 验证 markdown 非空",
                f"version={r.get('version', 'N/A')}",
            )
        else:
            result.fail("2.1 get_role_skills", err or "未知错误")

        # 2.2 发布需求
        r, err = await call_tool_safe(
            pm,
            "publish_requirement",
            {
                "project_id": ctx.project_id,
                "requirement": {
                    "title": "用户登录功能",
                    "content": (
                        "实现基于 JWT 的用户登录认证功能。需求包括："
                        "1. 用户名密码登录 2. JWT token 签发与验证 "
                        "3. token 刷新机制 4. 登出失效 "
                        "5. 密码加密存储（bcrypt）。"
                        "接口：POST /api/login, POST /api/refresh, POST /api/logout。"
                        "错误码：401 未授权，403 禁止访问，429 限流。"
                    ),
                    "properties": {
                        "priority": "P1",
                        "module": "auth",
                        "acceptance_criteria": (
                            "1. 正确用户名密码登录成功 2. 错误密码返回401 "
                            "3. token过期后可刷新 4. 登出后token失效"
                        ),
                    },
                    "tags": ["认证", "安全", "JWT"],
                },
                "operation_id": ctx.operation_id,
            },
        )
        if result.check(err is None, "2.2 publish_requirement"):
            ctx.batch_id_1 = r.get("batch_id")
            status = r.get("status")
            item_count = r.get("item_count")
            result.check(
                status == "pending_review" and item_count == 1,
                "2.2 验证 status=pending_review, item_count=1",
                f"status={status}, count={item_count}",
            )
        else:
            result.fail("2.2 publish_requirement", err or "未知错误")

        # 2.3 搜索相似需求（应为空，未审批）
        r, err = await call_tool_safe(
            pm,
            "search_similar_requirements",
            {"project_id": ctx.project_id, "query": "登录认证"},
        )
        if result.check(err is None, "2.3 search_similar_requirements（未审批）"):
            fused = r.get("fused") or []
            result.check(
                len(fused) == 0, "2.3 验证返回空列表", f"fused_count={len(fused)}"
            )
        else:
            result.fail("2.3 search_similar_requirements", err or "未知错误")

    return result


async def scenario_3_admin_auto_approve(ctx: TestContext) -> ScenarioResult:
    """场景 3：Admin 自动审批（无冲突）"""
    result = ScenarioResult("场景 3：Admin 自动审批（无冲突）")
    print(f"\n{'='*60}")
    print("场景 3：Admin 自动审批（无冲突）")
    print(f"{'='*60}")

    if not ctx.batch_id_1:
        result.fail("前置条件", "batch_id_1 未创建")
        return result

    async with make_client(ctx.url, ctx.admin_key) as admin:
        # 3.1 查看待审批列表
        r, err = await call_tool_safe(admin, "review_pending_list", {})
        if result.check(err is None, "3.1 review_pending_list"):
            batches = r.get("batches") or []
            found = any(
                str(b.get("batch_id")) == str(ctx.batch_id_1) for b in batches
            )
            result.check(
                found,
                "3.1 验证列表包含场景2的 batch",
                f"total={r.get('total', 0)}, found={found}",
            )
        else:
            result.fail("3.1 review_pending_list", err or "未知错误")

        # 3.2 自动审批
        r, err = await call_tool_safe(
            admin, "review_auto_process", {"batch_id": ctx.batch_id_1}
        )
        if result.check(err is None, "3.2 review_auto_process"):
            decision = r.get("decision")
            status = r.get("status")
            result.check(
                decision == "auto_approved" and status == "approved",
                "3.2 验证 decision=auto_approved, status=approved",
                f"decision={decision}, status={status}",
            )
        else:
            result.fail("3.2 review_auto_process", err or "未知错误")

        # 3.3 获取批次详情（提取 target_id 即 requirement_node_id）
        r, err = await call_tool_safe(
            admin, "review_batch_detail", {"batch_id": ctx.batch_id_1}
        )
        if result.check(err is None, "3.3 review_batch_detail（提取 node_id）"):
            items = r.get("items") or []
            for item in items:
                if item.get("item_type") == "node" and item.get("target_id"):
                    ctx.requirement_node_id = str(item["target_id"])
                    break
            result.check(
                bool(ctx.requirement_node_id),
                "3.3 验证提取 requirement_node_id",
                f"node_id={ctx.requirement_node_id}",
            )
        else:
            result.fail("3.3 review_batch_detail", err or "未知错误")

    # 3.4 PM 搜索相似需求（应能找到已审批的需求）
    if ctx.pm_key:
        async with make_client(ctx.url, ctx.pm_key) as pm:
            r, err = await call_tool_safe(
                pm,
                "search_similar_requirements",
                {"project_id": ctx.project_id, "query": "登录认证"},
            )
            if result.check(
                err is None, "3.4 search_similar_requirements（已审批）"
            ):
                fused = r.get("fused") or []
                if fused:
                    title = fused[0].get("title", "N/A")
                    result.check(
                        True, "3.4 验证返回 >=1 条结果", f"title={title}"
                    )
                else:
                    result.fail("3.4 验证返回 >=1 条结果", "fused 为空")
            else:
                result.fail(
                    "3.4 search_similar_requirements", err or "未知错误"
                )

    return result


async def scenario_4_conflict_detection(ctx: TestContext) -> ScenarioResult:
    """场景 4：冲突检测与人工审批"""
    result = ScenarioResult("场景 4：冲突检测与人工审批")
    print(f"\n{'='*60}")
    print("场景 4：冲突检测与人工审批")
    print(f"{'='*60}")

    if not ctx.pm_key:
        result.fail("前置条件", "PM key 未创建")
        return result

    # 4.1 PM 发布高度相似的需求
    async with make_client(ctx.url, ctx.pm_key) as pm:
        r, err = await call_tool_safe(
            pm,
            "publish_requirement",
            {
                "project_id": ctx.project_id,
                "requirement": {
                    "title": "用户登录认证模块",
                    "content": (
                        "实现基于 JWT 的用户登录认证功能。需求包括："
                        "1. 用户名密码登录 2. JWT token 签发与验证 "
                        "3. token 刷新机制 4. 登出失效 "
                        "5. 密码加密存储（bcrypt）。"
                        "接口：POST /api/login, POST /api/refresh, POST /api/logout。"
                        "错误码：401 未授权，403 禁止访问，429 限流。"
                        "额外补充：支持 OAuth2 第三方登录。"
                    ),
                    "properties": {
                        "priority": "P1",
                        "module": "auth",
                        "acceptance_criteria": (
                            "1. 正确用户名密码登录成功 2. 错误密码返回401 "
                            "3. token过期后可刷新 4. 登出后token失效"
                        ),
                    },
                    "tags": ["认证", "安全", "JWT"],
                },
            },
        )
        if result.check(err is None, "4.1 发布相似需求"):
            ctx.batch_id_2 = r.get("batch_id")
            status = r.get("status")
            result.check(
                status == "pending_review",
                "4.1 验证 status=pending_review",
                f"batch_id={ctx.batch_id_2}",
            )
        else:
            result.fail("4.1 发布相似需求", err or "未知错误")

    # 4.2 ~ 4.4 Admin 操作
    if ctx.batch_id_2:
        async with make_client(ctx.url, ctx.admin_key) as admin:
            # 4.2 自动审批（应检测到冲突）
            r, err = await call_tool_safe(
                admin, "review_auto_process", {"batch_id": ctx.batch_id_2}
            )
            if result.check(err is None, "4.2 review_auto_process（冲突检测）"):
                decision = r.get("decision")
                conflict_hint = r.get("conflict_hint") or {}
                has_conflict = conflict_hint.get("has_conflict")
                result.check(
                    decision == "needs_human_review" and has_conflict,
                    "4.2 验证 decision=needs_human_review, has_conflict=True",
                    f"decision={decision}, has_conflict={has_conflict}",
                )
            else:
                result.fail("4.2 review_auto_process", err or "未知错误")

            # 4.3 查看批次详情
            r, err = await call_tool_safe(
                admin, "review_batch_detail", {"batch_id": ctx.batch_id_2}
            )
            if result.check(err is None, "4.3 review_batch_detail"):
                items = r.get("items") or []
                result.check(
                    len(items) > 0,
                    "4.3 验证 items 非空",
                    f"items_count={len(items)}",
                )
            else:
                result.fail("4.3 review_batch_detail", err or "未知错误")

            # 4.4 人工审批通过（允许并存）
            r, err = await call_tool_safe(
                admin,
                "review_approve",
                {
                    "batch_id": ctx.batch_id_2,
                    "review_comment": (
                        "人工确认：REQ-001 重复提交，内容含 OAuth2 补充，"
                        "批准更新版本"
                    ),
                },
            )
            if result.check(err is None, "4.4 review_approve（人工通过）"):
                status = r.get("status")
                result.check(
                    status == "approved",
                    "4.4 验证 status=approved",
                    f"status={status}",
                )
            else:
                result.fail("4.4 review_approve", err or "未知错误")

    return result


async def scenario_5_dev_submit(ctx: TestContext) -> ScenarioResult:
    """场景 5：Dev 提交开发产物"""
    result = ScenarioResult("场景 5：Dev 提交开发产物")
    print(f"\n{'='*60}")
    print("场景 5：Dev 提交开发产物")
    print(f"{'='*60}")

    if not ctx.dev_key or not ctx.requirement_node_id:
        result.fail(
            "前置条件",
            f"dev_key={ctx.dev_key is not None}, "
            f"req_node_id={ctx.requirement_node_id is not None}",
        )
        return result

    async with make_client(ctx.url, ctx.dev_key) as dev:
        # 5.1 获取 Dev skills
        r, err = await call_tool_safe(dev, "get_role_skills", {})
        if result.check(err is None, "5.1 get_role_skills(dev)"):
            result.check(
                bool(r.get("skills_markdown")),
                "5.1 验证 markdown 非空",
                f"version={r.get('version', 'N/A')}",
            )
        else:
            result.fail("5.1 get_role_skills", err or "未知错误")

        # 5.2 提交开发产物
        r, err = await call_tool_safe(
            dev,
            "submit_dev_artifacts",
            {
                "project_id": ctx.project_id,
                "requirement_id": ctx.requirement_node_id,
                "artifacts": {
                    "code_snippets": [
                        {
                            "ref": "login_service",
                            "title": "LoginService 类",
                            "content": (
                                "class LoginService:\n"
                                "    def __init__(self, user_repo, jwt_handler):\n"
                                "        self.user_repo = user_repo\n"
                                "        self.jwt_handler = jwt_handler\n\n"
                                "    async def login(self, username, password):\n"
                                "        user = await self.user_repo"
                                ".find_by_username(username)\n"
                                "        if not user or not verify_password("
                                "password, user.password_hash):\n"
                                "            raise AuthError('Invalid credentials')\n"
                                "        return self.jwt_handler.create_token"
                                "(user.id)\n"
                            ),
                            "properties": {
                                "name": "LoginService",
                                "type": "class",
                                "responsibility": (
                                    "处理用户登录认证，验证凭据并签发 JWT token"
                                ),
                                "file_path": "src/auth/login_service.py",
                            },
                            "tags": ["认证", "JWT"],
                        }
                    ],
                    "solutions": [
                        {
                            "ref": "jwt_solution",
                            "title": "JWT 无状态认证方案",
                            "content": (
                                "采用 JWT 无状态认证方案。"
                                "Access token 有效期 15 分钟，"
                                "Refresh token 有效期 7 天。"
                                "token 中包含 user_id 和 role 信息。"
                                "服务端不存储 session，通过签名验证 token 真实性。"
                            ),
                            "properties": {
                                "approach": (
                                    "使用 JWT token 实现无状态认证，"
                                    "access + refresh 双 token 机制"
                                ),
                                "version": "1.0",
                                "alternatives": (
                                    "Session-based 认证（需 Redis 存储），"
                                    "OAuth2 全权委托"
                                ),
                            },
                            "tags": ["认证", "JWT", "架构"],
                        }
                    ],
                    "pitfalls": [
                        {
                            "ref": "token_expire_pitfall",
                            "title": "Token 过期未刷新导致用户被登出",
                            "content": (
                                "前端未处理 access token 过期时的自动刷新，"
                                "导致用户操作中途被登出。"
                                "解决方案：在 axios 拦截器中捕获 401，"
                                "自动调用 refresh 接口获取新 token 后重试原请求。"
                            ),
                            "properties": {
                                "symptom": "用户操作中途突然被登出，需重新登录",
                                "root_cause": "前端未实现 token 自动刷新拦截器",
                                "solution": (
                                    "在 HTTP 客户端拦截器中捕获 401，"
                                    "自动调用 refresh 接口"
                                ),
                                "severity": "P2",
                            },
                            "tags": ["认证", "前端", "踩坑"],
                        }
                    ],
                },
                "relations": [
                    {
                        "from_ref": "login_service",
                        "to_ref": "jwt_solution",
                        "relation_type": "realized_by",
                    }
                ],
            },
        )
        if result.check(err is None, "5.2 submit_dev_artifacts"):
            ctx.batch_id_3 = r.get("batch_id")
            status = r.get("status")
            item_count = r.get("item_count")
            # 3 节点 + 1 自动 implements 边 + 1 realized_by 边 = 5
            result.check(
                status == "pending_review" and item_count >= 4,
                "5.2 验证 status=pending_review, item_count>=4",
                f"status={status}, count={item_count}",
            )
        else:
            result.fail("5.2 submit_dev_artifacts", err or "未知错误")

    return result


async def scenario_6_admin_approve_dev(ctx: TestContext) -> ScenarioResult:
    """场景 6：Admin 审批开发产物"""
    result = ScenarioResult("场景 6：Admin 审批开发产物")
    print(f"\n{'='*60}")
    print("场景 6：Admin 审批开发产物")
    print(f"{'='*60}")

    if not ctx.batch_id_3:
        result.fail("前置条件", "batch_id_3 未创建")
        return result

    # 6.1 Admin 自动审批
    async with make_client(ctx.url, ctx.admin_key) as admin:
        r, err = await call_tool_safe(
            admin, "review_auto_process", {"batch_id": ctx.batch_id_3}
        )
        if result.check(err is None, "6.1 review_auto_process"):
            decision = r.get("decision")
            result.check(
                decision == "auto_approved",
                "6.1 验证 decision=auto_approved",
                f"decision={decision}",
            )
        else:
            result.fail("6.1 review_auto_process", err or "未知错误")

    # 6.2 Dev 搜索代码片段
    if ctx.dev_key:
        async with make_client(ctx.url, ctx.dev_key) as dev:
            r, err = await call_tool_safe(
                dev,
                "search_code_snippets",
                {"project_id": ctx.project_id, "query": "登录认证"},
            )
            if result.check(err is None, "6.2 search_code_snippets"):
                fused = r.get("fused") or []
                if fused:
                    title = fused[0].get("title", "N/A")
                    result.check(
                        True, "6.2 验证返回 >=1 条结果", f"title={title}"
                    )
                else:
                    result.fail("6.2 验证返回 >=1 条结果", "fused 为空")
            else:
                result.fail("6.2 search_code_snippets", err or "未知错误")

    # 6.3 PM 分析影响面
    if ctx.pm_key and ctx.requirement_node_id:
        async with make_client(ctx.url, ctx.pm_key) as pm:
            r, err = await call_tool_safe(
                pm,
                "analyze_impact_scope",
                {
                    "project_id": ctx.project_id,
                    "requirement_id": ctx.requirement_node_id,
                },
            )
            if result.check(err is None, "6.3 analyze_impact_scope"):
                codes = r.get("codes") or []
                solutions = r.get("solutions") or []
                result.check(
                    len(codes) > 0 and len(solutions) > 0,
                    "6.3 验证 codes 和 solutions 非空",
                    f"codes={len(codes)}, solutions={len(solutions)}",
                )
            else:
                result.fail("6.3 analyze_impact_scope", err or "未知错误")

    return result


async def scenario_7_knowledge_retrieval(ctx: TestContext) -> ScenarioResult:
    """场景 7：知识检索与审计"""
    result = ScenarioResult("场景 7：知识检索与审计")
    print(f"\n{'='*60}")
    print("场景 7：知识检索与审计")
    print(f"{'='*60}")

    async with make_client(ctx.url, ctx.admin_key) as admin:
        # 7.1 列出知识节点
        r, err = await call_tool_safe(
            admin,
            "list_knowledge",
            {"project_id": ctx.project_id, "status": "approved"},
        )
        if result.check(err is None, "7.1 list_knowledge(approved)"):
            nodes = r.get("nodes") or []
            result.check(
                len(nodes) >= 4,
                "7.1 验证 >=4 个节点",
                f"count={len(nodes)}",
            )
        else:
            result.fail("7.1 list_knowledge", err or "未知错误")

        # 7.2 按类型过滤（Pitfall）
        r, err = await call_tool_safe(
            admin,
            "list_knowledge",
            {"project_id": ctx.project_id, "node_type": "Pitfall"},
        )
        if result.check(err is None, "7.2 list_knowledge(Pitfall)"):
            nodes = r.get("nodes") or []
            result.check(
                len(nodes) >= 1,
                "7.2 验证 >=1 个 Pitfall",
                f"count={len(nodes)}",
            )
        else:
            result.fail("7.2 list_knowledge(Pitfall)", err or "未知错误")

        # 7.4 查询审计日志
        r, err = await call_tool_safe(
            admin,
            "query_audit_log",
            {"project_id": ctx.project_id, "limit": 50},
        )
        if result.check(err is None, "7.4 query_audit_log(project)"):
            logs = r.get("logs") or []
            result.check(
                len(logs) > 0,
                "7.4 验证审计日志非空",
                f"count={len(logs)}",
            )
        else:
            result.fail("7.4 query_audit_log", err or "未知错误")

    # 7.3 PM 获取需求上下文
    if ctx.pm_key and ctx.requirement_node_id:
        async with make_client(ctx.url, ctx.pm_key) as pm:
            r, err = await call_tool_safe(
                pm,
                "get_requirement_context",
                {
                    "requirement_id": ctx.requirement_node_id,
                    "depth": 3,
                },
            )
            if result.check(err is None, "7.3 get_requirement_context"):
                requirement = r.get("requirement")
                related = r.get("related_nodes") or []
                req_ok = requirement is not None
                result.check(
                    req_ok and len(related) > 0,
                    "7.3 验证 requirement 非空 + related_nodes 非空",
                    f"requirement={'OK' if req_ok else 'None'}, "
                    f"related={len(related)}",
                )
            else:
                result.fail("7.3 get_requirement_context", err or "未知错误")

    return result


async def scenario_8_error_handling(ctx: TestContext) -> ScenarioResult:
    """场景 8：错误处理与安全"""
    result = ScenarioResult("场景 8：错误处理与安全")
    print(f"\n{'='*60}")
    print("场景 8：错误处理与安全")
    print(f"{'='*60}")

    # 8.1 无 X-MCP-Key header
    try:
        async with make_client(ctx.url) as no_auth:
            r, err = await call_tool_safe(no_auth, "get_role_skills", {})
            result.check(
                err is not None,
                "8.1 无 header 认证失败",
                f"error={err[:80] if err else 'N/A'}",
            )
    except Exception as e:
        result.check(True, "8.1 无 header 认证失败（连接层）", str(e)[:80])

    # 8.2 无效 Access Key
    try:
        async with make_client(ctx.url, "ak_invalid.invalid") as bad_auth:
            r, err = await call_tool_safe(bad_auth, "get_role_skills", {})
            result.check(
                err is not None,
                "8.2 无效 Key 认证失败",
                f"error={err[:80] if err else 'N/A'}",
            )
    except Exception as e:
        result.check(True, "8.2 无效 Key 认证失败（连接层）", str(e)[:80])

    if not ctx.pm_key:
        result.fail("前置条件", "PM key 未创建，跳过 8.3-8.6")
        return result

    async with make_client(ctx.url, ctx.pm_key) as pm:
        # 8.3 PM 调用 admin 专属工具
        r, err = await call_tool_safe(pm, "review_pending_list", {})
        result.check(
            err is not None,
            "8.3 PM 越权调用 review_pending_list 被拒",
            f"error={err[:80] if err else 'N/A'}",
        )

        # 8.4 PM 调用 list_access_keys
        r, err = await call_tool_safe(pm, "list_access_keys", {})
        result.check(
            err is not None,
            "8.4 PM 越权调用 list_access_keys 被拒",
            f"error={err[:80] if err else 'N/A'}",
        )

        # 8.5 Schema 校验失败（缺必填字段）
        r, err = await call_tool_safe(
            pm,
            "publish_requirement",
            {
                "project_id": ctx.project_id,
                "requirement": {
                    "title": "测试需求",
                    "content": "测试内容",
                    "properties": {
                        # 缺少 requirement_id, priority, module
                        "acceptance_criteria": "测试",
                    },
                    "tags": [],
                },
            },
        )
        result.check(
            err is not None,
            "8.5 Schema 校验失败（缺必填字段）",
            f"error={err[:80] if err else 'N/A'}",
        )

        # 8.6 幂等重放
        r, err = await call_tool_safe(
            pm,
            "publish_requirement",
            {
                "project_id": ctx.project_id,
                "requirement": {
                    "title": "用户登录功能",
                    "content": "实现基于JWT的用户登录认证功能...",
                    "properties": {
                        "priority": "P1",
                        "module": "auth",
                        "acceptance_criteria": "测试",
                    },
                    "tags": ["认证"],
                },
                "operation_id": ctx.operation_id,  # 与场景 2 相同
            },
        )
        if result.check(err is None, "8.6 幂等重放"):
            batch_id = r.get("batch_id")
            result.check(
                str(batch_id) == str(ctx.batch_id_1),
                "8.6 验证返回相同 batch_id",
                f"got={batch_id}, expected={ctx.batch_id_1}",
            )
        else:
            result.fail("8.6 幂等重放", err or "未知错误")

    return result


# ========== 主函数 ==========


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="MemLake 端到端 MCP 协议测试"
    )
    parser.add_argument(
        "--admin-key", required=True, help="Admin Access Key 明文"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"MCP 服务器 URL（默认 {DEFAULT_URL}）",
    )
    args = parser.parse_args()

    print("\nMemLake 端到端测试")
    print(f"服务器: {args.url}")
    print(f"Admin Key: {args.admin_key[:12]}...")

    ctx = TestContext(args.url, args.admin_key)

    scenarios = [
        scenario_1_bootstrap,
        scenario_2_pm_publish,
        scenario_3_admin_auto_approve,
        scenario_4_conflict_detection,
        scenario_5_dev_submit,
        scenario_6_admin_approve_dev,
        scenario_7_knowledge_retrieval,
        scenario_8_error_handling,
    ]

    all_results: list[ScenarioResult] = []
    for scenario_fn in scenarios:
        try:
            result = await scenario_fn(ctx)
            all_results.append(result)
        except Exception as e:
            print(f"\n  [ERROR] 场景异常: {e}")
            traceback.print_exc()
            sr = ScenarioResult(scenario_fn.__name__)
            sr.fail("场景执行", str(e))
            all_results.append(sr)

    # 汇总
    print(f"\n\n{'='*60}")
    print("测试汇总")
    print(f"{'='*60}")
    total = len(all_results)
    passed = sum(1 for r in all_results if r.passed)
    failed = total - passed

    for r in all_results:
        status = "PASS" if r.passed else "FAIL"
        steps_passed = sum(1 for s in r.steps if s["passed"])
        steps_total = len(r.steps)
        print(f"  [{status}] {r.name} ({steps_passed}/{steps_total} 步骤通过)")

    print(f"\n总计: {passed}/{total} 场景通过, {failed} 失败")

    if failed > 0:
        print("\n失败详情:")
        for r in all_results:
            if not r.passed:
                for s in r.steps:
                    if not s["passed"]:
                        print(f"  {r.name} > {s['step']}: {s['detail']}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
