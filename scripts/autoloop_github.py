"""Bounded merged-PR audit reads; failures contain fixed codes, never GitHub bodies."""
import json
import re
import subprocess
from time import monotonic

import feedback_triage as triage

PAGE_SIZE = 25
SEARCH_LIMIT = 1000
READ_TIMEOUT = 30
AUDIT_TIMEOUT = 120
# Keep audit identity, branch/body markers and merge provenance; check rollups belong to controller reads.
FIELDS = ('number id state headRefName headRefOid baseRefName isDraft body mergedAt mergeCommit { oid } '
          'autoMergeRequest { enabledAt } mergeQueueEntry { state } headRepository { nameWithOwner }')


def read(argv, timeout):
    """One read, no retry. Only recognized status/transport codes leave this boundary."""
    try:
        done = subprocess.run(['gh', *argv], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=min(timeout, READ_TIMEOUT))
    except subprocess.TimeoutExpired:
        raise triage.Refused('gh-read: read-timeout') from None
    except OSError:
        raise triage.Refused('gh-read: command-unavailable') from None
    if done.returncode:
        status = re.search(r'\bHTTP\s+([45][0-9]{2})\b', done.stderr or '')
        code = 'http-' + status[1] if status else 'transport-timeout' if re.search(
            r'\b(?:i/o timeout|TLS handshake timeout|context deadline exceeded)\b', done.stderr or '') \
            else 'command-failed'
        if code == 'command-failed':
            try:
                reply = json.loads(done.stdout)
                if isinstance(reply, dict) and reply.get('errors'):
                    code = 'graphql-errors'
            except (ValueError, TypeError):
                pass
        raise triage.Refused(f'gh-read: {code}') from None
    return done.stdout


def search(gh, since, repo):
    """Return the entire inventory or refuse; search caps and moving/partial pages never advance audit state."""
    query = ('query($q: String!, $after: String) { search(query: $q, type: ISSUE, first: 25, after: $after) { '
             f'issueCount pageInfo {{ hasNextPage endCursor }} nodes {{ ... on PullRequest {{ {FIELDS} }} }} }} }}')
    q = f'repo:{repo} is:pr is:merged merged:>={since.date().isoformat()}'
    out, numbers, cursors, after, total = [], set(), set(), None, None
    deadline = monotonic() + AUDIT_TIMEOUT
    for page in range(1, SEARCH_LIMIT // PAGE_SIZE + 1):
        def fail(code):
            raise triage.Refused(f'merged-pr-audit page={page}: {code}') from None
        if monotonic() >= deadline:
            fail('audit-time-budget')
        argv = ['api', 'graphql', '-f', 'query=' + query, '-f', 'q=' + q] + (['-f', f'after={after}'] if after else [])
        try:
            text = gh(argv)
        except triage.Refused as error:
            safe = re.fullmatch(r'gh-read: (http-[45][0-9]{2}|transport-timeout|read-timeout|command-unavailable|command-failed|graphql-errors)',
                                str(error))
            fail(safe[1] if safe else 'read-failed')
        if monotonic() >= deadline:
            fail('audit-time-budget')
        try:
            reply = json.loads(text)
        except (ValueError, TypeError):
            fail('invalid-json')
        if not isinstance(reply, dict):
            fail('invalid-page')
        if reply.get('errors'):
            fail('graphql-errors')
        try:
            found = reply['data']['search']
            count, nodes, info = found['issueCount'], found['nodes'], found['pageInfo']
            more, cursor = info['hasNextPage'], info['endCursor']
        except (KeyError, TypeError):
            fail('invalid-page')
        if type(count) is not int or count < 0 or not isinstance(nodes, list) or type(more) is not bool:
            fail('invalid-page')
        if count > SEARCH_LIMIT:
            fail('search-limit')
        if total is not None and total != count:
            fail('inventory-changed')
        total = count
        if len(nodes) > PAGE_SIZE:
            fail('invalid-page')
        for node in nodes:
            if (not isinstance(node, dict) or type(node.get('number')) is not int or node['number'] <= 0
                    or node.get('state') != 'MERGED' or not isinstance(node.get('headRefName'), str)
                    or not isinstance(node.get('body'), str)
                    or not re.fullmatch(r'[0-9a-f]{40}', str(node.get('headRefOid')))):
                fail('invalid-node')
            if node['number'] in numbers:
                fail('duplicate-node')
            numbers.add(node['number'])
            out.append(node)
        if more:
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                fail('invalid-cursor')
            cursors.add(cursor)
            after = cursor
            if not nodes or len(out) >= total:
                fail('incomplete-inventory')
        elif len(out) != total:
            fail('incomplete-inventory')
        else:
            return out
    fail('page-limit')
