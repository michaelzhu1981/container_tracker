"""Local web UI: view query status, start and stop tracking jobs."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import ROOT, SCREENSHOT_DIR
from excel_io import ExcelReadError
from job import JobBusyError, JobIdleError, JobManager, JobStartError

STATIC_DIR = ROOT / "static"


class StartRequest(BaseModel):
    resume: bool = False
    wait_for_challenge: bool = True
    headed: bool = False
    carriers: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)


def create_app(manager: JobManager | None = None) -> FastAPI:
    job = manager or JobManager()
    app = FastAPI(title="Container Tracker", docs_url=None, redoc_url=None)
    app.state.job = job

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict:
        return job.snapshot()

    @app.post("/api/start")
    async def start(body: StartRequest) -> dict:
        try:
            job.start(
                resume=body.resume,
                wait_for_challenge=body.wait_for_challenge,
                headed=body.headed,
                carriers=body.carriers,
                limit=body.limit,
            )
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (JobStartError, ExcelReadError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()

    @app.post("/api/stop")
    def stop() -> dict:
        try:
            job.request_stop()
        except JobIdleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()

    @app.post("/api/reload")
    def reload_board() -> dict:
        try:
            job.reload_from_disk()
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExcelReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.snapshot()

    @app.get("/screenshots/{name}")
    def screenshot(name: str) -> FileResponse:
        path = (SCREENSHOT_DIR / name).resolve()
        root = SCREENSHOT_DIR.resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(status_code=404, detail="Screenshot not found.")
        return FileResponse(path)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    from runner import configure_logging

    configure_logging()
    run_server()
