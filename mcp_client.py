# -*- coding: utf-8 -*-
"""MCP 客户端管理器 - 连接和管理多个 MCP 服务器"""
import asyncio
import json
import logging
from typing import Dict, List, Any, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
try:
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
except ImportError:
    # 旧版本 mcp 的兼容（< 1.9.x）
    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        streamable_http_client = None
from contextlib import AsyncExitStack

logger = logging.getLogger(__name__)


class MCPClientManager:
    """管理多个 MCP 服务器的连接和工具调用"""
    
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.server_params: Dict[str, StdioServerParameters] = {}
        self.exit_stack = AsyncExitStack()
        self._tools_cache: Dict[str, List[Any]] = {}
    
    async def connect_server(self, name: str, command: str, args: List[str] = None, env: Dict[str, str] = None):
        """连接到一个 MCP 服务器
        
        Args:
            name: 服务器名称（用于标识）
            command: 服务器启动命令（如 "python"）
            args: 命令参数（如 ["server.py"]）
            env: 环境变量
        """
        if args is None:
            args = []
        
        if command in ("sse", "http", "url"):
            # URL 连接 (args[0] 是 URL)
            # 优先使用 streamable HTTP，失败后回退到 SSE，提高兼容性。
            url = args[0]
            timeout_seconds = 10.0
            transport = None
            last_error = None

            if command in ("http", "url"):
                logger.info("[MCPClient] 连接 HTTP MCP 服务器: %s", url)
                try:
                    transport = await asyncio.wait_for(
                        self.exit_stack.enter_async_context(streamable_http_client(url)),
                        timeout=timeout_seconds
                    )
                except Exception as e:
                    last_error = e
                    logger.warning("[MCPClient] HTTP MCP 连接失败，尝试 SSE 回退: %s", e)

            if transport is None:
                logger.info("[MCPClient] 连接 SSE 服务器: %s", url)
                try:
                    transport = await asyncio.wait_for(
                        self.exit_stack.enter_async_context(sse_client(url)),
                        timeout=timeout_seconds
                    )
                except asyncio.TimeoutError as e:
                    last_error = e
                    logger.error("[MCPClient] 连接 URL 服务器超时: %s", url)
                    return
                except Exception as e:
                    last_error = e
                    logger.error("[MCPClient] 连接 URL 服务器失败: %s, 错误: %s", url, e)
                    return

            if transport is None:
                logger.error("[MCPClient] 无法建立连接: %s, 错误: %s", url, last_error)
                return
        else:
            # Stdio 连接
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=env
            )
            self.server_params[name] = server_params
            
            transport = await self.exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            
        read, write = transport
        
        session = await self.exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        
        # 初始化
        await session.initialize()
        
        self.sessions[name] = session
        logger.info("[MCPClient] 已连接到 MCP 服务器: %s", name)
        
        # 缓存工具列表
        await self._refresh_tools(name)
    
    async def _refresh_tools(self, server_name: str):
        """刷新服务器的工具列表"""
        session = self.sessions.get(server_name)
        if not session:
            return
        
        tools_result = await session.list_tools()
        self._tools_cache[server_name] = tools_result.tools
        logger.info("[MCPClient] %s 提供 %d 个工具", server_name, len(tools_result.tools))
    
    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有 MCP 服务器的工具定义（转换为 OpenAI Function Calling 格式）"""
        all_tools = []
        
        for server_name, tools in self._tools_cache.items():
            for tool in tools:
                # 转换 MCP Tool Schema 为 OpenAI Function Schema
                openai_tool = {
                    "type": "function",
                    "function": {
                        "name": f"mcp_{server_name}_{tool.name}",  # 添加前缀避免冲突
                        "description": tool.description,
                        "parameters": tool.inputSchema,
                    }
                }
                all_tools.append(openai_tool)
        
        return all_tools
    
    def _parse_mcp_tool_name(self, tool_name: str):
        """解析 MCP 工具名称，返回 (server_name, original_tool_name)
        
        工具名称格式：mcp_{server_name}_{original_tool_name}
        由于 server_name 可能包含下划线（如 sqlite_db），
        需要匹配已注册的服务器名称来正确分割。
        """
        if not tool_name.startswith("mcp_"):
            return None, None
        
        remainder = tool_name[4:]  # 去掉 "mcp_" 前缀
        
        # 按已注册的服务器名称匹配
        for server_name in self.sessions:
            prefix = server_name + "_"
            if remainder.startswith(prefix):
                original_tool_name = remainder[len(prefix):]
                return server_name, original_tool_name
        
        return None, None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用 MCP 工具
        
        Args:
            tool_name: 工具名称（格式：mcp_{server_name}_{original_tool_name}）
            arguments: 工具参数
        
        Returns:
            工具执行结果（JSON 字符串）
        """
        server_name, original_tool_name = self._parse_mcp_tool_name(tool_name)
        
        if not server_name:
            return json.dumps({"error": f"无法解析 MCP 工具名称: {tool_name}"}, ensure_ascii=False)
        
        session = self.sessions.get(server_name)
        if not session:
            return json.dumps({"error": f"MCP 服务器 '{server_name}' 未连接"}, ensure_ascii=False)
        
        try:
            # 调用工具
            result = await session.call_tool(original_tool_name, arguments)
            
            # 提取文本内容
            if result.content:
                text_parts = [c.text for c in result.content if hasattr(c, 'text')]
                return "\n".join(text_parts)
            else:
                return json.dumps({"result": "success", "data": None}, ensure_ascii=False)
        
        except Exception as e:
            logger.warning("[MCPClient] 工具调用失败: %s", e)
            return json.dumps({"error": f"MCP 工具调用失败: {str(e)}"}, ensure_ascii=False)
    
    def get_tool_info(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """获取工具信息"""
        server_name, original_tool_name = self._parse_mcp_tool_name(tool_name)
        
        if not server_name:
            return None
        
        tools = self._tools_cache.get(server_name, [])
        for tool in tools:
            if tool.name == original_tool_name:
                return {
                    "server": server_name,
                    "name": tool.name,
                    "description": tool.description,
                    "schema": tool.inputSchema,
                }
        return None
    
    async def disconnect_all(self):
        """断开所有服务器连接"""
        await self.exit_stack.aclose()
        self.sessions.clear()
        self._tools_cache.clear()
        logger.info("[MCPClient] 已断开所有 MCP 服务器连接")


# ========== 同步包装器（供非异步代码使用）==========

class MCPClientWrapper:
    """MCP 客户端的同步包装器
    
    因为 Agent 主循环可能是同步的，这个包装器提供同步接口
    """
    
    def __init__(self):
        self.loop = None
        self.manager = None
        self._initialized = False
    
    def initialize(self, server_configs: List[Dict[str, Any]]):
        """初始化并连接所有 MCP 服务器
        
        Args:
            server_configs: 服务器配置列表
                [
                    {
                        "name": "sqlite_db",
                        "command": "python",
                        "args": ["mcp_servers/sqlite_db_mcp/server.py"]
                    },
                    ...
                ]
        """
        if self._initialized:
            return
        
        # 创建事件循环
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        
        # 创建管理器
        self.manager = MCPClientManager()
        
        # 连接所有服务器
        async def connect_all():
            for config in server_configs:
                try:
                    await self.manager.connect_server(
                        name=config["name"],
                        command=config["command"],
                        args=config.get("args", []),
                        env=config.get("env")
                    )
                except Exception as e:
                    logging.error(f"[MCPClient] 连接服务器 '{config['name']}' 失败: {e}")
        
        self.loop.run_until_complete(connect_all())
        self._initialized = True
    
    def get_all_tools(self) -> List[Dict[str, Any]]:
        """获取所有工具定义（同步）"""
        if not self._initialized:
            return []
        return self.loop.run_until_complete(self.manager.get_all_tools())
    
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """调用工具（同步）"""
        if not self._initialized:
            return json.dumps({"error": "MCP 客户端未初始化"}, ensure_ascii=False)
        return self.loop.run_until_complete(self.manager.call_tool(tool_name, arguments))
    
    def cleanup(self):
        """清理资源"""
        if self._initialized and self.manager:
            try:
                self.loop.run_until_complete(self.manager.disconnect_all())
            except RuntimeError as e:
                # 在部分 Python/anyio 版本组合下，stdio 关闭阶段可能抛出 cancel scope 错误。
                # 这是清理阶段异常，不影响主流程结果，降级为 warning。
                logger.warning("[MCPClient] 清理阶段出现可忽略异常: %s", e)
            except Exception as e:
                logger.warning("[MCPClient] 清理阶段出现异常: %s", e)
            self._initialized = False
