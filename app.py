import datetime as dt
import json
import uuid
from pathlib import Path

import cv2
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import analysis as A

DATA = Path("data")
IMG_DIR = DATA / "images"
DB = DATA / "entries.json"
IMG_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Eczema Tracker", layout="wide")


def load_entries() -> list[dict]:
    return json.loads(DB.read_text()) if DB.exists() else []


def save_entries(entries: list[dict]):
    entries.sort(key=lambda e: e["date"])
    DB.write_text(json.dumps(entries, indent=2))


def add_entry(file_bytes: bytes, date: dt.date, med: str, crop: int, show_warnings=True):
    img = A.center_crop(A.load_bgr(file_bytes), crop)
    if show_warnings:
        for w in A.quality_warnings(img):
            st.warning(w)

    cv = A.cv_metrics(img)
    try:
        llm = A.llm_score(img)
    except Exception as ex:  # still save CV-only result if the API call fails
        llm = {"error": str(ex)}
        st.warning(f"LLM scoring failed, using computer-vision metrics only: {ex}")

    path = IMG_DIR / f"{uuid.uuid4().hex}.jpg"
    cv2.imwrite(str(path), img)

    entries = load_entries()
    entries.append({
        "date": date.isoformat(), "medication": med, "image": str(path),
        "cv": cv, "llm": llm, "severity": A.severity(cv, llm),
    })
    save_entries(entries)


# ---------------- sidebar: input ----------------
st.sidebar.title("Add a photo")
med = st.sidebar.text_input("Medication / treatment", placeholder="e.g. hydrocortisone 1%")
date = st.sidebar.date_input("Photo date", dt.date.today())
crop = st.sidebar.slider("Center crop % (cut out background)", 40, 100, 80)
use_camera = st.sidebar.toggle("Use camera instead of upload")
photo = st.sidebar.camera_input("Take photo") if use_camera else st.sidebar.file_uploader(
    "Upload photo", type=["jpg", "jpeg", "png"])

if st.sidebar.button("Analyze & save", type="primary", disabled=not (photo and med)):
    with st.spinner("Analyzing..."):
        add_entry(photo.getvalue(), date, med, crop)
    st.rerun()

with st.sidebar.expander("Demo data loader"):
    demo_files = st.file_uploader("Several images, oldest first", type=["jpg", "jpeg", "png"],
                                  accept_multiple_files=True, key="demo")
    demo_med = st.text_input("Medication", key="demo_med")
    demo_start = st.date_input("First photo date", dt.date.today() - dt.timedelta(days=21),
                               key="demo_start")
    demo_gap = st.number_input("Days between photos", 1, 14, 3)
    if st.button("Load demo set", disabled=not (demo_files and demo_med)):
        with st.spinner("Analyzing demo images..."):
            for i, f in enumerate(demo_files):
                add_entry(f.getvalue(), demo_start + dt.timedelta(days=i * demo_gap),
                          demo_med, crop, show_warnings=False)
        st.rerun()

if st.sidebar.button("Reset all data"):
    DB.unlink(missing_ok=True)
    st.rerun()

# ---------------- main view ----------------
st.title("Eczema Tracker")
st.caption("Photo-based trend tracking. Not a medical diagnosis. Talk to a clinician about "
           "treatment decisions.")

entries = load_entries()
if not entries:
    st.info("Add your first photo in the sidebar to get started.")
    st.stop()

df = pd.DataFrame({
    "date": [e["date"] for e in entries],
    "medication": [e["medication"] for e in entries],
    "Overall severity": [e["severity"] for e in entries],
    "CV redness": [e["cv"]["redness_score"] for e in entries],
    "Affected area %": [e["cv"]["area_pct"] for e in entries],
})

label, change = A.verdict(df["Overall severity"].tolist())
colors = {"improving": "green", "worsening": "red"}
c1, c2, c3 = st.columns(3)
c1.metric("Trend", label.title(), f"{change:+.2f} severity", delta_color="inverse")
c2.metric("Entries", len(entries))
c3.metric("Latest medication", entries[-1]["medication"])

# timeline chart with medication-change markers
fig = go.Figure()
for col in ["Overall severity", "CV redness"]:
    fig.add_trace(go.Scatter(x=df["date"], y=df[col], mode="lines+markers", name=col))
for i, row in df.iterrows():
    if i == 0 or row["medication"] != df.loc[i - 1, "medication"]:
        fig.add_shape(type="line", x0=row["date"], x1=row["date"], y0=0, y1=10,
                      line=dict(dash="dash", color="gray"))
        fig.add_annotation(x=row["date"], y=10, text=row["medication"], showarrow=False,
                           yshift=10, textangle=-20)
fig.update_layout(yaxis=dict(title="Severity (0-10, lower is better)", range=[0, 11]),
                  height=400, margin=dict(t=40))
st.plotly_chart(fig, use_container_width=True)

# first vs latest
first, last = entries[0], entries[-1]
a, b = st.columns(2)
a.image(first["image"], caption=f"First: {first['date']} ({first['medication']})")
b.image(last["image"], caption=f"Latest: {last['date']} ({last['medication']})")

# summary
if st.button("Generate plain-language summary"):
    with st.spinner("Writing summary..."):
        try:
            st.write(A.summarize(entries, label, change))
        except Exception as ex:
            st.error(f"Summary failed: {ex}")

with st.expander("Raw data"):
    st.dataframe(df)
    st.json([e["llm"] for e in entries])
