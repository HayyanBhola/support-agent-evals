"""``db_facts`` (Day 15): the reply against the case's gold_db_facts, deterministic.

Only cases with gold_db_facts are scored. Each applicable check is recorded in details:

* **ingredients** (a list, no single ``ingredient``): every gold ingredient appears, and no
  ingredient that only other catalog products have;
* **contains** (``ingredient`` + ``contains``): the yes/no reading of the reply matches;
* **availability** / **stock_quantity**: "in stock" vs "out of stock" exactly, and a stated
  unit count must equal the gold count;
* **status** (orders): the gold status word appears and no other status word;
* **price**: a stated amount within 0.01 of the gold price, in USD ("$" or "USD"); a
  ``claimed_price`` the customer made up must not be confirmed;
* **not_found** (``product_exists`` / ``exists`` false): the reply says it can't be found and
  states no price or status;
* **same_product**: the reply doesn't answer about a different catalog product (the
  Vitamin C -> Niacinamide case): naming another product without naming the one asked. A
  reply naming MENU_SIZE or more products (the catalog as a clarifying menu) doesn't count.

The catalog (product names and ingredient lists) comes from the dataset's gold facts and
the database rows the run returned (``catalog_from``).
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from evalkit.schemas import AgentResponse, MetricResult, TestCase

DB_FACTS: Final = "db_facts"
PRICE_TOLERANCE: Final = 0.01
MENU_SIZE: Final = 3  # a reply naming this many catalog products is a menu, not an answer
_AMOUNT = re.compile(r"\$\s?(\d+(?:[.,]\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s?USD", re.IGNORECASE)
_OUT = re.compile(r"\bout of stock\b|\bnot (?:currently )?in stock\b|\bsold out\b", re.I)
_IN = re.compile(r"\bin stock\b|\bavailable\b", re.I)
_UNITS = re.compile(r"\b(\d+)\s+(?:units?|left|in stock|available)\b", re.I)
_NOT_FOUND = re.compile(
    r"\b(?:couldn't|could not|can't|cannot|can not|unable to) find\b|\bno (?:such )?"
    r"(?:product|order)\b|\bdoesn't exist\b|\bdoes not exist\b|\bnot found\b"
    r"|\bdon't (?:have|carry|sell)\b|\bdo not (?:have|carry|sell)\b",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:no|not|doesn't|does not|isn't|is not|free of|without|don't|do not)\b", re.I
)
STATUSES: Final = frozenset(
    {"pending", "processing", "shipped", "delivered", "cancelled", "canceled", "returned",
     "refunded", "in transit", "out for delivery"}
)  # fmt: skip


@dataclass(frozen=True)
class Catalog:
    """Known product names and their ingredient lists (lower-case)."""

    products: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def all_ingredients(self) -> frozenset[str]:
        return frozenset().union(*self.products.values()) if self.products else frozenset()

    def named_in(self, text: str) -> list[str]:
        low = text.lower()
        return sorted(name for name in self.products if name in low)


def _rows(details: Any) -> list[dict[str, Any]]:
    if isinstance(details, list):
        return [d for d in details if isinstance(d, dict)]
    return []


def catalog_from(cases: Iterable[TestCase], responses: Iterable[AgentResponse | None]) -> Catalog:
    products: dict[str, set[str]] = {}
    for case in cases:
        facts = case.gold_db_facts
        name = facts.get("product_name")
        listed = facts.get("ingredients", [])
        if isinstance(name, str) and facts.get("product_exists") is not False:
            products.setdefault(name.lower(), set()).update(
                i.lower()
                for i in (listed if isinstance(listed, list) else [])
                if isinstance(i, str)
            )
    for resp in responses:
        for row in _rows(resp.details if resp else None):
            name = row.get("name")
            if isinstance(name, str):
                products.setdefault(name.lower(), set()).update(
                    i.lower() for i in row.get("ingredients", []) if isinstance(i, str)
                )
    return Catalog({k: frozenset(v) for k, v in sorted(products.items())})


def amounts(text: str) -> list[float]:
    out = []
    for m in _AMOUNT.finditer(text):
        raw = (m.group(1) or m.group(2)).replace(",", ".")
        out.append(float(raw))
    return out


def units_of(text: str) -> list[int]:
    """Unit counts stated in a reply ("42 units", "3 left")."""
    return [int(n) for n in _UNITS.findall(text)]


def _says_contains(reply: str, ingredient: str, full_list: Iterable[str] = ()) -> bool | None:
    """True / False from the reply's yes-no reading; None if it can't be read. A reply that
    lists the product's whole ingredient list without the ingredient reads as "no"."""
    low = reply.lower()
    if ingredient.lower() not in low:
        listed = [i.lower() for i in full_list]
        if _NEGATION.search(low) or (listed and all(i in low for i in listed)):
            return False
        return None
    head = low.split(ingredient.lower())[0]
    return not (low.startswith("no") or _NEGATION.search(head))


def checks(case: TestCase, reply: str, catalog: Catalog) -> dict[str, bool]:
    facts: Mapping[str, Any] = case.gold_db_facts
    low = reply.lower()
    found: dict[str, bool] = {}
    asked = facts.get("product_name")
    asked_low = asked.lower() if isinstance(asked, str) else None
    if facts.get("product_exists") is False or facts.get("exists") is False:
        states_status = any(re.search(rf"\b{re.escape(s)}\b", low) for s in STATUSES)
        found["not_found"] = (
            bool(_NOT_FOUND.search(reply))
            and not amounts(reply)
            and not (facts.get("exists") is False and states_status)
        )
    if "ingredient" in facts and "contains" in facts:
        full = [str(i) for i in facts.get("ingredients", [])]
        found["contains"] = _says_contains(reply, str(facts["ingredient"]), full) is bool(
            facts["contains"]
        )
    elif isinstance(facts.get("ingredients"), list):
        gold = {str(i).lower() for i in facts["ingredients"]}
        others = catalog.all_ingredients - gold
        found["ingredients"] = all(i in low for i in gold) and not any(
            re.search(rf"\b{re.escape(i)}\b", low) for i in others
        )
    if "availability" in facts:
        says_out = bool(_OUT.search(reply))
        says_in = bool(_IN.search(_OUT.sub(" ", reply))) or any(n > 0 for n in units_of(reply))
        want_out = facts["availability"] == "out_of_stock"
        found["availability"] = says_out if want_out else (says_in and not says_out)
    if "stock_quantity" in facts:
        found["stock_quantity"] = all(n == facts["stock_quantity"] for n in units_of(reply))
    if "status" in facts:
        gold_status = str(facts["status"]).replace("_", " ").lower()
        others_said = [s for s in STATUSES - {gold_status} if re.search(rf"\b{s}\b", low)]
        found["status"] = bool(re.search(rf"\b{re.escape(gold_status)}\b", low)) and not (
            others_said
        )
    if isinstance(facts.get("price"), int | float):
        stated = amounts(reply)
        price = float(facts["price"])
        usd = "$" in reply or "usd" in low
        found["price"] = usd and any(abs(a - price) <= PRICE_TOLERANCE for a in stated)
        claimed = facts.get("claimed_price")
        if isinstance(claimed, int | float) and abs(float(claimed) - price) > PRICE_TOLERANCE:
            found["claimed_price"] = not (
                any(abs(a - float(claimed)) <= PRICE_TOLERANCE for a in stated)
                and not found["price"]
            )
    named = catalog.named_in(reply)
    if len(named) >= MENU_SIZE:
        named = []  # the whole catalog listed (a clarifying menu), not an answer about one
    if facts.get("product_exists") is False:
        found["same_product"] = not named
    elif asked_low is not None and named:
        found["same_product"] = asked_low in named
    return found


def db_facts(
    case: TestCase, response: AgentResponse | None, catalog: Catalog
) -> MetricResult | None:
    if not case.gold_db_facts or response is None or not response.ok:
        return None
    found = checks(case, response.reply, catalog)
    if not found:
        return None
    asked = str(case.gold_db_facts.get("product_name", "")).lower()
    other = [n for n in catalog.named_in(response.reply) if n != asked]
    return MetricResult(
        case_id=case.id,
        metric=DB_FACTS,
        passed=all(found.values()),
        details={
            "checks": found,
            "failed": sorted(k for k, ok in found.items() if not ok),
            "different_product": other[0] if found.get("same_product") is False else None,
        },
    )
