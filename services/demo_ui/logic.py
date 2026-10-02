"""Everything the demo UI decides, as plain Python (ADR-0023). `app.py` only lays it out.

The UI plays two roles, both for the demo only:
  - the customer's Nequi app: it submits disputes to the intake API and follows their status
  - the bank's login: it signs a short-lived login token for the chosen synthetic customer, with
    the demo identity provider's private key (read from Key Vault in the cluster). The intake API
    verifies that token exactly as it would a real one; the UI gets no other privilege.

What the customer sees is the STORED business status of the dispute (ADR-0016), polled from the
intake API, never a guess from which step is running.
"""

import json
import os
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt

# --- The synthetic customers and their transactions (services/core_systems/adapters/fixtures.py) ---


@dataclass(frozen=True)
class DemoTransaction:
    transaction_id: str
    amount: str  # what the customer believes they lost, as they would type it
    label: str  # how the app lists it
    story: str  # what the demo shows with it

    @property
    def display(self) -> str:
        """The label, plus the end of the ID: two transfers can look alike, an ID cannot."""
        return f"{self.label} · …{self.transaction_id[-4:]}"


TRANSACTIONS: dict[str, list[DemoTransaction]] = {
    "user-1001": [
        DemoTransaction("TX-20261001000001", "50000.00", "50.000 a ****4821 a las 09:00, fallida",
                        "Escenario 1: tres agentes investigan; se aprueba y se paga"),
        DemoTransaction("TX-20261001000002", "450000.00", "450.000 a ****7710 a las 07:00, fallida",
                        "Supera el límite automático: decide una persona"),
        DemoTransaction("TX-20261001000003", "80000.00", "80.000 a ****4821 a las 06:00, completada",
                        "Se completó con normalidad: no corresponde reembolso"),
        DemoTransaction("TX-20261001000008", "50000.00", "50.000 a ****4821 a las 08:00, fallida",
                        "Escenario 5: prueba una inyección de instrucciones en la descripción"),
    ],
    "user-1002": [
        DemoTransaction("TX-20261001000009", "35000.00", "35.000 a Banco Andino a las 09:10, fallida",
                        "Escenario 2: cubierta por un incidente confirmado; no se usa ningún modelo"),
        DemoTransaction("TX-20261001000011", "20000.00", "20.000 a Banco Andino a las 10:30, fallida",
                        "Escenario 2, lado a lado: la misma falla después de la ventana; investigan los agentes"),
        DemoTransaction("TX-20261001000004", "30000.00", "30.000 a ****0093 a las 11:00, fallida",
                        "Riesgo de fraude alto: la revisa el equipo de seguridad"),
        DemoTransaction("TX-20261001000005", "20000.00", "20.000 a ****5512 a las 12:00, en curso",
                        "Todavía en curso: aún no hay decisión"),
    ],
    "user-1003": [
        DemoTransaction("TX-20261001000010", "60000.00", "60.000 a Banco Andino a las 09:25, fallida",
                        "Escenario 3: después del reembolso masivo ya está devuelta"),
        DemoTransaction("TX-20261001000007", "25000.00", "25.000 a ****3307 a las 10:00, fallida",
                        "Ya tiene 3 reembolsos automáticos este mes: decide una persona"),
        DemoTransaction("TX-20261001000006", "40000.00", "40.000 a ****3307 hace dos días, reversada",
                        "Ya fue reembolsada: no se debe nada"),
    ],
}

DEFAULT_DESCRIPTION = "Envié plata y no llegó"


def find_transaction(user_id: str, transaction_id: str) -> DemoTransaction:
    return next(t for t in TRANSACTIONS[user_id] if t.transaction_id == transaction_id)


# --- Login (the demo identity provider) -----------------------------------------------------------


@dataclass(frozen=True)
class LoginSettings:
    private_key_pem: str
    issuer: str
    audience: str
    lifetime: timedelta = timedelta(minutes=15)

    @classmethod
    def from_env(cls) -> "LoginSettings":
        key_file = os.environ.get("DEMO_IDP_PRIVATE_KEY_FILE", ".local/jwt-private.pem")
        return cls(private_key_pem=Path(key_file).read_text(), issuer=os.environ["JWT_ISSUER"],
                   audience=os.environ["JWT_AUDIENCE"])


def login(user_id: str, settings: LoginSettings) -> str:
    """A short-lived login token for a synthetic customer, as the bank's login would issue."""
    if user_id not in TRANSACTIONS:
        raise ValueError(f"{user_id} is not a demo customer")
    now = datetime.now(UTC)
    claims = {"iss": settings.issuer, "aud": settings.audience, "sub": user_id, "jti": uuid.uuid4().hex,
              "iat": now, "exp": now + settings.lifetime}
    return jwt.encode(claims, settings.private_key_pem, algorithm="RS256")


# --- The intake API ---------------------------------------------------------------------------


class IntakeClient:
    """The two calls a customer's app makes: submit a dispute, and read it back."""

    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=30)

    def submit(self, token: str, transaction: DemoTransaction, description: str) -> httpx.Response:
        body = {"transaction_id": transaction.transaction_id, "reason": "failed_transfer",
                "claimed_amount": transaction.amount, "description": description[:1000]}
        return self._http.post("/v1/disputes", json=body, headers={"Authorization": f"Bearer {token}"})

    def get(self, token: str, dispute_id: str) -> dict:
        response = self._http.get(f"/v1/disputes/{dispute_id}", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()


def is_settled(view: dict) -> bool:
    """Nothing more will change without a person. A finished run that is still refund_approved is
    waiting for its payment (ADR-0021), so it is not settled yet."""
    if view.get("execution_status") == "failed":
        return True
    return view.get("execution_status") == "finished" and view.get("status") != "refund_approved"


STATUS_LABELS = {
    "received": "📥 Recibida",
    "investigating": "🔎 En revisión",
    "refund_approved": "✅ Reembolso aprobado (pagándose)",
    "refund_paid": "💸 Reembolso pagado",
    "pending_human_approval": "👤 La revisa una persona",
    "closed_no_refund": "📁 Cerrada, no corresponde reembolso",
    "rejected": "⛔ Reembolso rechazado",
}


@dataclass
class Progress:
    """The business statuses a dispute went through, as the customer's app saw them."""

    started: float  # time.monotonic() at submission
    seen: list[tuple[str, float]] = field(default_factory=list)  # (status, seconds since submission)

    def observe(self, view: dict, now: float) -> None:
        status = view.get("status")
        if status and (not self.seen or self.seen[-1][0] != status):
            self.seen.append((status, round(now - self.started, 1)))

    def lines(self) -> list[str]:
        return [f"{STATUS_LABELS.get(status, status)} · {at:.1f} s" for status, at in self.seen]


# --- Reading an outcome ------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    path: str  # "incident" | "agents" | "none"
    headline: str
    detail: str
    checks: list[dict]  # the refund policy's checks, for a table
    steps: list[str]
    payment: dict | None
    trace_url: str | None


def read_outcome(view: dict) -> Outcome:
    result = view.get("result") or {}
    incident = result.get("incident")
    approval = result.get("approval") or {}
    steps = result.get("steps") or []
    if incident:
        path = "incident"
        headline = "⚡ Decidida sin un modelo: 0 llamadas al modelo, $0"
        detail = (f"Cubierta por el incidente confirmado **{incident['incident_id']}** "
                  f"({incident['title']}), confirmado por {incident['confirmed_by']}. El código leyó el libro "
                  "contable y el motor de riesgo y aplicó la misma política de reembolsos que a toda disputa.")
    elif any(step.startswith(("ledger_agent", "fraud_agent")) for step in steps):
        path = "agents"
        headline = "🤖 Investigada por los agentes"
        detail = "El supervisor consultó a los agentes de libro contable y de fraude; luego el código aplicó la política de reembolsos."
    else:
        path = "none"
        headline = "No se hizo ninguna investigación"
        detail = result.get("escalation_reason") or ""
    return Outcome(path=path, headline=headline, detail=detail, checks=approval.get("checks") or [],
                   steps=steps, payment=result.get("payment"), trace_url=result.get("trace_url"))


# --- The evaluation dashboard --------------------------------------------------------------------


def load_runs(results_dir: Path) -> list[dict]:
    """One row per evaluation run, oldest first: the counts and total cost, and the two ratios
    built from them (success rate, cost per success), recomputed here so they can be checked."""
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            report = json.loads(path.read_text())
            meta, results = report["meta"], report["results"]
        except (ValueError, KeyError):
            continue
        evaluated = len(results)
        successful = sum(1 for r in results if r.get("task_success"))
        total_cost = sum(r.get("cost_usd") or 0 for r in results)
        fast = [r for r in results if (r.get("actual") or {}).get("incident_id")]
        accept = [r["accept_ms"] for r in results if r.get("accept_ms") is not None]
        latency = [r["latency_ms"] for r in results if r.get("latency_ms") is not None]
        rows.append({
            "run": meta.get("date", path.stem), "commit": meta.get("commit"), "target": meta.get("target"),
            "evaluated": evaluated, "successful": successful,
            "success_rate": successful / evaluated if evaluated else None,
            "total_cost_usd": round(total_cost, 4),
            "cost_per_success_usd": round(total_cost / successful, 4) if successful else None,
            "fast_path": len(fast),
            "fast_path_cost_usd": round(sum(r.get("cost_usd") or 0 for r in fast), 4),
            "median_accept_ms": round(statistics.median(accept)) if accept else None,
            "median_result_s": round(statistics.median(latency) / 1000, 1) if latency else None,
        })
    return rows


def latest_scenarios(results_dir: Path) -> list[dict]:
    """The latest run's scenarios, for a table. The path says what decided it: a confirmed
    incident, the agents, or nothing (a duplicate replayed from the gate, a request refused)."""
    reports = sorted(results_dir.glob("*.json"))
    if not reports:
        return []
    rows = []
    for r in json.loads(reports[-1].read_text())["results"]:
        actual = r.get("actual") or {}
        model_calls = actual.get("model_calls")
        if actual.get("incident_id"):
            path = "⚡ incidente"
        elif model_calls or (model_calls is None and r.get("cost_usd")):
            path = "🤖 agentes"
        else:
            path = "— sin revisión"
        rows.append({"escenario": r["id"], "éxito": "sí" if r.get("task_success") else "NO", "camino": path,
                     "llamadas al modelo": model_calls, "resultado (s)": round(r["latency_ms"] / 1000, 1),
                     "costo ($)": round(r.get("cost_usd") or 0, 4), "resultado": actual.get("status") or "—"})
    return rows


# --- The trace, shown inside the demo (read from Langfuse) ---------------------------------------
#
# Each run is traced to Langfuse (ADR-0013). Instead of sending the viewer to Langfuse, the UI
# reads the trace's observations from Langfuse's public API and draws them. A trace is ingested
# asynchronously, so a trace read seconds after the run can still be arriving: the UI says so and
# offers to read it again.

# LangChain internals: true but noisy. Hidden unless the viewer asks for every step.
_INTERNAL = ("ModelCallLimitMiddleware", "PydanticToolsParser", "RunnableSequence", "RunnableLambda",
             "ChannelWrite", "_after_", "StructuredOutput")
_INTERNAL_EXACT = {"model", "tools", "LangGraph"}

_NODE_LABELS = {
    "dispute-triage": "📨 Dispute triage (the whole run)",
    "supervisor": "🧭 Supervisor decides the next step",
    "ledger_agent": "📒 Ask the Ledger Agent",
    "fraud_agent": "🛡️ Ask the Fraud Agent",
    "write_verdict": "✍️ Supervisor writes its recommendation",
    "policy": "⚙️ Refund policy (code)",
    "escalate": "👤 Hand to a person",
    "ledger-agent": "📒 Ledger Agent service",
    "fraud-agent": "🛡️ Fraud Agent service",
}


def trace_id_from_url(trace_url: str | None) -> str | None:
    return trace_url.rstrip("/").rsplit("/", 1)[-1] if trace_url else None


class LangfuseReader:
    """Reads one trace's observations. Needs the project's keys (ADR-0025)."""

    def __init__(self, base_url: str, public_key: str, secret_key: str, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), auth=(public_key, secret_key), timeout=20)

    @classmethod
    def from_env(cls) -> "LangfuseReader | None":
        """None when no keys are configured: the UI then only links to the trace."""
        base = os.environ.get("LANGFUSE_BASE_URL", "").strip()
        public = _secret("LANGFUSE_PUBLIC_KEY")
        secret = _secret("LANGFUSE_SECRET_KEY")
        return cls(base, public, secret) if base and public and secret else None

    def observations(self, trace_id: str) -> list[dict]:
        response = self._http.get("/api/public/v2/observations", params={
            "traceId": trace_id, "limit": 500, "fields": "core,basic,usage,model,io"})
        response.raise_for_status()
        return response.json().get("data", [])


def _secret(name: str) -> str:
    """A value from NAME, or from the file NAME_FILE (Key Vault, mounted by the CSI driver)."""
    if value := os.environ.get(name, "").strip():
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    return Path(path).read_text().strip() if path and Path(path).exists() else ""


def _is_internal(observation: dict) -> bool:
    name = observation.get("name") or ""
    return name in _INTERNAL_EXACT or name.startswith(_INTERNAL)


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _label(observation: dict) -> str:
    kind, name = observation.get("type"), observation.get("name") or "?"
    if kind == "GENERATION":
        return f"🧠 Model call ({observation.get('model') or 'model'})"
    if kind == "TOOL":
        try:
            args = json.loads(observation.get("input") or "{}")
        except (TypeError, ValueError):
            args = {}
        shown = ", ".join(f"{v}" for v in args.values()) if isinstance(args, dict) else ""
        return f"🔧 {name}({shown})"
    return _NODE_LABELS.get(name, name)


@dataclass(frozen=True)
class TraceView:
    rows: list[dict]  # in time order, each with depth, label, offsets, tokens, cost, input, output
    model_calls: int
    tool_calls: int
    tokens: int
    cost_usd: float
    duration_s: float
    hidden: int  # internal steps not shown


def trace_view(observations: list[dict], include_internal: bool = False) -> TraceView:
    """A readable trace: the meaningful steps in time order, indented under their parents. The
    totals always count every model call, shown or not."""
    by_id = {o["id"]: o for o in observations}
    shown = {o["id"] for o in observations if include_internal or not _is_internal(o)}

    def depth(o: dict) -> int:
        level, parent = 0, o.get("parentObservationId")
        while parent in by_id:
            if parent in shown:
                level += 1
            parent = by_id[parent].get("parentObservationId")
        return level

    starts = [t for o in observations if (t := _when(o.get("startTime")))]
    ends = [t for o in observations if (t := _when(o.get("endTime")))]
    origin = min(starts) if starts else None
    rows = []
    for o in sorted(observations, key=lambda o: o.get("startTime") or ""):
        if o["id"] not in shown:
            continue
        start, end = _when(o.get("startTime")), _when(o.get("endTime"))
        usage = o.get("usageDetails") or {}
        rows.append({
            "depth": depth(o), "label": _label(o), "type": o.get("type"), "name": o.get("name"),
            "start_ms": round((start - origin).total_seconds() * 1000) if start and origin else 0,
            "end_ms": round((end - origin).total_seconds() * 1000) if end and origin else None,
            "tokens": usage.get("total") or 0, "cost_usd": o.get("totalCost") or 0.0,
            "input": o.get("input"), "output": o.get("output"),
        })
    generations = [o for o in observations if o.get("type") == "GENERATION"]
    return TraceView(
        rows=rows, model_calls=len(generations),
        tool_calls=sum(1 for o in observations if o.get("type") == "TOOL"),
        tokens=sum((o.get("usageDetails") or {}).get("total") or 0 for o in generations),
        cost_usd=round(sum(o.get("totalCost") or 0 for o in generations), 6),
        duration_s=round((max(ends) - origin).total_seconds(), 1) if ends and origin else 0.0,
        hidden=len(observations) - len(shown),
    )


# Graph steps that call an agent service, and the name that service's part of the trace has.
_AGENT_SERVICES = {"ledger_agent": "ledger-agent", "fraud_agent": "fraud-agent"}


def missing_from_trace(observations: list[dict], steps: list[str]) -> list[str]:
    """Agent services the dispute's steps say ran, but whose part of the trace has not arrived
    yet. Each service sends its part separately and Langfuse ingests asynchronously, so a trace
    read early is silently partial (LEARNINGS entry 8): say so instead of showing it as complete."""
    ran = {_AGENT_SERVICES[node] for step in steps
           for node in [step.split(":")[0].split(" ->")[0].strip()] if node in _AGENT_SERVICES}
    arrived = {o.get("name") for o in observations if o.get("type") == "AGENT"}
    return sorted(ran - arrived)


# --- The reviewer: a person who decides what the system sent to them (ADR-0027) ------------------

REVIEWERS = ["ops-ana", "ops-luis"]  # synthetic bank employees with the reviewer role


def login_reviewer(reviewer_id: str, settings: LoginSettings) -> str:
    """A short-lived token for a synthetic reviewer: the same signature as a customer's, plus the
    reviewer role. The intake API checks the role on every review route."""
    if reviewer_id not in REVIEWERS:
        raise ValueError(f"{reviewer_id} is not a demo reviewer")
    now = datetime.now(UTC)
    claims = {"iss": settings.issuer, "aud": settings.audience, "sub": reviewer_id, "jti": uuid.uuid4().hex,
              "iat": now, "exp": now + settings.lifetime, "roles": ["dispute-reviewer"]}
    return jwt.encode(claims, settings.private_key_pem, algorithm="RS256")


class ReviewClient:
    """The reviewer's calls to the intake API."""

    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=30)

    def _get(self, token: str, path: str):
        response = self._http.get(path, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()

    def queue(self, token: str) -> list[dict]:
        return self._get(token, "/v1/reviews/queue")

    def dispute(self, token: str, dispute_id: str) -> dict:
        return self._get(token, f"/v1/reviews/disputes/{dispute_id}")

    def judgements(self, token: str) -> list[dict]:
        return self._get(token, "/v1/reviews/judgements")

    def follow_ups(self, token: str) -> list[dict]:
        return self._get(token, "/v1/reviews/follow-ups")

    def resolve(self, token: str, dispute_id: str, note: str) -> httpx.Response:
        return self._http.post(f"/v1/reviews/follow-ups/{dispute_id}/resolve", json={"note": note},
                               headers={"Authorization": f"Bearer {token}"})

    def decide(self, token: str, dispute_id: str, decision: str, note: str) -> httpx.Response:
        return self._http.post(f"/v1/reviews/disputes/{dispute_id}/decision", json={"decision": decision, "note": note},
                               headers={"Authorization": f"Bearer {token}"})


JUDGE_LABELS = {True: "✅ aprobada", False: "❌ con problemas", None: "⚠️ no se pudo evaluar"}


def judge_badge(judgement: dict | None) -> str:
    return "— sin evaluar" if not judgement else JUDGE_LABELS[judgement.get("passed")]


def why_a_person(result: dict | None) -> str:
    """Why the dispute is waiting for a person, in the reviewer's language."""
    result = result or {}
    if reason := result.get("escalation_reason"):
        return reason
    failed = [c["name"] for c in (result.get("approval") or {}).get("checks", []) if not c["passed"]]
    if failed:
        return "reglas que no se cumplieron: " + ", ".join(failed)
    if (result.get("verdict") or {}).get("decision") == "escalate_fraud":
        return "riesgo de fraude: lo revisa el equipo de seguridad"
    return "la revisión automática no terminó"


def queue_rows(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        dispute = item["dispute"]
        rows.append({"disputa": dispute["dispute_id"][:8], "cliente": item["customer_id"],
                     "transferencia": dispute["transaction_id"], "monto reclamado": item["request"].get("claimed_amount"),
                     "por qué llegó a una persona": why_a_person(dispute.get("result")),
                     "juez": judge_badge(item.get("judgement")), "recibida": dispute["created_at"][:16].replace("T", " ")})
    return rows


def ledger_owed(result: dict | None) -> str | None:
    """What the ledger showed owed when the system looked, for the reviewer's information. The
    approval itself re-reads the ledger: this figure is never what gets paid."""
    ledger = (result or {}).get("ledger")
    if not ledger:
        return None
    from decimal import Decimal

    return f"{Decimal(ledger['debited_amount']) - Decimal(ledger['credited_amount'])} {ledger.get('currency', 'COP')}"


CRITERIA_ES = {"groundedness": "Basada en los registros", "completeness": "Completa", "clarity": "Clara"}


def judgement_rows(judgement: dict | None) -> list[dict]:
    result = (judgement or {}).get("result") or {}
    return [{"criterio": CRITERIA_ES[name], "resultado": "✅ cumple" if result[name]["passed"] else "❌ no cumple",
             "razón del juez": result[name]["reason"]} for name in CRITERIA_ES if name in result]


# --- The judge's health: calibration runs (ADR-0026) ------------------------------------------------


def load_calibrations(results_dir: Path) -> list[dict]:
    """One row per calibration run and split: agreement and unsafe passes, oldest first."""
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            run = json.loads(path.read_text())
        except ValueError:
            continue
        for split, report in run.get("report", {}).items():
            row = {"run": run["meta"]["date"], "prompt": run["meta"].get("prompt_version", "?"),
                   "split": "tuning" if split == "tuning" else "held-out", "cases": report["cases"],
                   "agreement": report["overall_agreement"], "unsafe_passes": report["unsafe_passes"]}
            for name, c in report["criteria"].items():
                row[f"{name}_agreement"] = c["agreement"]
                row[f"{name}_unsafe"] = c["unsafe_passes"]
            rows.append(row)
    return rows
