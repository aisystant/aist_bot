"""
Клиенты для внешних API.

Содержит:
- claude.py: ClaudeClient для работы с Claude API
- mcp.py: MCPClient для работы с Knowledge MCP (SYS.017)
- discourse.py: DiscourseClient для публикации на systemsworld.club
"""

from .claude import ClaudeClient, claude
from .mcp import MCPClient, mcp_knowledge
from .digital_twin import DigitalTwinClient, digital_twin
from .discourse import DiscourseClient, discourse

__all__ = [
    'ClaudeClient',
    'claude',
    'MCPClient',
    'mcp_knowledge',
    'DigitalTwinClient',
    'digital_twin',
    'DiscourseClient',
    'discourse',
]
