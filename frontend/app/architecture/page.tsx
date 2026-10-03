import type { Metadata } from "next";

export const metadata: Metadata = { title: "Architecture · Audio Notes" };

const GITHUB_URL = process.env.NEXT_PUBLIC_GITHUB_URL || "https://github.com/";

export default function Architecture() {
  return (
    <article className="card prose">
      <h1>How this works</h1>
      <p>
        Source code: <a href={GITHUB_URL}>{GITHUB_URL}</a>
      </p>

      <h2>The pieces</h2>
      <ul>
        <li><strong>Frontend</strong>: Next.js on Vercel. Upload page, recording page, and this page.</li>
        <li><strong>API</strong>: FastAPI on Railway. Short, fast HTTP requests only.</li>
        <li><strong>Worker</strong>: a second process on Railway (same code, different start command) that does all the slow work.</li>
        <li><strong>PostgreSQL</strong> (Railway): recording metadata, processing status, per-chunk progress, transcripts and summaries. Also used as the job queue.</li>
        <li><strong>Object storage</strong> (Supabase Storage, private bucket <code>audio-notes</code>, used through its S3-compatible API): the original audio files. Audio never goes into the database.</li>
        <li><strong>Gnani speech-to-text</strong> (REST API) for transcription, and <strong>Google Gemini</strong> (<code>gemini-3.8-flash</code>) for the summary.</li>
      </ul>

      <h2>From upload to transcript</h2>
      <ol>
        <li>The browser asks the API to create a recording. The API validates the file type, size and language, inserts a row with status <code>uploading</code>, and returns a <strong>presigned upload URL</strong> valid for 15 minutes.</li>
        <li>The browser uploads the file <strong>directly to the storage bucket</strong> with that URL (that&apos;s where the real upload percentage comes from). Files never pass through our API server.</li>
        <li>The browser tells the API the upload finished. The API checks the object exists in the bucket and its size matches, then sets the status to <code>queued</code>.</li>
        <li>The worker claims the oldest queued recording with <code>SELECT … FOR UPDATE SKIP LOCKED</code>, so two workers can never take the same job.</li>
        <li><code>preprocessing</code>: the worker downloads the file, and ffmpeg converts whatever was uploaded (mp3, m4a, …) to 16&nbsp;kHz mono WAV. A corrupt or non-audio file fails here with a clear message.</li>
        <li><code>transcribing</code>: the WAV is cut into 25-second chunks, which are sent to Gnani three at a time. Progress (&ldquo;7 of 24 chunks&rdquo;) and each chunk&apos;s text are saved after every chunk, so the page shows the transcript filling in.</li>
        <li><code>summarizing</code>: the chunk transcripts are joined in order and sent to Gemini, which writes a summary in the same language as the transcript and is told not to invent anything.</li>
        <li><code>completed</code>. The page polls the API every 2 seconds and stops when it reaches <code>completed</code> or <code>failed</code>.</li>
      </ol>

      <h2>How long audio is handled</h2>
      <p>
        Gnani&apos;s REST endpoint transcribes one short clip per request. The docs say up to 60 seconds,
        but when I tested the live API it rejected anything over <strong>30 seconds</strong> (<code>MAX_AUDIO_DURATION_EXCEEDED</code>).
        So every recording, however long, is split into 25-second chunks; a last chunk under 2 seconds is merged into the
        previous one, which still stays under 30 seconds. Each chunk is its own row in the database with its own status and
        attempt count. That gives real progress, lets three chunks run in parallel, and means a retry only re-sends the chunks
        that failed. A silent chunk comes back from Gnani as an empty transcript, which counts as valid &ldquo;no speech&rdquo;, not an error.
        Very long transcripts are summarized in parts and the part summaries are then combined; nothing is silently cut off.
      </p>
      <p>
        <strong>Size limit:</strong> uploads are capped at 50 MB, which is the per-file limit of Supabase Storage&apos;s free plan.
        That is about 50 minutes of 128&nbsp;kbps MP3 (a 2-minute MP3 is about 2&nbsp;MB), but only about 4–5 minutes of
        uncompressed 44.1&nbsp;kHz stereo WAV. Bigger files are rejected before upload, with a clear message.
        The processing pipeline itself has no length limit.
      </p>

      <h2>What runs synchronously vs in the background</h2>
      <p>
        <strong>Synchronous (inside an API request, milliseconds):</strong> validating and creating the recording, issuing presigned
        URLs, confirming the upload, listing and reading recordings, and queuing a retry.
      </p>
      <p>
        <strong>Background (worker process):</strong> downloading, decoding, chunking, every Gnani call, the Gemini call, and
        housekeeping (re-queuing jobs whose worker died, and expiring uploads that never finished). I used a separate process with
        Postgres as the queue instead of FastAPI&apos;s <code>BackgroundTasks</code>, because those run inside the API process and are lost
        if it restarts. I also didn&apos;t add Redis or Celery, because Postgres was already required and is enough at this scale.
      </p>

      <h2>Failures</h2>
      <ul>
        <li><strong>Gnani</strong>: 429, 5xx, timeouts and connection errors are retried with exponential backoff (up to 3 attempts per chunk).
          401/403 mean a configuration problem, so the job stops immediately without retrying. Gnani has two different error formats; both are handled.</li>
        <li><strong>Gemini</strong>: retried with backoff by the SDK (configured explicitly, because it doesn&apos;t retry by default). If summarizing fails, the transcript is kept.</li>
        <li><strong>Retry is idempotent</strong>: each stage checks whether its result already exists, so a retry never redoes finished chunks
          (or pays for them again) and never creates duplicate rows.</li>
        <li><strong>Crashed workers</strong>: the worker updates a heartbeat after every chunk. A job silent for 10 minutes goes back
          to the queue, and after 3 crashes it is marked failed.</li>
        <li>Every failure stores a message that&apos;s safe to show the user (shown on the recording page, with a Retry button when that makes sense) and a separate internal detail that the API never returns.</li>
      </ul>

      <h2>Privacy without accounts</h2>
      <p>
        There is no login. Each browser generates a random ID, keeps it in localStorage and sends it as a header. You can
        only list, retry or confirm your own recordings, so there&apos;s no public list of everyone&apos;s uploads. A single recording
        can be opened by its random UUID, so a link can be shared. The bucket is private; playback uses short-lived signed URLs.
        All API keys stay on the server. Note that the Gemini free tier allows Google to use submitted content to improve
        its products, so I wouldn&apos;t use this setup for sensitive audio.
      </p>

      <h2>What I&apos;d do differently with more time</h2>
      <ul>
        <li>Cut chunks at pauses (ffmpeg silence detection) instead of every 25 seconds. Hard cuts sometimes garble a word at the boundary.</li>
        <li>Try Gnani&apos;s Batch STT API for long files (up to 4 hours per file, diarization) and compare quality and cost against chunking.</li>
        <li>Real accounts instead of the per-browser ID, plus delete and rename.</li>
        <li>Push progress over Server-Sent Events instead of polling, and add structured logging and metrics (Gnani latency, retry rate).</li>
        <li>Detect the language automatically instead of asking the user, and add speaker labels.</li>
        <li>Use a paid LLM tier so user content isn&apos;t used for training, and add rate limiting on the upload endpoint.</li>
        <li>Lift the 50 MB cap: a paid storage plan, or resumable/multipart uploads, or compressing audio in the browser before upload.</li>
      </ul>
    </article>
  );
}
