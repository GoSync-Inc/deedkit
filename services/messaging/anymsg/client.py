"""Bounded, credential-safe REST calls without automatic retries or redirects."""
from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx


SAFE_ERROR_CODES = frozenset({
    'ambiguous_reference','attachment_unavailable','body_timeout','body_too_large',
    'chat_required','consumer_filter_mismatch','consumer_limit','cursor_changed',
    'disconnected','forbidden','invalid_body','invalid_consumer','invalid_cursor',
    'invalid_date_range','invalid_filter','invalid_fuzzy_query','invalid_host',
    'invalid_limit','invalid_origin','invalid_parameters','invalid_query',
    'invalid_receipt','invalid_utc_date','invalid_version','not_found',
    'query_timeout','rate_limited','service_busy','service_unavailable',
    'stale_ack','temporarily_unavailable','unauthorized','version_conflict',
    'invalid_message_text','invalid_request_id','idempotency_conflict','sending_disabled','sender_not_ready','session_unavailable','invalid_anchor',
    'invalid_message_format','unsafe_message_markup','invalid_message_entities',
    'formatting_too_complex','unsupported_message_formatting',
    'lossy_formatting_requires_confirmation','message_too_long','preview_required','preview_mismatch',
    'unsupported_operation','invalid_operation_parameters','operation_disabled','operation_not_ready',
    'operation_target_changed','operation_access_changed','operation_result_unknown','already_member',
    'operation_expired','operation_rejected','invalid_operation_result',
    'reply_target_changed','invalid_subscription','subscription_limit','subscription_conflict','subscription_closed',
    'subscription_access_changed','invalid_subscription_state',
    'invalid_upload','invalid_upload_chunk','upload_limit','upload_capacity',
    'upload_expired','upload_offset_conflict','upload_hash_mismatch',
    'media_request_limit',
    'subscription_wait_timeout','subscription_receipt_limit','invalid_feedback','feedback_limit',
    'invalid_receiver','delivery_not_configured','consumer_already_initialized',
    'send_wait_busy','send_status_timeout','send_access_changed','send_expired','send_result_unknown',
    # ANYM-519: self-service webhook endpoints.
    'delivery_disabled','delivery_policy_unset','unsafe_endpoint','invalid_signing_secret','invalid_header','reserved_header',
    'rotation_in_progress','endpoint_limit','endpoint_conflict','operator_disabled','invalid_endpoint_state','endpoint_paused','endpoint_auto_paused',
})

_UPLOAD_CHUNK_BYTES = 262144
_UPLOAD_BODY_BYTES = 360448
_UPLOAD_MAX_BYTES = 32 * 1024**2
_UPLOAD_FIELDS = {
    'prepare_file_upload': {'account','chat_id','filename','mime','bytes','sha256','request_id'},
    'upload_file_chunk': {'upload_id','offset','data_base64'},
    'get_file_upload': {'upload_id'},
}


def _upload_parameters(operation, parameters):
    if parameters.keys() != _UPLOAD_FIELDS[operation]:
        raise ValueError('invalid_upload_parameters')
    for name, value in parameters.items():
        if name in ('account','chat_id'):
            valid = isinstance(value,str) and bool(re.fullmatch(r'[^\x00-\x20\x7f]{1,256}',value))
        elif name == 'filename':
            valid = isinstance(value,str) and 1 <= len(value) <= 255 and value not in ('.','..') and not any(
                ord(c)<32 or ord(c)==127 or 0xD800<=ord(c)<=0xDFFF or c in '/\\' for c in value)
        elif name == 'mime':
            valid = isinstance(value,str) and len(value)<=128 and bool(re.fullmatch(
                r'[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+',value))
        elif name == 'bytes':valid = type(value) is int and 1<=value<=_UPLOAD_MAX_BYTES
        elif name == 'sha256':valid = isinstance(value,str) and bool(re.fullmatch(r'[a-f0-9]{64}',value))
        elif name == 'request_id':valid = isinstance(value,str) and bool(re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',value))
        elif name == 'upload_id':valid = isinstance(value,str) and bool(re.fullmatch(r'up_[a-f0-9]{32}',value))
        elif name == 'offset':valid = type(value) is int and 0<=value<=_UPLOAD_MAX_BYTES
        else:
            valid = isinstance(value,str) and 1<=len(value)<=349528
            if valid:
                try:
                    decoded=base64.b64decode(value,validate=True)
                    valid=1<=len(decoded)<=_UPLOAD_CHUNK_BYTES and base64.b64encode(decoded).decode()==value
                except (ValueError,binascii.Error):valid=False
        if not valid:raise ValueError('invalid_upload_parameters')


def _file_snapshot(fd):
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 1<=info.st_size<=_UPLOAD_MAX_BYTES:
        raise ValueError('invalid_upload_file')
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _file_hash(fd):
    os.lseek(fd,0,os.SEEK_SET)
    digest=hashlib.sha256()
    total=0
    while chunk:=os.read(fd,_UPLOAD_CHUNK_BYTES):
        total+=len(chunk)
        if total>_UPLOAD_MAX_BYTES:raise AnyMsgError('upload_file_changed')
        digest.update(chunk)
    return digest.hexdigest()


def _upload_receipt(value, metadata, *, upload_id=None, offset=None):
    if (not isinstance(value,dict) or any(value.get(k)!=v for k,v in metadata.items() if k!='request_id')
            or not isinstance(value.get('upload_id'),str)
            or not re.fullmatch(r'up_[a-f0-9]{32}',value['upload_id'])
            or upload_id is not None and value['upload_id']!=upload_id
            or type(value.get('offset')) is not int or not 0<=value['offset']<=metadata['bytes']
            or offset is not None and value['offset']!=offset
            or type(value.get('chunk_bytes')) is not int or value['chunk_bytes']!=_UPLOAD_CHUNK_BYTES
            or value.get('changes_provider_state') is not False
            or value.get('owner_approval_verified_by_server') is not False
            or value.get('status') not in ('uploading','ready','expired')
            or (value['offset']==metadata['bytes']) != (value.get('status')=='ready') and value.get('status')!='expired'):
        raise AnyMsgError('invalid_upload_response')
    if value['status']=='expired':raise AnyMsgError('upload_expired')
    return value


_MESSENGER_FIELDS = {
    'list_messenger_operations': ({'account'}, {'account'}),
    'preview_messenger_operation': ({'account', 'operation'},
        {'account', 'operation', 'chat_id', 'message_id', 'expected_revision', 'parameters', 'request_id'}),
    'execute_messenger_operation': ({'account', 'operation', 'request_id'},
        {'account', 'operation', 'chat_id', 'message_id', 'expected_revision', 'parameters',
         'request_id', 'preview_digest', 'wait_s'}),
    'get_messenger_operation': ({'operation_id'}, {'operation_id'}),
}
_OMITTED = object()
_SUBSCRIPTION_FIELDS = {
    'create_subscription': ({'chat_ids'}, {'chat_ids','sender_ids','kinds','direction','origins','label','expires_at',
        'wake_rules','reply_to_send','delivery'}),
    'list_subscriptions': (set(), {'limit'}),
    'get_subscription': ({'subscription_id'}, {'subscription_id'}),
    'update_subscription': ({'subscription_id','version','action'},
        {'subscription_id','version','action','resume_policy'}),
    'get_subscription_events': ({'subscription_id'}, {'subscription_id','after','limit'}),
    'fetch_subscription_events': ({'subscription_id'}, {'subscription_id','limit'}),
    'ack_subscription_events': ({'subscription_id','receipt','version'}, {'subscription_id','receipt','version'}),
    'wait_for_subscription_events': ({'subscription_id'}, {'subscription_id','timeout','limit'}),
    'configure_subscription_delivery': ({'subscription_id','version'}, {'subscription_id','version','receiver_id','delivery_policy','endpoint_id'}),
    'test_subscription': ({'subscription_id','request_id'}, {'subscription_id','request_id'}),
    # ANYM-519: the caller's own webhook endpoints. Secrets and header values are write-only.
    'register_delivery_endpoint': ({'request_id','url','signing_secret'}, {'request_id','url','signing_secret','label','headers'}),
    'list_delivery_endpoints': (set(), set()),
    'update_delivery_endpoint': ({'endpoint_id','version','action'}, {'endpoint_id','version','action','new_secret','headers'}),
    'delete_delivery_endpoint': ({'endpoint_id','version'}, {'endpoint_id','version'}),
    'test_delivery_endpoint': ({'endpoint_id','request_id'}, {'endpoint_id','request_id'}),
}
_FEEDBACK_STATUSES = ('received','accepted','declined','duplicate','fixed','verify_requested','verified','reopened')
_FEEDBACK_FIELDS = {
    'submit_feedback': ({'request_id','category','description'}, {'request_id','category','description','operation','refs'}),
    'list_feedback': (set(), {'limit','status'}),
}
_SUBSCRIPTION_KINDS = frozenset({'message.created','message.edited','message.deleted','message.status',
    'attachment.ready','file.text.ready','file.transcript.ready','file.description.ready','file.processing.failed',
    'send.failed','source.health'})
_SUBSCRIPTION_ORIGINS = frozenset({'live','backfill','reconcile','fixture'})
_WAKE_RULES = frozenset({'incoming_message','owner_mention','reply_to_owner','reply_to_confirmed_send'})


def _subscription_reference(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[^\x00-\x20\x7f]{1,256}',value))


def _wait_seconds(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=25:
        raise ValueError('invalid_parameters')
    return value


def _uuid(value):
    return isinstance(value,str) and bool(re.fullmatch(
        r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',value))


def _signing_secret(value):
    return isinstance(value,str) and bool(re.fullmatch(r'whsec_[A-Za-z0-9+/=]{32,96}',value))


def _request_id(value):
    return isinstance(value,str) and bool(re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}',value))


def _subscription_parameters(operation, parameters):
    """Strict structural envelope checks; authorization and lifecycle stay on the server."""
    required,allowed=_SUBSCRIPTION_FIELDS[operation]
    if not isinstance(parameters,dict) or not required<=parameters.keys() or parameters.keys()-allowed:
        raise ValueError('invalid_parameters')
    for name,value in parameters.items():
        valid=True
        if name in ('chat_ids','sender_ids','kinds','origins','wake_rules'):
            if value is None and name!='chat_ids':continue
            maximum=11 if name=='kinds' else 4 if name in ('origins','wake_rules') else 64
            valid=isinstance(value,list) and 1<=len(value)<=maximum
            if valid:
                valid=all(_subscription_reference(item) if name in ('chat_ids','sender_ids')
                    else isinstance(item,str) and item in (_SUBSCRIPTION_KINDS if name=='kinds'
                        else _SUBSCRIPTION_ORIGINS if name=='origins' else _WAKE_RULES) for item in value)
        elif name in ('limit','version','after'):
            minimum,maximum={'limit':(1,100),'version':(1,9007199254740991),'after':(0,9007199254740991)}[name]
            valid=type(value) is int and minimum<=value<=maximum
        elif name=='subscription_id':valid=_subscription_reference(value)
        elif name=='receipt':valid=isinstance(value,str) and 32<=len(value)<=128
        elif name=='timeout':_wait_seconds(value)
        elif name=='request_id':valid=_request_id(value)
        elif name=='receiver_id':valid=value is None or _uuid(value)
        elif name=='endpoint_id':valid=(value is None and operation=='configure_subscription_delivery') or _uuid(value)
        elif name=='url':valid=isinstance(value,str) and 1<=len(value)<=2048
        elif name in ('signing_secret','new_secret'):valid=(value is None and name=='new_secret') or _signing_secret(value)
        # The server checks header names, reserved names and values (invalid_header, reserved_header).
        elif name=='headers':
            valid=value is None or isinstance(value,dict) and len(value)<=4 and all(
                isinstance(k,str) and isinstance(v,str) for k,v in value.items())
        elif name=='delivery':
            valid=value is None or (isinstance(value,dict) and 'endpoint_id' in value
                and not value.keys()-{'endpoint_id','delivery_policy'} and _uuid(value['endpoint_id'])
                and isinstance(value.get('delivery_policy',{}),dict) and len(json.dumps(value))<=4096)
        elif name=='reply_to_send':valid=value is None or isinstance(value,str) and 1<=len(value)<=128
        # The server validates fields and bounds (invalid_delivery_policy); the envelope is a small object.
        elif name=='delivery_policy':valid=value is None or isinstance(value,dict) and len(json.dumps(value))<=4096
        elif name=='label':valid=isinstance(value,str) and len(value)<=128 and not re.search(r'[\x00-\x1f\x7f]',value)
        elif name in ('action','direction','resume_policy'):
            valid=isinstance(value,str) and value in {
                'action':('pause','resume','close') if operation=='update_subscription'
                    else ('rotate_secret','set_headers','pause','resume'), 'direction':('','incoming','outgoing'),
                'resume_policy':('catch_up','skip')}[name]
        elif name=='expires_at':
            if value is None:continue
            valid=isinstance(value,str) and 20<=len(value)<=64 and bool(re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})',value))
            if valid:
                try:
                    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                    valid=parsed.tzinfo is not None and parsed.utcoffset() is not None
                except ValueError:valid=False
        if not valid:raise ValueError('invalid_parameters')
    if ('reply_to_confirmed_send' in (parameters.get('wake_rules') or [])
            and parameters.get('reply_to_send') is None):
        raise ValueError('invalid_parameters')


def _feedback_parameters(operation, parameters):
    required,allowed=_FEEDBACK_FIELDS[operation]
    if not required<=parameters.keys() or parameters.keys()-allowed:
        raise ValueError('invalid_parameters')
    for name,value in parameters.items():
        if name=='request_id':valid=_request_id(value)
        elif name=='limit':valid=type(value) is int and 1<=value<=100
        elif name=='status':valid=isinstance(value,str) and value in _FEEDBACK_STATUSES
        elif name=='category':valid=isinstance(value,str) and value in ('bug','feature','quality','other')
        elif name=='description':
            valid=isinstance(value,str) and 1<=len(value)<=4000 and bool(value.strip()) and not any(
                ord(ch)<32 and ch not in '\n\t' or ord(ch)==127 for ch in value)
        elif name=='operation':
            # The server's current catalog is authoritative; future operations
            # can be referenced without an SDK upgrade.
            valid=value is None or isinstance(value,str) and bool(re.fullmatch(r'[a-z][a-z0-9_]{0,128}',value))
        else:
            valid=value is None or isinstance(value,dict) and not value.keys()-{'message_ids','chat_ids','subscription_ids'}
            if valid and value is not None:
                valid=all(isinstance(items,list) and len(items)<=32 and all(
                    isinstance(item,str) and 1<=len(item)<=256 and not any(
                        ord(ch)<32 or ord(ch)==127 for ch in item) for item in items) for items in value.values())
                if valid:valid=len(json.dumps(value).encode())<=8192
        if not valid:raise ValueError('invalid_parameters')


def _messenger_parameters(operation, parameters):
    """Reject malformed/unknown envelopes before transport; backend checks the catalog."""
    required, allowed = _MESSENGER_FIELDS[operation]
    if not isinstance(parameters, dict) or not required <= parameters.keys() or parameters.keys() - allowed:
        raise ValueError('invalid_parameters')
    for name, value in parameters.items():
        if name == 'expected_revision':
            if type(value) is not int or value < 0: raise ValueError('invalid_parameters')
        elif name == 'parameters':
            if not isinstance(value, dict) or any(type(key) is not str for key in value):
                raise ValueError('invalid_parameters')
        elif name == 'wait_s':
            _wait_seconds(value)
        elif type(value) is not str:
            raise ValueError('invalid_parameters')
    return parameters


class AnyMsgError(RuntimeError):
    """Safe error metadata. Remote bodies, request parameters and tokens are omitted."""
    def __init__(self, code, *, status=None, request_id=None):
        self.code = code
        self.status = status
        self.request_id = request_id
        super().__init__(code)


def _base_url(value):
    if not isinstance(value, str) or not value or any(ord(c)<33 for c in value):
        raise ValueError("invalid_base_url")
    try:
        parsed = urlsplit(value)
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError("invalid_base_url") from None
    local = parsed.hostname == "localhost"
    if parsed.hostname:
        try: local = local or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError: pass
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.scheme != "https" and not (parsed.scheme == "http" and local)):
        raise ValueError("https_or_loopback_required")
    return value.rstrip("/")


def _bad_constant(_):
    raise ValueError("invalid_json_constant")


class Client:
    """Call an AnyMsg REST root, e.g. https://archive.example/anymsg.

    The token is sent only in Authorization. Configure the REST root, not /mcp
    or /v1; each call appends /v1/<operation>. Reads never mark source messages
    read. send_message requires exact owner confirmation by the acting agent.
    Ambiguous writes are never automatically retried by this SDK.
    """
    def __init__(self, base_url, token, *, timeout=30, max_response_bytes=8*1024**2,
                 transport=None):
        self.base_url = _base_url(base_url)
        if (not isinstance(token,str) or not token or len(token)>8192
                or any(ord(c)<33 or ord(c)>126 for c in token)):
            raise ValueError("invalid_bearer_token")
        if (type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300
                or type(max_response_bytes) is not int or not 1024<=max_response_bytes<=32*1024**2):
            raise ValueError("invalid_client_limits")
        self.max_response_bytes = max_response_bytes
        self._http = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False,
            headers={"Authorization":"Bearer "+token,"Accept":"application/json","Accept-Encoding":"identity"},transport=transport)

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()

    def close(self):
        self._http.close()

    def call(self, operation, parameters=None):
        if not isinstance(operation,str) or not re.fullmatch("[a-z][a-z0-9_]{0,63}",operation):
            raise ValueError("invalid_operation")
        parameters = {} if parameters is None else parameters
        if not isinstance(parameters,dict) or "token" in parameters or any(not isinstance(k,str) for k in parameters):
            raise ValueError("invalid_parameters")
        if operation in _MESSENGER_FIELDS:
            _messenger_parameters(operation, parameters)
        if operation in _SUBSCRIPTION_FIELDS:
            _subscription_parameters(operation,parameters)
        if operation in _FEEDBACK_FIELDS:
            _feedback_parameters(operation,parameters)
        if operation in ('send_message','execute_messenger_operation') and 'wait_s' in parameters:
            _wait_seconds(parameters['wait_s'])
            if parameters['wait_s']==0:
                parameters={name:value for name,value in parameters.items() if name!='wait_s'}
        if operation in _UPLOAD_FIELDS:
            _upload_parameters(operation,parameters)
        if operation == 'request_file_download' and (parameters.keys() != {'attachment_id'}
                or type(parameters.get('attachment_id')) is not str
                or not 1 <= len(parameters['attachment_id']) <= 256):
            raise ValueError('invalid_parameters')
        try:
            body=json.dumps(parameters,ensure_ascii=False,allow_nan=False,separators=(",",":")).encode("utf-8")
        except (ValueError,TypeError,RecursionError):
            raise ValueError("invalid_parameters") from None
        if len(body)>(_UPLOAD_BODY_BYTES if operation=='upload_file_chunk' else 16384):
            raise ValueError("request_too_large")
        try:
            with self._http.stream("POST",self.base_url+"/v1/"+operation,content=body,
                    headers={"Content-Type":"application/json"}) as response:
                if 300<=response.status_code<400:
                    raise AnyMsgError("redirect_rejected",status=response.status_code)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise AnyMsgError("unsupported_content_encoding",status=response.status_code)
                data=bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    data.extend(chunk)
                    if len(data)>self.max_response_bytes:
                        raise AnyMsgError("response_too_large",status=response.status_code)
                try:
                    value=json.loads(data,parse_constant=_bad_constant)
                    if not isinstance(value,dict):raise ValueError
                except (ValueError,TypeError,RecursionError):
                    raise AnyMsgError("invalid_response",status=response.status_code) from None
                if not 200<=response.status_code<300:
                    code=value.get("error",value.get("code"))
                    if not isinstance(code,str) or code not in SAFE_ERROR_CODES:code="http_error"
                    request_id=value.get("request_id")
                    if not isinstance(request_id,str) or not re.fullmatch("[0-9a-f]{32}",request_id):request_id=None
                    raise AnyMsgError(code,status=response.status_code,request_id=request_id)
                return value
        except httpx.HTTPError:
            raise AnyMsgError("request_failed") from None

    def capabilities(self, *, detail=None, sections=None):
        """The short summary by default; detail="full" is the whole payload, sections=[names] only those blocks."""
        parameters = {} if detail is None else {"detail": detail}
        if sections is not None:
            parameters["sections"] = list(sections)
        return self.call("capabilities", parameters)

    def request_file_download(self, *, attachment_id):
        """Prioritize one existing original; poll get_attachment without requesting again.

        Requires download permission. Retrieval remains asynchronous, bounded by
        collector policy, source availability and disk floor. Never sends or marks read.
        """
        return self.call('request_file_download', {'attachment_id': attachment_id})

    def prepare_file_upload(self, *, account, chat_id, filename, mime, bytes, sha256, request_id):
        """Stage scoped metadata only. Preserve request_id after an uncertain response."""
        return self.call('prepare_file_upload',dict(account=account,chat_id=chat_id,filename=filename,
            mime=mime,bytes=bytes,sha256=sha256,request_id=request_id))

    def upload_file_chunk(self, *, upload_id, offset, data_base64):
        """Append at most 256 KiB; no automatic retry and no messenger action."""
        return self.call('upload_file_chunk',dict(upload_id=upload_id,offset=offset,data_base64=data_base64))

    def get_file_upload(self, *, upload_id):
        """Inspect an existing upload without sending it or changing its bytes."""
        return self.call('get_file_upload',{'upload_id':upload_id})

    def upload_file(self, file, *, account, chat_id, request_id, filename=None,
                    mime='application/octet-stream'):
        """Hash and stage a local regular file, at most 32 MiB, without sending it.

        The same descriptor stays open for hashing and transfer. Changes reject
        the operation; only a basename enters the protocol. An explicit repeated
        invocation with the same request_id may resume the same upload. Unknown
        requests are never retried automatically. Chunks are paced below 8/sec.
        """
        try:path=os.fspath(file)
        except TypeError:raise ValueError('invalid_upload_file') from None
        if not isinstance(path,str) or not path or '://' in path or '\x00' in path:
            raise ValueError('invalid_upload_file')
        name=os.path.basename(path) if filename is None else filename
        content_type=mime.lower() if isinstance(mime,str) else mime
        _upload_parameters('prepare_file_upload',dict(account=account,chat_id=chat_id,filename=name,
            mime=content_type,bytes=1,sha256='0'*64,request_id=request_id))
        fd=None
        try:
            if not all(hasattr(os,name) for name in ('O_NOFOLLOW','O_NONBLOCK')):
                raise AnyMsgError('upload_file_unavailable')
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            snapshot=_file_snapshot(fd)
            digest=_file_hash(fd)
            if _file_snapshot(fd)!=snapshot:raise AnyMsgError('upload_file_changed')
            metadata=dict(account=account,chat_id=chat_id,filename=name,
                mime=content_type,bytes=snapshot[2],sha256=digest,request_id=request_id)
            _upload_parameters('prepare_file_upload',metadata)
            receipt=_upload_receipt(self.prepare_file_upload(**metadata),metadata)
            upload_id=receipt['upload_id']
            offset=receipt['offset']
            os.lseek(fd,offset,os.SEEK_SET)
            last_chunk=None
            while offset<snapshot[2]:
                if _file_snapshot(fd)!=snapshot:raise AnyMsgError('upload_file_changed')
                size=min(receipt['chunk_bytes'],snapshot[2]-offset)
                chunk=os.read(fd,size)
                if len(chunk)!=size:raise AnyMsgError('upload_file_changed')
                if last_chunk is not None:time.sleep(max(0,0.15-(time.monotonic()-last_chunk)))
                last_chunk=time.monotonic()
                receipt=_upload_receipt(self.upload_file_chunk(upload_id=upload_id,offset=offset,
                    data_base64=base64.b64encode(chunk).decode('ascii')),metadata,
                    upload_id=upload_id,offset=offset+len(chunk))
                offset=receipt['offset']
            if _file_snapshot(fd)!=snapshot or _file_hash(fd)!=digest or _file_snapshot(fd)!=snapshot:
                raise AnyMsgError('upload_file_changed')
            return receipt
        except OSError:raise AnyMsgError('upload_file_unavailable') from None
        finally:
            if fd is not None:os.close(fd)

    def search(self, query, **filters):
        return self.call("search",{"query":query,**filters})

    def dismiss_reply(self, *, chat_id, message_id, expected_revision, reason=None, note=None):
        """Dismiss an exact hint for this key, without changing provider read state."""
        return self.call('dismiss_reply',dict(chat_id=chat_id,message_id=message_id,expected_revision=expected_revision,
            **{k:v for k,v in dict(reason=reason,note=note).items() if v is not None}))

    def snooze_reply(self, *, chat_id, message_id, expected_revision, until, note=None):
        """Hide an exact hint for this key until `until` (RFC 3339) or a new meaningful incoming."""
        return self.call('snooze_reply',dict(chat_id=chat_id,message_id=message_id,expected_revision=expected_revision,
            until=until,**({} if note is None else {'note':note})))

    def restore_reply(self, *, chat_id, message_id, expected_revision):
        """Undo this key's dismissal or snooze of the exact hint. After an uncertain result, list instead of retrying."""
        return self.call('restore_reply',dict(chat_id=chat_id,message_id=message_id,expected_revision=expected_revision))

    def mute_reply_source(self, *, chat_id=None, account=None, sender_id=None, reason=None, note=None):
        """Keep a chat, or a sender within one account, out of this agent's reply queue until unmuted."""
        return self.call('mute_reply_source',{k:v for k,v in dict(chat_id=chat_id,account=account,sender_id=sender_id,
            reason=reason,note=note).items() if v is not None})

    def unmute_reply_source(self, *, chat_id=None, account=None, sender_id=None):
        """Remove this agent's mute of the chat, or of the sender within the account."""
        return self.call('unmute_reply_source',{k:v for k,v in dict(chat_id=chat_id,account=account,
            sender_id=sender_id).items() if v is not None})

    def list_reply_dismissals(self, *, kind='dismissed', after='', limit=20):
        """List this key's dismissals or snoozes, or this agent's mutes, in chats it can read now."""
        return self.call('list_reply_dismissals',dict(kind=kind,after=after,limit=limit))

    def preview_message(self,*,account,chat_id,text,reply_to='',format='plain',
                        link_preview=False,allow_lossy=False):
        """Render a scoped draft without queuing or contacting a messenger.

        The digest binds content and policy; it is not owner approval. Review
        visible text, formatting, warnings and link destinations before a send.
        """
        return self.call('preview_message',dict(account=account,chat_id=chat_id,text=text,
            reply_to=reply_to,format=format,link_preview=link_preview,allow_lossy=allow_lossy))

    def send_message(self,*,account,chat_id,text,request_id,reply_to='',format='plain',
                     preview_digest='',link_preview=False,allow_lossy=False,wait_s=0):
        """Submit exactly confirmed content; rich sends must match a reviewed preview.

        Keep request_id until the result is known. The digest never proves human
        approval; the acting agent obtains exact owner confirmation separately.
        """
        parameters=dict(account=account,chat_id=chat_id,text=text,
            request_id=request_id,reply_to=reply_to)
        _wait_seconds(wait_s)
        if wait_s!=0:parameters['wait_s']=wait_s
        # Default plain calls remain compatible with older strict servers.
        # Nondefault values pass unchanged so the server can reject bad types.
        if (format!='plain' or preview_digest!='' or link_preview is not False
                or allow_lossy is not False):
            parameters.update(format=format,preview_digest=preview_digest,
                link_preview=link_preview,allow_lossy=allow_lossy)
        return self.call('send_message',parameters)

    def get_send_status(self,send_id):
        return self.call('get_send_status',{'send_id':send_id})

    def list_messenger_operations(self, *, account):
        """Read this account's allowlisted operations and your independently scoped grants."""
        return self.call('list_messenger_operations', {'account': account})

    def preview_messenger_operation(self, *, account, operation, chat_id='', message_id='',
                                   expected_revision=0, parameters=_OMITTED, request_id=_OMITTED):
        """Passively validate intent. A preview digest is not human approval.

        request_id (ANYM-549) is accepted so preview and execute share arguments; it is
        ignored and sent only when given, so older servers keep taking the call.
        """
        return self.call('preview_messenger_operation', dict(account=account, operation=operation,
            chat_id=chat_id, message_id=message_id, expected_revision=expected_revision,
            parameters={} if parameters is _OMITTED else parameters,
            **({} if request_id is _OMITTED else {'request_id': request_id})))

    def execute_messenger_operation(self, *, account, operation, request_id, chat_id='',
                                   message_id='', expected_revision=0, parameters=_OMITTED,
                                   preview_digest='', wait_s=0):
        """Queue exact confirmed intent; poll the receipt and never retry ambiguous effects.

        Named live-read permissions are separate. For every mutation, the acting
        agent obtains exact owner confirmation; the digest only binds the payload.
        wait_s (0-25 s, ANYM-549) waits for the receipt; it is validated, and 0 is not sent.
        """
        return self.call('execute_messenger_operation', dict(account=account, operation=operation,
            chat_id=chat_id, message_id=message_id, expected_revision=expected_revision,
            parameters={} if parameters is _OMITTED else parameters,
            request_id=request_id, preview_digest=preview_digest, wait_s=wait_s))

    def get_messenger_operation(self, *, operation_id):
        """Read this key's scoped operation receipt without invoking the provider again."""
        return self.call('get_messenger_operation', {'operation_id': operation_id})

    def create_subscription(self, *, chat_ids, sender_ids=None, kinds=None,
                            direction='incoming', origins=None, label='', expires_at=None,
                            wake_rules=None, reply_to_send=None, delivery=None):
        """Persist explicit archived sources from the committed boundary; no push or provider action.

        Requires read and explicit subscription-write. A timeout can leave creation
        uncertain: inspect list_subscriptions before retrying. Names are not sender IDs.
        delivery={'endpoint_id': ..., 'delivery_policy': ...} binds your own endpoint (delivery-write).
        """
        parameters=dict(chat_ids=chat_ids,sender_ids=sender_ids,kinds=kinds,direction=direction,
            origins=origins,label=label,expires_at=expires_at)
        if wake_rules is not None:parameters['wake_rules']=wake_rules
        if reply_to_send is not None:parameters['reply_to_send']=reply_to_send
        if delivery is not None:parameters['delivery']=delivery
        return self.call('create_subscription',parameters)

    def list_subscriptions(self, *, limit=50):
        """Read only this originating credential's definitions, up to 100."""
        return self.call('list_subscriptions',{'limit':limit})

    def get_subscription(self, *, subscription_id):
        """Read lifecycle and archive matcher frontier; this does not prove provider freshness."""
        return self.call('get_subscription',{'subscription_id':subscription_id})

    def update_subscription(self, *, subscription_id, version, action, resume_policy='catch_up'):
        """Version-checked pause/resume/close. After uncertainty, read before retrying."""
        return self.call('update_subscription',dict(subscription_id=subscription_id,version=version,
            action=action,resume_policy=resume_policy))

    def get_subscription_events(self, *, subscription_id, after=0, limit=50):
        """Inspect durable matched references; never ACK, wake an agent or mark provider read."""
        return self.call('get_subscription_events',dict(subscription_id=subscription_id,after=after,limit=limit))

    def fetch_subscription_events(self, *, subscription_id, limit=50):
        """Read an unacknowledged page; an explicit ACK grant may issue a short-lived receipt."""
        return self.call('fetch_subscription_events',dict(subscription_id=subscription_id,limit=limit))

    def ack_subscription_events(self, *, subscription_id, receipt, version):
        """ACK exactly the issued page and ack_version; never changes provider read state."""
        return self.call('ack_subscription_events',dict(subscription_id=subscription_id,receipt=receipt,version=version))

    def wait_for_subscription_events(self, *, subscription_id, timeout=25, limit=50):
        """Wait up to 25 seconds without ACK or provider polling. Explicit client timeouts apply."""
        return self.call('wait_for_subscription_events',dict(subscription_id=subscription_id,timeout=timeout,limit=limit))

    def configure_subscription_delivery(self, *, subscription_id, version, receiver_id=None, delivery_policy=None,
                                        endpoint_id=None):
        """Bind an operator-registered receiver or your own endpoint_id (not both), or disable with None. URLs are not accepted."""
        arguments = dict(subscription_id=subscription_id,version=version,receiver_id=receiver_id)
        if delivery_policy is not None: arguments['delivery_policy'] = delivery_policy
        if endpoint_id is not None: arguments['endpoint_id'] = endpoint_id
        return self.call('configure_subscription_delivery',arguments)

    def test_subscription(self, *, subscription_id, request_id):
        """Queue an idempotent synthetic hint; receiver success never proves a host turn."""
        return self.call('test_subscription',dict(subscription_id=subscription_id,request_id=request_id))

    def register_delivery_endpoint(self, *, request_id, url, signing_secret, label='', headers=None):
        """Register your own HTTPS endpoint inside the operator's host policy; returns a signed test's result.

        Requires read and explicit delivery-write. signing_secret (whsec_<base64>) and header
        values are write-only and never returned. Repeat the same request_id to recover a lost response.
        """
        arguments=dict(request_id=request_id,url=url,signing_secret=signing_secret,label=label)
        if headers is not None:arguments['headers']=headers
        return self.call('register_delivery_endpoint',arguments)

    def list_delivery_endpoints(self):
        """This key's endpoints with header names and fingerprints, never secrets or header values."""
        return self.call('list_delivery_endpoints',{})

    def update_delivery_endpoint(self, *, endpoint_id, version, action, new_secret=None, headers=None):
        """Version-checked rotate_secret (new_secret), set_headers (headers), pause or resume of your endpoint."""
        arguments=dict(endpoint_id=endpoint_id,version=version,action=action)
        if new_secret is not None:arguments['new_secret']=new_secret
        if headers is not None:arguments['headers']=headers
        return self.call('update_delivery_endpoint',arguments)

    def delete_delivery_endpoint(self, *, endpoint_id, version):
        """Delete your endpoint; bound subscriptions become pull-only."""
        return self.call('delete_delivery_endpoint',dict(endpoint_id=endpoint_id,version=version))

    def test_delivery_endpoint(self, *, endpoint_id, request_id):
        """Send one signed test delivery; the same request_id reads the same test back."""
        return self.call('test_delivery_endpoint',dict(endpoint_id=endpoint_id,request_id=request_id))

    def submit_feedback(self, *, request_id, category, description, operation=None, refs=None):
        """Record bounded private feedback locally. Never forwards or publishes it."""
        return self.call('submit_feedback',dict(request_id=request_id,category=category,
            description=description,operation=operation,refs=refs))

    def list_feedback(self, *, limit=50, status=None):
        """Read only this credential's private feedback and its status, without former source references."""
        return self.call('list_feedback',{'limit':limit,**({} if status is None else {'status':status})})
