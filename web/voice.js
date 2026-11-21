const $ = (id) => document.getElementById(id);
const state = { session: null, rtc: null, mic: null, rtm: null, key: "", seen: new Set() };
const header = () => ({"Content-Type":"application/json", ...(state.key ? {"X-API-Key":state.key} : {})});
const setState = (text, live=false) => { $("connection").textContent=text; $("dot").classList.toggle("live",live); };
const showError = (e) => { $("error").textContent = e?.message || String(e); };
const addMessage = (role,text) => { const empty=document.querySelector(".empty"); if(empty) empty.remove(); const box=document.createElement("div"); box.className=`bubble ${role}`; const who=document.createElement("span"); who.className="role"; who.textContent=role.toUpperCase(); box.append(who,document.createTextNode(text)); $("messages").append(box); $("messages").scrollTop=$("messages").scrollHeight; };
const api = async (url, options={}) => { const response=await fetch(url,{...options,headers:{...header(),...(options.headers||{})}}); let body; try{body=await response.json()}catch{body={}} if(!response.ok) throw new Error(body.detail || `Request failed (${response.status})`); return body; };

async function connectRtc(info) {
  const [{default:AgoraRTC},{default:AgoraRTM}] = await Promise.all([
    import("https://esm.sh/agora-rtc-sdk-ng@4.24.10"),
    import("https://esm.sh/agora-rtm-sdk@2.2.4")
  ]);
  const {RTM}=AgoraRTM;
  const rtc=AgoraRTC.createClient({mode:"rtc",codec:"vp8"});
  rtc.on("user-published",async(user,type)=>{if(type==="audio"){await rtc.subscribe(user,type);user.audioTrack?.play();$("activity").textContent="Speaking";}});
  rtc.on("user-unpublished",()=>$("activity").textContent="Listening");
  rtc.on("connection-state-change",(current,previous,reason)=>{setState(current==="RECONNECTING"?"Reconnecting…":`Agora ${current.toLowerCase()}`,current==="CONNECTED");if(current==="RECONNECTING")$("activity").textContent="Reconnecting";if(current==="CONNECTED"){$("activity").textContent="Listening";$("error").textContent="";}if(current==="DISCONNECTED"&&previous==="CONNECTED")showError(`Voice connection lost${reason?`: ${reason}`:""}. The Agora SDK will try to reconnect; if it does not recover, end and restart the session.`);});
  await rtc.join(info.app_id,info.channel,info.rtc_token,info.uid);
  state.mic=await AgoraRTC.createMicrophoneAudioTrack();
  await rtc.publish([state.mic]); state.rtc=rtc;
  const rtm=new RTM(info.app_id,String(info.uid));
  rtm.addEventListener("message",async(event)=>{
    try { const raw=typeof event.message==="string"?event.message:new TextDecoder().decode(event.message);const msg=JSON.parse(raw);if(msg.type!=="transcript"||!msg.text)return;const role=msg.role==="user"?"user":"agent";const signature=`${role}:${msg.text}:${Boolean(msg.final)}`;if(state.seen.has(signature))return;state.seen.add(signature);addMessage(role,msg.text);if(role==="user")$("activity").textContent="Processing";else $("activity").textContent="Listening";if(msg.final){api(`/api/voice/transcript/${state.session.session_id}`,{method:"POST",body:JSON.stringify({lines:[{role,text:msg.text,final:true}]})}).catch(showError);if(role==="user")api("/api/voice/evidence",{method:"POST",body:JSON.stringify({query:msg.text})}).then(renderEvidence).catch(showError);if(role==="agent")loadActions();}}
    catch { /* Ignore non-transcript Agora signaling messages. */ }
  });
  await rtm.login({token:info.rtm_token}); await rtm.subscribe(info.channel); state.rtm=rtm;
}

async function start() {
  state.key=$("api-key").value.trim(); showError(""); $("activity").textContent="Starting";setState("Requesting microphone",false);
  try {
    // Request permission before spending a session start on the provider.
    if(!navigator.mediaDevices?.getUserMedia)throw new Error("Microphone access requires HTTPS or localhost in a supported browser.");
    const permission=await navigator.mediaDevices.getUserMedia({audio:true});permission.getTracks().forEach(track=>track.stop());$("mic").textContent="Microphone: allowed";
    const info=await api("/api/voice/session/start",{method:"POST",body:JSON.stringify({user_id:"browser-user"})});state.session=info.session;
    await connectRtc(info);$("start").disabled=true;$("stop").disabled=false;$("stop").textContent="End conversation";setState("Connected • listening",true);$("activity").textContent="Listening";
  } catch(e) { showError(e.name==="NotAllowedError"?"Microphone permission was denied. Allow microphone access in your browser settings and try again.":e.message);setState("Could not connect");$("activity").textContent="Unavailable";if(state.session)await stop(); }
}

async function stop() {
  const current=state.session;$("activity").textContent="Ending";let stopFailed=false;
  try{if(state.rtm){await state.rtm.unsubscribe(current.channel);await state.rtm.logout();}}catch{}
  try{state.mic?.stop();state.mic?.close();if(state.rtc){await state.rtc.leave();}}catch{}
  state.rtm=null;state.rtc=null;state.mic=null;
  if(current){try{await api(`/api/voice/session/${current.session_id}/stop`,{method:"POST"});}catch(e){showError(e.message);stopFailed=true;}}
  state.session=stopFailed?current:null;$("start").disabled=stopFailed;$("stop").disabled=!stopFailed;$("stop").textContent=stopFailed?"Retry ending session":"End conversation";$("mic").textContent="Microphone: off";setState(stopFailed?"Session end failed":"Disconnected");$("activity").textContent=stopFailed?"Needs retry":"Ready";
}

async function sendQuestion(text) {
  if(!text.trim())return; addMessage("user",text);$("activity").textContent="Processing";
  try{const result=await api("/api/voice/message",{method:"POST",body:JSON.stringify({message:text,session_id:state.session?.session_id||null})});addMessage("agent",result.answer);const evidence=await api("/api/voice/evidence",{method:"POST",body:JSON.stringify({query:text})});renderEvidence(evidence);$("activity").textContent="Ready";await loadActions();
    // Agora owns audio for microphone turns. Text fallback uses browser speech only.
    if(!fromVoice&&"speechSynthesis" in window){speechSynthesis.cancel();speechSynthesis.speak(new SpeechSynthesisUtterance(result.answer));}
  }catch(e){showError(e.message);$("activity").textContent="Error";}
}

function renderEvidence(evidence) {
  const root=$("evidence-content");root.replaceChildren();
  if(!evidence||evidence.retrieval_status==="insufficient_evidence"){root.textContent="Insufficient evidence. RecallLoop did not find a matching source record.";return;}
  const quote=document.createElement("div");quote.className="quote";quote.textContent=evidence.answer||"Evidence record retrieved.";root.append(quote);
  const source=document.createElement("div");source.className="meta";source.textContent=`Memory ${evidence.matching_memory_id||"unknown"} · ${evidence.source_timestamp||"timestamp unavailable"} · confidence ${evidence.evidence_confidence||"unknown"}`;root.append(source);
  if(evidence.commitment){const commitment=document.createElement("div");commitment.className="meta";const due=evidence.commitment.due_at?` · due ${evidence.commitment.due_at}`:" · no resolved deadline";const actor=evidence.commitment.actor?` · owner ${evidence.commitment.actor} (${evidence.commitment.actor_confidence})`:" · unassigned";commitment.textContent=`Commitment: ${evidence.commitment.status}${due}${actor}`;root.append(commitment);}
  if(evidence.source_transcript){const transcript=document.createElement("details");const summary=document.createElement("summary");summary.textContent="Original transcript";const content=document.createElement("p");content.textContent=evidence.source_transcript;transcript.append(summary,content);root.append(transcript);}
}

async function loadActions(){try{const items=await api("/actions?status=awaiting_approval");const root=$("actions");root.replaceChildren();if(!items.length){root.textContent="No pending actions.";root.className="actions muted";return;}root.className="actions";for(const item of items){const card=document.createElement("div");card.className="action-card";const title=document.createElement("div");title.textContent=`${item.description} · ${item.action_type} · ${item.status}`;card.append(title);const details=document.createElement("pre");details.className="action-details";details.textContent=JSON.stringify({target:item.target,parameters:item.parameters,commitment_id:item.commitment_id,evidence_references:item.evidence_references},null,2);card.append(details);const note=document.createElement("div");note.className="meta";note.textContent="Approval below will execute these exact parameters through the configured tool.";card.append(note);const buttons=document.createElement("div");buttons.className="action-buttons";const approve=document.createElement("button");approve.className="approve";approve.textContent="Approve & execute";approve.onclick=async()=>{try{await api(`/actions/${item.action_id}/approve`,{method:"POST",body:JSON.stringify({approved_by:"browser-user"})});const result=await api(`/actions/${item.action_id}/execute`,{method:"POST"});addMessage("agent",`Action ${result.status}. This does not verify the physical work.`);await loadActions();}catch(e){showError(e.message);}};const reject=document.createElement("button");reject.className="reject";reject.textContent="Reject";reject.onclick=async()=>{try{await api(`/api/voice/action/${item.action_id}/reject`,{method:"POST",body:JSON.stringify({cancelled_by:"browser-user",reason:"Rejected by user"})});await loadActions();}catch(e){showError(e.message);}};buttons.append(approve,reject);card.append(buttons);root.append(card);}}catch(e){showError(e.message);}}

$("start").onclick=start;$("stop").onclick=stop;$("refresh-actions").onclick=loadActions;
$("message-form").addEventListener("submit",async(event)=>{event.preventDefault();const input=$("message");const value=input.value.trim();input.value="";await sendQuestion(value);});
