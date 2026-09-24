import streamlit as st
from openai import OpenAI
import os, json, sqlite3, uuid, hashlib
from datetime import datetime

st.set_page_config(page_title="Interview Coach Pro", page_icon="🎓", layout="wide")
DB="interview_coach.db"

def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS rooms(code TEXT PRIMARY KEY,pin TEXT,student TEXT,role TEXT,band TEXT,vacancy TEXT,question TEXT,answer TEXT,result TEXT,shared INTEGER DEFAULT 0,status TEXT,updated TEXT)""")
    
    for col in ["job_advert TEXT", "job_description TEXT", "person_spec TEXT", "application_form TEXT", "question_bank TEXT"]:
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

def client():
    if not os.getenv("OPENAI_API_KEY"):
        st.error("OPENAI_API_KEY is not available on this computer/server."); st.stop()
    return OpenAI()

def transcribe(audio):
    data=audio.getvalue()
    out=client().audio.transcriptions.create(model="gpt-4o-mini-transcribe",file=(getattr(audio,"name","answer.wav"),data,getattr(audio,"type","audio/wav")))
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
    instructions="""You are a rigorous healthcare interview assessor. Assess only against the actual question and supplied vacancy/JD/person specification. First classify the question. Do not force STAR onto motivation, knowledge or other unsuitable questions. Apply clinical safety, escalation and scope criteria only when relevant. Empty answers score 0; very short or vague answers score very low. Keyword stuffing without meaningful evidence must not score highly. Unsafe clinical answers must be flagged and overall score capped. Separate factual/clinical correctness from interview effectiveness. Never invent candidate experience. When a genuine example is absent, provide a framework/template, not a fabricated event. Return JSON only with keys: question_type, panel_is_testing, likely_keywords, vacancy_matches, overall_score, verdict, correctness, safety_status, score_breakdown, strengths, missing_points, improvements, framework, suggested_answer, follow_up_questions. overall_score is 0-100. verdict is one of Strong / Interview Ready; Good but Can Be Strengthened; Partially Correct; Significant Improvement Needed; Safety-Critical Concern."""
    prompt=f"""ROLE: {role}
BAND: {band}
VACANCY/JD/PERSON SPECIFICATION:
{vacancy or "Not supplied"}
QUESTION:
{question}
CANDIDATE ANSWER:
{answer}
Assess fairly, specifically and evidence-first."""
    res=client().responses.create(model="gpt-5.6",instructions=instructions,input=prompt)
    text=res.output_text.strip()
    if text.startswith("```"): text=text.split("\n",1)[1].rsplit("```",1)[0].strip()
    return json.loads(text)

def show(r):
    st.metric("Overall score",f"{int(r.get('overall_score',0))}/100")
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
    st.markdown("### Detailed assessment")
    st.write("**Question type:**",r.get("question_type",""))
    st.write("**Panel is testing:**",r.get("panel_is_testing",""))
    st.write("**Correctness:**",r.get("correctness",""))
    st.write("**Safety:**",r.get("safety_status",""))
    st.write("**Score breakdown:**",r.get("score_breakdown",{}))
    st.write("**Expected concepts/keywords:**",", ".join(r.get("likely_keywords",[])))
    st.markdown("### Recommended framework"); st.write(r.get("framework",""))
    st.markdown("### Stronger answer"); st.write(r.get("suggested_answer",""))
    st.markdown("### Likely follow-up questions")
    for x in r.get("follow_up_questions",[]): st.write("•",x)

st.title("🎓 Interview Coach Pro")
st.caption("Teacher-controlled interview room • Student voice answers • Vacancy-specific AI assessment")
mode=st.sidebar.radio("Open as",["Teacher","Student","Student Practice","Mock Interview"])

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
                c.commit(); c.close(); st.session_state.code=code; st.session_state.pin=pin
                st.success(f"Room created. Student Code: {code}")
                st.info("Give the student only this code. Keep your Teacher PIN private.")
    with t2:
        oc=st.text_input("Room code").upper().strip(); op=st.text_input("Teacher PIN",type="password",key="op")
        if st.button("Open Dashboard"):
            r=room(oc)
            if r and r["pin"]==hp(op): st.session_state.code=oc; st.session_state.pin=op; st.rerun()
            else: st.error("Incorrect room code or Teacher PIN.")
    code=st.session_state.get("code")
    if code:
        r=room(code)
        if r and r["pin"]==hp(st.session_state.get("pin","")):
            st.divider(); st.subheader(f"Live Room: {code}")
            st.write(f"**Student:** {r['student'] or 'Not named'} | **Role:** {r['role']} | **{r['band']}**")
            st.markdown("### 🧠 Adaptive AI Question Bank")
            nq=st.slider("Number of questions to generate",5,20,10)
            if st.button("✨ Generate Questions from Advert + JD + PS + Application",type="primary"):
                with st.spinner("Reading the recruitment documents and building vacancy-specific questions..."):
                    try:
                        bank=generate_questions(r["role"],r["band"],r["job_advert"] or "",r["job_description"] or "",r["person_spec"] or "",r["application_form"] or "",nq)
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
                if q.strip(): update(code,question=q.strip(),answer="",result="",shared=0,status="question_sent"); st.success("Only this question was sent to the student."); st.rerun()
            if c2.button("🔄 Refresh"): st.rerun()
            r=room(code); st.markdown("### Student answer")
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
    code=st.text_input("Enter Student Code").upper().strip()
    if code:
        r=room(code)
        if not r: st.error("Room not found.")
        elif not r["question"]:
            st.info("Waiting for teacher to send a question.")
            if st.button("Refresh"): st.rerun()
        else:
            st.write(f"**Role:** {r['role']} | **{r['band']}**")
            st.markdown("### Interview Question"); st.info(r["question"])
            method=st.radio("Answer using",["🎙️ Microphone","⌨️ Type"],horizontal=True)
            if method=="🎙️ Microphone":
                audio=st.audio_input("Record your answer")
                if audio:
                    st.audio(audio)
                    if st.button("Transcribe My Answer"):
                        with st.spinner("Transcribing..."):
                            try: st.session_state.transcript=transcribe(audio)
                            except Exception as e: st.error(f"Transcription error: {e}")
                ans=st.text_area("Review your transcript",value=st.session_state.get("transcript",""),height=220)
            else: ans=st.text_area("Your answer",height=220)
            if st.button("Submit Answer to Teacher",type="primary"):
                if ans.strip(): update(code,answer=ans.strip(),result="",shared=0,status="answered"); st.success("Answer sent privately to teacher.")
                else: st.warning("Record or type your answer first.")
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
