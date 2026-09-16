"""
A2UI Protocol Definitions (A2UI v0.9+ / v1.0 Spec).
Quy dinh cac kieu thong diep chuan de Agent va Client trao doi giao dien.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
import json
import uuid


@dataclass
class CreateSurface:
    surfaceId: str
    catalogId: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"createSurface": asdict(self)}


@dataclass
class UpdateComponents:
    surfaceId: str
    components: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {"updateComponents": asdict(self)}


@dataclass
class UpdateDataModel:
    surfaceId: str
    value: Dict[str, Any]
    path: str = "/"

    def to_dict(self) -> Dict[str, Any]:
        return {"updateDataModel": asdict(self)}


@dataclass
class DeleteSurface:
    surfaceId: str

    def to_dict(self) -> Dict[str, Any]:
        return {"deleteSurface": asdict(self)}


@dataclass
class CallAgentFunction:
    name: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    functionCallId: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        return {"callAgentFunction": asdict(self)}


@dataclass
class CallRendererFunction:
    name: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    functionCallId: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        return {"callRendererFunction": asdict(self)}


@dataclass
class AgentFunctionResponse:
    functionCallId: str
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"agentFunctionResponse": asdict(self)}


class A2UIMessage:
    """Dong goi goi tin thong diep A2UI co dinh version."""
    def __init__(self, payload: Any, version: str = "v1.0"):
        self.version = version
        self.payload = payload

    def to_dict(self) -> Dict[str, Any]:
        data = {"version": self.version}
        if hasattr(self.payload, "to_dict"):
            data.update(self.payload.to_dict())
        elif isinstance(self.payload, dict):
            data.update(self.payload)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def parse_a2ui_message(raw_data: Any) -> Dict[str, Any]:
    """Parse va validate co ban thong diep A2UI tu dict hoac json string."""
    if isinstance(raw_data, str):
        data = json.loads(raw_data)
    elif isinstance(raw_data, dict):
        data = dict(raw_data)
    else:
        raise ValueError("Invalid A2UI message payload type")

    # Kiem tra xem co key thong diep hop le khong
    valid_keys = {
        "createSurface", "updateComponents", "updateDataModel",
        "deleteSurface", "callAgentFunction", "callRendererFunction",
        "agentFunctionResponse"
    }
    keys_in_data = set(data.keys()) - {"version"}
    if not (keys_in_data & valid_keys):
        raise ValueError(f"Khong tim thay khoa giao thuc A2UI hop le trong: {list(data.keys())}")

    return data
