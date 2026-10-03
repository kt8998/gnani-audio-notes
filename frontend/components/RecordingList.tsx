"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, formatDuration, RecordingListItem, TERMINAL } from "@/lib/api";
import StatusBadge from "./StatusBadge";

export default function RecordingList() {
  const [items, setItems] = useState<RecordingListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    let cancelled = false;

    async function load() {
      try {
        const data = await api.list();
        if (cancelled) return;
        setItems(data);
        setError(null);
        // Keep refreshing while anything is still in progress.
        if (data.some((r) => !TERMINAL.includes(r.status))) timer = setTimeout(load, 4000);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "Could not load your recordings.");
        timer = setTimeout(load, 8000);
      }
    }
    load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);

  return (
    <section className="card">
      <h2>Your recordings</h2>
      {error && <div className="alert error">{error}</div>}
      {items === null && !error && <p className="muted">Loading…</p>}
      {items?.length === 0 && <p className="muted">No recordings yet. Upload one above.</p>}
      {items && items.length > 0 && (
        <ul className="list">
          {items.map((r) => (
            <li key={r.id}>
              <Link href={`/recordings/${r.id}`}>
                <span className="name">{r.original_filename}</span>
                <span className="small muted">{formatDuration(r.duration_seconds)}</span>
                <span className="small muted">{new Date(r.created_at).toLocaleString()}</span>
                <StatusBadge status={r.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}
      <p className="small muted" style={{ marginBottom: 0 }}>
        History is kept per browser (no sign-in), so recordings from another device won&apos;t appear here.
      </p>
    </section>
  );
}
