from __future__ import annotations

import argparse
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .config import (
    OUTPUT_DIR,
    RECOMMENDED_IMAGE_SUFFIX,
    RECOMMENDED_VIDEO_SUFFIX,
    load_settings,
    public_settings,
    save_settings,
)
from .engine import Engine, list_products, load_accounts, safe_name
from .images import list_images

STATIC_DIR = Path(__file__).resolve().parent / "static"
engine: Engine | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine
    engine = Engine()
    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(title="AutoCreateVideo", lifespan=lifespan)


class StartRequest(BaseModel):
    mode: str = "manual"  # "manual" | "random"
    products: list[str] = []
    count: int = 1


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return public_settings(load_settings())


@app.get("/api/settings/templates")
async def settings_templates() -> dict[str, str]:
    return {"image_suffix": RECOMMENDED_IMAGE_SUFFIX, "video_suffix": RECOMMENDED_VIDEO_SUFFIX}


@app.put("/api/settings")
async def put_settings(body: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    # An empty key field means "keep the saved key".
    if not body.get("vs_api_key"):
        body["vs_api_key"] = current.get("vs_api_key", "")
    if body.get("vs_api_key_clear"):
        body["vs_api_key"] = ""
    return public_settings(save_settings({**current, **body}))


@app.get("/api/products")
async def products() -> dict[str, Any]:
    settings = load_settings()
    products_dir = Path(settings["products_dir"])
    return {"dir": str(products_dir), "exists": products_dir.is_dir(),
            "products": list_products(products_dir)}


@app.get("/api/products/{name}/thumb")
async def product_thumb(name: str) -> FileResponse:
    root = Path(load_settings()["products_dir"]).resolve()
    folder = (root / name).resolve()
    if folder.parent != root or not folder.is_dir():
        raise HTTPException(404)
    images = list_images(folder)
    if not images:
        raise HTTPException(404)
    return FileResponse(images[0])


@app.get("/api/accounts")
async def accounts() -> dict[str, Any]:
    settings = load_settings()
    items = load_accounts(Path(settings["muse_accounts_dir"]))
    for item in items:
        item.pop("state_dir", None)
    return {"accounts": items, "usable": sum(a["usable"] for a in items)}


@app.get("/api/videoscript/options")
async def videoscript_options() -> dict[str, Any]:
    base = load_settings()["videoscript_url"].rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            backgrounds = (await client.get(f"{base}/api/backgrounds")).json()
            shapes = (await client.get(f"{base}/api/bag-shapes")).json()
        return {"online": True, "backgrounds": backgrounds, "bag_shapes": shapes}
    except Exception as exc:
        return {"online": False, "error": str(exc), "backgrounds": [], "bag_shapes": []}


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return engine.snapshot()


@app.post("/api/run")
async def start_run(body: StartRequest) -> dict[str, Any]:
    settings = load_settings()
    if body.mode == "random":
        names = engine.random_products(Path(settings["products_dir"]), body.count)
    else:
        names = list(dict.fromkeys(body.products))
    try:
        return engine.start(settings, names)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/run/retry")
async def retry_run() -> dict[str, Any]:
    try:
        return engine.retry(load_settings())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/run/stop")
async def stop_run() -> dict[str, Any]:
    await engine.stop()
    return engine.snapshot()


@app.get("/api/run/download")
async def download_videos(product: str | None = None) -> FileResponse:
    """All finished videos of the current run (or one product) as one zip."""
    run = engine.run
    if not run:
        raise HTTPException(404, "Chưa có phiên nào")
    entries: list[tuple[Path, str]] = []
    for item in run["products"]:
        if product and item["name"] != product:
            continue
        for variant in item["variants"]:
            for index, url in enumerate(variant.get("videos") or ([variant["video"]] if variant.get("video") else [])):
                path = OUTPUT_DIR / url.removeprefix("/files/")
                if path.is_file():
                    suffix = f"_{index + 1}" if index else ""
                    entries.append((path, f"{safe_name(item['name'])}_v{variant['index']}{suffix}.mp4"))
    if not entries:
        raise HTTPException(404, "Chưa có video nào hoàn tất")
    name = f"videos_{run['id']}" + (f"_{safe_name(product)}" if product else "") + ".zip"
    target = Path(tempfile.gettempdir()) / f"autovideo_{uuid.uuid4().hex}.zip"
    # Videos are already compressed: store them without recompressing.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, arcname in entries:
            archive.write(path, arcname)
    return FileResponse(target, media_type="application/zip", filename=name,
                        background=BackgroundTask(target.unlink, missing_ok=True))


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=OUTPUT_DIR), name="files")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="AutoCreateVideo dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    print(f"AutoCreateVideo: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
