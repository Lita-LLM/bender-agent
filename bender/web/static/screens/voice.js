// Аудио только по выбору хозяина; дорожки и ObjectURL живут до смены результата/экрана.
import {html,useEffect,useRef,useState,Button,ErrorBox} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
import {useApp,useEvents,setState} from '../lib/store.js';
function useSpeech(){
 const [result,setResult]=useState(null),[error,setError]=useState(null),[busy,setBusy]=useState(false);
 useEffect(()=>()=>{if(result?.url)URL.revokeObjectURL(result.url);},[result]);
 const speak=async text=>{setBusy(true);setError(null);try{
  const data=await post('/api/voice/speech',{text});
  const raw=Uint8Array.from(atob(data.audio),c=>c.charCodeAt(0));
  setResult({...data,url:URL.createObjectURL(new Blob([raw],{type:data.mime}))});
 }catch(e){setError(e);}finally{setBusy(false);}};
 return {speak,result,error,busy};
}
function AudioResult({speech}){return html`${speech.result?html`<audio controls autoplay src=${speech.result.url} aria-label="Озвученный ответ"></audio><p class="faint">${speech.result.notice}</p>`:null}<${ErrorBox} error=${speech.error}/>`;}
async function asWav(blob){
 const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)throw Error('Браузер не умеет готовить WAV. Выключи «Микрофон в WAV» или используй Chromium.');
 const context=new Audio({sampleRate:16000});let sound;
 try{sound=await context.decodeAudioData(await blob.arrayBuffer());}finally{await context.close();}
 const buffer=new ArrayBuffer(44+sound.length*2),view=new DataView(buffer),write=(offset,text)=>{for(let i=0;i<text.length;i++)view.setUint8(offset+i,text.charCodeAt(i));};
 write(0,'RIFF');view.setUint32(4,buffer.byteLength-8,true);write(8,'WAVE');write(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
 view.setUint32(24,sound.sampleRate,true);view.setUint32(28,sound.sampleRate*2,true);view.setUint16(32,2,true);view.setUint16(34,16,true);write(36,'data');view.setUint32(40,sound.length*2,true);
 const channels=Array.from({length:sound.numberOfChannels},(_,i)=>sound.getChannelData(i));
 for(let i=0;i<sound.length;i++){const sample=channels.reduce((sum,c)=>sum+c[i],0)/channels.length;view.setInt16(44+i*2,Math.round(Math.max(-1,Math.min(1,sample))*32767),true);}
 return new Blob([buffer],{type:'audio/wav'});
}
export function SpeechButton({text}){
 const {st}=useApp(),speech=useSpeech();
 if(!st?.voice?.enabled||!st?.voice?.tts)return null;
 return html`<div class="speech"><${Button} small busy=${speech.busy} onClick=${()=>speech.speak(text)}>Озвучить</${Button}><${AudioResult} speech=${speech}/></div>`;
}
export function VoiceControls({onTranscript,onAttachment,sid}){
 const {st}=useApp(),speech=useSpeech(),record=useRef(null),[recording,setRecording]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(null),seen=useRef(new Set());
 const generation=useRef(0);
 useEffect(()=>()=>{generation.current++;if(record.current){const r=record.current;r.recorder.onstop=null;clearTimeout(r.timer);r.recorder.stop();r.stream.getTracks().forEach(t=>t.stop());record.current=null;setState({face:'idle'});}},[sid]);
 useEvents(ev=>{
  if(ev.type==='turn_end'&&ev.session===sid&&st?.voice?.enabled&&st?.voice?.auto_speak&&!ev.result?.error){
   get(`/api/session/${sid}/messages?limit=20`).then(d=>{
    const last=[...d.messages].reverse().find(m=>m.role==='assistant'&&typeof m.content==='string'&&m.content.trim());
    if(last&&!seen.current.has(last.id)){seen.current.add(last.id);speech.speak(last.content);}
   }).catch(setError);
  }
 },[sid,st?.voice]);
 if(!st?.voice?.enabled)return null;
 const mic=async()=>{
  setError(null);
  if(record.current){record.current.recorder.stop();return;}
  try{
   if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)throw Error('Запись недоступна в этом браузере. Открой пульт на localhost или по HTTPS и разреши микрофон.');
   const stream=await navigator.mediaDevices.getUserMedia({audio:true});
   const mime=['audio/webm;codecs=opus','audio/ogg;codecs=opus','audio/mp4'].find(v=>MediaRecorder.isTypeSupported(v));
   if(!mime){stream.getTracks().forEach(t=>t.stop());throw Error('Нет поддерживаемого формата записи. Попробуй Chromium или пришли текст.');}
   const recorder=new MediaRecorder(stream,{mimeType:mime}),parts=[],start=Date.now(),current=++generation.current;
   recorder.ondataavailable=e=>{if(e.data.size)parts.push(e.data);};
   recorder.onstop=async()=>{
    if(record.current)clearTimeout(record.current.timer);stream.getTracks().forEach(t=>t.stop());record.current=null;setRecording(false);setState({face:'idle'});setBusy(true);
    try{
     const blob=st.voice.capture_wav?await asWav(new Blob(parts,{type:mime})):new Blob(parts,{type:mime});
     const audio=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=reject;reader.onload=()=>resolve(reader.result.split(',')[1]);reader.readAsDataURL(blob);});
     const result=await post('/api/voice/transcribe',{audio,seconds:(Date.now()-start)/1000,session:sid,ext:st.voice.capture_wav?'wav':mime.includes('ogg')?'ogg':mime.includes('mp4')?'m4a':'webm'});
     if(current!==generation.current)return;
     onTranscript(result.text||'Голосовое сообщение.');
     if(result.attachment&&onAttachment)onAttachment(result.attachment);
    }catch(e){setError(e);}finally{setBusy(false);}
   };
   recorder.start();record.current={recorder,stream,timer:setTimeout(()=>{if(recorder.state!=='inactive')recorder.stop();},600000)};setRecording(true);setState({face:'listening'});
  }catch(e){setError(e);}
 };
 return html`<div class="voice-controls">${st.voice.stt?html`<${Button} small busy=${busy} kind=${recording?'danger':''} onClick=${mic}>${recording?'Закончить запись':'Микрофон'}</${Button}>`:null}<a href="#/settings">Настройки голоса</a><${ErrorBox} error=${error}/><${AudioResult} speech=${speech}/></div>`;
}
