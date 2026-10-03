// All calls to our FastAPI backend. The browser only ever talks to our API and to a
// short-lived presigned storage URL; no Gnani/Gemini/database/storage credentials live here.

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");

export type Status =
  | "uploading" | "queued" | "preprocessing" | "transcribing" | "summarizing" | "completed" | "failed";

export interface Chunk {
  idx: number;
  start_seconds: number;
  end_seconds: number;
  status: "pending" | "done" | "failed";
  transcript: string | null;
}

export interface RecordingListItem {
  id: string;
  original_filename: string;
  language_code: string;
  status: Status;
  duration_seconds: number | null;
  chunks_total: number;
  chunks_done: number;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface Recording extends RecordingListItem {
  size_bytes: number;
  transcript: string | null;
  summary: string | null;
  can_retry: boolean;
  updated_at: string;
  chunks: Chunk[];
}

export interface Language {
  code: string;
  name: string;
}

// No login: each browser gets a random id, kept in localStorage, sent as X-Client-Id.
// The backend only lists/modifies recordings that carry this id.
export function getClientId(): string {
  const KEY = "audio-notes-client-id";
  let id = localStorage.getItem(KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(KEY, id);
  }
  return id;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", "X-Client-Id": getClientId(), ...(init.headers || {}) },
    });
  } catch {
    throw new Error("Could not reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {}
    throw new Error(message);
  }
  return res.json();
}

export const api = {
  languages: () => request<Language[]>("/api/languages"),
  list: () => request<RecordingListItem[]>("/api/recordings"),
  get: (id: string) => request<Recording>(`/api/recordings/${id}`),
  retry: (id: string) => request<Recording>(`/api/recordings/${id}/retry`, { method: "POST" }),
  audioUrl: (id: string) => request<{ url: string }>(`/api/recordings/${id}/audio`),
  create: (file: File, languageCode: string) =>
    request<{ recording: Recording; upload_url: string; upload_headers: Record<string, string> }>("/api/recordings", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type,
        size_bytes: file.size,
        language_code: languageCode,
      }),
    }),
  markUploaded: (id: string) => request<Recording>(`/api/recordings/${id}/uploaded`, { method: "POST" }),
};

// fetch() can't report upload progress, XMLHttpRequest can.
export function putWithProgress(
  url: string,
  file: File,
  headers: Record<string, string>,
  onProgress: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    Object.entries(headers).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve()
        : reject(new Error(`Upload to storage failed (HTTP ${xhr.status}).`));
    xhr.onerror = () => reject(new Error("Upload failed: network error. Check your connection and try again."));
    xhr.onabort = () => reject(new Error("Upload was cancelled."));
    xhr.send(file);
  });
}

export const TERMINAL: Status[] = ["completed", "failed"];

export function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
