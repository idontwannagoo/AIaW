import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://aiaw:aiaw@postgres:5432/aiaw",
)

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-prod")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.environ.get("JWT_EXPIRE_DAYS", "30"))

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://minio:9000")
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT", S3_ENDPOINT)
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "aiaw")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "aiaw-secret")
S3_BUCKET = os.environ.get("S3_BUCKET", "aiaw")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")

MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(100 * 1024 * 1024)))

SYNC_ENABLED = os.environ.get("SYNC_ENABLED", "true").lower() == "true"
