import streamlit as st
import yt_dlp
import subprocess
import os
import re
import json
import zipfile
import io
from pathlib import Path
import google.generativeai as genai
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled

DOWNLOAD_DIR = "downloads"
CLIPS_DIR = "clips"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True)
        return True
    except FileNotFoundError:
        return False


def time_to_seconds(t: str):
    t = t.strip()
    parts = t.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(t)
    except (ValueError, IndexError):
        return None


def get_youtube_id(url: str):
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([^&\n?#]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def get_transcript(video_id: str):
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join([item['text'] for item in transcript])
    except TranscriptsDisabled:
        return None
    except Exception as e:
        st.error(f"Transcript error: {str(e)}")
        return None


def get_available_formats(url: str):
    try:
        ydl_opts = {"quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            quality_map = {}
            for fmt in formats:
                if fmt.get("vcodec") != "none" and fmt.get("acodec") != "none":
                    height = fmt.get("height", 0)
                    if height:
                        quality_map[f"{height}p"] = fmt.get("format_id")
            return quality_map
    except Exception as e:
        st.error(f"Format fetch error: {str(e)}")
        return {}


def download_video(url: str, quality_id: str = None):
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).60s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }

    if quality_id:
        ydl_opts["format"] = quality_id
    else:
        ydl_opts["format"] = "best[ext=mp4]/best[height<=720]/best"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        base = os.path.splitext(filename)[0]
        path = base + ".mp4"
        return path, info.get("title", "video"), info.get("duration", 0)


def detect_viral_moments(transcript: str, video_duration: int, gemini_key: str):
    if not gemini_key:
        st.error("Gemini API key required for auto-detection")
        return []

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = f"""Analyze this transcript and identify viral/engaging moments that would work well as short clips (15-60 seconds each).

Look for:
- Shocking or surprising moments
- Emotional peaks (excitement, sadness, anger, joy)
- Unexpected twists
- Strong hooks or openers
- Cliffhangers
- Funny/comedy moments
- Inspirational moments
- Key takeaways
- Controversial or debated points

Video duration: {int(video_duration)} seconds

TRANSCRIPT:
{transcript}

Return ONLY a JSON array like this (no other text):
[
  {{"start": "0:05", "end": "0:35", "reason": "Strong hook - grabs attention immediately"}},
  {{"start": "1:20", "end": "2:15", "reason": "Shocking revelation"}}
]

Return 5-8 of the best moments. Times must be in MM:SS format."""

    try:
        response = model.generate_content(prompt)
        json_str = response.text.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("```")[1].replace("json", "").strip()
        moments = json.loads(json_str)
        return moments
    except Exception as e:
        st.error(f"AI Analysis error: {str(e)}")
        return []


def crop_to_ratio(input_file: str, output_file: str, ratio: str):
    if ratio == "16:9":
        filter_str = "scale=1920:1080"
    elif ratio == "9:16":
        filter_str = "scale=1080:1920"
    elif ratio == "1:1":
        filter_str = "scale=1080:1080"
    else:
        filter_str = None

    cmd = [
        "ffmpeg", "-y", "-i", input_file,
        "-vf", filter_str if filter_str else "copy",
        "-c:a", "aac",
        output_file,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def cut_clip(input_file: str, start_str: str, end_str: str, output_file: str):
    start = time_to_seconds(start_str)
    end = time_to_seconds(end_str)

    if start is None or end is None or end <= start:
        return False, "Invalid time format"

    duration = end - start

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_file,
        "-t", str(duration),
        "-c", "copy",
        output_file,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, None if result.returncode == 0 else result.stderr[-200:]


def sanitize(name: str) -> str:
    return re.sub(r"[^\w\s-]", "", name).strip().replace(" ", "_") or "clip"


def create_zip_download(clips: list):
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for name, path in clips:
            if os.path.exists(path):
                zip_file.write(path, arcname=os.path.basename(path))
    zip_buffer.seek(0)
    return zip_buffer


# ── PAGE CONFIG ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AI Video Clipper - AI Soft Tech Solution",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Custom CSS for branding ────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stSidebarNav"] {background-color: #0066CC;}
    .stTitle {color: #0066CC;}
    h1, h2, h3 {color: #0066CC;}
</style>
""", unsafe_allow_html=True)

# ── HEADER with LOGO ───────────────────────────────────────────────────────────
col1, col2, col3 = st.columns([1, 3, 1])
with col2:
    if os.path.exists("logo.png"):
        st.image("logo.png", width=300, use_column_width=False)
    st.markdown("<h1 style='text-align: center; color: #0066CC;'>AI Video Clipper</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #666; font-size: 16px;'><b>by Imtiaz Ahmad</b></p>", unsafe_allow_html=True)
    st.markdown(f"<p style='text-align: center; color: #0066CC;'><a href='https://aisofttechsolution.com' target='_blank' style='text-decoration: none; color: #0066CC;'>🌐 aisofttechsolution.com</a></p>", unsafe_allow_html=True)

st.divider()

# ── SIDEBAR SETTINGS ───────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ Settings")
    st.markdown("**AI Soft Tech Solution**")

    gemini_key = st.text_input(
        "Google Gemini API Key",
        type="password",
        help="Get from: https://aistudio.google.com/app/apikey"
    )

    if gemini_key:
        st.success("✅ Gemini API configured")
    else:
        st.warning("⚠️ Add API key for auto-detection")

st.markdown("### 📊 Paste YouTube link → Auto-detect viral moments → Download clips")

if not check_ffmpeg():
    st.error("❌ **FFmpeg not found!** Install from: https://ffmpeg.org/download.html")
    st.stop()

st.markdown("---")
st.markdown("<p style='text-align: center; font-size: 12px; color: #999;'>AI Video Clipper by Imtiaz Ahmad | <a href='https://aisofttechsolution.com' target='_blank' style='color: #0066CC; text-decoration: none;'>aisofttechsolution.com</a></p>", unsafe_allow_html=True)

# ── SESSION STATE ──────────────────────────────────────────────────────────────
defaults = {
    "video_path": None,
    "video_title": None,
    "video_duration": 0,
    "video_url": None,
    "available_formats": {},
    "num_clips": 0,
    "auto_clips": [],
    "manual_clips": [],
    "generated_clips": [],
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── STEP 1: DOWNLOAD ───────────────────────────────────────────────────────────
st.markdown("### ① Download Video")

col1, col2, col3 = st.columns([3, 1, 1])

with col1:
    url_input = st.text_input(
        "YouTube URL",
        placeholder="https://www.youtube.com/watch?v=...",
        label_visibility="collapsed",
    )

with col2:
    fetch_quality_btn = st.button("📊 Get Formats", use_container_width=True)

with col3:
    download_btn = st.button("⬇️ Download", type="primary", use_container_width=True)

if fetch_quality_btn and url_input.strip():
    with st.spinner("Fetching available qualities..."):
        st.session_state.available_formats = get_available_formats(url_input.strip())
        st.session_state.video_url = url_input.strip()
        st.rerun()

if st.session_state.available_formats:
    selected_quality = st.selectbox(
        "Select Quality",
        options=list(st.session_state.available_formats.keys()),
        index=0
    )
else:
    selected_quality = None

if download_btn:
    if not url_input.strip():
        st.warning("Enter YouTube URL")
    else:
        with st.spinner("Downloading video..."):
            try:
                quality_id = st.session_state.available_formats.get(selected_quality) if selected_quality else None
                path, title, duration = download_video(url_input.strip(), quality_id)
                st.session_state.video_path = path
                st.session_state.video_title = title
                st.session_state.video_duration = duration
                st.session_state.auto_clips = []
                st.session_state.manual_clips = []
                st.session_state.generated_clips = []
                st.success(f"✅ Downloaded: **{title}** ({int(duration)}s)")
            except Exception as e:
                st.error(f"Download failed: {str(e)}")

# ── STEP 2: VIDEO PREVIEW ──────────────────────────────────────────────────────
if st.session_state.video_path and os.path.exists(st.session_state.video_path):
    st.divider()
    st.markdown(f"### ② Preview")
    st.markdown(f"**{st.session_state.video_title}** • {int(st.session_state.video_duration)}s")

    st.video(st.session_state.video_path)

    # ── STEP 3: AUTO-DETECT OR MANUAL ──────────────────────────────────────────
    st.divider()
    st.markdown("### ③ Create Clips")

    tab1, tab2 = st.tabs(["🤖 Auto-Detect", "✂️ Manual"])

    with tab1:
        st.caption("AI analyzes transcript and detects viral moments automatically")

        col1, col2 = st.columns(2)
        with col1:
            aspect_ratio = st.selectbox(
                "Aspect Ratio",
                ["Keep Original", "9:16 (TikTok)", "16:9 (YouTube)", "1:1 (Instagram)"],
                key="auto_ratio"
            )

        with col2:
            auto_detect_btn = st.button("🤖 Detect Viral Moments", type="primary", use_container_width=True)

        if auto_detect_btn:
            if not gemini_key:
                st.error("Configure Gemini API key in Settings first")
            else:
                with st.spinner("Fetching transcript..."):
                    video_id = get_youtube_id(st.session_state.video_url or url_input)
                    transcript = get_transcript(video_id) if video_id else None

                if transcript:
                    with st.spinner("🤖 Analyzing with AI..."):
                        moments = detect_viral_moments(transcript, st.session_state.video_duration, gemini_key)
                        st.session_state.auto_clips = moments if moments else []
                        st.rerun()
                else:
                    st.warning("Could not fetch transcript. Use manual mode.")

        if st.session_state.auto_clips:
            st.success(f"Found {len(st.session_state.auto_clips)} viral moments!")

            for i, moment in enumerate(st.session_state.auto_clips):
                with st.expander(f"Clip {i+1}: {moment.get('reason', 'Viral moment')}"):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.caption(f"{moment['start']} → {moment['end']}")
                    with col2:
                        st.text_input(f"Name", value=f"clip_{i+1}", key=f"auto_name_{i}")

            if st.button("✂️ Cut All Detected Clips", type="primary", use_container_width=True):
                st.session_state.generated_clips = []
                ratio_map = {
                    "Keep Original": None,
                    "9:16 (TikTok)": "9:16",
                    "16:9 (YouTube)": "16:9",
                    "1:1 (Instagram)": "1:1",
                }

                for i, moment in enumerate(st.session_state.auto_clips):
                    clip_name = st.session_state.get(f"auto_name_{i}", f"clip_{i+1}")
                    safe_name = sanitize(clip_name)
                    output_path = os.path.join(CLIPS_DIR, f"{safe_name}.mp4")

                    with st.spinner(f"Cutting: {clip_name}"):
                        ok, err = cut_clip(
                            st.session_state.video_path,
                            moment['start'],
                            moment['end'],
                            output_path
                        )

                        if ok and ratio_map[aspect_ratio]:
                            final_path = os.path.join(CLIPS_DIR, f"{safe_name}_final.mp4")
                            crop_to_ratio(output_path, final_path, ratio_map[aspect_ratio])
                            st.session_state.generated_clips.append((clip_name, final_path))
                        elif ok:
                            st.session_state.generated_clips.append((clip_name, output_path))

                st.success(f"✅ Created {len(st.session_state.generated_clips)} clips!")
                st.rerun()

    with tab2:
        st.caption("Manually define clip timestamps")

        col1, col2 = st.columns(2)
        with col1:
            num_clips = st.number_input("Number of clips", min_value=1, value=st.session_state.num_clips or 1)
            st.session_state.num_clips = num_clips

        with col2:
            aspect_ratio = st.selectbox(
                "Aspect Ratio",
                ["Keep Original", "9:16 (TikTok)", "16:9 (YouTube)", "1:1 (Instagram)"],
                key="manual_ratio"
            )

        for i in range(num_clips):
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1:
                st.text_input("Clip name", value=f"clip_{i+1}", key=f"m_name_{i}")
            with c2:
                st.text_input("Start (MM:SS)", placeholder="0:30", key=f"m_start_{i}")
            with c3:
                st.text_input("End (MM:SS)", placeholder="1:45", key=f"m_end_{i}")

        if st.button("✂️ Cut Manual Clips", type="primary", use_container_width=True):
            st.session_state.generated_clips = []
            ratio_map = {
                "Keep Original": None,
                "9:16 (TikTok)": "9:16",
                "16:9 (YouTube)": "16:9",
                "1:1 (Instagram)": "1:1",
            }

            for i in range(num_clips):
                name = st.session_state.get(f"m_name_{i}", f"clip_{i+1}")
                start = st.session_state.get(f"m_start_{i}", "")
                end = st.session_state.get(f"m_end_{i}", "")

                if not start or not end:
                    st.warning(f"Skip {name}: missing times")
                    continue

                safe_name = sanitize(name)
                output_path = os.path.join(CLIPS_DIR, f"{safe_name}.mp4")

                with st.spinner(f"Cutting: {name}"):
                    ok, err = cut_clip(st.session_state.video_path, start, end, output_path)

                    if ok and ratio_map[aspect_ratio]:
                        final_path = os.path.join(CLIPS_DIR, f"{safe_name}_final.mp4")
                        crop_to_ratio(output_path, final_path, ratio_map[aspect_ratio])
                        st.session_state.generated_clips.append((name, final_path))
                    elif ok:
                        st.session_state.generated_clips.append((name, output_path))
                    else:
                        st.error(f"{name}: {err}")

            st.success(f"✅ Created {len(st.session_state.generated_clips)} clips!")
            st.rerun()

    # ── STEP 4: DOWNLOAD CLIPS ────────────────────────────────────────────────
    if st.session_state.generated_clips:
        st.divider()
        st.markdown("### ④ Download Clips")

        col1, col2, col3 = st.columns(3)

        with col1:
            for name, path in st.session_state.generated_clips:
                if os.path.exists(path):
                    size_mb = os.path.getsize(path) / (1024 * 1024)
                    st.markdown(f"🎞️ **{name}** • {size_mb:.1f}MB")

        with col2:
            if st.button("⬇️ Download All (ZIP)", type="primary", use_container_width=True):
                zip_data = create_zip_download(st.session_state.generated_clips)
                st.download_button(
                    label="📦 Click to Download ZIP",
                    data=zip_data,
                    file_name="clips.zip",
                    mime="application/zip",
                )

        with col3:
            for i, (name, path) in enumerate(st.session_state.generated_clips):
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        st.download_button(
                            label=f"⬇️ {name}",
                            data=f,
                            file_name=os.path.basename(path),
                            mime="video/mp4",
                            key=f"dl_{i}",
                        )
