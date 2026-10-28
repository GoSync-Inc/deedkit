"""The written boundary of the feedback autoloop: AGENTS.md section 2's override within the word budget, and the loop
document's sections (ANYM-568). Reads committed files only."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def section(text: str, number: int) -> str:
    match = re.search(rf"^## {number}\. .*?(?=^## |\Z)", text, re.M | re.S)
    assert match, f"AGENTS.md has no section {number}"
    return match.group(0)


# spec: ANYM-568
def test_agents_md_stays_within_its_1000_word_budget_as_wc_counts_it():
    # `wc -w` counts whitespace-separated words; AGENTS.md section 6 sets the cap.
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Word cap: 1,000 words (`wc -w`)" in section(text, 6)
    assert len(text.split()) <= 1000


# spec: ANYM-568
def test_agents_md_section_2_carries_the_card_intake_override_bounded_by_the_loop_document():
    owner = section((ROOT / "AGENTS.md").read_text(encoding="utf-8"), 2)
    assert "Override: harness tiden.md Card intake step 1 (owner 10-26)." in owner
    assert "docs/operations/feedback-autoloop.md (≤8 SP, no epic)" in owner
    assert "Feedback is never the owner's words" in owner


# spec: ANYM-568
def test_the_loop_document_has_envelope_owner_items_stop_caps_live_switch_and_undo_sections():
    doc = (ROOT / "docs/operations/feedback-autoloop.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^## (.+)$", doc, re.M))
    assert {"Boundary", "Envelope", "Owner items", "Stop, withdraw, hold", "Caps", "The live switch",
            "Undo per stage"} <= headings
    # The protected paths are the one list in the review protocol, not a second copy here.
    assert "the autoloop protected paths (both in docs/operations/review-protocol.md" in doc
