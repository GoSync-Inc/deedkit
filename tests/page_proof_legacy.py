"""ANYM-950: today's page-first proof, frozen (copied from chat_queries._page_first at main d23f516b).

The oracle and the baseline of tests/test_search_performance.py and tests/test_list_chats_page_first.py. The proof
SQL is never imported from live code: a change to it must not move the oracle. Only its inputs (the principal's
message scope, the cursor's date) come from the live helpers the page plan itself uses.
"""
from messaging_service.policy import scope_filter
from messaging_service.search_query import position_date


def legacy_page_proof(p, clauses, args, *, accounts, last_filter, pos, lower, upper, found, excluded):
    """Today's proof and its arguments: no kept message of a chat outside the candidates at or after the page's
    oldest head, the floor read from chat_page at run time."""
    at = position_date(pos['at']) if pos is not None else None
    msg_scope, msg_args = scope_filter(p, 'm')
    bounds, bound_args = [], []
    if accounts is not None:bounds.append('m.account=ANY(%s)');bound_args.append(accounts)
    if at is not None:bounds.append('m.sent_at<=%s');bound_args.append(at)
    if upper:bounds.append('m.sent_at<%s');bound_args.append(upper)
    if lower:bounds.append('m.sent_at>=%s');bound_args.append(lower)
    scoped = ' AND '.join([msg_scope, 'NOT m.deleted', *bounds])
    kept = (' JOIN chats c ON c.id=m.chat AND c.account=m.account LEFT JOIN message_metadata md ON md.message=m.id'
        ' AND md.revision=m.revision WHERE '+' AND '.join([last_filter, *clauses]))
    proof_sql = ('NOT EXISTS(SELECT 1 FROM messages m'+kept+' AND '+scoped+
        " AND NOT m.chat=ANY(%s) AND m.sent_at>=(SELECT min(coalesce(page_head,'-infinity')) FROM chat_page))")
    return proof_sql, [*args, *msg_args, *bound_args, [*found, *excluded]]


def with_legacy_proof(page_first):
    """`page_first` (chat_queries._page_first) whose candidates are proven by today's frozen proof."""
    def legacy(c, p, clauses, args, *, accounts, limit, last_sql, last_filter, pos, lower, upper, exclude_chats):
        found = page_first(c, p, clauses, args, accounts=accounts, limit=limit, last_sql=last_sql, last_filter=last_filter,
            pos=pos, lower=lower, upper=upper, exclude_chats=exclude_chats)
        if found is None:return None
        proof, proof_args = legacy_page_proof(p, clauses, args, accounts=accounts, last_filter=last_filter, pos=pos,
            lower=lower, upper=upper, found=found['found'], excluded=found['excluded'])
        return {**found, 'proof': proof, 'proof_args': proof_args}
    return legacy
