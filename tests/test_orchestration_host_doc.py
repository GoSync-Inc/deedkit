from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# spec: ANYM-626
def test_parallel_development_names_bb_as_the_orchestration_host_and_maps_every_paradev_command():
    text = (ROOT / "docs/operations/parallel-development.md").read_text()
    section = text.split("## Orchestration host", 1)[1]
    for command in ("bb thread spawn", "--parent-thread", "bb thread list --parent-thread", "bb thread output",
                    "bb thread tell", "bb thread archive", "bb automation create"):
        assert command in section, command
    for paradev in ("superset workspaces create", "SendMessage", "superset workspaces archive", "superset automations"):
        assert paradev in section, paradev
