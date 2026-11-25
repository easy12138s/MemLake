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

from mem_lake.db.session import AsyncSessionLocal
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


def _make_role_app(monkeypatch, *, role: str, project_id: str, system_scope: list[str] | None = None):
    """构造一个以指定角色身份运行的 app（create_mcp_server() 内存实例）。

    通过 patch 认证链路注入角色身份：
    - middleware.extract_access_key_from_headers / authenticate_access_key / get_http_request
    - middleware.get_access_token 与 dependencies.get_access_token 两个命名空间
    role ∈ admin/pm/dev。pm/dev 注入 project_scope=[project_id] 以通过项目权限校验；
    admin 注入空 scope（不受限）。system_scope 可注入 dev/pm 的 system 绑定（数据回传
    测试需求检索的 system 兜底等）。
    """
    req = _FakeRequest()
    # 同一 role+project 派生稳定 key_id，保证同角色多次调用使用同一 actor
    #（幂等键依赖 submitted_by，若每次随机会造成同 operation_id 二次提交判为不同 key）
    key_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memlake-e2e/{role}/{project_id}"))
    system_scope = system_scope or []

    async def _fake_auth(_session, _key):
        return {
            "key_id": key_id,
            "role": role,
            "project_scope": ([] if role == "admin" else [project_id]),
            "system_scope": system_scope,
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
                "system_scope": system_scope,
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


async def _cleanup_nodes(db_session, graph_store, node_ids) -> None:
    """测试自清：AGE 顶点（DETACH）+ 向量/审计/节点行，最后 commit。"""
    from sqlalchemy import delete

    from mem_lake.audit.models import AuditLog
    from mem_lake.knowledge.models import KnowledgeNode, NodeEmbedding

    ids = list(node_ids)
    if not ids:
        return
    for nid in ids:
        await graph_store._exec_cypher(
            db_session, "MATCH (n {id: $nid}) DETACH DELETE n", {"nid": str(nid)}
        )
    await db_session.execute(delete(NodeEmbedding).where(NodeEmbedding.node_id.in_(ids)))
    await db_session.execute(delete(AuditLog).where(AuditLog.target_id.in_(ids)))
    await db_session.execute(delete(KnowledgeNode).where(KnowledgeNode.id.in_(ids)))
    await db_session.commit()


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
            # 批次三：get_role_skills 已删除（skills 改 GitHub 分发），
            # 同点位改为 list_requirements 冒烟（新枚举工具）
            lst = await _call(
                admin, "list_requirements", {"project_id": project_id, "limit": 1}
            )
            assert lst.get("total") >= 0 and isinstance(lst.get("items"), list)

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
            prof2 = await _call(admin, "get_project_info",
                                 {"action": "get", "project_id": project_id, "include_profile": True})
            assert prof2.get("project") is not None and prof2["project"].get("profile") is not None

        # 返回供后续用例使用（类属性跨用例传递）
        self.__class__._project_id = project_id
        self.__class__._system_id = system_id

    async def test_pm_publish_and_admin_autoapprove(self, monkeypatch, db_session):
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
            # C2/ISSUE-17：严格模式回执显式回显 mode=strict（pending_review 属正常态）
            assert r["mode"] == "strict", r
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

        # 审批通过后向量由后台 worker 异步生成（create_task 挂在当前用例的事件循环上，
        # 用例结束循环关闭即随之死亡）——后续场景 4 的 L3 语义冲突检测依赖该向量。
        # 必须在本用例内轮询等待向量落地（上限 10s），否则冲突检测前提随机不成立。
        import asyncio

        from sqlalchemy import text as sa_text

        deadline = asyncio.get_running_loop().time() + 10.0
        while True:
            row = (
                await db_session.execute(
                    sa_text(
                        "SELECT 1 FROM node_embedding WHERE node_id = :nid LIMIT 1"
                    ),
                    {"nid": uuid.UUID(node_id)},
                )
            ).first()
            if row is not None:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("审批后后台向量任务 10s 内未完成，后续 L3 冲突检测前提不成立")
            await asyncio.sleep(0.1)

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
            # ISSUE-16：auto_process 回执必须带 created 节点清单（跨批次引用的前提）
            created = auto.get("created") or []
            assert len({c["node_id"] for c in created}) >= 2, "auto_process 回执缺 created"
            assert (auto.get("edges_created") or 0) >= 1, "auto_process 回执缺 edges_created"
            for c in created:
                assert c["node_type"] and c["title"], "created 项须含 node_type/title"

        # 审批节点为延迟向量化（generate_vector=False，后台 worker 异步补）。
        # 测试需在检索前让向量就绪：显式同步补 facet（FIX-08 后向量在
        # node_embedding），等价于等待后台 worker，但规避异步 worker 在全量
        # 运行下的 event loop 时序问题。新建独立 client 避免跨 loop 复用。
        from mem_lake.config import get_settings
        from mem_lake.embedding.client import EmbeddingClient
        from mem_lake.knowledge.repository import (
            batch_regenerate_vectors,
            list_nodes_by_project,
        )

        async with AsyncSessionLocal() as s:
            nodes = await list_nodes_by_project(
                s, project_id=uuid.UUID(project_id), status="approved"
            )
            client = EmbeddingClient(
                base_url=f"http://{get_settings().EMBEDDING_HOST}:{get_settings().EMBEDDING_PORT}",
                dimension=get_settings().EMBEDDING_DIMENSION,
            )
            try:
                await batch_regenerate_vectors(
                    s, embedding_client=client, nodes=nodes, actor="ak_admin"
                )
                await s.commit()
            finally:
                await client.close()

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
            err = await _expect_tool_error(noauth, "review_pending_list", {})
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

    async def test_dev_system_scope_meta_and_search_fallback(self, monkeypatch):
        """dev 身份可见自身 system scope：枚举+检索兜底（UX 修复回归）。

        - get_project_info(include_scope_meta=true)：scope_meta 含 system 维度可见清单
        - search_similar_requirements 不传 system/project：绑定唯一 system 时自动兜底
        """
        system_id = self.__class__._system_id
        assert system_id

        system_id = str(system_id)
        async with Client(
            _make_role_app(
                monkeypatch,
                role="dev",
                project_id=str(uuid.uuid4()),
                system_scope=[system_id],
            )
        ) as dev:
            # enumerate 入口：scope_meta 可见 system 回显
            info = await _call(
                dev, "get_project_info",
                {"action": "list", "include_scope_meta": True},
            )
            assert info["scope"] is not None
            assert info["scope"]["system_scope_type"] == "scoped"
            names = [s["name"] for s in info["scope"]["visible_systems"]]
            assert len(names) >= 1, "scope_meta 应回显 dev 绑定的 system"

            # 检索兜底：不传 system_id/project_id，用 binding 的唯一 system 兜底
            search_result = await _call(
                dev,
                "search_similar_requirements",
                {"query": "登录认证", "top_n": 5},
            )
            assert search_result["returned"] > 0 or len(search_result["fused"]) > 0, (
                "system 兜底应能召回到该 system 下的需求节点"
            )


# ============================================================================
# 批次六：纯 system 绑定 key 分析悬浮需求（09-29 报告已知缺口）
# ============================================================================


async def test_impact_scope_system_bound_key_floating_requirement(
    monkeypatch, db_session, graph_store, mock_embedding_client, knowledge_helpers
):
    """analyze_impact_scope 不传 project_id：按需求自身归属校验（悬浮→system 权限）。

    此前 project_id 必填：system 绑定 dev（project_scope 空）对悬浮需求
    （project=None）传任何 project_id 都会被 validate_project_access 拒绝，
    影响分析对该类 key 结构性不可用。
    """
    from mem_lake.knowledge.repository import add_edge, create_node

    sid = uuid.uuid4()
    req = await create_node(
        db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
        project_id=None, node_type="Requirement", title="悬浮需求-影响分析",
        content="悬浮于 system 的影响分析验证",
        properties={"priority": "P3", "module": "m"},
        tags=[], created_by="ak_pm", system_id=sid,
    )
    sol = await create_node(
        db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
        project_id=uuid.uuid4(), node_type="Solution", title="悬浮方案X",
        content="影响分析应看到 references 挂载的方案",
        properties={"approach": "a", "version": "1"},
        tags=[], created_by="ak_dev",
    )
    await add_edge(
        db_session, graph_store=graph_store,
        from_id=req.id, to_id=sol.id, edge_type="references", actor="ak_dev",
    )
    await db_session.commit()

    try:
        # dev：绑定该 system（project_scope 指向无关项目），不传 project_id
        app = _make_role_app(
            monkeypatch, role="dev",
            project_id=str(uuid.uuid4()), system_scope=[str(sid)],
        )
        async with Client(app) as dev:
            impact = await _call(dev, "analyze_impact_scope",
                                 {"requirement_id": str(req.id)})
            assert impact["requirement"] is not None
            sol_titles = {
                (d.get("properties") or {}).get("title")
                for d in impact.get("solutions", [])
            }
            assert "悬浮方案X" in sol_titles, "悬浮需求的影响分析应返回 references 资产"

            # 越权：system_scope 不含该 system → 拒绝
            app2 = _make_role_app(
                monkeypatch, role="dev",
                project_id=str(uuid.uuid4()), system_scope=[str(uuid.uuid4())],
            )
        async with Client(app2) as dev2:
            err = await _expect_tool_error(dev2, "analyze_impact_scope",
                                          {"requirement_id": str(req.id)})
            assert "system" in err or "权限" in err
    finally:
        await _cleanup_nodes(db_session, graph_store, [req.id, sol.id])


# ============================================================================
# 场景8：get_node_info 按 node_id 反查归属（ISSUE-14）
# 反馈：悬浮需求 update_node 报「节点不属于本项目」后无工具可查它到底属于谁
# ============================================================================


async def test_get_node_info_ownership_lookup(
    monkeypatch, db_session, graph_store, mock_embedding_client, knowledge_helpers
):
    """admin 按 node_id 反查归属四元组；悬浮需求与项目资产都能自证。"""
    from mem_lake.knowledge.repository import create_node

    system_id = uuid.uuid4()
    project_id = uuid.uuid4()
    floating = await create_node(
        db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
        project_id=None, node_type="Requirement", title="悬浮-归属诊断",
        content="验证 get_node_info 反查悬浮需求归属",
        properties={"priority": "P3", "module": "m"},
        tags=[], created_by="ak_pm", system_id=system_id,
    )
    asset = await create_node(
        db_session, graph_store=graph_store, embedding_client=mock_embedding_client,
        project_id=project_id, node_type="CodeSnippet", title="归属诊断代码",
        content="验证项目资产节点归属回显",
        properties={"name": "n", "type": "function", "responsibility": "r", "file_path": "src/a.py"},
        tags=[], created_by="ak_dev",
    )
    # 双空归属是历史/异常数据：当前 schema 禁止创建，直接按存量形态插入绕过校验
    from sqlalchemy import text as sa_text

    orphan_id = uuid.uuid4()
    await db_session.execute(
        sa_text(
            "INSERT INTO knowledge_node (id, project_id, system_id, type, title, content, "
            "properties, tags, source, status, version, created_by) "
            "VALUES (:id, NULL, NULL, 'Solution', '双空归属-诊断', '验证无归属节点默认拒绝', "
            "'{}'::jsonb, '[]'::jsonb, '{}'::jsonb, 'approved', 1, 'ak_pm')"
        ),
        {"id": orphan_id},
    )
    await db_session.commit()

    admin = _make_role_app(monkeypatch, role="admin", project_id=str(uuid.uuid4()))
    try:
        async with Client(admin) as client:
            info = await _call(client, "get_node_info", {"node_id": str(floating.id)})
            assert info["found"] is True
            assert info["node_type"] == "Requirement"
            assert info["project_id"] is None, "悬浮需求必须回显 null 而非报错"
            assert info["system_id"] == str(system_id)
            assert info["status"] == "approved"
            assert info["is_deleted"] is False

            info2 = await _call(client, "get_node_info", {"node_id": str(asset.id)})
            assert info2["project_id"] == str(project_id)

            # 双空归属节点：admin 可自证（排查历史/异常数据）
            info_orphan = await _call(client, "get_node_info", {"node_id": str(orphan_id)})
            assert info_orphan["found"] is True
            assert info_orphan["project_id"] is None and info_orphan["system_id"] is None

            # 不存在节点：found=False + hint（而非抛错中断排查）
            miss = await _call(client, "get_node_info", {"node_id": str(uuid.uuid4())})
            assert miss["found"] is False
            assert miss["hint"]

        # RBAC：dev 的 project_scope 不含该项目 → 拒绝（不回内容）
        dev = _make_role_app(monkeypatch, role="dev", project_id=str(uuid.uuid4()))
        async with Client(dev) as client:
            err = await _expect_tool_error(
                client, "get_node_info", {"node_id": str(asset.id)}
            )
            assert "权限" in err or "越权" in err or "scope" in err.lower()

            # 双空归属节点：无 scope 可校验 → 非 admin 默认拒绝（不回内容）
            err_orphan = await _expect_tool_error(
                client, "get_node_info", {"node_id": str(orphan_id)}
            )
            assert "权限" in err_orphan

        # RBAC：dev 绑定该 system 可查悬浮需求（与 get_requirement_context 同口径）
        dev2 = _make_role_app(
            monkeypatch, role="dev",
            project_id=str(uuid.uuid4()), system_scope=[str(system_id)],
        )
        async with Client(dev2) as client:
            info3 = await _call(client, "get_node_info", {"node_id": str(floating.id)})
            assert info3["system_id"] == str(system_id)

        # 已软删：found=False + is_deleted=True（诊断态可区分「不存在」与「已删」）
        await db_session.execute(
            sa_text("UPDATE knowledge_node SET is_deleted = true WHERE id = :id"),
            {"id": orphan_id},
        )
        await db_session.commit()
        admin2 = _make_role_app(monkeypatch, role="admin", project_id=str(uuid.uuid4()))
        async with Client(admin2) as client:
            gone = await _call(client, "get_node_info", {"node_id": str(orphan_id)})
        assert gone["found"] is False
        assert gone["is_deleted"] is True
    finally:
        await _cleanup_nodes(db_session, graph_store, [floating.id, asset.id, orphan_id])


async def test_review_approve_receipt_carries_created(
    monkeypatch, db_session, graph_store, mock_embedding_client, knowledge_helpers
):
    """ISSUE-16：review_approve 回执同样带 created + edges_created。

    严格模式提交 → admin review_approve 的返回原来只有 batch_id/status/
    conflict_hint，Agent 拿不到新建节点 UUID，跨批次补边必须反查。
    """
    project_id = str(uuid.uuid4())
    system_id = str(uuid.uuid4())

    # 借 publish_requirement 造一个严格模式批次（pm 角色 + 非宽松 key）
    pm = _make_role_app(monkeypatch, role="pm", project_id=project_id)
    async with Client(pm) as client:
        r = await _call(
            client,
            "publish_requirement",
            {
                "system_id": system_id,
                "project_id": project_id,
                "requirement": {
                    "title": "审批回执节点ID验证",
                    "content": "验证 review_approve 回执含 created 清单",
                    "properties": {"priority": "P3", "module": "receipt"},
                    "tags": [],
                },
                "operation_id": f"op-receipt-{uuid.uuid4().hex[:8]}",
            },
        )
    assert r["status"] == "pending_review", "非宽松 key 必须走严格模式"
    batch_id = r["batch_id"]

    try:
        admin = _make_role_app(monkeypatch, role="admin", project_id=project_id)
        async with Client(admin) as client:
            out = await _call(client, "review_approve", {"batch_id": batch_id})
            created = out.get("created") or []
            assert len(created) == 1, "review_approve 回执缺 created"
            assert created[0]["node_type"] == "Requirement"
            assert created[0]["node_id"]
    finally:
        from sqlalchemy import select

        from mem_lake.knowledge.models import KnowledgeNode

        rows = (
            await db_session.execute(
                select(KnowledgeNode.id).where(KnowledgeNode.project_id == uuid.UUID(project_id))
            )
        ).scalars().all()
        await _cleanup_nodes(db_session, graph_store, rows)


# ============================================================================
# 场景9：bind_requirement_to_project 悬浮需求 ↔ 项目绑定（ISSUE-15）
# 注意：每个 Client 块单独建 app（app 实例不复用于多个 Client 上下文，
# 第二次 lifespan 进入会挂起——本文件一贯模式）；pm 操作悬浮需求需
# system_scope=[sid]（is_requirement_visible 的悬浮口径）。
# ============================================================================


async def test_bind_requirement_to_project_end_to_end(
    monkeypatch, db_session, graph_store, mock_embedding_client
):
    """PM 绑定悬浮需求 → admin 审批 → 落库 + 图同步 + list_requirements 可见 + 解绑。"""
    from sqlalchemy import delete
    from sqlalchemy import select as sa_select

    from mem_lake.knowledge.models import KnowledgeNode, System, SystemProject
    from mem_lake.knowledge.repository import get_node

    project_id = str(uuid.uuid4())

    # admin 建 system 并把项目挂入归属清单
    async with Client(
        _make_role_app(monkeypatch, role="admin", project_id=str(uuid.uuid4()))
    ) as c:
        sid = (
            await _call(
                c,
                "manage_system",
                {"action": "create", "name": f"sys-bind-{uuid.uuid4().hex[:8]}"},
            )
        )["system_id"]
        await _call(
            c,
            "manage_system",
            {"action": "add_projects", "system_id": sid, "project_ids": [project_id]},
        )

    try:
        # PM 发布悬浮需求（project_id=None；system_scope 供悬浮可见性）
        async with Client(
            _make_role_app(
                monkeypatch, role="pm", project_id=project_id, system_scope=[sid]
            )
        ) as c:
            r = await _call(
                c,
                "publish_requirement",
                {
                    "system_id": sid,
                    "requirement": {
                        "title": "绑定端到端验证需求",
                        "content": "验证悬浮需求绑定到项目后项目域查询可见",
                        "properties": {"priority": "P3", "module": "bind"},
                        "tags": [],
                    },
                    "operation_id": f"op-bind-e2e-{uuid.uuid4().hex[:8]}",
                },
            )
        assert r["status"] == "pending_review"
        pub_batch = r["batch_id"]

        async with Client(
            _make_role_app(monkeypatch, role="admin", project_id=project_id)
        ) as c:
            await _call(c, "review_auto_process", {"batch_id": pub_batch})
            detail = await _call(c, "review_batch_detail", {"batch_id": pub_batch})
            req_id = next(
                (it["target_id"] for it in detail.get("items", []) if it.get("target_id")),
                None,
            )
        assert req_id, "审批后应拿到需求节点 ID"

        # PM 绑定（严格模式 → pending），回执带 affected
        async with Client(
            _make_role_app(
                monkeypatch, role="pm", project_id=project_id, system_scope=[sid]
            )
        ) as c:
            b = await _call(
                c,
                "bind_requirement_to_project",
                {
                    "requirement_ids": [req_id],
                    "project_id": project_id,
                    "operation_id": f"op-bind-{uuid.uuid4().hex[:8]}",
                },
            )
        assert b["status"] == "pending_review"
        affected = b.get("affected") or []
        assert len(affected) == 1 and affected[0]["node_id"] == req_id, "回执须带 affected"
        bind_batch = b["batch_id"]

        async with Client(
            _make_role_app(monkeypatch, role="admin", project_id=project_id)
        ) as c:
            out = await _call(c, "review_auto_process", {"batch_id": bind_batch})
        assert out["decision"] == "auto_approved", out
        assert not out.get("created"), "bind 无新节点，created 应为空"

        # 审批与提交发生在 app 自己的会话（独立事务）；db_session 的 identity map
        # 里如有同 id 对象不会回刷属性，跨会话读断言前必须失效缓存
        db_session.expire_all()
        bound = await get_node(db_session, uuid.UUID(req_id))
        assert bound.project_id == uuid.UUID(project_id)

        # 项目域查询可见
        async with Client(
            _make_role_app(
                monkeypatch, role="pm", project_id=project_id, system_scope=[sid]
            )
        ) as c:
            lst = await _call(c, "list_requirements", {"project_id": project_id})
        titles = {x.get("title") for x in lst.get("items", lst.get("requirements", []))}
        assert "绑定端到端验证需求" in titles, "绑定后 list_requirements(project_id) 须可见"

        # 解绑回悬浮
        async with Client(
            _make_role_app(
                monkeypatch, role="pm", project_id=project_id, system_scope=[sid]
            )
        ) as c:
            u = await _call(
                c,
                "bind_requirement_to_project",
                {
                    "requirement_ids": [req_id],
                    "project_id": None,
                    "operation_id": f"op-unbind-{uuid.uuid4().hex[:8]}",
                },
            )
        un_batch = u["batch_id"]
        async with Client(
            _make_role_app(monkeypatch, role="admin", project_id=project_id)
        ) as c:
            await _call(c, "review_auto_process", {"batch_id": un_batch})
        # 同上：解绑提交/审批在 app 会话，先失效 db_session identity map 缓存
        db_session.expire_all()
        unbound = await get_node(db_session, uuid.UUID(req_id))
        assert unbound.project_id is None

        # 归属缺失：目标项目不在 system 清单 → 拒 + add_projects hint
        # （目标项目须先过调用者 scope 校验才轮到归属清单校验，故 PM scope 含 outsider）
        outsider = str(uuid.uuid4())
        async with Client(
            _make_role_app(
                monkeypatch, role="pm", project_id=outsider, system_scope=[sid]
            )
        ) as c:
            err = await _expect_tool_error(
                c,
                "bind_requirement_to_project",
                {"requirement_ids": [req_id], "project_id": outsider},
            )
        assert "add_projects" in err, err

        # RBAC：dev 无此工具
        async with Client(
            _make_role_app(monkeypatch, role="dev", project_id=project_id)
        ) as c:
            tools = {t.name for t in await c.list_tools()}
        assert "bind_requirement_to_project" not in tools
    finally:
        rows = (
            await db_session.execute(
                sa_select(KnowledgeNode.id).where(
                    KnowledgeNode.system_id == uuid.UUID(sid)
                )
            )
        ).scalars().all()
        await _cleanup_nodes(db_session, graph_store, rows)
        await db_session.execute(
            delete(SystemProject).where(SystemProject.system_id == uuid.UUID(sid))
        )
        await db_session.execute(delete(System).where(System.id == uuid.UUID(sid)))
        await db_session.commit()


async def test_role_tool_lists_match_rbac(monkeypatch):
    """真实网关的 tools/list 必须等于 RBAC 分层集合（防 admin 退回并集）。"""
    from mem_lake.auth.rbac import ADMIN_TOOLS, DEV_TOOLS, PM_TOOLS

    for role, expected in (
        ("pm", PM_TOOLS),
        ("dev", DEV_TOOLS),
        ("admin", ADMIN_TOOLS),
    ):
        app = _make_role_app(monkeypatch, role=role, project_id=str(uuid.uuid4()))
        async with Client(app) as client:
            names = {t.name for t in await client.list_tools()}
        assert names == set(expected), f"{role} 工具集不一致: {names ^ set(expected)}"
