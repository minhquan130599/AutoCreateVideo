from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS_ROOT = APP_ROOT.parent
MUSE_API_ROOT = EXTENSIONS_ROOT / "MuseAI-API"

# Reuse the Muse client library from the sibling MuseAI-API project.
if str(MUSE_API_ROOT) not in sys.path:
    sys.path.insert(0, str(MUSE_API_ROOT))

SETTINGS_PATH = APP_ROOT / "settings.json"
RUNS_DIR = APP_ROOT / "runs"
OUTPUT_DIR = APP_ROOT / "output"

DEFAULT_CONTINUE_MESSAGE = (
    "Làm tiếp ngay các ảnh còn thiếu: {missing_list} (đã có {have}/{target} ảnh). "
    "Giữ nguyên bối cảnh, sản phẩm, nhãn và phong cách như các ảnh trước, đặt nhãn ảnh đúng số thứ tự. "
    "Không cần hỏi lại, không cần gửi lại ảnh đã làm."
)
# Prompts are sent exactly as VideoScript wrote them; prefixes are optional.
DEFAULT_IMAGE_PREFIX = ""
DEFAULT_VIDEO_PREFIX = ""
DEFAULT_VIDEO_RETRY_MESSAGE = (
    "Tiếp. Làm nốt các cảnh còn thiếu hoặc lỗi, rồi ghép tất cả thành 1 video hoàn chỉnh duy nhất "
    "dài {duration} giây theo đúng mô tả trên và gửi file video cuối cùng. Không cần hỏi lại."
)

# Optional text appended after VideoScript's prompts (off unless filled in).
RECOMMENDED_IMAGE_SUFFIX = (
    "Giữ nguyên 100% sản phẩm trong ảnh tham chiếu đính kèm: đúng bao bì, hình dạng, màu sắc, "
    "kích thước, chất liệu và chữ trên nhãn (nếu có). Không đổi sang sản phẩm khác, không tự thêm "
    "nhãn, logo hay chữ. Nếu prompt mô tả sản phẩm khác với ảnh tham chiếu thì làm theo ảnh tham chiếu."
)
# The clip plan / stitching now comes from VideoScript's Muse prompt; this only
# reinforces product fidelity.
RECOMMENDED_VIDEO_SUFFIX = (
    "Giữ nguyên 100% sản phẩm như trong các ảnh đính kèm: cùng bao bì, hình dạng, màu sắc, chất liệu; "
    "không thay bằng sản phẩm khác, không thêm nhãn, logo hay chữ."
)

DEFAULTS: dict[str, Any] = {
    # Sources
    "products_dir": str(APP_ROOT / "products"),
    "muse_accounts_dir": str(MUSE_API_ROOT / ".muse-web" / "accounts"),
    "videoscript_url": "http://localhost:3000",
    # VideoScript script generation (/api/generate-job body)
    "vs_api_key": "",
    "vs_model": "gpt-6-luna",
    "vs_base_url": "https://opencode.ai/zen/go/v1",
    "vs_protocol": "auto",
    "vs_max_tokens": 32000,
    "vs_background_option": "auto",
    "vs_custom_background": "",
    "vs_video_duration": 15,
    "vs_muse_clip_seconds": 10,
    "vs_muse_mode": "block",
    # True: skip the image stage; Muse makes the video straight from the product photos.
    "skip_images": False,
    "vs_num_scenes": 5,
    "vs_num_videos": 1,
    "vs_aspect_ratio": "9:16",
    "vs_additional_prompt": "",
    "vs_prompt_language": "vi",
    "vs_packaging_enabled": False,
    "vs_packaging_shape": "random",
    "vs_max_product_images": 4,
    "vs_timeout": 900,
    # Concurrency
    "threads": 2,
    "script_concurrency": 2,
    "muse_retries": 3,
    # Muse image stage
    "muse_max_input_images": 4,
    "image_prefix": DEFAULT_IMAGE_PREFIX,
    "image_suffix": "",
    "continue_message": DEFAULT_CONTINUE_MESSAGE,
    "max_image_rounds": 5,
    "image_round_timeout": 600,
    "image_idle_seconds": 20,
    # Muse video stage
    "video_prefix": DEFAULT_VIDEO_PREFIX,
    "video_suffix": "",
    "video_retry_message": DEFAULT_VIDEO_RETRY_MESSAGE,
    "video_timeout": 1800,
    "video_idle_seconds": 90,
    "video_attempts": 3,
}

INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}
BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for key, value in (data or {}).items():
        if key not in DEFAULTS or value is None:
            continue
        if key in BOOL_KEYS:
            out[key] = bool(value)
        elif key in INT_KEYS:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            out[key] = str(value)
    out["threads"] = max(1, min(20, out["threads"]))
    out["script_concurrency"] = max(1, min(10, out["script_concurrency"]))
    out["vs_num_scenes"] = max(2, min(12, out["vs_num_scenes"]))
    out["vs_num_videos"] = max(1, min(10, out["vs_num_videos"]))
    out["max_image_rounds"] = max(1, min(10, out["max_image_rounds"]))
    out["video_attempts"] = max(1, min(6, out["video_attempts"]))
    out["muse_retries"] = max(0, min(10, out["muse_retries"]))
    out["vs_muse_clip_seconds"] = max(3, min(10, out["vs_muse_clip_seconds"]))
    if out["vs_muse_mode"] not in ("block", "shot"):
        out["vs_muse_mode"] = "block"
    out["vs_max_tokens"] = max(2000, min(64000, out["vs_max_tokens"]))
    return out


def load_settings() -> dict[str, Any]:
    try:
        return normalize(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    settings = normalize(data)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    key = out.get("vs_api_key") or ""
    out["vs_api_key"] = ""
    out["vs_api_key_set"] = bool(key)
    out["vs_api_key_hint"] = ("…" + key[-4:]) if len(key) > 8 else ""
    return out
