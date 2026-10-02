import React, {useCallback, useEffect, useRef, useState} from "react";
import {createRoot} from "react-dom/client";
import "./style.css";
import {api, getToken, handleUnauthorized, setToken} from "./api";
import {AuthScreen} from "./AuthScreen";
import {Answer, Citation} from "./Answer";

type Model = {id:string;provider:string;name:string;category:string;modalities:string[];languages:string;status:string;selectable:boolean;default?:boolean};
type User = {id:string|null;email:string|null};

// What a person sees for each document state (the raw states are internal names).
const STATUS_LABEL:Record<string,string> = {
  indexed:"ready", queued:"processing…", uploaded:"processing…", awaiting_confirmation:"needs your OK",
  waiting_for_quota:"waiting for free quota", ocr_unavailable:"needs OCR set up", audio_unavailable:"needs transcription set up",
  no_text:"no readable text", failed:"failed",
};
// A document can wait for a decision (confirm a large OCR/transcription job) or for a free-tier quota to reset.
const ACTIONS:Record<string,{label:string;action:string}[]> = {
  awaiting_confirmation:[{label:"Process",action:"confirm"},{label:"Skip",action:"skip"}],
  waiting_for_quota:[{label:"Retry",action:"retry"},{label:"Skip",action:"skip"}],
  ocr_unavailable:[{label:"Retry OCR",action:"retry"}],
  audio_unavailable:[{label:"Retry",action:"retry"}],
};

function App(){
  const [auth,setAuth]=useState<{enabled:boolean;registrationOpen:boolean;passwordReset:boolean}|null>(null);
  const [user,setUser]=useState<User|null>(null);
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
  const [uploading,setUploading]=useState(false);
  const [error,setError]=useState("");
  const [menuOpen,setMenuOpen]=useState(false);
  const input=useRef<HTMLTextAreaElement>(null);
  const bottom=useRef<HTMLDivElement>(null);
  const activeId=useRef<string|null>(null);
  activeId.current=active?.id??null;

  const signedOut=useCallback(()=>{
    setToken(null); setUser(null); setSessions([]); setActive(null); setMessages([]); setDocs([]); setJobs({}); setError("");
  },[]);
  useEffect(()=>{ handleUnauthorized(signedOut); },[signedOut]);

  // Find out whether this server uses accounts, then whether we are already signed in. Nothing is shown until both
  // are known, so a signed-in person never sees the sign-in form flash by.
  useEffect(()=>{
    (async()=>{
      try{
        const c=await api("/auth/config");
        let who:User|null=null;
        if(!c.auth_enabled) who={id:null,email:null};
        else if(getToken()){ try{ who=await api("/auth/me"); }catch{ setToken(null); } }
        setUser(who);
        setAuth({enabled:c.auth_enabled,registrationOpen:c.registration_open,passwordReset:!!c.password_reset});
      }catch(e:any){ setError(e.message); }
    })();
  },[]);

  // Once signed in, load the models and the person's chats.
  useEffect(()=>{
    if(!user) return;
    (async()=>{
      try{
        const [m,s]=await Promise.all([api("/models"),api("/sessions")]);
        setModels(m); setSessions(s);
        const server=m.find((x:Model)=>x.default); if(server){setProvider(server.provider); setModel(server.id);}
        if(s.length) selectSession(s[0]);
      }catch(e:any){ setError(e.message); }
    })();
  },[user]);

  useEffect(()=>{ bottom.current?.scrollIntoView({block:"end"}); },[messages,busy]);

  async function refreshDocs(id:string){
    const documents=await api(`/sessions/${id}/documents`);
    if(activeId.current===id) setDocs(documents);
    return documents;
  }

  async function selectSession(x:any){
    setActive(x); setMenuOpen(false); setError("");
    try{
      const [msg,documents]=await Promise.all([api(`/sessions/${x.id}/messages`),api(`/sessions/${x.id}/documents`)]);
      if(activeId.current===x.id){ setMessages(msg); setDocs(documents); }
    }catch(e:any){ setError(e.message); }
  }

  async function newChat(){
    try{
      const x=await api("/sessions",{method:"POST",json:{title:"New chat"}});
      setSessions(s=>[x,...s]); setActive(x); setMessages([]); setDocs([]); setMenuOpen(false);
    }catch(e:any){ setError(e.message); }
  }

  async function renameChat(){
    if(!active) return;
    const title=window.prompt("Rename this chat",active.title)?.trim();
    if(!title||title===active.title) return;
    try{
      const x=await api(`/sessions/${active.id}`,{method:"PATCH",json:{title}});
      setActive(x); setSessions(s=>s.map(y=>y.id===x.id?x:y));
    }catch(e:any){ setError(e.message); }
  }

  async function deleteChat(x:any){
    if(!window.confirm(`Delete "${x.title}" with its messages and documents? This cannot be undone.`)) return;
    try{
      await api(`/sessions/${x.id}`,{method:"DELETE"});
      const rest=sessions.filter(y=>y.id!==x.id);
      setSessions(rest);
      if(active?.id===x.id){ rest.length?selectSession(rest[0]):(setActive(null),setMessages([]),setDocs([])); }
    }catch(e:any){ setError(e.message); }
  }

  async function deleteDoc(d:any){
    if(!window.confirm(`Remove "${d.filename}" from this chat?`)) return;
    try{ await api(`/documents/${d.id}`,{method:"DELETE"}); setDocs(x=>x.filter(y=>y.id!==d.id)); }
    catch(e:any){ setError(e.message); }
  }

  async function send(){
    if(!query.trim()||!active||busy) return;
    const q=query.trim(); setQuery(""); setBusy(true); setError("");
    setMessages(m=>[...m,{role:"user",content:q}]);
    try{
      const d=await api("/chat",{method:"POST",json:{session_id:active.id,content:q,provider,model}});
      setMessages(m=>[...m,{...d.message,route:d.route,citations:d.citations||[]}]);
    } catch(e:any) {
      setError(e.message||"The request could not be completed.");
    } finally {setBusy(false); input.current?.focus();}
  }

  async function upload(e:React.ChangeEvent<HTMLInputElement>){
    const f=e.target.files?.[0]; e.target.value="";
    if(!f||!active)return;
    const fd=new FormData(); fd.append("session_id",active.id); fd.append("file",f);
    setUploading(true); setError("");
    try{
      const d=await api("/uploads",{method:"POST",body:fd});
      await refreshDocs(active.id);
      if(d.duplicate) setError(`"${f.name}" is already in this chat.`);
      else if(d.job){ setJobs(j=>({...j,[d.job.id]:d.job})); pollJob(d.job.id,active.id); }
    }catch(err:any){ setError(err.message||"Upload failed."); }
    finally{ setUploading(false); }
  }

  async function pollJob(id:string,sessionId:string){
    const tick=async()=>{
      try{
        const j=await api(`/jobs/${id}`);
        setJobs(x=>({...x,[id]:j}));
        if(["queued","running"].includes(j.status)){ setTimeout(tick,1200); return; }
        // completed, failed, or paused (awaiting confirmation / waiting for quota)
        if(activeId.current===sessionId) await refreshDocs(sessionId);
        setTimeout(()=>setJobs(x=>{const {[id]:_,...rest}=x; return rest;}),4000);
      }catch{ /* the job vanished (document deleted); stop polling */ }
    };
    tick();
  }

  async function processDoc(doc:any,action:string){
    try{
      const d=await api(`/documents/${doc.id}/process`,{method:"POST",json:{action}});
      setJobs(j=>({...j,[d.job.id]:d.job}));
      await refreshDocs(doc.session_id??active.id);
      pollJob(d.job.id,active.id);
    }catch(e:any){ setError(e.message||"Could not continue this document."); }
  }

  async function signOut(){
    try{ await api("/auth/logout",{method:"POST"}); }catch{ /* already signed out */ }
    signedOut();
  }

  async function changePassword(){
    const current_password=window.prompt("Your current password");
    if(!current_password) return;
    const new_password=window.prompt("New password (at least 8 characters). Your other devices will be signed out.");
    if(!new_password) return;
    try{ await api("/auth/password",{method:"POST",json:{current_password,new_password}}); setError("Password changed."); }
    catch(e:any){ setError(e.message); }
  }

  async function deleteAccount(){
    const password=window.prompt("This permanently deletes your account, chats and documents. Enter your password to confirm.");
    if(!password) return;
    try{ await api("/auth/me",{method:"DELETE",json:{password}}); signedOut(); }
    catch(e:any){ setError(e.message); }
  }

  const providers=[...new Set(models.filter(m=>m.selectable).map(m=>m.provider))];
  const options=models.filter(m=>m.provider===provider&&m.selectable);
  useEffect(()=>{
    if(options.length&&!options.some(m=>m.id===model)) setModel(options[0].id);
  },[provider,models]);

  if(!auth) return <div className="auth-shell"><div className="auth-card"><div className="brand">KNAVIS</div>{error?<><div className="request-error" role="alert">{error}</div><button className="send" onClick={()=>location.reload()}>Try again</button></>:<p className="auth-lead">Loading…</p>}</div></div>;
  if(!user) return <AuthScreen registrationOpen={auth.registrationOpen} passwordReset={auth.passwordReset} onSignedIn={setUser}/>;

  return <div className={`shell${menuOpen?" menu-open":""}`}>
    <aside className="sidebar">
      <div className="brand">KNAVIS <span>v0.4</span></div>
      <button className="new" onClick={newChat}>＋ New chat</button>
      <div className="section-title">Chats</div>
      <div className="sessions">{sessions.map(x=><div key={x.id} className={active?.id===x.id?"session-row active":"session-row"}>
        <button className="session" onClick={()=>selectSession(x)} title={x.title}>{x.title}</button>
        <button className="icon" aria-label={`Delete chat ${x.title}`} title="Delete chat" onClick={()=>deleteChat(x)}>×</button>
      </div>)}{sessions.length===0&&<small className="muted">No chats yet.</small>}</div>
      <div className="section-title">Documents</div>
      <div className="documents">{docs.map(d=><div className="doc" key={d.id}>
        <div className="doc-head"><b title={d.filename}>{d.filename}</b>
          <button className="icon" aria-label={`Remove ${d.filename}`} title="Remove document" onClick={()=>deleteDoc(d)}>×</button></div>
        <small className={`status status-${d.status}`}>{STATUS_LABEL[d.status]||d.status.replace(/_/g," ")}{d.details?.chunks!=null?` · ${d.details.chunks} chunks`:""}{d.details?.tables?.length?` · ${d.details.tables.length} table${d.details.tables.length>1?"s":""} for calculations`:""}</small>
        {d.details?.pause?.message&&<small className="pause">{d.details.pause.message}</small>}
        {d.details?.embedding?.error&&<small className="pause">Keyword search only: {d.details.embedding.hint}</small>}
        {ACTIONS[d.status]&&<div className="doc-actions">{ACTIONS[d.status].map(a=><button key={a.action} onClick={()=>processDoc(d,a.action)}>{a.label}</button>)}</div>}
      </div>)}{active&&docs.length===0&&<small className="muted">Attach a file with 📎 to get started.</small>}</div>
      {user.email&&<div className="account">
        <small title={user.email}>{user.email}</small>
        <div><button className="link" onClick={signOut}>Sign out</button><button className="link" onClick={changePassword}>Change password</button><button className="link danger" onClick={deleteAccount}>Delete account</button></div>
      </div>}
    </aside>
    {menuOpen&&<div className="scrim" onClick={()=>setMenuOpen(false)}/>}

    <main className="main">
      <header className="topbar">
        <button className="hamburger" aria-label="Open menu" onClick={()=>setMenuOpen(true)}>☰</button>
        <div className="title"><strong>{active?.title||"KNAVIS"}</strong>{active&&<button className="icon" aria-label="Rename chat" title="Rename chat" onClick={renameChat}>✎</button>}<small>Evidence-grounded · multilingual · agentic</small></div>
        <div className="selectors">
          <select aria-label="Provider" value={provider} onChange={e=>setProvider(e.target.value)}>
            {providers.map(p=><option key={p} value={p}>{p}</option>)}
          </select>
          <select aria-label="Model" value={model} onChange={e=>setModel(e.target.value)}>
            {options.map(m=><option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </div>
      </header>

      <section className="chat">
        {!active&&<div className="empty"><h1>Welcome.</h1><p>Start a new chat, attach a document, then ask a question about it.</p></div>}
        {active&&messages.length===0 && <div className="empty"><h1>Ask your documents.</h1><p>Attach a file, then ask a grounded question. For spreadsheets you can also ask for totals, counts and the highest or lowest group. Ordinary conversation is handled separately.</p></div>}
        {messages.map((m,i)=><div key={m.id||i} className={`msg ${m.role}`}><div className="bubble">
          {m.role==="assistant"?<Answer content={m.content}/>:<div className="answer-text">{m.content}</div>}
          {m.citations?.length>0&&<div className="citations">{m.citations.map((c:any,j:number)=><Citation key={j} c={c}/>)}</div>}
        </div></div>)}
        {busy&&<div className="msg assistant"><div className="bubble muted">Thinking with evidence…</div></div>}
        <div ref={bottom}/>
      </section>

      <footer className="composer">
        <div className="jobbar">{uploading&&<span>uploading…</span>}{Object.values(jobs).map((j:any)=><span key={j.id} className={j.status==="failed"?"error":""}>{(j.stage||j.status||"working").replace(/_/g," ")} · {j.progress??0}%</span>)}</div>
        {error&&<div className="request-error" role="alert">{error} <button className="link" onClick={()=>setError("")}>dismiss</button></div>}
        <div className="compose-row">
          <label className={`attach${active?"":" disabled"}`} title="Attach a document">📎<input type="file" onChange={upload} disabled={!active||uploading}/></label>
          <textarea ref={input} value={query} onChange={e=>setQuery(e.target.value)} disabled={!active}
            onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}}}
            placeholder={active?"Ask in English, Hindi, Hinglish, or another supported language…":"Start a new chat first"}/>
          <button className="send" onClick={send} disabled={busy||!active}>Send</button>
        </div>
        <small className="disclaimer">Knowledge answers are evidence-gated. If the evidence is insufficient, the system abstains.</small>
      </footer>
    </main>
  </div>
}

// A rendering mistake must never leave a blank page: show a way out instead.
class Boundary extends React.Component<{children:React.ReactNode},{failed:boolean}>{
  state={failed:false};
  static getDerivedStateFromError(){ return {failed:true}; }
  componentDidCatch(error:unknown){ console.error(error); }
  render(){
    return this.state.failed
      ? <div className="auth-shell"><div className="auth-card"><div className="brand">KNAVIS</div>
          <p className="auth-lead">Something went wrong while showing this page.</p>
          <button className="send" onClick={()=>location.reload()}>Reload</button></div></div>
      : this.props.children;
  }
}
createRoot(document.getElementById("root")!).render(<Boundary><App/></Boundary>);
