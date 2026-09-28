import streamlit as st
import requests
from openai import OpenAI
import os, json, sqlite3, uuid, hashlib, html, base64, io
from datetime import datetime
import streamlit.components.v1 as components

st.set_page_config(page_title="Interview Coach Pro", page_icon="🎓", layout="wide")
DB="interview_coach.db"

def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS rooms(code TEXT PRIMARY KEY,pin TEXT,student TEXT,role TEXT,band TEXT,vacancy TEXT,question TEXT,answer TEXT,result TEXT,shared INTEGER DEFAULT 0,status TEXT,updated TEXT)""")
    for col in ["job_advert TEXT", "job_description TEXT", "person_spec TEXT", "application_form TEXT", "question_bank TEXT", "audio_data BLOB", "audio_mime TEXT", "audio_name TEXT", "teacher_token TEXT", "mock_settings TEXT", "mock_state TEXT", "interview_mode TEXT"]:
        try: c.execute(f"ALTER TABLE rooms ADD COLUMN {col}")
        except sqlite3.OperationalError: pass
    c.commit(); return c

def hp(x): return hashlib.sha256(x.encode()).hexdigest()

def room(code):
    c=conn(); r=c.execute("SELECT * FROM rooms WHERE code=?",(code.upper(),)).fetchone(); c.close(); return r

def update(code,**kw):
    c=conn()
    for k,v in kw.items(): c.execute(f"UPDATE rooms SET {k}=? WHERE code=?",(v,code.upper()))
    c.execute("UPDATE rooms SET updated=? WHERE code=?",(datetime.now().isoformat(timespec="seconds"),code.upper()))
    c.commit(); c.close()

def secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name)

def client():
    key=secret("OPENAI_API_KEY")
    if not key:
        st.error("OPENAI_API_KEY is not available on this computer/server."); st.stop()
    return OpenAI(api_key=key)

def livekit_credentials_ok():
    return all(secret(x) for x in ["LIVEKIT_URL","LIVEKIT_API_KEY","LIVEKIT_API_SECRET"])

def livekit_token(room_code, identity, can_publish=True, can_subscribe=True):
    from livekit import api
    url=secret("LIVEKIT_URL")
    key=secret("LIVEKIT_API_KEY")
    sec=secret("LIVEKIT_API_SECRET")
    if not all([url,key,sec]):
        raise RuntimeError("LiveKit secrets are missing.")
    grant=api.VideoGrants(
        room_join=True,
        room=f"interview-{room_code.upper()}",
        can_publish=can_publish,
        can_subscribe=can_subscribe,
    )
    token=(api.AccessToken(key,sec)
           .with_identity(identity)
           .with_grants(grant)
           .to_jwt())
    return url,token

def live_audio_panel(room_code, role):
    """LiveKit two-way audio. Teacher uses the proven iframe implementation."""
    if not livekit_credentials_ok():
        st.warning("Live audio is not configured. Check LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET in Streamlit Secrets.")
        return

    identity=f"{role.lower()}-{uuid.uuid4().hex[:12]}"
    try:
        url,token=livekit_token(room_code,identity,can_publish=True,can_subscribe=True)
    except Exception as e:
        st.error(f"Live audio setup error: {e}")
        return

    safe_url=html.escape(str(url),quote=True)
    safe_token=html.escape(str(token),quote=True)
    role_label="Teacher" if role=="Teacher" else "Student"

    live_html=f"""
    <!doctype html><html><head><meta charset="utf-8">
    <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
    <style>
      body {{ font-family: Arial,sans-serif; margin:0; }}
      .box {{ border:1px solid #d9d9d9; border-radius:12px; padding:14px; }}
      button {{ padding:10px 14px; margin:4px; border:0; border-radius:8px; cursor:pointer; font-weight:600; }}
      #join {{ background:#16a34a;color:white; }} #mute {{ background:#e5e7eb; }} #leave {{ background:#dc2626;color:white; }}
      #status {{ margin-top:8px;font-size:14px; }} #remoteAudio audio {{ width:100%;margin-top:8px; }}
    </style></head><body><div class="box">
      <b>🎧 Live Interview Audio — {role_label}</b><br>
      <button id="join">Join Live Audio</button><button id="mute" disabled>Mute</button><button id="leave" disabled>Leave</button>
      <div id="status">Not connected</div><div id="remoteAudio"></div>
    </div><script>
      const LK=LivekitClient; let room=null; let micEnabled=true;
      function status(msg){{document.getElementById('status').textContent=msg;}}
      function attachTrack(track){{if(track.kind===LK.Track.Kind.Audio){{const el=track.attach();el.autoplay=true;document.getElementById('remoteAudio').appendChild(el);el.play().catch(()=>{{}});}}}}
      async function autoJoin(){{try{{status('Reconnecting automatically…');room=new LK.Room({{adaptiveStream:true,dynacast:true}});
        room.on(LK.RoomEvent.TrackSubscribed,(track)=>{{attachTrack(track);status('Connected — live audio active');}});
        room.on(LK.RoomEvent.TrackUnsubscribed,(track)=>track.detach().forEach(el=>el.remove()));
        room.on(LK.RoomEvent.ParticipantConnected,()=>status('Connected — other participant joined'));
        room.on(LK.RoomEvent.ParticipantDisconnected,()=>status('Connected — waiting for other participant'));
        await room.connect("{safe_url}","{safe_token}");
        room.remoteParticipants.forEach((p)=>p.trackPublications.forEach((pub)=>{{if(pub.track)attachTrack(pub.track);}}));
        await room.localParticipant.setMicrophoneEnabled(true);
        document.getElementById('join').disabled=true;document.getElementById('mute').disabled=false;document.getElementById('leave').disabled=false;status('Connected — microphone live');
      }}catch(e){{status('Live audio error: '+(e.message||e));}}}}
      document.getElementById('join').onclick=autoJoin;
      autoJoin();
      document.getElementById('mute').onclick=async()=>{{if(!room)return;micEnabled=!micEnabled;await room.localParticipant.setMicrophoneEnabled(micEnabled);document.getElementById('mute').textContent=micEnabled?'Mute':'Unmute';status(micEnabled?'Connected — microphone live':'Connected — microphone muted');}};
      document.getElementById('leave').onclick=async()=>{{if(!room)return;await room.disconnect();room=null;document.getElementById('join').disabled=false;document.getElementById('mute').disabled=true;document.getElementById('leave').disabled=true;status('Disconnected');}};
    </script></body></html>"""
    components.html(live_html,height=170,scrolling=False)

# Student component: one microphone stream is both published to LiveKit and recorded.
STUDENT_AUDIO_HTML = """
<div class="icp-box">
  <b>🎧 Live Interview Audio + Answer Capture</b><br>
  <button id="join">Join Live Audio</button>
  <button id="start" disabled>Start Answer</button>
  <button id="stop" disabled>Stop & Send Recording</button>
  <button id="leave" disabled>Leave</button>
  <div id="status">Not connected</div>
  <div id="timer">⏱️ 00:00:00</div>
  <div id="remote"></div>
</div>
"""
STUDENT_AUDIO_CSS = """
.icp-box{border:1px solid #d9d9d9;border-radius:12px;padding:14px;font-family:Arial,sans-serif}
button{padding:10px 14px;margin:4px;border:0;border-radius:8px;cursor:pointer;font-weight:600}
#join{background:#16a34a;color:white} #start{background:#2563eb;color:white} #stop{background:#dc2626;color:white} #leave{background:#6b7280;color:white}
#status{margin-top:8px;font-size:14px} #timer{margin-top:8px;font-size:22px;font-weight:700} #remote audio{width:100%;margin-top:8px}
"""
STUDENT_AUDIO_JS = r"""
export default function(component) {
  const { data, parentElement, setStateValue } = component;
  const q = (s) => parentElement.querySelector(s);
  const join=q('#join'), start=q('#start'), stop=q('#stop'), leave=q('#leave'), status=q('#status'), timer=q('#timer'), remote=q('#remote');
  let room=null, localTrack=null, recorder=null, chunks=[], timerInterval=null, recordingStartedAt=null;
  function formatElapsed(ms){
    const total=Math.max(0,Math.floor(ms/1000));
    const h=String(Math.floor(total/3600)).padStart(2,'0');
    const m=String(Math.floor((total%3600)/60)).padStart(2,'0');
    const sec=String(total%60).padStart(2,'0');
    return `${h}:${m}:${sec}`;
  }
  function startTimer(){
    recordingStartedAt=Date.now();
    timer.textContent='🔴 Recording — 00:00:00';
    if(timerInterval) clearInterval(timerInterval);
    timerInterval=setInterval(()=>{ timer.textContent='🔴 Recording — '+formatElapsed(Date.now()-recordingStartedAt); },250);
  }
  function stopTimer(){
    if(timerInterval){ clearInterval(timerInterval); timerInterval=null; }
    if(recordingStartedAt) timer.textContent='⏹️ Recorded — '+formatElapsed(Date.now()-recordingStartedAt);
  }
  const say=(m)=>{ status.textContent=m; };

  async function loadLK(){
    if(window.LivekitClient) return window.LivekitClient;
    await new Promise((resolve,reject)=>{
      const existing=document.querySelector('script[data-icp-livekit]');
      if(existing){ existing.addEventListener('load',resolve,{once:true}); existing.addEventListener('error',reject,{once:true}); return; }
      const s=document.createElement('script'); s.dataset.icpLivekit='1'; s.src='https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js'; s.onload=resolve; s.onerror=reject; document.head.appendChild(s);
    });
    return window.LivekitClient;
  }
  function attach(track,LK){
    if(track.kind===LK.Track.Kind.Audio){ const el=track.attach(); el.autoplay=true; remote.appendChild(el); el.play().catch(()=>{}); }
  }
  async function autoJoin(){
    try{
      if(room) return;
      say('Reconnecting automatically…'); const LK=await loadLK(); room=new LK.Room({adaptiveStream:true,dynacast:true});
      room.on(LK.RoomEvent.TrackSubscribed,(track)=>attach(track,LK));
      room.on(LK.RoomEvent.TrackUnsubscribed,(track)=>track.detach().forEach(el=>el.remove()));
      await room.connect(data.url,data.token);
      room.remoteParticipants.forEach((p)=>p.trackPublications.forEach((pub)=>{if(pub.track)attach(pub.track,LK);}));
      localTrack=await LK.createLocalAudioTrack(); await room.localParticipant.publishTrack(localTrack);
      join.disabled=true; start.disabled=false; leave.disabled=false; say('Connected — teacher can hear you');
    }catch(e){ say('Live audio error: '+(e.message||e)); }
  }
  join.onclick=autoJoin;
  autoJoin();
  start.onclick=()=>{
    if(!localTrack || !localTrack.mediaStreamTrack){
      say('Press Join Live Audio first.');
      return;
    }
    try{
      chunks=[];
      const recordingTrack=localTrack.mediaStreamTrack.clone();
      const answerStream=new MediaStream([recordingTrack]);
      const candidates=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/mp4'];
      const preferred=candidates.find(t=>window.MediaRecorder && MediaRecorder.isTypeSupported(t));
      recorder=preferred ? new MediaRecorder(answerStream,{mimeType:preferred}) : new MediaRecorder(answerStream);
      recorder.ondataavailable=(e)=>{ if(e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop=()=>{
        stopTimer();
        const blob=new Blob(chunks,{type:recorder.mimeType||'audio/webm'});
        const reader=new FileReader();
        reader.onloadend=()=>{
          setStateValue('recording',{data_url:reader.result,mime:blob.type,size:blob.size,duration_ms:(recordingStartedAt ? Date.now()-recordingStartedAt : 0)});
          say('Answer recorded and sent for transcription. Live audio remains connected.');
        };
        reader.readAsDataURL(blob);
        recordingTrack.stop();
      };
      recorder.start(1000);
      startTimer();
      start.disabled=true;
      stop.disabled=false;
      say('Recording answer — teacher can hear you live');
    }catch(e){
      say('Recording error: '+(e.message||e));
    }
  };
  stop.onclick=()=>{ if(recorder && recorder.state!=='inactive'){ recorder.stop(); stop.disabled=true; start.disabled=false; } };
  leave.onclick=async()=>{ try{ if(recorder&&recorder.state!=='inactive')recorder.stop(); stopTimer(); if(localTrack)localTrack.stop(); if(room)await room.disconnect(); }finally{ room=null;localTrack=null;join.disabled=false;start.disabled=true;stop.disabled=true;leave.disabled=true;say('Disconnected'); } };
  return ()=>{ try{ if(localTrack)localTrack.stop(); if(room)room.disconnect(); }catch(e){} };
}
"""

try:
    student_audio_component = st.components.v2.component(
        "interview_coach_student_audio",
        html=STUDENT_AUDIO_HTML,
        css=STUDENT_AUDIO_CSS,
        js=STUDENT_AUDIO_JS,
    )
except Exception:
    student_audio_component = None

def student_live_audio_capture(room_code):
    if not livekit_credentials_ok():
        st.warning("Live audio is not configured.")
        return None
    if student_audio_component is None:
        st.error("This app needs Streamlit 1.51 or newer for single-stream live recording.")
        return None
    state_key=f"student_audio_identity_{room_code}"
    if state_key not in st.session_state:
        st.session_state[state_key]=f"student-{uuid.uuid4().hex[:12]}"
    try:
        url,token=livekit_token(room_code,st.session_state[state_key],can_publish=True,can_subscribe=True)
    except Exception as e:
        st.error(f"Live audio setup error: {e}"); return None
    result=student_audio_component(
        data={"url":str(url),"token":str(token)},
        key=f"student_audio_{room_code}",
        default={"recording": None},
        on_recording_change=lambda: None,
    )
    return getattr(result,"recording",None)


def create_realtime_client_secret(instructions, voice="marin"):
    """Create a short-lived Realtime credential on the Streamlit server."""
    api_key = secret("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing from Streamlit Secrets.")

    payload = {
        "session": {
            "type": "realtime",
            "model": "gpt-realtime-2.1",
            "instructions": instructions,
            "audio": {
                "output": {"voice": voice},
                "input": {
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True
                    }
                }
            }
        }
    }

    sid = hashlib.sha256(
        ("interview-coach-pro|" + str(datetime.now().date())).encode()
    ).hexdigest()[:32]

    response = requests.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Safety-Identifier": sid
        },
        json=payload,
        timeout=30
    )
    if not response.ok:
        raise RuntimeError(
            f"Could not prepare Live Voice ({response.status_code}): "
            + response.text[:400]
        )

    data = response.json()
    ephemeral = data.get("value")
    if not ephemeral and isinstance(data.get("client_secret"), dict):
        ephemeral = data["client_secret"].get("value")
    if not ephemeral:
        raise RuntimeError("OpenAI did not return the short-lived Live Voice credential.")
    return ephemeral


def live_voice_component(client_secret, first_question, room_code, voice="marin", livekit_url="", livekit_token_value=""):
    """Natural hands-free browser interview using WebRTC and automatic turn detection."""
    secret_js = json.dumps(client_secret)
    q_js = json.dumps(first_question or "")
    room_js = json.dumps(room_code or "")
    lk_url_js = json.dumps(livekit_url or "")
    lk_token_js = json.dumps(livekit_token_value or "")

    live_html = f"""
    <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
    <div style="font-family:Arial,sans-serif;border:1px solid #555;border-radius:14px;padding:14px">
      <div style="font-size:18px;font-weight:700">🎙️ British-English Live AI Interviewer</div>
      <div id="status" style="margin:8px 0">Ready. Press Start once.</div>
      <button id="start" style="padding:10px 15px">▶ Start live interview</button>
      <button id="stop" disabled style="padding:10px 15px;margin-left:6px">■ End & disconnect</button>
      <div id="turn" style="margin-top:10px;font-size:14px">
        When you finish speaking, the interviewer will respond automatically.
      </div>
      <audio id="remoteAudio" autoplay></audio>
    </div>

    <script>
    (() => {{
      const token = {secret_js};
      const firstQuestion = {q_js};
      const roomCode = {room_js};
      const lkUrl = {lk_url_js};
      const lkToken = {lk_token_js};
      const status = document.getElementById("status");
      const turn = document.getElementById("turn");
      const start = document.getElementById("start");
      const stop = document.getElementById("stop");
      const remoteAudio = document.getElementById("remoteAudio");

      let pc = null, dc = null, mic = null, lkRoom = null, lkMicTrack = null, aiPublishedTrack = null;

      function disconnect() {{
        if (mic) mic.getTracks().forEach(t => t.stop());
        if (dc) {{ try {{ dc.close(); }} catch(e) {{}} }}
        if (pc) {{ try {{ pc.close(); }} catch(e) {{}} }}
        if (aiPublishedTrack) {{ try {{ aiPublishedTrack.stop(); }} catch(e) {{}} aiPublishedTrack=null; }}
        if (lkMicTrack) {{ try {{ lkMicTrack.stop(); }} catch(e) {{}} lkMicTrack=null; }}
        if (lkRoom) {{ try {{ lkRoom.disconnect(); }} catch(e) {{}} lkRoom=null; }}
        mic = null; dc = null; pc = null;
        start.disabled = false;
        stop.disabled = true;
      }}

      start.onclick = async () => {{
        start.disabled = true;
        status.textContent = "Connecting securely…";
        try {{
          pc = new RTCPeerConnection();
          pc.ontrack = async e => {{
            remoteAudio.srcObject = e.streams[0];
            // Publish the AI interviewer audio into the SAME LiveKit room so the teacher hears it.
            try {{
              if (lkRoom && e.track && e.track.kind === "audio" && !aiPublishedTrack) {{
                aiPublishedTrack = new LivekitClient.LocalAudioTrack(e.track);
                await lkRoom.localParticipant.publishTrack(aiPublishedTrack, {{name:"ai-interviewer"}});
              }}
            }} catch(err) {{ console.warn("Could not publish AI audio to teacher room", err); }}
          }};

          mic = await navigator.mediaDevices.getUserMedia({{audio:true}});
          mic.getTracks().forEach(track => pc.addTrack(track, mic));

          // Join the teacher's existing LiveKit room without changing the rest of the app.
          // Student microphone is published so the teacher hears the student.
          // Teacher microphone is subscribed here so the student hears the teacher.
          if (lkUrl && lkToken && window.LivekitClient) {{
            try {{
              const LK = window.LivekitClient;
              lkRoom = new LK.Room({{adaptiveStream:true,dynacast:true}});
              lkRoom.on(LK.RoomEvent.TrackSubscribed, (track, publication, participant) => {{
                if (track.kind === LK.Track.Kind.Audio && publication.trackName !== "ai-interviewer") {{
                  const el = track.attach(); el.autoplay = true; el.style.display = "none";
                  document.body.appendChild(el); el.play().catch(()=>{{}});
                }}
              }});
              await lkRoom.connect(lkUrl, lkToken);
              lkRoom.remoteParticipants.forEach((p)=>p.trackPublications.forEach((pub)=>{{
                if(pub.track && pub.track.kind===LK.Track.Kind.Audio && pub.trackName !== "ai-interviewer") {{
                  const el=pub.track.attach(); el.autoplay=true; el.style.display="none"; document.body.appendChild(el); el.play().catch(()=>{{}});
                }}
              }}));
              const micTrack = mic.getAudioTracks()[0];
              if (micTrack) {{
                lkMicTrack = new LK.LocalAudioTrack(micTrack.clone());
                await lkRoom.localParticipant.publishTrack(lkMicTrack, {{name:"student-microphone"}});
              }}
            }} catch(err) {{ console.warn("Teacher room audio connection failed", err); }}
          }}

          dc = pc.createDataChannel("oai-events");

          dc.onopen = () => {{
            status.textContent = "Connected — live interview in progress";
            stop.disabled = false;
            const opening = firstQuestion
              ? "Begin now. Give a brief professional welcome, then ask exactly this first interview question: " + firstQuestion
              : "Begin now with a brief professional welcome and the first interview question.";
            dc.send(JSON.stringify({{
              type: "response.create",
              response: {{ instructions: opening }}
            }}));
          }};

          dc.onmessage = ev => {{
            let e;
            try {{ e = JSON.parse(ev.data); }} catch (_) {{ return; }}
            if (e.type === "input_audio_buffer.speech_started") {{
              turn.textContent = "🎤 Listening…";
            }} else if (e.type === "input_audio_buffer.speech_stopped") {{
              turn.textContent = "🧠 Answer complete — interviewer is responding…";
            }} else if (e.type === "response.done") {{
              turn.textContent = "🎤 Your turn.";
            }} else if (e.type === "error") {{
              status.textContent = "Live Voice error: " + (e.error?.message || "Unknown error");
            }}
          }};

          const offer = await pc.createOffer();
          await pc.setLocalDescription(offer);

          const r = await fetch("https://api.openai.com/v1/realtime/calls", {{
            method: "POST",
            body: offer.sdp,
            headers: {{
              "Authorization": "Bearer " + token,
              "Content-Type": "application/sdp"
            }}
          }});

          if (!r.ok) throw new Error(await r.text());
          const answer = await r.text();
          await pc.setRemoteDescription({{type:"answer", sdp:answer}});
        }} catch (err) {{
          status.textContent = "Connection failed: " + err.message;
          disconnect();
        }}
      }};

      stop.onclick = () => {{
        disconnect();
        status.textContent = "Disconnected. Live Voice usage has stopped.";
        turn.textContent = "Interview connection closed.";
      }};

      window.addEventListener("beforeunload", disconnect);
    }})();
    </script>
    """
    components.html(live_html, height=220)


def transcribe(audio):
    data=audio.getvalue()
    out=client().audio.transcriptions.create(model="gpt-4o-mini-transcribe",file=(getattr(audio,"name","answer.wav"),data,getattr(audio,"type","audio/wav")))
    return out.text

def recording_bytes(recording):
    data_url=(recording or {}).get("data_url","")
    if not data_url or "," not in data_url:
        return b"", (recording or {}).get("mime","audio/webm")
    header,payload=data_url.split(",",1)
    mime=(recording or {}).get("mime") or (header.split(";")[0].replace("data:","") if header.startswith("data:") else "audio/webm")
    return base64.b64decode(payload), mime

def transcribe_data_url(recording):
    if not recording or not recording.get("data_url"):
        return ""
    header,b64=recording["data_url"].split(",",1)
    data=base64.b64decode(b64)
    mime=recording.get("mime") or "audio/webm"
    ext="webm" if "webm" in mime else ("ogg" if "ogg" in mime else "wav")
    out=client().audio.transcriptions.create(model="gpt-4o-mini-transcribe",file=(f"student-answer.{ext}",data,mime))
    return out.text

def speak_text(text, key="speech"):
    """Free browser text-to-speech: no additional paid voice service is activated."""
    if not text: return
    payload=json.dumps(str(text))
    components.html(f"""<div style='padding:6px 0'><button onclick='speechSynthesis.cancel();let u=new SpeechSynthesisUtterance({payload});u.rate=0.95;speechSynthesis.speak(u);'>🔊 Hear AI Interviewer</button><button onclick='speechSynthesis.cancel()'>⏹ Stop</button></div>""",height=48)

def delivery_metrics(answer, duration_seconds=None):
    words=[w for w in str(answer).replace("\n"," ").split() if w.strip()]
    low=" "+" ".join(words).lower()+" "
    fillers=[" um "," erm "," uh "," you know "," basically "," actually "," like "]
    filler_count=sum(low.count(x) for x in fillers)
    repeated=0
    for a,b in zip(words,words[1:]):
        if a.strip('.,!?').lower()==b.strip('.,!?').lower(): repeated+=1
    wpm=None
    if duration_seconds and duration_seconds>5:
        wpm=round(len(words)/(duration_seconds/60))
    return {"word_count":len(words),"duration_seconds":round(duration_seconds or 0,1),"words_per_minute":wpm,"filler_words_detected":filler_count,"immediate_word_repetitions":repeated}

def scrutinize(role,band,vacancy,question,answer,prior_probes=0,max_probes=3,practice=False,star_check=True,safety_check=True,contradiction_check=True,application_check=True,scrutiny_level='High'):
    instructions="""You are the strict but professional AI interview panel for Interview Coach Pro. Judge ONLY evidence actually spoken by the candidate and criteria supported by the supplied vacancy material. Do not reward keywords alone. Determine whether the answer is correct, relevant, sufficiently specific, safe, within role scope, and complete for the question. For behavioural questions examine personal contribution and STAR evidence where appropriate. For clinical/scenario questions prioritise safety, prioritisation, escalation, communication and role scope where relevant. Identify contradictions or unsupported claims cautiously; ask for clarification rather than accusing dishonesty.

Return JSON only with keys: status, completion_percent, safety_status, missing_critical_evidence, probe_question, reason, earned_well_done, completion_matrix, evidence_quotes. completion_matrix is a list of objects with criterion, met, evidence. evidence_quotes must be brief phrases actually heard in the answer.
status must be one of complete, incomplete, vague, incorrect, unsafe, irrelevant.
earned_well_done is true ONLY when the candidate has sufficiently answered the question without unresolved critical gaps.
If incomplete/vague/incorrect/unsafe and another probe is allowed, write ONE natural panel follow-up that probes the most important unresolved issue WITHOUT revealing the model answer. Never include a hint in Mock mode. If the probe limit is exhausted, probe_question must be empty. completion_percent is evidence coverage, not a probability."""
    prompt=f"""ROLE: {role}\nBAND: {band}\nVACANCY MATERIAL:\n{vacancy}\nQUESTION:\n{question}\nCANDIDATE ANSWER:\n{answer}\nPROBES ALREADY USED: {prior_probes}\nMAX PROBES: {max_probes}\nMODE: {'PRACTICE/TEACHER' if practice else 'MOCK INTERVIEW'}\nSCRUTINY LEVEL: {scrutiny_level}\nSTAR CHECK: {star_check}\nSAFETY CHECK: {safety_check}\nCONTRADICTION CHECK: {contradiction_check}\nAPPLICATION EVIDENCE CHECK: {application_check}"""
    res=client().responses.create(model="gpt-5.6",instructions=instructions,input=prompt)
    raw=res.output_text.strip()
    if raw.startswith("```"): raw=raw.split("\n",1)[1].rsplit("```",1)[0].strip()
    out=json.loads(raw)
    if prior_probes>=max_probes: out["probe_question"]=""
    return out

def final_panel_report(role,band,vacancy,turns,delivery=None):
    instructions="""Act as a structured NHS-style interview assessor. Use the supplied vacancy's Trust/Board criteria and any explicit employer scoring matrix found in the materials. Never invent an official employer scale. If an explicit employer scale is present, use it. Otherwise label the result 'NHS-style simulated structured scoring' and use a transparent 0-5 evidence scale. Distinguish independent first-answer performance from evidence obtained after panel probing. Do not treat eventual prompted knowledge as equivalent to an excellent independent first answer. Return JSON only with keys employer, scoring_framework_label, scale_explanation, overall_percent, questions_answered_independently, total_questions, jd_ps_coverage_percent, content_percent, star_percent, safety_summary, strongest_area, priority_improvements, question_results, delivery_comment, panel_perspectives. panel_perspectives must include appropriate Hiring Manager, Role-Specific/Clinical-Technical, and Values/People viewpoints; never impose clinical criteria on a non-clinical vacancy. Each question_results item must contain question, score, max_score, initial_evidence_percent, final_evidence_percent, probes_required, evidence_met, evidence_missing, score_reason."""
    prompt=f"ROLE: {role}\nBAND: {band}\nVACANCY MATERIAL:\n{vacancy}\nINTERVIEW RECORD:\n{json.dumps(turns)}\nDELIVERY METRICS:\n{json.dumps(delivery or {})}"
    res=client().responses.create(model="gpt-5.6",instructions=instructions,input=prompt)
    raw=res.output_text.strip()
    if raw.startswith("```"): raw=raw.split("\n",1)[1].rsplit("```",1)[0].strip()
    return json.loads(raw)


def performance_traffic_light(percent):
    """Coaching traffic-light indicator; not an employer pass/fail rule unless vacancy materials say so."""
    try:
        pct=max(0, min(100, float(percent)))
    except Exception:
        pct=0
    if pct >= 70:
        return "GREEN", "Strong performance", "#15803d", "🟢"
    if pct >= 50:
        return "YELLOW", "Developing / needs improvement", "#ca8a04", "🟡"
    return "RED", "Below expected evidence level", "#b91c1c", "🔴"

def show_traffic_light(percent, title="Performance indicator"):
    band,label,colour,icon=performance_traffic_light(percent)
    html=(f'<div style="border:2px solid {colour};border-radius:12px;padding:12px 16px;margin:8px 0 14px 0;">'
          f'<div style="font-size:1.35rem;font-weight:700;color:{colour};">{icon} {band} — {label}</div>'
          f'<div style="font-size:0.95rem;">{title}: <b>{round(float(percent))}%</b></div></div>')
    st.markdown(html, unsafe_allow_html=True)
    return band,label

def show_panel_report(r):
    st.header("📋 Final Interview Panel Report")
    st.caption(str(r.get("scoring_framework_label","NHS-style simulated structured scoring")))
    a,b,c=st.columns(3)
    a.metric("Overall evidence",f"{r.get('overall_percent',0)}%")
    b.metric("JD/PS coverage",f"{r.get('jd_ps_coverage_percent',0)}%")
    c.metric("Content",f"{r.get('content_percent',0)}%")
    show_traffic_light(r.get("overall_percent",0), "Overall mock-interview performance")
    st.caption("Traffic-light colours are coaching indicators. They are not an official employer pass/fail threshold unless the supplied vacancy/recruitment materials explicitly define those thresholds.")
    st.write("**Employer:**",r.get("employer","Not identified"))
    st.write("**Scoring basis:**",r.get("scale_explanation",""))
    st.write("**Safety:**",r.get("safety_summary",""))
    st.write("**Questions answered independently:**",f"{r.get('questions_answered_independently',0)}/{r.get('total_questions',0)}")
    st.markdown("### Question-by-question evidence")
    for i,x in enumerate(r.get("question_results",[]),1):
        with st.expander(f"Q{i}: {x.get('score',0)}/{x.get('max_score',5)} — {x.get('question','')}"):
            try:
                qmax=float(x.get('max_score',5) or 5); qscore=float(x.get('score',0) or 0)
                qpercent=(qscore/qmax)*100 if qmax else 0
            except Exception:
                qpercent=0
            show_traffic_light(qpercent, f"Question {i} performance")
            st.write("**Initial evidence coverage:**",f"{x.get('initial_evidence_percent',0)}%")
            st.write("**Final evidence coverage:**",f"{x.get('final_evidence_percent',0)}%")
            st.write("**Panel probes required:**",x.get("probes_required",0))
            st.write("**Evidence met:**",x.get("evidence_met",[]))
            st.write("**Evidence missing:**",x.get("evidence_missing",[]))
            st.write("**Why this score:**",x.get("score_reason",""))
    st.markdown("### Priority improvements")
    for x in r.get("priority_improvements",[]): st.write("•",x)
    if r.get("delivery_comment"): st.write("**Speaking/delivery:**",r.get("delivery_comment"))
    if r.get("panel_perspectives"):
        st.markdown("### 👥 Simulated panel perspectives")
        pp=r.get("panel_perspectives")
        if isinstance(pp,dict):
            for panel_name,comment in pp.items():
                st.write(f"**{panel_name}:**",comment)
        else:
            st.write(pp)


def extract_upload(upload):
    if upload is None: return ""
    name=upload.name.lower(); data=upload.getvalue()
    try:
        if name.endswith(".txt"):
            return data.decode("utf-8",errors="ignore")
        if name.endswith(".pdf"):
            import io
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        if name.endswith(".docx"):
            import io
            from docx import Document
            return "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    except Exception as e:
        st.warning(f"Could not read {upload.name}: {e}")
    return ""

def source_box(label,key):
    up=st.file_uploader(f"Upload {label} (PDF, DOCX or TXT)",type=["pdf","docx","txt"],key=f"up_{key}")
    uploaded_text=extract_upload(up)
    return st.text_area(f"Or paste {label}",value=uploaded_text,height=140,key=f"txt_{key}")

def generate_questions(role,band,advert,jd,ps,application,n):
    instructions="""You are an NHS/healthcare interview-panel question designer. Use ONLY the recruitment materials supplied. Generate realistic, vacancy-specific interview questions. Cross-reference the job advert, job description, person specification and candidate application. Prioritise essential criteria and core responsibilities; include relevant values, clinical/technical knowledge, safeguarding/safety, communication/teamwork, scenarios and motivation. Generate application-evidence follow-ups only where the application actually supports them. Do not invent candidate experience or requirements. Avoid duplicate/generic questions when specific evidence exists. Return JSON only as an object with key questions. questions is an array of objects with keys question, type, why_asked, source_basis, expected_concepts. type should be one of motivation, competency, behavioural_STAR, scenario, safeguarding, values, knowledge, clinical_technical, teamwork_leadership, application_followup."""
    prompt=f"""ROLE: {role}\nBAND: {band}\nNUMBER OF QUESTIONS: {n}\n\nJOB ADVERT:\n{advert}\n\nJOB DESCRIPTION:\n{jd}\n\nPERSON SPECIFICATION:\n{ps}\n\nCANDIDATE APPLICATION FORM:\n{application}\n\nGenerate exactly {n} adaptive questions grounded in these materials."""
    res=client().responses.create(model="gpt-5.6",instructions=instructions,input=prompt)
    text=res.output_text.strip()
    if text.startswith("```"): text=text.split("\n",1)[1].rsplit("```",1)[0].strip()
    return json.loads(text)["questions"]

def assess(role,band,vacancy,question,answer):
    instructions="""You are a rigorous NHS interview assessor.

PRIMARY RULE — TRUST-SPECIFIC SCORING:
Every score must be based on the requirements supplied for THIS vacancy, not on a generic NHS marking sheet.

First extract the vacancy-specific assessment criteria from the supplied material, including where present:
- employing NHS Trust/Board and service/department
- job description and duties
- person specification essential criteria
- person specification desirable criteria
- qualifications/registration requirements
- experience requirements
- clinical/technical competencies
- communication, teamwork, leadership and behavioural competencies
- safeguarding, patient safety, scope of practice, escalation and governance requirements
- Trust/Board values and behaviours
- role-specific knowledge
- Band/seniority expectations
- candidate application claims that legitimately invite follow-up

Then classify the interview question and select ONLY the vacancy criteria that are relevant to that particular question. Build the marking matrix BEFORE scoring the answer.

SCORING:
Use a standardised 0-5 display scale so the student can understand performance, but the CONTENT required to earn each mark must come from the specific Trust/Board vacancy requirements.

0 = no relevant evidence, no answer, or fundamentally unsafe answer where safety is central.
1 = very weak evidence against the relevant vacancy criteria; major essential points absent.
2 = limited evidence; some relevant points but important vacancy-specific indicators are missing.
3 = satisfactory evidence meeting the main relevant vacancy requirements for this question.
4 = strong, specific evidence meeting nearly all relevant vacancy indicators at the expected role/Band level.
5 = excellent, comprehensive and specific evidence meeting all or almost all relevant vacancy indicators at the expected role/Band level.

SCORING-SCALE SOURCE RULE:
- First identify the employing NHS Trust/Board from the supplied vacancy material.
- Search ONLY the supplied recruitment/scoring material for an explicit interview scale, weighting, pass rule, competency matrix or question-specific scoring instruction.
- If an explicit employer scale is supplied, reproduce and apply that scale exactly, including its maximum score and documented threshold where present.
- Never infer a Trust/Board scoring scale merely from the employer name.
- If no explicit employer scoring scale is supplied, use the app's 0-5 COACHING SIMULATION scale below and clearly state that the scale is simulated, while the CONTENT criteria remain specific to that Trust/Board vacancy.
- Explain the source of the scale in scoring_note.

RULES:
- Award marks only for evidence actually present in the candidate's answer.
- Do not award marks merely because a keyword appears.
- Essential person-specification requirements relevant to the question carry greater significance than desirable criteria.
- Do not invent Trust values, requirements, thresholds or candidate experience.
- Do not assume all NHS organisations use the same interview scoring system.
- STAR is useful for behavioural/competency/experience questions but must not be forced onto motivation, knowledge or other unsuitable question types.
- For clinical/scenario/safeguarding questions, assess factual/clinical correctness, safety, scope, prioritisation, escalation and communication only when relevant to the question and vacancy.
- Empty answer = 0. Very short/vague answers should score very low.
- Materially unsafe content must be flagged and may cap the score.
- Separate factual/clinical correctness from interview effectiveness.
- Suggested answers must remain consistent with the candidate's supplied application evidence. Never fabricate personal experience.
- If no genuine personal example is available, give a framework/template the candidate can complete truthfully.
- Do not declare a candidate universally NHS 'appointable' or 'not appointable' unless the supplied employer documents explicitly define such a threshold and the available evidence permits that conclusion.

Return JSON only with keys:
trust_or_board, question_type, panel_is_testing, trust_requirements_used, marking_matrix, essential_criteria_relevant, desirable_criteria_relevant, values_relevant, likely_keywords, vacancy_matches, nhs_score, max_score, overall_score, verdict, correctness, safety_status, score_breakdown, criteria_met, criteria_not_met, strengths, missing_points, improvements, framework, suggested_answer, follow_up_questions, scoring_note, scoring_framework_status, employer_pass_rule, performance_indicator.

marking_matrix must identify each relevant vacancy-specific criterion and what evidence in this answer would demonstrate it.
trust_requirements_used must identify which supplied Trust/Board requirements were actually used to score this question.
If no Trust/Board-specific criterion relevant to a category is supplied, say "Not specified in supplied vacancy material" rather than inventing one.

Unless an explicit employer scoring scale supplied in the vacancy materials requires otherwise:
nhs_score must be an integer 0-5.
max_score must be 5.
overall_score must equal nhs_score * 20.

verdict must be one of:
Strong / Interview Ready
Good but Can Be Strengthened
Partially Correct
Significant Improvement Needed
Safety-Critical Concern
"""
    prompt=f"""ROLE: {role}
BAND: {band}

SUPPLIED TRUST/BOARD VACANCY MATERIALS:
{vacancy or "No vacancy material supplied"}

INTERVIEW QUESTION:
{question}

CANDIDATE ANSWER:
{answer}

Score this answer specifically against the requirements of the supplied Trust/Board vacancy. Do not substitute generic NHS criteria for vacancy-specific criteria."""
    res=client().responses.create(model="gpt-5.6",instructions=instructions,input=prompt)
    raw=res.output_text.strip()
    if raw.startswith("```"): raw=raw.split("\n",1)[1].rsplit("```",1)[0].strip()
    result=json.loads(raw)

    # Keep the app's comparable 0-5 display unless the returned result clearly
    # provides another employer-specific max score.
    try:
        max_score=int(result.get("max_score",5))
        score=int(result.get("nhs_score",0))
        if max_score <= 0: max_score=5
        score=max(0,min(max_score,score))
    except Exception:
        max_score,score=5,0

    result["nhs_score"]=score
    result["max_score"]=max_score
    result["overall_score"]=round((score/max_score)*100) if max_score else 0

    # Transparent coaching traffic-light display.
    # These colours are NOT presented as an employer pass/fail rule unless the
    # supplied employer material explicitly states such a rule.
    pct=result["overall_score"]
    if pct >= 70:
        result["traffic_light"]="GREEN"
        result["traffic_label"]="Strong performance"
        result["traffic_icon"]="🟢"
    elif pct >= 50:
        result["traffic_light"]="YELLOW"
        result["traffic_label"]="Developing / needs strengthening"
        result["traffic_icon"]="🟡"
    else:
        result["traffic_light"]="RED"
        result["traffic_label"]="Significant improvement needed"
        result["traffic_icon"]="🔴"
    return result

def show(r):
    score=int(r.get("nhs_score",0))
    max_score=int(r.get("max_score",5) or 5)
    pct=int(r.get("overall_score",0))
    if pct >= 70:
        icon,label,bg,border="🟢","GREEN — Strong performance","#e8f5e9","#2e7d32"
    elif pct >= 50:
        icon,label,bg,border="🟡","YELLOW — Developing / needs strengthening","#fff8e1","#f9a825"
    else:
        icon,label,bg,border="🔴","RED — Significant improvement needed","#ffebee","#c62828"

    cscore,cpercent,cstatus=st.columns(3)
    cscore.metric("Question score",f"{score}/{max_score}")
    cpercent.metric("Equivalent percentage",f"{pct}/100")
    cstatus.metric("Performance",f"{icon} {label.split(' — ')[0]}")
    st.markdown(
        f"""<div style="padding:14px;border-radius:10px;background:{bg};
        border:2px solid {border};font-weight:700;font-size:18px">
        {icon} {label}
        </div>""",
        unsafe_allow_html=True
    )
    st.caption("Traffic-light colours are coaching indicators unless the supplied Trust/Board recruitment material explicitly defines its own pass/performance thresholds.")
    st.subheader(r.get("verdict","Assessment"))
    a,b=st.columns(2)
    with a:
        st.markdown("### ✅ What went well")
        for x in r.get("strengths",[]): st.write("•",x)
        st.markdown("### 🎯 JD/PS matches")
        for x in r.get("vacancy_matches",[]): st.write("•",x)
    with b:
        st.markdown("### Missing points")
        for x in r.get("missing_points",[]): st.write("•",x)
        st.markdown("### How to improve")
        for x in r.get("improvements",[]): st.write("•",x)
    st.markdown("### Trust/Board requirements used for this score")
    trust_name=r.get("trust_or_board","Not identified from supplied vacancy material")
    st.write("**Employer:**",trust_name)
    for x in r.get("trust_requirements_used",[]): st.write("•",x)
    st.markdown("### Question-specific marking matrix")
    for x in r.get("marking_matrix",[]): st.write("•",x)
    st.markdown("### Detailed assessment")
    st.write("**Question type:**",r.get("question_type",""))
    st.write("**Panel is testing:**",r.get("panel_is_testing",""))
    st.write("**Correctness:**",r.get("correctness",""))
    st.write("**Safety:**",r.get("safety_status",""))
    st.write("**Score breakdown:**",r.get("score_breakdown",{}))
    st.write("**Criteria met:**",r.get("criteria_met",[]))
    st.write("**Criteria not met:**",r.get("criteria_not_met",[]))
    st.write("**Scoring framework status:**",r.get("scoring_framework_status","Employer-specific scale not explicitly identified; coaching simulation used unless supplied material states otherwise."))
    st.write("**Employer pass/performance rule:**",r.get("employer_pass_rule","Not specified in supplied recruitment material."))
    st.caption(r.get("scoring_note",""))
    st.write("**Expected concepts/keywords:**",", ".join(r.get("likely_keywords",[])))
    st.markdown("### Recommended framework"); st.write(r.get("framework",""))
    st.markdown("### Stronger answer"); st.write(r.get("suggested_answer",""))
    st.markdown("### Likely follow-up questions")
    for x in r.get("follow_up_questions",[]): st.write("•",x)

def persistent_login_params():
    """Restore Teacher/Student room access from this browser's URL until the user logs out."""
    try:
        saved_mode=st.query_params.get("mode", "")
        saved_code=st.query_params.get("room", "").upper().strip()
        saved_token=st.query_params.get("token", "")
    except Exception:
        return "", "", ""
    return saved_mode, saved_code, saved_token

def remember_teacher(code, token):
    st.query_params["mode"]="Teacher"
    st.query_params["room"]=code
    st.query_params["token"]=token

def remember_student(code):
    st.query_params["mode"]="Student"
    st.query_params["room"]=code
    if "token" in st.query_params:
        del st.query_params["token"]

def logout_button(label="Log Out"):
    if st.sidebar.button(label, type="secondary"):
        for k in ["code","pin","teacher_token"]:
            st.session_state.pop(k, None)
        st.query_params.clear()
        st.rerun()

saved_mode,saved_code,saved_token=persistent_login_params()

st.title("🎓 Interview Coach Pro")
st.caption("Teacher-controlled interview room • Live two-way audio • Timed voice capture • Trust/Board-specific evidence scoring • Red/Yellow/Green performance • Vacancy-specific AI assessment")
mode_options=["Teacher","Student","Student Practice","Mock Interview"]
default_mode=saved_mode if saved_mode in ["Teacher","Student"] else "Teacher"
mode=st.sidebar.radio("Open as",mode_options,index=mode_options.index(default_mode))

if mode=="Teacher":
    st.header("👩‍🏫 Teacher Dashboard")
    t1,t2=st.tabs(["Create room","Open room"])
    with t1:
        student=st.text_input("Student name")
        role=st.text_input("Role")
        band=st.selectbox("Band",["Band 2","Band 3","Band 4","Band 5","Band 6","Band 7","Other"])
        employer=st.text_input("Employing NHS Trust / Health Board",help="Example: NHS Lothian, Manchester University NHS Foundation Trust, NHS Greater Glasgow and Clyde.")
        st.markdown("### Recruitment documents")
        advert=source_box("Job Advert","advert")
        jd=source_box("Job Description (JD)","jd")
        ps=source_box("Person Specification (PS)","ps")
        application=source_box("Candidate Application Form / Supporting Information","application")
        scoring_policy=source_box("Trust/Board Interview Scoring Policy or Recruitment Scoring Matrix (optional)","scoring_policy")
        st.caption("If an official employer scoring matrix is supplied here, the assessor will use it. If none is supplied, the app will not invent an official Trust/Board scale.")
        pin=st.text_input("Create private Teacher PIN",type="password")
        if st.button("Create Interview Room",type="primary"):
            if not role or not pin: st.warning("Enter the role and Teacher PIN.")
            else:
                code=uuid.uuid4().hex[:6].upper(); c=conn()
                combined="\n\n".join(["EMPLOYING NHS TRUST/BOARD:\n"+employer,"JOB ADVERT:\n"+advert,"JOB DESCRIPTION:\n"+jd,"PERSON SPECIFICATION:\n"+ps,"APPLICATION FORM:\n"+application,"EMPLOYER INTERVIEW SCORING POLICY / MATRIX:\n"+scoring_policy])
                c.execute("INSERT INTO rooms(code,pin,student,role,band,vacancy,status,updated,job_advert,job_description,person_spec,application_form,question_bank) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(code,hp(pin),student,role,band,combined,"waiting",datetime.now().isoformat(),advert,jd,ps,application,""))
                teacher_token=uuid.uuid4().hex
                c.execute("UPDATE rooms SET teacher_token=? WHERE code=?",(teacher_token,code))
                c.commit(); c.close(); st.session_state.code=code; st.session_state.pin=pin; st.session_state.teacher_token=teacher_token
                remember_teacher(code,teacher_token)
                st.success(f"Room created. Student Code: {code}")
                st.info("Give the student only this code. Keep your Teacher PIN private.")
    with t2:
        oc=st.text_input("Room code").upper().strip(); op=st.text_input("Teacher PIN",type="password",key="op")
        if st.button("Open Dashboard"):
            r=room(oc)
            if r and r["pin"]==hp(op):
                teacher_token=uuid.uuid4().hex
                update(oc,teacher_token=teacher_token)
                st.session_state.code=oc; st.session_state.pin=op; st.session_state.teacher_token=teacher_token
                remember_teacher(oc,teacher_token)
                st.rerun()
            else: st.error("Incorrect room code or Teacher PIN.")
    code=st.session_state.get("code") or (saved_code if saved_mode=="Teacher" else "")
    if code:
        r=room(code)
        pin_ok=bool(r and st.session_state.get("pin") and r["pin"]==hp(st.session_state.get("pin","")))
        token_ok=bool(r and saved_token and r["teacher_token"] and saved_token==r["teacher_token"])
        if pin_ok or token_ok:
            st.session_state.code=code
            logout_button("🚪 Log Out of Teacher Room")
            st.divider(); st.subheader(f"Live Room: {code}")
            st.write(f"**Student:** {r['student'] or 'Not named'} | **Role:** {r['role']} | **{r['band']}**")
            st.markdown("### 🎧 Live Interview Audio")
            st.caption("Live audio reconnects automatically while you remain in this room. Use Leave only when you intentionally want to disconnect audio.")
            live_audio_panel(code,"Teacher")

            st.markdown("### 🎛️ Teacher Mock Interview Controls")
            st.caption("These controls are private. Students do not see them; they only experience the interview.")
            try:
                saved_mock_settings=json.loads(r["mock_settings"] or "{}")
            except Exception:
                saved_mock_settings={}
            tc1,tc2,tc3=st.columns(3)
            mock_difficulty=tc1.selectbox("Difficulty",["Standard","Challenging","Very Challenging"],
                index=["Standard","Challenging","Very Challenging"].index(saved_mock_settings.get("difficulty","Standard")))
            mock_scrutiny=tc2.selectbox("Panel scrutiny",["Standard","High","Strict Panel"],
                index=["Standard","High","Strict Panel"].index(saved_mock_settings.get("scrutiny","Strict Panel")))
            mock_max_probes=tc3.slider("Maximum probes per question",1,5,int(saved_mock_settings.get("max_probes",3)))
            tc4,tc5=st.columns(2)
            mock_n_questions=tc4.slider("Number of mock interview questions",3,20,int(saved_mock_settings.get("n_questions",8)))
            mock_ack=tc5.selectbox("AI acknowledgement",["Never","Occasionally","When threshold reached"],
                index=["Never","Occasionally","When threshold reached"].index(saved_mock_settings.get("acknowledgement","Never")))
            ta1,ta2,ta3=st.columns(3)
            mock_talking=ta1.toggle("🔊 Talking AI interviewer",value=bool(saved_mock_settings.get("talking",True)))
            live_voice_enabled=ta1.toggle("🌐 GPT-Live natural voice",value=bool(saved_mock_settings.get("live_voice_enabled",False)),
                help="When enabled, the student can start a metered GPT-Live voice session. This may incur OpenAI API usage charges.")
            mock_delivery=ta2.toggle("🎧 Speaking & delivery analysis",value=bool(saved_mock_settings.get("analyse_delivery",True)))
            mock_star=ta3.toggle("⭐ STAR scrutiny",value=bool(saved_mock_settings.get("star_check",True)))
            tb1,tb2,tb3=st.columns(3)
            mock_safety=tb1.toggle("🛡️ Safety scrutiny",value=bool(saved_mock_settings.get("safety_check",True)))
            mock_contradiction=tb2.toggle("🔁 Contradiction checking",value=bool(saved_mock_settings.get("contradiction_check",True)))
            mock_application=tb3.toggle("📄 Application evidence checking",value=bool(saved_mock_settings.get("application_check",True)))

            current_settings={
                "difficulty":mock_difficulty,"scrutiny":mock_scrutiny,"max_probes":mock_max_probes,
                "n_questions":mock_n_questions,"acknowledgement":mock_ack,"talking":mock_talking,
                "analyse_delivery":mock_delivery,"live_voice_enabled":live_voice_enabled,"live_voice":"marin","star_check":mock_star,"safety_check":mock_safety,
                "contradiction_check":mock_contradiction,"application_check":mock_application
            }
            if current_settings != saved_mock_settings:
                update(code,mock_settings=json.dumps(current_settings))
                r=room(code)

            mc1,mc2=st.columns(2)
            if mc1.button("🎬 Start Teacher-Controlled Mock Interview",type="primary"):
                with st.spinner("Building the vacancy-specific mock interview..."):
                    try:
                        mock_bank=generate_questions(r["role"],r["band"],r["job_advert"] or "",r["job_description"] or "",r["person_spec"] or "",r["application_form"] or "",int(mock_n_questions))
                        ms={"bank":mock_bank,"index":0,"turns":[],"probe":None,"probe_count":0,"answers":[],"finished":False,"report":None}
                        first_q=mock_bank[0].get("question","") if mock_bank else ""
                        update(code,mock_settings=json.dumps(current_settings),mock_state=json.dumps(ms),interview_mode="mock",
                               question=first_q,answer="",audio_data=None,audio_mime=None,audio_name=None,result="",shared=0,status="mock_question_sent")
                        st.success("Mock interview started. The first question is now on the student's Mock Interview screen.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Mock interview setup error: {e}")
            if mc2.button("⏹️ End Mock Interview"):
                update(code,interview_mode="",status="mock_ended")
                st.rerun()

            # Teacher remains the question controller during Mock Interview.
            # This does not change scoring or any other interview behaviour.
            r=room(code)
            if r and r["interview_mode"]=="mock":
                st.markdown("### 🎤 Active Mock — Teacher Question Control")
                st.caption("The AI must use the question currently sent by you. You can replace it with another vacancy-grounded question at any time.")
                teacher_live_q=st.text_area("Current mock question — teacher may edit before sending", value=r["question"] or "", key=f"teacher_live_mock_q_{code}")
                if st.button("📨 Send / Replace Current Mock Question", key=f"send_live_mock_q_{code}"):
                    if teacher_live_q.strip():
                        update(code,question=teacher_live_q.strip(),status="mock_question_sent")
                        # Force a fresh Realtime session so the AI opens with the newly teacher-selected question.
                        st.session_state.pop("live_secret_"+code, None)
                        st.success("Your question is now the active mock-interview question.")
                        st.rerun()

            st.markdown("### 🧠 Adaptive AI Question Bank")
            st.caption("No app-set question-bank limit: generate additional batches whenever you want. Existing questions are kept.")
            nq=st.number_input("Questions to add in this batch",min_value=1,max_value=50,value=10,step=1)
            if st.button("✨ Generate More Questions from Advert + JD + PS + Application",type="primary"):
                with st.spinner("Reading the recruitment documents and building vacancy-specific questions..."):
                    try:
                        existing=json.loads(r["question_bank"] or "[]")
                        more=generate_questions(r["role"],r["band"],r["job_advert"] or "",r["job_description"] or "",r["person_spec"] or "",r["application_form"] or "",int(nq))
                        bank=existing+more
                        update(code,question_bank=json.dumps(bank)); st.rerun()
                    except Exception as e: st.error(f"Question generation error: {e}")
            r=room(code); bank=json.loads(r["question_bank"] or "[]")
            if bank:
                labels=[f"{i+1}. [{x.get('type','question')}] {x.get('question','')}" for i,x in enumerate(bank)]
                chosen=st.selectbox("Teacher question bank",range(len(labels)),format_func=lambda i:labels[i])
                item=bank[chosen]
                st.caption("Why asked: "+str(item.get("why_asked","")))
                st.write("**Source basis:**",item.get("source_basis",""))
                st.write("**Expected concepts:**",", ".join(item.get("expected_concepts",[])))
                q=st.text_area("Selected question — teacher may edit before sending",value=item.get("question",""),key=f"q_{chosen}")
            else:
                q=st.text_area("Manual question (or generate the adaptive question bank above)",value=r["question"] or "")
            c1,c2=st.columns(2)
            if c1.button("📨 Send Selected Question"):
                if q.strip(): update(code,question=q.strip(),answer="",audio_data=None,audio_mime=None,audio_name=None,result="",shared=0,status="question_sent"); st.success("Only this question was sent to the student."); st.rerun()
            if c2.button("🔄 Refresh"): st.rerun()
            r=room(code); st.markdown("### Student answer")
            st.caption("No app-set analysis limit: the teacher can analyse or re-analyse answers whenever needed; API/service limits and costs may still apply.")
            if r["audio_data"]:
                st.markdown("#### 🔊 Student voice recording")
                st.audio(bytes(r["audio_data"]), format=r["audio_mime"] or "audio/webm")
            if r["answer"]:
                st.write(r["answer"])
                if st.button("🧠 Analyse Answer",type="primary"):
                    with st.spinner("Assessing against the question and vacancy..."):
                        try: update(code,result=json.dumps(assess(r["role"],r["band"],r["vacancy"],r["question"],r["answer"])),shared=0,status="assessed"); st.rerun()
                        except Exception as e: st.error(f"Assessment error: {e}")
            else: st.info("Waiting for student answer.")
            r=room(code)
            if r["result"]:
                st.markdown("## 🔒 Private Teacher Result"); show(json.loads(r["result"]))
                if not r["shared"]:
                    if st.button("📤 Share Result with Student"): update(code,shared=1); st.rerun()
                else:
                    st.success("Result is visible to the student.")
                    if st.button("Make Result Private"): update(code,shared=0); st.rerun()

elif mode=="Student":
    st.header("🎤 Student Interview Room")
    initial_student_code=saved_code if saved_mode=="Student" else ""
    code=st.text_input("Enter Student Code",value=initial_student_code).upper().strip()
    if code:
        r=room(code)
        if not r: st.error("Room not found.")
        else:
            if saved_mode!="Student" or saved_code!=code:
                remember_student(code)
            logout_button("🚪 Log Out of Student Room")
            st.markdown("### 🎧 Live Interview Audio")
            st.caption("Live audio reconnects automatically while you remain in this room. When answering, press Start Answer, speak normally, then press Stop & Send Recording. Use Leave only when you intentionally want to disconnect audio.")
            recording=student_live_audio_capture(code)
            if recording:
                rec_id=hashlib.sha256(str(recording.get("data_url","")).encode()).hexdigest()[:16]
                if st.session_state.get(f"processed_recording_{code}") != rec_id:
                    with st.spinner("Transcribing your answer..."):
                        try:
                            audio_bytes,audio_mime=recording_bytes(recording)
                            transcript=transcribe_data_url(recording)
                            st.session_state[f"captured_transcript_{code}"]=transcript
                            st.session_state[f"processed_recording_{code}"]=rec_id
                            if transcript.strip():
                                update(code,answer=transcript.strip(),audio_data=audio_bytes,audio_mime=audio_mime,audio_name="live-answer",result="",shared=0,status="answered")
                                st.success("Your recorded answer was transcribed and sent privately to the teacher.")
                        except Exception as e:
                            st.error(f"Transcription error: {e}")

            if not r["question"]:
                st.info("Waiting for teacher to send a question.")
                if st.button("Refresh"): st.rerun()
            else:
                st.write(f"**Role:** {r['role']} | **{r['band']}**")
                st.markdown("### Interview Question"); st.info(r["question"])
                method=st.radio("Answer using",["🎙️ Live microphone + recording","🎤 Backup voice recorder","⌨️ Type"],horizontal=True)
                if method=="🎙️ Live microphone + recording":
                    st.caption("The recording timer continues until you press Stop & Send Recording. The app does not impose a fixed answer-time cutoff; normal browser/server limits can still apply.")
                    ans=st.text_area("Captured transcript",value=st.session_state.get(f"captured_transcript_{code}",r["answer"] or ""),height=220,help="The transcript appears here after Stop & Send Recording.")
                    if ans.strip() and ans.strip() != (r["answer"] or "").strip():
                        if st.button("Send Edited Transcript to Teacher",type="primary"):
                            update(code,answer=ans.strip(),result="",shared=0,status="answered"); st.success("Edited transcript sent privately to teacher.")
                elif method=="🎤 Backup voice recorder":
                    st.caption("Second voice option: record here. Both the recording and transcript are sent privately to the teacher.")
                    backup_audio=st.audio_input("Record your answer for the teacher",key=f"backup_audio_{code}")
                    if backup_audio and st.button("Send Voice Recording to Teacher",type="primary"):
                        with st.spinner("Saving and transcribing your voice answer..."):
                            try:
                                raw=backup_audio.getvalue()
                                transcript=transcribe(backup_audio)
                                mime=getattr(backup_audio,"type","audio/wav") or "audio/wav"
                                name=getattr(backup_audio,"name","student-answer.wav")
                                update(code,answer=transcript.strip(),audio_data=raw,audio_mime=mime,audio_name=name,result="",shared=0,status="answered")
                                st.session_state[f"captured_transcript_{code}"]=transcript
                                st.success("Your voice recording and transcript were sent privately to the teacher.")
                            except Exception as e:
                                st.error(f"Voice recording error: {e}")
                else:
                    ans=st.text_area("Your answer",height=220)
                    if st.button("Submit Typed Answer to Teacher",type="primary"):
                        if ans.strip():
                            update(code,answer=ans.strip(),audio_data=None,audio_mime=None,audio_name=None,result="",shared=0,status="answered")
                            st.success("Answer sent privately to teacher.")
                        else: st.warning("Type your answer first.")
                if st.button("Refresh Feedback"): st.rerun()
                r=room(code)
                if r["result"] and r["shared"]: st.divider(); st.header("📋 Teacher-Shared Feedback"); show(json.loads(r["result"]))
                elif r["result"]: st.info("Your answer has been assessed. The teacher has not shared the result yet.")

elif mode=="Student Practice":
    st.header("🧑‍🎓 Student Practice")
    is_mock=False
    role=st.text_input("Role")
    band=st.selectbox("Band",["Band 2","Band 3","Band 4","Band 5","Band 6","Band 7","Other"])
    vacancy=st.text_area("Paste Job Advert + JD + Person Specification + Trust/Board values + relevant application evidence",height=220)
    scoring_policy=st.text_area("Employer interview scoring policy/matrix (optional — paste only if verified)",height=100)
    full_vacancy=vacancy+("\n\nEMPLOYER SCORING POLICY / MATRIX:\n"+scoring_policy if scoring_policy.strip() else "")

    st.markdown("### Interview settings")
    c1,c2,c3=st.columns(3)
    difficulty=c1.selectbox("Difficulty",["Standard","Challenging","Very Challenging"])
    scrutiny=c2.selectbox("Panel scrutiny",["Standard","High","Strict Panel"],index=2 if is_mock else 1)
    max_probes=c3.slider("Maximum probes per question",1,5,3)
    n_questions=st.slider("Number of interview questions",3,20,8)

    st.markdown("### Teacher-controlled assessment options")
    a1,a2,a3=st.columns(3)
    talking=a1.toggle("🔊 Talking AI interviewer",value=True)
    analyse_delivery=a2.toggle("🎧 Speaking & delivery analysis",value=True)
    star_check=a3.toggle("⭐ STAR scrutiny",value=True)
    b1,b2,b3=st.columns(3)
    safety_check=b1.toggle("🛡️ Safety scrutiny",value=True)
    contradiction_check=b2.toggle("🔁 Contradiction checking",value=True)
    application_check=b3.toggle("📄 Application evidence checking",value=True)
    acknowledgement=st.selectbox("AI acknowledgement",["Never","Occasionally","When threshold reached"],index=0 if is_mock else 2)
    if is_mock:
        st.info("Mock Interview keeps scores, missing points, keywords and suggested answers hidden until the interview is finished.")

    state_key="mock_state" if is_mock else "practice_state"
    if st.button("🎬 Start "+("Mock Interview" if is_mock else "Practice Session"),type="primary"):
        if not role.strip() or not vacancy.strip(): st.warning("Enter the role and vacancy materials first.")
        else:
            with st.spinner("Building a vacancy-specific interview panel..."):
                try:
                    bank=generate_questions(role,band,vacancy,"",vacancy,"",n_questions)
                    st.session_state[state_key]={"bank":bank,"index":0,"turns":[],"probe":None,"probe_count":0,"answers":[],"finished":False,"report":None}
                    st.rerun()
                except Exception as e: st.error(f"Interview setup error: {e}")

    state=st.session_state.get(state_key)
    if state and not state.get("finished"):
        i=state["index"]; bank=state["bank"]
        if i < len(bank):
            item=bank[i]; base_q=item.get("question","")
            current_q=state.get("probe") or base_q
            st.divider(); st.subheader(f"Question {i+1} of {len(bank)}")
            if is_mock: st.info(current_q)
            else:
                st.info(current_q)
                if not state.get("probe"): st.caption("Practice mode: feedback and teaching are available after your attempt.")
            if talking: speak_text(current_q,f"speak_{i}_{state.get('probe_count',0)}")
            audio=st.audio_input("🎙️ Record your answer",key=f"aud_{state_key}_{i}_{state.get('probe_count',0)}")
            typed=st.text_area("Or type your answer",key=f"txt_{state_key}_{i}_{state.get('probe_count',0)}",height=150)
            if st.button("Submit Answer",type="primary",key=f"submit_{state_key}_{i}_{state.get('probe_count',0)}"):
                ans=typed.strip(); duration=None
                if audio and not ans:
                    with st.spinner("Transcribing your answer..."):
                        ans=transcribe(audio).strip()
                if not ans: st.warning("Record or type an answer first.")
                else:
                    with st.spinner("Panel is scrutinising your answer..."):
                        try:
                            check=scrutinize(role,band,full_vacancy,base_q,ans,state.get("probe_count",0),max_probes,practice=not is_mock,star_check=star_check,safety_check=safety_check,contradiction_check=contradiction_check,application_check=application_check,scrutiny_level=scrutiny)
                            if state.get("probe") is None:
                                qrec={"question":base_q,"initial_answer":ans,"initial_evidence_percent":check.get("completion_percent",0),"responses":[{"prompt":base_q,"answer":ans,"assessment":check}],"probes_required":0}
                                state["turns"].append(qrec)
                            else:
                                qrec=state["turns"][-1]; qrec["responses"].append({"prompt":current_q,"answer":ans,"assessment":check})
                            state["answers"].append(ans)
                            probe=check.get("probe_question","").strip()
                            can_probe=state.get("probe_count",0)<max_probes
                            if check.get("earned_well_done") or check.get("status")=="complete":
                                qrec["final_evidence_percent"]=check.get("completion_percent",0)
                                if not is_mock: st.success("Well done. You have now covered the key evidence required for this question.")
                                state["index"]+=1; state["probe"]=None; state["probe_count"]=0
                                if state["index"]>=len(bank): state["finished"]=True
                                st.session_state[state_key]=state; st.rerun()
                            elif probe and can_probe:
                                state["probe_count"]+=1; qrec["probes_required"]=state["probe_count"]; state["probe"]=probe
                                st.session_state[state_key]=state; st.rerun()
                            else:
                                qrec["final_evidence_percent"]=check.get("completion_percent",0)
                                state["index"]+=1; state["probe"]=None; state["probe_count"]=0
                                if state["index"]>=len(bank): state["finished"]=True
                                st.session_state[state_key]=state; st.rerun()
                        except Exception as e: st.error(f"Panel analysis error: {e}")

    state=st.session_state.get(state_key)
    if state and state.get("finished"):
        st.success("Thank you. That concludes your "+("mock interview." if is_mock else "practice session."))
        if talking: speak_text("Thank you. That concludes your mock interview." if is_mock else "Well done. That concludes your practice session.","closing")
        if state.get("report") is None:
            if st.button("📊 Generate Final Panel Report",type="primary"):
                with st.spinner("Preparing evidence-based panel report..."):
                    try:
                        joined=" ".join(state.get("answers",[])); metrics=delivery_metrics(joined,None) if analyse_delivery else {}
                        state["report"]=final_panel_report(role,band,full_vacancy,state.get("turns",[]),metrics)
                        st.session_state[state_key]=state; st.rerun()
                    except Exception as e: st.error(f"Report error: {e}")
        else:
            show_panel_report(state["report"])
            if analyse_delivery:
                st.markdown("### 🎧 Observable speaking/delivery indicators")
                st.write(delivery_metrics(" ".join(state.get("answers",[])),None))
            if st.button("Start New Session"):
                del st.session_state[state_key]; st.rerun()


elif mode=="Mock Interview":
    st.header("🎤 Student Mock Interview")
    st.caption("Your teacher controls the mock interview privately. Assessment settings, scoring, keywords and coaching controls are hidden from the student.")
    code=st.text_input("Enter Student Code",key="mock_student_code").upper().strip()
    if code:
        r=room(code)
        if not r:
            st.error("Room not found.")
        else:
            st.write(f"**Role:** {r['role']} | **{r['band']}**")
            if r["interview_mode"]!="mock":
                st.info("Waiting for the teacher to start your mock interview.")
                if st.button("Refresh Mock Interview"): st.rerun()
            else:
                try:
                    settings=json.loads(r["mock_settings"] or "{}")
                    ms=json.loads(r["mock_state"] or "{}")
                except Exception:
                    settings={}; ms={}
                talking=bool(settings.get("talking",True))
                max_probes=int(settings.get("max_probes",3))
                scrutiny=settings.get("scrutiny","Strict Panel")
                star_check=bool(settings.get("star_check",True))
                safety_check=bool(settings.get("safety_check",True))
                contradiction_check=bool(settings.get("contradiction_check",True))
                application_check=bool(settings.get("application_check",True))
                acknowledgement=settings.get("acknowledgement","Never")

                if ms.get("finished"):
                    st.success("Thank you. That concludes your mock interview.")
                    if talking:
                        speak_text("Thank you. That concludes your mock interview.",f"mock_close_{code}")
                    st.caption("Your teacher can review the private panel assessment.")
                else:
                    q=(r["question"] or "").strip()
                    if q:
                        idx=int(ms.get("index",0))
                        bank=ms.get("bank",[])
                        st.markdown(f"### Question {min(idx+1,len(bank)) if bank else idx+1}")
                        st.info(q)
                        if talking:
                            speak_text(q,f"mock_speak_{code}_{idx}_{ms.get('probe_count',0)}")

                        if bool(settings.get("live_voice_enabled",False)):
                            st.markdown("### 🗣️ Natural Live Interview")
                            st.caption("Press Start live interview once. After that, speak naturally; the AI uses voice activity detection for turn-taking and can respond without a Submit button.")
                            if "live_secret_"+code not in st.session_state:
                                if st.button("Enable natural live voice", type="primary", key=f"enable_live_{code}"):
                                    with st.spinner("Preparing secure live voice session..."):
                                        try:
                                            vacancy_context=(r["vacancy"] or "")[:8000]
                                            live_instructions=f"""You are the natural-voice interviewer for Interview Coach Pro.
Conduct a professional NHS-style interview for {r['role']} {r['band']}.
The teacher controls the interview. Never reveal private scoring, keywords, model answers, assessment settings, or hidden criteria to the student.
Ask one question at a time. Listen fully. When the student finishes, respond naturally.
If the answer is incomplete, vague, contradictory, unsafe, or needs evidence, ask one concise targeted follow-up.
Otherwise move naturally to the next appropriate question.
Do not coach during Mock Interview. Do not tell the student their score.
Speak in natural British English with a calm, professional UK interview-panel manner. Use ordinary British pronunciation and vocabulary without exaggerating the accent. Be warm, professional, concise, and human-sounding.
Vacancy context:
{vacancy_context}
Current teacher-selected difficulty: {settings.get('difficulty','Standard')}
Current panel scrutiny: {settings.get('scrutiny','Strict Panel')}
Maximum probes per question: {settings.get('max_probes',3)}
"""
                                            st.session_state["live_secret_"+code]=create_realtime_client_secret(
                                                live_instructions, settings.get("live_voice","marin"))
                                            st.rerun()
                                        except Exception as e:
                                            st.error(str(e))
                            else:
                                try:
                                    lk_url, lk_token_value = livekit_token(code, f"mock-student-{uuid.uuid4().hex[:10]}", can_publish=True, can_subscribe=True)
                                except Exception as e:
                                    lk_url, lk_token_value = "", ""
                                    st.warning(f"Teacher room audio is unavailable: {e}")
                                live_voice_component(st.session_state["live_secret_"+code], q, code, settings.get("live_voice","marin"), lk_url, lk_token_value)
                                st.caption("Live voice is metered only while a Realtime session is actually connected. The teacher can join the same room to hear the student and AI, and speak to the student.")
                            st.divider()
                            st.caption("Recorded-answer fallback")

                        audio=st.audio_input("🎙️ Record your answer",key=f"mock_audio_{code}_{idx}_{ms.get('probe_count',0)}")
                        typed=st.text_area("Or type your answer",height=150,key=f"mock_text_{code}_{idx}_{ms.get('probe_count',0)}")
                        if st.button("Submit Answer",type="primary",key=f"mock_submit_{code}_{idx}_{ms.get('probe_count',0)}"):
                            ans=typed.strip()
                            raw=None; mime=None
                            if audio and not ans:
                                with st.spinner("Transcribing your answer..."):
                                    raw=audio.getvalue()
                                    mime=getattr(audio,"type","audio/wav") or "audio/wav"
                                    ans=transcribe(audio).strip()
                            if not ans:
                                st.warning("Record or type an answer first.")
                            else:
                                with st.spinner("Submitting your answer to the interview panel..."):
                                    try:
                                        base_q=bank[idx].get("question","") if bank and idx < len(bank) else q
                                        check=scrutinize(r["role"],r["band"],r["vacancy"],base_q,ans,
                                            ms.get("probe_count",0),max_probes,practice=False,
                                            star_check=star_check,safety_check=safety_check,
                                            contradiction_check=contradiction_check,
                                            application_check=application_check,scrutiny_level=scrutiny)

                                        if ms.get("probe") is None:
                                            qrec={"question":base_q,"initial_answer":ans,
                                                  "initial_evidence_percent":check.get("completion_percent",0),
                                                  "responses":[{"prompt":q,"answer":ans,"assessment":check}],
                                                  "probes_required":0}
                                            ms.setdefault("turns",[]).append(qrec)
                                        else:
                                            qrec=ms["turns"][-1]
                                            qrec.setdefault("responses",[]).append({"prompt":q,"answer":ans,"assessment":check})
                                        ms.setdefault("answers",[]).append(ans)

                                        probe=(check.get("probe_question") or "").strip()
                                        can_probe=int(ms.get("probe_count",0)) < max_probes
                                        completed=bool(check.get("earned_well_done") or check.get("status")=="complete")

                                        if (not completed) and probe and can_probe:
                                            ms["probe_count"]=int(ms.get("probe_count",0))+1
                                            qrec["probes_required"]=ms["probe_count"]
                                            ms["probe"]=probe
                                            next_q=probe
                                        else:
                                            qrec["final_evidence_percent"]=check.get("completion_percent",0)
                                            ms["index"]=idx+1
                                            ms["probe"]=None
                                            ms["probe_count"]=0
                                            if ms["index"] >= len(bank):
                                                ms["finished"]=True
                                                next_q=""
                                            else:
                                                next_q=bank[ms["index"]].get("question","")

                                        # Private teacher result: never shown to the mock student.
                                        private_result={
                                            "mock_panel_check":check,
                                            "question_number":idx+1,
                                            "probe_count":qrec.get("probes_required",0),
                                            "student_answer":ans
                                        }
                                        update(code,answer=ans,audio_data=raw,audio_mime=mime,audio_name="mock-answer" if raw else None,
                                               result=json.dumps(private_result),shared=0,
                                               mock_state=json.dumps(ms),question=next_q,
                                               status="mock_finished" if ms.get("finished") else "mock_question_sent")
                                        if completed and acknowledgement=="When threshold reached":
                                            st.success("Thank you. That covers the question.")
                                        else:
                                            st.success("Answer submitted.")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Mock interview processing error: {e}")
                    else:
                        st.info("Waiting for the next interview question.")
                        if st.button("Refresh"): st.rerun()
