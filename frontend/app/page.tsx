"use client";

import { useCallback, useEffect, useState } from "react";
import { Activity, BarChart3, Clapperboard, Clock3, Film, FolderOpen, HardDrive, House, Link2, LogOut, Moon, Plus, Radio, Scissors, Settings as Gear, Sun, Upload, Video } from "lucide-react";
import { DiscoveryFields, Editor, JobStatus, parseRanges, ProjectStudio } from "../components/Studio";
import { type Detail, type Discovery, type Project, type Settings, type Usage, bytes, request } from "../lib/api";

const links = [
  ["Dashboard", House], ["New Project", Plus], ["My Projects", FolderOpen], ["Generated Clips", Film],
  ["Clip Editor", Scissors], ["Publishing", Radio], ["Analytics", BarChart3], ["Settings", Gear],
] as const;
type Page = typeof links[number][0];
const defaults: Discovery = {count: 5, language: "auto", ranges: [[15,30],[30,60],[60,90]]};

export default function Home() {
  const [account, setAccount] = useState<string | null | undefined>(undefined);
  const [register, setRegister] = useState(false);
  const [light, setLight] = useState(false);
  const [view, setView] = useState<Page>("Dashboard");
  const [projects, setProjects] = useState<Project[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [projectId, setProjectId] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [clipId, setClipId] = useState("");
  const [formOptions, setFormOptions] = useState(defaults);
  const [sourceType, setSourceType] = useState<"file"|"url">("file");
  const [file, setFile] = useState<File | null>(null);
  const [pendingProject, setPendingProject] = useState("");
  const [clipProjects, setClipProjects] = useState<Detail[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [loadError, setLoadError] = useState("");

  const refresh = useCallback(async () => {
    const [rows, totals] = await Promise.all([request<Project[]>("/projects"), request<Usage>("/usage")]);
    setProjects(rows); setUsage(totals);
    if (projectId) setDetail(await request<Detail>("/projects/" + projectId));
    if (view === "Generated Clips" || (view === "Clip Editor" && !projectId)) {
      setClipProjects(await Promise.all(rows.filter(row => row.source).map(row => request<Detail>("/projects/" + row.id))));
    }
  }, [projectId, view]);

  useEffect(() => {
    setLight(localStorage.getItem("clipforge-theme") === "light");
    request<{email: string}>("/auth/me").then(value => setAccount(value.email)).catch(() => setAccount(null));
  }, []);
  useEffect(() => { document.documentElement.dataset.theme = light ? "light" : "dark"; }, [light]);
  useEffect(() => {
    if (!account) return;
    request<Settings>("/settings").then(setSettings).catch(error => setLoadError(error.message));
  }, [account]);
  useEffect(() => {
    if (!account) return;
    const update = () => refresh().then(() => setLoadError("")).catch(error => setLoadError(error.message));
    void update();
    const timer = setInterval(update, 3500);
    return () => clearInterval(timer);
  }, [account, refresh]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 3200);
    return () => clearTimeout(timer);
  }, [toast]);

  const act = async (operation: () => Promise<void>, message = "Changes saved") => {
    if (busy) return;
    setBusy(true); setError("");
    try { await operation(); if (message) setToast(message); }
    catch (issue) { setError(issue instanceof Error ? issue.message : "Something went wrong"); }
    finally { setBusy(false); }
  };
  const navigate = (page: Page) => {
    setView(page); setError(""); setProjectId(""); setDetail(null); setClipId("");
  };
  const open = (id: string, clip = "") => {
    setView(clip ? "Clip Editor" : "My Projects"); setProjectId(id); setClipId(clip); setError("");
    if (detail?.id !== id) setDetail(null);
  };
  const toggle = () => {
    setLight(!light); localStorage.setItem("clipforge-theme", light ? "dark" : "light");
  };
  const attach = async (form: FormData, id: string) => {
    if (sourceType === "url") {
      await request(`/projects/${id}/url`, "POST", {url: form.get("url"), authorized: form.get("authorized") === "on"});
    } else {
      if (!file) throw new Error("Choose a video file");
      const payload = new FormData();
      payload.set("file", file); payload.set("authorized", "true");
      await request(`/projects/${id}/upload`, "POST", payload);
    }
    setPendingProject(""); setFile(null); open(id);
  };

  const sourceForm = (existing?: string) => <form className="stack" onSubmit={event => {
    event.preventDefault(); const form = event.currentTarget; const values = new FormData(form);
    void act(async () => {
      if (sourceType === "file" && !file) throw new Error("Choose a video file");
      if (file && settings && file.size > settings.max_upload_mb * 1024 ** 2 && sourceType === "file") throw new Error("The file exceeds your upload limit");
      let id = existing || pendingProject;
      if (!id) {
        const created = await request<Project>("/projects", "POST", {name: values.get("name"),
          discovery: {...formOptions, ranges: parseRanges(form)}});
        id = created.id; setPendingProject(id);
      }
      await attach(values, id);
    }, "Processing job queued");
  }}>
    {!existing && <label>Project name<input name="name" placeholder="e.g. Studio interview" required maxLength={120} disabled={!!pendingProject}/></label>}
    <div className="tabs compact"><button type="button" aria-pressed={sourceType === "file"} onClick={() => setSourceType("file")}><Upload size={16}/> Upload file</button>
      <button type="button" aria-pressed={sourceType === "url"} onClick={() => setSourceType("url")}><Link2 size={16}/> Direct URL</button></div>
    {sourceType === "file" ? <label className="drop" onDragOver={event => event.preventDefault()} onDrop={event => {
      event.preventDefault(); setFile(event.dataTransfer.files[0] || null);
    }}><Upload size={24}/><strong>{file?.name || "Choose or drop a video"}</strong>
      <span>MP4, MOV or WebM {settings && `· Up to ${settings.max_upload_mb} MB`}</span>
      <input type="file" accept=".mp4,.mov,.webm,video/mp4,video/quicktime,video/webm" onChange={event => setFile(event.target.files?.[0] || null)}/>
    </label> : <label>Direct HTTPS video URL<input name="url" type="url" placeholder="https://…" required/>
      <small>{settings?.url_mode === "allowlist" ? "Use a direct MP4, MOV or WebM file from an approved host." : settings?.url_ready ? "Use a direct MP4, MOV or WebM file or a signed object-storage link." : "URL imports are disabled. Use file upload."} For YouTube, TikTok or Instagram, upload an authorized exported file.</small>
    </label>}
    {!existing && !pendingProject && <DiscoveryFields value={formOptions} change={setFormOptions} max={settings?.max_clips}/>}
    <label className="check"><input name="authorized" type="checkbox" required/> I own this video or have permission to process it.</label>
    <button className="primary" disabled={busy || (sourceType === "url" && !settings?.url_ready)}><Video size={18}/> {busy ? "Submitting…" : "Start processing"}</button>
  </form>;

  const projectTable = (rows: Project[]) => rows.length ? <div className="project-list">{rows.map(project => <button key={project.id} className="project-row" onClick={() => open(project.id)}>
    <span className="project-icon"><Film size={19}/></span><span className="project-name"><strong>{project.name}</strong><small>{project.source?.filename || "Awaiting video"}</small></span>
    <span className={`pill ${project.job?.status || ""}`}>{project.job?.status || "draft"}</span><span className="clip-count">{project.clip_count} clips</span>
  </button>)}</div> : <div className="empty"><FolderOpen/><h3>No projects yet</h3><p>Your projects and processing status will appear here.</p>
    <button className="secondary" onClick={() => navigate("New Project")}><Plus size={17}/> Create a project</button></div>;

  const clip = detail?.clips.find(item => item.id === clipId);
  const usageTiles = [
    ["Videos processed", usage?.processed, Video], ["Clips generated", usage?.clips, Scissors],
    ["Processing minutes", usage?.processing_minutes.toFixed(1), Clock3],
    ["Storage used", usage && bytes(usage.storage_bytes), HardDrive],
  ] as const;
  const brand = <div className="brand"><span className="brand-icon"><Clapperboard size={21}/></span><span>ClipForge <em>AI</em></span></div>;
  const notices = <>{(error || loadError) && <div className="notice error" role="alert">{error || loadError}</div>}
    {toast && <div className="toast" role="status">{toast}</div>}</>;

  if (account === undefined) return <main className="auth-shell"><div role="status">Loading ClipForge…</div></main>;
  if (!account) return <main className="auth-shell">
    <div className="auth-card panel content-panel">{brand}<h1>{register ? "Create your account" : "Sign in to ClipForge"}</h1>
      <p className="muted">Find and edit short clips from your long-form videos.</p>{notices}
      <form className="stack" onSubmit={event => {
        event.preventDefault(); const data = new FormData(event.currentTarget);
        void act(async () => {
          const user = await request<{email: string}>(register ? "/auth/register" : "/auth/login", "POST", {
            email: data.get("email"), password: data.get("password"),
          });
          setAccount(user.email); navigate("Dashboard");
        }, "");
      }}>
        <label>Email<input type="email" name="email" required autoComplete="email"/></label>
        <label>Password<input name="password" type="password" minLength={12} maxLength={128} required
          autoComplete={register ? "new-password" : "current-password"}/></label>
        {register && <small>Use at least 12 characters.</small>}
        <button className="primary" disabled={busy}>{busy ? "Please wait…" : register ? "Create account" : "Sign in"}</button>
      </form>
      <button className="ghost" onClick={() => {setRegister(!register); setError("");}}>{register ? "Already have an account? Sign in" : "New to ClipForge? Create an account"}</button>
    </div>
  </main>;

  return <div className="shell">
    <a className="skip" href="#content">Skip to content</a>
    <aside className="sidebar">{brand}<nav aria-label="Workspace">
      {links.map(([item, Icon]) => <button key={item} aria-current={view === item ? "page" : undefined}
        onClick={() => navigate(item)}><Icon size={18}/><span>{item}</span></button>)}
    </nav><div className="sidebar-bottom"><span className="avatar">{account[0].toUpperCase()}</span><div><strong>Workspace</strong><small title={account}>{account}</small></div></div></aside>
    <main id="content" className="main">
      <header className="topbar"><span className="muted">{view}</span><div className="inline">
        <button className="icon-button" aria-label={light ? "Switch to dark theme" : "Switch to light theme"} onClick={toggle}>{light ? <Moon size={18}/> : <Sun size={18}/>}</button>
        <button className="icon-button" aria-label="Sign out" onClick={() => void act(async () => {
          await request("/auth/logout", "POST"); setAccount(null); setProjects([]); setUsage(null); setSettings(null); setPendingProject(""); setFile(null); navigate("Dashboard");
        }, "")}><LogOut size={18}/></button>
      </div></header>
      <div className="main-content">
        <div className="heading"><div><h1>{clip?.settings.title || (detail && view === "My Projects" ? detail.name : view)}</h1>
          {view === "Dashboard" && <p className="muted">Track your videos, generated clips, and processing activity.</p>}
          {view === "New Project" && <p className="muted">Add an authorized video and choose the moments you want to find.</p>}</div>
          {["Dashboard","My Projects"].includes(view) && <button className="primary" onClick={() => navigate("New Project")}><Plus size={18}/> New project</button>}
          {view === "Clip Editor" && detail && <button className="secondary" onClick={() => open(detail.id)}>Project details</button>}
        </div>
        {notices}
        {view === "Dashboard" && <>
          <section className="metrics" aria-label="Workspace totals">{usageTiles.map(([label, value, Icon]) =>
            <article className="panel metric" key={label}><div className="between"><span>{label}</span><Icon size={18}/></div><strong>{value ?? "—"}</strong></article>)}</section>
          <div className="dashboard-grid">
            <section className="panel content-panel"><div className="between"><h2>Recent projects</h2><button className="ghost" onClick={() => navigate("My Projects")}>View all</button></div>{projectTable(projects.slice(0,5))}</section>
            <section className="panel content-panel"><h2><Activity size={19}/> Processing activity</h2>
              <p className="muted">{usage?.active ?? "—"} active · {usage?.failed ?? "—"} failed attempts</p>
              {projects.filter(item => item.job && ["queued","running","failed"].includes(item.job.status)).slice(0,5).map(item =>
                <div className="activity" key={item.id}><button className="ghost" onClick={() => open(item.id)}>{item.name}</button>
                  <JobStatus job={item.job!} act={act} refresh={refresh}/></div>)}
              {!projects.some(item => item.job && ["queued","running","failed"].includes(item.job.status)) && <p className="muted">No active jobs or recent errors.</p>}
            </section>
          </div>
        </>}
        {view === "New Project" && <section className="panel content-panel form-panel">
          {settings && (!settings.transcription_ready || !settings.discovery_ready) && <p className="notice">Provider setup is required before all processing stages can complete. See Settings for the current status.</p>}
          {sourceForm()}
        </section>}
        {view === "My Projects" && (projectId ? detail ? detail.source ?
          <ProjectStudio key={detail.id} project={detail} refresh={refresh} act={act} choose={id => open(detail.id, id)} busy={busy} max={settings?.max_clips ?? 10}/>
          : <section className="panel content-panel form-panel">{sourceForm(detail.id)}</section> : <p role="status">Loading project…</p>
          : <section className="panel content-panel">{projectTable(projects)}</section>)}
        {view === "Clip Editor" && clip && detail && <Editor key={clip.id} project={detail} clip={clip} act={act} refresh={refresh} busy={busy}/>}
        {view === "Clip Editor" && projectId && !clip && <p role="status">Loading clip…</p>}
        {(view === "Generated Clips" || (view === "Clip Editor" && !projectId)) && <section className="panel content-panel">
          {clipProjects.flatMap(row => row.clips.filter(item => view !== "Generated Clips" || item.available).map(item =>
            <button key={item.id} className="list-button" onClick={() => open(row.id, item.id)}><Scissors size={17}/><span>{item.settings.title}</span>
              <small>{row.name}</small><span className="pill">{item.available ? "Ready" : "Draft"}</span></button>))}
          {!clipProjects.some(row => row.clips.some(item => view !== "Generated Clips" || item.available)) && <div className="empty"><Scissors/><h3>No {view === "Generated Clips" ? "exports" : "clips"} yet</h3>
            <p>Open a project and choose a moment to start editing.</p><button className="secondary" onClick={() => navigate("My Projects")}>My Projects</button></div>}
        </section>}
        {view === "Publishing" && <section className="panel content-panel form-panel"><h2>Manual publishing</h2>
          <p>Download an approved MP4 and upload it through TikTok, Instagram Reels, or YouTube Shorts.</p>
          <p className="muted">Account connections and scheduling require an official platform integration with the appropriate OAuth permissions. This workspace uses manual publishing.</p>
          <button className="primary" onClick={() => navigate("Generated Clips")}><Film size={17}/> Browse exports</button>
        </section>}
        {view === "Analytics" && <section className="panel content-panel form-panel"><h2>Processing analytics</h2>
          {usage ? <><dl className="data-list">{Object.entries({
            "Completed jobs": usage.completed, "Failed attempts": usage.failed, "Average processing time": usage.average_seconds.toFixed(1) + " s",
            "Generated clips": usage.clips, "Storage used": bytes(usage.storage_bytes),
            "Transcription minutes": (usage.usage.transcription_minutes || 0).toFixed(2), "Prompt tokens": usage.usage.prompt_tokens || 0,
            "Completion tokens": usage.usage.completion_tokens || 0, "Render time": (usage.usage.render_seconds || 0).toFixed(1) + " s",
            "Download requests": usage.usage.download_requests || 0, "Estimated provider cost": usage.estimated_cost_usd === null ? "Pricing is unconfigured" : "$" + usage.estimated_cost_usd.toFixed(4),
          }).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
            <h3>Model usage</h3>{Object.entries(usage.models).map(([model, values]) => <dl key={model} className="data-list">
              <div><dt>Model</dt><dd>{model}</dd></div>{Object.entries(values).map(([metric, amount]) => <div key={metric}><dt>{metric.replaceAll("_"," ")}</dt><dd>{amount.toFixed(2)}</dd></div>)}
            </dl>)}</> : <p role="status">Loading usage…</p>}
        </section>}
        {view === "Settings" && <section className="panel content-panel form-panel"><h2>Workspace configuration</h2>
          {settings ? <dl className="data-list">{Object.entries({
            "Transcription": settings.transcription_ready ? settings.stt : "Configure STT_API_KEY or local Whisper",
            "Clip discovery": settings.discovery_ready ? "Configured" : "Configure LLM_API_KEY",
            "URL ingestion": settings.url_mode === "public" ? "Public HTTPS video files" : settings.url_ready ? "Enabled for approved hosts" : "Disabled",
            "Upload limit": `${settings.max_upload_mb} MB`, "Source duration limit": `${settings.max_duration / 60} min`,
            "Candidate limit": settings.max_clips, "Concurrent jobs": settings.max_active_jobs, "Daily jobs": settings.max_daily_jobs,
          }).map(([name,value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl> : <p role="status">Loading configuration…</p>}
          <p className="muted">Provider credentials and limits are configured on the server.</p>
        </section>}
      </div>
    </main>
  </div>;
}
