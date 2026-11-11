"""The native WhatsApp adapter's `.repair-status` file: the ANYM-809 repair count and, once it has started, the
ANYM-963 republish pass. Fixed integers and fixed codes only; anything else is no report."""


NATIVE_REPAIR_STATES=('would_add_content','label_only','same','no_raw','placeholder','sender_key_only','parse_error')
# ANYM-963: the republish pass's block in the same file, absent until that pass starts (repair.go status).
NATIVE_REPUBLISH_FIELDS=('version','done','rows_scanned','published','unsupported','parse_error','last_error')


def valid_native_republish(value):
    def count(x):return type(x) is int and 0<=x<2**53
    return (isinstance(value,dict) and set(value)==set(NATIVE_REPUBLISH_FIELDS) and count(value['version'])
        and 1<=value['version']<=10**6 and type(value['done']) is bool
        and all(count(value[k]) for k in ('rows_scanned','published','unsupported','parse_error'))
        and value['last_error'] in ('','republish_read','republish_storage','republish_spool'))


def validate_native_repair_status(value):
    from .envelope import utc
    def count(x):return type(x) is int and 0<=x<2**53
    if isinstance(value,dict) and 'republish' in value:
        if not valid_native_republish(value['republish']):raise ValueError('invalid_native_repair_status')
        republish={k:value['republish'][k] for k in NATIVE_REPUBLISH_FIELDS}
        value={k:v for k,v in value.items() if k!='republish'}
    else:republish=None
    if (not isinstance(value,dict) or set(value)!={'version','done','rows_scanned','counts','last_error','observed_at'}
        or not count(value['version']) or not 1<=value['version']<=10**6 or type(value['done']) is not bool
        or not count(value['rows_scanned']) or value['last_error'] not in ('','repair_read','repair_storage')
        or not isinstance(value['counts'],dict) or set(value['counts'])!=set(NATIVE_REPAIR_STATES)
        or not all(count(x) for x in value['counts'].values())):
        raise ValueError('invalid_native_repair_status')
    try:observed=utc(value['observed_at'])
    except (ValueError,TypeError,OverflowError):raise ValueError('invalid_native_repair_status') from None
    return {**value,'counts':{k:value['counts'][k] for k in NATIVE_REPAIR_STATES},'observed_at':observed,
        **({'republish':republish} if republish is not None else {})}
