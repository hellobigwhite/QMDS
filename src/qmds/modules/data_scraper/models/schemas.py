from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Platform(str, Enum):
    SHOPIFY = "shopify"
    WOOCOMMERCE = "woocommerce"
    MAGENTO = "magento"
    BIGCOMMERCE = "bigcommerce"
    UNKNOWN = "unknown"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Store:
    url: str
    domain: str
    platform: Platform = Platform.UNKNOWN
    product_count: int = 0
    category: str = ""
    discovered_at: datetime = field(default_factory=datetime.utcnow)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Product:
    source_url: str
    title: str
    handle: str = ""
    sku: str = ""
    price: float = 0.0
    compare_at_price: Optional[float] = None
    currency: str = "USD"
    body_html: str = ""
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    product_type: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)
    category: str = ""
    scraped_at: datetime = field(default_factory=datetime.utcnow)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScrapeTask:
    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    source: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    results: list[Product] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
