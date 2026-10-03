"""
Integration-test setup.

* Database: a separate "<name>_test" database on the same Postgres server as
  DATABASE_URL, created automatically. Real Postgres, so FOR UPDATE SKIP LOCKED,
  constraints and migrations are exercised for real.
* Storage: moto's local S3-compatible server stands in for Supabase Storage's S3 API, so
  presigned PUT URLs work over real HTTP.
Environment variables are set here, before any app module reads its settings.
"""

import os
import socket
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from dotenv import dotenv_values

BACKEND = Path(__file__).resolve().parent.parent
_base_url = os.environ.get("DATABASE_URL") or dotenv_values(BACKEND / ".env").get("DATABASE_URL") or ""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


S3_PORT = _free_port()
os.environ.update(
    S3_ENDPOINT_URL=f"http://127.0.0.1:{S3_PORT}",
    S3_ACCESS_KEY_ID="test",
    S3_SECRET_ACCESS_KEY="test",
    S3_BUCKET="test-bucket",
    S3_REGION="us-east-1",
    GNANI_API_KEY="unused-in-tests",
    GEMINI_API_KEY="unused-in-tests",
)

if _base_url:
    parts = urlsplit(_base_url)
    test_db_name = parts.path.lstrip("/") + "_test"
    TEST_DATABASE_URL = urlunsplit(parts._replace(path="/" + test_db_name))
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
else:
    TEST_DATABASE_URL = ""


def _ensure_test_database():
    import psycopg

    plain = _base_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(plain, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db_name,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{test_db_name}"')


@pytest.fixture(scope="session")
def database():
    if not TEST_DATABASE_URL:
        pytest.skip("DATABASE_URL not set in backend/.env")
    _ensure_test_database()

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.downgrade(cfg, "base")  # start from an empty schema every session...
    command.upgrade(cfg, "head")  # ...so the migration itself is tested from scratch
    return cfg


@pytest.fixture(scope="session")
def s3():
    import boto3
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=S3_PORT, verbose=False)
    server.start()
    boto3.client(
        "s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], aws_access_key_id="test",
        aws_secret_access_key="test", region_name="us-east-1",
    ).create_bucket(Bucket="test-bucket")
    yield
    server.stop()


@pytest.fixture
def db(database):
    from sqlalchemy import text

    from app.db import SessionLocal, engine

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE chunks, recordings"))
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def api(db, s3, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import storage

    client = TestClient(app)
    client.uploaded = {}  # recording id -> bytes, filled by the tests' upload() helper

    # On the development laptop, antivirus resets plain-HTTP downloads over ~400 KB on
    # localhost (verified with a bare http.server, independent of this code). So when the
    # worker downloads, hand it the bytes the test just uploaded instead of fetching them
    # back over loopback. Uploads still go over real HTTP, and test_storage_download_roundtrip
    # exercises the real download_to() with a small file.
    def download_uploaded_bytes(key, path):
        Path(path).write_bytes(client.uploaded[key.split("/")[1]])

    monkeypatch.setattr(storage, "download_to", download_uploaded_bytes)
    return client
