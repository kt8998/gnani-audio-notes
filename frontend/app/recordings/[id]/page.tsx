"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import Markdown from "@/components/Markdown";
import StatusBadge from "@/components/StatusBadge";
import { api, formatBytes, formatDuration, Recording, Status, TERMINAL } from "@/lib/api";

const POLL_MS = 2000;
const STAGES: { key: Status; label: string }[] = [
  { key: "uploading", label: "Uploaded" },
  { key: "queued", label: "Queued" },
  { key: "preprocessing", label: "Preparing audio" },
  { key: "transcribing", label: "Transcribing" },
  { key: "summarizing", label: "Summarizing" },
  { key: "completed", label: "Done" },
];

// The API only says "failed"; work out which stage it failed in from what was saved.
function failedStage(r: Recording): Status {
  if (r.transcript !== null) return "summarizing";
  if (r.chunks_total > 0) return "transcribing";
  if (r.duration_seconds === null && r.chunks_total === 0 && r.error_message?.includes("upload")) return "uploading";
  return "preprocessing";
}

function StageTracker({ r }: { r: Recording }) {
  const current = r.status === "failed" ? failedStage(r) : r.status;
  const currentIdx = STAGES.findIndex((s) => s.key === current);
  return (
    <ol className="stages">
      {STAGES.map((s, i) => {
        let cls = "";
        if (r.status === "completed" || i < currentIdx) cls = "done";
        else if (i === currentIdx) cls = r.status === "failed" ? "failed" : "current";
        return <li key={s.key} className={cls}>{s.label}</li>;
      })}
    </ol>
  );
}

function stageMessage(r: Recording): string {
  switch (r.status) {
    case "uploading": return "Waiting for the upload to finish…";
    case "queued": return "Waiting for a worker to pick this up…";
    case "preprocessing": return "Downloading and decoding the audio, splitting it into 25-second chunks…";
    case "transcribing": return `Transcribing with Gnani: ${r.chunks_done} of ${r.chunks_total} chunks done`;
    case "summarizing": return "Transcript ready. Generating the summary with Gemini…";
    case "completed": return "Done.";
    case "failed": return "Processing failed.";
  }
}

export default function RecordingPage() {
  const { id } = useParams<{ id: string }>();
  const [rec, setRec] = useState<Recording | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [connectionLost, setConnectionLost] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pollKey, setPollKey] = useState(0); // bump to restart polling after a retry

  // Poll until the recording reaches a terminal state.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;
    async function poll() {
      try {
        const r = await api.get(id);
        if (cancelled) return;
        setRec(r);
        setConnectionLost(false);
        if (!TERMINAL.includes(r.status)) timer = setTimeout(poll, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : "Could not load this recording.";
        if (msg.includes("not found")) { setLoadError(msg); return; }
        setConnectionLost(true); // transient: keep trying, but tell the user
        timer = setTimeout(poll, POLL_MS * 3);
      }
    }
    poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [id, pollKey]);

  const hasAudio = rec !== null && rec.status !== "uploading";
  useEffect(() => {
    if (hasAudio && !audioUrl) api.audioUrl(id).then((r) => setAudioUrl(r.url)).catch(() => {});
  }, [hasAudio, audioUrl, id]);

  const retry = useCallback(async () => {
    setRetrying(true);
    setActionError(null);
    try {
      setRec(await api.retry(id));
      setPollKey((k) => k + 1);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Retry failed.");
    } finally {
      setRetrying(false);
    }
  }, [id]);

  if (loadError) {
    return (
      <div className="card">
        <div className="alert error">{loadError}</div>
        <p><Link href="/">← Back to uploads</Link></p>
      </div>
    );
  }
  if (!rec) return <p className="muted">Loading…</p>;

  const working = !TERMINAL.includes(rec.status);
  const chunkPercent = rec.chunks_total ? Math.round((rec.chunks_done / rec.chunks_total) * 100) : 0;
  const partial = rec.chunks.filter((c) => c.status === "done" && c.transcript).map((c) => c.transcript).join(" ");

  return (
    <>
      <p className="small"><Link href="/">← All recordings</Link></p>

      <section className="card">
        <div className="row" style={{ marginTop: 0, justifyContent: "space-between" }}>
          <h1 style={{ wordBreak: "break-word" }}>{rec.original_filename}</h1>
          <StatusBadge status={rec.status} />
        </div>
        <div className="small muted">
          {formatBytes(rec.size_bytes)} · duration {formatDuration(rec.duration_seconds)} · {rec.language_code} ·
          uploaded {new Date(rec.created_at).toLocaleString()}
        </div>

        <StageTracker r={rec} />
        <div className="small">{stageMessage(rec)}</div>
        {rec.status === "transcribing" && rec.chunks_total > 0 && (
          <div className="progress"><div style={{ width: `${chunkPercent}%` }} /></div>
        )}
        {working && rec.status !== "transcribing" && (
          <div className="progress"><div style={{ width: "100%", opacity: 0.35 }} /></div>
        )}
        {connectionLost && <div className="alert info">Lost connection to the server. Retrying…</div>}

        {rec.status === "failed" && (
          <div className="alert error">
            <strong>{rec.error_message || "Processing failed."}</strong>
            {rec.can_retry ? (
              <div style={{ marginTop: 10 }}>
                <button className="primary" onClick={retry} disabled={retrying}>
                  {retrying ? "Retrying…" : "Retry"}
                </button>
                <span className="small" style={{ marginLeft: 10 }}>Finished parts won&apos;t be redone.</span>
              </div>
            ) : (
              <div className="small" style={{ marginTop: 6 }}><Link href="/">Upload the file again</Link></div>
            )}
          </div>
        )}
        {actionError && <div className="alert error">{actionError}</div>}
        {audioUrl && <audio controls preload="none" src={audioUrl} />}
      </section>

      <section className="card">
        <h2>Summary</h2>
        {rec.summary ? (
          <Markdown text={rec.summary} />
        ) : (
          <p className="pending-text">
            {rec.status === "failed" ? "No summary (processing failed)." : "The summary will appear here once the transcript is ready."}
          </p>
        )}
      </section>

      <section className="card">
        <h2>Transcript</h2>
        {rec.transcript !== null ? (
          rec.transcript ? <p className="transcript">{rec.transcript}</p> : <p className="pending-text">No speech was detected.</p>
        ) : partial ? (
          <p className="transcript">{partial} <span className="pending-text">{working ? "…" : ""}</span></p>
        ) : (
          <p className="pending-text">{rec.status === "failed" ? "No transcript." : "The transcript will appear here as chunks finish."}</p>
        )}
      </section>
    </>
  );
}
