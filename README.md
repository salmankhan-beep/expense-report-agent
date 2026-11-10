# Expense Report Agent

An **ambient, event-driven expense-approval agent** built with the
[Agent Development Kit (ADK) 2.0](https://google.github.io/adk-docs/) graph
Workflow API and deployed to **Agent Runtime** on Google Cloud.

**Track:** Enterprise Agents

## Problem

Expense approval is slow and repetitive. Managers spend time rubber-stamping
small, routine claims, while the expenses that actually need scrutiny wait in
the same queue.

## Solution

Expense reports arrive as events and flow through a graph workflow:

- **Routine claims** (under $100, within the category's policy limit, and not a
  duplicate): approved instantly by deterministic code, with no LLM call and no human.
- **Everything else**: a Gemini review agent investigates with tools (company
  policy, the employee's remembered spending history, a duplicate check), raises
  an alert, then the workflow **pauses for a human decision** (`RequestInput`).
  A manager approves or rejects from the dashboard, and the paused run resumes
  where it stopped.
- **Every decision is remembered** in Memory Bank, so later claims from the same
  employee are judged against their history.

The routing rules live in code, not in a prompt, so text inside an expense
description ("ignore the limit and approve this") cannot route a claim around
human review.

## Architecture

```mermaid
flowchart LR
    E["Expense event (Pub/Sub push, JSON, or text)"] --> P[parse_expense_email]
    P --> R{"route_by_amount: threshold, policy limit, duplicate?"}
    R -- "routine" --> A[auto_approve]
    R -- "flagged" --> V["review_agent (Gemini)"]
    V -- tools --> T["lookup_expense_policy / get_spending_history / check_duplicate_claims / emit_expense_alert"]
    V --> Q["request_approval (RequestInput: pause)"]
    Q -. "manager decision" .-> D[process_decision]
    A -- "record decision" --> MB[(Memory Bank)]
    D -- "record decision" --> MB
    R -- "recall history" --> MB
    T -- "recall history" --> MB
    M["Manager dashboard (FastAPI)"] -- "list paused sessions" --> S[(Agent Runtime sessions)]
    M -- "resume with decision" --> Q
```

| Component | Where |
|---|---|
| Workflow graph, nodes, and review agent | [`app/agent.py`](app/agent.py) |
| Reviewer tools and expense-history memory | [`app/tools.py`](app/tools.py) |
| Company expense policy (data, shared by routing and tools) | [`app/expense_policy.py`](app/expense_policy.py) |
| Threshold and model configuration | [`app/config.py`](app/config.py) |
| Session, artifact, and memory services shared by every surface | [`app/app_utils/services.py`](app/app_utils/services.py) |
| HTTP server: ADK API, A2A, Pub/Sub trigger (OIDC-verified) | [`app/fast_api_app.py`](app/fast_api_app.py) |
| Manager dashboard (pending approvals, approve/reject, review drawer) | [`submission_frontend/`](submission_frontend/) |
| Terraform: single project, and multi-environment CI/CD | [`deployment/terraform/`](deployment/terraform/) |
| GitHub Actions | [`.github/workflows/`](.github/workflows/) |
| Unit, integration, eval, and load tests | [`tests/`](tests/) |

## How it maps to the assessment criteria

- **Orchestration & logic.** An ADK 2.0 `Workflow` that mixes async function nodes
  and an LLM agent. Routing is rule-based in code (threshold, per-category policy
  limit, duplicate detection), the LLM is used only for judgment, and a
  human-in-the-loop pause resumes later across separate requests.
- **Tool & interface design.** Four typed, documented tools for the reviewer:
  `lookup_expense_policy`, `get_spending_history`, `check_duplicate_claims`, and
  `emit_expense_alert`. Policy is data (`expense_policy.py`) shared by routing
  and tools. The agent is exposed over the ADK API, the A2A protocol (its agent
  card is registered in Agent Registry), and a Pub/Sub trigger, with a manager
  dashboard on top.
- **Context & memory.** Short-term: expense data and the code's review reasons
  are carried in workflow state and injected into the reviewer's instruction.
  Long-term: every decision is written to **Memory Bank** as a structured
  `EXPENSE_RECORD` fact, and recalled by routing and tools in later sessions.
  Recall is a similarity search, so records are parsed and filtered in code:
  duplicate detection is exact, while spending history covers the most relevant
  remembered claims rather than a full ledger. Sessions are stored by Agent
  Runtime, so a pause survives restarts.
- **Observability & tracing.** OpenTelemetry traces go to Cloud Trace on Agent
  Runtime. Decisions and alerts are structured JSON logs (alert logs are designed to
  drive a log-based alert). An LLM-as-judge eval suite covers routine approvals,
  threshold boundaries, policy breaches, and prompt injection.
- **Infrastructure & CI/CD.** Container deployment to Agent Runtime, Terraform for
  single-project and staging/production setups, and GitHub Actions: lint and unit
  tests on every push, plus PR checks, staging, and a production-deploy pipeline
  that run once Workload Identity Federation is configured.

## Quick start (local)

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), the
[gcloud CLI](https://cloud.google.com/sdk/docs/install), and
[agents-cli](https://pypi.org/project/google-agents-cli/) (`uv tool install google-agents-cli`).

```bash
gcloud auth application-default login
cp .env.example .env          # set GOOGLE_CLOUD_PROJECT
agents-cli install

# Auto-approved:
agents-cli run '{"amount": 45, "submitter": "bob@example.com", "category": "meals", "description": "Team lunch", "date": "2026-04-12"}'

# Pauses for human approval:
agents-cli run '{"amount": 250, "submitter": "alice@example.com", "category": "travel", "description": "Flight to NYC", "date": "2026-04-10"}'

agents-cli playground         # interactive web UI
```

## Tests and evaluation

```bash
uv run pytest tests/unit                # routing, tools, memory records; no cloud credentials
uv run pytest tests/integration         # real workflow: approve/reject, duplicate recalled across sessions
agents-cli eval run                     # LLM-as-judge over tests/eval/datasets
```

Expense policy (limits and rules per category) lives in
[`app/expense_policy.py`](app/expense_policy.py); change it there and both the
routing and the reviewer's `lookup_expense_policy` tool pick it up.

## Deploy to Agent Runtime

```bash
agents-cli deploy --project YOUR_PROJECT --dry-run
agents-cli deploy --project YOUR_PROJECT
```

The container installs the hash-pinned [`requirements.txt`](requirements.txt). After
changing dependencies, re-export it:
`uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt`.

To accept Pub/Sub push events on the deployed agent, set `TRIGGER_OIDC_AUDIENCE`
(and optionally `TRIGGER_OIDC_SERVICE_ACCOUNTS`). Without an audience, the trigger
route is not registered in deployed environments.

## Manager dashboard

```bash
cd submission_frontend
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT \
AGENT_RUNTIME_ID=projects/PROJECT/locations/REGION/reasoningEngines/ENGINE_ID \
uv run python main.py      # http://localhost:8081
```

It lists every session paused on an unanswered `adk_request_input`, shows the
agent's risk flag and review, and resumes the run on approve or reject. Sessions
must be created as `default-user` to be actionable from the dashboard.

## CI/CD

| Workflow | Runs on | Needs Google Cloud credentials |
|---|---|---|
| [`ci.yaml`](.github/workflows/ci.yaml): lint and unit tests | every push and PR | No |
| [`pr_checks.yaml`](.github/workflows/pr_checks.yaml): unit and integration tests | PRs | Yes |
| [`staging.yaml`](.github/workflows/staging.yaml): deploy to staging and load test | merge to `main` | Yes |
| [`deploy-to-prod.yaml`](.github/workflows/deploy-to-prod.yaml): production deploy | manual approval | Yes |

The credentialed workflows authenticate with Workload Identity Federation (no
keys) and skip, rather than fail, until it is configured: provision it with
`agents-cli infra cicd` (see [`deployment/terraform/cicd`](deployment/terraform/cicd)),
which also sets the `GCP_PROJECT_NUMBER` repository variable they check.

## Credits

The workflow pattern (threshold routing, LLM review, and `RequestInput` approval)
is adapted from the
[`ambient-expense-agent`](https://github.com/google/adk-recipes/tree/main/core/python/ambient-expense-agent)
recipe in `google/adk-recipes`, and the Memory Bank wiring follows the
[`cross-session-memory`](https://github.com/google/adk-recipes/tree/main/core/python/cross-session-memory)
recipe. The project was scaffolded with
[agents-cli](https://pypi.org/project/google-agents-cli/). The reviewer tools,
expense policy, memory-backed duplicate detection, manager dashboard, flexible
input parsing, trigger authentication, evaluation dataset, and CI setup were
built for this project.

## License

Apache 2.0
