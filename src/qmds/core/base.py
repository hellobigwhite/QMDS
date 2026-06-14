from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ScrapeResult:
    source: str
    data: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total_found: int = 0
    total_scraped: int = 0


class BaseScraper(ABC):
    """所有爬取器的基类"""

    def __init__(self, name: str = ""):
        self.name = name or self.__class__.__name__

    @abstractmethod
    def scrape(self, **kwargs) -> ScrapeResult:
        ...

    def validate(self, data: dict[str, Any]) -> bool:
        return bool(data)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.name}>"
