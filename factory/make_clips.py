"""Fabrica Sidekick: baja los clips mas vistos de cada streamer, los pasa a vertical
(camara arriba, juego abajo), les quema subtitulos en ingles y espanol y publica
una galeria web para aprobarlos."""
import datetime
import html
import json
import os
import pathlib
import shutil
import subprocess
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).parent
SITE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "site")
CFG = json.loads((ROOT / "streamers.json").read_text())
PER = int(os.environ.get("CLIPS_PER_STREAMER", "4") or 4)
RANGE = os.environ.get("RANGE", "24hr") or "24hr"
KEEP_DAYS = 7
TODAY = datetime.datetime.utcnow().strftime("%Y-%m-%d")
DAY = SITE / "clips" / TODAY
WORK = pathlib.Path("work")
W, H, HALF = 1080, 1920, 960
FONT = "DejaVu Sans"


def run(cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True)


def ytdlp_json(args):
    r = subprocess.run(["yt-dlp", *args], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-2000:])
        return None
    return json.loads(r.stdout)


# ---------- 1. buscar clips ----------
def top_clips(login):
    data = ytdlp_json(["--flat-playlist", "-J",
                       f"https://www.twitch.tv/{login}/clips?filter=clips&range={RANGE}"])
    return (data or {}).get("entries", [])[: PER * 2]


# ---------- 2. encontrar la camara del streamer ----------
def find_cam(path):
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    vw, vh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    found = []
    if casc.empty():
        print("sin detector de caras, uso formato completo")
        return None, vw, vh
    for i in range(10):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (i + 0.5) / 10))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = casc.detectMultiScale(gray, 1.1, 6, minSize=(int(vh * 0.05), int(vh * 0.05)))
        if len(faces):
            x, y, w, h = max(faces, key=lambda b: b[2] * b[3])
            found.append((x + w / 2, y + h / 2, w))
    cap.release()
    if len(found) < 4:
        return None, vw, vh
    cx, cy, fw = np.median(np.array(found), axis=0)
    if fw > vw * 0.22:  # cara enorme = camara a pantalla completa, no hay overlay
        return None, vw, vh
    cw = min(vw, fw * 4.5)
    ch = cw * HALF / W
    if ch > vh:
        ch = vh
        cw = ch * W / HALF
    x = min(max(cx - cw / 2, 0), vw - cw)
    y = min(max(cy - ch / 2, 0), vh - ch)
    ev = lambda v: int(v) // 2 * 2
    return (ev(x), ev(y), ev(cw), ev(ch)), vw, vh


# ---------- 3. transcribir y traducir ----------
_model = None


def transcribe(path):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    segs, info = _model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    segs = [s for s in segs]
    return segs, info.language


def translate(texts, src, dst):
    if src == dst:
        return texts
    try:
        from deep_translator import GoogleTranslator
        tr = GoogleTranslator(source=src, target=dst)
        return [tr.translate(t) if t.strip() else t for t in texts]
    except Exception as e:  # sin traduccion, mejor sin subtitulos que roto
        print("traduccion fallo:", e)
        return None


def chunks_from_words(segs, size=3):
    out = []
    for s in segs:
        words = list(s.words or [])
        for i in range(0, len(words), size):
            g = words[i:i + size]
            out.append((g[0].start, g[-1].end, "".join(w.word for w in g).strip()))
    return out


def chunks_spread(segs, texts, size=3):
    out = []
    for s, t in zip(segs, texts):
        words = (t or "").split()
        if not words:
            continue
        groups = [words[i:i + size] for i in range(0, len(words), size)]
        step = (s.end - s.start) / len(groups)
        for k, g in enumerate(groups):
            out.append((s.start + k * step, s.start + (k + 1) * step, " ".join(g)))
    return out


def ts(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_ass(chunks, path, y_center):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{FONT},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for a, b, text in chunks:
        text = text.upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ts(a)},{ts(b)},Main,,0,0,0,,{{\\pos({W // 2},{y_center})}}{text}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ---------- 4. armar el video vertical ----------
def render(src, cam, vw, vh, ass, dst):
    if cam:
        x, y, cw, ch = cam
        gw = min(vw, vh * W / HALF)
        graph = (f"[0:v]crop={cw}:{ch}:{x}:{y},scale={W}:{HALF},setsar=1[top];"
                 f"[0:v]crop={int(gw)//2*2}:{int(vh)//2*2}:{int((vw-gw)/2)//2*2}:0,scale={W}:{HALF},setsar=1[bot];"
                 f"[top][bot]vstack=inputs=2[v]")
    else:
        graph = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2[bg];"
                 f"[0:v]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v]")
    graph += f";[v]subtitles={ass.as_posix()}[out]"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", graph,
         "-map", "[out]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", "59", dst])


# ---------- 5. galeria ----------
def build_index():
    days = sorted([d for d in (SITE / "clips").glob("*") if d.is_dir()], reverse=True)
    for old in days[KEEP_DAYS:]:
        shutil.rmtree(old)
    days = days[:KEEP_DAYS]
    blocks = []
    for d in days:
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        items = json.loads(mf.read_text())
        cards = []
        for it in items:
            vids = "".join(
                f'<figure><video controls preload="none" src="clips/{d.name}/{f}"></video>'
                f'<figcaption>{lang.upper()}</figcaption></figure>'
                for lang, f in it["files"].items())
            cards.append(
                f'<article><h3>{html.escape(it["streamer"])} · {it.get("views") or "?"} vistas</h3>'
                f'<p class="t">{html.escape(it["title"])}</p><div class="v">{vids}</div>'
                f'<p class="h">{html.escape(" ".join(it["hashtags"]))}</p>'
                f'<p><a href="{it["source"]}">clip original</a> · id <code>{it["id"]}</code></p></article>')
        blocks.append(f"<h2>{d.name}</h2>" + "".join(cards))
    page = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Fábrica Sidekick</title><style>
body{{font-family:system-ui;background:#0b0620;color:#eee;margin:0;padding:16px}}
h1{{color:#ff2896}}h2{{color:#00e6ff;border-bottom:1px solid #333}}
article{{background:#1a1238;border-radius:12px;padding:12px;margin:12px 0}}
.v{{display:flex;gap:10px;flex-wrap:wrap}}video{{width:220px;border-radius:8px;background:#000}}
figure{{margin:0}}.t{{opacity:.8}}.h{{color:#00e6ff}}a{{color:#ff9bd0}}</style>
<h1>Fábrica Sidekick</h1><p>Clips generados automáticamente. Dile a Claude cuáles aprobar.</p>{''.join(blocks) or '<p>Aún no hay clips.</p>'}"""
    (SITE / "index.html").write_text(page, encoding="utf-8")


def main():
    DAY.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(exist_ok=True)
    (SITE / ".nojekyll").touch()
    mf = DAY / "manifest.json"
    manifest = json.loads(mf.read_text()) if mf.exists() else []
    done = {m["id"] for m in manifest}
    for st in CFG["streamers"]:
        made = 0
        for e in top_clips(st["login"]):
            if made >= PER:
                break
            cid = str(e.get("id") or e.get("url", "").rstrip("/").split("/")[-1])
            if cid in done:
                continue
            url = e.get("url")
            try:
                meta = ytdlp_json(["-J", url]) or {}
                raw = WORK / f"{cid}.mp4"
                run(["yt-dlp", "-f", "best[height<=1080]/best", "-o", raw, url])
                cam, vw, vh = find_cam(raw)
                segs, lang = transcribe(raw)
                files = {}
                y_sub = HALF if cam else int(H * 0.78)
                texts = [s.text.strip() for s in segs]
                for target in st.get("languages", ["en", "es"]):
                    if target == lang:
                        chunks = chunks_from_words(segs)
                    else:
                        tr = translate(texts, lang, target)
                        if tr is None:
                            continue
                        chunks = chunks_spread(segs, tr)
                    ass = WORK / f"{cid}-{target}.ass"
                    write_ass(chunks, ass, y_sub)
                    name = f"{st['login']}-{cid}-{target}.mp4"
                    render(raw, cam, vw, vh, ass, DAY / name)
                    files[target] = name
                if not files:
                    continue
                manifest.append({
                    "id": cid, "streamer": st["name"], "login": st["login"],
                    "title": meta.get("title") or e.get("title") or "",
                    "views": meta.get("view_count"), "source": url,
                    "hashtags": st["hashtags"], "layout": "cam-arriba" if cam else "completo",
                    "spoken_language": lang, "transcript": " ".join(texts)[:1500],
                    "files": files,
                })
                made += 1
                mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
            except Exception as ex:
                print("clip fallo", cid, ex, flush=True)
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    build_index()
    print(f"listo: {len(manifest)} clips en {DAY}")


if __name__ == "__main__":
    main()
