import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


# spec: ANYM-819
def test_ci_runs_the_agent_runner_suite_with_docker_in_the_services_job():
    workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text()
    services = workflow[workflow.index("\n  services:"):workflow.index("\n  go:")]
    step = re.search(r"      - name: Agent runner tests\n(?P<body>(?:        .*\n|\n)+)", services)
    assert step, "the agent runner step is missing from the services job"
    body = step.group("body")
    assert re.search(r"^        id: agent_runner$", body, re.M) and "working-directory: services/agent-runner" in body
    assert "AGENT_RUNNER_DOCKER_TESTS: required" in body
    assert '--junitxml="$RUNNER_TEMP/ci-evidence/agent-runner.xml"' in body
    assert "uv sync --locked --group dev" in body and "uv run ruff check src tests" in body
    assert '"agent_runner":"${{ steps.agent_runner.outcome }}"' in services
    # No existing step id changes; the seven anymsg-ci checks live in the verify job.
    for step_id in ("source", "gateway", "platform_contracts", "calls", "calls_frontend", "inference"):
        assert f"        id: {step_id}\n" in services, step_id
    for check in ("build", "release", "python", "collector", "npm", "smoke", "distributions"):
        assert re.search(rf"        id: {check}\n", workflow), check
    tiden = (ROOT / "scripts" / "tiden_ci.py").read_text()
    assert '"agent_runner": ("agent-runner.xml", "python", "services/agent-runner")' in tiden
