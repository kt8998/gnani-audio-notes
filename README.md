# Audio Notes

Upload an audio file and get a transcript (Gnani speech-to-text) and a summary (Google Gemini).
Built for the Gnani internship take-home.

- **Frontend:** Next.js (Vercel), in `frontend/`
- **Backend:** FastAPI API + background worker (Railway), in `backend/`
- **Database:** PostgreSQL (Railway), which also serves as the job queue
- **Storage:** Cloudflare R2 (private bucket; the browser uploads via presigned URLs)

The live app's `/architecture` page explains the design: the upload → transcript flow, long-audio chunking,
sync vs background work, failure handling and trade-offs.

## How it works (short version)

1. The browser asks the API for a presigned URL, uploads the file **directly to R2** (with real progress), then confirms.
2. The API marks the recording `queued`. A separate **worker** process claims it (`SELECT … FOR UPDATE SKIP LOCKED`).
3. The worker decodes the audio with ffmpeg, splits it into **25 s chunks** (the live Gnani API rejects anything over 30 s),
   transcribes 3 chunks at a time with retries, joins the text, and asks Gemini for a summary.
4. The frontend polls `GET /api/recordings/{id}` every 2 s and shows the stage, chunk progress, the transcript as it
   fills in, the summary, and any error with a Retry button. Retries skip work that already succeeded.

## Run locally

```bash
# backend
cd backend
python -m venv .venv && .venv/Scripts/activate      # (Linux/macOS: source .venv/bin/activate)
pip install -r requirements-dev.txt
cp .env.example .env                                # fill in DATABASE_URL, GNANI_API_KEY, GEMINI_API_KEY (S3_* optional)
python scripts/dev_local_stack.py                   # API on :8000 + worker (+ local S3 stand-in if S3_* is empty)

# frontend
cd frontend
npm install
npm run dev                                         # http://localhost:3000
```

## Tests

```bash
cd backend
pytest            # 54 tests; database tests use a separate "<db>_test" database next to DATABASE_URL
```

Gnani and Gemini are replaced in the automated tests by replayed responses whose formats were verified against the
live APIs. `scripts/gnani_smoke_test.py` and `scripts/run_local_e2e.py` call the real services.

## Deploy

| Service | Where | Notes |
|---|---|---|
| API | Railway, root `backend/`, Dockerfile | default command runs migrations, then uvicorn |
| Worker | Railway, same repo/Dockerfile | start command: `python -m app.worker.run` |
| Postgres | Railway | `DATABASE_URL` referenced from the Postgres service |
| Storage | Cloudflare R2 | run `scripts/configure_r2_cors.py <frontend-origin>` once |
| Frontend | Vercel, root `frontend/` | `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_GITHUB_URL` |

Backend env vars: see `backend/.env.example`. No secrets are ever sent to the browser.
