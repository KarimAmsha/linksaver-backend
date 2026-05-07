import os
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl


APP_NAME = "LinkSaver Backend"

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
BACKEND_API_KEY = os.getenv("BACKEND_API_KEY", "").strip()

MAX_FILE_AGE_MINUTES = int(os.getenv("MAX_FILE_AGE_MINUTES", "60"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "500"))

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title=APP_NAME,
    version="1.0.0",
    description="Backend resolver for LinkSaver Android app"
)

app.mount("/files", StaticFiles(directory=str(DOWNLOAD_DIR)), name="files")


class ResolveRequest(BaseModel):
    url: HttpUrl


class ResolveResponse(BaseModel):
    success: bool
    platform: str
    title: Optional[str] = None
    fileName: Optional[str] = None
    downloadUrl: Optional[str] = None
    message: str


DIRECT_VIDEO_EXTENSIONS = (
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".3gp",
    ".3gpp"
)


def require_api_key(x_api_key: Optional[str]) -> None:
    if not BACKEND_API_KEY:
        return

    if not x_api_key or x_api_key != BACKEND_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized request"
        )


def get_public_base_url(request: Request) -> str:
    if BASE_URL:
        return BASE_URL

    return str(request.base_url).rstrip("/")


def is_direct_video_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(DIRECT_VIDEO_EXTENSIONS)


def detect_platform(url: str) -> str:
    value = url.lower()
    host = urlparse(value).netloc.lower()

    if "tiktok.com" in host:
        return "TIKTOK"

    if "instagram.com" in host or "cdninstagram.com" in host:
        return "INSTAGRAM"

    if "facebook.com" in host or "fb.watch" in host or "fbcdn.net" in host:
        return "FACEBOOK"

    if "youtube.com" in host or "youtu.be" in host or "googlevideo.com" in host:
        return "YOUTUBE"

    if "linkedin.com" in host or "licdn.com" in host:
        return "LINKEDIN"

    if is_direct_video_url(value):
        return "DIRECT_VIDEO_URL"

    return "UNKNOWN"


def cleanup_old_files() -> None:
    now = time.time()
    max_age_seconds = MAX_FILE_AGE_MINUTES * 60

    for file_path in DOWNLOAD_DIR.glob("*"):
        if not file_path.is_file():
            continue

        file_age = now - file_path.stat().st_mtime

        if file_age > max_age_seconds:
            try:
                file_path.unlink()
            except Exception:
                pass


def find_downloaded_file(video_id: str) -> Optional[Path]:
    matches = list(DOWNLOAD_DIR.glob(f"{video_id}.*"))

    if not matches:
        return None

    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def build_download_url(request: Request, file_name: str) -> str:
    public_base_url = get_public_base_url(request)
    return f"{public_base_url}/files/{file_name}"


def build_ydl_options(output_template: str) -> dict:
    return {
        "outtmpl": output_template,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            )
        },
    }


@app.get("/")
def root():
    return {
        "success": True,
        "name": APP_NAME,
        "message": "LinkSaver backend is running"
    }


@app.get("/health")
def health():
    return {
        "success": True,
        "status": "ok"
    }


@app.post("/resolve", response_model=ResolveResponse)
def resolve_video(
    request_body: ResolveRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None)
):
    require_api_key(x_api_key)
    cleanup_old_files()

    url = str(request_body.url)
    platform = detect_platform(url)

    if is_direct_video_url(url):
        return ResolveResponse(
            success=True,
            platform=platform,
            title=None,
            fileName=None,
            downloadUrl=url,
            message="Direct video URL detected"
        )

    video_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{video_id}.%(ext)s")
    ydl_opts = build_ydl_options(output_template)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        downloaded_file = find_downloaded_file(video_id)

        if downloaded_file is None or not downloaded_file.exists():
            raise HTTPException(
                status_code=500,
                detail="Video was processed but output file was not found"
            )

        file_name = downloaded_file.name
        download_url = build_download_url(request, file_name)

        title = None
        if isinstance(info, dict):
            title = info.get("title")

        return ResolveResponse(
            success=True,
            platform=platform,
            title=title,
            fileName=file_name,
            downloadUrl=download_url,
            message="Video resolved successfully"
        )

    except yt_dlp.utils.DownloadError as error:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve video: {str(error)}"
        )

    except HTTPException:
        raise

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected backend error: {str(error)}"
        )
