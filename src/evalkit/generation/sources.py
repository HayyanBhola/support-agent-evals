"""Ground truth for generated cases, read straight from the agent snapshot's data files.

Labels and gold facts come from these files and the system rules, never from the agent's
behaviour: products from ``data/products/products.json``, orders from
``database/seed_data.sql``, policy text from the chunk export (``datasets/agent_chunks.jsonl``).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from evalkit.importers.chunks import ChunkRecord
from evalkit.io import read_jsonl
from evalkit.snapshot_router import DEFAULT_SNAPSHOT

DEFAULT_CHUNKS: Final = Path("datasets") / "agent_chunks.jsonl"
_ORDER_INSERT = re.compile(r"SELECT\s+'(GC\d{5})',\s*customer_id,\s*'(\w+)',\s*([\d.]+)")


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    price: float
    currency: str
    ingredients: tuple[str, ...]
    stock_quantity: int
    availability: str


@dataclass(frozen=True)
class Order:
    order_id: str
    status: str
    total_amount: float


@dataclass(frozen=True)
class Sources:
    chunks: tuple[ChunkRecord, ...]
    products: tuple[Product, ...]
    orders: tuple[Order, ...]

    def chunk(self, chunk_id: str) -> ChunkRecord:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        raise KeyError(chunk_id)

    @property
    def kb_text(self) -> str:
        """Every chunk's text, lower-cased (for "is this topic in the knowledge base?")."""
        return "\n".join(c.text for c in self.chunks).lower()


def load_products(snapshot_dir: Path = DEFAULT_SNAPSHOT) -> tuple[Product, ...]:
    path = snapshot_dir / "data" / "products" / "products.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return tuple(
        Product(
            product_id=p["product_id"],
            name=p["product_name"],
            price=float(p["price"]),
            currency=p["currency"],
            ingredients=tuple(p["ingredients"]),
            stock_quantity=int(p["stock_quantity"]),
            availability=p["availability"],
        )
        for p in data["products"]
    )


def load_orders(snapshot_dir: Path = DEFAULT_SNAPSHOT) -> tuple[Order, ...]:
    """The seed orders: ``INSERT INTO orders ... SELECT 'GC10241', customer_id, 'shipped', ..``."""
    sql = (snapshot_dir / "database" / "seed_data.sql").read_text(encoding="utf-8")
    orders = tuple(Order(m[0], m[1], float(m[2])) for m in _ORDER_INSERT.findall(sql))
    if not orders:
        raise ValueError("no orders found in seed_data.sql")
    return orders


def load_sources(
    snapshot_dir: Path = DEFAULT_SNAPSHOT, chunks_path: Path = DEFAULT_CHUNKS
) -> Sources:
    return Sources(
        chunks=tuple(read_jsonl(chunks_path, ChunkRecord)),
        products=load_products(snapshot_dir),
        orders=load_orders(snapshot_dir),
    )
