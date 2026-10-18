import { PipecatClient } from '@pipecat-ai/client-js';
import { SmallWebRTCTransport } from '@pipecat-ai/small-webrtc-transport';
import { ActivityGate, TurnTiming, summarize } from './timing.mjs';
import { startRemoteAudio, stopRemoteAudio } from './audio-playback.mjs';
import { AudioHealth, audioHealthText } from './audio-health.mjs';
import { settleDisconnectedCall } from './call-cleanup.mjs';
import { MicrophoneControl, bindTalkButton } from './microphone.mjs';
import { createLearningUI } from './learning.mjs';
import './style.css';

const $ = id => document.getElementById(id);
const selected = new Map();
const fieldLabels = { delivery_window:'Интервал доставки', additional_cost:'Дополнительная плата',counterparty_response:'Реакция собеседника' };
const outcomeLabels = { unresolved:'Ответы ещё не собраны.', needs_owner:'Вопрос сохранён. Решение владельца можно дать позже.', followup_ready:'Решение сохранено. Можно подготовить продолжение.', needs_summary:'Решение передано. Ассистенту нужно обновить итог.', incomplete:'Не все поля собраны.', collected_needs_review:'Ответы собраны. Проверь результат.' };
const statusLabels = { waiting_for_browser:'Ожидает браузер', connecting:'Подключается…', active:'Разговор идёт', finished:'Разговор завершён', failed:'Ошибка модели', interrupted:'Прерван перезапуском' };
let config, brief = null, current = null, client = null, active = false, stopping = false, preparing = false;
let audioContext = null, probes = [], poller = null, audioPoller = null, timing = new TurnTiming(), lastRender = '';
let inboxSignature='';
let transport=null,health=new AudioHealth(),healthPoller=null,healthBusy=false,playerWaiting=0;
const microphone=new MicrophoneControl(enabled=>client?.enableMic(enabled),mic=>{
  $('talk').setAttribute('aria-pressed',String(mic.enabled));
  $('talk').textContent=mic.enabled?'Говори · отпусти, чтобы слушать':'Удерживай, чтобы говорить';
  $('mic-state').textContent=mic.mode
    ? (mic.enabled?'Микрофон включён, пока держишь кнопку.':'Микрофон выключен. Для ответа удерживай кнопку или пробел, когда она в фокусе.')
    : 'Микрофон открыт во время разговора. В шумном месте включи режим по кнопке перед запуском.';
  $('mute').textContent=mic.enabled?'Выключить микрофон':'Включить микрофон';
  locks();
});
bindTalkButton($('talk'),microphone,window,document);
Object.assign(outcomeLabels,{refused:'Собеседник отказался от разговора.',unavailable:'Собеседник сейчас недоступен.'});
const learningUI=createLearningUI({api,node,error,currentCall:()=>current,isActive:()=>active,
  promptChanged:async()=>{invalidate();await refreshConfig();}});

async function api(path, method='GET', body) {
  const response = await fetch(path,{method,headers:{'Content-Type':'application/json','X-Voice-Service':'1'},
    body:body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Сервис недоступен.');
  return result;
}
function error(message='') { $('error').textContent=message; $('error').hidden=!message; }
function node(tag,text,cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function status(text,isActive=false) { $('status').textContent=text; $('status').classList.toggle('active',isActive); }
function ms(value) { return Number.isFinite(value) ? `${Math.round(value)} мс` : '—'; }
function locks() {
  for (const id of ['goal','counterparty','language','voice','grok-voice','conversation-mode','dialog-engine','self-reference','owner-notes','supervisor-notes','fields','approval-fields','account','query','search','prepare','keys-toggle']) $(id).disabled=active || preparing;
  const joinWaiting=current?.id===config?.active_call_id && current?.status==='waiting_for_browser' && brief?.id===current?.brief_id;
  $('start').disabled=config?.channels?.browser==='local_rehearsal_only' || active || preparing || !brief || !config?.credentials[brief?.dialog_engine==='grok_realtime'?'xai':'gemini'] || (!!config?.active_call_id && !joinWaiting);
  $('stop').disabled=!active;
  $('push-to-talk').disabled=active || preparing;
  $('talk').hidden=!microphone.mode;
  $('talk').disabled=!active || !client || !microphone.ready;
  $('mute').hidden=microphone.mode;
  $('mute').disabled=!active || !client || !microphone.ready;
  document.querySelectorAll('.source-row input').forEach(el=>el.disabled=active || preparing);
}
function invalidate() {
  brief=null;
  $('brief-feedback').textContent='Настройки изменены. Подготовь поручение заново.';
  $('prompt-version').textContent='Предыдущий контекст не будет использован.';
  locks();
}
function formBody() {
  return {goal:$('goal').value.trim(),counterparty:$('counterparty').value.trim(),language:$('language').value,
    voice:$('voice').value,grok_voice:$('grok-voice').value,conversation_mode:$('conversation-mode').value,dialog_engine:$('dialog-engine').value,self_reference:$('self-reference').value,owner_notes:$('owner-notes').value.trim(),supervisor_notes:$('supervisor-notes').value.trim(),
    success_fields:$('fields').value.split(',').map(v=>v.trim()).filter(Boolean),
    approval_fields:$('approval-fields').value.split(',').map(v=>v.trim()).filter(Boolean),source_message_ids:[...selected.keys()]};
}
async function refreshConfig() {
  config=await api('/api/config');
  if(config.channels?.browser==='local_rehearsal_only' && !$('local-rehearsal-link')) {
    const link=node('a','Браузерная репетиция на этом Mac');link.id='local-rehearsal-link';link.href='http://127.0.0.1:13842/';
    $('start').parentElement.before(link);
  }
  providerUI();
  $('archive-label').textContent=config.archive.connected ? 'AnyMsg подключён' : 'AnyMsg недоступен';
  if (!config.archive.connected) $('archive-label').title='Можно проверить поручение с собственными фактами. Поиск сообщений пока недоступен.';
  // The daily call cap and every owner raise with the owner's quoted words (ANYM-353).
  const cap=config.call_cap;
  $('call-cap-label').textContent=cap ? `Звонков на сутки: осталось ${cap.daily_calls_remaining} из ${cap.daily_call_units}` : 'Лимит звонков недоступен';
  $('cap-raises').replaceChildren(...(cap?.raises || []).map(r=>node('small',`+${r.extra_units} до ${new Date(r.expires_at).toLocaleString('ru-RU')} · слова владельца: «${r.owner_instruction}»`)));
  const previous=$('account').value;
  $('account').replaceChildren(new Option('Все доступные аккаунты',''),...(config.archive.accounts || []).map(a=>new Option(a.label || a.id,a.id)));
  $('account').value=previous;
  if (!$('voice').options.length) {
    $('voice').replaceChildren(...config.voices.map(v=>new Option(v,v)));
    $('voice').value='Puck';
  }
  if (!$('grok-voice').options.length) {
    $('grok-voice').replaceChildren(...config.voice_providers.xai.voices.map(v=>new Option(v,v)));
    $('grok-voice').value='rex';
  }
  locks();
}
function providerUI() {
  const grok=$('dialog-engine').value==='grok_realtime', provider=grok?'xai':'gemini', label=grok?'xAI':'Gemini';
  $('grok-voice-field').hidden=!grok;
  $('key-provider').value=provider;
  $('voice').parentElement.hidden=grok;
  $('model-label').textContent=grok?config?.voice_providers.xai.model:config?.model;
  $('keys-toggle').textContent=config?.credentials[provider]?`Ключ ${label} сохранён`:`Подключить ${label}`;
}
function snapshot(b) {
  $('dialog-engine').value=b.dialog_engine || 'gemini_live';
  $('grok-voice').value=b.grok_voice || 'rex';
  providerUI();
  $('prompt').textContent=b.prompt;
  $('prompt-version').textContent=`${b.prompt_version} / ${b.model} / ${b.snapshot_sha256.slice(0,12)}`;
  $('facts').replaceChildren(...b.facts.map(f=>{
    const p=node('p',f.text,`fact${f.active_conflicts ? ' warn' : ''}`);
    p.append(node('small',f.source_ref==='owner_notes' ? 'Твои факты и ограничения' : `Источник ${f.source_ref}; ревизия ${f.source_revision ?? '—'}${f.active_conflicts ? '; есть конфликт в архиве' : ''}${f.truncated ? '; текст сокращён' : ''}`));
    return p;
  }));
}
function renderSources(found=[]) {
  const rows=[...selected.values(),...found.filter(r=>!selected.has(r.id))];
  $('selection-count').textContent=`${selected.size} выбрано`;
  $('sources').replaceChildren(...rows.map(r=>{
    const label=node('label',undefined,'source-row'), checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.checked=selected.has(r.id); checkbox.disabled=active;
    checkbox.onchange=()=>{
      if (checkbox.checked && selected.size>=8) { checkbox.checked=false; error('Можно выбрать до 8 сообщений.'); return; }
      if(checkbox.checked)selected.set(r.id,r);else selected.delete(r.id);
      $('selection-count').textContent=`${selected.size} выбрано`; invalidate();
    };
    const content=node('span'); content.append(node('b',`${r.account || ''} / ${r.chat || ''}`),node('span',r.snippet || '[Нет текста]','source-text'),node('small',`${r.sent_at || ''} / ${r.id}`));
    label.append(checkbox,content); return label;
  }));
  if(!rows.length)$('sources').append(node('p','По этому запросу сообщения не найдены.','empty'));
}
async function search() {
  error(); $('search').disabled=true;
  try { const r=await api('/api/context/search','POST',{query:$('query').value.trim(),account:$('account').value}); renderSources(r.items); }
  catch(e) {error(e.message);}
  finally {$('search').disabled=active;}
}
async function prepare(event) {
  event.preventDefault(); error(); preparing=true; locks();
  try {
    brief=await api('/api/briefs','POST',formBody()); snapshot(brief);
    $('brief-feedback').textContent=`Поручение подготовлено. ${brief.facts.length} источника фактов. Проверь инструкции ниже.`;
    $('snapshot').open=true;
    current=null; lastRender=''; renderEmpty(); status('Поручение готово');
    $('call-hint').textContent='Нажми «Начать разговор». Ты играешь собеседника из поручения.';
  } catch(e) {error(e.message); brief=null;}
  finally {preparing=false;locks();}
}
function renderEmpty() {
  $('transcript').replaceChildren(node('p','После начала разговора здесь появятся реплики.','empty'));
  $('answers').replaceChildren(node('p','Ответы ещё не сохранены.','empty')); $('decisions').replaceChildren();
  $('tools').replaceChildren(node('p','Здесь появятся вызовы инструментов.','empty')); $('tool-count').textContent='0';
  $('summary').textContent=''; $('outcome').textContent=outcomeLabels.unresolved; $('review-form').hidden=true;
  $('p50').textContent='—'; $('p95').textContent='—'; $('samples').textContent='0 ответов';
  $('flow-stage').textContent='Этап появится после запуска Flows.';
  $('audio-health').textContent=audioHealthText(null);
  $('task-rating').value=''; $('naturalness').value=''; $('review-notes').value=''; $('review-feedback').textContent='';
}
function renderRecord(record, force=false) {
  current=record;
  const signature=`${record.id}/${record.events.length}/${record.status}/${record.outcome}/${record.review ? JSON.stringify(record.review) : ''}`;
  if(signature===lastRender && !force)return;
  lastRender=signature; status(record.end_reason==='disconnected' ? 'Соединение прервано' : statusLabels[record.status] || record.status,record.status==='active');
  const phase=record.events.filter(e=>e.kind==='flow').at(-1)?.flow;
  const phases={discussion:'Беседа',review:'Подведение итогов',confirmation:'Ожидание ответа на предложение завершить',ending:'Прощание с проверкой новой речи'};
  $('flow-stage').textContent=phase ? `Flows: ${phases[phase.node] || phase.node} · переход ${phase.revision}` : 'Прежний режим Gemini Live';
  // Who was called, on which owner words, and where the number came from (ANYM-288).
  const source=record.destination_source==='owner_named' ? 'номер назвал владелец' : record.destination_source==='agent_found' ? `номер нашёл агент: ${record.destination_evidence}` : 'источник номера не указан';
  const origin=record.owner_instruction ? `Звонок на ${record.brief.destination} · ${source} · слова владельца: «${record.owner_instruction}»` : '';
  $('call-origin').textContent=origin; $('call-origin').hidden=!origin;
  const utterances=record.events.filter(e=>e.kind==='transcript');
  $('transcript').replaceChildren(...utterances.map(e=>{
    const p=node('p',undefined,`utterance ${e.role}`); p.append(node('small',e.role==='user' ? 'Ты играешь собеседника' : `Ассистент${e.interrupted ? ' / прерванная реплика' : ''}`),document.createTextNode(typeof e.text==='string' ? e.text : JSON.stringify(e.text))); return p;
  }));
  if(!utterances.length)$('transcript').append(node('p','Ожидаем первую реплику…','empty'));
  $('transcript').scrollTop=$('transcript').scrollHeight;
  const answers=Object.values(record.answers);
  $('answers').replaceChildren(...answers.map(a=>{
    const d=node('dl',undefined,'answer'); d.append(node('dt',fieldLabels[a.field] || a.field),node('dd',a.value),node('blockquote',a.evidence),node('small',a.evidence_event ? 'Связано с репликой собеседника; требует проверки' : 'Цитата не найдена в расшифровке; проверь вручную')); return d;
  }));
  if(!answers.length)$('answers').append(node('p','Ассистент ещё не вызвал record_answer.','empty'));
  $('outcome').textContent=outcomeLabels[record.outcome] || record.outcome;
  $('summary').textContent=record.summary;
  // Preserve a partially typed owner answer while new conversation events arrive.
  for (const d of record.decisions) {
    let box=document.getElementById(`decision-${d.id}`);
    if(box && box.dataset.status===d.status && box.dataset.callStatus===record.status)continue;
    const replacement=node('div',undefined,'decision'); replacement.id=`decision-${d.id}`; replacement.dataset.status=d.status; replacement.dataset.callStatus=record.status;
    replacement.append(node('h3',`${d.status==='superseded' ? 'Прежний раунд' : d.status==='pending' ? 'Решение владельца' : 'Ответ владельца'}${d.round ? ` · ${d.round}` : ''}`),node('p',d.question));
    for(const [field,value] of Object.entries(d.conditions || {}))replacement.append(node('p',`${fieldLabels[field] || field}: ${value}`));
    if(d.trigger==='required_fields')replacement.append(node('small','Запрос создан сервисом по обязательным полям поручения.'));
    if(d.status==='superseded')replacement.append(node('small','Этот раунд заменён. Его ответ не действует для текущего согласования.'));
    if(d.status==='pending') {
      const options=node('div',undefined,'options');
      for(const value of d.options) { const b=node('button',value); b.onclick=()=>answerDecision(record.id,d.id,value); options.append(b); }
      const form=document.createElement('form'), input=document.createElement('input'); input.placeholder='Свой ответ владельца'; input.maxLength=1000; input.required=true; input.setAttribute('aria-label','Ответ владельца');
      const send=node('button','Передать решение'); send.type='submit';
      form.onsubmit=e=>{e.preventDefault();answerDecision(record.id,d.id,input.value.trim());};
      form.append(input,send); replacement.append(options,form,node('small','Ответ можно дать после завершения разговора.'));
    } else replacement.append(node('p',d.answer || 'Решение не получено.'));
    if(box)box.replaceWith(replacement);else $('decisions').append(replacement);
  }
  const decisions=new Set(record.decisions.map(d=>`decision-${d.id}`));
  [...$('decisions').children].filter(n=>!decisions.has(n.id)).forEach(n=>n.remove());
  const tools=record.events.filter(e=>e.kind==='tool'); $('tool-count').textContent=tools.length;
  $('tools').replaceChildren(...tools.map(e=>{
    const div=node('div',undefined,'tool-event');div.append(node('strong',e.name),node('small',` / ${new Date(e.at).toLocaleTimeString()}`),node('pre',JSON.stringify({arguments:e.arguments,result:e.result},null,2)));return div;
  }));
  if(!tools.length)$('tools').append(node('p','Вызовов инструментов ещё нет.','empty'));
  const terminal=!['waiting_for_browser','connecting','active'].includes(record.status);
  $('review-form').hidden=!terminal;
  if(force) { $('task-rating').value=record.review?.task || ''; $('naturalness').value=record.review?.naturalness || ''; $('review-notes').value=record.review?.notes || ''; $('review-feedback').textContent=''; }
  if(!active && record.metrics)renderTiming(record.metrics);
  if(!active)$('audio-health').textContent=audioHealthText(record.metrics?.audio_health);
  if(terminal)learningUI.analysis().catch(e=>error(e.message));
}
async function answerDecision(callId,decisionId,answer) {
  if(!answer)return;
  const box=document.getElementById(`decision-${decisionId}`);box?.querySelectorAll('button').forEach(b=>b.disabled=true);
  try { await api(`/api/calls/${callId}/decisions/${decisionId}`,'POST',{answer}); await pollRecord(); await loadInbox();await loadHistory(); }
  catch(e) {error(e.message);box?.querySelectorAll('button').forEach(b=>b.disabled=false);}
}
async function loadInbox() {
  const items=(await api('/api/decisions')).items;
  const signature=JSON.stringify(items);
  if(signature===inboxSignature)return;
  inboxSignature=signature;
  const container=$('owner-requests');
  for(const item of items) {
    const d=item.decision, id=`inbox-${d.id}`;
    let existing=document.getElementById(id);
    const itemSignature=JSON.stringify(item);
    if(existing?.dataset.signature===itemSignature)continue;
    const row=node('article',undefined,'decision');row.id=id;row.dataset.signature=itemSignature;
    row.append(node('h3',`${item.counterparty} · ${d.status==='pending'?'Ждёт решения':'Решение сохранено'}`),node('p',d.question));
    for(const [field,value] of Object.entries(d.conditions || {}))row.append(node('p',`${fieldLabels[field] || field}: ${value}`));
    if(d.status==='pending') {
      const options=node('div',undefined,'options');
      for(const value of d.options) { const button=node('button',value);button.onclick=()=>answerDecision(item.call_id,d.id,value);options.append(button); }
      const form=document.createElement('form'),input=document.createElement('input');input.required=true;input.maxLength=1000;input.placeholder='Свой ответ';input.setAttribute('aria-label','Решение по сохранённому вопросу');
      const submit=node('button','Сохранить решение');submit.type='submit';form.append(input,submit);
      form.onsubmit=e=>{e.preventDefault();answerDecision(item.call_id,d.id,input.value.trim());};
      row.append(options,form);
    } else {
      row.append(node('p',d.answer));
      const finished=!['waiting_for_browser','connecting','active'].includes(item.call_status);
      if(finished && item.outcome!=='refused' && item.followup_supported) {
        const button=node('button',item.followup_status && !['waiting_for_browser','connecting','active'].includes(item.followup_status)?'Продолжение завершено':'Подготовить продолжение в браузере');
        button.disabled=active || Boolean(item.followup_status && item.followup_status!=='waiting_for_browser');
        button.onclick=()=>prepareFollowup(item.call_id,d.id);row.append(button);
      }
      if(!item.followup_supported)row.append(node('small','Исторический ответ не привязан к точным условиям. Для продолжения нужно новое поручение.'));
    }
    const open=node('button','Открыть разговор','subtle');open.disabled=active;
    open.onclick=async()=>{try{const saved=await api(`/api/calls/${item.call_id}`);renderRecord(saved,true);snapshot(saved.brief);}catch(e){error(e.message);}};
    row.append(open,node('small',`${statusLabels[item.call_status] || item.call_status} · раунд ${d.round || 1}`));
    if(existing)existing.replaceWith(row);else container.append(row);
  }
  const keep=new Set(items.map(item=>`inbox-${item.decision.id}`));
  [...container.children].filter(n=>!keep.has(n.id)).forEach(n=>n.remove());
  if(!items.length)container.append(node('p','Сохранённых вопросов пока нет.','empty'));
}
async function prepareFollowup(callId,decisionId) {
  if(active)return;
  error();preparing=true;locks();
  try {
    const prepared=await api(`/api/calls/${callId}/decisions/${decisionId}/followup`,'POST');
    brief=prepared;
    for(const [id,key] of [['goal','goal'],['counterparty','counterparty'],['language','language'],['voice','voice'],['conversation-mode','conversation_mode'],['self-reference','self_reference'],['owner-notes','owner_notes'],['supervisor-notes','supervisor_notes']])$(id).value=brief[key] || (key==='conversation_mode' ? 'task' : '');
    $('fields').value=brief.success_fields.join(', ');$('approval-fields').value=brief.approval_fields.join(', ');
    selected.clear();renderSources();snapshot(brief);current=null;lastRender='';renderEmpty();
    $('brief-feedback').textContent='Продолжение подготовлено с сохранённым решением владельца. Начни новую браузерную проверку.';
    $('call-hint').textContent='Ты снова играешь собеседника. Ассистент сообщит сохранённый ответ; это не внешний звонок.';
    status('Продолжение готово');await loadInbox();
  } catch(e) {error(e.message);}
  finally {preparing=false;locks();}
}
function attachTrack(track,participant) {
  if(track.kind!=='audio' || !audioContext || probes.some(p=>p.track.id===track.id))return;
  const local=participant?.local===true;
  const stream=new MediaStream([track]);
  const source=audioContext.createMediaStreamSource(stream);
  const analyser=audioContext.createAnalyser();analyser.fftSize=1024;source.connect(analyser);
  let player=null;
  if(!local) {
    const playback=startRemoteAudio(stream);player=playback.player;
    player.addEventListener('waiting',()=>{playerWaiting++;});
    playback.ready.catch(()=>error('Разреши воспроизведение звука в браузере.'));
  }
  probes.push({track,source,analyser,player,local,samples:new Float32Array(analyser.fftSize),gate:new ActivityGate({threshold:local?.018:.008,attackMs:local?60:30,silenceMs:local?200:350})});
}
function renderTiming(values=summarize(timing.responses.map(r=>r.latencyMs))) {
  $('p50').textContent=ms(values.p50); $('p95').textContent=ms(values.p95); $('samples').textContent=`${values.n || 0} ответов`;
}
function pollAudio() {
  if(!active)return;
  health.tick(performance.now());
  for(const p of probes) {
    p.analyser.getFloatTimeDomainData(p.samples);
    const rms=Math.sqrt(p.samples.reduce((sum,x)=>sum+x*x,0)/p.samples.length);
    $(p.local?'mic-level':'bot-level').value=Math.min(1,rms*9);
    const change=p.gate.sample(rms,performance.now());if(!change)continue;
    if(p.local)change.type==='start' ? timing.userStart(change.at) : timing.userStop(change.at);
    else { change.type==='start' ? timing.botStart(change.at) : timing.botStop(change.at);$('voice-mark').classList.toggle('speaking',change.type==='start'); }
    renderTiming();
  }
}
async function pollHealth() {
  if(healthBusy)return;
  healthBusy=true;
  try {
    // Version-pinned transport hook; absent stats remain unknown.
    await health.sample(transport?.pc);
    $('audio-health').textContent=audioHealthText(health.snapshot());
  } finally {healthBusy=false;}
}
async function pollRecord() {
  if(!current?.id)return;
  const id=current.id;
  const record=await api(`/api/calls/${id}`);
  if(current?.id!==id)return;
  renderRecord(record);
  if(active && !['waiting_for_browser','connecting','active'].includes(record.status))await stop(false);
}
async function start() {
  if(!brief || active)return;
  error(); active=true; stopping=false;timing=new TurnTiming();locks();status('Подключается…');
  health=new AudioHealth();playerWaiting=0;
  $('call-hint').textContent='Разреши микрофон. Ты играешь собеседника из поручения.';
  try {
    audioContext=new AudioContext({latencyHint:'interactive'});await audioContext.resume();
    current=await api('/api/calls/start','POST',{brief_id:brief.id});lastRender='';renderRecord(current,true);
    if(current.status!=='waiting_for_browser')throw new Error('Эта проверка уже завершена. Подготовь новое поручение.');
    transport=new SmallWebRTCTransport({iceServers:[]});
    client=new PipecatClient({transport,enableMic:!microphone.mode,enableCam:false,callbacks:{
      onTrackStarted:attachTrack,
      onBotReady:()=>{if(!active || stopping)return;microphone.start();status('Разговор идёт',true);$('call-hint').textContent='Говори как собеседник. На вопросы владельца можно ответить после завершения.';locks();},
      onServerMessage:message=>{if(message.kind==='error'){error(message.message);stop(false);}else pollRecord().catch(()=>{});},
      onError:()=>{error('Сбой подключения или передачи звука. Подробности сохраняются в диагностике разговора.');stop();},
      onDisconnected:()=>{if(active && !stopping)stop(false);},
    }});
    locks();audioPoller=setInterval(pollAudio,20);
    healthPoller=setInterval(pollHealth,500);
    poller=setInterval(()=>pollRecord().catch(()=>error('Потеряно соединение с локальным сервисом.')),800);
    await client.connect({webrtcRequestParams:{endpoint:`${location.origin}/api/offer`,headers:new Headers({'X-Voice-Service':'1'}),requestData:{call_id:current.id}}});
  } catch(e) {
    error(e.name==='NotAllowedError' ? 'Разреши доступ к микрофону в браузере и попробуй снова.' : e.message || 'Подключение не удалось.');
    await stop();
  }
}
async function stop(requestStop=true) {
  if(stopping || !active)return;
  stopping=true; active=false;microphone.stop();clearInterval(poller);clearInterval(audioPoller);
  clearInterval(healthPoller);
  // Collect while the peer still exists, before server stop or client.disconnect.
  await pollHealth();
  const audioHealth={...health.snapshot(),audio_context_state:audioContext?.state ?? null,player_waiting_events:playerWaiting};
  const id=current?.id;
  try {if(id && requestStop)await api(`/api/calls/${id}/stop`,'POST');}catch(e){error(e.message);}
  try {await client?.disconnect();}catch{}
  for(const p of probes){p.source.disconnect();p.analyser.disconnect();stopRemoteAudio(p.player);}probes=[];
  if(audioContext)await audioContext.close();audioContext=null;client=null;transport=null;
  $('voice-mark').classList.remove('speaking');$('mic-level').value=0;$('bot-level').value=0;$('mute').textContent='Выключить микрофон';
  try {
    if(id && !requestStop)await settleDisconnectedCall(api,id);
    if(id)renderRecord(await api(`/api/calls/${id}/result`,'POST',{metrics:{...summarize(timing.responses.map(r=>r.latencyMs)),interruption_stop_ms:timing.interruptions,method:'browser_audio_rms',audio_health:audioHealth}}),true);
    await refreshConfig();await loadHistory();await loadInbox();
  }catch(e){error(e.message);}
  brief=null;stopping=false;locks();
  $('brief-feedback').textContent='Проверка сохранена. Для повторения подготовь поручение заново.';
  $('call-hint').textContent='Разговор завершён. Проверь ответы, цитаты и вызовы инструментов.';
}
async function loadHistory() {
  const records=(await api('/api/calls')).items;
  $('history').replaceChildren(...records.map(r=>{
    const row=node('div',undefined,'history-row'),b=node('button',r.brief.goal);b.disabled=active;
    b.onclick=async()=>{
      if(active)return;
      try {const record=await api(`/api/calls/${r.id}`);renderRecord(record,true);snapshot(record.brief);$('call-hint').textContent='Открыта сохранённая проверка. Поручение слева относится к следующему разговору.';}catch(e){error(e.message);}
    };
    row.append(b,node('span',`${r.brief.language} / ${r.brief.voice}`),node('span',outcomeLabels[r.outcome] || r.outcome),node('span',new Date(r.started_at).toLocaleString(),'date'));return row;
  }));
  if(!records.length)$('history').append(node('p','После первого разговора здесь появится запись.','empty'));
}
$('keys-toggle').onclick=()=>{$('keys-panel').hidden=!$('keys-panel').hidden;if(!$('keys-panel').hidden)$('gemini-key').focus();};
$('keys-form').onsubmit=async e=>{
  e.preventDefault();error();
  try {await api('/api/credentials','POST',{[$('key-provider').value]:$('gemini-key').value});$('gemini-key').value='';$('key-feedback').textContent='Ключ сохранён. Доступ к модели проверяется при разговоре.';await refreshConfig();}
  catch(e){error(e.message);}
};
$('forget-key').onclick=async()=>{if(active)await stop();try{await api('/api/credentials','DELETE');$('gemini-key').value='';$('key-feedback').textContent='Ключи удалены.';await refreshConfig();}catch(e){error(e.message);}};
$('brief-form').onsubmit=prepare;
for(const id of ['goal','counterparty','language','voice','grok-voice','conversation-mode','dialog-engine','self-reference','owner-notes','supervisor-notes','fields','approval-fields'])$(id).addEventListener('input',invalidate);
$('dialog-engine').addEventListener('change',()=>{providerUI();$('key-provider').value=$('dialog-engine').value==='grok_realtime'?'xai':'gemini';});
$('inbox-refresh').onclick=()=>loadInbox().catch(e=>error(e.message));
$('search').onclick=search;
$('query').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();search();}};
$('start').onclick=start;$('stop').onclick=stop;
$('mute').onclick=()=>microphone.toggle();
$('push-to-talk').onchange=()=>microphone.setMode($('push-to-talk').checked);
$('review-form').onsubmit=async e=>{
  e.preventDefault();if(!current || active)return;
  try {renderRecord(await api(`/api/calls/${current.id}/result`,'POST',{review:{task:$('task-rating').value,naturalness:Number($('naturalness').value),notes:$('review-notes').value.trim()}}));$('review-feedback').textContent='Оценка сохранена.';await loadHistory();}
  catch(e){error(e.message);}
};
try {
  await refreshConfig();await loadHistory();await loadInbox();await learningUI.refresh();
  if(config.active_call_id) {
    current=await api(`/api/calls/${config.active_call_id}`);renderRecord(current,true);snapshot(current.brief);
    if(current.status==='waiting_for_browser') {
      brief=current.brief;
      for(const [id,key] of [['goal','goal'],['counterparty','counterparty'],['language','language'],['voice','voice'],['conversation-mode','conversation_mode'],['self-reference','self_reference'],['owner-notes','owner_notes'],['supervisor-notes','supervisor_notes']])$(id).value=brief[key] || (key==='conversation_mode' ? 'task' : '');
      $('fields').value=brief.success_fields.join(', ');
      $('approval-fields').value=(brief.approval_fields || []).join(', ');
      for(const f of brief.facts.filter(f=>f.source_ref!=='owner_notes'))selected.set(f.source_ref,{id:f.source_ref,snippet:f.text,account:f.account,chat:f.chat,sent_at:f.sent_at});
      renderSources();$('snapshot').open=true;
      $('brief-feedback').textContent='Сохранённое поручение ожидает браузер. Проверь контекст перед запуском.';
      $('call-hint').textContent='Поручение подготовлено через сервис. Подключи ключ Gemini и начни разговор.';locks();
    } else {
      active=true;locks();$('mute').disabled=true;status('Незавершённая проверка');
      $('call-hint').textContent='После обновления страницы останови предыдущую проверку и подготовь новую.';
    }
  }
}catch(e){error(e.message);}
