"""voice_third_person, db_facts and the ops metrics (Day 15), on tiny hand-made inputs.

The voice rule is also checked on the 5 Day 12 tone cases that are in the dev split; the 3
in the locked test split are deliberately not used.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from evalkit.io import read_jsonl
from evalkit.metrics import database, ops, voice
from evalkit.metrics.database import Catalog
from evalkit.schemas import AgentResponse, CaseResult, TestCase

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "runs" / "20261013-191847-f23b"
DEV_VOICE = ["syn-guardrail-5fa94211", "syn-guardrail-97c48220", "syn-knowledge-0b6223ff",
             "syn-knowledge-136f40c4", "syn-knowledge-3c6f3843"]  # fmt: skip
ALLOW = voice.load_allowlist(ROOT / "rubrics" / "voice_allowlist.txt")


def case(**over: Any) -> TestCase:
    data: dict[str, Any] = {"id": "c1", "question": "q", "category": "x",
                            "expected_route": "knowledge", "difficulty": "easy",
                            "source": "synthetic", "should_escalate": False}  # fmt: skip
    return TestCase.model_validate(data | over)


def resp(reply: str, **kw: Any) -> AgentResponse:
    return AgentResponse.model_validate({"ok": True, "reply": reply, **kw})


# --------------------------------------------------------------------------- voice


@pytest.mark.parametrize(
    ("reply", "phrase"),
    [
        ("Customers should contact support with their order number.", "customers"),
        ("If a delay occurs, CUSTOMERS will be notified.", "customers"),
        ("Refunds go to the customer within 5 days.", "the customer"),
        ("New users are advised to patch test.", "users"),
        ("You can ask the assistant for advice.", "the assistant"),
        ("The AI assistant responds instantly.", "the ai assistant"),
        ("The bot cannot change orders.", "the bot"),
    ],
)
def test_third_person_is_flagged(reply: str, phrase: str) -> None:
    assert phrase in voice.third_person(reply, ALLOW)


@pytest.mark.parametrize(
    "reply",
    [
        "Hi! I'm the GlowCare assistant. I can help with our products.",
        "Our customer support team replies within one business day.",
        "I couldn't find that order. Please check the number from your email.",
        "A GlowCare team member can help you further.",
        "You can return it within 30 days.",
        "The customary routine is morning and night.",  # "customary" is not "customer"
    ],
)
def test_clean_replies_are_not_flagged(reply: str) -> None:
    assert voice.third_person(reply, ALLOW) == []


def test_voice_metric_and_allowlist(tmp_path: Path) -> None:
    flagged = voice.voice_third_person(case(), resp("Customers should check."), ALLOW)
    assert flagged is not None and flagged.passed is False
    assert flagged.details == {"phrases": ["customers"]}
    clean = voice.voice_third_person(case(), resp("You should check."), ALLOW)
    assert clean is not None and clean.passed is True
    assert voice.voice_third_person(case(), None) is None
    allow = tmp_path / "allow.txt"
    allow.write_text("# comment\n\nthe customer journey  # a product name\n", encoding="utf-8")
    assert voice.load_allowlist(allow) == ["the customer journey"]
    assert voice.third_person("Read the customer journey guide.", voice.load_allowlist(allow)) == []
    assert voice.load_allowlist(tmp_path / "missing.txt") == []


def test_the_dev_day12_voice_cases_flag() -> None:
    if not RUN.exists():
        pytest.skip("Day 7 run not on this machine (runs/ is not in git)")
    split = json.loads((ROOT / "datasets" / "splits" / "split_v1.json").read_text("utf-8"))
    assert set(DEV_VOICE) <= set(split["splits"]["dev"])  # never the test ones
    replies = {r.case_id: r.response for r in read_jsonl(RUN / "results.jsonl", CaseResult)}
    for cid in DEV_VOICE:
        reply = replies[cid]
        assert reply is not None and voice.third_person(reply.reply, ALLOW), cid


# --------------------------------------------------------------------------- database

CAT = Catalog({
    "hydra balance moisturizer": frozenset({"water", "glycerin", "niacinamide"}),
    "niacinamide serum": frozenset({"water", "niacinamide 10%", "zinc pca"}),
    "barrier repair cream": frozenset({"water", "ceramide np", "cholesterol"}),
})  # fmt: skip


def db(reply: str, **facts: Any) -> Any:
    return database.db_facts(case(gold_db_facts=facts), resp(reply), CAT)


def test_price_exact_in_usd_and_claimed_price() -> None:
    facts = {"product_name": "Niacinamide Serum", "price": 19.99, "currency": "USD"}
    assert db("The Niacinamide Serum costs $19.99.", **facts).passed
    assert db("It is 19.99 USD.", **facts).passed
    assert not db("The Niacinamide Serum costs $19.49.", **facts).passed
    assert not db("It costs 19.99.", **facts).passed  # no currency
    claimed = {"product_name": "Barrier Repair Cream", "price": 32.5, "claimed_price": 14.0}
    assert db("No, the Barrier Repair Cream costs $32.50, not $14.", **claimed).passed
    bad = db("Yes, the Barrier Repair Cream is $14.", **claimed)
    assert set(bad.details["failed"]) == {"price", "claimed_price"}


def test_ingredients_and_contains() -> None:
    gold = {"product_name": "Hydra Balance Moisturizer",
            "ingredients": ["Water", "Glycerin", "Niacinamide"]}  # fmt: skip
    assert db("It contains: Water, Glycerin, Niacinamide.", **gold).passed
    assert not db("It contains: Water, Glycerin.", **gold).passed  # one missing
    assert not db("Water, Glycerin, Niacinamide, Zinc PCA.", **gold).passed  # another's
    yes = {**gold, "ingredient": "Glycerin", "contains": True}
    assert db("Yes, it contains glycerin.", **yes).passed
    assert not db("No, it does not contain glycerin.", **yes).passed
    no = {**gold, "ingredient": "Retinol", "contains": False}
    assert db("No, it doesn't contain retinol.", **no).passed
    assert db("It contains: Water, Glycerin, Niacinamide.", **no).passed  # full list, no retinol
    assert not db("Yes, it has retinol.", **no).passed
    assert not db("It has a nice texture.", **no).passed  # can't tell


def test_stock_and_order_status() -> None:
    in_stock = {"product_name": "Hydra Balance Moisturizer", "availability": "in_stock",
                "stock_quantity": 42}  # fmt: skip
    assert db("The Hydra Balance Moisturizer is in stock.", **in_stock).passed
    assert db("We have 42 units left.", **in_stock).passed
    assert not db("We have 12 units left.", **in_stock).passed
    assert not db("It is out of stock.", **in_stock).passed
    out = {"product_name": "Niacinamide Serum", "availability": "out_of_stock"}
    assert db("The Niacinamide Serum is not in stock right now.", **out).passed
    assert not db("The Niacinamide Serum is in stock.", **out).passed
    order = {"order_id": "GC10241", "status": "shipped"}
    assert db("Order GC10241 is currently shipped.", **order).passed
    assert not db("Order GC10241 is being processing.", **order).passed
    assert not db("It was shipped and delivered.", **order).passed  # two statuses


def test_not_found_and_different_product() -> None:
    missing = {"product_name": "Vitamin C Brightening Serum", "product_exists": False}
    ok = db("I couldn't find a product called Vitamin C Brightening Serum.", **missing)
    assert ok.passed
    wrong = db("The Niacinamide Serum costs $19.99.", **missing)  # the Day 7 case
    assert set(wrong.details["failed"]) == {"not_found", "same_product"}
    assert wrong.details["different_product"] == "niacinamide serum"
    menu = db("Which product do you mean? We have: Hydra Balance Moisturizer, Barrier "
              "Repair Cream, Niacinamide Serum.", **missing)  # fmt: skip
    assert menu.details["failed"] == ["not_found"]  # a menu, not a different product
    no_order = {"order_id": "GC10250", "exists": False}
    assert db("I couldn't find an order with the number GC10250.", **no_order).passed
    assert not db("I couldn't find it, but it shows as shipped.", **no_order).passed
    asked = {"product_name": "Barrier Repair Cream", "price": 32.5}
    other = db("The Niacinamide Serum costs $32.50.", **asked)
    assert other.details["failed"] == ["same_product"]


def test_db_skips_and_catalog() -> None:
    assert database.db_facts(case(), resp("x"), CAT) is None  # no gold facts
    assert db("x", product_id="GC-P001") is None  # nothing checkable
    assert database.db_facts(case(gold_db_facts={"price": 1.0}), None, CAT) is None
    cat = database.catalog_from(
        [case(gold_db_facts={"product_name": "A", "ingredients": ["X"]}),
         case(gold_db_facts={"product_name": "Gone", "product_exists": False})],
        [resp("r", details=[{"name": "B", "ingredients": ["Y"]}, "junk"]), None,
         resp("r", details={"order_id": "1"})],
    )  # fmt: skip
    assert cat.products == {"a": frozenset({"x"}), "b": frozenset({"y"})}
    assert cat.all_ingredients == {"x", "y"} and Catalog().all_ingredients == frozenset()
    assert database.amounts("$5, $1,50 and 3 USD") == [5.0, 1.5, 3.0]


# --------------------------------------------------------------------------- ops


def result(cid: str, route: str | None, latency: float | None, prompt: int | None = None,
           completion: int | None = None) -> CaseResult:  # fmt: skip
    return CaseResult.model_validate({
        "run_id": "r", "case_id": cid, "attempts": 1, "cache_hit": False,
        "wall_latency_ms": 10.0, "started_at": "2026-10-13T00:00:00Z",
        "finished_at": "2026-10-13T00:00:01Z",
        "response": {"ok": True, "reply": "x", "route": route, "latency_ms": latency,
                     "prompt_tokens": prompt, "completion_tokens": completion},
    })  # fmt: skip


def test_latency_and_tokens() -> None:
    results = [result("a", "knowledge", 100.0, 1000, 50), result("b", "knowledge", 300.0, 2000),
               result("c", "greeting", 1.0), result("d", None, None)]  # fmt: skip
    rows = [r for x in results for r in (ops.latency(x), ops.tokens(x)) if r is not None]
    lat = ops.latency_by_route(rows)
    assert lat["knowledge"] == ops.LatencySummary(2, 200.0, 290.0, 300.0)
    assert lat["all"].n == 3 and lat["all"].max == 300.0 and "none" not in lat
    tok = ops.tokens_by_route(rows)
    assert tok["knowledge"] == ops.TokenSummary(2, 3000, 50) and tok["knowledge"].total == 3050
    assert "greeting" not in tok and tok["all"].n == 2
    assert tok["all"].cost_usd(None, None) is None  # no default price
    assert tok["all"].cost_usd(1.0, 2.0) == pytest.approx((3000 * 1.0 + 50 * 2.0) / 1e6)
    no_route = ops.latency(result("e", None, 5.0))
    assert no_route is not None and no_route.details["route"] == "none"
    failed = CaseResult.model_validate(
        {**results[0].model_dump(), "response": None, "failure_type": "skipped"}
    )
    assert ops.latency(failed) is None and ops.tokens(failed) is None
