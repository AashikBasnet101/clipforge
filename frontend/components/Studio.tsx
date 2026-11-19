"use client";

import { useEffect, useRef, useState } from "react";
import { Download, Play, ScanLine, Scissors, Sparkles, Subtitles } from "lucide-react";
import { Caption, Clip, Detail, Discovery, Job, Options, Transcript, elapsed, request, signed } from "../lib/api";

type Action = (operation: () => Promise<void>) => void;

export function JobStatus({job, act, refresh}: {job: Job; act: Action; refresh: () => Promise<void>}) {
  return <div className="job">
    <div className="between"><strong>{job.stage}</strong><span className={`pill ${job.status}`}>{job.status}</span></div>
    {["queued", "running"].includes(job.status) && <progress value={job.progress} max={100} aria-label="Processing progress" />}
    {job.error && <p className="error">{job.error}</p>}
    {["failed","cancelled","queued","running"].includes(job.status) && <button className="secondary" onClick={() => act(async () => {
      await request(`/jobs/${job.id}/${["failed","cancelled"].includes(job.status) ? "retry" : "cancel"}`, "POST");
      await refresh();
    })}>{["failed","cancelled"].includes(job.status) ? "Retry job" : "Cancel job"}</button>}
  </div>;
}

export function DiscoveryFields({value, change, max = 10}: {value: Discovery; change: (value: Discovery) => void; max?: number}) {
  return <div className="field-grid">
    <label>Maximum candidates<input type="number" min="1" max={max} value={value.count} onChange={event => change({...value, count: +event.target.value})} required /></label>
    <label>Speech language<select value={value.language} onChange={event => change({...value, language: event.target.value})}>
      <option value="auto">Auto-detect</option><option value="en">English</option><option value="hi">Hindi</option><option value="ne">Nepali</option>
    </select></label>
    <label className="wide">Clip duration ranges (seconds)<input key={JSON.stringify(value.ranges)} name="ranges"
      defaultValue={value.ranges.map(range => range.join("–")).join(", ")} required /></label>
  </div>;
}

export function parseRanges(form: HTMLFormElement): [number, number][] {
  const raw = String(new FormData(form).get("ranges") || "");
  return raw.split(",").map(item => {
    const match = item.trim().match(/^(\d+)\s*[-–]\s*(\d+)$/);
    if (!match) throw new Error("Enter duration ranges such as 15–30, 30–60");
    return [+match[1], +match[2]];
  });
}

function useMedia(project: string, name: string, enabled: boolean, version?: string) {
  const [url, setUrl] = useState("");
  const [issue, setIssue] = useState("");
  useEffect(() => {
    let alive = true;
    setUrl("");
    setIssue("");
    if (!enabled) return;
    const refresh = () => signed(project, name).then(value => {
      if (alive) { setUrl(value); setIssue(""); }
    }).catch(error => { if (alive) setIssue(error.message); });
    void refresh();
    const timer = setInterval(refresh, 12 * 60 * 1000);
    return () => { alive = false; clearInterval(timer); };
  }, [project, name, enabled, version]);
  return {url, issue};
}

export function ProjectStudio({project, refresh, act, choose, busy, max}: {
  project: Detail; refresh: () => Promise<void>; act: Action; choose: (id: string) => void; busy: boolean; max: number;
}) {
  const [tab, setTab] = useState("Moments");
  const [discovery, setDiscovery] = useState(project.discovery);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const video = useRef<HTMLVideoElement>(null);
  const source = useMedia(project.id, "source", !!project.source?.preview);
  const base = `/projects/${project.id}`;
  const analysis = project.jobs.find(job => job.kind === "analyze");
  useEffect(() => {
    if (tab !== "Transcript") return;
    let alive = true;
    request<Transcript>(base + "/transcript").then(data => { if (alive) setTranscript(data); }).catch(() => {});
    return () => { alive = false; };
  }, [tab, base, analysis?.status]);
  const seek = (seconds: number) => {
    if (!video.current) return;
    video.current.currentTime = seconds;
    void video.current.play().catch(() => {});
  };
  return <>
    <div className="studio-grid">
      <section className="panel content-panel">
        <h2>Source video</h2>
        <p className="muted">{project.source?.filename || "No video attached"}</p>
        {source.issue && <p className="error">{source.issue}</p>}
        {source.url ? <video ref={video} src={source.url} controls preload="metadata"
          onTimeUpdate={event => setPlayhead(event.currentTarget.currentTime)} /> :
          <div className="media-placeholder"><ScanLine size={30}/><span>Preview appears after inspection</span></div>}
      </section>
      <section className="panel content-panel">
        <h2>Processing</h2>
        {analysis ? <JobStatus job={analysis} act={act} refresh={refresh}/> : <p className="muted">No processing job has been created.</p>}
        <details><summary>Discovery settings</summary>
          <form className="stack" onSubmit={event => {
            event.preventDefault(); const form = event.currentTarget;
            act(async () => {
              await request(base + "/discovery", "PATCH", {...discovery, ranges: parseRanges(form)});
              await refresh();
            });
          }}><DiscoveryFields value={discovery} change={setDiscovery} max={max}/>
            <div className="inline wrap">
              <button className="secondary" disabled={busy}>Save settings</button>
              {analysis?.status === "complete" && <button className="ghost" type="button" disabled={busy} onClick={() => act(async () => {
                await request(base + "/analysis", "POST");
                await refresh();
              })}>Find moments again</button>}
            </div>
          </form>
        </details>
      </section>
    </div>
    <div className="tabs" aria-label="Project views">{["Moments", "Transcript", "Exports"].map(item =>
      <button key={item} aria-pressed={tab === item} onClick={() => setTab(item)}>{item}</button>)}</div>
    {tab === "Moments" && <div className="cards">
      {project.candidates.map(candidate => <article className="panel content-panel" key={candidate.id}>
        <div className="between"><span className="pill">{candidate.category}</span><span className="score" title="Heuristic editorial score">{candidate.scores.overall}/100</span></div>
        <h3>{candidate.title}</h3><p>{candidate.hook}</p><p className="muted">{candidate.summary}</p>
        <div className="between wrap"><button className="ghost" disabled={!source.url} onClick={() => seek(candidate.start_time)}><Play size={15}/> {elapsed(candidate.start_time)} – {elapsed(candidate.end_time)}</button>
          <button className="primary" disabled={busy} onClick={() => act(async () => {
            const clip = await request<{id: string}>(base + "/clips", "POST", {candidate_id: candidate.id});
            await refresh(); choose(clip.id);
          })}><Scissors size={16}/> Edit clip</button></div>
        {!!Object.keys(candidate.platforms).length && <details><summary>Platform copy</summary>
          {Object.entries(candidate.platforms).map(([platform, copy]) => <div className="copy" key={platform}>
            <strong>{platform}</strong><p>{copy.title}</p><p>{copy.caption}</p><p className="muted">{copy.description}</p>
            <p>{copy.hashtags.join(" ")}</p>{copy.thumbnail_text && <p>Thumbnail text · {copy.thumbnail_text}</p>}
          </div>)}
        </details>}
      </article>)}
      {!project.candidates.length && <div className="panel empty"><Sparkles/><h3>No moments yet</h3><p>Candidate suggestions appear after transcript analysis.</p></div>}
    </div>}
    {tab === "Transcript" && <section className="panel content-panel">
      <h2>Transcript</h2><p className="muted">{transcript?.language ? `${transcript.language} · ${transcript.provider} · Select a time to play the source.` : "The transcript appears after transcription."}</p>
      {transcript?.segments.map(segment => <form key={`${segment.id}-${segment.text}-${segment.start}-${segment.end}`}
        className={`segment ${playhead >= segment.start && playhead < segment.end ? "active-segment" : ""}`}
        onSubmit={event => {
          event.preventDefault(); const data = new FormData(event.currentTarget);
          act(async () => {
            await request(base + "/transcript/" + segment.id, "PATCH", {start: Number(data.get("start")),
              end: Number(data.get("end")), text: data.get("text")});
            setTranscript(await request<Transcript>(base + "/transcript"));
          });
        }}>
        <button type="button" className="ghost" onClick={() => seek(segment.start)}><Play size={14}/> {elapsed(segment.start)}</button>
        <label>Start<input name="start" type="number" step=".001" min="0" defaultValue={segment.start}/></label>
        <label>End<input name="end" type="number" step=".001" min="0" defaultValue={segment.end}/></label>
        <textarea aria-label="Segment text" name="text" defaultValue={segment.text} required maxLength={300}/>
        <button className="secondary" disabled={busy}>Save</button>
      </form>)}
    </section>}
    {tab === "Exports" && <section className="panel content-panel"><h2>Project clips</h2>
      {project.clips.length ? project.clips.map(clip => <button className="list-button" key={clip.id} onClick={() => choose(clip.id)}>
        <Scissors size={17}/>{clip.settings.title}<span className="pill">{clip.available ? "Ready" : "Draft"}</span>
      </button>) : <p className="muted">Choose a moment to start editing.</p>}
    </section>}
  </>;
}

export function Editor({project, clip, act, refresh, busy}: {project: Detail; clip: Clip; act: Action; refresh: () => Promise<void>; busy: boolean}) {
  const [options, setOptions] = useState(clip.settings);
  const [playhead, setPlayhead] = useState(options.start);
  const [mode, setMode] = useState<"source" | "render">("source");
  const video = useRef<HTMLVideoElement>(null);
  const stopAt = useRef<number | null>(null);
  const base = `/projects/${project.id}/clips/${clip.id}`;
  const job = project.jobs.find(item => item.clip_id === clip.id);
  const complete = project.jobs.find(item => item.clip_id === clip.id && item.status === "complete");
  const source = useMedia(project.id, "source", !!project.source?.preview);
  const exported = useMedia(project.id, clip.id + ".mp4", clip.available, complete?.id);
  const duration = project.source?.duration || options.end;
  const set = <K extends keyof Options,>(key: K, value: Options[K]) => setOptions(current => ({...current, [key]: value}));
  const number = (key: keyof Pick<Options, "start"|"end"|"font_size"|"stroke"|"shadow"|"words_per_line">, name: string, min: number, max: number, step = 1) =>
    <label>{name}<input type="number" value={options[key]} min={min} max={max} step={step} onChange={event => setOptions(current => ({
      ...current, [key]: +event.target.value,
    }))} required/></label>;
  const choice = (key: "ratio"|"framing"|"quality"|"style"|"font"|"position", name: string, choices: [string, string][]) =>
    <label>{name}<select aria-label={name} value={options[key]} onChange={event => set(key, event.target.value)}>
      {choices.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select></label>;
  const download = (ext: string) => act(async () => { window.location.assign(await signed(project.id, clip.id + "." + ext, true)); });
  const isRunning = ["queued", "running"].includes(job?.status || "");
  const updateCue = (index: number, change: Partial<Caption>) => set("captions", options.captions?.map(
    (cue, position) => index === position ? {...cue, ...change} : cue) || []);
  const jump = (time: number) => {
    stopAt.current = null;
    if (video.current) video.current.currentTime = time;
    setPlayhead(time);
  };
  return <div className="editor-grid">
    <section className="panel content-panel preview-panel">
      <div className="between wrap"><h2>Preview</h2><div className="tabs compact">
        <button aria-pressed={mode === "source"} onClick={() => setMode("source")}>Source</button>
        <button disabled={!clip.available} aria-pressed={mode === "render"} onClick={() => setMode("render")}>Last export</button>
      </div></div>
      {(source.issue || exported.issue) && <p className="error">{source.issue || exported.issue}</p>}
      {(mode === "source" ? source.url : exported.url) ? <video key={mode} ref={video} controls preload="metadata"
        src={mode === "source" ? source.url : exported.url}
        onLoadedMetadata={() => { if (mode === "source") jump(options.start); }}
        onTimeUpdate={event => {
          const player = event.currentTarget;
          if (mode === "source" && stopAt.current !== null && player.currentTime >= stopAt.current) {
            player.pause();
            stopAt.current = null;
          }
          setPlayhead(player.currentTime);
        }} /> : <div className="media-placeholder"><Play/></div>}
      {mode === "source" && <div className="timeline">
        <label>Source timeline <span className="muted">{elapsed(playhead)} / {elapsed(duration)}</span>
        <input aria-label="Seek source timeline" type="range" min="0" max={duration} step=".01" value={playhead}
          onChange={event => jump(+event.target.value)} /></label>
        <div className="range-track"><i style={{left: `${100*options.start/duration}%`, width: `${Math.max(0,100*(options.end-options.start)/duration)}%`}}/></div>
        <div className="between wrap">
          <button className="secondary" disabled={!source.url || options.end <= options.start} onClick={() => {
            jump(options.start);
            stopAt.current = options.end;
            void video.current?.play().catch(() => {});
          }}><Play size={15}/> Play trim</button>
          <button className="secondary" onClick={() => set("start", +playhead.toFixed(2))}>Set in point</button>
          <button className="secondary" onClick={() => set("end", +playhead.toFixed(2))}>Set out point</button>
        </div>
      </div>}
      {job && <JobStatus job={job} act={act} refresh={refresh}/>}
      {clip.framing && <p className="muted">Last export framing · {clip.framing}</p>}
      {clip.available && <div className="inline wrap">
        <button className="primary" onClick={() => download("mp4")}><Download size={17}/> Download MP4</button>
        <button className="secondary" onClick={() => download("srt")}><Subtitles size={17}/> SRT</button>
      </div>}
    </section>
    <form className="panel content-panel stack" onSubmit={event => {
      event.preventDefault(); act(async () => { await request(base + "/render", "POST", options); await refresh(); });
    }}>
      <div className="between"><h2>Clip settings</h2><span className="pill">{Math.max(0, options.end-options.start).toFixed(1)} s before cuts</span></div>
      <button type="button" className="secondary" onClick={() => setOptions(current => ({
        ...current, framing: "smart", quality: "high", fps: 0, normalize_audio: true, shorten_pauses: true,
      }))}><Sparkles size={16}/> Apply automatic edit settings</button>
      <label>Title<input value={options.title} required maxLength={150} onChange={event => set("title", event.target.value)}/></label>
      <div className="field-grid">
        {number("start","Start (seconds)",0,duration,.001)}{number("end","End (seconds)",0,duration,.001)}
        {choice("ratio","Aspect ratio",[["9:16","9:16 Vertical"],["1:1","1:1 Square"],["16:9","16:9 Landscape"]])}
        {choice("quality","Quality",[["high","High"],["standard","Standard"]])}
        {choice("framing","Framing",[["manual","Manual crop"],["smart","Smart framing (preserve the full shot when needed)"],["fit","Fit with padding"]])}
        <label>Frame rate<select value={options.fps} onChange={event => set("fps", +event.target.value)}>
          <option value={0}>Match source</option>{[24,30,60].map(fps => <option key={fps}>{fps}</option>)}</select></label>
      </div>
      {options.framing === "manual" && <label>Crop position <span className="muted">{Math.round(options.crop * 100)}%</span>
        <input type="range" min="0" max="1" step=".01" value={options.crop} onChange={event => set("crop", +event.target.value)}/></label>}
      <label className="check"><input type="checkbox" checked={options.shorten_pauses} onChange={event => set("shorten_pauses", event.target.checked)}/> Shorten long pauses between words</label>
      <label className="check"><input type="checkbox" checked={options.normalize_audio} onChange={event => set("normalize_audio", event.target.checked)}/> Normalize audio</label>
      <label>Branding overlay<input value={options.overlay} maxLength={120} onChange={event => set("overlay", event.target.value)}/></label>
      <label>Hashtags<input value={options.hashtags} maxLength={500} onChange={event => set("hashtags", event.target.value)}/></label>
      <details open><summary>Subtitles</summary>
        <div className="field-grid">
          {choice("style","Style",[["minimal","Minimal"],["creator","Bold creator"],["highlight","Word highlight"],["karaoke","Karaoke"],["educational","Educational"],["off","Off"]])}
          {choice("font","Font",[["Noto Sans","Noto Sans"],["Noto Sans Devanagari","Noto Sans Devanagari"],["DejaVu Sans","DejaVu Sans"]])}
          {choice("position","Position",[["top","Top"],["middle","Middle"],["bottom","Bottom"]])}
          {number("font_size","Font size",24,88)}{number("stroke","Stroke",0,10)}{number("shadow","Shadow",0,8)}
          {number("words_per_line","Words per line",1,12)}
          <label>Text color<input type="color" value={options.text_color} onChange={event => set("text_color", event.target.value)}/></label>
          <label>Highlight color<input type="color" value={options.highlight_color} onChange={event => set("highlight_color", event.target.value)}/></label>
        </div>
        <div className="inline wrap">
          <button type="button" className="secondary" onClick={() => act(async () => {
            const captions = await request<Caption[]>(base + `/captions?start=${options.start}&end=${options.end}&words_per_line=${options.words_per_line}`);
            set("captions", captions);
          })}>Load editable captions</button>
          {options.captions !== null && <button type="button" className="ghost" onClick={() => set("captions", null)}>Use transcript timing</button>}
        </div>
        {options.captions !== null && <div className="stack">
          <p className="muted">Caption timestamps use the trim before pause removal. The export retimes them to the edited clip.</p>
          {options.captions.map((cue, index) => <div className="cue" key={index}>
            <label>Start<input type="number" step=".001" min="0" value={cue.start} onChange={event => updateCue(index,{start:+event.target.value})}/></label>
            <label>End<input type="number" step=".001" min="0" value={cue.end} onChange={event => updateCue(index,{end:+event.target.value})}/></label>
            <input aria-label={`Caption ${index+1} text`} value={cue.text} maxLength={300} required onChange={event => updateCue(index,{text:event.target.value})}/>
            <button type="button" className="ghost" onClick={() => set("captions", options.captions?.filter((_, i) => i !== index) || [])}>Remove</button>
          </div>)}
          <button type="button" className="secondary" onClick={() => set("captions", [...(options.captions || []), {start: 0, end: 1, text: ""}])}>Add caption</button>
        </div>}
      </details>
      <button className="primary" disabled={busy || isRunning}><Scissors size={17}/> {isRunning ? "Rendering…" : clip.available ? "Re-render clip" : "Render clip"}</button>
    </form>
  </div>;
}
