"""Kie.ai client — GPT Image 2 / Nano Banana for admin quick-book generation."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx

KIE_API_KEY = (os.environ.get("KIE_API_KEY") or "").strip()
KIE_API_BASE = (os.environ.get("KIE_API_BASE") or "https://api.kie.ai/api/v1").rstrip("/")
KIE_UPLOAD_URL = (
    os.environ.get("KIE_UPLOAD_URL")
    or "https://kieai.redpandaai.co/api/file-stream-upload"
)
# gpt-image-2 image-to-image — uses the child's photo as reference
KIE_I2I_MODEL = os.environ.get("KIE_I2I_MODEL") or "gpt-image-2-image-to-image"
# Google Nano Banana via Kie (image-to-image)
KIE_NANOBANANA_MODEL = os.environ.get("KIE_NANOBANANA_MODEL") or "google/nano-banana"
# A4-ish portrait pages
KIE_ASPECT_RATIO = os.environ.get("KIE_ASPECT_RATIO") or "3:4"  # closest to A4 among enum
KIE_RESOLUTION = os.environ.get("KIE_RESOLUTION") or "1K"
KIE_POLL_INTERVAL = float(os.environ.get("KIE_POLL_INTERVAL") or "3")
KIE_POLL_TIMEOUT = float(os.environ.get("KIE_POLL_TIMEOUT") or "300")


def kie_configured() -> bool:
    return bool(KIE_API_KEY)


def model_for_provider(provider: Optional[str] = None) -> str:
    """Map UI provider id → Kie model slug."""
    p = (provider or "chatgpt").strip().lower()
    if p in ("nanobanana", "nano", "nano-banana", "nano_banana"):
        return KIE_NANOBANANA_MODEL
    return KIE_I2I_MODEL


def normalize_provider(provider: Optional[str] = None) -> str:
    p = (provider or "chatgpt").strip().lower()
    if p in ("nanobanana", "nano", "nano-banana", "nano_banana"):
        return "nanobanana"
    return "chatgpt"


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {KIE_API_KEY}",
    }


def _json_headers() -> dict:
    h = _auth_headers()
    h["Content-Type"] = "application/json"
    return h


def _raise_kie(payload: dict, context: str) -> None:
    code = payload.get("code")
    if code in (200, None) and payload.get("success", True) is not False:
        # some endpoints use success=true; jobs use code=200
        if code is None or code == 200:
            return
    msg = payload.get("msg") or payload.get("message") or "Unknown Kie.ai error"
    if code == 401:
        raise RuntimeError("مفتاح Kie.ai غير صالح.")
    if code == 402:
        raise RuntimeError("رصيد Kie.ai خلص. عبّي credits من لوحة Kie.")
    if code == 429:
        raise RuntimeError("Kie.ai: معدل الطلبات عالي. استنى لحظات وجرّب تاني.")
    raise RuntimeError(f"Kie.ai ({context}): {msg}")


async def upload_image(
    path: Path,
    client: httpx.AsyncClient,
    *,
    upload_path: str = "coloring-book",
    file_name: Optional[str] = None,
) -> str:
    """Upload a local image and return a public download URL for input_urls."""
    name = file_name or path.name
    with open(path, "rb") as fh:
        files = {"file": (name, fh, "image/png")}
        data = {"uploadPath": upload_path, "fileName": name}
        resp = await client.post(
            KIE_UPLOAD_URL,
            headers=_auth_headers(),
            files=files,
            data=data,
            timeout=120.0,
        )
    resp.raise_for_status()
    payload = resp.json()
    _raise_kie(payload, "upload")
    data_obj = payload.get("data") or {}
    url = data_obj.get("downloadUrl") or data_obj.get("fileUrl") or data_obj.get("url")
    if not url:
        raise RuntimeError("Kie.ai: فشل رفع الصورة (مفيش رابط).")
    return str(url)


def _build_i2i_input(
    model: str,
    prompt: str,
    input_urls: list[str],
    *,
    aspect_ratio: str,
    resolution: str,
) -> dict[str, Any]:
    """Schema varies slightly between gpt-image-2 and google/nano-banana."""
    m = (model or "").lower()
    if "nano-banana" in m or m.startswith("google/"):
        # Nano Banana family — common Kie schema
        return {
            "prompt": prompt,
            "image_urls": input_urls,
            "output_format": "jpeg",
            "image_size": aspect_ratio,
        }
    return {
        "prompt": prompt,
        "input_urls": input_urls,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }


async def create_i2i_task(
    prompt: str,
    input_urls: list[str],
    client: httpx.AsyncClient,
    *,
    model: Optional[str] = None,
    aspect_ratio: str = KIE_ASPECT_RATIO,
    resolution: str = KIE_RESOLUTION,
) -> str:
    model_id = model or KIE_I2I_MODEL
    body: dict[str, Any] = {
        "model": model_id,
        "input": _build_i2i_input(
            model_id, prompt, input_urls,
            aspect_ratio=aspect_ratio, resolution=resolution,
        ),
    }
    resp = await client.post(
        f"{KIE_API_BASE}/jobs/createTask",
        headers=_json_headers(),
        json=body,
        timeout=60.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    _raise_kie(payload, "createTask")
    task_id = (payload.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError("Kie.ai: مفيش taskId.")
    return str(task_id)


async def poll_task(
    task_id: str,
    client: httpx.AsyncClient,
    *,
    interval: float = KIE_POLL_INTERVAL,
    timeout: float = KIE_POLL_TIMEOUT,
) -> dict:
    """Poll until success/fail. Returns task data dict."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = await client.get(
            f"{KIE_API_BASE}/jobs/recordInfo",
            headers=_auth_headers(),
            params={"taskId": task_id},
            timeout=60.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        _raise_kie(payload, "recordInfo")
        last = payload.get("data") or {}
        state = (last.get("state") or "").lower()
        if state == "success":
            return last
        if state == "fail":
            fail = last.get("failMsg") or last.get("failCode") or "generation failed"
            raise RuntimeError(f"Kie.ai فشل التوليد: {fail}")
        await asyncio.sleep(interval)
    raise TimeoutError("Kie.ai: انتهى وقت انتظار توليد الصورة.")


def extract_result_url(task_data: dict) -> str:
    raw = task_data.get("resultJson")
    if not raw:
        raise RuntimeError("Kie.ai: مفيش نتيجة صورة.")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("Kie.ai: نتيجة الصورة غير صالحة.") from e
    elif isinstance(raw, dict):
        parsed = raw
    else:
        raise RuntimeError("Kie.ai: نتيجة الصورة غير صالحة.")

    urls = parsed.get("resultUrls") or parsed.get("result_urls") or []
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        # common alternate keys
        for key in ("url", "imageUrl", "image_url"):
            if parsed.get(key):
                return str(parsed[key])
        raise RuntimeError("Kie.ai: مفيش رابط صورة في النتيجة.")
    return str(urls[0])


async def download_image_bytes(url: str, client: httpx.AsyncClient) -> bytes:
    resp = await client.get(url, timeout=120.0, follow_redirects=True)
    resp.raise_for_status()
    if not resp.content:
        raise RuntimeError("Kie.ai: الصورة الفاضية.")
    return resp.content


async def generate_image_to_image(
    prompt: str,
    image_path: Path,
    client: httpx.AsyncClient,
    *,
    input_url: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[bytes, str]:
    """
    Full pipeline: upload (if needed) → create task → poll → download bytes.
    Returns (image_bytes, input_url_used).
    """
    if not kie_configured():
        raise RuntimeError("KIE_API_KEY مش مضبوط.")

    url = input_url
    if not url:
        url = await upload_image(image_path, client)

    task_id = await create_i2i_task(prompt, [url], client, model=model)
    task_data = await poll_task(task_id, client)
    result_url = extract_result_url(task_data)
    img_bytes = await download_image_bytes(result_url, client)
    return img_bytes, url
