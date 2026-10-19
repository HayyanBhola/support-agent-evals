"""Run the snapshot's agent/router.py offline, as a cross-check for expected routes.

router.py is plain regex rules (it imports only re, dataclasses and difflib), so it can be
loaded straight from agent_snapshot/ without the agent's database, LLM or network. Products
come from agent_snapshot/data/products/products.json (which seed_data.sql mirrors), renamed
to the ``name`` key the database returns.

This predicts only the router's first decision: the agent's memory step can still change
the route when a message has history, so history cases are not predicted.
"""

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_SNAPSHOT = Path("agent_snapshot")


def load_products(snapshot_dir: Path = DEFAULT_SNAPSHOT) -> list[dict[str, Any]]:
    path = snapshot_dir / "data" / "products" / "products.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return [{"product_id": p["product_id"], "name": p["product_name"]} for p in data["products"]]


def load_router(snapshot_dir: Path = DEFAULT_SNAPSHOT) -> Callable[[str], str]:
    """A function message -> route name, using the snapshot's router rules."""
    path = snapshot_dir / "agent" / "router.py"
    spec = importlib.util.spec_from_file_location("agent_snapshot_router", path)
    if spec is None or spec.loader is None:  # pragma: no cover - only for a broken path
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    products = load_products(snapshot_dir)

    def predict(message: str) -> str:
        return str(module.route_message(message, products).route)

    return predict
