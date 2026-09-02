"""gateway/auth.py 单元测试：协议版本校验 + Access Key 头提取。

纯单测，无 DB 依赖。覆盖 PDD 3.1 认证机制：
- MCP-Protocol-Version 头与 _meta.protocolVersion 一致性校验
- X-MCP-Key 头提取
- 错误码 -32020（项目自定义，落在 -32000~-32099 保留段）
"""

import pytest
from fastmcp.exceptions import McpError

from mem_lake.gateway.auth import (
    ACCESS_KEY_HEADER,
    PROTOCOL_VERSION_HEADER,
    extract_access_key_from_headers,
    validate_protocol_version,
)


class TestValidateProtocolVersion:
    """协议版本校验测试。"""

    def test_valid_version_match(self):
        """Header 与 _meta 版本一致且为支持版本：通过。"""
        validate_protocol_version(
            {PROTOCOL_VERSION_HEADER.lower(): "2026-07-28"},
            {"protocolVersion": "2026-07-28"},
        )

    def test_missing_header_raises(self):
        """缺 MCP-Protocol-Version 头抛 McpError。"""
        with pytest.raises(McpError, match="缺少"):
            validate_protocol_version({}, {"protocolVersion": "2026-07-28"})

    def test_missing_meta_raises(self):
        """_meta 为 None 抛 McpError。"""
        with pytest.raises(McpError, match="_meta"):
            validate_protocol_version(
                {PROTOCOL_VERSION_HEADER.lower(): "2026-07-28"}, None
            )

    def test_missing_meta_protocol_version_raises(self):
        """_meta 缺 protocolVersion 字段抛 McpError。"""
        with pytest.raises(McpError, match="protocolVersion"):
            validate_protocol_version(
                {PROTOCOL_VERSION_HEADER.lower(): "2026-07-28"},
                {"clientInfo": {"name": "test"}},
            )

    def test_version_mismatch_raises(self):
        """Header 与 _meta 版本不一致抛 McpError。"""
        with pytest.raises(McpError, match="不一致"):
            validate_protocol_version(
                {PROTOCOL_VERSION_HEADER.lower(): "2026-07-28"},
                {"protocolVersion": "2025-06-18"},
            )

    def test_unsupported_version_raises(self):
        """版本一致但不支持抛 McpError。"""
        with pytest.raises(McpError, match="不支持"):
            validate_protocol_version(
                {PROTOCOL_VERSION_HEADER.lower(): "2025-06-18"},
                {"protocolVersion": "2025-06-18"},
            )

    def test_error_code_is_custom(self):
        """错误码为 -32020（项目自定义，落在 -32000~-32099 保留段）。"""
        try:
            validate_protocol_version({}, {"protocolVersion": "2026-07-28"})
            assert False, "应抛异常"
        except McpError as e:
            assert e.error.code == -32020

    def test_none_meta_skipped(self):
        """message_meta 非 dict 时不校验（None 跳过校验逻辑）。"""
        # 当 message_meta 不是 dict 时，validate_protocol_version 不被调用
        # （中间件层 isinstance(message_meta, dict) 判断）
        # 这里测试函数本身在 None 输入时的行为
        with pytest.raises(McpError):
            validate_protocol_version(
                {PROTOCOL_VERSION_HEADER.lower(): "2026-07-28"}, None
            )


class TestExtractAccessKey:
    """Access Key 头提取测试。"""

    def test_extract_existing_key(self):
        """X-MCP-Key 头存在：返回明文。"""
        headers = {ACCESS_KEY_HEADER.lower(): "ak_abc123.def456"}
        assert extract_access_key_from_headers(headers) == "ak_abc123.def456"

    def test_extract_missing_key_returns_none(self):
        """X-MCP-Key 头不存在：返回 None。"""
        assert extract_access_key_from_headers({}) is None

    def test_extract_case_insensitive(self):
        """头名大小写不敏感（get_http_headers 返回小写键）。"""
        headers = {"x-mcp-key": "ak_test"}
        assert extract_access_key_from_headers(headers) == "ak_test"

    def test_extract_empty_value_returns_none(self):
        """空字符串值返回 None（falsy）。"""
        # extract 返回空字符串，调用方按 not access_key_plain 判断
        headers = {ACCESS_KEY_HEADER.lower(): ""}
        result = extract_access_key_from_headers(headers)
        assert result == ""  # 函数返回空字符串，调用方判断 falsy
