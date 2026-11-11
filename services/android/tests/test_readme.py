"""The service README states what was measured on Android 16 and the checks to run after a major OTA."""

from pathlib import Path
import re

README = Path(__file__).parents[1] / 'README.md'


def section(title):
    match = re.search(rf'^## {re.escape(title)}\n(.*?)(?=^## |\Z)', README.read_text(), re.MULTILINE | re.DOTALL)
    assert match, f'README.md has no "## {title}" section'
    return match.group(1)


# spec: ANYM-979
def test_readme_states_android_16_as_measured_and_the_checks_after_a_major_ota():
    body = section('Android versions')
    assert 'Android 16' in body and '#864' in body and '`mCurrentFocus`' in body
    steps = ('`adb devices`', '`android_capabilities`', '`android_observe`', 'saved read-only scenario')
    positions = [body.find(step) for step in steps]
    assert min(positions) >= 0, 'every post-OTA check is named'
    assert positions == sorted(positions), 'the checks are listed in order'
    assert 'unauthorized' in body and 'offline' in body
