"""
Клиенты для внешних API.

Содержит:
- claude.py: ClaudeClient для работы с Claude API
- mcp.py: MCPClient для работы с MCP серверами
"""

from .claude import ClaudeClient, claude
from .mcp import MCPClient, mcp_guides, mcp_knowledge, mcp
from .digital_twin import DigitalTwinClient, digital_twin

__all__ = [
    'ClaudeClient',
    'claude',
    'MCPClient',
    'mcp_guides',
    'mcp_knowledge',
    'mcp',
    'DigitalTwinClient',
    'digital_twin',
]
