import re
from pathlib import Path

# spec: ANYM-682
def test_ci_runs_the_inference_suite_in_the_services_job():
    workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "checks.yml").read_text()
    services = workflow[workflow.index("\n  services:"):workflow.index("\n  go:")]
    step = re.search(r"      - name: Inference service tests\n(?P<body>(?:        .*\n|\n)+)", services)
    assert step, "the inference step is missing from the services job"
    body = step.group("body")
    # No step id: tests/test_tiden_ci.py pins the id set of the services job to the Tiden modules.
    assert not re.search(r"^        id:", body, re.M) and "working-directory: services/inference" in body
    assert "uv sync --locked --group dev" in body and "uv run pytest -q" in body
    assert "uv run ruff check src" in body
    # The existing step ids of the job are untouched; the seven anymsg-ci checks live in the verify job.
    for step_id in ("source", "gateway", "platform_contracts", "calls", "calls_frontend"):
        assert f"        id: {step_id}\n" in services, step_id
    for check in ("build", "release", "python", "collector", "npm", "smoke", "distributions"):
        assert re.search(rf"        id: {check}\n", workflow), check
