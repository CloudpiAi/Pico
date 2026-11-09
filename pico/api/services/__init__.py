"""API Services module.

Consolidated service layer for Pico Agent API.
"""

# Core services
from pico.api.services.datus_service_cache import DatusServiceCache

# Lazy imports - services are imported only when needed by routes
# This avoids circular dependencies and import errors

__all__ = [
    "DatusServiceCache",
]


def __getattr__(name):
    """Lazy import of services on demand."""
    if name == "DatusService":
        from pico.api.services.datus_service import DatusService

        return DatusService
    elif name == "ChatService":
        from pico.api.services.chat_service import ChatService

        return ChatService
    elif name == "ChatTaskManager":
        from pico.api.services.chat_task_manager import ChatTaskManager

        return ChatTaskManager
    elif name == "ChatTask":
        from pico.api.services.chat_task_manager import ChatTask

        return ChatTask
    elif name == "CLIService":
        from pico.api.services.cli_service import CLIService

        return CLIService
    elif name == "DatasourceService":
        from pico.api.services.database_service import DatasourceService

        return DatasourceService
    elif name == "ExplorerService":
        from pico.api.services.explorer_service import ExplorerService

        return ExplorerService
    elif name == "MCPService":
        from pico.api.services.mcp_service import MCPService

        return MCPService
    elif name == "KbService":
        from pico.api.services.kb_service import KbService

        return KbService
    elif name == "action_to_sse_event":
        from pico.api.services.action_sse_converter import action_to_sse_event

        return action_to_sse_event
    elif name == "AgentService":
        from pico.api.services.agent_service import AgentService

        return AgentService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
