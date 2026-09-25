'use strict';
let token = sessionStorage.getItem('doorbell-token') || '';
let data, settings, dirty = false, busy = false, renames = {};
const $ = id => document.getElementById(id);
const node = (tag, text, cls) => {const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(cls)el.className=cls; return el;};
function notice(message, error=false){$('notice').hidden=false;$('notice').textContent=message;$('notice').className=error?'error':'';}
function changed(){dirty=true;$('save-status').textContent='Unsaved changes';}
async function api(path, options={}){
  const response=await fetch(path,{...options,headers:{'X-Doorbell-Token':token,'X-Doorbell-Admin':'1',...(options.headers||{})}});
  const body=await response.json().catch(()=>({detail:'Unexpected server response'}));
  if(!response.ok){if(response.status===401){$('login').hidden=false;$('workspace').hidden=true;$('connection').textContent='Locked';}throw Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail));}
  return body;
}
async function action(button, fn){if(busy)return;busy=true;button.disabled=true;try{await fn();}catch(e){notice(e.message,true);}finally{busy=false;button.disabled=false;}}
function field(label,value,type,onchange, options={}){
  const wrap=node('label',label);let input;
  if(type==='select'){input=node('select');for(const [v,t] of options.choices){const op=node('option',t);op.value=v;input.append(op);}}
  else{input=node('input');input.type=type;}
  if(type==='checkbox'){input.checked=!!value;wrap.className='toggle';}else input.value=value??'';
  for(const k of ['min','max','step','placeholder'])if(options[k]!==undefined)input[k]=options[k];
  input.addEventListener('input',()=>{onchange(type==='checkbox'?input.checked:type==='number'?(input.value===''?null:Number(input.value)):input.value);changed();});
  wrap.append(input);return wrap;
}
function button(text, fn, cls){const b=node('button',text,cls);b.type='button';b.onclick=()=>action(b,fn);return b;}
function soundSelect(file, onselect){return field('Chime sound',file,'select',onselect,{choices:[['','Choose a sound'],...data.audio.map(a=>[a.file,a.file+(a.error?' · unreadable':` · ${a.duration_seconds.toFixed(2)}s`)])]});}
function durationFor(file){return data.audio.find(a=>a.file===file)?.duration_seconds||1;}
function configuredBells(){return [['default',settings.chime],...Object.entries(settings.doorbells)];}
function renderBells(){
  const list=$('bell-list');list.replaceChildren();
  configuredBells().forEach(([name,bell],index)=>{
    const card=node('div',undefined,'bell'),left=node('div'),right=node('div'),heading=node('div',undefined,'bell-heading');
    heading.append(node('span',String(index+1).padStart(2,'0'),'number'),node('h3',name==='default'?'Default doorbell':name));left.append(heading);
    if(name!=='default')left.append(field('Webhook name',renames[name]??name,'text',v=>{renames[name]=v;},{placeholder:'back-door'}));
    card.dataset.bell=name;
    right.append(soundSelect(bell.file,v=>{bell.file=v;bell.duration_seconds=durationFor(v);right.querySelector('.timing').textContent=`${durationFor(v).toFixed(2)} seconds · measured automatically`;}));
    right.append(node('p',`${durationFor(bell.file).toFixed(2)} seconds · measured automatically`,'hint timing'));
    right.append(field('Extra time after sound (seconds)',bell.tail_seconds??settings.chime.tail_seconds,'number',v=>bell.tail_seconds=v,{min:0,max:10,step:.1}));
    const tools=node('div',undefined,'bell-tools');
    tools.append(button('Test on speakers',async()=>{if(dirty)throw Error('Save your changes before testing on speakers.');const r=await api('/test/chime?doorbell='+encodeURIComponent(name),{method:'POST'});showTestResult(r);}));
    if(name!=='default')tools.append(button('Remove',async()=>{delete settings.doorbells[name];delete renames[name];changed();renderBells();},'danger'));left.append(tools);
    const url=node('div',undefined,'bell-url'),path=name==='default'?'/doorbell':'/doorbell/'+encodeURIComponent(name),text=data.base_url+path;
    url.append(node('code',text+'?token=••••••'),button('Copy webhook',async()=>{if(dirty)throw Error('Save your changes before copying the webhook.');await copyText(text+'?token='+encodeURIComponent(token));notice('Webhook URL copied, including your token.');}));
    card.append(left,right,url);list.append(card);
  });
}
function renderRooms(){
  $('room-count').textContent=data.rooms.length;$('room-list').replaceChildren();
  if(!data.rooms.length)$('room-list').append(node('div','No speakers found yet. Discovery keeps running. Try “Find speakers” or check the subnet in Network & discovery.','empty'));
  for(const room of data.rooms){
    const prefs=settings.room_preferences[room.id]||{name:room.name,enabled:room.enabled,chime_volume:room.chime_volume,chime_when_idle:room.chime_when_idle,chime_when_muted:room.chime_when_muted};
    settings.room_preferences[room.id]=prefs;
    const card=node('div',undefined,'room-card'+(!room.available?' inactive':'')),top=node('div',undefined,'room-top');
    top.append(node('h3',room.name),field('Enabled',prefs.enabled,'checkbox',v=>prefs.enabled=v));card.append(top,node('small',room.available?`${room.host} · ${room.detail||'BluOS player'}`:'Not currently discovered · preferences retained'));
    card.append(field('Chime volume · 0–100',prefs.chime_volume,'number',v=>prefs.chime_volume=v,{min:0,max:100,placeholder:`Default (${settings.chime.default_volume})`}),field('Chime when idle',prefs.chime_when_idle,'checkbox',v=>prefs.chime_when_idle=v));
    // Retain existing mute behavior; the service cannot reliably unmute every model.
    card.append(field('Allow chime while muted',prefs.chime_when_muted,'checkbox',v=>prefs.chime_when_muted=v));
    card.append(node('small','Muted playback depends on the player’s mute behavior.'));
    card.append(button('Test this room',async()=>{if(dirty)throw Error('Save your changes before testing.');if(!prefs.enabled||!room.available)throw Error('Enable and save this room before testing.');const r=await api('/test/chime?zone='+encodeURIComponent(room.name),{method:'POST'});showTestResult(r);}));
    $('room-list').append(card);
  }
}
const descriptors={
 'general-fields':[['Default chime volume','chime.default_volume','number',0,100],['Fade duration (milliseconds)','behaviour.fade_ms','number',0,5000],['Repeat suppression (seconds)','behaviour.debounce_seconds','number',0,300]],
 'playback-fields':[['Fade steps','behaviour.fade_steps','number',1,100],['Saved state expiry (seconds)','behaviour.state_ttl_seconds','number',10,3600],['Player request timeout (seconds)','behaviour.http_timeout_seconds','number',.1,60],['Restore paused playback','behaviour.restore_pause_state','checkbox'],['When a group’s primary is not selected','behaviour.group_policy','select',null,null,[['primary','Play through the whole group'],['skip','Skip that group']]]],
 'network-fields':[['Player-facing service URL (blank = automatic)','service_base_url','text'],['Discovery subnet (blank = automatic)','discovery.subnet','text'],['Refresh interval (seconds)','discovery.refresh_seconds','number',30,86400],['Full scan every N refreshes (0 = only when needed)','discovery.full_sweep_every','number',0,1000],['Listen for speaker announcements','discovery.listen','checkbox'],['Listening address · restart required','listen_host','text'],['Listening port · restart required','listen_port','number',1,65535],['Log level','log_level','select',null,null,['DEBUG','INFO','WARNING','ERROR','CRITICAL'].map(v=>[v,v])]],
 'security-fields':[['Allowed doorbell devices (comma separated; blank = any)','webhook.allowed_devices','list']]
};
function renderSettings(){for(const [id,fields] of Object.entries(descriptors)){$(id).replaceChildren();for(const [label,path,type,min,max,choices] of fields){const parts=path.split('.'),key=parts.pop();let obj=settings;for(const p of parts)obj=obj[p];$(id).append(field(label,type==='list'?obj[key].join(', '):obj[key],type==='list'?'text':type,v=>obj[key]=type==='list'?v.split(',').map(s=>s.trim()).filter(Boolean):v,{min,max,choices,step:path.includes('seconds')?.1:1}));}}}
function renderAudio(){
 $('audio-list').replaceChildren();
 if(!data.audio.length)$('audio-list').append(node('p','Upload your first MP3 to get started.','empty'));
 for(const sound of data.audio){const row=node('div',undefined,'audio-row'),info=node('div',undefined,'audio-info');info.append(node('strong',sound.file),node('small',sound.error||`${sound.duration_seconds.toFixed(2)} seconds · ${(sound.bytes/1024).toFixed(0)} KB`));row.append(info);
 if(!sound.error){const audio=node('audio');audio.controls=true;audio.preload='none';audio.src='/chimes/'+encodeURIComponent(sound.file);audio.setAttribute('aria-label','Preview '+sound.file);row.append(audio);}
 row.append(button('Delete',async()=>{if(configuredBells().some(([,b])=>b.file===sound.file))throw Error('Choose another sound for this doorbell before deleting it.');await api('/api/chimes/'+encodeURIComponent(sound.file),{method:'DELETE'});data.audio=data.audio.filter(a=>a.file!==sound.file);renderAudio();renderBells();notice('Sound deleted.');},'danger'));$('audio-list').append(row);}
}
function render(next){data=next;settings=structuredClone(data.settings);renames={};dirty=false;$('workspace').hidden=false;$('login').hidden=true;$('logout').hidden=false;$('connection').textContent='Service connected';$('new-rooms').checked=settings.new_room_enabled;$('new-token').value='';$('save-status').textContent='All changes saved';renderRooms();renderBells();renderAudio();renderSettings();$('service-status').textContent=JSON.stringify({build:data.build,last_ring:data.last_result},null,2);$('discovery-status').textContent=data.discovery?.last_error||`Discovery is ${data.discovery?.listening?'listening for announcements':'using periodic scans'}. ${data.discovery?.network||'Network determined automatically.'}`;if(data.token_warning)notice('Your token is blank or still the starter value. Set a new token under Access & webhook security.');if(data.restart_required)notice('Saved. Restart the container to apply the new listening address or port.');}
async function load(){render(await api('/api/settings'));}
$('login-form').onsubmit=async event=>{event.preventDefault();token=$('login-token').value;try{await load();if(!data.token_warning&&!data.restart_required)$('notice').hidden=true;sessionStorage.setItem('doorbell-token',token);$('login-token').value='';}catch(e){notice(e.message,true);}};
$('logout').onclick=()=>{token='';sessionStorage.removeItem('doorbell-token');location.reload();};
$('new-rooms').onchange=()=>{settings.new_room_enabled=$('new-rooms').checked;changed();};
$('new-token').oninput=changed;
$('generate-token').onclick=()=>{const bytes=crypto.getRandomValues(new Uint8Array(24));$('new-token').value=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');changed();notice('New token generated. Save, then copy the updated webhook URLs.');};
$('add-doorbell').onclick=()=>{let name='back';let i=2;while(settings.doorbells[name])name='door-'+i++;settings.doorbells[name]={file:settings.chime.file,duration_seconds:settings.chime.duration_seconds,tail_seconds:null};changed();renderBells();};
$('scan').onclick=()=>action($('scan'),async()=>{notice('Looking for speakers. A full scan can take a minute or two.');const next=await api('/api/discovery/refresh',{method:'POST'});data.rooms=next.rooms;data.discovery=next.discovery;renderRooms();$('discovery-status').textContent=next.discovery?.last_error||`Last scanned ${next.discovery?.network||'local network'}`;notice(`Discovery finished. ${next.rooms.filter(r=>r.available).length} speakers found.`);});
$('upload').onchange=()=>action($('upload'),async()=>{const file=$('upload').files[0];if(!file)return;if(file.size>12*1024*1024)throw Error('Choose an MP3 smaller than 12 MB.');notice('Uploading and measuring your chime…');const sound=await api('/api/chimes?filename='+encodeURIComponent(file.name),{method:'POST',body:file,headers:{'Content-Type':'audio/mpeg'}});data.audio.push(sound);renderAudio();renderBells();$('upload').value='';notice(`Uploaded: ${sound.duration_seconds.toFixed(2)} seconds. Select it for a doorbell above, then save.`);});
$('save').onclick=()=>action($('save'),async()=>{
 const bells={};for(const card of $('bell-list').children){const old=card.dataset.bell;if(old==='default')continue;const name=(renames[old]??old).trim().toLowerCase();if(!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name)||name==='default')throw Error('Doorbell names must use 1–32 letters, numbers, hyphens or underscores. “default” is reserved.');if(bells[name])throw Error('Each doorbell needs a unique name.');bells[name]=settings.doorbells[old];}
 const draft=structuredClone(settings);draft.doorbells=bells;
 if(!Array.from(document.querySelectorAll('#workspace input')).every(input=>input.checkValidity()))throw Error('Check the highlighted number fields and try again.');
 const body={revision:data.revision,settings:draft};const newToken=$('new-token').value;if(newToken)body.new_token=newToken;
 const next=await api('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(newToken){token=newToken;sessionStorage.setItem('doorbell-token',token);}notice('Settings saved. Your room and chime choices are active.');render(next);
});
$('reload').onclick=()=>action($('reload'),async()=>{await load();notice('Loaded saved settings.');});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
load().catch(e=>{if(!$('login').hidden)notice('Enter your token to continue.');else notice(e.message,true);});

async function copyText(text){
 if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return;}
 const box=node('textarea');box.value=text;box.style.position='fixed';box.style.opacity='0';document.body.append(box);box.select();
 const copied=document.execCommand('copy');box.remove();if(!copied)throw Error('Your browser could not copy the URL. Enable clipboard access or use HTTPS.');
}
setInterval(async()=>{
 if(!data||busy||dirty||!$('login').hidden)return;
 try{const next=await api('/api/settings');
  if(next.revision!==data.revision){notice('Settings changed in another window. Reload saved settings to see them.');return;}
  if(JSON.stringify(next.rooms.map(({last_seen_seconds,...r})=>r))!==JSON.stringify(data.rooms.map(({last_seen_seconds,...r})=>r))){data.rooms=next.rooms;renderRooms();}
  $('service-status').textContent=JSON.stringify({build:next.build,last_ring:next.last_result},null,2);
 }catch(e){notice(e.message,true);}
},15000);

function showTestResult(result){
 const failed=result.status==='error'||Boolean(result.errors?.length);
 const label=result.status==='chimed'?'Playback command sent':result.status;
 const details=[...(result.errors||[]),...(result.skipped||[]),...(result.notes||[])];
 notice(`${label}: ${(result.groups||[]).join(', ')||'no rooms'}${details.length?' — '+details.join('; '):''}`,failed);
}
