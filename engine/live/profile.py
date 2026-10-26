from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ClientProfile:
    profile_id: str
    process_name: str
    executable_path: Path
    sha256: str
    file_version: str
    architecture: str
    locators: dict[str, Any]
    known_players: dict[str, dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> "ClientProfile":
        data = json.loads(path.read_text(encoding="utf-8"))
        client = data["client"]
        return cls(
            profile_id=str(data["profile_id"]),
            process_name=str(client["process_name"]),
            executable_path=Path(client["executable_path"]),
            sha256=str(client["sha256"]).upper(),
            file_version=str(client.get("file_version", "")),
            architecture=str(client.get("architecture", "")),
            locators=dict(data.get("locators", {})),
            known_players={
                str(name): {str(key): str(value) for key, value in details.items()}
                for name, details in data.get("known_players", {}).items()
                if isinstance(details, dict)
            },
        )

    def validate_executable(self) -> tuple[bool, str]:
        if not self.executable_path.exists():
            return False, f"未找到客户端：{self.executable_path}"
        digest = hashlib.sha256(self.executable_path.read_bytes()).hexdigest().upper()
        if digest != self.sha256:
            return False, f"客户端指纹不匹配：{digest}"
        return True, "客户端版本验证通过"

    @property
    def calibrated(self) -> bool:
        return bool(self.locators.get("combat_event_source"))
