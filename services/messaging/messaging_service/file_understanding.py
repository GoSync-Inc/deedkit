"""Independent descriptions: retain ASR/extraction, publish fenced search metadata.

Model outputs are untrusted observations. This worker never downloads models,
opens provider sessions, or chooses endpoints from attachment contents.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .enrichment_providers import ModelError, infer, normalize_result, validate_profile, private_text, profile_hash
from . import resource_admission
from .enrichment import MAX_RESULT_BYTES, child_limits, kill_group, verified_copy


SOURCE_COLUMNS='''a.id AS attachment,a.digest AS source_digest,m.revision AS source_revision,
 a.mime,a.bytes,a.active,m.deleted,m.account,m.sent_at,u.kind,
 f.state AS text_state,f.indexed_at AS text_indexed_at,f.source_digest AS text_digest,f.source_revision AS text_revision,
 f.text_content AS extracted_text,f.extraction_metadata,f.truncated AS text_truncated,
 e.state AS enrichment_state,e.indexed_at AS enrichment_indexed_at,e.kind AS enrichment_kind,e.source_digest AS enrichment_digest,
 e.source_revision AS enrichment_revision,e.text_content AS enriched_text,e.metadata AS enrichment_metadata,
 e.truncated AS enrichment_truncated,e.title AS enrichment_title,e.summary AS enrichment_summary,
 e.provider AS enrichment_provider,e.model AS enrichment_model'''
SOURCE_JOIN='''FROM attachment_understanding u JOIN attachments a ON a.id=u.attachment
 JOIN messages m ON m.id=a.message LEFT JOIN attachment_text f ON f.attachment=a.id
 LEFT JOIN attachment_enrichment e ON e.attachment=a.id'''
# Exactly the characters str.strip() removes: a source _source() finds blank must also fail
# SQL eligibility, or blank rows fill the claim window and starve ready ones.
WHITESPACE='\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0                　'
BLANK="E'"+''.join('\\u%04x'%ord(c) for c in WHITESPACE)+"'"
SOURCE_ELIGIBLE={
    'audio':"(e.state='ready' OR e.indexed_at IS NOT NULL) AND e.kind='audio' AND e.source_digest=a.digest AND e.source_revision=m.revision AND length(btrim(e.text_content,"+BLANK+"))>0",
    'document':"(f.state='ready' OR f.indexed_at IS NOT NULL) AND f.source_digest=a.digest AND f.source_revision=m.revision AND length(btrim(f.text_content,"+BLANK+"))>0",
    'image':"(r.state='resident' OR ((e.state='ready' OR e.indexed_at IS NOT NULL) AND e.source_digest=a.digest AND e.source_revision=m.revision AND length(btrim(e.title))>0 AND length(btrim(e.summary))>0))",
    'video':"r.state='resident'"}


def _source(row):
    row=dict(row);kind=row['kind']
    row.update(source_text='',source_metadata={},source_truncated=False,source_ready=True)
    current=lambda prefix: ((row.get(prefix+'_state')=='ready' or row.get(prefix+'_indexed_at') is not None)
        and row.get(prefix+'_digest')==row['source_digest']
        and row.get(prefix+'_revision')==row['source_revision'])
    if kind=='document':
        row.update(source_text=row.get('extracted_text') or '',source_metadata=row.get('extraction_metadata') or {},
            source_truncated=bool(row.get('text_truncated')),source_ready=current('text'))
    elif kind=='audio':
        row.update(source_text=row.get('enriched_text') or '',source_metadata=row.get('enrichment_metadata') or {},
            source_truncated=bool(row.get('enrichment_truncated')),
            source_ready=current('enrichment') and row.get('enrichment_kind')=='audio')
    elif kind=='image' and current('enrichment') and row.get('enrichment_title') and row.get('enrichment_summary'):
        row.update(source_text='\n'.join(row.get(x) or '' for x in ('enrichment_title','enrichment_summary','enriched_text')),
            source_metadata=row.get('enrichment_metadata') or {},source_truncated=bool(row.get('enrichment_truncated')))
        row['reuse_result']={'title':row['enrichment_title'],'summary':row['enrichment_summary'],
            'truncated':row['source_truncated']}
    if kind in ('audio','document') and not row['source_text'].strip():row['source_ready']=False
    return row


def current_understanding_source(c,attachment):
    row=c.execute('SELECT '+SOURCE_COLUMNS+' '+SOURCE_JOIN+' WHERE u.attachment=%s',(attachment,)).fetchone()
    return _source(row) if row else None


def valid_understanding(c,row):
    source=current_understanding_source(c,row['attachment'])
    return bool(source and source['source_ready'] and source['active'] and not source['deleted']
        and source['source_digest']==row['source_digest'] and source['source_revision']==row['source_revision']
        and source_artifact_hash(source)==row['source_artifact_hash'])


def configuration(path):
    raw=json.loads(private_text(path,65536))
    if not isinstance(raw,dict) or set(raw)-{'enabled','profiles','accounts','max_jobs_per_day',
            'profile_max_jobs_per_day','account_max_jobs_per_day'}:
        raise ValueError('invalid_understanding_config')
    if raw.get('enabled') is not True:raise ValueError('understanding_disabled')
    accounts=raw.get('accounts')
    if (not isinstance(accounts,list) or not accounts or len(accounts)>100
            or any(not isinstance(v,str) or not v or len(v)>64 for v in accounts)):
        raise ValueError('understanding_accounts_required')
    profiles=raw.get('profiles')
    if not isinstance(profiles,dict) or set(profiles)-{'audio','document','image','video'}:
        raise ValueError('invalid_understanding_profiles')
    maximum=raw.get('max_jobs_per_day',20)
    if type(maximum) is not int or not 1<=maximum<=10000:raise ValueError('invalid_understanding_budget')
    for field,keys in [('profile_max_jobs_per_day',profiles),('account_max_jobs_per_day',accounts)]:
        limits=raw.get(field,{})
        if not isinstance(limits,dict) or set(limits)-set(keys) or any(type(v) is not int or not 1<=v<=10000 for v in limits.values()):
            raise ValueError('invalid_understanding_budget')
    return {**raw,'max_jobs_per_day':maximum,'profiles':{k:understanding_profile(v,k) for k,v in profiles.items()}}


def understanding_profile(raw, kind):
    if kind not in ('audio','document','image','video'):
        raise ValueError('invalid_understanding_kind')
    if not isinstance(raw,dict) or raw.get('provider') in ('faster_whisper','codex_cli'):
        raise ValueError('understanding_text_or_vision_profile_required')
    values=dict(raw);video={}
    if kind=='video':
        for key,default,minimum,maximum in [('video_frames',4,1,8),('video_frame_edge',384,64,512),
                ('video_max_seconds',14400,1,14400),('max_input_bytes',128*1024**2,1,1024**3)]:
            value=values.pop(key,default)
            if type(value) is not int or not minimum<=value<=maximum:raise ValueError('invalid_video_limit')
            video[key]=value
    profile=validate_profile(values,'document' if kind in ('audio','document') else 'image')
    profile.update(video)
    profile['prompt_version']='retained-file-understanding-v1'
    return profile


def source_artifact_hash(row):
    # A caption revision changes the occurrence fence, not retained file content.
    source={'kind':row['kind'],'digest':row['source_digest'],
        'text':row.get('source_text',''),'metadata':row.get('source_metadata',{}),
        'truncated':bool(row.get('source_truncated'))}
    return hashlib.sha256(json.dumps(source,sort_keys=True,separators=(',',':'),
        ensure_ascii=False).encode()).hexdigest()


def description_result(value, coverage):
    clean=normalize_result(value)
    clean['title']=clean['title'].strip();clean['summary']=clean['summary'].strip()
    if not clean['title'] or not clean['summary']:
        raise ModelError('understanding_description_empty')
    # Model prose cannot replace the retained transcript or extracted document.
    clean['text']='';clean['coverage']=coverage
    clean['metadata']['prompt_version']='retained-file-understanding-v1'
    return clean


def sample_times(duration, count):
    if (type(duration) not in (int,float) or not math.isfinite(duration) or duration<=0
            or type(count) is not int or not 1<=count<=8):
        raise ModelError('invalid_video')
    return [round(duration*index/count,6) for index in range(count)]


def _source_incomplete(row):
    metadata=row.get('source_metadata',{})
    return bool(row.get('source_truncated') or metadata.get('complete') is False
        or metadata.get('source_truncated') is True or metadata.get('audio_truncated') is True)


def infer_understanding(profile,row,path=None,*,transport=None):
    kind=row['kind']
    if kind not in ('audio','document','image'):
        raise ModelError('invalid_understanding_kind')
    if kind in ('audio','document'):
        text=row.get('source_text','')
        if not text.strip():raise ModelError('understanding_source_empty')
        capped=len(text)>profile['max_text_chars']
        metadata=row.get('source_metadata',{})
        coverage={'mode':'transcript_summary' if kind=='audio' else 'extracted_text_summary',
            'complete':not (capped or _source_incomplete(row)),
            'source':'attachment_enrichment' if kind=='audio' else 'attachment_text',
            'retained_chars':len(text),'input_chars':min(len(text),profile['max_text_chars'])}
        if kind=='audio':coverage['timestamps_available']=bool(metadata.get('segments'))
        result=infer(profile,'document',None,'text/plain',text,transport=transport)
        clean=description_result(result,coverage)
        clean['truncated']=clean['truncated'] or not coverage['complete']
        return clean
    result=infer(profile,'image',path,row['mime'],transport=transport)
    return description_result(result,{'mode':'image_description','complete':not result['truncated']})


VIDEO_FORMATS='mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,mpegts,ogg'


def _video_tool(command,*,capture=False):
    with tempfile.TemporaryFile() as output:
        try:
            done=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=output if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,timeout=20,check=False,pass_fds=resource_admission.inherited_fds())
        except FileNotFoundError:raise ModelError('video_tools_unavailable') from None
        except subprocess.TimeoutExpired:raise ModelError('video_processing_timeout') from None
        if done.returncode:raise ModelError('invalid_video')
        if capture:
            output.seek(0);data=output.read(65537)
            if len(data)>65536:raise ModelError('invalid_video')
            return data


def sample_video(path,directory,profile):
    """Only local, explicitly allowed containers; samples never imply speech ASR.

    Call in the isolated child: its process-group, CPU, AS and file limits cover
    ffprobe and every ffmpeg descendant. Native network protocols are disabled.
    """
    directory=Path(directory)
    raw=_video_tool(['ffprobe','-v','error','-protocol_whitelist','file,pipe',
        '-format_whitelist',VIDEO_FORMATS,'-select_streams','v:0','-show_entries',
        'format=duration,format_name:stream=duration,width,height','-of','json',str(path)],capture=True)
    try:
        probe=json.loads(raw);streams=probe['streams'];stream=streams[0]
        width,height=stream['width'],stream['height']
        if (type(width) is not int or type(height) is not int or not 1<=width<=8192 or not 1<=height<=8192
                or width*height>16777216):raise ValueError
        candidates=[probe.get('format',{}).get('duration'),stream.get('duration')]
        durations=[float(value) for value in candidates if value not in (None,'N/A')]
        if not durations or any(not math.isfinite(value) or value<=0 for value in durations):raise ValueError
        duration=max(durations)
        if duration>profile['video_max_seconds']:raise ModelError('video_duration_limit')
    except (ValueError,KeyError,TypeError,IndexError,AttributeError):raise ModelError('invalid_video') from None
    times=sample_times(duration,profile['video_frames']);edge=profile['video_frame_edge']
    for index,second in enumerate(times):
        frame=directory/f'frame-{index}.png'
        _video_tool(['ffmpeg','-v','error','-nostdin','-threads','2','-protocol_whitelist','file,pipe',
            '-format_whitelist',VIDEO_FORMATS,'-ss',str(second),'-i',str(path),'-map','0:v:0',
            '-an','-sn','-dn','-frames:v','1','-vf',
            f'scale={edge}:{edge}:force_original_aspect_ratio=decrease,pad={edge}:{edge}:(ow-iw)/2:(oh-ih)/2',
            '-f','image2','-vcodec','png',str(frame)])
        if not frame.is_file() or not 0<frame.stat().st_size<=MAX_RESULT_BYTES:raise ModelError('invalid_video')
    columns=math.ceil(math.sqrt(len(times)));rows=math.ceil(len(times)/columns)
    contact=directory/'sampled-video.png'
    _video_tool(['ffmpeg','-v','error','-nostdin','-threads','2','-protocol_whitelist','file,pipe',
        '-f','image2','-framerate','1','-i',str(directory/'frame-%d.png'),'-frames:v','1',
        '-vf',f'tile={columns}x{rows}:nb_frames={len(times)}','-f','image2','-vcodec','png',str(contact)])
    if not contact.is_file() or not 0<contact.stat().st_size<=MAX_RESULT_BYTES:raise ModelError('invalid_video')
    return contact,{'mode':'sampled_frames','complete':False,'sampled_frames':len(times),
        'sampled_timestamps_seconds':times,'duration_seconds':duration,'audio_transcribed':False}


def _infer_video(profile,path,*,transport=None):
    from . import enrichment_providers
    with tempfile.TemporaryDirectory(prefix='video-samples-') as directory:
        contact,coverage=sample_video(path,directory,profile)
        original_prompt=enrichment_providers.PROMPT
        # This module runs a single request per isolated child. Never accept a
        # prompt from the file; the timestamps originate in bounded sampling.
        enrichment_providers.PROMPT=(original_prompt+' This image is a row-major contact sheet of video frames sampled at '
            +json.dumps(coverage['sampled_timestamps_seconds'])+' seconds. Describe ONLY these sampled frames. '
            'The video was not watched in full and its audio was not transcribed. '
            'Do not claim events between samples, spoken words, or a complete video transcript.')
        try:result=infer(profile,'image',contact,'image/png',transport=transport)
        finally:enrichment_providers.PROMPT=original_prompt
        return description_result(result,coverage)


def _isolated_process(files,row,profile):
    with tempfile.TemporaryDirectory(prefix='file-understanding-') as directory:
        path=None
        if row['kind'] in ('image','video') and not row.get('reuse_result'):
            path=verified_copy(files,row,directory,profile['max_input_bytes'])
        payload={key:row[key] for key in ('kind','mime','source_text','source_metadata','source_truncated','reuse_result') if key in row}
        metadata=row.get('source_metadata',{})
        payload['source_metadata']={key:metadata[key] for key in ('complete','audio_truncated','source_truncated') if key in metadata}
        if metadata.get('segments'):payload['source_metadata']['segments']=[{}]
        payload.update(profile=profile,path=str(path) if path else None)
        env={key:value for key,value in os.environ.items() if key in ('PATH','LANG','SSL_CERT_FILE')}
        env.update(HOME=directory,TMPDIR=directory,HF_HUB_OFFLINE='1',TOKENIZERS_PARALLELISM='false',
            OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
        lease=row.get('_resource_lease')
        if lease:env['PROCESSING_RESOURCE_FD']=str(lease.fd)
        entry='import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+');from messaging_service.file_understanding import main;main()'
        with tempfile.TemporaryFile() as output:
            child=subprocess.Popen([sys.executable,'-I','-c',entry,'--child'],stdin=subprocess.PIPE,
                stdout=output,stderr=subprocess.DEVNULL,env=env,cwd=directory,start_new_session=True,
                close_fds=True,pass_fds=(lease.fd,) if lease else ())
            try:child.communicate(json.dumps(payload).encode(),timeout=profile['timeout_seconds']+10)
            except subprocess.TimeoutExpired:
                kill_group(child);child.wait();raise ModelError('model_timeout') from None
            finally:kill_group(child)
            if child.returncode:raise ModelError('model_process_failed')
            output.seek(0);raw=output.read(MAX_RESULT_BYTES+1)
        if len(raw)>MAX_RESULT_BYTES:raise ModelError('model_response_too_large')
        try:result=json.loads(raw)
        except (ValueError,TypeError,RecursionError):raise ModelError('invalid_model_response') from None
        if not isinstance(result,dict):raise ModelError('invalid_model_response')
        if result.get('error'):
            import re
            code=result['error']
            if not isinstance(code,str) or not re.fullmatch('[a-z_]{1,64}',code):code='understanding_failed'
            raise ModelError(code)
        return result


def _coverage(row,value):
    if not isinstance(value,dict) or len(json.dumps(value))>16384:raise ModelError('invalid_understanding_coverage')
    kind=row['kind'];mode={'audio':'transcript_summary','document':'extracted_text_summary',
        'image':'image_description','video':'sampled_frames'}[kind]
    if value.get('mode')!=mode or type(value.get('complete')) is not bool:raise ModelError('invalid_understanding_coverage')
    if kind in ('audio','document'):
        length=len(row['source_text']);count=min(length,row['_max_text_chars'])
        complete=not (count<length or _source_incomplete(row))
        if value['complete']!=complete:raise ModelError('invalid_understanding_coverage')
        clean={'mode':mode,'complete':complete,'source':'attachment_enrichment' if kind=='audio' else 'attachment_text',
            'retained_chars':length,'input_chars':count}
        if kind=='audio':clean['timestamps_available']=bool(row.get('source_metadata',{}).get('segments'))
        return clean
    if kind=='image':return {'mode':mode,'complete':value['complete'] and not row.get('source_truncated')}
    duration=value.get('duration_seconds');count=value.get('sampled_frames');times=value.get('sampled_timestamps_seconds')
    if (value['complete'] is not False or value.get('audio_transcribed') is not False
            or type(duration) not in (int,float) or not math.isfinite(duration) or not 0<duration<=row['_video_max_seconds']
            or type(count) is not int or count!=row['_video_frames'] or not isinstance(times,list)
            or len(times)!=count or any(type(t) not in (int,float) or not math.isfinite(t) or not 0<=t<duration for t in times)
            or times!=sample_times(duration,count)):
        raise ModelError('invalid_understanding_coverage')
    return {'mode':mode,'complete':False,'duration_seconds':duration,'sampled_frames':count,
        'sampled_timestamps_seconds':times,'audio_transcribed':False}


def process(files,row,profile):
    return _isolated_process(files,row,profile)


class FileUnderstandingWorker:
    def __init__(self,dsn,files,config,*,processor=process,lock_directory=None):
        self.dsn,self.files,self.config,self.processor=dsn,Path(files),config,processor
        self.lock_directory=lock_directory or os.environ.get('PROCESSING_LOCK_DIR','/processing-locks')

    def connection(self):
        return psycopg.connect(self.dsn,row_factory=dict_row,connect_timeout=5,
            options='-c statement_timeout=5000 -c timezone=UTC')

    def publish_profiles(self,*,enabled=True):
        import re
        from .file_lifecycle import lifecycle_lock
        with self.connection() as c:
            lifecycle_lock(c)
            for account in sorted(self.config['accounts']):
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
                for kind,profile in self.config['profiles'].items():
                    ph=profile_hash(profile);model=profile['model']
                    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',model):model='configured_model'
                    c.execute('''UPDATE attachment_understanding u SET
                        state=CASE WHEN indexed_at IS NOT NULL THEN 'ready' ELSE 'not_configured' END,
                        lease_token=NULL,lease_until=NULL,attempts=0,
                        next_due=clock_timestamp(),updated_at=clock_timestamp()
                        FROM attachments a,messages m WHERE u.attachment=a.id AND m.id=a.message
                        AND m.account=%s AND u.kind=%s AND u.profile_hash IS NOT NULL
                        AND u.profile_hash<>%s AND u.state<>'superseded' ''',(account,kind,ph))
                    c.execute('''INSERT INTO file_understanding_profiles(account,kind,profile_hash,provider,model,enabled,expires_at,pause_reason,paused_until)
                        SELECT %s,%s,%s,%s,%s,%s,clock_timestamp()+make_interval(secs=>%s),pc.pause_reason,pc.paused_until
                        FROM (SELECT 1) singleton LEFT JOIN processing_control pc ON pc.account=%s AND pc.kind=%s AND pc.profile_hash=%s
                        AND pc.pause_reason IS NOT NULL AND (pc.paused_until IS NULL OR pc.paused_until>clock_timestamp())
                        ON CONFLICT(account,kind) DO UPDATE SET profile_hash=excluded.profile_hash,provider=excluded.provider,
                        model=excluded.model,enabled=excluded.enabled,expires_at=excluded.expires_at,
                        observed_at=clock_timestamp(),pause_reason=excluded.pause_reason,paused_until=excluded.paused_until''',
                        (account,kind,ph,profile['provider'],model,enabled,profile['timeout_seconds']+120,account,kind,ph))
                    if enabled:
                        source=SOURCE_ELIGIBLE[kind]
                        unsupported="f.state='unsupported'" if kind=='document' else "e.state='unsupported'" if kind=='audio' else 'false'
                        waiting={'audio':'waiting_for_transcript','document':'waiting_for_text',
                            'image':'waiting_for_original','video':'waiting_for_original'}[kind]
                        c.execute('''UPDATE attachment_understanding target SET
                            state=CASE WHEN NOT coalesce(('''+source+'''),false) AND ('''+unsupported+''') THEN 'unsupported' ELSE 'queued' END,
                            error_code=CASE WHEN coalesce(('''+source+'''),false) THEN NULL
                                WHEN ('''+unsupported+''') THEN 'source_unsupported' ELSE %s END
                            FROM attachment_understanding u JOIN attachments a ON a.id=u.attachment JOIN messages m ON m.id=a.message
                            LEFT JOIN attachment_text f ON f.attachment=a.id LEFT JOIN attachment_enrichment e ON e.attachment=a.id
                            LEFT JOIN attachment_residency r ON r.digest=a.digest
                            WHERE target.attachment=u.attachment AND m.account=%s AND u.kind=%s AND a.active AND NOT m.deleted
                            AND target.indexed_at IS NULL AND target.state IN ('not_configured','queued','unsupported')''',
                            (waiting,account,kind))

    def claim(self):
        from .file_lifecycle import lifecycle_lock
        with resource_admission.transaction(self.connection()) as (c,resources):
            lifecycle_lock(c)
            c.execute('SELECT pg_advisory_xact_lock(74509139)')
            for account in sorted(self.config['accounts']):
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
            c.execute("INSERT INTO enrichment_budget(day,attempts) VALUES(current_date,0) ON CONFLICT DO NOTHING")
            used=c.execute('SELECT attempts FROM enrichment_budget WHERE day=current_date FOR UPDATE').fetchone()['attempts']
            if used>=self.config['max_jobs_per_day']:return None
            c.execute('''UPDATE attachment_understanding u SET state='failed',error_code='retry_exhausted',lease_token=NULL,lease_until=NULL
                FROM attachments a,messages m WHERE a.id=u.attachment AND m.id=a.message AND m.account=ANY(%s)
                AND u.state='leased' AND u.lease_until<=clock_timestamp() AND u.attempts>=3''',(self.config['accounts'],))
            profiles=[]
            for kind,profile in self.config['profiles'].items():
                last=c.execute('''SELECT coalesce(max(last_started_at),'epoch'::timestamptz) started
                    FROM processing_control WHERE kind=%s AND profile_hash=%s AND account=ANY(%s)''',
                    (kind,profile_hash(profile),self.config['accounts'])).fetchone()['started']
                profiles.append((last,kind,profile))
            for _,kind,profile in sorted(profiles,key=lambda item:item[0]):
                ph=profile_hash(profile)
                used_profile=c.execute('SELECT coalesce(sum(attempts),0) n FROM processing_budget WHERE day=current_date AND kind=%s',(kind,)).fetchone()['n']
                if used_profile>=self.config.get('profile_max_jobs_per_day',{}).get(kind,10000):continue
                accounts=[]
                for account in self.config['accounts']:
                    total=c.execute('SELECT coalesce(sum(attempts),0) n FROM processing_budget WHERE day=current_date AND account=%s',(account,)).fetchone()['n']
                    if total<self.config.get('account_max_jobs_per_day',{}).get(account,10000):accounts.append(account)
                if not accounts:continue
                source_condition=SOURCE_ELIGIBLE[kind]
                rows=c.execute('SELECT '+SOURCE_COLUMNS+''',u.state,u.profile_hash,u.attempts,u.source_artifact_hash,
                    r.state AS residency_state '''+SOURCE_JOIN+'''
                    LEFT JOIN attachment_residency r ON r.digest=a.digest
                    LEFT JOIN processing_control pc ON pc.account=m.account AND pc.kind=%s AND pc.profile_hash=%s
                    WHERE u.kind=%s AND a.active AND NOT m.deleted AND m.account=ANY(%s)
                    AND ('''+source_condition+''')
                    AND u.source_digest=a.digest AND u.source_revision=m.revision AND u.next_due<=clock_timestamp()
                    AND (pc.pause_reason IS NULL OR pc.paused_until<=clock_timestamp())
                    AND ((u.state IN ('not_configured','queued','failed') AND (u.attempts<3 OR u.profile_hash IS DISTINCT FROM %s))
                     OR (u.state='leased' AND u.lease_until<=clock_timestamp() AND u.attempts<3)
                     OR (u.state IN ('ready','unsupported') AND u.profile_hash IS DISTINCT FROM %s))
                    ORDER BY coalesce(pc.last_started_at,'epoch'::timestamptz),
                        (m.sent_at>=clock_timestamp()-interval '1 day') DESC,m.sent_at DESC,a.id
                    LIMIT 64 FOR UPDATE OF u SKIP LOCKED''',(kind,ph,kind,accounts,ph,ph)).fetchall()
                for raw in rows:
                    row=_source(raw)
                    if not row['source_ready']:continue
                    if kind in ('image','video') and not row.get('reuse_result') and row['residency_state']!='resident':continue
                    token=uuid4();ttl=profile['timeout_seconds']+60;lease=None
                    if resource_admission.local_heavy(profile) or kind=='video':
                        lease=resource_admission.acquire(c,self.lock_directory,'understanding',row['attachment'],token,ttl)
                        if lease is None:continue
                        resources.append(lease)
                    artifact=source_artifact_hash(row)
                    attempts=1 if row['profile_hash']!=ph or row['source_artifact_hash']!=artifact else row['attempts']+1
                    c.execute('''UPDATE attachment_understanding SET state='leased',source_artifact_hash=%s,
                        profile_hash=%s,provider=%s,model=%s,attempts=%s,lease_token=%s,
                        lease_until=clock_timestamp()+make_interval(secs=>%s),updated_at=clock_timestamp()
                        WHERE attachment=%s''',(artifact,ph,profile['provider'],profile['model'],attempts,token,ttl,row['attachment']))
                    c.execute('UPDATE enrichment_budget SET attempts=attempts+1 WHERE day=current_date')
                    c.execute('''INSERT INTO processing_budget(day,account,kind,profile_hash,attempts) VALUES(current_date,%s,%s,%s,1)
                        ON CONFLICT(day,account,kind,profile_hash) DO UPDATE SET attempts=processing_budget.attempts+1''',(row['account'],kind,ph))
                    c.execute('''INSERT INTO processing_control(account,kind,profile_hash,last_started_at) VALUES(%s,%s,%s,clock_timestamp())
                        ON CONFLICT(account,kind,profile_hash) DO UPDATE SET last_started_at=excluded.last_started_at,updated_at=clock_timestamp()''',(row['account'],kind,ph))
                    return {**row,'profile_hash':ph,'source_artifact_hash':artifact,'attempts':attempts,
                        'provider':profile['provider'],'model':profile['model'],'lease_token':token,'_resource_lease':lease,
                        '_max_text_chars':profile['max_text_chars'],'_video_frames':profile.get('video_frames'),
                        '_video_max_seconds':profile.get('video_max_seconds')},profile
        return None

    def finish(self,row,result=None,error=None):
        from .file_lifecycle import lifecycle_lock
        lease=row.get('_resource_lease')
        try:
            with self.connection() as c:
                lifecycle_lock(c)
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+row['account'],))
                if not valid_understanding(c,row):
                    resource_admission.release(c,lease);return False
                clean=description_result(result,_coverage(row,result.get('coverage',{}))) if result is not None else None
                if clean:
                    clean['truncated']=clean['truncated'] or bool(result.get('truncated')) or bool(row.get('source_truncated'))
                    if row['kind']!='video' and not clean['coverage']['complete']:clean['truncated']=True
                    if row.get('reuse_result'):clean['coverage'].update(source='attachment_enrichment',reused_description=True)
                provider=row.get('enrichment_provider') if row.get('reuse_result') else row['provider']
                model=row.get('enrichment_model') if row.get('reuse_result') else row['model']
                import re
                if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',model):model='configured_model'
                if not isinstance(provider,str) or not re.fullmatch(r'[a-z_]{1,64}',provider):provider='retained_enrichment'
                success=clean is not None
                saved=c.execute('''UPDATE attachment_understanding u SET state=%s,
                    title=CASE WHEN %s THEN %s ELSE title END,summary=CASE WHEN %s THEN %s ELSE summary END,
                    coverage=CASE WHEN %s THEN %s ELSE coverage END,truncated=CASE WHEN %s THEN %s ELSE truncated END,
                    published_profile_hash=CASE WHEN %s THEN %s ELSE published_profile_hash END,
                    published_provider=CASE WHEN %s THEN %s ELSE published_provider END,
                    published_model=CASE WHEN %s THEN %s ELSE published_model END,
                    error_code=%s,lease_token=NULL,lease_until=NULL,
                    indexed_at=CASE WHEN %s THEN clock_timestamp() ELSE indexed_at END,
                    next_due=clock_timestamp()+interval '10 minutes',updated_at=clock_timestamp()
                    WHERE attachment=%s AND lease_token=%s AND lease_until>clock_timestamp()
                    AND source_artifact_hash=%s AND profile_hash=%s RETURNING attachment''',
                    ('ready' if clean else 'failed',success,clean['title'] if clean else '',success,clean['summary'] if clean else '',
                     success,Jsonb(clean['coverage'] if clean else {}),success,clean['truncated'] if clean else False,
                     success,row['profile_hash'],success,provider,success,model,error,success,
                     row['attachment'],row['lease_token'],row['source_artifact_hash'],row['profile_hash'])).fetchone()
                resource_admission.release(c,lease)
                if saved and error:resource_admission.pause_profile(c,row['account'],row['kind'],row['profile_hash'],error)
                return saved is not None
        finally:
            if lease:lease.close()

    def once(self):
        claim=self.claim()
        if not claim:return False
        row,profile=claim
        try:self.finish(row,self.processor(self.files,row,profile))
        except ModelError as exc:self.finish(row,error=exc.code)
        except Exception:
            self.finish(row,error='understanding_failed')
        return True


def main():
    import argparse
    import signal
    import threading
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path);parser.add_argument('--child',action='store_true')
    args=parser.parse_args()
    if args.child:
        try:
            raw=sys.stdin.buffer.read(8*1024**2+1)
            if len(raw)>8*1024**2:raise ModelError('understanding_input_too_large')
            payload=json.loads(raw);profile=payload['profile'];child_limits(profile)
            import resource
            resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_RESULT_BYTES,MAX_RESULT_BYTES))
            if payload.get('reuse_result'):
                result=description_result(payload['reuse_result'],{'mode':'image_description',
                    'complete':not payload.get('source_truncated',False)})
                result['truncated']=bool(payload.get('source_truncated'))
            elif payload['kind']=='video':result=_infer_video(profile,payload['path'])
            else:result=infer_understanding(profile,payload,payload.get('path'))
            encoded=json.dumps(result,ensure_ascii=False).encode()
            if len(encoded)>MAX_RESULT_BYTES:raise ModelError('model_response_too_large')
            sys.stdout.buffer.write(encoded)
        except ModelError as exc:print(json.dumps({'error':exc.code}))
        except Exception:print(json.dumps({'error':'understanding_failed'}))
        return
    if not args.config:parser.error('--config required')
    cfg=configuration(args.config)
    worker=FileUnderstandingWorker(private_text(os.environ['DATABASE_URL_FILE']).strip(),os.environ['PROBE_FILES'],cfg)
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    while not stop.is_set():
        worker.publish_profiles()
        if not worker.once():stop.wait(10)
    worker.publish_profiles(enabled=False)


if __name__=='__main__':main()
