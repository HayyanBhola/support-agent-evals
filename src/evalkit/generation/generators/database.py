"""Database generator: cases built by code from products.json and the seed orders.

Gold facts are exact copies of the data; the LLM only rewords the template question and the
reworded question must still contain the product name / order id (and the ingredient, for
"does it contain" questions). Labels by rule: rules 1-4 -> expected_route database,
should_escalate false (no rule 9 trigger).

Traps: order ids that don't exist (rule 3), ingredients a product doesn't contain (rule 4),
and products GlowCare doesn't sell (decision 6: expected_route null, behaviour checked via
gold ``product_exists: false`` and no invented price; tags route_unchecked,
hallucination_trap).
"""

import random
from typing import Any, Final

from evalkit.generation.base import Evolution, GenContext, GenOutput, make_candidate
from evalkit.generation.filters import Candidate
from evalkit.generation.generators import labels
from evalkit.generation.generators.paraphrase import FALLBACK_TAG, Template, paraphrase
from evalkit.schemas import Route

GEN: Final = "database"
CONTAINS_YES: Final = 4
CONTAINS_NO: Final = 5
ROMAN_URDU_N: Final = 5
CASUAL_N: Final = 4

# Template wording per intent and style. {name} / {id} / {ing} are filled by code.
TEXTS: Final[dict[str, dict[Evolution, str]]] = {
    "price": {
        Evolution.PLAIN: "How much does the {name} cost?",
        Evolution.CASUAL: "hey whats the price on the {name}??",
        Evolution.ROMAN_URDU: "{name} kitne ka hai?",
    },
    "stock": {
        Evolution.PLAIN: "Is the {name} in stock right now?",
        Evolution.CASUAL: "u guys got the {name} in stock",
        Evolution.ROMAN_URDU: "Kya {name} stock mein hai?",
    },
    "ingredients": {
        Evolution.PLAIN: "What ingredients are in the {name}?",
        Evolution.CASUAL: "whats actually in the {name}, like the ingredients",
        Evolution.ROMAN_URDU: "{name} mein kya ingredients hain?",
    },
    "contains": {
        Evolution.PLAIN: "Does the {name} contain {ing}?",
        Evolution.CASUAL: "is there any {ing} in the {name}",
        Evolution.ROMAN_URDU: "Kya {name} mein {ing} hai?",
    },
    "order_status": {
        Evolution.PLAIN: "Can you check the status of order {id}?",
        Evolution.CASUAL: "wheres my order {id} at",
        Evolution.ROMAN_URDU: "Mera order {id} kahan hai?",
    },
}
ORDER_PLAIN_2: Final = "What's happening with my order {id}?"
FAKE_ORDER_TEXT: Final = "Can you tell me the status of my order {id}?"
NONEXISTENT_TEXT: Final = "How much does the {name} cost?"


def database_templates(ctx: GenContext) -> list[Template]:
    rng = ctx.rng(GEN)
    products = ctx.sources.products
    plain: list[tuple[str, dict[str, str], dict[str, Any]]] = []
    plain += [("price", {"name": p.name}, labels.price_fields(p)) for p in products]
    plain += [("stock", {"name": p.name}, labels.stock_fields(p)) for p in products]
    plain += [("ingredients", {"name": p.name}, labels.ingredients_fields(p)) for p in products]
    for p in rng.sample(list(products), CONTAINS_YES):
        ing = labels.ingredient_name(rng.choice([i for i in p.ingredients if i != "Water"]))
        plain.append(("contains", {"name": p.name, "ing": ing}, labels.contains_fields(p, ing)))
    plain += [
        ("order_status", {"id": o.order_id}, labels.order_fields(o)) for o in ctx.sources.orders
    ]

    styles = _assign_styles(rng, len(plain))
    templates = []
    for n, ((intent, slots, fields), evolution) in enumerate(zip(plain, styles, strict=True)):
        text = TEXTS[intent][evolution].format(**slots)
        templates.append(_template(intent, n, evolution, text, slots, fields))
    # a second plain phrasing per real order, so order status gets 6 cases
    for o in ctx.sources.orders:
        n = len(templates)
        templates.append(
            _template("order_status", n, Evolution.PLAIN, ORDER_PLAIN_2.format(id=o.order_id),
                      {"id": o.order_id}, labels.order_fields(o))
        )  # fmt: skip

    for p in rng.sample(list(products), CONTAINS_NO):
        ing = rng.choice(labels.ABSENT_INGREDIENTS)
        n = len(templates)
        text = TEXTS["contains"][Evolution.PLAIN].format(name=p.name, ing=ing)
        templates.append(
            _template("contains_absent", n, Evolution.TRAP, text, {"name": p.name, "ing": ing},
                      labels.contains_fields(p, ing), tags=["hallucination_trap"])
        )  # fmt: skip
    real_ids = {o.order_id for o in ctx.sources.orders}
    for order_id in labels.FAKE_ORDER_IDS:
        if order_id in real_ids:  # pragma: no cover - guarded by a test on the seed data
            raise ValueError(f"fake order id {order_id} exists in seed_data.sql")
        n = len(templates)
        templates.append(
            _template("fake_order", n, Evolution.TRAP, FAKE_ORDER_TEXT.format(id=order_id),
                      {"id": order_id}, labels.fake_order_fields(order_id),
                      tags=["hallucination_trap"])
        )  # fmt: skip
    for name in labels.NONEXISTENT_PRODUCTS:
        n = len(templates)
        templates.append(
            _template("nonexistent_product", n, Evolution.TRAP, NONEXISTENT_TEXT.format(name=name),
                      {"name": name}, labels.nonexistent_product_fields(name),
                      tags=["route_unchecked", "hallucination_trap"], route=None)
        )  # fmt: skip
    return templates


def _assign_styles(rng: random.Random, n: int) -> list[Evolution]:
    styles = [Evolution.ROMAN_URDU] * ROMAN_URDU_N + [Evolution.CASUAL] * CASUAL_N
    styles += [Evolution.PLAIN] * (n - len(styles))
    rng.shuffle(styles)
    return styles


def _template(
    subtype: str,
    n: int,
    evolution: Evolution,
    text: str,
    keep: dict[str, str],
    fields: dict[str, Any],
    *,
    tags: list[str] | None = None,
    route: Route | None = Route.DATABASE,
) -> Template:
    slot = f"{subtype}/{n}"

    def build(question: str, fallback: bool) -> Candidate:
        return make_candidate(
            generator=GEN,
            subtype=subtype,
            evolution=evolution,
            question=question,
            category="database",
            expected_route=route,
            slot=slot,
            tags=(tags or []) + ([FALLBACK_TAG] if fallback else []),
            **fields,
        )

    return Template(subtype, slot, evolution, text, tuple(keep.values()), build)


async def generate_database(ctx: GenContext) -> GenOutput:
    out = GenOutput()
    await paraphrase(ctx, GEN, database_templates(ctx), out)
    return out
