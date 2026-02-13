"""
Клиенты для внешних API.

Содержит:
- claude.py: ClaudeClient для работы с Claude API
- mcp.py: MCPClient для работы с Knowledge MCP (SYS.017)
"""

from .claude import ClaudeClient, claude
from .mcp import MCPClient, mcp_knowledge
from .digital_twin import DigitalTwinClient, digital_twin

__all__ = [
    'ClaudeClient',
    'claude',
    'MCPClient',
    'mcp_knowledge',
    'DigitalTwinClient',
    'digital_twin',
]
