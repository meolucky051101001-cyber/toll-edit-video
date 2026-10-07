"""
A2UI (Agent-to-User Interface) Extension Package for Tool V1.
Module doc lap, ho tro sinh giao dien dong tu AI Agent theo chuan A2UI mo.
"""

from .protocol import A2UIMessage, CreateSurface, UpdateComponents, UpdateDataModel, DeleteSurface, CallAgentFunction
from .catalog import MEDIA_COMPONENT_CATALOG
from .agent_generator import generate_a2ui_for_video
from .mock_agent import get_preset_scenario

__all__ = [
    "A2UIMessage",
    "CreateSurface",
    "UpdateComponents",
    "UpdateDataModel",
    "DeleteSurface",
    "CallAgentFunction",
    "MEDIA_COMPONENT_CATALOG",
    "generate_a2ui_for_video",
    "get_preset_scenario",
]
