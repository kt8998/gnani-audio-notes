"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, formatBytes, Language, putWithProgress } from "@/lib/api";

const ACCEPT = ".mp3,.wav,.m4a,.ogg,.oga,.opus,.flac,.aac,.webm,.mp4,audio/*";

type Phase = "idle" | "creating" | "uploading" | "confirming" | "error";

export default function UploadCard() {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [languages, setLanguages] = useState<Language[]>([{ code: "en-IN", name: "English" }]);
  const [language, setLanguage] = useState("en-IN");
  const [dragging, setDragging] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [percent, setPercent] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.languages().then(setLanguages).catch(() => {}); // keep the English default if this fails
  }, []);

  const busy = phase === "creating" || phase === "uploading" || phase === "confirming";

  function pick(f: File | undefined) {
    if (!f || busy) return;
    setFile(f);
    setError(null);
    setPhase("idle");
    setPercent(0);
  }

  async function upload() {
    if (!file) return;
    setError(null);
    try {
      // 1. Ask our API for a recording id + a presigned upload URL (validates type/size/language).
      setPhase("creating");
      const { recording, upload_url, upload_headers } = await api.create(file, language);
      // 2. Send the file straight to object storage, with real progress.
      setPhase("uploading");
      await putWithProgress(upload_url, file, upload_headers, setPercent);
      // 3. Tell the API the upload finished; it verifies the file and queues processing.
      setPhase("confirming");
      await api.markUploaded(recording.id);
      router.push(`/recordings/${recording.id}`);
    } catch (e) {
      setPhase("error");
      setError(e instanceof Error ? e.message : "Upload failed.");
    }
  }

  return (
    <section className="card">
      <h2>New recording</h2>
      <div
        className={`dropzone${dragging ? " active" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); pick(e.dataTransfer.files[0]); }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
      >
        <input ref={inputRef} type="file" accept={ACCEPT} hidden onChange={(e) => pick(e.target.files?.[0])} />
        {file ? (
          <>
            <strong>{file.name}</strong>
            <div className="muted small">{formatBytes(file.size)} · click to choose a different file</div>
          </>
        ) : (
          <>
            <strong>Drop an audio file here, or click to browse</strong>
            <div className="muted small">MP3, WAV, M4A, OGG, FLAC, AAC, WEBM · up to 50 MB (about 50 min of MP3)</div>
          </>
        )}
      </div>

      <div className="row">
        <label className="small muted" htmlFor="lang">Spoken language</label>
        <select id="lang" value={language} onChange={(e) => setLanguage(e.target.value)} disabled={busy}>
          {languages.map((l) => <option key={l.code} value={l.code}>{l.name}</option>)}
        </select>
        <button className="primary" onClick={upload} disabled={!file || busy}>
          {busy ? "Uploading…" : phase === "error" ? "Try again" : "Upload & transcribe"}
        </button>
      </div>

      {busy && (
        <div style={{ marginTop: 16 }}>
          <div className="progress"><div style={{ width: `${phase === "creating" ? 0 : percent}%` }} /></div>
          <div className="small muted">
            {phase === "creating" && "Preparing upload…"}
            {phase === "uploading" && `Uploading… ${percent}%`}
            {phase === "confirming" && "Upload complete, verifying…"}
          </div>
        </div>
      )}
      {error && <div className="alert error">{error}</div>}
    </section>
  );
}
