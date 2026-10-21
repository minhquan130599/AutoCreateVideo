from __future__ import annotations

import asyncio
import re
import struct
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from . import config  # noqa: F401  (puts MuseAI-API on sys.path)
from muse_ai.auth import MuseAuth
from muse_ai.client import MuseClient
from muse_ai.media import ImageRef, VideoRef, extract_image_refs, extract_video_refs
from muse_ai.text import (
    _role_of,
    assistant_response_finished,
    extract_assistant_texts,
    latest_assistant_text,
)

POLL_SECONDS = 5
USER_ROLES = {"user", "human"}
LABEL_RE = re.compile(
    r"(?:ảnh|anh|image|img|hình|hinh|frame|cảnh|canh|scene)\s*#?\s*(\d{1,2})\s*([ab])?(?![\w])",
    re.IGNORECASE,
)


def _events(history: Any) -> list | None:
    """Muse history is {"result": {"chat_events": [...]}}; older shapes use messages."""
    if isinstance(history, dict):
        result = history.get("result")
        if isinstance(result, dict) and isinstance(result.get("chat_events"), list):
            return result["chat_events"]
        if isinstance(history.get("chat_events"), list):
            return history["chat_events"]
    return _transcript(history)


def _transcript(node: Any) -> list | None:
    if isinstance(node, list):
        if any(isinstance(item, dict) and _role_of(item) for item in node):
            return node
        for item in node:
            found = _transcript(item)
            if found is not None:
                return found
    elif isinstance(node, dict):
        for key in ("messages", "result", "data", "history", "transcript"):
            if key in node:
                found = _transcript(node[key])
                if found is not None:
                    return found
    return None


def _is_user(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("event_name") == "message.user":
        return True
    payload = event.get("payload")
    role = payload.get("role") if isinstance(payload, dict) else None
    return (role or _role_of(event) or "") in USER_ROLES


def _event_id(event: Any) -> str:
    if not isinstance(event, dict):
        return repr(event)[:200]
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return str(
        payload.get("id") or event.get("message_id") or payload.get("message_id")
        or payload.get("chat_event_seq") or event.get("occurred_at_ms") or repr(event)[:200]
    )


def generated_image_refs(history: Any) -> list[ImageRef]:
    """Image refs produced by Muse, ignoring images attached by the user."""
    events = _events(history)
    if events is None:
        return extract_image_refs(history)
    return extract_image_refs([event for event in events if not _is_user(event)])


def image_key(label: str | None) -> str | None:
    """'Ảnh 1B — mở thùng' -> '1B', 'Image 4: ...' -> '4', 'Ảnh 1' -> '1A',
    'banh-ngo-terrazzo-1b' -> '1B' (file-name style labels)."""
    text = label or ""
    match = (LABEL_RE.search(text)
             or re.match(r"\s*(\d{1,2})\s*([ab])?(?![\w])", text, re.I)
             or re.search(r"(?:^|[-_\s])(\d{1,2})([ab])?(?:\.\w{2,4})?\s*$", text, re.I))
    if not match:
        return None
    number = int(match.group(1))
    letter = (match.group(2) or "").upper()
    if number == 1 and not letter:
        letter = "A"
    return f"{number}{letter}"


def expected_keys(target: int) -> list[str]:
    """VideoScript numbering: 1A (closed box), 1B (opened box), then 2..N."""
    return ["1A", "1B"] + [str(i) for i in range(2, target)]


def key_order(key: str) -> tuple[int, str]:
    match = re.match(r"(\d+)([A-Z]?)", key)
    return (int(match.group(1)), match.group(2)) if match else (999, key)


def turn_summary(
    history: Any, baseline_ids: set[str], extract: Callable[[Any], list] = extract_image_refs
) -> dict[str, Any]:
    """What Muse has emitted since our message: new events, and whether a text
    reply came after the newest media presentation."""
    events = _events(history) or []
    new = [e for e in events if _event_id(e) not in baseline_ids and not _is_user(e)]
    last_image = last_text = -1
    for i, event in enumerate(new):
        if extract(event):
            last_image = i
        elif latest_assistant_text([event]):
            last_text = i
    return {"new_events": len(new), "text_after_images": last_text > last_image >= 0,
            "text_only": last_text >= 0 and last_image < 0}


ERROR_TEXT_RE = re.compile(
    r"lỗi|không tạo được|không thể tạo|thất bại|gặp sự cố|sự cố|thử lại|error|failed|unable|could not",
    re.IGNORECASE,
)


class MuseConnection:
    """One live Muse connection. Each worker thread owns one, so two workers on
    the same account run in two independent chats.

    Long video renders can outlive the websocket, so every call has a timeout
    and a failed call drops the connection; the next call reconnects. The chat
    itself lives on Muse, so reconnecting does not lose anything.
    """

    CONNECT_TIMEOUT = 180
    CALL_TIMEOUT = 60

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.client: MuseClient | None = None
        self.failures = 0
        self.last_error = ""

    async def get(self) -> MuseClient:
        if self.client is None:
            client = MuseClient(MuseAuth(state_dir=self.state_dir), state_dir=self.state_dir)
            try:
                await asyncio.wait_for(client.connect(), self.CONNECT_TIMEOUT)
            except BaseException:
                await client.close()
                raise
            self.client = client
        return self.client

    async def reset(self) -> None:
        if self.client is not None:
            try:
                await asyncio.wait_for(self.client.close(), 15)
            except BaseException:
                pass
            self.client = None

    async def history(self, session_id: str) -> Any | None:
        """Chat history, or None after a failure (the connection is reset)."""
        try:
            client = await self.get()
            history = await asyncio.wait_for(
                client.history(session_id=session_id, limit=80), self.CALL_TIMEOUT
            )
            self.failures = 0
            return history
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            await self.reset()
            return None

    async def send(self, *, session_id: str, prompt: str, attachments: list[Path]) -> None:
        """Send one chat message; one reconnect-and-retry on a broken connection."""
        for attempt in range(2):
            try:
                client = await self.get()
                await asyncio.wait_for(
                    client.chat_stream(prompt=prompt, images=attachments, session_id=session_id),
                    self.CALL_TIMEOUT + 60,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.reset()
                if attempt:
                    raise


def _report_failures(connection: MuseConnection, on_issue: Callable[[str], None] | None) -> None:
    if on_issue and connection.failures in (3, 10, 30):
        on_issue(f"Mất kết nối Muse {connection.failures} lần liên tiếp, đang kết nối lại "
                 f"({connection.last_error})")


async def send_and_collect_images(
    connection: MuseConnection,
    *,
    session_id: str,
    prompt: str,
    attachments: list[Path],
    known: set[str],
    need: int,
    timeout: float,
    idle_seconds: float,
    on_progress: Callable[[int, str], None],
    on_issue: Callable[[str], None] | None = None,
) -> tuple[list[ImageRef], str]:
    """Send one chat turn and wait until Muse stops adding images to it.

    Muse has no "turn finished" flag (every assistant message is "completed"),
    so a turn is over when Muse writes text after its newest images and then
    stays quiet, or when nothing at all happens for a long time.
    """
    baseline_refs = set(known)
    baseline_events: set[str] = set()
    before = await connection.history(session_id)
    if before is not None:
        baseline_refs |= {ref.identity for ref in generated_image_refs(before)}
        baseline_events = {_event_id(e) for e in _events(before) or []}

    await connection.send(session_id=session_id, prompt=prompt, attachments=attachments)

    deadline = time.monotonic() + timeout
    changed_at = time.monotonic()
    refs: list[ImageRef] = []
    text = ""
    last_sig = None
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        history = await connection.history(session_id)
        if history is None:
            _report_failures(connection, on_issue)
            continue
        refs = [r for r in generated_image_refs(history) if r.identity not in baseline_refs]
        summary = turn_summary(history, baseline_events)
        new_text = latest_assistant_text([
            e for e in _events(history) or []
            if _event_id(e) not in baseline_events and not _is_user(e)
        ])
        text = new_text or text
        sig = (len(refs), summary["new_events"], new_text)
        if sig != last_sig:
            last_sig = sig
            changed_at = time.monotonic()
            on_progress(len(refs), text)
        quiet = time.monotonic() - changed_at
        if len(refs) >= need and quiet >= 8:
            break
        if refs and summary["text_after_images"] and quiet >= idle_seconds:
            break
        # Muse answered with text only: an error report ends the turn quickly,
        # anything else (question / "working on it") gets a longer grace period.
        if summary["text_only"] and ERROR_TEXT_RE.search(text) and quiet >= 20:
            break
        if summary["text_only"] and quiet >= max(90, idle_seconds * 4):
            break
        if quiet >= max(300, idle_seconds * 10):
            break
    return refs, text


def generated_video_refs(history: Any) -> list[VideoRef]:
    """Videos produced by Muse, one per file: Muse repeats the same file under
    several reference shapes (path / url / variants)."""
    events = _events(history)
    nodes = history if events is None else [e for e in events if not _is_user(e)]
    unique: dict[str, VideoRef] = {}
    for ref in extract_video_refs(nodes):
        unique.setdefault(video_name(ref), ref)
    return list(unique.values())


def video_name(ref: VideoRef) -> str:
    for candidate in (ref.path, ref.url):
        if candidate:
            name = Path(urlparse(candidate).path).name
            if name:
                return name
    return ref.identity


def mp4_duration(path: Path) -> float | None:
    """Duration from the MP4 'mvhd' box (no ffprobe needed)."""
    try:
        data = path.read_bytes()
        i = data.find(b"mvhd")
        if i < 0:
            return None
        if data[i + 4] == 1:
            timescale, duration = struct.unpack(">IQ", data[i + 24:i + 36])
        else:
            timescale, duration = struct.unpack(">II", data[i + 16:i + 24])
        return duration / timescale if timescale else None
    except Exception:
        return None


async def send_and_collect_videos(
    connection: MuseConnection,
    *,
    session_id: str,
    prompt: str,
    attachments: list[Path],
    known: set[str],
    timeout: float,
    idle_seconds: float,
    on_new: Callable[[VideoRef], Awaitable[bool]],
    on_progress: Callable[[int, str], None],
    on_issue: Callable[[str], None] | None = None,
) -> tuple[list[VideoRef], str]:
    """Send one chat turn and collect the videos Muse posts in it.

    on_new is awaited once per new video (it downloads it) and returns True when
    that video is a finished one, which ends the wait early.
    """
    baseline_events: set[str] = set()
    before = await connection.history(session_id)
    if before is not None:
        known |= {video_name(ref) for ref in generated_video_refs(before)}
        baseline_events = {_event_id(e) for e in _events(before) or []}

    await connection.send(session_id=session_id, prompt=prompt, attachments=attachments)

    deadline = time.monotonic() + timeout
    changed_at = time.monotonic()
    new_refs: list[VideoRef] = []
    text = ""
    last_sig = None
    final_found = False
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        history = await connection.history(session_id)
        if history is None:
            _report_failures(connection, on_issue)
            continue
        for ref in generated_video_refs(history):
            name = video_name(ref)
            if name in known:
                continue
            known.add(name)
            new_refs.append(ref)
            if await on_new(ref):
                final_found = True
        summary = turn_summary(history, baseline_events, extract_video_refs)
        new_text = latest_assistant_text([
            e for e in _events(history) or []
            if _event_id(e) not in baseline_events and not _is_user(e)
        ])
        text = new_text or text
        sig = (len(new_refs), summary["new_events"], new_text)
        if sig != last_sig:
            last_sig = sig
            changed_at = time.monotonic()
            on_progress(len(new_refs), text)
        quiet = time.monotonic() - changed_at
        if final_found and quiet >= 10:
            break
        # Video rendering has long silent gaps, so only a closing text after
        # the newest video (or a text-only answer) ends the turn.
        if new_refs and summary["text_after_images"] and quiet >= idle_seconds:
            break
        # Muse reported a failure (e.g. "công cụ tạo video gặp lỗi") with no video.
        if not new_refs and summary["text_only"] and ERROR_TEXT_RE.search(text) and quiet >= 20:
            break
        if summary["text_only"] and quiet >= max(120, idle_seconds * 2):
            break
        if quiet >= max(600, idle_seconds * 6):
            break
    return new_refs, text
