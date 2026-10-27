import re
import time
from flask import Flask, render_template, request, jsonify
from youtube_transcript_api import YouTubeTranscriptApi

app = Flask(__name__)
# Reload edited templates on refresh without restarting (debug mode is off)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Caption tracks to show, top line first.
#   lang  - the language you want to display
#   codes - YouTube language codes that count as a match for it
# If a video has no real track in that language, the server falls back to
# YouTube's machine translation of another track (marked "translated").
TRACK_CONFIG = [
    {"lang": "pt", "codes": ["pt", "pt-BR", "pt-PT"]},
    {"lang": "en", "codes": ["en", "en-US", "en-GB"]},
]

# Complete results are cached for good. Results with a missing track are cached
# briefly, so the controller and display pages don't both hammer YouTube
# (translation requests in particular get rate-limited).
PARTIAL_CACHE_SECONDS = 120
caption_cache = {}   # video_id -> (expires_at or None, result)

# Shared playback state: the controller page writes it, the display page reads it
playback = {"video_id": None, "t": 0.0, "playing": False}


def extract_video_id(url_or_id):
    """Extract a YouTube video ID from common URL patterns or return a raw ID."""
    patterns = [
        r'[?&]v=([0-9A-Za-z_-]{11})',
        r'youtu\.be/([0-9A-Za-z_-]{11})',
        r'(?:embed|shorts|live)/([0-9A-Za-z_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    if re.fullmatch(r'[0-9A-Za-z_-]{11}', url_or_id):
        return url_or_id
    return None


def format_timestamp(seconds):
    """Convert seconds into HH:MM:SS or MM:SS."""
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def short_error(e):
    """One-line description of an exception: its class name plus a short message."""
    text = " ".join(str(e).split())
    return f"{type(e).__name__}: {text[:160]}" if text else type(e).__name__


def segments_from(transcript):
    """Download a Transcript handle and convert it to our segment format."""
    raw = transcript.fetch().to_raw_data()  # list of {"text", "start", "duration"}
    return [
        {
            "time": format_timestamp(item["start"]),
            "seconds": item["start"],
            "duration": item["duration"],
            "text": item["text"],
        }
        for item in raw
    ]


def find_best_track(transcript_list, codes):
    """Prefer a human-made track in ANY of the codes, then an auto-generated one.
    (find_transcript() alone checks code by code, so an auto-generated 'pt'
    would beat a human-made 'pt-BR'.)"""
    try:
        return transcript_list.find_manually_created_transcript(codes)
    except Exception:
        return transcript_list.find_generated_transcript(codes)


def get_track(transcript_list, cfg):
    """Return (track, None) on success or (None, reason) if it can't be obtained."""
    reasons = []

    # 1. A real track in the wanted language (manual preferred over auto-generated)
    try:
        tr = find_best_track(transcript_list, cfg["codes"])
    except Exception as e:
        tr = None
        reasons.append(f"no {cfg['lang']} track ({type(e).__name__})")
    if tr is not None:
        try:
            return {
                "lang": cfg["lang"],
                "source_lang": tr.language_code,
                "generated": tr.is_generated,
                "translated": False,
                "segments": segments_from(tr),
            }, None
        except Exception as e:
            reasons.append(f"downloading {tr.language_code} track: {short_error(e)}")

    # 2. Otherwise, ask YouTube to machine-translate another track (max 2 tries,
    #    since each attempt is another request to YouTube)
    tried = 0
    for tr in transcript_list:
        if not tr.is_translatable:
            continue
        if tried >= 2:
            break
        tried += 1
        try:
            translated = tr.translate(cfg["lang"])
            return {
                "lang": cfg["lang"],
                "source_lang": tr.language_code,
                "generated": True,
                "translated": True,
                "segments": segments_from(translated),
            }, None
        except Exception as e:
            reasons.append(f"translating {tr.language_code}: {short_error(e)}")
            # If YouTube is refusing our requests, more attempts only make it worse
            if type(e).__name__ in ("IpBlocked", "RequestBlocked"):
                break
    if tried == 0:
        reasons.append("no translatable track")

    return None, "; ".join(reasons)


def cache_get(video_id):
    entry = caption_cache.get(video_id)
    if not entry:
        return None
    expires, result = entry
    if expires is not None and time.time() > expires:
        del caption_cache[video_id]
        return None
    return result


# ---------- Pages ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/control")
def control():
    return render_template("control.html")


@app.route("/display")
def display():
    return render_template("display.html")


# ---------- API ----------

@app.route("/api/captions", methods=["POST"])
def get_captions():
    data = request.get_json() or {}
    raw_input = data.get("url", "").strip()

    if not raw_input:
        return jsonify({"error": "Please provide a YouTube video URL or ID."}), 400

    video_id = extract_video_id(raw_input)
    if not video_id:
        return jsonify({"error": "Invalid YouTube URL or Video ID format."}), 400

    cached = cache_get(video_id)
    if cached:
        return jsonify(cached)

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch captions: {short_error(e)}"}), 500

    tracks, missing, errors = [], [], {}
    for cfg in TRACK_CONFIG:
        track, reason = get_track(transcript_list, cfg)
        if track:
            tracks.append(track)
        else:
            missing.append(cfg["lang"])
            errors[cfg["lang"]] = reason

    if not tracks:
        detail = "; ".join(f"{lang} ({errors[lang]})" for lang in missing)
        return jsonify({"error": "No captions available: " + detail}), 404

    first = tracks[0]["segments"]
    result = {
        "video_id": video_id,
        "tracks": tracks,
        "missing": missing,
        "errors": errors,
        # kept for index.html and control.html, which read the first track only
        "segments": first,
        "full_text": " ".join(seg["text"] for seg in first),
    }

    expires = time.time() + PARTIAL_CACHE_SECONDS if missing else None
    caption_cache[video_id] = (expires, result)
    return jsonify(result)


@app.route("/api/debug/<video_id>")
def debug_tracks(video_id):
    """Lists every caption track the library can see, and what happens when we
    try to get each configured language. Open it in a browser to diagnose a video."""
    if not re.fullmatch(r'[0-9A-Za-z_-]{11}', video_id):
        return jsonify({"error": "Not a valid 11-character video ID."}), 400

    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    except Exception as e:
        return jsonify({"error": short_error(e)}), 500

    wanted = [cfg["lang"] for cfg in TRACK_CONFIG]
    available = []
    for tr in transcript_list:
        targets = {t.language_code for t in tr.translation_languages}
        available.append({
            "language_code": tr.language_code,
            "language": tr.language,
            "generated": tr.is_generated,
            "translatable": tr.is_translatable,
            "can_translate_to": [lang for lang in wanted if lang in targets],
        })

    attempts = {}
    for cfg in TRACK_CONFIG:
        track, reason = get_track(transcript_list, cfg)
        if track:
            attempts[cfg["lang"]] = {
                "ok": True,
                "source_lang": track["source_lang"],
                "translated": track["translated"],
                "generated": track["generated"],
                "lines": len(track["segments"]),
            }
        else:
            attempts[cfg["lang"]] = {"ok": False, "reason": reason}

    return jsonify({"video_id": video_id, "available": available, "attempts": attempts})


@app.route("/api/state", methods=["GET", "POST"])
def playback_state():
    if request.method == "POST":
        d = request.get_json() or {}
        playback.update(
            video_id=d.get("video_id"),
            t=float(d.get("t", 0)),
            playing=bool(d.get("playing")),
        )
        return jsonify(ok=True)
    return jsonify(playback)


if __name__ == "__main__":
    # 0.0.0.0 lets other devices on your network connect.
    # debug is off because debug mode exposed to the network is unsafe.
    app.run(host="0.0.0.0", port=5000, debug=False)