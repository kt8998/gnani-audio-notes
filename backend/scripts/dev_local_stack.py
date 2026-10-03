"""
Local development stack in one process: S3 stand-in (moto) + FastAPI API + worker.
Uses the real DATABASE_URL, Gnani and Gemini keys from backend/.env.

If S3_ENDPOINT_URL is set in .env (Supabase Storage), the stand-in is skipped and the real bucket is used.

    python scripts/dev_local_stack.py      # API on http://localhost:8000
"""

import os
import sys
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import dotenv_values  # noqa: E402

S3_PORT = 9000
use_stand_in = not dotenv_values(BACKEND / ".env").get("S3_ENDPOINT_URL")
if use_stand_in:
    # Must be set before `import app`, which reads settings.
    os.environ.update(
        S3_ENDPOINT_URL=f"http://localhost:{S3_PORT}", S3_ACCESS_KEY_ID="local", S3_SECRET_ACCESS_KEY="local",
        S3_BUCKET="local-bucket", S3_REGION="us-east-1",
    )

import app  # noqa: E402,F401  (TLS setup before boto3 is imported)


def start_stand_in():
    import boto3
    from moto.server import ThreadedMotoServer

    ThreadedMotoServer(ip_address="127.0.0.1", port=S3_PORT, verbose=False).start()
    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], aws_access_key_id="local",
                      aws_secret_access_key="local", region_name="us-east-1")
    s3.create_bucket(Bucket="local-bucket")
    s3.put_bucket_cors(Bucket="local-bucket", CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": ["*"], "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["*"],
    }]})
    print(f"S3 stand-in on http://localhost:{S3_PORT} (bucket local-bucket)")


def main():
    import logging

    import uvicorn
    from alembic import command
    from alembic.config import Config

    from app.worker.run import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if use_stand_in:
        start_stand_in()

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")

    def worker_loop():  # run_forever() installs signal handlers, which only work on the main thread
        import time

        worker = Worker()
        while True:
            try:
                if not worker.run_once():
                    time.sleep(2)
            except Exception:
                logging.exception("worker loop error")
                time.sleep(10)

    threading.Thread(target=worker_loop, daemon=True).start()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
