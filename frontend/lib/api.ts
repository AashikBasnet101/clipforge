export const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function request<T>(path: string, method = "GET", data?: unknown): Promise<T> {
  const multipart = data instanceof FormData;
  const response = await fetch(API + path, {
    method, credentials: "include", cache: "no-store",
    headers: data && !multipart ? { "Content-Type": "application/json" } : undefined,
    body: data ? multipart ? data : JSON.stringify(data) : undefined,
  });
  const payload = await response.json();
  if (!response.ok) {
    const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.map(
      (item: { loc: string[]; msg: string }) => `${item.loc.at(-1)} · ${item.msg}`
    ).join(". ");
    throw new Error(detail || "The request could not be completed");
  }
  return payload as T;
}

export const elapsed = (seconds: number) => {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
};

export const bytes = (value: number) => value >= 1024 ** 3
  ? `${(value / 1024 ** 3).toFixed(2)} GB` : `${(value / 1024 ** 2).toFixed(1)} MB`;

export type Job = {id: string; project_id: string; clip_id: string | null; kind: string; status: string; stage: string; progress: number; error: string | null};
export type Discovery = { count: number; ranges: [number, number][]; language: string };
export type Project = {id: string; name: string; created_at: number; discovery: Discovery; job: Job | null; clip_count: number;
  source: {filename: string; duration: number | null; size: number; preview: boolean; state: string} | null };
export type Copy = {title: string; caption: string; description: string; hashtags: string[]; thumbnail_text: string};
export type Candidate = {id: string; title: string; hook: string; summary: string; reasoning_summary: string; category: string; start_time: number; end_time: number; scores: Record<string, number>; platforms: Record<string, Copy>};
export type Caption = {start: number; end: number; text: string};
export type Segment = Caption & {id: string; words: {word: string; start: number; end: number}[]};
export type Transcript = {language: string | null; provider: string | null; segments: Segment[]};
export type Options = {start: number; end: number; title: string; hashtags: string; ratio: string; crop: number; framing: string; quality: string;
  fps: number; shorten_pauses: boolean; normalize_audio: boolean; style: string; font: string; font_size: number; text_color: string; highlight_color: string;
  stroke: number; shadow: number; position: string; words_per_line: number; overlay: string; captions: Caption[] | null};
export type Clip = {id: string; candidate_id: string; settings: Options; framing: string | null; available: boolean};
export type Detail = Project & {jobs: Job[]; candidates: Candidate[]; clips: Clip[]};
export type Usage = {processed: number; completed: number; clips: number; failed: number; active: number; storage_bytes: number;
  processing_minutes: number; average_seconds: number; estimated_cost_usd: number | null; usage: Record<string, number>; models: Record<string, Record<string, number>>};
export type Settings = {stt: string; transcription_ready: boolean; discovery_ready: boolean; url_ready: boolean; url_mode: "disabled" | "public" | "allowlist";
  max_upload_mb: number; max_clips: number; max_duration: number; max_daily_jobs: number; max_active_jobs: number};

export async function signed(project: string, name: string, download = false) {
  return API + (await request<{url: string}>(`/projects/${project}/asset-links/${name}?download=${download}`)).url;
}
