import io
import json
import re
from statistics import mean

import cv2
import numpy as np
from google import genai
from google.genai import types
from PIL import Image, ImageOps

MODEL = "gemini-2.5-flash"  # free tier via Google AI Studio; change if needed
_client = None


def client():
    global _client
    if _client is None:
        _client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY) from env
    return _client


# ---------- image loading / prep ----------

def load_bgr(file_bytes: bytes) -> np.ndarray:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes))).convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def center_crop(img: np.ndarray, pct: int = 100) -> np.ndarray:
    if pct >= 100:
        return img
    h, w = img.shape[:2]
    ch, cw = int(h * pct / 100), int(w * pct / 100)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    return img[y0:y0 + ch, x0:x0 + cw]


def to_jpeg_bytes(img: np.ndarray, max_side: int = 1024) -> bytes:
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes()


# ---------- photo quality check ----------

def quality_warnings(img: np.ndarray) -> list[str]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    warnings = []
    if cv2.Laplacian(gray, cv2.CV_64F).var() < 60:
        warnings.append("Photo looks blurry. Retake for a reliable score.")
    brightness = gray.mean()
    if brightness < 70:
        warnings.append("Photo is too dark.")
    elif brightness > 200:
        warnings.append("Photo is overexposed.")
    return warnings


# ---------- classical CV: redness + area ----------

def cv_metrics(img: np.ndarray) -> dict:
    """Redness = how much redder the affected region is than the surrounding skin,
    measured on the a* channel of Lab (higher a* = redder). Area = fraction of the
    frame that is segmented as affected."""
    h, w = img.shape[:2]
    small = cv2.resize(img, (640, max(1, int(640 * h / w))))
    a = cv2.cvtColor(small, cv2.COLOR_BGR2LAB)[:, :, 1]
    a_blur = cv2.GaussianBlur(a, (9, 9), 0)

    # Otsu splits "redder" pixels from "less red" pixels
    _, mask = cv2.threshold(a_blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)).astype(bool)

    empty = {"redness_delta": 0.0, "area_pct": 0.0, "redness_score": 0.0}
    if mask.sum() < 50 or (~mask).sum() < 50:
        return empty

    delta = float(a[mask].mean() - a[~mask].mean())
    if delta < 4:  # no meaningful contrast, so no clear lesion
        return empty

    return {
        "redness_delta": round(delta, 2),
        "area_pct": round(float(mask.mean() * 100), 2),
        "redness_score": round(float(np.clip(delta / 25 * 10, 0, 10)), 2),  # 0-10
    }


# ---------- vision LLM scoring ----------

RUBRIC = """You are helping track eczema (atopic dermatitis) over time from photos.
Score ONLY what is visible in the image on a 0-10 scale (0 = none, 3 = mild, 6 = moderate, 10 = severe):
- redness: erythema. On darker skin tones this may look violet, brown or dark gray rather than red, so score the inflammation, not the literal color.
- scaling: dryness, flaking, roughness, thickened skin
- excoriation: scratch marks, broken skin

Also report:
- oozing_or_crusting: true if there is weeping, yellow crusting or pus-like material
- note: one short, neutral sentence describing what you see

If the image does not show skin or is unusable, return {"error": "<reason>"}.
Return ONLY valid JSON, no markdown, in this shape:
{"redness": 0, "scaling": 0, "excoriation": 0, "oozing_or_crusting": false, "note": ""}"""


def _parse_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def llm_score(img: np.ndarray) -> dict:
    resp = client().models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=to_jpeg_bytes(img), mime_type="image/jpeg"),
            RUBRIC,
        ],
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
    )
    try:
        return _parse_json(resp.text)
    except Exception:
        return {"error": "Could not parse model output"}


# ---------- combine, verdict, summary ----------

def severity(cv: dict, llm: dict | None) -> float:
    """Single 0-10 severity number: average of CV redness and LLM sign scores."""
    parts = [cv["redness_score"]]
    if llm and "error" not in llm:
        parts.append(mean([llm["redness"], llm["scaling"], llm["excoriation"]]))
    return round(mean(parts), 2)


def verdict(sev: list[float], threshold: float = 0.7) -> tuple[str, float]:
    """Compare the most recent readings to the ones before them.
    The threshold keeps normal day-to-day noise from flipping the label."""
    n = len(sev)
    if n < 2:
        return "not enough data", 0.0
    if n < 4:
        recent, base = sev[-1], mean(sev[:-1])
    else:
        recent, base = mean(sev[-2:]), mean(sev[max(0, n - 5): n - 2])
    change = recent - base
    if change <= -threshold:
        return "improving", change
    if change >= threshold:
        return "worsening", change
    return "no clear change", change


def summarize(entries: list[dict], label: str, change: float) -> str:
    rows = [{
        "date": e["date"],
        "medication": e["medication"],
        "severity_0_10": e["severity"],
        "cv_redness_0_10": e["cv"]["redness_score"],
        "affected_area_pct": e["cv"]["area_pct"],
        "llm": e.get("llm"),
    } for e in entries]
    prompt = f"""Here is a patient's eczema photo-tracking log (higher severity = worse):
{json.dumps(rows, indent=1)}

Automated trend: {label} (severity change {change:+.2f} on a 0-10 scale).

Write a 3-4 sentence plain-language summary for the patient.
Rules: describe the trend "while using <medication>" and never claim the medication caused it
(eczema fluctuates naturally). Mention when the medication changed if it did. Do not diagnose.
If things are worsening or any entry notes oozing/crusting, suggest seeing a doctor."""
    resp = client().models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3),
    )
    return resp.text.strip()
