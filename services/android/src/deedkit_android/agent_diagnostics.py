"""Bounded native error receipts. No raw stderr, request bodies or phone dumps are persisted."""

import re


def message(text, private=()):
    text = str(text)
    if any(item in text.lower() for item in ('<hierarchy', '<node ', 'begin private', '"messages":', '"input":[')):
        return '<non-diagnostic content withheld>'
    for value in private:
        if len(str(value)) >= 3:
            text = text.replace(str(value), '<task-value>')
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    text = re.sub(r'''(?i)((?:authorization|bearer|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|password|secret)["']?\s*[:=]?\s*["']?)[^\s,;]+''',
                  r'\1<redacted>', text)
    text = re.sub(r'(?:sk-|eyJ)[A-Za-z0-9_.-]{10,}|[A-Za-z0-9_-]{48,}', '<redacted>', text)
    text = re.sub(r'https?://[^\s)]+', '<url-redacted>', text)
    text = re.sub(r'[^\s@]+@[^\s@]+', '<email-redacted>', text)
    return ' '.join(text.split())[:700]


def receipt(stream_errors, stderr, private=()):
    found = []
    for source, lines in (('stream', stream_errors), ('stderr', stderr.decode(errors='replace').splitlines())):
        for line in lines:
            if source == 'stderr' and not re.search(r'(?i)\b(error|warn(?:ing)?|failed|denied|timeout|disconnected)\b', line):
                continue
            item = {'source': source, 'message': message(line, private)}
            if item not in found:
                found.append(item)
    return found[-8:]
