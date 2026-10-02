import React, {useEffect, useRef, useState} from "react";
import {createRoot} from "react-dom/client";
import "./style.css";

const API = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000/api";

type Model = {id:string;provider:string;name:string;category:string;modalities:string[];languages:string;status:string;selectable:boolean;default?:boolean};

function App(){
  const [models,setModels]=useState<Model[]>([]);
  const [sessions,setSessions]=useState<any[]>([]);
  const [active,setActive]=useState<any>(null);
  const [messages,setMessages]=useState<any[]>([]);
  const [provider,setProvider]=useState("nvidia");
  const [model,setModel]=useState("");
  const [query,setQuery]=useState("");
  const [docs,setDocs]=useState<any[]>([]);
  const [jobs,setJobs]=useState<Record<string,any>>({});
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState("");
  const input=useRef<HTMLTextAreaElement>(null);

  useEffect(()=>{
    Promise.all([
      fetch(API+"/models").then(r=>r.json()),
      fetch(API+"/sessions").then(r=>r.json())
    ]).then(([m,s])=>{
      setModels(m); setSessions(s);
      const server=m.find((x:Model)=>x.default); if(server){setProvider(server.provider); setModel(server.id);}
      if(s.length) selectSession(s[0]);
    });
  },[]);

  async function selectSession(x:any){
    setActive(x);
    const [msg,documents]=await Promise.all([
      fetch(`${API}/sessions/${x.id}/messages`).then(r=>r.json()),
      fetch(`${API}/sessions/${x.id}/documents`).then(r=>r.json())
    ]);
    setMessages(msg); setDocs(documents);
  }

  async function newChat(){
    const x=await fetch(API+"/sessions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:"New chat"})}).then(r=>r.json());
    setSessions(s=>[x,...s]); setActive(x); setMessages([]); setDocs([]);
  }

  async function send(){
    if(!query.trim()||!active||busy) return;
    const q=query.trim(); setQuery(""); setBusy(true);
    setMessages(m=>[...m,{role:"user",content:q}]);
    try{
      const r=await fetch(API+"/chat",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({session_id:active.id,content:q,provider,model})});
      const d=await r.json();
      if(!r.ok) throw new Error(d.detail||"The request could not be completed.");
      setMessages(m=>[...m,{...(d.message||{role:"assistant",content:"The request could not be completed."}),route:d.route,citations:d.citations||[]}]);
    } catch(e:any) {
      setError(e.message||"The request could not be completed.");
    } finally {setBusy(false); input.current?.focus();}
  }

  async function upload(e:any){
    const f=e.target.files?.[0]; e.target.value="";
    if(!f||!active)return;
    const fd=new FormData(); fd.append("session_id",active.id); fd.append("file",f);
    const r=await fetch(API+"/uploads",{method:"POST",body:fd});
    const d=await r.json();
    if(!r.ok){setError(d.detail||"Upload failed.");return;}
    if(d.job){
      setJobs(j=>({...j,[d.job.id]:d.job}));
      pollJob(d.job.id);
    }
  }

  async function pollJob(id:string){
    const tick=async()=>{
      const j=await fetch(`${API}/jobs/${id}`).then(r=>r.json());
      setJobs(x=>({...x,[id]:j}));
      if(["queued","running"].includes(j.status)){
        setTimeout(tick,1200);
      } else {   // completed, failed, or paused (awaiting confirmation / waiting for quota)
        const documents=await fetch(`${API}/sessions/${active.id}/documents`).then(r=>r.json());
        setDocs(documents);
      }
    };
    tick();
  }

  // A document can wait for a decision (confirm a large OCR/transcription job) or for a free-tier quota to reset.
  const actions:Record<string,{label:string;action:string}[]>={
    awaiting_confirmation:[{label:"Process",action:"confirm"},{label:"Skip",action:"skip"}],
    waiting_for_quota:[{label:"Retry",action:"retry"},{label:"Skip",action:"skip"}],
    ocr_unavailable:[{label:"Retry OCR",action:"retry"}],
    audio_unavailable:[{label:"Retry",action:"retry"}],
  };
  async function processDoc(doc:any,action:string){
    const r=await fetch(`${API}/documents/${doc.id}/process`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});
    const d=await r.json();
    if(!r.ok){setError(d.detail||"Could not continue this document.");return;}
    setJobs(j=>({...j,[d.job.id]:d.job}));
    pollJob(d.job.id);
  }

  const providers=[...new Set(models.filter(m=>m.selectable).map(m=>m.provider))];
  const options=models.filter(m=>m.provider===provider&&m.selectable);
  useEffect(()=>{
    if(options.length&&!options.some(m=>m.id===model)) setModel(options[0].id);
  },[provider,models]);

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand">KNAVIS <span>v0.3</span></div>
      <button className="new" onClick={newChat}>＋ New chat</button>
      <div className="section-title">Chats</div>
      <div className="sessions">{sessions.map(x=><button key={x.id} className={active?.id===x.id?"session active":"session"} onClick={()=>selectSession(x)}>{x.title}</button>)}</div>
      <div className="section-title">Documents</div>
      <div className="documents">{docs.map(d=><div className="doc" key={d.id}>
        <b>{d.filename}</b><small>{d.status.replace(/_/g," ")}{d.details?.chunks!=null?` · ${d.details.chunks} chunks`:""}</small>
        {d.details?.pause?.message&&<small className="pause">{d.details.pause.message}</small>}
        {actions[d.status]&&<div className="doc-actions">{actions[d.status].map(a=><button key={a.action} onClick={()=>processDoc(d,a.action)}>{a.label}</button>)}</div>}
      </div>)}</div>
    </aside>

    <main className="main">
      <header className="topbar">
        <div><strong>{active?.title||"KNAVIS"}</strong><small>Evidence-grounded · multilingual · agentic</small></div>
        <div className="selectors">
          <select value={provider} onChange={e=>setProvider(e.target.value)}>
            {providers.map(p=><option key={p} value={p}>{p}</option>)}
          </select>
          <select value={model} onChange={e=>setModel(e.target.value)}>
            {options.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </div>
      </header>

      <section className="chat">
        {messages.length===0 && <div className="empty"><h1>Ask your documents.</h1><p>Upload a file, then ask a grounded question. Ordinary conversation is handled separately.</p></div>}
        {messages.map((m,i)=><div key={i} className={`msg ${m.role}`}><div className="bubble"><div>{m.content}</div>{m.citations?.length>0&&<div className="citations">{m.citations.map((c:any,j:number)=><span key={j}>{c.source}{c.locator?` · ${c.locator}`:(c.page?` · p.${c.page}`:"")}{c.evidence_id?` · #${c.evidence_id}`:""}</span>)}</div>}</div></div>)}
        {busy&&<div className="msg assistant"><div className="bubble muted">Thinking with evidence…</div></div>}
      </section>

      <footer className="composer">
        <div className="jobbar">{Object.values(jobs).map((j:any)=><span key={j.id} className={j.status==="failed"?"error":""}>{j.stage} · {j.progress}%</span>)}</div>
        {error&&<div className="request-error">{error}</div>}
        <div className="compose-row">
          <label className="attach">📎<input type="file" onChange={upload}/></label>
          <textarea ref={input} value={query} onChange={e=>setQuery(e.target.value)}
            onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}}}
            placeholder="Ask in English, Hindi, Hinglish, or another supported language…"/>
          <button className="send" onClick={send} disabled={busy}>Send</button>
        </div>
        <small className="disclaimer">Knowledge answers are evidence-gated. If the evidence is insufficient, the system abstains.</small>
      </footer>
    </main>
  </div>
}
createRoot(document.getElementById("root")!).render(<App/>);
