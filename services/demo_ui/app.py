"""Demo UI (Milestone 7, ADR-0023): the customer's app, the evaluation dashboard, and the demo
script, in one Streamlit page. Every decision lives in `logic.py`; this file only lays it out.

Run locally:  make run-ui            In the cluster:  make ui  (a port-forward; no public address)
"""

import os
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from services.demo_ui import logic

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://127.0.0.1:8004")
RESULTS_DIR = Path(os.environ.get("EVAL_RESULTS_DIR", "evals/results"))
DEMO_SCRIPT = Path(os.environ.get("DEMO_SCRIPT_FILE", "docs/DEMO_SCRIPT.md"))
POLL_SECONDS, FOLLOW_SECONDS = 0.5, 120

st.set_page_config(page_title="AI dispute triage demo", page_icon="💸", layout="wide")


@st.cache_resource
def intake() -> logic.IntakeClient:
    return logic.IntakeClient(SUPERVISOR_URL)


@st.cache_resource
def login_settings() -> logic.LoginSettings:
    return logic.LoginSettings.from_env()


@st.cache_resource
def langfuse() -> logic.LangfuseReader | None:
    return logic.LangfuseReader.from_env()


TYPE_COLOURS = {"GENERATION": "#DA0081", "TOOL": "#5A9EFB", "AGENT": "#200020", "CHAIN": "#B9A6D6"}


def show_trace(dispute_id: str, trace_url: str, steps: list[str]) -> None:
    """The run's trace, drawn inside the demo from Langfuse's API (ADR-0025)."""
    reader = langfuse()
    if reader is None:
        st.link_button("Open the Langfuse trace", trace_url)
        return
    key = f"trace-{dispute_id}"

    def read() -> None:  # a callback runs before the page is redrawn, so the label below is current
        st.session_state[key] = reader.observations(logic.trace_id_from_url(trace_url))

    st.button("🔍 Show the trace" if key not in st.session_state else "↻ Read the trace again",
              key=f"btn-{key}", on_click=read)
    observations = st.session_state.get(key)
    if observations is None:
        st.caption("Every step of the run, read from Langfuse: who decided what, which tools were called, "
                   "time, tokens, and cost.")
        return
    if not observations:
        st.warning("The trace is still arriving in Langfuse (it is sent asynchronously). Read it again in a few seconds.")
        return
    if missing := logic.missing_from_trace(observations, steps):
        st.warning(f"Still arriving in Langfuse: the part of the trace sent by {', '.join(missing)}. "
                   "The numbers below are incomplete; read the trace again in a few seconds.")
    everything = st.toggle("Include LangChain's internal steps", key=f"all-{key}")
    view = logic.trace_view(observations, include_internal=everything)
    top = st.columns(3)
    top[0].metric("Model calls", view.model_calls)
    top[1].metric("Tool calls", view.tool_calls)
    top[2].metric("Tokens", f"{view.tokens:,}")
    bottom = st.columns(3)
    bottom[0].metric("Model cost", f"${view.cost_usd:.4f}")
    bottom[1].metric("Duration", f"{view.duration_s:.1f} s")

    import altair as alt

    timeline = pd.DataFrame([{"step": f"{i:02d} " + "  " * r["depth"] + r["label"], "start": r["start_ms"],
                              "end": r["end_ms"] if r["end_ms"] is not None else r["start_ms"] + 1,
                              "type": r["type"], "tokens": r["tokens"], "cost": r["cost_usd"]}
                             for i, r in enumerate(view.rows)])
    chart = alt.Chart(timeline).mark_bar(cornerRadius=4).encode(
        y=alt.Y("step:N", sort=None, title=None, axis=alt.Axis(labelLimit=420, labelOverlap=False)),
        x=alt.X("start:Q", title="milliseconds since the run started"), x2="end:Q",
        color=alt.Color("type:N", scale=alt.Scale(domain=list(TYPE_COLOURS), range=list(TYPE_COLOURS.values())),
                        legend=alt.Legend(title=None, orient="top")),
        tooltip=["step", "type", "start", "end", "tokens", "cost"],
    ).properties(height=max(180, 30 * len(timeline)))
    st.altair_chart(chart, width="stretch")
    if view.hidden:
        st.caption(f"{view.hidden} internal LangChain steps hidden. Totals count every model call.")

    labels = [f"{i:02d} {r['label']}" for i, r in enumerate(view.rows)]
    chosen = st.selectbox("Look inside a step: what went in and what came out", labels, key=f"step-{key}")
    row = view.rows[labels.index(chosen)]
    left, right = st.columns(2)
    left.markdown("**Input**")
    left.code(str(row["input"] or "")[:4000], language="json", wrap_lines=True)
    right.markdown("**Output**")
    right.code(str(row["output"] or "")[:4000], language="json", wrap_lines=True)
    st.link_button("Open in Langfuse", trace_url)


# --- 1. The customer's app ----------------------------------------------------------------------


def dispute_form(slot: str) -> dict | None:
    """One customer's form. Returns what to submit, or None."""
    user_id = st.selectbox("Customer", list(logic.TRANSACTIONS), key=f"user-{slot}")
    # The options are transaction IDs, unique by definition. (Labels can repeat, and a selectbox
    # tracks its choice by what it displays: two equal labels once silently swapped transfers.)
    ids = [t.transaction_id for t in logic.TRANSACTIONS[user_id]]
    chosen = st.selectbox("Transfer", ids, key=f"tx-{slot}-{user_id}",
                          format_func=lambda tx_id: logic.find_transaction(user_id, tx_id).display)
    tx = logic.find_transaction(user_id, chosen)
    st.caption(tx.story)
    description = st.text_area("What happened?", logic.DEFAULT_DESCRIPTION, key=f"desc-{slot}", max_chars=1000)
    return {"slot": slot, "user_id": user_id, "tx": tx, "description": description}


def show_outcome(view: dict, progress_lines: list[str]) -> None:
    outcome = logic.read_outcome(view)
    st.markdown("**Status, as the app saw it**")
    st.markdown("\n".join(f"- {line}" for line in progress_lines) or "-")
    if outcome.path == "incident":
        st.success(outcome.headline)
    elif outcome.path == "agents":
        st.info(outcome.headline)
    st.markdown(outcome.detail)
    st.markdown("**What the customer is told**")
    st.write(view.get("customer_message", ""))
    if outcome.payment:
        p = outcome.payment
        st.markdown(f"**Paid by the ledger:** {p['amount']} {p['currency']} as `{p['refund_id']}` "
                    f"(idempotency key `{p['idempotency_key']}`)")
    if outcome.checks:
        with st.expander("Refund policy checks (code, not a model)", expanded=outcome.path == "incident"):
            st.dataframe(pd.DataFrame(outcome.checks), hide_index=True, width="stretch")
    with st.expander("Steps taken"):
        st.code("\n".join(outcome.steps) or "(none)", language=None)
    if outcome.trace_url:
        with st.expander("🔍 The trace: every step of this run", expanded=True):
            show_trace(view["dispute_id"], outcome.trace_url, outcome.steps)
    elif outcome.path == "incident":
        st.caption("No trace: no model ran.")


def follow(jobs: list[dict], columns: dict) -> None:
    """Poll every submitted dispute until each one has settled, updating its column live."""
    placeholders = {job["slot"]: columns[job["slot"]].empty() for job in jobs}
    deadline = time.monotonic() + FOLLOW_SECONDS
    pending = {job["slot"] for job in jobs}
    while pending and time.monotonic() < deadline:
        for job in jobs:
            if job["slot"] not in pending:
                continue
            view = intake().get(job["token"], job["dispute_id"])
            job["progress"].observe(view, time.monotonic())
            with placeholders[job["slot"]].container():
                if logic.is_settled(view):
                    pending.discard(job["slot"])
                    st.session_state[f"done-{job['slot']}"] = (view, job["progress"].lines())
                    show_outcome(view, job["progress"].lines())
                else:
                    st.markdown("\n".join(f"- {line}" for line in job["progress"].lines()))
                    st.spinner("Waiting for the next status…")
        time.sleep(POLL_SECONDS)


def customer_tab() -> None:
    st.markdown("Submit one dispute, or two side by side (for example scenario 2: the same failure "
                "inside and outside a confirmed incident's window).")
    left, right = st.columns(2, gap="large")
    forms = {}
    with left:
        st.subheader("Dispute A")
        forms["A"] = dispute_form("A")
    with right:
        st.subheader("Dispute B")
        use_b = st.toggle("Also submit dispute B", value=False)
        forms["B"] = dispute_form("B") if use_b else None

    if st.button("Submit", type="primary"):
        jobs = []
        for slot, form in forms.items():
            if form is None:
                continue
            st.session_state.pop(f"done-{slot}", None)
            token = logic.login(form["user_id"], login_settings())
            started = time.monotonic()
            response = intake().submit(token, form["tx"], form["description"])
            column = left if slot == "A" else right
            if response.status_code not in (200, 202):
                column.error(f"HTTP {response.status_code}: {response.text[:300]}")
                continue
            if response.headers.get("idempotent-replay") == "true":
                column.warning("This dispute already exists: the app shows the existing one (no new run).")
            column.caption(f"Accepted in {(time.monotonic() - started) * 1000:.0f} ms "
                           f"(HTTP {response.status_code}) · dispute `{response.json()['dispute_id']}`")
            jobs.append({"slot": slot, "token": token, "dispute_id": response.json()["dispute_id"],
                         "progress": logic.Progress(started=started)})
        follow(jobs, {"A": left, "B": right})
    else:
        for slot, column in (("A", left), ("B", right)):
            if done := st.session_state.get(f"done-{slot}"):
                with column:
                    show_outcome(*done)


# --- 2. The evaluation dashboard ----------------------------------------------------------------


def dashboard_tab() -> None:
    runs = logic.load_runs(RESULTS_DIR)
    if not runs:
        st.info(f"No evaluation reports in {RESULTS_DIR}.")
        return
    last = runs[-1]
    st.markdown(f"Latest run: **{last['run']}**, commit `{last['commit']}`, target `{last['target']}`")
    cols = st.columns(5)
    cols[0].metric("Evaluated requests", last["evaluated"])
    cols[1].metric("Successful requests", last["successful"])
    cols[2].metric("Success rate", f"{last['success_rate']:.0%}", help="successful ÷ evaluated: is it right?")
    cols[3].metric("Total cost", f"${last['total_cost_usd']:.4f}", help="every model call, retries and wrong answers included")
    cols[4].metric("Cost per success", f"${last['cost_per_success_usd']:.4f}" if last["cost_per_success_usd"] else "n/a",
                   help="total cost ÷ successful: what does each right answer cost?")
    st.caption(f"Fast path in this run: {last['fast_path']} dispute(s) decided by a confirmed incident, "
               f"${last['fast_path_cost_usd']:.4f} of model cost. Time to accept (median): "
               f"{last['median_accept_ms']} ms · time to result (median): {last['median_result_s']} s.")

    st.markdown("**Success rate and cost per success, run by run.** Neither is enough alone: a cheap "
                "system that often fails can look fine on cost, and an always-right one can be too "
                "expensive to run.")
    history = pd.DataFrame(runs)
    chart_left, chart_right = st.columns(2)
    chart_left.line_chart(history.set_index("run")["success_rate"], y_label="success rate")
    chart_right.line_chart(history.set_index("run")["cost_per_success_usd"], y_label="cost per success ($)")
    st.dataframe(history, hide_index=True, width="stretch")
    st.markdown("**Latest run, scenario by scenario**")
    st.dataframe(pd.DataFrame(logic.latest_scenarios(RESULTS_DIR)), hide_index=True, width="stretch")


# --- 3. The demo script ---------------------------------------------------------------------------


def script_tab() -> None:
    if DEMO_SCRIPT.exists():
        st.markdown(DEMO_SCRIPT.read_text())
    else:
        st.info("docs/DEMO_SCRIPT.md is not in this image.")


HEADER = """
<div style="background:#200020;border-radius:1.25rem;padding:1.4rem 1.6rem;margin-bottom:0.8rem">
  <div style="color:#DA0081;font-weight:800;letter-spacing:.08em;font-size:.8rem">AI DISPUTE TRIAGE · LIVE DEMO</div>
  <div style="color:#FFFFFF;font-weight:800;font-size:1.9rem;line-height:1.2;margin:.3rem 0">
    Your money didn't arrive? <span style="color:#DA0081">We check, decide, and pay back.</span></div>
  <div style="color:#ECE7F5;font-size:.95rem">Three AI agents investigate on Azure Kubernetes; code approves
    and pays, exactly once. Built for a Nequi interview.</div>
</div>
"""
st.markdown(HEADER, unsafe_allow_html=True)
st.caption("Independent interview demo, not a Nequi product and not affiliated with Nequi. Synthetic customers "
           "and data only; never enter real personal or banking details. Each customer and transfer is "
           "investigated once until the demo is reset; submitting it again shows the existing dispute, at no cost.")
customer, dashboard, script = st.tabs(["📱 Customer app", "📊 Evaluation dashboard", "🗺️ Demo script"])
with customer:
    customer_tab()
with dashboard:
    dashboard_tab()
with script:
    script_tab()
