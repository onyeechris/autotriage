import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="autotriage-demo-app")

LOG_PATH = os.environ.get("APP_LOG_PATH", "/var/log/autotriage/app.log")
Path(os.path.dirname(LOG_PATH)).mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("autotriage")
logger.setLevel(logging.INFO)
logger.propagate = False
_formatter = logging.Formatter("%(message)s")

_file_handler = logging.FileHandler(LOG_PATH)
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(_formatter)
logger.addHandler(_stream_handler)

CHAOS_JUNK_PREFIX = "autotriage-chaos-junk-"
CHAOS_DISK_TARGET_PERCENT = 90
CHAOS_DISK_CHUNK_BYTES = 10 * 1024 * 1024  # 10 MiB
CHAOS_DISK_MAX_FILES = 2000  # safety cap so a bug here can't fill the whole disk unboundedly

state = {"force_500": False}


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    start = time.time()
    if state["force_500"] and request.url.path != "/chaos/reset":
        response = JSONResponse(status_code=500, content={"error": "chaos_500_active"})
    else:
        response = await call_next(request)

    logger.info(json.dumps({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": request.method,
        "path": request.url.path,
        "status_code": response.status_code,
        "duration_ms": round((time.time() - start) * 1000, 2),
        "client": request.client.host if request.client else None,
    }))
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chaos/500")
async def chaos_500():
    state["force_500"] = True
    return {"chaos": "500_enabled", "note": "every subsequent request will 500 until /chaos/reset"}


@app.post("/chaos/crash")
async def chaos_crash():
    logger.warning(json.dumps({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "chaos_crash_triggered",
    }))
    os._exit(1)


@app.post("/chaos/disk")
async def chaos_disk():
    junk_dir = Path("/tmp")
    written_files = 0
    while True:
        usage = shutil.disk_usage("/")
        percent_used = usage.used / usage.total * 100
        if percent_used >= CHAOS_DISK_TARGET_PERCENT or written_files >= CHAOS_DISK_MAX_FILES:
            break
        file_path = junk_dir / f"{CHAOS_JUNK_PREFIX}{written_files}"
        with open(file_path, "wb") as f:
            f.write(os.urandom(CHAOS_DISK_CHUNK_BYTES))
        written_files += 1

    usage = shutil.disk_usage("/")
    return {
        "chaos": "disk_filled",
        "percent_used": round(usage.used / usage.total * 100, 1),
        "files_written": written_files,
    }


@app.post("/chaos/reset")
async def chaos_reset():
    state["force_500"] = False
    removed = 0
    for path in Path("/tmp").glob(f"{CHAOS_JUNK_PREFIX}*"):
        path.unlink(missing_ok=True)
        removed += 1
    return {"chaos": "reset", "junk_files_removed": removed}
