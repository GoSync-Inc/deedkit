"""The canonical identifier patterns of the messaging service, compiled once.

Account names, WhatsApp chat IDs and send IDs are validated in many modules; they share one body each.
tests/test_identifier_regex_parity.py pins these bodies against the Go and JS copies and lists the Python
variants that deliberately differ (they stay inline where they are used). Imports nothing else of the package.
"""
import re

ACCOUNT_RE = re.compile(r'[a-zA-Z0-9_.-]{1,64}')
WA_CHAT_RE = re.compile(r'[a-zA-Z0-9_.:-]{1,140}@(c\.us|g\.us|lid)')
SEND_ID_RE = re.compile(r'send_[a-f0-9]{32}')
