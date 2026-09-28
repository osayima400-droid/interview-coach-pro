import streamlit as st
from openai import OpenAI
import os, json, sqlite3, uuid, hashlib, html, base64, io
from datetime import datetime
import streamlit.components.v1 as components

st.set_page_config(page_title="Interview Coach Pro", page_icon="🎓", layout="wide")
DB="interview_coach.db"

def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS rooms(code TEXT PRIMARY KEY,pin TEXT,student TEXT,role TEXT,band TEXT,vacancy TEXT,question TEXT,answer TEXT,result TEXT,shared INTEGER DEFAULT 0,status TEXT,updated TEXT)""")
    for col in ["job_advert TEXT", "job_description TEXT", "person_spec TEXT", "application_form TEXT", "question_bank TEXT", "audio_data BLOB", "audio_mime TEXT", "audio_name TEXT", "teacher_token TEXT"]:
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
          setStateValue('recording',{data_url:reader.result,mime:blob.type,size:blob.size});
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

If the supplied Trust/Board documents explicitly state their own interview scoring scale, weighting, values framework, competency framework, pass rule, or question-specific scoring instructions, FOLLOW THAT documented method instead of assuming a generic NHS method. Explain the applicable scale in scoring_note.

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
trust_or_board, question_type, panel_is_testing, trust_requirements_used, marking_matrix, essential_criteria_relevant, desirable_criteria_relevant, values_relevant, likely_keywords, vacancy_matches, nhs_score, max_score, overall_score, verdict, correctness, safety_status, score_breakdown, criteria_met, criteria_not_met, strengths, missing_points, improvements, framework, suggested_answer, follow_up_questions, scoring_note.

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
    return result

def show(r):
    cscore,cpercent=st.columns(2)
    cscore.metric("NHS-style question score",f"{int(r.get('nhs_score',0))}/{int(r.get('max_score',5))}")
    cpercent.metric("Equivalent percentage",f"{int(r.get('overall_score',0))}/100")
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
st.caption("Teacher-controlled interview room • Live two-way audio • Timed voice capture • NHS-style evidence scoring • Vacancy-specific AI assessment")
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
        st.markdown("### Recruitment documents")
        advert=source_box("Job Advert","advert")
        jd=source_box("Job Description (JD)","jd")
        ps=source_box("Person Specification (PS)","ps")
        application=source_box("Candidate Application Form / Supporting Information","application")
        pin=st.text_input("Create private Teacher PIN",type="password")
        if st.button("Create Interview Room",type="primary"):
            if not role or not pin: st.warning("Enter the role and Teacher PIN.")
            else:
                code=uuid.uuid4().hex[:6].upper(); c=conn()
                combined="\n\n".join(["JOB ADVERT:\n"+advert,"JOB DESCRIPTION:\n"+jd,"PERSON SPECIFICATION:\n"+ps,"APPLICATION FORM:\n"+application])
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

else:
    st.header("🧑‍🎓 "+mode)
    role=st.text_input("Role"); band=st.selectbox("Band",["Band 2","Band 3","Band 4","Band 5","Band 6","Band 7","Other"])
    vacancy=st.text_area("Paste Job Advert / JD / PS / relevant application evidence",height=200)
    question=st.text_area("Interview question")
    method=st.radio("Answer using",["🎙️ Microphone","⌨️ Type"],horizontal=True)
    if method=="🎙️ Microphone":
        audio=st.audio_input("Record your answer")
        if audio and st.button("Transcribe"):
            with st.spinner("Transcribing..."):
                try: st.session_state.pt=transcribe(audio)
                except Exception as e: st.error(f"Transcription error: {e}")
        ans=st.text_area("Transcript",value=st.session_state.get("pt",""),height=200)
    else: ans=st.text_area("Your answer",height=200)
    if st.button("Analyse My Answer",type="primary"):
        if not question.strip() or not ans.strip(): st.warning("Enter a question and answer.")
        else:
            with st.spinner("Analysing..."):
                try: show(assess(role,band,vacancy,question,ans))
                except Exception as e: st.error(f"Assessment error: {e}")
