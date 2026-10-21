from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import OUTPUT_DIR, RUNS_DIR
from .images import list_images, shrink_copy
from .muse_flow import (
    MuseClient,
    MuseConnection,
    expected_keys,
    image_key,
    key_order,
    mp4_duration,
    send_and_collect_images,
    send_and_collect_videos,
    video_name,
)
from .videoscript import extract_variants, generate_scripts

FINAL_VARIANT = {"done", "failed", "stopped"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    return re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_") or "item"


def file_url(path: Path) -> str:
    return "/files/" + path.relative_to(OUTPUT_DIR).as_posix()


TRANSIENT_RE = re.compile(
    r"HTTP 5\d\d|status(?: code)?:? 5\d\d|websocket|connection (?:closed|reset|refused|lost)"
    r"|connectionclosed|timed? ?out|temporarily|bad gateway|service unavailable",
    re.IGNORECASE,
)


def is_transient(exc: BaseException) -> bool:
    """Network / gateway failures worth retrying (not Muse refusing the task)."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return bool(TRANSIENT_RE.search(f"{type(exc).__name__}: {exc}"))


def fill_suffix(template: str, scene_count: int, duration: int) -> str:
    template = (template or "").strip()
    if not template:
        return ""
    return "\n\n" + (template.replace("{count}", str(scene_count))
                     .replace("{duration}", str(duration)))


# ---------------------------------------------------------------- accounts

def load_accounts(accounts_dir: Path) -> list[dict[str, Any]]:
    """Muse accounts managed by MuseAI-API (accounts.json + one session dir each)."""
    try:
        records = json.loads((accounts_dir / "accounts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    accounts = []
    for record in records if isinstance(records, list) else []:
        account_id = record.get("id")
        if not account_id:
            continue
        state_dir = accounts_dir / account_id
        has_session = (state_dir / "cookies.json").exists()
        accounts.append({
            "id": account_id,
            "label": record.get("label") or record.get("email") or account_id[:8],
            "enabled": bool(record.get("enabled", True)),
            "has_session": has_session,
            "usable": bool(record.get("enabled", True)) and has_session,
            "state_dir": str(state_dir),
        })
    return accounts


def list_products(products_dir: Path) -> list[dict[str, Any]]:
    if not products_dir.is_dir():
        return []
    products = []
    for folder in sorted(p for p in products_dir.iterdir() if p.is_dir()):
        images = list_images(folder)
        products.append({
            "name": folder.name,
            "image_count": len(images),
            "thumb": f"/api/products/{folder.name}/thumb" if images else None,
        })
    return products


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self) -> None:
        self.run: dict[str, Any] | None = None
        self.settings: dict[str, Any] = {}
        self.workers: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._load_last_run()

    # ---- persistence
    def _run_path(self) -> Path:
        return RUNS_DIR / f"{self.run['id']}.json"

    def save(self) -> None:
        if not self.run:
            return
        temp = self._run_path().with_suffix(".tmp")
        temp.write_text(json.dumps(self.run, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(self._run_path())

    def _load_last_run(self) -> None:
        files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return
        try:
            self.run = json.loads(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        # Anything that was in flight when the server stopped is interrupted.
        changed = False
        for product in self.run["products"]:
            if product["stage"] in ("script_queued", "script"):
                product.update(stage="failed", message="Bị gián đoạn khi tắt server")
                changed = True
            for variant in product["variants"]:
                if variant["stage"] not in FINAL_VARIANT:
                    variant.update(stage="stopped", message="Bị gián đoạn khi tắt server")
                    changed = True
        if self.run.get("status") == "running":
            self.run["status"] = "stopped"
            changed = True
        if changed:
            self.save()

    # ---- state helpers
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def log(self, item: dict[str, Any], text: str) -> None:
        item.setdefault("logs", []).append(f"[{datetime.now():%H:%M:%S}] {text}")
        del item["logs"][:-60]
        item["message"] = text
        item["updated_at"] = now()
        self.save()

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "run": self.run,
            "workers": self.workers,
        }

    # ---- control
    def start(self, settings: dict[str, Any], product_names: list[str]) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("Đang có phiên chạy, hãy dừng trước khi bắt đầu phiên mới")
        products_dir = Path(settings["products_dir"])
        available = {p["name"] for p in list_products(products_dir) if p["image_count"]}
        names = [name for name in product_names if name in available]
        if not names:
            raise ValueError("Không có sản phẩm hợp lệ (thư mục phải có ảnh)")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run = {
            "id": run_id,
            "created_at": now(),
            "status": "running",
            "products": [
                {
                    "id": uuid.uuid4().hex[:10],
                    "name": name,
                    "stage": "script_queued",
                    "message": "Chờ tạo kịch bản",
                    "variants": [],
                    "logs": [],
                }
                for name in names
            ],
        }
        self.save()
        self._launch(settings)
        return self.snapshot()

    def retry(self, settings: dict[str, Any]) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("Phiên đang chạy")
        if not self.run:
            raise ValueError("Chưa có phiên nào để chạy lại")
        count = 0
        for product in self.run["products"]:
            if not product["variants"]:
                if product["stage"] != "done":
                    product.update(stage="script_queued", message="Chờ tạo lại kịch bản")
                    count += 1
                continue
            for variant in product["variants"]:
                if variant["stage"] in ("failed", "stopped"):
                    variant.update(stage="queued", message="Chờ chạy lại", error=None)
                    count += 1
        if not count:
            raise ValueError("Không có mục lỗi / bị dừng để chạy lại")
        self.run["status"] = "running"
        self.save()
        self._launch(settings)
        return self.snapshot()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def _launch(self, settings: dict[str, Any]) -> None:
        self.settings = dict(settings)
        self._task = asyncio.create_task(self._run_all(), name="autovideo-run")

    # ---- pipeline
    async def _run_all(self) -> None:
        settings = self.settings
        run = self.run
        accounts = [a for a in load_accounts(Path(settings["muse_accounts_dir"])) if a["usable"]]
        self._queue = asyncio.Queue()
        threads = settings["threads"]
        self.workers = []
        if accounts:
            # Thread i -> account i % N: 3 threads on 2 accounts puts two
            # independent chats (connections) on the first account.
            for i in range(threads):
                account = accounts[i % len(accounts)]
                self.workers.append({
                    "id": i + 1,
                    "account_id": account["id"],
                    "account_label": account["label"],
                    "state_dir": account["state_dir"],
                    "status": "idle",
                    "current": None,
                    "done": 0,
                    "failed": 0,
                })
        try:
            if not accounts:
                reason = "Không có tài khoản Muse sẵn sàng (đăng nhập trong MuseAI-API)"
                for product in run["products"]:
                    if product["stage"] == "script_queued":
                        product.update(stage="failed", message=reason)
                    for variant in product["variants"]:
                        if variant["stage"] == "queued":
                            variant.update(stage="failed", message=reason, error=reason)
                return
            workers = [asyncio.create_task(self._worker(w)) for w in self.workers]
            script_sem = asyncio.Semaphore(settings["script_concurrency"])
            await asyncio.gather(*(
                self._prepare_product(product, script_sem) for product in run["products"]
            ))
            for _ in workers:
                self._queue.put_nowait(None)
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for product in run["products"]:
                if product["stage"] in ("script_queued", "script"):
                    product.update(stage="failed", message="Đã dừng")
                for variant in product["variants"]:
                    if variant["stage"] not in FINAL_VARIANT:
                        variant.update(stage="stopped", message="Đã dừng")
            raise
        finally:
            for worker in self.workers:
                worker.update(status="stopped", current=None)
            for product in run["products"]:
                self._refresh_product(product)
            run["status"] = "finished"
            run["finished_at"] = now()
            self.save()

    def _refresh_product(self, product: dict[str, Any]) -> None:
        variants = product["variants"]
        if not variants:
            return
        stages = [v["stage"] for v in variants]
        done = stages.count("done")
        if all(s in FINAL_VARIANT for s in stages):
            product["stage"] = "done" if done == len(stages) else ("partial" if done else "failed")
            product["message"] = f"Hoàn tất {done}/{len(stages)} video"
        else:
            product["stage"] = "producing"
            product["message"] = f"Đang sản xuất: {done}/{len(stages)} video xong"

    def _product_dir(self, product: dict[str, Any]) -> Path:
        return OUTPUT_DIR / self.run["id"] / safe_name(product["name"])

    async def _prepare_product(self, product: dict[str, Any], sem: asyncio.Semaphore) -> None:
        """Generate scripts (if needed) and enqueue the product's videos."""
        if not product["variants"] and product["stage"] == "script_queued":
            async with sem:
                await self._generate_scripts(product)
        for variant in product["variants"]:
            if variant["stage"] == "queued":
                self._queue.put_nowait((product, variant))
        self._refresh_product(product)
        self.save()

    async def _generate_scripts(self, product: dict[str, Any]) -> None:
        settings = self.settings
        folder = Path(settings["products_dir"]) / product["name"]
        images = list_images(folder)[: settings["vs_max_product_images"]]
        product["stage"] = "script"
        product["started_at"] = now()
        self.log(product, f"Gọi VideoScript với {len(images)} ảnh sản phẩm")

        def tick(seconds: int) -> None:
            product["message"] = f"VideoScript đang viết kịch bản… {seconds}s"

        try:
            result = await generate_scripts(settings, images, on_tick=tick)
            variants = extract_variants(result)
            if not variants:
                raise RuntimeError("VideoScript không trả về kịch bản hợp lệ")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            product["stage"] = "failed"
            product["error"] = str(exc)
            self.log(product, f"Lỗi tạo kịch bản: {exc}")
            return

        out_dir = self._product_dir(product)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "scripts.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        product["scripts_url"] = file_url(out_dir / "scripts.json")
        # Which images the Muse prompt was written for (see _produce_variant).
        product["image_source"] = "product" if settings["skip_images"] else "scenes"
        product["product_name"] = (result.get("data") or {}).get("product_analysis", {}).get("product_name")
        for item in variants:
            vdir = out_dir / f"v{item['index']}"
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / "script.json").write_text(
                json.dumps(item["script"], ensure_ascii=False, indent=1), encoding="utf-8"
            )
            product["variants"].append({
                "id": uuid.uuid4().hex[:10],
                "index": item["index"],
                "title": item["title"],
                "concept": item["concept"],
                "image_target": item["image_target"],
                "image_prompt": item["image_prompt"],
                "video_prompt": item["video_prompt"],
                "stage": "queued",
                "message": "Chờ luồng Muse rảnh",
                "images": [],
                "image_rounds": 0,
                "video": None,
                "session_id": None,
                "worker": None,
                "account_label": None,
                "error": None,
                "logs": [],
            })
        product["stage"] = "scripted"
        self.log(product, f"Đã tạo {len(variants)} kịch bản")

    async def _worker(self, worker: dict[str, Any]) -> None:
        connection = MuseConnection(Path(worker["state_dir"]))
        try:
            while True:
                job = await self._queue.get()
                if job is None:
                    break
                product, variant = job
                worker.update(status="busy", current=f"{product['name']} · KB{variant['index']}")
                try:
                    await self._produce_with_retry(connection, worker, product, variant)
                    worker["done"] += 1
                except asyncio.CancelledError:
                    variant.update(stage="stopped", message="Đã dừng")
                    raise
                except Exception as exc:
                    worker["failed"] += 1
                    variant["stage"] = "failed"
                    variant["error"] = str(exc)
                    variant["finished_at"] = now()
                    self.log(variant, f"Lỗi: {exc}")
                    # The Muse connection may be broken; reconnect for the next job.
                    await connection.reset()
                finally:
                    worker.update(status="idle", current=None)
                    self._refresh_product(product)
                    self.save()
        finally:
            await connection.reset()

    async def _produce_with_retry(
        self,
        connection: MuseConnection,
        worker: dict[str, Any],
        product: dict[str, Any],
        variant: dict[str, Any],
    ) -> None:
        """Muse's gateway sometimes answers 502/503 or drops the websocket; those
        are retried on a fresh connection after a growing pause."""
        retries = self.settings["muse_retries"]
        for attempt in range(retries + 1):
            try:
                await self._produce_variant(connection, worker, product, variant)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= retries or not is_transient(exc):
                    raise
                await connection.reset()
                wait = 30 * (attempt + 1)
                variant["stage"] = "queued"
                self.log(variant, f"Lỗi tạm thời từ Muse ({exc}); thử lại sau {wait}s "
                                  f"(lần {attempt + 1}/{retries})")
                await asyncio.sleep(wait)

    async def _produce_variant(
        self,
        connection: MuseConnection,
        worker: dict[str, Any],
        product: dict[str, Any],
        variant: dict[str, Any],
    ) -> None:
        settings = self.settings
        vdir = self._product_dir(product) / f"v{variant['index']}"
        img_dir = vdir / "images"
        variant.update(
            worker=worker["id"], account_label=worker["account_label"],
            started_at=now(), finished_at=None, error=None,
        )
        target = variant["image_target"]
        # A new Muse chat (thread) for every video; the id is a fresh UUID.
        session_id = str(uuid.uuid4())
        variant["session_id"] = session_id

        self.log(variant, f"Luồng {worker['id']} ({worker['account_label']}) kết nối Muse")
        await connection.get()

        existing = [img_dir / Path(item["url"]).name for item in variant["images"]]
        # The Muse prompt was written for one image source when the scripts were made;
        # keep using that source even if the setting changed since.
        skip_images = product.get("image_source") == "product"
        variant["skip_images"] = skip_images
        send_dir = vdir / "send"
        if skip_images:
            # No-image mode: Muse renders the video straight from the real product photos.
            refs = self._product_refs(product, vdir / "refs")
            images = []
            self.log(variant, f"Bỏ qua bước tạo ảnh, dùng {len(refs)} ảnh sản phẩm gốc làm tham chiếu")
        elif len(existing) >= target and all(p.exists() for p in existing):
            images = existing[:target]
            self.log(variant, f"Dùng lại {len(images)} ảnh đã tạo trước đó")
        else:
            images = await self._produce_images(connection, variant, product, session_id, img_dir)

        # ---- video
        variant["stage"] = "video"
        shutil.rmtree(send_dir, ignore_errors=True)
        # Attachment order matters: scene images 1A, 1B, 2..N are referenced by position
        # in the Muse prompt; in no-image mode only product photos are sent.
        attachments = [
            shrink_copy(path, send_dir / f"{i + 1:02d}.jpg")
            for i, path in enumerate(refs if skip_images else images)
        ]
        prompt = (settings["video_prefix"].replace("{count}", str(len(attachments)))
                  + variant["video_prompt"]
                  + fill_suffix(settings["video_suffix"], len(attachments),
                                settings["vs_video_duration"]))
        finals = await self._produce_video(connection, variant, session_id, attachments, prompt, vdir)
        variant["video"] = file_url(finals[0])
        variant["videos"] = [file_url(v) for v in finals]
        variant["stage"] = "done"
        variant["finished_at"] = now()
        self.log(variant, f"Hoàn tất: {len(finals)} video hoàn chỉnh")

    @staticmethod
    async def _download(connection: MuseConnection, kind: str, ref: Any, path: Path) -> Path:
        """Download through the current connection, reconnecting once if it broke."""
        for attempt in range(2):
            try:
                client = await connection.get()
                fetch = client.download_video if kind == "video" else client.download_image
                return await asyncio.wait_for(fetch(ref, path), 300)
            except asyncio.CancelledError:
                raise
            except Exception:
                await connection.reset()
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    async def _produce_video(
        self,
        connection: MuseConnection,
        variant: dict[str, Any],
        session_id: str,
        attachments: list[Path],
        prompt: str,
        vdir: Path,
    ) -> list[Path]:
        """Muse either renders one video directly, or renders one clip per scene
        and stitches them later (sometimes only after a "tiếp"). Every Muse video
        is downloaded to clips/; only a finished video is copied to video/."""
        settings = self.settings
        target_seconds = settings["vs_video_duration"]
        clips_dir, video_dir = vdir / "clips", vdir / "video"
        shutil.rmtree(clips_dir, ignore_errors=True)
        shutil.rmtree(video_dir, ignore_errors=True)
        clips_dir.mkdir(parents=True, exist_ok=True)
        variant.update(clips=[], video=None, videos=[])
        known: set[str] = set()
        finals: list[Path] = []

        for attempt in range(1, settings["video_attempts"] + 1):
            turn: list[Path] = []
            if attempt == 1:
                message, files = prompt, attachments
                self.log(variant, f"Gửi {len(files)} ảnh + prompt video lên Muse")
            else:
                message = (settings["video_retry_message"]
                           .replace("{duration}", str(target_seconds)))
                files = []
                self.log(variant, f"Chưa có video hoàn chỉnh, nhắn Muse làm tiếp (lần {attempt - 1})")

            async def on_new(ref: Any) -> bool:
                path = clips_dir / video_name(ref)
                if path.suffix.lower() != ".mp4":
                    path = path.with_name(path.name + ".mp4")
                try:
                    await self._download(connection, "video", ref, path)
                except Exception as exc:
                    self.log(variant, f"Không tải được video {path.name}: {exc}")
                    return False
                seconds = mp4_duration(path)
                turn.append(path)
                variant["clips"].append({"url": file_url(path), "seconds": seconds})
                self.log(variant, f"Muse trả video {path.name} ({seconds or '?'}s)")
                # A stitched video reaches the script length; single clips cap at ~10s.
                if seconds and target_seconds > 10 and seconds >= 0.9 * target_seconds:
                    finals.append(path)
                    return True
                return False

            def progress(count: int, text: str) -> None:
                variant["message"] = f"Lượt video {attempt}: Muse đã trả {count} video"
                if text:
                    variant["muse_reply"] = text[-800:]

            _, reply = await send_and_collect_videos(
                connection,
                session_id=session_id,
                prompt=message,
                attachments=files,
                known=known,
                timeout=settings["video_timeout"],
                idle_seconds=settings["video_idle_seconds"],
                on_new=on_new,
                on_progress=progress,
                on_issue=lambda text: self.log(variant, text),
            )
            if reply:
                variant["muse_reply"] = reply[-800:]
            # Exactly one video in a turn, with no scene clips before it, is
            # Muse rendering the whole script in one go.
            if not finals and len(turn) == 1 and len(variant["clips"]) == 1:
                finals.append(turn[0])
            if finals:
                video_dir.mkdir(parents=True, exist_ok=True)
                return [Path(shutil.copy2(path, video_dir / path.name)) for path in finals]
            if turn:
                self.log(variant, f"Lượt video {attempt}: {len(turn)} clip cảnh, chưa có video ghép hoàn chỉnh")
        raise RuntimeError(
            f"Muse chưa trả video hoàn chỉnh sau {settings['video_attempts']} lượt "
            f"({len(variant['clips'])} clip cảnh trong clips/). "
            f"Phản hồi Muse: {(variant.get('muse_reply') or '')[-300:]}"
        )

    async def _produce_images(
        self,
        connection: MuseConnection,
        variant: dict[str, Any],
        product: dict[str, Any],
        session_id: str,
        img_dir: Path,
    ) -> list[Path]:
        settings = self.settings
        target = variant["image_target"]
        shutil.rmtree(img_dir, ignore_errors=True)
        img_dir.mkdir(parents=True, exist_ok=True)
        variant.update(stage="images", images=[], image_rounds=0)

        product_refs = self._product_refs(product, img_dir.parent / "refs")

        # One entry per image slot. Muse labels its images ("Ảnh 1B — ..."), may
        # deliver them out of order, or redo one; the label key keeps one
        # image per slot and the final scene order.
        saved: list[dict[str, Any]] = []
        known: set[str] = set()
        expected = expected_keys(target)
        empty_rounds = 0

        def publish() -> None:
            ordered = self._ordered(saved)
            variant["images"] = [
                {"url": file_url(item["path"]), "label": item["label"], "key": item["key"]}
                for item in ordered
            ]

        for round_no in range(1, settings["max_image_rounds"] + 1):
            variant["image_rounds"] = round_no
            missing = target - len(saved)
            have_keys = {item["key"] for item in saved if item["key"]}
            missing_keys = [k for k in expected if k not in have_keys]
            label_word = "Image" if settings["vs_prompt_language"] == "en" else "Ảnh"
            missing_list = ", ".join(f"{label_word} {k}" for k in missing_keys[:missing]) or str(missing)
            if round_no == 1:
                prompt = settings["image_prefix"] + variant["image_prompt"]
                if settings["image_suffix"].strip():
                    prompt += "\n\n" + settings["image_suffix"].strip()
                attachments = product_refs
                self.log(variant, f"Lượt 1: gửi {len(attachments)} ảnh sản phẩm + prompt {target} ảnh")
            else:
                prompt = (settings["continue_message"]
                          .replace("{have}", str(len(saved)))
                          .replace("{target}", str(target))
                          .replace("{missing}", str(missing))
                          .replace("{missing_list}", missing_list))
                attachments = []
                self.log(variant, f"Lượt {round_no}: yêu cầu làm tiếp {missing} ảnh ({missing_list})")

            def progress(count: int, text: str, base=len(saved)) -> None:
                variant["message"] = f"Lượt {round_no}: Muse đã trả {count} ảnh mới (đã có {base}/{target})"
                if text:
                    variant["muse_reply"] = text[-800:]

            refs, reply = await send_and_collect_images(
                connection,
                session_id=session_id,
                prompt=prompt,
                attachments=attachments,
                known=known,
                need=missing,
                timeout=settings["image_round_timeout"],
                idle_seconds=settings["image_idle_seconds"],
                on_progress=progress,
                on_issue=lambda text: self.log(variant, text),
            )
            if reply:
                variant["muse_reply"] = reply[-800:]
            added = 0
            for ref in refs:
                known.add(ref.identity)
                key = image_key(ref.label)
                if key not in expected:
                    key = None
                slot = next((item for item in saved if key and item["key"] == key), None)
                if slot is None and len(saved) >= target:
                    continue
                suffix = Path(MuseClient._image_filename_for(ref, 0)).suffix or ".png"
                path = img_dir / f"img_{key or 'x'}_{len(known):02d}{suffix}"
                try:
                    downloaded = await self._download(connection, "image", ref, path)
                except Exception as exc:
                    self.log(variant, f"Không tải được ảnh {ref.label or ''}: {exc}")
                    continue
                if slot is not None:
                    # Muse redid this image: keep the newest version.
                    slot.update(path=downloaded, label=ref.label or "")
                else:
                    saved.append({"path": downloaded, "label": ref.label or "", "key": key})
                    added += 1
                publish()
                self.save()
            self.log(variant, f"Lượt {round_no}: nhận {len(refs)} ảnh ({added} ảnh mới), tổng {len(saved)}/{target}")
            if len(saved) >= target:
                return [item["path"] for item in self._ordered(saved)][:target]
            empty_rounds = empty_rounds + 1 if not added else 0
            if empty_rounds >= 2:
                break
        raise RuntimeError(
            f"Chỉ tạo được {len(saved)}/{target} ảnh sau {variant['image_rounds']} lượt. "
            f"Phản hồi Muse: {(variant.get('muse_reply') or '')[-300:]}"
        )

    def _product_refs(self, product: dict[str, Any], refs_dir: Path) -> list[Path]:
        """First N product photos, shrunk, as Muse reference images."""
        folder = Path(self.settings["products_dir"]) / product["name"]
        shutil.rmtree(refs_dir, ignore_errors=True)
        return [
            shrink_copy(path, refs_dir / f"ref_{i + 1:02d}.jpg", max_dim=1600)
            for i, path in enumerate(list_images(folder)[: self.settings["muse_max_input_images"]])
        ]

    @staticmethod
    def _ordered(saved: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Scene order by label when every image has one, else arrival order."""
        if saved and all(item["key"] for item in saved):
            return sorted(saved, key=lambda item: key_order(item["key"]))
        return list(saved)

    # ---- helpers for the UI
    def random_products(self, products_dir: Path, count: int) -> list[str]:
        names = [p["name"] for p in list_products(products_dir) if p["image_count"]]
        return random.sample(names, min(max(1, count), len(names)))
