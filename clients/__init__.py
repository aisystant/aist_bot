"""
Клиенты для внешних API.

Содержит:
- claude.py: ClaudeClient для работы с Claude API
- mcp.py: MCPClient для работы с Knowledge MCP (SYS.017) — legacy, будет удалён
- gateway_mcp.py: GatewayMCPClient — единый клиент для Gateway (WP-209)
- digital_twin.py: DigitalTwinClient — legacy, замещается Gateway (WP-209)
- discourse.py: DiscourseClient для публикации на systemsworld.club
- aisystant.py: AisystantClient для расписания, оплаты, подписок (WP-79)
"""

from .claude import ClaudeClient, claude
from .mcp import MCPClient, mcp_knowledge
from .digital_twin import DigitalTwinClient, digital_twin
from .gateway_mcp import GatewayMCPClient, gateway_mcp
from .discourse import DiscourseClient, discourse
from .aisystant import AisystantClient, aisystant

__all__ = [
    'ClaudeClient',
    'claude',
    'MCPClient',
    'mcp_knowledge',
    'DigitalTwinClient',
    'digital_twin',
    'GatewayMCPClient',
    'gateway_mcp',
    'DiscourseClient',
    'discourse',
    'AisystantClient',
    'aisystant',
]
