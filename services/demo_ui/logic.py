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


TRANSACTIONS: dict[str, list[DemoTransaction]] = {
    "user-1001": [
        DemoTransaction("TX-20261001000001", "50000.00", "50.000 to ****4821, failed",
                        "Scenario 1: three agents investigate; approved and paid"),
        DemoTransaction("TX-20261001000002", "450000.00", "450.000 to ****7710, failed",
                        "Over the automatic limit: a person decides"),
        DemoTransaction("TX-20261001000003", "80000.00", "80.000 to ****4821, completed",
                        "Settled normally: no refund is due"),
        DemoTransaction("TX-20261001000008", "50000.00", "50.000 to ****4821, failed",
                        "Scenario 5: try a prompt injection in the description"),
    ],
    "user-1002": [
        DemoTransaction("TX-20261001000009", "35000.00", "35.000 to Banco Andino at 09:10, failed",
                        "Scenario 2: covered by a confirmed incident; no model is used"),
        DemoTransaction("TX-20261001000011", "20000.00", "20.000 to Banco Andino at 10:30, failed",
                        "Scenario 2, side by side: the same failure after the window; the agents investigate"),
        DemoTransaction("TX-20261001000004", "30000.00", "30.000 to ****0093, failed",
                        "High fraud risk: the security team reviews it"),
        DemoTransaction("TX-20261001000005", "20000.00", "20.000 to ****5512, in progress",
                        "Still in flight: no decision yet"),
    ],
    "user-1003": [
        DemoTransaction("TX-20261001000010", "60000.00", "60.000 to Banco Andino at 09:25, failed",
                        "Scenario 3: after the batch refund it is already returned"),
        DemoTransaction("TX-20261001000007", "25000.00", "25.000 to ****3307, failed",
                        "Already 3 automatic refunds this month: a person decides"),
        DemoTransaction("TX-20261001000006", "40000.00", "40.000 to ****3307, reversed",
                        "Already refunded: nothing is due"),
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
    "received": "📥 Received",
    "investigating": "🔎 Investigating",
    "refund_approved": "✅ Refund approved (being paid)",
    "refund_paid": "💸 Refund paid",
    "pending_human_approval": "👤 With a person",
    "closed_no_refund": "📁 Closed, no refund due",
    "rejected": "⛔ Refund rejected",
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
        headline = "⚡ Decided without a model: 0 model calls, $0"
        detail = (f"Covered by confirmed incident **{incident['incident_id']}** "
                  f"({incident['title']}), confirmed by {incident['confirmed_by']}. Code read the ledger and the "
                  "risk engine and applied the same refund policy as every dispute.")
    elif any(step.startswith(("ledger_agent", "fraud_agent")) for step in steps):
        path = "agents"
        headline = "🤖 Investigated by the agents"
        detail = "The supervisor asked the Ledger and Fraud agents; code then applied the refund policy."
    else:
        path = "none"
        headline = "No investigation ran"
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
    """The latest run's scenarios, for a table."""
    reports = sorted(results_dir.glob("*.json"))
    if not reports:
        return []
    results = json.loads(reports[-1].read_text())["results"]
    return [{"scenario": r["id"], "success": "yes" if r.get("task_success") else "NO",
             "path": "⚡ incident" if (r.get("actual") or {}).get("incident_id") else "🤖 agents",
             "model calls": (r.get("actual") or {}).get("model_calls"),
             "result (s)": round(r["latency_ms"] / 1000, 1), "cost ($)": round(r.get("cost_usd") or 0, 4),
             "outcome": (r.get("actual") or {}).get("status")} for r in results]


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
