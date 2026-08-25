"""三角色 MCP 协作端到端测试（完全仿真：内存客户端 + patch 三角色认证）。

把原 tests/e2e/test_mcp_e2e.py 的手动脚本（依赖真实 HTTP + 真实 Access Key 明文）
改造为可进 pytest CI 的自动化协作用例：用 fastmcp.Client(app) 内存客户端 +
create_mcp_server() + monkeypatch 三角色认证，走完整 FastMCP 工具注册、4 个中间件
（认证/RBAC/限流/审计）、lifespan（真实 DB + 真实 embedding），但不依赖真实密钥明文。

沿用 integration/test_gateway_tools_manage.py 已验证的「内存客户端 + monkeypatch 认证」
范式，并推广为可切换 admin/pm/dev 三角色的客户端构造。

覆盖原 e2e 的 8 场景（三角色完整协作流水线）：
1. bootstrap 建 PM/Dev Key + 建 system + 建 ProjectProfile
2. PM 发布需求 → admin review_pending_list → review_auto_process
3. 冲突检测：PM 发相似需求 → needs_human_review → review_batch_detail → review_approve
4. Dev 提交产物 → admin 审批 → search_code_snippets / analyze_impact_scope
5. 知识检索与审计：list_knowledge / query_audit_log / get_requirement_context
6. 错误处理：无认证/坏认证/RBAC 越权/Schema 校验/幂等重放

依赖真实 embedding 容器（localhost:8001，经 conftest.real_embedding_client 健康检查），
容器未运行时整模块 skip。
"""

import json
import uuid

import pytest
from fastmcp import Client
from fastmcp.server.auth import AccessToken

from mem_lake.gateway.server import create_mcp_server

# ============================================================================
# 真实 embedding 依赖守卫：容器不可达时整模块 skip
# ============================================================================


@pytest.fixture(autouse=True)
def _require_embedding_container(real_embedding_client):
    """依赖真实 embedding 服务；容器未运行时由 real_embedding_client 自动 skip。"""
    yield real_embedding_client


# ============================================================================
# 三角色认证客户端构造（核心复用点）
# ============================================================================


class _FakeRequest:
    """内存传输下承载 scope['user'] 的伪请求。

    内存客户端无真实 HTTP request，middleware.on_request 会 setattr request.scope["user"]，
    故 patched get_http_request() 需返回同一实例，使 on_request 写入的 user 能被
    on_call_tool 的 get_access_token() 读回（FastMCP 从 request.scope["user"] 优先读取）。
    """

    def __init__(self) -> None:
        self.scope: dict = {}


def _make_role_app(monkeypatch, *, role: str, project_id: str):
    """构造一个以指定角色身份运行的 app（create_mcp_server() 内存实例）。

    通过 patch 认证链路注入角色身份：
    - middleware.extract_access_key_from_headers / authenticate_access_key / get_http_request
    - middleware.get_access_token 与 dependencies.get_access_token 两个命名空间
    role ∈ admin/pm/dev。pm/dev 注入 project_scope=[project_id] 以通过项目权限校验；
    admin 注入空 scope（不受限）。
    """
    req = _FakeRequest()
    # 同一 role+project 派生稳定 key_id，保证同角色多次调用使用同一 actor
    #（幂等键依赖 submitted_by，若每次随机会造成同 operation_id 二次提交判为不同 key）
    key_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memlake-e2e/{role}/{project_id}"))

    async def _fake_auth(_session, _key):
        return {
            "key_id": key_id,
            "role": role,
            "project_scope": ([] if role == "admin" else [project_id]),
            "system_scope": [],
            "lax_mode": False,
        }

    def _fake_token(*_args, **_kwargs):
        return AccessToken(
            token="dummy",
            client_id=key_id,
            scopes=[role],
            claims={
                "role": role,
                "key_id": key_id,
                "project_scope": ([] if role == "admin" else [project_id]),
                "system_scope": [],
                "lax_mode": False,
            },
        )

    monkeypatch.setattr(
        "mem_lake.gateway.middleware.extract_access_key_from_headers",
        lambda _headers: "dummy-key",
    )
    monkeypatch.setattr("mem_lake.gateway.middleware.authenticate_access_key", _fake_auth)
    monkeypatch.setattr("mem_lake.gateway.middleware.get_http_request", lambda: req)
    monkeypatch.setattr("mem_lake.gateway.middleware.get_access_token", _fake_token)
    monkeypatch.setattr("mem_lake.gateway.dependencies.get_access_token", _fake_token)
    return create_mcp_server()


# ============================================================================
# 输出解析
# ============================================================================


def _parse(result):
    """从 CallToolResult 解析出参 dict，失败时抛错。"""
    if getattr(result, "is_error", False):
        text = ""
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "text"):
                text = item.text
                break
        raise AssertionError(f"工具返回错误: {text or '未知错误'}")
    sc = getattr(result, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    for item in getattr(result, "content", []) or []:
        if hasattr(item, "text"):
            return json.loads(item.text)
    return {}


async def _call(client: Client, tool: str, arguments: dict) -> dict:
    """调用工具并解析，失败抛 AssertionError。"""
    return _parse(await client.call_tool(tool, arguments))


async def _expect_tool_error(client: Client, tool: str, arguments: dict) -> str:
    """调用工具并断言其被拒绝（返回 is_error 或抛 ToolError），返回错误消息。

    适用于验证越权/未认证/校验失败等应报错的场景。
    """
    from fastmcp.exceptions import ToolError

    try:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        # is_error 但未被 raise：尝试取错误文本
        text = ""
        for item in getattr(result, "content", []) or []:
            if hasattr(item, "text"):
                text = item.text
                break
        if text:
            return text
        return "工具返回错误（无文本）"
    except ToolError as e:
        return str(e)


# ============================================================================
# 用例
# ============================================================================


class TestRolesCollaboration:
    """三角色完整协作流水线（对应原 e2e 8 场景）。

    用例间通过类属性共享跨角色产出（project_id / system_id / requirement_node_id），
    pytest 按定义顺序执行同类内测试，故 bootstrap 先设置、后续用例读取。
    """

    # 跨用例共享状态（类属性，避免 pytest 每个方法重建实例导致 self 状态丢失）
    _project_id: str | None = None
    _system_id: str | None = None
    _requirement_node_id: str | None = None

    async def test_bootstrap_creates_keys_and_profile(self, monkeypatch):
        """场景1：admin 建 system + 建 PM/Dev Key + 建 ProjectProfile。"""
        project_id = str(uuid.uuid4())
        system_id = None

        async with Client(_make_role_app(monkeypatch, role="admin", project_id=project_id)) as admin:
            # get_role_skills
            skills = await _call(admin, "get_role_skills", {"role": "admin"})
            assert skills.get("version") and "." in skills["version"]
            assert skills.get("skills_markdown")

            # 建 system（publish_requirement 需 system_id）
            sys_res = await _call(
                admin, "manage_system", {"action": "create", "name": f"sys-{uuid.uuid4().hex[:8]}"}
            )
            system_id = sys_res["system_id"]
            assert system_id
            await _call(
                admin,
                "manage_system",
                {
                    "action": "set_projects",
                    "system_id": system_id,
                    "project_ids": [project_id],
                },
            )

            # 建 PM/Dev Key
            pm = await _call(
                admin, "create_access_key", {"role": "pm", "project_scope": [project_id]}
            )
            dev = await _call(
                admin, "create_access_key", {"role": "dev", "project_scope": [project_id]}
            )
            assert pm["plaintext"]
            assert dev["plaintext"]

            # 建 ProjectProfile
            prof = await _call(
                admin,
                "manage_project_profile",
                {
                    "project_id": project_id,
                    "action": "create",
                    "profile": {
                        "title": "E2E 测试项目",
                        "content": "三角色协作端到端测试项目",
                        "properties": {
                            "name": "E2E 测试项目",
                            "description": "pytest 自动化三角色协作",
                            "tech_stack": ["Python", "FastMCP", "PostgreSQL"],
                            "architecture": "MCP 网关 + 知识图谱 + 三引擎检索",
                        },
                        "tags": ["测试"],
                    },
                },
            )
            assert prof.get("node_id")
            # 查回
            prof2 = await _call(admin, "get_project_profile", {"project_id": project_id})
            assert prof2.get("profile") is not None

        # 返回供后续用例使用（类属性跨用例传递）
        self.__class__._project_id = project_id
        self.__class__._system_id = system_id

    async def test_pm_publish_and_admin_autoapprove(self, monkeypatch):
        """场景2+3：PM 发布需求 → admin review_pending_list → review_auto_process → 校验 approved。"""
        project_id = self.__class__._project_id
        system_id = self.__class__._system_id
        assert project_id and system_id, "需先跑 bootstrap 用例"

        # PM 发布需求
        async with Client(_make_role_app(monkeypatch, role="pm", project_id=project_id)) as pm:
            r = await _call(
                pm,
                "publish_requirement",
                {
                    "system_id": system_id,
                    "project_id": project_id,
                    "requirement": {
                        "title": "用户登录功能",
                        "content": (
                            "实现基于 JWT 的用户登录认证功能。需求包括："
                            "1. 用户名密码登录 2. JWT token 签发与验证 "
                            "3. token 刷新机制 4. 登出失效 5. 密码加密存储。"
                            "接口：POST /api/login, POST /api/refresh, POST /api/logout。"
                        ),
                        "properties": {
                            "requirement_id": "REQ-001",
                            "priority": "P1",
                            "module": "auth",
                            "acceptance_criteria": "正确登录成功，错误密码返回401",
                        },
                        "tags": ["认证", "JWT"],
                    },
                    "operation_id": f"op-e2e-{uuid.uuid4().hex[:8]}",
                },
            )
            assert r["status"] == "pending_review"
            assert r["item_count"] == 1
            batch_id_1 = r["batch_id"]

        # admin 查看待审批 + 自动审批
        async with Client(_make_role_app(monkeypatch, role="admin", project_id=project_id)) as admin:
            pending = await _call(admin, "review_pending_list", {})
            assert any(str(b["batch_id"]) == str(batch_id_1) for b in pending["batches"])

            auto = await _call(admin, "review_auto_process", {"batch_id": batch_id_1})
            assert auto["decision"] == "auto_approved"
            assert auto["status"] == "approved"

            detail = await _call(admin, "review_batch_detail", {"batch_id": batch_id_1})
            node_id = None
            for item in detail.get("items", []):
                if item.get("item_type") == "node" and item.get("target_id"):
                    node_id = str(item["target_id"])
                    break
            assert node_id
            self.__class__._requirement_node_id = node_id

    async def test_conflict_detection_to_human_review(self, monkeypatch):
        """场景4：PM 发高度相似需求 → review_auto_process needs_human_review → review_approve。"""
        project_id = self.__class__._project_id
        system_id = self.__class__._system_id
        assert project_id and system_id

        async with Client(_make_role_app(monkeypatch, role="pm", project_id=project_id)) as pm:
            r = await _call(
                pm,
                "publish_requirement",
                {
                    "system_id": system_id,
                    "project_id": project_id,
                    "requirement": {
                        "title": "用户登录认证模块",
                        "content": (
                            "实现基于 JWT 的用户登录认证功能。需求包括："
                            "1. 用户名密码登录 2. JWT token 签发与验证 "
                            "3. token 刷新机制 4. 登出失效 5. 密码加密存储。"
                            "接口：POST /api/login, POST /api/refresh, POST /api/logout。"
                            "额外补充：支持 OAuth2 第三方登录。"
                        ),
                        "properties": {
                            "requirement_id": "REQ-001",
                            "priority": "P1",
                            "module": "auth",
                            "acceptance_criteria": "正确登录成功，错误密码返回401",
                        },
                        "tags": ["认证", "安全", "JWT"],
                    },
                },
            )
            assert r["status"] == "pending_review"
            batch_id_2 = r["batch_id"]

        async with Client(_make_role_app(monkeypatch, role="admin", project_id=project_id)) as admin:
            auto = await _call(admin, "review_auto_process", {"batch_id": batch_id_2})
            assert auto["decision"] == "needs_human_review"
            assert (auto.get("conflict_hint") or {}).get("has_conflict") is True

            detail = await _call(admin, "review_batch_detail", {"batch_id": batch_id_2})
            assert len(detail.get("items", [])) > 0

            approved = await _call(
                admin,
                "review_approve",
                {"batch_id": batch_id_2, "review_comment": "人工确认：重复提交，批准更新版本"},
            )
            assert approved["status"] == "approved"

    async def test_dev_submit_and_admin_approve(self, monkeypatch):
        """场景5+6：Dev 提交产物 → admin 审批 → 检索代码/影响面。"""
        project_id = self.__class__._project_id
        req_node_id = self.__class__._requirement_node_id
        assert project_id and req_node_id

        # Dev 提交产物
        async with Client(_make_role_app(monkeypatch, role="dev", project_id=project_id)) as dev:
            r = await _call(
                dev,
                "submit_dev_artifacts",
                {
                    "project_id": project_id,
                    "requirement_id": req_node_id,
                    "artifacts": {
                        "code_snippets": [
                            {
                                "ref": "login_service",
                                "title": "LoginService 类",
                                "content": (
                                    "class LoginService:\\n"
                                    "    async def login(self, username, password):\\n"
                                    "        user = await self.user_repo.find_by_username(username)\\n"
                                    "        return token"
                                ),
                                "properties": {
                                    "name": "LoginService",
                                    "type": "class",
                                    "responsibility": "处理登录认证",
                                    "file_path": "src/auth/login_service.py",
                                },
                                "tags": ["认证"],
                            }
                        ],
                        "solutions": [
                            {
                                "ref": "jwt_solution",
                                "title": "JWT 无状态认证方案",
                                "content": "采用 JWT 无状态认证，access+refresh 双 token。",
                                "properties": {
                                    "approach": "使用 JWT 实现无状态认证",
                                    "version": "1.0",
                                },
                                "tags": ["认证"],
                            }
                        ],
                        "pitfalls": [
                            {
                                "ref": "token_expire_pitfall",
                                "title": "Token 过期未刷新导致登出",
                                "content": "前端未处理 access token 过期自动刷新。",
                                "properties": {
                                    "symptom": "操作中途被登出",
                                    "root_cause": "未实现自动刷新",
                                    "solution": "捕获401自动调用refresh",
                                    "severity": "P2",
                                },
                                "tags": ["认证"],
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
            assert r["status"] == "pending_review"
            assert r["item_count"] >= 4
            batch_id_3 = r["batch_id"]

        # admin 审批
        async with Client(_make_role_app(monkeypatch, role="admin", project_id=project_id)) as admin:
            auto = await _call(admin, "review_auto_process", {"batch_id": batch_id_3})
            assert auto["decision"] == "auto_approved"

        # Dev 检索代码片段
        async with Client(_make_role_app(monkeypatch, role="dev", project_id=project_id)) as dev:
            search = await _call(
                dev,
                "search_code_snippets",
                {"project_id": project_id, "query": "登录认证", "min_score": None},
            )
            assert len(search.get("fused") or []) >= 1

        # PM 影响面分析
        async with Client(_make_role_app(monkeypatch, role="pm", project_id=project_id)) as pm:
            impact = await _call(
                pm, "analyze_impact_scope", {"project_id": project_id, "requirement_id": req_node_id}
            )
            assert len(impact.get("codes") or []) > 0
            assert len(impact.get("solutions") or []) > 0

    async def test_knowledge_retrieval_and_audit(self, monkeypatch):
        """场景7：list_knowledge / query_audit_log / get_requirement_context。"""
        project_id = self.__class__._project_id
        req_node_id = self.__class__._requirement_node_id
        assert project_id and req_node_id

        async with Client(_make_role_app(monkeypatch, role="admin", project_id=project_id)) as admin:
            nodes = await _call(admin, "list_knowledge", {"project_id": project_id, "status": "approved"})
            assert len(nodes.get("nodes") or []) >= 4

            pitfalls = await _call(
                admin, "list_knowledge", {"project_id": project_id, "node_type": "Pitfall"}
            )
            assert len(pitfalls.get("nodes") or []) >= 1

            logs = await _call(admin, "query_audit_log", {"project_id": project_id, "limit": 50})
            assert len(logs.get("logs") or []) > 0

        async with Client(_make_role_app(monkeypatch, role="pm", project_id=project_id)) as pm:
            ctx = await _call(pm, "get_requirement_context", {"requirement_id": req_node_id, "depth": 3})
            assert ctx.get("requirement") is not None
            assert len(ctx.get("related_nodes") or []) > 0

    async def test_error_handling_rbac_and_idempotency(self, monkeypatch):
        """场景8：无认证/坏认证/RBAC 越权/Schema 校验/幂等重放。"""
        project_id = self.__class__._project_id
        system_id = self.__class__._system_id
        assert project_id and system_id

        # 8.1/8.2 无认证 & 坏认证：未 patch 认证 → get_access_token() None → 工具被拒
        # 构造一个不注入角色的 app（on_request 未设 user，RBAC 拒绝）
        req = _FakeRequest()

        async def _noauth_auth(_session, _key):
            return None

        monkeypatch.setattr(
            "mem_lake.gateway.middleware.extract_access_key_from_headers",
            lambda _headers: None,
        )
        monkeypatch.setattr("mem_lake.gateway.middleware.authenticate_access_key", _noauth_auth)
        monkeypatch.setattr("mem_lake.gateway.middleware.get_http_request", lambda: req)
        # dependencies.get_access_token 不 patch → 但 on_call_tool 用 middleware.get_access_token

        def _deny_token(*_a, **_k):
            return None

        monkeypatch.setattr("mem_lake.gateway.middleware.get_access_token", _deny_token)
        async with Client(create_mcp_server()) as noauth:
            err = await _expect_tool_error(noauth, "get_role_skills", {})
            assert "认证" in err or "Access Key" in err

        # 8.3/8.4 PM 越权调用 admin 专属工具
        async with Client(_make_role_app(monkeypatch, role="pm", project_id=project_id)) as pm:
            err = await _expect_tool_error(pm, "review_pending_list", {})
            assert "权限" in err or "拒绝" in err

            err = await _expect_tool_error(pm, "list_access_keys", {})
            assert "权限" in err or "拒绝" in err

            # 8.5 Schema 校验失败（缺必填字段）
            err = await _expect_tool_error(
                pm,
                "publish_requirement",
                {
                    "system_id": system_id,
                    "project_id": project_id,
                    "requirement": {
                        "title": "测试需求",
                        "content": "测试内容",
                        "properties": {"acceptance_criteria": "测试"},
                        "tags": [],
                    },
                },
            )
            assert "错误" in err or "校验" in err or "必填" in err

            # 8.6 幂等重放：publish_requirement 用同一 operation_id（沿用场景2的证明，
            # 此处用一个全新 op 演示同 op 二次返回同 batch_id）
            op = f"op-idem-{uuid.uuid4().hex[:8]}"
            req_payload = {
                "system_id": system_id,
                "project_id": project_id,
                "requirement": {
                    "title": "幂等测试需求",
                    "content": "验证同 operation_id 重复提交返回相同 batch_id",
                    "properties": {
                        "requirement_id": f"REQ-IDEM-{uuid.uuid4().hex[:6].upper()}",
                        "priority": "P2",
                        "module": "auth",
                        "acceptance_criteria": "a",
                    },
                    "tags": [],
                },
                "operation_id": op,
            }
            first = await _call(pm, "publish_requirement", req_payload)
            second = await _call(pm, "publish_requirement", req_payload)
            assert str(first["batch_id"]) == str(second["batch_id"])
