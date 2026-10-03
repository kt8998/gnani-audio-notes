"""
Allow the frontend's origin(s) to PUT uploads to, and GET audio from, the R2 bucket.
Browsers enforce CORS on the presigned-URL requests, so this is required once per bucket.

Usage (from backend/, with S3_* set in .env):
    python scripts/configure_r2_cors.py https://your-app.vercel.app http://localhost:3000
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402,F401  (TLS setup before boto3)
from app.services import storage  # noqa: E402

origins = sys.argv[1:]
if not origins:
    sys.exit("usage: configure_r2_cors.py <origin> [<origin> ...]")

client = storage.get_client()
client.put_bucket_cors(
    Bucket=storage.bucket(),
    CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": origins,
        "AllowedMethods": ["PUT", "GET", "HEAD"],
        "AllowedHeaders": ["content-type"],
        "MaxAgeSeconds": 3600,
    }]},
)
print("CORS now:", client.get_bucket_cors(Bucket=storage.bucket())["CORSRules"])
