"""The fixed error-code allow-lists of the messaging service, read from error_codes.json.

The Go adapter and the browser fallback keep their own copies of some lists; tests/test_error_code_parity.py
pins them against this file. Imports nothing else of the package, so any module may load it.
"""
import json
from pathlib import Path

CODES = json.loads(Path(__file__).with_name('error_codes.json').read_text(encoding='utf-8'))
