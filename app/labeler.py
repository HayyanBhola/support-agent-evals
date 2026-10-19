"""Human labeling app (Day 9): a thin Streamlit UI over evalkit.labeling.

Start (from the repo root):
    uv run --group labeling streamlit run app/labeler.py -- --labeler <name>

Options after ``--`` (or environment variables, used by the tests):
    --labeler NAME   (EVALKIT_LABELER, required)
    --sample PATH    (EVALKIT_LABEL_SAMPLE, default datasets/label_sample_v1.jsonl)
    --labels PATH    (EVALKIT_LABELS, default derived from the sample: label_sample_v1.jsonl
                      -> datasets/labels/labels_raw_v1.jsonl, overlap_v1.jsonl ->
                      labels_raw_overlap_v1.jsonl, relabel_v1.jsonl -> labels_raw_relabel_v1.jsonl)
    --rubrics DIR    (EVALKIT_RUBRICS, default rubrics)

No automated scores are shown (rule checks, route match), so the labeler isn't anchored, and
only the current labeler's own saved labels are ever shown (never another labeler's).
"""

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

from evalkit import rubrics
from evalkit.io import read_jsonl
from evalkit.labeling import applicability, store, view, workflow
from evalkit.schemas import Criterion, Label, LabelSample, LabelValue

BREAK_EVERY = 40
CHOICES = [v.value for v in LabelValue]
JUDGED_CHOICES = [LabelValue.PASS.value, LabelValue.FAIL.value]
NA_ON_JUDGED = "Saved as n_a, but the rubric says judge this: choose a verdict."
FILTERS = ["unlabeled", "labeled", "to re-check under the current rubric", "all"]


def parse_config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="labeler")
    parser.add_argument("--labeler", default=os.environ.get("EVALKIT_LABELER"))
    parser.add_argument(
        "--sample",
        default=os.environ.get(
            "EVALKIT_LABEL_SAMPLE", str(Path("datasets") / workflow.SAMPLE_FILE)
        ),
    )
    parser.add_argument("--labels", default=os.environ.get("EVALKIT_LABELS"))
    parser.add_argument(
        "--rubrics", default=os.environ.get("EVALKIT_RUBRICS", str(rubrics.DEFAULT_DIR))
    )
    args, _ = parser.parse_known_args(sys.argv[1:])
    if not args.labels:
        args.labels = str(store.DEFAULT_LABELS_DIR / store.raw_file_for(Path(args.sample)))
    return args


HOW_TO = """
- **Left:** the question, any history, the agent's reply, the retrieved chunks and the DB
  facts. **Right:** the route, answer_type, escalated flag and the case's gold fields.
- **Sidebar:** at the top, the Key rules of the criterion picked under "Rubric for"; your
  progress; a filter (unlabeled / labeled / ...); and the rubric for any criterion
  (definition, Pass if, Fail if, Edge cases). The rubric is the only guide.
- **Verdicts:** pass / fail / n_a for each of the 7 criteria. A criterion the rubric marks
  n_a for this record is pre-set to n_a and greyed out; tick *override* to judge it anyway.
  Safety, relevance and tone apply to every reply, so they offer only pass / fail.
  `escalated` and `should_escalate` are shown next to escalation_appropriate.
  A saved n_a on a criterion the rubric says to judge comes back unset with a warning:
  choose the verdict again before saving.
- **Notes:** required whenever any verdict is fail (one short reason).
- **Save:** check the "Saving to: ..." line above the button names the record you mean.
  After each save a green box confirms the case_id, all 7 verdicts and the note, and the
  app moves to the next unlabeled record.
- **Navigation:** Back, Next unlabeled, or type a case_id and press Go. Your work is saved
  on every Save; after a restart the app picks up where you left off.
"""


@st.cache_data
def load_samples(path: str, mtime: float) -> list[LabelSample]:
    return list(read_jsonl(Path(path), LabelSample))


@st.cache_data
def load_rubric_state(
    rubric_dir: str, fingerprint: str
) -> tuple[list[rubrics.Rubric], list[Criterion]]:
    d = Path(rubric_dir)
    return rubrics.load_rubrics(d, None), rubrics.judging_order(d)


def saved_summary(label: Label) -> str:
    """The confirmation shown after a save: case_id, all 7 verdicts and the note."""
    verdicts = ", ".join(f"{c.value}={v.value}" for c, v in label.verdicts.items())
    return f"Saved {label.case_id}: {verdicts}. Note: {label.notes or '(none)'}"


def main() -> None:
    st.set_page_config(page_title="GlowCare labeling", layout="wide")
    with st.expander("How to use this app"):
        st.markdown(HOW_TO)
    cfg = parse_config()
    if not cfg.labeler:
        st.error(
            "Start the app with a labeler name: "
            "`uv run --group labeling streamlit run app/labeler.py -- --labeler <name>`"
        )
        st.stop()
    labeler = cfg.labeler
    sample_path, rubric_dir = Path(cfg.sample), Path(cfg.rubrics)
    samples = load_samples(str(sample_path), sample_path.stat().st_mtime)
    current_hash = rubrics.rubric_hash(rubric_dir)
    loaded, order = load_rubric_state(str(rubric_dir), current_hash)
    versions = rubrics.load_versions(rubric_dir)
    by_criterion = {r.criterion: r for r in loaded}
    label_store = store.LabelStore(Path(cfg.labels))
    mine = label_store.for_labeler(labeler)

    state = st.session_state
    state.setdefault("filter", FILTERS[0])
    state.setdefault("saved", 0)
    state.setdefault("current", None)

    def in_filter(s: LabelSample) -> bool:
        lab = mine.get(s.case_id)
        f = state["filter"]
        if f == "unlabeled":
            return lab is None
        if f == "labeled":
            return lab is not None
        if f == FILTERS[2]:
            return lab is not None and workflow.needs_recheck(lab, current_hash, versions)
        return True

    # ---------------------------------------------------------------- sidebar
    with st.sidebar:
        # the Key rules of the criterion picked under "Rubric for" below (safety at first)
        key_for = Criterion(state.get("rubric_view", order[0].value))
        rules = [f"- {rule}" for rule in by_criterion[key_for].key_rules]
        st.info("\n".join([f"**Key rules: {key_for.value}**", "", *rules]))
        st.markdown(f"**Labeler:** `{labeler}` · **Set:** `{sample_path.name}`")
        st.progress(len(mine) / len(samples), text=f"{len(mine)}/{len(samples)} labeled")
        st.radio("Show", FILTERS, key="filter")
        if label_store.recovered:
            st.warning("A partial last line (crash) was dropped from the labels file.")
        st.divider()
        shown = st.selectbox("Rubric for", [c.value for c in order], key="rubric_view")
        rub = by_criterion[Criterion(shown)]
        st.markdown(f"**{shown}**: {rub.definition}")
        for section in ("Pass if", "Fail if", "Edge cases"):
            with st.expander(section, expanded=section != "Edge cases"):
                st.markdown(rub.sections.get(section, ""))
        st.caption(f"rubric_hash `{current_hash[:12]}`")

    records = [s for s in samples if in_filter(s)]
    ids = [s.case_id for s in samples]
    if state["current"] not in ids or (
        state["current"] not in {s.case_id for s in records} and records
    ):
        state["current"] = records[0].case_id if records else ids[0]

    # ---------------------------------------------------------------- navigation
    nav = st.columns([1, 1, 3, 1])
    pos = ids.index(state["current"])
    if nav[0].button("Back", key="back", disabled=pos == 0):
        state["current"] = ids[pos - 1]
        st.rerun()
    if nav[1].button("Next unlabeled", key="next"):
        nxt = next((i for i in ids[pos + 1 :] + ids[: pos + 1] if i not in mine), None)
        if nxt:
            state["current"] = nxt
            st.rerun()
    jump = nav[2].text_input(
        "Jump to case_id", key="jump", label_visibility="collapsed", placeholder="jump to case_id"
    )
    if nav[3].button("Go", key="go"):
        target = jump.strip()
        if target in ids:
            state["current"] = target
            st.rerun()
        st.error(
            f"'{target}' is not in the sample; still on {state['current']}."
            if target
            else f"Type a case_id to jump to; still on {state['current']}."
        )
    if not records:
        st.info(f"Nothing under '{state['filter']}'. Pick another filter.")

    sample = samples[ids.index(state["current"])]
    if state.get("shown_case") != sample.case_id:
        state["shown_case"], state["shown_at"] = sample.case_id, time.time()
    existing = mine.get(sample.case_id)

    # ---------------------------------------------------------------- record
    case, resp = sample.case, sample.response
    head = f"{sample.case_id}  ({pos + 1}/{len(samples)})"
    if existing is not None:
        recheck = workflow.needs_recheck(existing, current_hash, versions)
        stale = " (re-check under the current rubric)" if recheck else ""
        head += f"  - labeled{stale}"
    st.subheader(head)
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Question**")
        st.text(case.question)
        if case.history:
            st.markdown("**History**")
            for turn in case.history:
                st.text(f"{turn.role}: {turn.content}")
        st.markdown("**Reply**")
        st.text(resp.reply)
        chunks = view.chunks(resp)
        if chunks:
            with st.expander(f"Retrieved chunks ({len(chunks)})"):
                for ch in chunks:
                    st.markdown(f"`{ch.id}`")
                    st.text(ch.text)
        facts = view.db_facts(resp)
        if facts:
            with st.expander("DB facts (details)", expanded=True):
                st.json(facts)
    with right:
        st.table(view.facts_table(sample))
        if case.reference_answer:
            st.markdown("**Reference answer**")
            st.text(case.reference_answer)
        if case.gold_db_facts:
            st.markdown("**Gold DB facts**")
            st.json(dict(case.gold_db_facts))

    # ---------------------------------------------------------------- verdicts
    judged = applicability.applicability(loaded, case, resp)
    na_allowed = {r.criterion: applicability.allows_na(r) for r in loaded}
    st.markdown("### Verdicts")
    verdicts: dict[Criterion, str | None] = {}
    overrides: list[Criterion] = []
    for criterion in order:
        key = f"{sample.case_id}:{criterion.value}"
        prior = existing.verdicts[criterion].value if existing else None
        cols = st.columns([2, 3, 2])
        cols[0].markdown(f"**{criterion.value}**")
        if criterion is Criterion.ESCALATION_APPROPRIATE:
            cols[2].caption(view.escalation_facts(case.should_escalate, resp.escalated))
        if judged[criterion]:
            # criteria judged on every reply don't offer n_a (rubric v2: never for "unsure")
            choices = CHOICES if na_allowed[criterion] else JUDGED_CHOICES
            if prior == LabelValue.N_A.value:
                # a saved n_a on a criterion the rubric judges: never pre-select it, so a
                # re-save can't keep it by accident (it has to be chosen again)
                cols[2].warning(NA_ON_JUDGED)
                prior = None
            idx = choices.index(prior) if prior in choices else None
            verdicts[criterion] = cols[1].radio(
                criterion.value,
                choices,
                index=idx,
                key=key,
                horizontal=True,
                label_visibility="collapsed",
            )
            continue
        was_override = bool(existing and criterion in existing.overrides)
        override = cols[2].checkbox(
            "override (rubric: n_a)", value=was_override, key=f"{key}:override"
        )
        idx = CHOICES.index(prior) if (prior and override) else CHOICES.index(LabelValue.N_A.value)
        value = cols[1].radio(
            criterion.value,
            CHOICES,
            index=idx,
            key=key,
            horizontal=True,
            label_visibility="collapsed",
            disabled=not override,
        )
        verdicts[criterion] = value if override else LabelValue.N_A.value
        if override:
            overrides.append(criterion)
    notes = st.text_area(
        "Notes (required when any verdict is fail: one short reason)",
        value=(existing.notes or "") if existing else "",
        key=f"{sample.case_id}:notes",
    )

    st.header(f"Saving to: {sample.case_id}")
    if st.button(f"Save {sample.case_id}", key="save", type="primary"):
        missing = [c.value for c, v in verdicts.items() if v is None]
        fails = [c.value for c, v in verdicts.items() if v == LabelValue.FAIL.value]
        if missing:
            st.error(f"Choose a verdict for: {', '.join(missing)}")
        elif fails and not notes.strip():
            st.error(f"Add a note: fail on {', '.join(fails)}")
        else:
            label = Label(
                case_id=sample.case_id,
                run_id=sample.run_id,
                labeler=labeler,
                timestamp=datetime.now(UTC),
                rubric_hash=current_hash,
                response_sha256=sample.response_sha256,
                verdicts={c: LabelValue(v) for c, v in verdicts.items() if v is not None},
                overrides=overrides,
                notes=notes.strip() or None,
                seconds_spent=round(time.time() - state["shown_at"], 1),
            )
            label_store.append(label)
            state["saved"] += 1
            if state["saved"] % BREAK_EVERY == 0:
                st.toast(f"{state['saved']} records this session: time for a short break.")
            labeled = set(mine) | {sample.case_id}
            nxt = next((i for i in ids[pos + 1 :] + ids[:pos] if i not in labeled), None)
            state["current"] = nxt or sample.case_id
            state["last_saved"] = saved_summary(label)
            st.rerun()
    if state.get("last_saved"):
        st.success(state["last_saved"])
    if state["saved"] and state["saved"] % BREAK_EVERY == 0:
        st.warning(f"{state['saved']} records labeled this session: take a short break.")


main()
