from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .images import data_url


def build_payload(settings: dict[str, Any], images: list[Path]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "images": [
            {"imageBase64": data_url(path), "mimeType": "image/jpeg"}
            for path in images
        ],
        "model": settings["vs_model"],
        "baseUrl": settings["vs_base_url"],
        "protocol": settings["vs_protocol"],
        "maxTokens": settings["vs_max_tokens"],
        "backgroundOption": settings["vs_background_option"],
        "customBackground": settings["vs_custom_background"],
        "videoDuration": settings["vs_video_duration"],
        "museClipSeconds": settings["vs_muse_clip_seconds"],
        "museMode": settings["vs_muse_mode"],
        "museImageSource": "product" if settings["skip_images"] else "scenes",
        # Only the GPT image prompts and the Grok shot list are used here, so VideoScript
        # skips the other platforms' prompts and writes each video in its own parallel call.
        "outputProfile": "lean",
        "parallelVideos": True,
        "numScenes": settings["vs_num_scenes"],
        "numVideos": settings["vs_num_videos"],
        "aspectRatio": settings["vs_aspect_ratio"],
        "additionalPrompt": settings["vs_additional_prompt"],
        "promptLanguage": settings["vs_prompt_language"],
        "packagingOverride": (
            {"enabled": True, "shape": settings["vs_packaging_shape"]}
            if settings["vs_packaging_enabled"] else None
        ),
    }
    # Without a key, VideoScript falls back to OPENCODE_API_KEY from its own .env
    # (only for requests made from this machine).
    if settings["vs_api_key"]:
        payload["apiKey"] = settings["vs_api_key"]
    return payload


async def generate_scripts(
    settings: dict[str, Any],
    images: list[Path],
    on_tick: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Start a VideoScript background job and poll it until it finishes."""
    base = settings["videoscript_url"].rstrip("/")
    payload = await asyncio.to_thread(build_payload, settings, images)
    deadline = time.monotonic() + settings["vs_timeout"]
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(f"{base}/api/generate-job", json=payload)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Không kết nối được VideoScript tại {base}: {exc}") from exc
        data = response.json()
        if response.status_code >= 400 or not data.get("jobId"):
            raise RuntimeError(data.get("error") or f"VideoScript HTTP {response.status_code}")
        job_id = data["jobId"]
        while time.monotonic() < deadline:
            await asyncio.sleep(4)
            response = await client.get(f"{base}/api/jobs/{job_id}")
            data = response.json()
            if response.status_code == 404:
                raise RuntimeError(data.get("error") or "VideoScript mất tiến trình")
            if not data.get("done"):
                if on_tick:
                    on_tick(int(data.get("elapsedSeconds") or 0))
                continue
            result = data.get("result") or {}
            if not result.get("success"):
                raise RuntimeError(result.get("error") or "VideoScript trả lỗi không rõ")
            return result
    raise TimeoutError("VideoScript tạo kịch bản quá thời gian cho phép")


def extract_variants(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick the fields the Muse pipeline needs from each generated script."""
    data = result.get("data") or {}
    variants = []
    for index, video in enumerate(data.get("videos") or []):
        scenes = video.get("scenes") or []
        image_prompt = video.get("master_image_prompt_gpt") or ""
        vp = video.get("video_prompt") or {}
        # VideoScript's Muse prompt already plans the 10s generations and the final edit.
        video_prompt = (vp.get("video_prompt_muse") or vp.get("master_ai_video_prompt") or "")
        if not scenes or not image_prompt or not video_prompt:
            continue
        variants.append({
            "index": index + 1,
            "title": video.get("title_vi") or f"Kịch bản {index + 1}",
            "concept": video.get("concept_vibe") or "",
            # Scenes plus the extra hook-reveal image 1B.
            "image_target": len(scenes) + 1,
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "script": video,
        })
    return variants
