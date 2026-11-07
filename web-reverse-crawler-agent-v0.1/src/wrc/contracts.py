from dataclasses import dataclass, asdict
from typing import Any

@dataclass
class Candidate:
    url: str
    method: str
    status: int
    resource_type: str
    content_type: str
    sample_keys: list[str]

@dataclass
class Article:
    id: str
    title: str
    url: str
    published_at: str

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
