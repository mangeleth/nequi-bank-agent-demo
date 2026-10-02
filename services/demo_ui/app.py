"""Demo UI (Milestone 7, ADR-0023): the customer's app, the evaluation dashboard, and the demo
script, in one Streamlit page. Every decision lives in `logic.py`; this file only lays it out.

The interface is in Spanish (ADR-0025). What the system itself produced is shown as it is: the
customer message, the steps taken, the trace's steps, and the models' inputs and outputs.

Run locally:  make run-ui            In the cluster:  make ui, or the public link (ADR-0024)
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
MAGENTA, DARK = "#DA0081", "#200020"

st.set_page_config(page_title="Demo: revisión de disputas con IA", page_icon="💸", layout="wide")


@st.cache_resource
def intake() -> logic.IntakeClient:
    return logic.IntakeClient(SUPERVISOR_URL)


@st.cache_resource
def login_settings() -> logic.LoginSettings:
    return logic.LoginSettings.from_env()


@st.cache_resource
def langfuse() -> logic.LangfuseReader | None:
    return logic.LangfuseReader.from_env()


@st.cache_resource
def reviews() -> logic.ReviewClient:
    return logic.ReviewClient(SUPERVISOR_URL)


JUDGE_RESULTS_DIR = Path(os.environ.get("JUDGE_RESULTS_DIR", "evals/judge/results"))


TYPE_COLOURS = {"GENERATION": MAGENTA, "TOOL": "#5A9EFB", "AGENT": DARK, "CHAIN": "#B9A6D6"}
TYPE_NAMES = {"GENERATION": "llamada al modelo", "TOOL": "herramienta", "AGENT": "agente", "CHAIN": "paso"}


def show_trace(dispute_id: str, trace_url: str, steps: list[str]) -> None:
    """The run's trace, drawn inside the demo from Langfuse's API (ADR-0025)."""
    reader = langfuse()
    if reader is None:
        st.link_button("Abrir la traza en Langfuse", trace_url)
        return
    key = f"trace-{dispute_id}"

    def read() -> None:  # a callback runs before the page is redrawn, so the label below is current
        st.session_state[key] = reader.observations(logic.trace_id_from_url(trace_url))

    st.button("🔍 Ver la traza" if key not in st.session_state else "↻ Volver a leer la traza",
              key=f"btn-{key}", on_click=read)
    observations = st.session_state.get(key)
    if observations is None:
        st.caption("Cada paso de la ejecución, leído desde Langfuse: quién decidió qué, qué herramientas "
                   "se llamaron, tiempo, tokens y costo.")
        return
    if not observations:
        st.warning("La traza todavía está llegando a Langfuse (se envía de forma asíncrona). "
                   "Vuelve a leerla en unos segundos.")
        return
    if missing := logic.missing_from_trace(observations, steps):
        st.warning(f"Todavía está llegando a Langfuse la parte de la traza que envía {', '.join(missing)}. "
                   "Las cifras de abajo están incompletas; vuelve a leer la traza en unos segundos.")
    everything = st.toggle("Incluir los pasos internos de LangChain", key=f"all-{key}")
    view = logic.trace_view(observations, include_internal=everything)
    top = st.columns(3)
    top[0].metric("Llamadas al modelo", view.model_calls)
    top[1].metric("Llamadas a herramientas", view.tool_calls)
    top[2].metric("Tokens", f"{view.tokens:,}")
    bottom = st.columns(3)
    bottom[0].metric("Costo del modelo", f"${view.cost_usd:.4f}")
    bottom[1].metric("Duración", f"{view.duration_s:.1f} s")

    import altair as alt

    timeline = pd.DataFrame([{"paso": f"{i:02d} " + "  " * r["depth"] + r["label"], "inicio": r["start_ms"],
                              "fin": r["end_ms"] if r["end_ms"] is not None else r["start_ms"] + 1,
                              "tipo": TYPE_NAMES.get(r["type"], r["type"]), "tokens": r["tokens"],
                              "costo": r["cost_usd"]}
                             for i, r in enumerate(view.rows)])
    chart = alt.Chart(timeline).mark_bar(cornerRadius=4).encode(
        y=alt.Y("paso:N", sort=None, title=None, axis=alt.Axis(labelLimit=420, labelOverlap=False)),
        x=alt.X("inicio:Q", title="milisegundos desde el inicio de la ejecución"), x2="fin:Q",
        color=alt.Color("tipo:N", scale=alt.Scale(domain=[TYPE_NAMES[t] for t in TYPE_COLOURS],
                                                  range=list(TYPE_COLOURS.values())),
                        legend=alt.Legend(title=None, orient="bottom", columns=2)),
        tooltip=["paso", "tipo", "inicio", "fin", "tokens", "costo"],
    ).properties(height=max(180, 30 * len(timeline)))
    st.altair_chart(chart, width="stretch")
    if view.hidden:
        st.caption(f"{view.hidden} pasos internos de LangChain ocultos. Los totales cuentan todas las "
                   "llamadas al modelo.")

    labels = [f"{i:02d} {r['label']}" for i, r in enumerate(view.rows)]
    chosen = st.selectbox("Mira dentro de un paso: qué entró y qué salió", labels, key=f"step-{key}")
    row = view.rows[labels.index(chosen)]
    left, right = st.columns(2)
    left.markdown("**Entrada**")
    left.code(str(row["input"] or "")[:4000], language="json", wrap_lines=True)
    right.markdown("**Salida**")
    right.code(str(row["output"] or "")[:4000], language="json", wrap_lines=True)
    st.link_button("Abrir en Langfuse", trace_url)


# --- 1. The customer's app ----------------------------------------------------------------------


def dispute_form(slot: str) -> dict | None:
    """One customer's form. Returns what to submit, or None."""
    user_id = st.selectbox("Cliente", list(logic.TRANSACTIONS), key=f"user-{slot}")
    # The options are transaction IDs, unique by definition. (Labels can repeat, and a selectbox
    # tracks its choice by what it displays: two equal labels once silently swapped transfers.)
    ids = [t.transaction_id for t in logic.TRANSACTIONS[user_id]]
    chosen = st.selectbox("Transferencia", ids, key=f"tx-{slot}-{user_id}",
                          format_func=lambda tx_id: logic.find_transaction(user_id, tx_id).display)
    tx = logic.find_transaction(user_id, chosen)
    st.caption(tx.story)
    description = st.text_area("¿Qué pasó?", logic.DEFAULT_DESCRIPTION, key=f"desc-{slot}", max_chars=1000)
    return {"slot": slot, "user_id": user_id, "tx": tx, "description": description}


def show_outcome(view: dict, progress_lines: list[str]) -> None:
    outcome = logic.read_outcome(view)
    st.markdown("**Estados, tal como los vio la app**")
    st.markdown("\n".join(f"- {line}" for line in progress_lines) or "-")
    if outcome.path == "incident":
        st.success(outcome.headline)
    elif outcome.path == "agents":
        st.info(outcome.headline)
    st.markdown(outcome.detail)
    st.markdown("**Lo que se le dice al cliente** (texto del sistema, sin traducir)")
    st.write(view.get("customer_message", ""))
    if outcome.payment:
        p = outcome.payment
        st.markdown(f"**Pagado por el libro contable:** {p['amount']} {p['currency']} como `{p['refund_id']}` "
                    f"(clave de idempotencia `{p['idempotency_key']}`)")
    if outcome.checks:
        with st.expander("Reglas de la política de reembolsos (código, no un modelo)",
                         expanded=outcome.path == "incident"):
            checks = pd.DataFrame(outcome.checks).rename(columns={"name": "regla", "passed": "cumple",
                                                                  "detail": "detalle"})
            st.dataframe(checks, hide_index=True, width="stretch")
    with st.expander("Pasos realizados"):
        st.code("\n".join(outcome.steps) or "(ninguno)", language=None)
    if outcome.trace_url:
        with st.expander("🔍 La traza: cada paso de esta ejecución", expanded=True):
            show_trace(view["dispute_id"], outcome.trace_url, outcome.steps)
    elif outcome.path == "incident":
        st.caption("Sin traza: no se usó ningún modelo.")


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
                    st.caption("Esperando el siguiente estado…")
        time.sleep(POLL_SECONDS)


def customer_tab() -> None:
    st.markdown("Envía una disputa, o dos lado a lado (por ejemplo, el escenario 2: la misma falla dentro "
                "y fuera de la ventana de un incidente confirmado).")
    left, right = st.columns(2, gap="large")
    forms = {}
    with left:
        st.subheader("Disputa A")
        forms["A"] = dispute_form("A")
    with right:
        st.subheader("Disputa B")
        use_b = st.toggle("Enviar también la disputa B", value=False)
        forms["B"] = dispute_form("B") if use_b else None

    if st.button("Enviar", type="primary"):
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
                column.warning("Esta disputa ya existe: la app muestra la existente (sin una nueva revisión).")
            column.caption(f"Recibida en {(time.monotonic() - started) * 1000:.0f} ms "
                           f"(HTTP {response.status_code}) · disputa `{response.json()['dispute_id']}`")
            jobs.append({"slot": slot, "token": token, "dispute_id": response.json()["dispute_id"],
                         "progress": logic.Progress(started=started)})
        follow(jobs, {"A": left, "B": right})
    else:
        for slot, column in (("A", left), ("B", right)):
            if done := st.session_state.get(f"done-{slot}"):
                with column:
                    show_outcome(*done)


# --- 2. The reviewer: a person decides what the system sent to them (ADR-0027) ------------------


def show_judgement(judgement: dict | None) -> None:
    st.markdown(f"**Juez de IA:** {logic.judge_badge(judgement)}")
    if rows := logic.judgement_rows(judgement):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(f"Prompt del juez {judgement.get('prompt_version')}. El juez califica la explicación contra "
                   "los registros; no cambia ninguna decisión.")
    elif judgement and judgement.get("result", {}).get("error"):
        st.caption(f"No se pudo evaluar: {judgement['result']['error']}")


def review_tab() -> None:
    st.markdown("Eres una persona del banco que revisa las disputas que el sistema no pudo decidir solo. "
                "Al **aprobar**, se paga lo que el libro contable muestra pendiente (no lo que escribas); al "
                "**rechazar**, la disputa se cierra. Cada decisión queda en el registro de auditoría.")
    reviewer = st.selectbox("Revisor", logic.REVIEWERS, key="reviewer")
    if st.button("↻ Actualizar la cola"):
        st.session_state.pop("review-selected", None)
    try:
        token = logic.login_reviewer(reviewer, login_settings())
        queue = reviews().queue(token)
    except Exception as exc:  # the API or the login key is unavailable: say so, do not crash the page
        st.warning(f"La revisión no está disponible ahora ({type(exc).__name__}). Intenta de nuevo en unos segundos.")
        return
    st.subheader(f"Cola de revisión: {len(queue)} disputa(s) esperando")
    if not queue:
        st.info("No hay disputas esperando a una persona. Para crear una, en la app del cliente envía por "
                "ejemplo la transferencia de 450.000 de user-1001 (supera el límite automático).")
        return
    st.dataframe(pd.DataFrame(logic.queue_rows(queue)), hide_index=True, width="stretch")

    options = [item["dispute"]["dispute_id"] for item in queue]
    chosen = st.selectbox("Disputa a revisar", options, key="review-selected",
                          format_func=lambda d: next(f"{d[:8]} · {i['customer_id']} · {i['dispute']['transaction_id']}"
                                                     for i in queue if i["dispute"]["dispute_id"] == d))
    detail = reviews().dispute(token, chosen)
    dispute, result = detail["dispute"], detail["dispute"].get("result") or {}

    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**Lo que pidió el cliente**")
        st.write(f"{detail['request'].get('claimed_amount')} COP · “{detail['request'].get('description') or '—'}”")
        st.markdown(f"**Por qué llegó a una persona:** {logic.why_a_person(result)}")
        if owed := logic.ledger_owed(result):
            st.markdown(f"**Pendiente según el libro contable** (al revisarla el sistema): {owed}")
        if fraud := result.get("fraud"):
            st.markdown(f"**Riesgo de fraude:** {fraud['risk_score']} ({fraud['risk_level']})")
        if checks := (result.get("approval") or {}).get("checks"):
            st.markdown("**Reglas de la política de reembolsos**")
            st.dataframe(pd.DataFrame(checks).rename(columns={"name": "regla", "passed": "cumple", "detail": "detalle"}),
                         hide_index=True, width="stretch")
    with right:
        show_judgement(detail.get("judgement"))
        if explanation := (result.get("verdict") or {}).get("explanation"):
            st.markdown("**Explicación de los agentes** (texto del sistema, sin traducir)")
            st.write(explanation)
        with st.expander("Registro de auditoría"):
            st.dataframe(pd.DataFrame(detail.get("events", [])), hide_index=True, width="stretch")

    st.markdown("---")
    st.markdown("**Tu decisión**")
    decision = st.radio("Decisión", ["approve", "reject"], horizontal=True, key=f"decision-{chosen}",
                        format_func=lambda d: "✅ Aprobar el reembolso" if d == "approve" else "⛔ Rechazar")
    note = st.text_area("Motivo (obligatorio, queda en la auditoría)", key=f"note-{chosen}", max_chars=500)
    if st.button("Confirmar decisión", type="primary", disabled=len(note.strip()) < 5):
        response = reviews().decide(token, chosen, decision, note.strip())
        if response.status_code == 200:
            view = response.json()
            st.success(f"Decisión registrada: {logic.STATUS_LABELS.get(view['status'], view['status'])}. "
                       "Si la aprobaste, el pagador la paga en segundos; el cliente lo ve en su app.")
            st.session_state.pop("review-selected", None)
        else:
            st.error(f"HTTP {response.status_code}: {response.json().get('detail', response.text)}")


# --- 3. The evaluation dashboard ----------------------------------------------------------------

RUN_COLUMNS = {
    "run": "evaluación", "commit": "commit", "target": "entorno", "evaluated": "evaluadas",
    "successful": "exitosas", "success_rate": "tasa de éxito", "total_cost_usd": "costo total ($)",
    "cost_per_success_usd": "costo por éxito ($)", "fast_path": "por incidente",
    "fast_path_cost_usd": "costo por incidente ($)", "median_accept_ms": "recepción, mediana (ms)",
    "median_result_s": "resultado, mediana (s)",
}


def dashboard_tab() -> None:
    runs = logic.load_runs(RESULTS_DIR)
    if not runs:
        st.info(f"No hay reportes de evaluación en {RESULTS_DIR}.")
        return
    last = runs[-1]
    st.markdown(f"Última evaluación: **{last['run']}**, commit `{last['commit']}`, entorno `{last['target']}`")
    cols = st.columns(5)
    cols[0].metric("Solicitudes evaluadas", last["evaluated"])
    cols[1].metric("Solicitudes exitosas", last["successful"])
    cols[2].metric("Tasa de éxito", f"{last['success_rate']:.0%}",
                   help="exitosas ÷ evaluadas: ¿lo hace bien?")
    cols[3].metric("Costo total", f"${last['total_cost_usd']:.4f}",
                   help="todas las llamadas al modelo, incluidos reintentos y respuestas equivocadas")
    cols[4].metric("Costo por éxito", f"${last['cost_per_success_usd']:.4f}" if last["cost_per_success_usd"] else "n/d",
                   help="costo total ÷ exitosas: ¿cuánto cuesta cada respuesta correcta?")
    st.caption(f"Camino rápido en esta evaluación: {last['fast_path']} disputa(s) decididas por un incidente "
               f"confirmado, ${last['fast_path_cost_usd']:.4f} de costo de modelo. Tiempo hasta recibirla "
               f"(mediana): {last['median_accept_ms']} ms · tiempo hasta el resultado (mediana): "
               f"{last['median_result_s']} s.")

    st.markdown("**Tasa de éxito y costo por éxito, evaluación por evaluación.** Ninguna basta sola: un "
                "sistema barato que falla seguido puede verse bien en costo, y uno que siempre acierta "
                "puede ser demasiado caro de operar.")
    history = pd.DataFrame(runs)
    chart_left, chart_right = st.columns(2)
    chart_left.line_chart(history.set_index("run")["success_rate"], y_label="tasa de éxito", x_label="evaluación",
                          color=MAGENTA)
    chart_right.line_chart(history.set_index("run")["cost_per_success_usd"], y_label="costo por éxito ($)",
                           x_label="evaluación", color=DARK)
    st.dataframe(history.rename(columns=RUN_COLUMNS), hide_index=True, width="stretch")
    st.markdown("**Última evaluación, escenario por escenario**")
    st.dataframe(pd.DataFrame(logic.latest_scenarios(RESULTS_DIR)), hide_index=True, width="stretch")

    judge_health()


def judge_health() -> None:
    """The LLM judge's calibration against labelled answers, run by run (ADR-0026)."""
    st.markdown("---")
    st.subheader("🧑‍⚖️ Salud del juez de IA")
    st.markdown("El juez califica cada explicación contra los registros. Se calibra contra respuestas etiquetadas: "
                "**acuerdo** (coincide con la etiqueta) y **aprobaciones inseguras** (la etiqueta dice que está mal y "
                "el juez la aprueba: el error peligroso). El conjunto **held-out** nunca se usa para ajustar el "
                "prompt: mide si lo aprendido generaliza.")
    rows = logic.load_calibrations(JUDGE_RESULTS_DIR)
    if not rows:
        st.info("No hay calibraciones del juez en esta imagen.")
        return
    latest = rows[-1]["run"]
    current = [r for r in rows if r["run"] == latest]
    cols = st.columns(len(current) * 2)
    for i, r in enumerate(sorted(current, key=lambda r: r["split"], reverse=True)):
        cols[2 * i].metric(f"Acuerdo · {r['split']}", f"{r['agreement']:.0%}")
        cols[2 * i + 1].metric(f"Aprobaciones inseguras · {r['split']}", r["unsafe_passes"])
    history = pd.DataFrame(rows)
    chart = history.pivot_table(index="prompt", columns="split", values="agreement")
    st.line_chart(chart, y_label="acuerdo", x_label="versión del prompt", color=[MAGENTA, DARK])
    show = history[["run", "prompt", "split", "cases", "agreement", "unsafe_passes",
                    "groundedness_agreement", "groundedness_unsafe", "clarity_unsafe"]]
    st.dataframe(show.rename(columns={"run": "calibración", "prompt": "prompt", "split": "conjunto", "cases": "casos",
                                      "agreement": "acuerdo", "unsafe_passes": "aprob. inseguras",
                                      "groundedness_agreement": "acuerdo · registros",
                                      "groundedness_unsafe": "inseguras · registros",
                                      "clarity_unsafe": "inseguras · claridad"}),
                 hide_index=True, width="stretch")
    try:
        recent = reviews().judgements(logic.login_reviewer(logic.REVIEWERS[0], login_settings()))
    except Exception:
        recent = []
    if recent:
        st.markdown("**Últimas disputas evaluadas por el juez**")
        st.dataframe(pd.DataFrame([{"disputa": str(j["dispute_id"])[:8], "transferencia": j["transaction_id"],
                                    "estado": j["business_status"], "juez": logic.judge_badge(j),
                                    "prompt": j["prompt_version"], "evaluada": str(j["judged_at"])[:16]}
                                   for j in recent]), hide_index=True, width="stretch")


# --- 3. The demo script ---------------------------------------------------------------------------


def script_tab() -> None:
    st.caption("El guion de la demostración, tal como está en el repositorio (docs/DEMO_SCRIPT.md, en inglés).")
    if DEMO_SCRIPT.exists():
        st.markdown(DEMO_SCRIPT.read_text())
    else:
        st.info("docs/DEMO_SCRIPT.md no está en esta imagen.")


HEADER = """
<div style="background:#200020;border-radius:1.25rem;padding:1.4rem 1.6rem;margin-bottom:0.8rem">
  <div style="color:#DA0081;font-weight:800;letter-spacing:.08em;font-size:.8rem">REVISIÓN DE DISPUTAS CON IA · DEMO EN VIVO</div>
  <div style="color:#FFFFFF;font-weight:800;font-size:1.9rem;line-height:1.2;margin:.3rem 0">
    ¿Tu plata no llegó? <span style="color:#DA0081">La revisamos, decidimos y te la devolvemos.</span></div>
  <div style="color:#ECE7F5;font-size:.95rem">Tres agentes de IA investigan sobre Azure Kubernetes; el código
    aprueba y paga, una sola vez. Hecho para una entrevista en Nequi.</div>
</div>
"""
st.markdown(HEADER, unsafe_allow_html=True)
st.caption("Demo independiente para una entrevista: no es un producto de Nequi ni está afiliada a Nequi. Solo "
           "clientes y datos sintéticos; nunca ingreses datos personales o bancarios reales. Cada cliente y "
           "transferencia se revisa una sola vez hasta que se reinicia la demo; si la envías de nuevo, verás la "
           "disputa existente, sin costo.")
customer, reviewer_tab, dashboard, script = st.tabs(
    ["📱 App del cliente", "👤 Revisión (supervisor)", "📊 Tablero de evaluación", "🗺️ Guion de la demo"])
with customer:
    customer_tab()
with reviewer_tab:
    review_tab()
with dashboard:
    dashboard_tab()
with script:
    script_tab()
