"""Redo a pipeline stage: start the repository's stage workflow and read back how its runs went.

The repository that commits ``.prism-pipeline.json`` may also carry a GitHub Actions
workflow that redoes one stage of one board and proposes the result as a pull request
(pcb-agent-research: ``.github/workflows/rerun-stage.yml``). Prism only starts it with
``workflow_dispatch`` and shows the run's state on the stage row; it runs no agent and
holds no agent credential.

Contract with the workflow: inputs ``board`` and ``stage``; the run is named
``rerun <board> stage <n>``; its pull request comes from the branch
``repair/<board lowercase>-stage<n>-<run id>``. A run that fails on a credential check
stops in a step named ``Subscription only``, ``Claude sign-in`` or ``Stand access``; the row names that cause.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
REPAIRABLE_STAGES = range(6, 11)  # stages 1-5 change the circuit: a new run from the task, not a repair
RUN_NAME = re.compile(r"^rerun (\S+) stage (\d+)$")
RUNS_PAGE = 30
REQUEST_TIMEOUT_S = 20
# failed workflow step -> what the stage row says; any other failure stays "зупинено, без PR"
STEP_CAUSES = {
    "Subscription only": ("billing", "оплата не з підписки"),
    "Claude sign-in": ("token", "токен Claude не діє"),
    "Stand access": ("stand", "немає доступу до стенда"),
}
# the failed step of a finished run never changes: one request per run, not one per poll
_failed_steps: dict[tuple[str, Any], str] = {}


class RerunError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RerunConfig:
    repo: str
    workflow: str
    ref: str
    token: str

    @property
    def configured(self) -> bool:
        return bool(self.repo and self.token)


def config_from_settings() -> RerunConfig:
    from app.core.config import settings

    return RerunConfig(
        repo=settings.PRISM_PIPELINE_RERUN_REPO.strip(),
        workflow=settings.PRISM_PIPELINE_RERUN_WORKFLOW.strip(),
        ref=settings.PRISM_PIPELINE_RERUN_REF.strip(),
        token=settings.PRISM_PIPELINE_RERUN_TOKEN.strip(),
    )


# (method, path, params, json body) -> (status code, parsed body); tests pass a fake
Http = Callable[[str, str, Optional[dict[str, Any]], Optional[dict[str, Any]]], tuple[int, Any]]


def github_http(config: RerunConfig) -> Http:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {config.token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def call(method: str, path: str, params: Optional[dict[str, Any]], body: Optional[dict[str, Any]]):
        try:
            response = requests.request(method, f"{API_ROOT}{path}", headers=headers, params=params, json=body,
                                        timeout=REQUEST_TIMEOUT_S, allow_redirects=False)
        except requests.RequestException as exc:
            raise RerunError(f"GitHub could not be reached: {exc}") from exc
        parsed = response.json() if response.content else {}
        return response.status_code, parsed

    return call


def _branch(board: str, stage: int, run_id: Any) -> str:
    return f"repair/{board.lower()}-stage{stage}-{run_id}"


def _minutes(started: Optional[str], now: datetime) -> int:
    if not started:
        return 0
    begun = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return max(0, int((now - begun).total_seconds() // 60))


def _check(status: int, body: Any, what: str) -> None:
    if status in (401, 403, 404):
        raise RerunError(f"GitHub refused {what} ({status}): the token needs Actions read/write and Pull requests read "
                         "on this repository", status_code=502)
    if status >= 400:
        message = body.get("message") if isinstance(body, dict) else ""
        raise RerunError(f"GitHub rejected {what} ({status}): {message}", status_code=502)


def _failed_step(run: dict[str, Any], config: RerunConfig, http: Http) -> str:
    key = (config.repo, run.get("id"))
    if key not in _failed_steps:
        status, body = http("GET", f"/repos/{config.repo}/actions/runs/{run.get('id')}/jobs", None, None)
        _check(status, body, "the steps of a stage run")
        steps = [step for job in body.get("jobs") or [] for step in job.get("steps") or []]
        _failed_steps[key] = next((str(step.get("name")) for step in steps if step.get("conclusion") == "failure"), "")
    return _failed_steps[key]


def stage_runs(board: str, config: RerunConfig, http: Http, now: Optional[datetime] = None) -> dict[int, dict[str, Any]]:
    """The latest run of every stage of ``board`` that still needs the reader's attention, by stage number.

    A run whose pull request was merged or closed is left out: the stage row then shows the
    repository's new verdict, and the run is history.
    """

    now = now or datetime.now(timezone.utc)
    status, body = http("GET", f"/repos/{config.repo}/actions/workflows/{config.workflow}/runs",
                        {"per_page": RUNS_PAGE}, None)
    _check(status, body, "the list of stage runs")
    latest: dict[int, dict[str, Any]] = {}
    for run in body.get("workflow_runs") or []:  # newest first
        match = RUN_NAME.match(str(run.get("display_title") or run.get("name") or ""))
        if not match or match.group(1) != board:
            continue
        stage = int(match.group(2))
        if stage not in latest:
            latest[stage] = run

    result = {}
    for stage, run in latest.items():
        url = str(run.get("html_url") or "")
        if run.get("status") != "completed":
            if run.get("status") in ("queued", "waiting", "pending", "requested"):
                result[stage] = {"state": "queued", "text": "у черзі", "url": url}
            else:
                minutes = _minutes(run.get("run_started_at"), now)
                result[stage] = {"state": "running", "text": f"агент працює, {minutes} хв", "url": url}
            continue
        owner = config.repo.split("/")[0]
        status, pulls = http("GET", f"/repos/{config.repo}/pulls",
                             {"head": f"{owner}:{_branch(board, stage, run.get('id'))}", "state": "all"}, None)
        _check(status, pulls, "the pull request of a stage run")
        pull = pulls[0] if isinstance(pulls, list) and pulls else None
        if pull is None:
            cause = STEP_CAUSES.get(_failed_step(run, config, http)) if run.get("conclusion") == "failure" else None
            state, text = cause or ("stopped", "зупинено, без PR")
            result[stage] = {"state": state, "text": text, "url": url}
        elif pull.get("state") != "open":
            continue
        elif pull.get("draft"):
            result[stage] = {"state": "draft", "text": "не вдалось, PR-чернетка", "url": str(pull.get("html_url") or url)}
        else:
            result[stage] = {"state": "review", "text": "PR на огляд", "url": str(pull.get("html_url") or url)}
    return result


def start(board: str, stage: int, config: RerunConfig, http: Http) -> None:
    """Dispatch the stage workflow, unless a run of this board is still going (the workflow runs one per board)."""

    if stage not in REPAIRABLE_STAGES:
        raise RerunError("Only stages 6-10 are redone; stages 1-5 change the circuit.", status_code=422)
    going = [s for s, run in stage_runs(board, config, http).items() if run["state"] in ("queued", "running")]
    if going:
        raise RerunError(f"A run of {board} is already going (stage {going[0]}).", status_code=409)
    status, body = http("POST", f"/repos/{config.repo}/actions/workflows/{config.workflow}/dispatches", None,
                        {"ref": config.ref, "inputs": {"board": board, "stage": str(stage)}})
    _check(status, body, "starting the stage workflow")
    logger.info("Stage workflow dispatched", extra={"board": board, "stage": stage, "repo": config.repo})
