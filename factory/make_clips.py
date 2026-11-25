"""Fabrica Sidekick v4.

1. Busca los clips mas vistos de GTA de cada streamer (Twitch).
2. Los pasa a vertical: camara del streamer arriba, juego abajo.
3. Separa las dos "voces": lo que dice el streamer (arriba) y lo que dicen los
   personajes del juego (abajo, leido de los subtitulos del propio juego).
4. Subtitulos con la palabra actual resaltada en morado, en ingles y/o espanol.
5. Publica la galeria y prepara la lista de aprobacion del dia.
v4: filtro de desnudos (NudeNet), juez con IA (Gemini mira el clip: que tan gracioso es,
    si se entiende, titulo que describe lo que pasa y recorte de la mejor parte),
    clips de 20 s o mas y mejor calidad de imagen.
"""
import datetime
import difflib
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).parent
SITE = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "site")
CFG = json.loads((ROOT / "streamers.json").read_text())
REPO = os.environ.get("GITHUB_REPOSITORY", "sidekick-factory/fabrica")
RAW = f"https://raw.githubusercontent.com/{REPO}/gh-pages"
PAGES = f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}"  # se ve en el navegador
RANGE = os.environ.get("RANGE", "24hr") or "24hr"
PERIOD = {"24hr": "LAST_DAY", "7d": "LAST_WEEK", "30d": "LAST_MONTH"}.get(RANGE, "LAST_DAY")
KEEP_DAYS = 7
TODAY = datetime.datetime.utcnow().strftime("%Y-%m-%d")
DAY = SITE / "clips" / TODAY
WORK = pathlib.Path("work")
W, H, HALF = 1080, 1920, 960
MIN_SECONDS = int(os.environ.get("MIN_SECONDS", "20"))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
JUDGE_MINUTES = float(os.environ.get("JUDGE_MINUTES", "35"))  # tiempo maximo del juez por grupo
FONT = "DejaVu Sans"
PURPLE = "&H00FF4DC0&"  # morado brillante (#C04DFF en BGR)
BLOCKED = ["steamy", "sneaky pic", "nsfw", "nude", "naked", "sex", "boob", "thirst", "18+", "gyatt",
           "drunk", "drugs", "weed", "high", "racist", "n word", "slur", "fight irl", "gg ban", "kill myself"]
CHANNELS = {"en": "daily", "es": "diario"}
GQL_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"


def run(cmd):
    print("+", " ".join(map(str, cmd))[:400], flush=True)
    subprocess.run(list(map(str, cmd)), check=True)


def ytdlp_json(args):
    r = subprocess.run(["yt-dlp", *args], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr[-1500:])
        return None
    return json.loads(r.stdout)


# ---------------- 1. buscar clips de GTA ----------------
def gql_clips(login):
    q = ("query($l:String!){user(login:$l){clips(first:60,criteria:{period:%s,sort:VIEWS_DESC})"
         "{edges{node{id slug title viewCount durationSeconds url game{name} language}}}}}" % PERIOD)
    req = urllib.request.Request("https://gql.twitch.tv/gql", method="POST",
                                 data=json.dumps({"query": q, "variables": {"l": login}}).encode(),
                                 headers={"Client-Id": GQL_ID, "Content-Type": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        edges = data["data"]["user"]["clips"]["edges"]
        return [e["node"] for e in edges]
    except Exception as e:
        print("gql fallo", login, e)
        return None


def candidates(st):
    nodes = gql_clips(st["login"])
    out = []
    if nodes is None:  # respaldo sin filtro de juego
        data = ytdlp_json(["--flat-playlist", "-J",
                           f"https://www.twitch.tv/{st['login']}/clips?filter=clips&range={RANGE}"]) or {}
        for e in data.get("entries", []):
            out.append({"id": str(e.get("id")), "title": e.get("title") or "", "views": e.get("view_count") or 0,
                        "url": e.get("url"), "game": "?"})
        return out
    for n in nodes:
        game = ((n.get("game") or {}).get("name") or "")
        if "grand theft auto" not in game.lower():
            continue
        if (n.get("durationSeconds") or 0) < MIN_SECONDS:
            continue  # los clips muy cortos no tienen contexto
        out.append({"id": n["id"], "title": n.get("title") or "", "views": n.get("viewCount") or 0,
                    "seconds": n.get("durationSeconds") or 0,
                    "url": n.get("url") or f"https://clips.twitch.tv/{n['slug']}", "game": game})
    return out


# ---------------- 1b. filtros: desnudos y juez con IA ----------------
_NUDE = None
NUDE_HARD = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
NUDE_SOFT = {"FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"}


def nudity(path, frames=16):
    """Revisa 16 fotos del clip. Descarta desnudos explicitos (no es ultra estricto:
    un poco de piel o ropa de playa pasa; pechos/trasero expuestos en varias fotos no)."""
    global _NUDE
    try:
        if _NUDE is None:
            from nudenet import NudeDetector
            _NUDE = NudeDetector()
    except Exception as e:
        print("nudenet no disponible", e)
        return False, "sin-revisar"
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    hard, soft = 0, 0
    for i in range(frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (i + 0.5) / frames))
        ok, fr = cap.read()
        if not ok:
            continue
        WORK.mkdir(exist_ok=True)
        tmp = WORK / "nude.jpg"
        cv2.imwrite(str(tmp), fr)
        try:
            found = {d["class"] for d in _NUDE.detect(str(tmp)) if d["score"] >= (0.5 if d["class"] in NUDE_HARD else 0.6)}
        except Exception as e:
            print("nudenet error", e)
            continue
        hard += bool(found & NUDE_HARD)
        soft += bool(found & NUDE_SOFT)
    cap.release()
    bad = hard >= 1 or soft >= 3
    return bad, f"explicito={hard} piel={soft}"


JUDGE_PROMPT = """Eres editor de un canal de YouTube Shorts de momentos graciosos de streamers jugando GTA.
Te paso {n} fotos del clip de {name} (en orden, una cada {step:.1f} s), el titulo original del clip ("{title}")
y la transcripcion del audio con tiempos en segundos:
{transcript}

Responde SOLO un JSON:
{{
 "que_pasa": "una frase concreta de lo que ocurre (quien hace que y por que es gracioso)",
 "gracioso": 1-10 (que tan divertido/sorprendente es para alguien que no conoce el stream),
 "contexto": 1-10 (se entiende lo que pasa sin haber visto el directo),
 "desnudos": "ninguno" | "leve" | "explicito" (pechos, genitales o sexo visibles = explicito; ropa interior o escenas sugerentes = leve),
 "apto_monetizar": true/false (false SOLO si hay desnudos explicitos, sexo mostrado o descrito con detalle, o insultos de odio; la violencia, disparos y peleas DEL JUEGO GTA, groserias normales, gimnasio, ropa ajustada o bromas leves SI son aptos -> true; ante la duda, true),
 "inicio": segundo donde empieza la mejor parte (con algo de contexto antes del chiste),
 "fin": segundo donde termina (justo despues del remate),
 "titulo_en": "titulo en ingles, max 60 caracteres, que describa EXACTAMENTE lo que pasa y genere curiosidad, estilo Shorts virales, con el nombre del streamer, sin hashtags, 1 emoji al final",
 "titulo_es": "lo mismo en espanol natural (no traduccion literal)",
 "angulo": "reaccion" | "curiosidad_del_juego" | "fail" | "drama" | "otro" (curiosidad_del_juego = muestra algo sorprendente que se puede hacer en GTA 6),
 "idea_tendencia": numero de la idea en tendencia que este clip cumple (o null si ninguna)
}}
Ejemplo de buen titulo: "IlloJuan desinstala GTA por accidente en pleno directo 😭". Malo: "IlloJuan en GTA 6".
Si el clip muestra una mecanica curiosa del juego, usa el formato pregunta: "GTA 6: ¿qué pasa si le das tu arma al cajero?" (formato viral en otros canales).
{tendencias}{receta}"""

AI = {"calls": 0, "fails": 0, "off": False, "gemini_models": None}
AI_MAX_CALLS = int(os.environ.get("AI_MAX_CALLS", "45"))  # tope de consultas por corrida (cuida el cupo y los $)
AI_ORDER = [x for x in os.environ.get("AI_ORDER", "claude,gemini").split(",") if x]  # Claude primero (preciso), Gemini gratis de respaldo
RECETA_FILE = ROOT / "receta.json"
RECETA = json.loads(RECETA_FILE.read_text()) if RECETA_FILE.exists() else {}


def receta_text():
    """Lo que la fabrica ya aprendio de las vistas reales (lo actualiza factory/aprender.py)."""
    parts = []
    if RECETA.get("lecciones"):
        parts.append("Lo que mejor funciona en nuestros canales (datos reales de vistas):\n- " + "\n- ".join(RECETA["lecciones"][:12]))
    if RECETA.get("evitar"):
        parts.append("Evitar:\n- " + "\n- ".join(RECETA["evitar"][:8]))
    return ("\n" + "\n".join(parts)) if parts else ""


TEND_FILE = ROOT / "tendencias.json"
TEND = json.loads(TEND_FILE.read_text()) if TEND_FILE.exists() else {}


def tendencias_text():
    ideas = TEND.get("ideas") or []
    if not ideas:
        return ""
    lines = [f"{i}. {d.get('idea')} (titulo modelo: {d.get('titulo_modelo_es') or d.get('titulo_modelo_en')})"
             for i, d in enumerate(ideas[:8], 1)]
    return ("Ideas que se estan volviendo virales esta semana (si el clip cumple una, usa ese gancho en el titulo, "
            "con palabras propias):\n" + "\n".join(lines) + "\n")


def trend_hit(c, words):
    """Coincidencia rapida por palabras clave, para priorizar que clips revisa la IA."""
    text = ((c.get("title") or "") + " " + " ".join(w["word"] for w in (words or []))).lower()
    return any(k.lower() in text for d in (TEND.get("ideas") or []) for k in (d.get("buscar") or []) if len(k) > 3)


def streamer_weight(login):
    return float((RECETA.get("peso_streamer") or {}).get(login, 1.0))
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()  # opcion B: Claude (si algun dia se agrega la clave)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")


def clip_frames(path, n=8, width=512):
    """Fotos chicas del clip para que la IA 'vea' sin gastar tanto como un video entero."""
    cap = cv2.VideoCapture(str(path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    out = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / n))
        ok, fr = cap.read()
        if not ok:
            continue
        h, w = fr.shape[:2]
        fr = cv2.resize(fr, (width, int(h * width / w)))
        out.append(cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes())
    cap.release()
    return out, (total / fps) / max(n, 1)


def transcript_text(words, maxlen=2500):
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["start"]
        cur.append(w["word"])
        if len(cur) >= 12 or w["word"].endswith((".", "?", "!")):
            lines.append(f"[{t0:.1f}] " + " ".join(cur))
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.1f}] " + " ".join(cur))
    return "\n".join(lines)[:maxlen] or "(sin dialogo)"


def _parse(txt):
    txt = (txt or "").strip()
    if txt.startswith(chr(96) * 3):  # respuesta envuelta en bloque de codigo
        txt = txt.strip(chr(96)).split("\n", 1)[-1]
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < i:
        raise ValueError(f"la respuesta no trae un JSON completo (¿se corto?): {txt[:200]!r}")
    out = json.loads(txt[i:j + 1])
    return out[0] if isinstance(out, list) else out


def _gemini_models(client):
    if AI["gemini_models"] is None:
        env = [m for m in os.environ.get("GEMINI_MODELS", "").split(",") if m]
        found = []
        try:
            for m in client.models.list():
                name = m.name.split("/")[-1]
                if "flash" in name and not any(k in name for k in ("image", "tts", "live", "audio", "embed", "preview-tts")):
                    found.append(name)
        except Exception as e:
            print("no se pudo listar modelos de gemini", e, flush=True)
        def ver(n):
            nums = re.findall(r"\d+(?:\.\d+)?", n)
            return float(nums[0]) if nums else 0
        found.sort(key=lambda n: (-ver(n), "lite" in n, "preview" in n, len(n)))
        AI["gemini_models"] = (env or found[:3] or ["gemini-3.8-flash"])
        print("modelos de gemini a usar:", AI["gemini_models"], flush=True)
    return AI["gemini_models"]


def _ask_gemini(frames, prompt):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_KEY)
    parts = [types.Part.from_bytes(data=f, mime_type="image/jpeg") for f in frames] + [prompt]
    last = None
    for model in _gemini_models(client):
        for attempt in range(2):
            try:
                r = client.models.generate_content(
                    model=model, contents=parts,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4))
                out = _parse(r.text)
                out["modelo"] = model
                return out
            except Exception as e:
                last, msg = e, str(e)
                print("gemini", model, "fallo:", msg[:160], flush=True)
                if "503" in msg or "UNAVAILABLE" in msg:
                    time.sleep(10)
                    continue
                break  # 404 (modelo no existe) o 429 (sin cupo): probar el siguiente modelo
    raise RuntimeError(str(last)[:300])


def _ask_claude(frames, prompt):
    import base64
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(f).decode()}} for f in frames]
    content.append({"type": "text", "text": prompt})
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", method="POST",
        data=json.dumps({"model": CLAUDE_MODEL, "max_tokens": 2500,
                         "messages": [{"role": "user", "content": content}]}).encode(),
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    r = claude_request(req)
    out = _parse("".join(b.get("text", "") for b in r.get("content", [])))
    out["modelo"] = CLAUDE_MODEL
    return out


def claude_request(req):
    """Llama a la API de Claude y deja en el registro el motivo exacto si algo sale mal."""
    try:
        resp = urllib.request.urlopen(req, timeout=90)
        raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:400]
        raise RuntimeError(f"Claude HTTP {e.code}: {body!r}")
    try:
        r = json.loads(raw)
    except Exception:
        raise RuntimeError(f"Claude respondio algo que no es JSON (status {resp.status}): {raw[:300]!r}")
    if not any(b.get("text") for b in r.get("content", [])):
        raise RuntimeError(f"Claude respondio sin texto: {json.dumps(r)[:300]}")
    return r


def ai_judge(path, st, c, words):
    """La IA (Gemini gratis; Claude como opcion B) mira fotos del clip + lee lo que se dice
    y decide si vale la pena, la mejor parte y el titulo. Si no hay cupo, la fabrica sigue sin IA."""
    if AI["off"] or not (GEMINI_KEY or ANTHROPIC_KEY):
        return None
    if AI["calls"] >= AI_MAX_CALLS:
        if not AI.get("cap_msg"):
            print("tope de consultas a la IA alcanzado; el resto se elige por vistas", flush=True)
            AI["cap_msg"] = True
        return None
    frames, step = clip_frames(path)
    prompt = JUDGE_PROMPT.format(n=len(frames), step=step, name=st["name"], receta=receta_text(), tendencias=tendencias_text(),
                                 title=(c.get("title") or "").replace('"', "'"), transcript=transcript_text(words))
    AI["calls"] += 1
    providers = {"gemini": (_ask_gemini, GEMINI_KEY), "claude": (_ask_claude, ANTHROPIC_KEY)}
    for name in AI_ORDER:
        fn, ok = providers.get(name, (None, None))
        if not ok:
            continue
        try:
            out = fn(frames, prompt)
            AI["fails"] = 0
            return out
        except Exception as e:
            print("IA", name, "no respondio:", str(e)[:200], flush=True)
    AI["fails"] += 1
    if AI["fails"] >= 3:
        AI["off"] = True
        print("la IA fallo 3 veces seguidas (sin cupo o saturada): se apaga por hoy", flush=True)
    return None


def trim(src, start, end, dst):
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", src,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-c:a", "aac", "-b:a", "192k", dst])


def clean_title(t, st, lang):
    t = re.sub(r"[#]\w+", "", t or "").strip(" -|\"'")
    if not t:
        return None
    tag = st["hashtags"][0]
    return (t[:80] + f" {tag} #GTA6 #Shorts").strip()


# ---------------- 2. camara del streamer ----------------
def save_frame(path, dst):
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
    ok, frame = cap.read()
    cap.release()
    if ok:
        cv2.imwrite(str(dst), cv2.resize(frame, (960, 540)), [cv2.IMWRITE_JPEG_QUALITY, 80])


def find_cam(path, fixed=None):
    """La camara del streamer es la cara que NO se mueve; los personajes del juego si."""
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    vw, vh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    if fixed:
        cap.release()
        fx, fy, fw_, fh_ = fixed
        cx, cy, fw = (fx + fw_ / 2) * vw, (fy + fh_ / 2) * vh, fw_ * vw / 4.5
    else:
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if casc.empty():
            return None, vw, vh
        dets, frames = [], 16
        for i in range(frames):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * (i + 0.5) / frames))
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for (x, y, w, h) in casc.detectMultiScale(gray, 1.1, 6, minSize=(int(vh * 0.05),) * 2):
                dets.append((i, x + w / 2, y + h / 2, w))
        cap.release()
        best, best_score = None, 0
        for d in dets:
            near = [e for e in dets if abs(e[1] - d[1]) < vw * 0.035 and abs(e[2] - d[2]) < vh * 0.05]
            score = len({e[0] for e in near})
            if score > best_score:
                best, best_score = near, score
        if not best or best_score < frames * 0.4:
            return None, vw, vh
        cx, cy, fw = np.median(np.array([(e[1], e[2], e[3]) for e in best]), axis=0)
        if fw > vw * 0.22:
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


# ---------------- 3. leer los subtitulos del juego (voz de los personajes) ----------------
def _clean(t):
    t = re.sub(r"[^\w\s'¿?¡!.,áéíóúñÁÉÍÓÚÑ-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def ocr_game_subs(path, band):
    """Lee la franja donde el juego pone sus subtitulos (letra chica y blanca)."""
    import pytesseract
    x0, y0, x1, y1 = band
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    vw, vh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    step = max(1, int(fps * 0.4))
    reads = []
    for f in range(0, n, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        if not ok:
            break
        crop = frame[int(vh * y0):int(vh * y1), int(vw * x0):int(vw * x1)]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        _, th = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
        d = pytesseract.image_to_data(th, config="--psm 6", output_type=pytesseract.Output.DICT)
        words = []
        for txt, conf, h in zip(d["text"], d["conf"], d["height"]):
            # solo letra chica (subtitulo del juego), no los captions grandes del stream
            if txt.strip() and float(conf) > 50 and h / 2 < vh * 0.032:
                words.append(txt)
        reads.append((f / fps, _clean(" ".join(words))))
    cap.release()
    events = []
    for t, txt in reads:
        if len(txt.split()) < 2:
            continue
        if events and difflib.SequenceMatcher(None, events[-1]["text"].lower(), txt.lower()).ratio() > 0.7:
            events[-1]["end"] = t + 0.4
            if len(txt) > len(events[-1]["text"]):
                events[-1]["text"] = txt
        else:
            events.append({"start": t, "end": t + 0.4, "text": txt})
    return [e for e in events if e["end"] - e["start"] >= 0.7]


def guess_lang(text):
    w = set(re.findall(r"[a-záéíóúñ']+", text.lower()))
    es = len(w & {"el", "la", "que", "de", "no", "es", "y", "en", "un", "una", "por", "con", "lo", "me", "te", "qué", "pero"})
    en = len(w & {"the", "you", "i", "to", "a", "is", "it", "and", "that", "what", "my", "me", "on", "of", "your", "don't"})
    return "es" if es > en else "en"


# ---------------- 4. transcribir, separar voces, traducir ----------------
_model = None


def transcribe(path):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    segs, info = _model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in (s.words or []):
            words.append({"start": w.start, "end": w.end, "word": w.word.strip()})
    return words, info.language


def _norm(w):
    return re.sub(r"[^\wáéíóúñ']", "", w.lower())


def split_voices(words, game_events):
    """Separa las dos voces. Se decide por frase: si buena parte de una frase del audio
    coincide con el subtitulo que el juego muestra en ese momento, la frase es de un
    personaje; si no, es del streamer. Asi nunca se mezclan."""
    def matches(w):
        n = _norm(w["word"])
        if not n:
            return False
        for e in game_events:
            if e["start"] - 1.2 <= w["start"] <= e["end"] + 1.2:
                if any(difflib.SequenceMatcher(None, n, _norm(x)).ratio() >= 0.75 for x in e["text"].split()):
                    return True
        return False
    def in_window(w):
        return any(e["start"] - 0.6 <= w["start"] <= e["end"] + 0.6 for e in game_events)
    talk, game = [], []
    for p in phrases(words, gap=0.45, maxw=14):
        cand = [w for w in p["words"] if in_window(w)]
        hits = sum(matches(w) for w in cand)
        is_game = hits >= max(1, 0.3 * len(cand))
        for w in p["words"]:
            (game if is_game and w in cand else talk).append(w)
    talk.sort(key=lambda w: w["start"])
    game.sort(key=lambda w: w["start"])
    return talk, game


def phrases(words, gap=0.7, maxw=10):
    """Agrupa palabras en frases para traducir con sentido."""
    out, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > gap or len(cur) >= maxw):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return [{"start": p[0]["start"], "end": p[-1]["end"], "text": " ".join(x["word"] for x in p), "words": p} for p in out]


_tr_cache = {}


def translate(text, src, dst):
    if src == dst or not text.strip():
        return text
    key = (text, src, dst)
    if key in _tr_cache:
        return _tr_cache[key]
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    langs = {"en": "en-US", "es": "es-ES"}
    for make in (lambda: GoogleTranslator(source=src, target=dst),
                 lambda: MyMemoryTranslator(source=langs.get(src, src), target=langs.get(dst, dst))):
        try:
            out = make().translate(text)
            if out:
                _tr_cache[key] = out
                return out
        except Exception as e:
            print("traductor fallo:", type(e).__name__, e, flush=True)
    return None


def translate_batch(texts, src, dst):
    """Traduce todas las frases de un clip de una vez con Claude (natural, con jerga de streamer).
    Si no hay Claude o falla, usa el traductor gratis frase por frase."""
    if src == dst:
        return list(texts)
    todo = [x for x in texts if (x, src, dst) not in _tr_cache and x.strip()]
    if todo and ANTHROPIC_KEY:
        names = {"en": "ingles", "es": "espanol latino neutro"}
        prompt = (f"Traduce estas frases de un streamer jugando GTA del {names.get(src, src)} al {names.get(dst, dst)}. "
                  "Natural y con la misma energia (jerga y groserias suaves ok), cortas como subtitulos. "
                  "No traduzcas nombres propios. Responde SOLO un JSON: lista de strings, misma cantidad y orden.\n"
                  + json.dumps(todo, ensure_ascii=False))
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", method="POST",
                data=json.dumps({"model": CLAUDE_MODEL, "max_tokens": 3000,
                                 "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            r = claude_request(req)
            txt = "".join(b.get("text", "") for b in r.get("content", []))
            out = json.loads(txt[txt.find("["):txt.rfind("]") + 1])
            if len(out) == len(todo):
                for a, b in zip(todo, out):
                    _tr_cache[(a, src, dst)] = str(b)
        except Exception as e:
            print("traduccion con Claude fallo, uso el traductor gratis:", str(e)[:150], flush=True)
    res = []
    for x in texts:
        if (x, src, dst) in _tr_cache:
            res.append(_tr_cache[(x, src, dst)])
        else:
            time.sleep(0.3)  # el traductor gratis permite pocas consultas por segundo
            res.append(translate(x, src, dst))
    return res


def timed_words(phrase_list, src, dst):
    """Devuelve palabras con tiempo en el idioma destino (reparte el tiempo si se tradujo)."""
    out = []
    need = [p["text"] for p in phrase_list if not (src == dst and p.get("words"))]
    tr = dict(zip(need, translate_batch(need, src, dst))) if need else {}
    for p in phrase_list:
        if src == dst and p.get("words"):
            out.extend({"start": w["start"], "end": w["end"], "word": w["word"]} for w in p["words"])
            continue
        t = tr.get(p["text"])
        if t is None:
            return None
        ws = t.split()
        step = (p["end"] - p["start"]) / max(len(ws), 1)
        out.extend({"start": p["start"] + i * step, "end": p["start"] + (i + 1) * step, "word": w} for i, w in enumerate(ws))
    return out


# ---------------- 5. subtitulos con palabra resaltada en morado ----------------
def ts(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def karaoke_events(words, style, y, size=3):
    lines = []
    for i in range(0, len(words), size):
        g = words[i:i + size]
        for k, cur in enumerate(g):
            nxt = words[i + size]["start"] if i + size < len(words) else cur["end"] + 0.4
            end = g[k + 1]["start"] if k + 1 < len(g) else min(cur["end"] + 0.4, nxt)
            parts = []
            for j, w in enumerate(g):
                txt = w["word"].upper().replace("{", "(").replace("}", ")")
                if j == k:
                    parts.append(f"{{\\1c{PURPLE}\\fscx108\\fscy108}}{txt}{{\\1c&H00FFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(txt)
            lines.append(f"Dialogue: 0,{ts(cur['start'])},{ts(max(end, cur['start'] + 0.08))},{style},,0,0,0,,"
                         f"{{\\pos({W // 2},{y})}}" + " ".join(parts))
    return lines


def write_ass(path, top_words, game_words, y_top, y_game):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Streamer,{FONT},74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,5,50,50,0,1
Style: Juego,{FONT},56,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,5,50,50,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = karaoke_events(top_words or [], "Streamer", y_top) + karaoke_events(game_words or [], "Juego", y_game, size=4)
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


# ---------------- 6. armar el video vertical ----------------
def render(src, cam, vw, vh, band, ass, dst):
    bx0, by0, bx1, by1 = band
    if cam:
        x, y, cw, ch = cam
        gw = int(min(vw, vh * W / HALF)) // 2 * 2
        gx = int((vw - gw) / 2) // 2 * 2
        # franja de subtitulos originales (juego + captions del stream) dentro del recorte del juego
        fy0 = int(vh * (by0 - 0.02)) // 2 * 2
        fh = int(vh * (min(by1 + 0.08, 1) - by0 + 0.02)) // 2 * 2
        fh = min(fh, int(vh) - fy0) // 2 * 2
        graph = (f"[0:v]crop={cw}:{ch}:{x}:{y},scale={W}:{HALF},setsar=1[top];"
                 f"[0:v]crop={gw}:{int(vh)//2*2}:{gx}:0[g];"
                 f"[g]split[g1][g2];[g2]crop={gw}:{fh}:0:{fy0},boxblur=22:3[bl];"
                 f"[g1][bl]overlay=0:{fy0},scale={W}:{HALF},setsar=1[bot];"
                 f"[top][bot]vstack=inputs=2[v]")
    else:
        fy0 = int(vh * (by0 - 0.02)) // 2 * 2
        fh = min(int(vh * (min(by1 + 0.08, 1) - by0 + 0.02)), int(vh) - fy0) // 2 * 2
        graph = (f"[0:v]split[a][b];[b]crop={int(vw)//2*2}:{fh}:0:{fy0},boxblur=22:3[bl];[a][bl]overlay=0:{fy0}[c];"
                 f"[c]split[c1][c2];[c1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2[bg];"
                 f"[c2]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v]")
    graph = graph.replace(f"scale={W}:{HALF}", f"scale={W}:{HALF}:flags=lanczos")
    graph += f";[v]unsharp=5:5:0.6:5:5:0.0,subtitles={ass.as_posix()}[out]"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", graph,
         "-map", "[out]", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-maxrate", "6M", "-bufsize", "12M",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-t", "59", dst])


# ---------------- 7. titulos y descripciones ----------------
def make_title(st, clip_title, lang):
    t = re.sub(r"[#@]\w+", "", clip_title or "").strip(" -|")
    weak = len(re.findall(r"[a-zA-Z]{3,}", t)) < 3
    tag = st["hashtags"][0]
    if lang == "es":
        base = f"{st['name']} en GTA 6 😂" if weak else f"{st['name']}: {translate(t, guess_lang(t), 'es') or t} 😂"
    else:
        base = f"{st['name']} in GTA 6 😂" if weak else f"{st['name']}: {t} 😂"
    return (base[:72] + f" {tag} #GTA6 #Shorts").strip()


def make_desc(st, lang, channel_name):
    tags = " ".join(st["hashtags"] + ["#GTA6", "#GTAVI", "#Shorts"])
    src = f"twitch.tv/{st['login']}"
    if lang == "es":
        return (f"Momento de {st['name']} jugando GTA 6. Clip de su directo; todo el crédito para {st['name']}: {src}\n"
                f"Sigue a {channel_name} para los mejores momentos de GTA cada día.\n"
                f"¿Eres el creador y quieres que lo retiremos? Escríbenos y lo quitamos.\n\n{tags}")
    return (f"{st['name']} playing GTA 6. Clip from the live stream; all credit to {st['name']}: {src}\n"
            f"Follow {channel_name} for the best GTA moments every day.\n"
            f"Are you the creator and want it removed? Message us and we'll take it down.\n\n{tags}")


CH_NAME = {"daily": "Sidekick Daily", "diario": "Sidekick Diario"}


# ---------------- 8. galeria y lista de aprobacion ----------------
def build_index():
    days = sorted([d for d in (SITE / "clips").glob("*") if d.is_dir()], reverse=True)
    for old in days[KEEP_DAYS:]:
        shutil.rmtree(old)
    blocks = []
    for d in days[:KEEP_DAYS]:
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        cards = []
        for it in json.loads(mf.read_text()):
            vids = "".join(f'<figure><video controls preload="none" src="clips/{d.name}/{o["file"]}"></video>'
                           f'<figcaption>{CH_NAME[o["channel"]]}<br>{html.escape(o["title"])}</figcaption></figure>'
                           for o in it["outputs"])
            cards.append(f'<article><h3>{html.escape(it["streamer"])} · {it.get("views")} vistas</h3>'
                         f'<div class="v">{vids}<figure><img src="clips/{d.name}/{it["id"]}-frame.jpg">'
                         f'<figcaption>ORIGINAL</figcaption></figure></div></article>')
        blocks.append(f"<h2>{d.name}</h2>" + "".join(cards))
    (SITE / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Fábrica Sidekick</title><style>body{font-family:system-ui;background:#0b0620;color:#eee;padding:16px}"
        "h2{color:#00e6ff}article{background:#1a1238;border-radius:12px;padding:12px;margin:12px 0}"
        ".v{display:flex;gap:10px;flex-wrap:wrap}video,img{width:220px;border-radius:8px}figure{margin:0;width:220px}"
        "</style><h1>Fábrica Sidekick</h1>" + "".join(blocks), encoding="utf-8")


def build_issue(manifest):
    lines = [f"## Clips del {TODAY}",
             "Marca la casilla de cada video que quieras publicar y se sube solo en 1-2 minutos.",
             "Puedes cambiar el título antes de marcar (edita el texto después de `Título:`).", "",
             "- [ ] `todo` **Aprobar TODOS los de esta lista** (cuidado: publica todo)", ""]
    for i, it in enumerate(manifest, 1):
        tag = " · 🔥 versión propia de una idea viral" if it.get("tendencia") else ""
        lines.append(f"### {i}. {it['streamer']} · {it.get('views')} vistas{tag}")
        lines.append(f"![]({RAW}/clips/{TODAY}/{it['id']}-frame.jpg)")
        for o in it["outputs"]:
            url = f"{PAGES}/clips/{TODAY}/{o['file']}"
            lines.append(f"- [ ] `{TODAY}/{o['file']}|{o['channel']}` **{CH_NAME[o['channel']]}** "
                         f"([ver video]({url})) · Título: {o['title']}")
        lines.append("")
    return "\n".join(lines)


# ---------------- main ----------------
FUNNY = re.compile(r"\b(ja(ja)+|je(je)+|lol|lmao|wtf|bro|nah|what|que|no+o+|dios|madre)\b", re.I)


def judge(st, c):
    """Descarga y evalua el clip. Devuelve un puntaje (o None si se descarta)."""
    import math
    cid, url = c["id"], c["url"]
    raw = WORK / f"{cid}.mp4"
    if not raw.exists():
        run(["yt-dlp", "-q", "-f", "best[height<=1080]/best", "-o", raw, url])
    bad, why = nudity(raw)
    if bad:
        print("descartado por desnudos:", cid, why, flush=True)
        return None
    words, _ = transcribe(raw)
    if len(words) < 6:
        print("descartado: casi no hay dialogo", cid, flush=True)
        return None
    j = ai_judge(raw, st, c, words)
    c["ia"] = j
    base = math.log10(max(c["views"], 10)) * 0.4 + (streamer_weight(st["login"]) - 1.0) * 2
    if j:
        print("IA:", cid, json.dumps(j, ensure_ascii=False)[:400], flush=True)
        if j.get("desnudos") == "explicito" or j.get("apto_monetizar") is False:
            print("descartado por la IA (no monetizable)", cid, flush=True)
            return None
        if (j.get("gracioso") or 0) < 5 or (j.get("contexto") or 0) < 4:
            print("descartado por la IA (poco gracioso o sin contexto)", cid, flush=True)
            return None
        c["tendencia"] = bool(j.get("idea_tendencia"))
        bonus = 1.5 if c["tendencia"] else 0.0  # version propia de una idea viral
        return (j.get("gracioso") or 0) * 0.65 + (j.get("contexto") or 0) * 0.35 + base + 1.0 + bonus
    # sin IA: mas vistas, mas dialogo y mas reacciones ("jaja", "wtf", "no no no") = mejor
    secs = max(c.get("seconds") or 30, 1)
    density = min(len(words) / secs, 3.5)
    laughs = min(len(FUNNY.findall(" ".join(w["word"] for w in words))), 6)
    c["tendencia"] = trend_hit(c, words)
    return 4.0 + density * 0.4 + laughs * 0.15 + base + (1.0 if c["tendencia"] else 0.0)


def process(st, c, manifest):
    cid, url = c["id"], c["url"]
    raw = WORK / f"{cid}.mp4"
    if not raw.exists():
        run(["yt-dlp", "-q", "-f", "best[height<=1080]/best", "-o", raw, url])
    j = c.get("ia") or {}
    try:
        a, b = float(j.get("inicio") or 0), float(j.get("fin") or 0)
        if b - a >= MIN_SECONDS * 0.8 and b - a < (c.get("seconds") or 60) - 2:
            cut = WORK / f"{cid}-cut.mp4"
            trim(raw, max(a - 0.5, 0), b + 0.8, cut)
            raw = cut
    except Exception as e:
        print("recorte fallo", e)
    save_frame(raw, DAY / f"{cid}-frame.jpg")
    cam, vw, vh = find_cam(raw, st.get("cam"))
    band = st.get("sub_band", [0.12, 0.855, 0.88, 0.935])
    game_ev = ocr_game_subs(raw, band)
    words, spoken = transcribe(raw)
    talk, game_w = split_voices(words, game_ev)
    if len(words) < 6:
        print("descartado: casi no hay dialogo", cid)
        return False
    game_lang = spoken  # el audio del juego se transcribe junto con el del streamer
    talk_phr, game_phr = phrases(talk), phrases(game_w, gap=0.5, maxw=12)
    y_top, y_game = (HALF - 110, HALF + 600) if cam else (560, 1330)
    outputs = []
    for lang in st["outputs"]:
        top = timed_words(talk_phr, spoken, lang)
        game = timed_words(game_phr, game_lang, lang)
        if top is None or game is None:
            continue
        ass = WORK / f"{cid}-{lang}.ass"
        write_ass(ass, top, game, y_top, y_game)
        name = f"{st['login']}-{cid}-{lang}.mp4"
        render(raw, cam, vw, vh, band, ass, DAY / name)
        ch = CHANNELS[lang]
        title = clean_title(j.get(f"titulo_{lang}"), st, lang) or make_title(st, c["title"], lang)
        outputs.append({"file": name, "lang": lang, "channel": ch,
                        "title": title, "desc": make_desc(st, lang, CH_NAME[ch])})
    if not outputs:
        return False
    manifest.append({"id": cid, "streamer": st["name"], "login": st["login"], "title": c["title"],
                     "views": c["views"], "source": url, "game": c.get("game"),
                     "layout": "cam-arriba" if cam else "completo", "spoken_language": spoken,
                     "ia": j or None, "tendencia": bool(c.get("tendencia")),
                     "game_text": " ".join(w["word"] for w in game_w)[:1500], "streamer_text": " ".join(w["word"] for w in talk)[:1500],
                     "outputs": outputs})
    return True


def main():
    WORK.mkdir(exist_ok=True)
    if os.environ.get("FRESH") == "si" and DAY.exists():
        shutil.rmtree(DAY)
    DAY.mkdir(parents=True, exist_ok=True)
    (SITE / ".nojekyll").touch()
    mf = DAY / "manifest.json"
    manifest = json.loads(mf.read_text()) if mf.exists() else []
    done = {m["id"] for m in manifest}
    before = set(done)  # clips de corridas anteriores del mismo dia (ya estan en otra lista)
    for group, quota in CFG["daily"].items():
        pool = []
        for st in [s for s in CFG["streamers"] if s["group"] == group]:
            for c in candidates(st)[:12]:
                pool.append((st, c))
        pool.sort(key=lambda p: -p[1]["views"])
        # 1) el juez revisa ~3 veces mas clips de los que se necesitan
        judged, per_j = [], {}
        t0 = time.time()
        for st, c in pool:
            if len(judged) >= quota * (3 if ANTHROPIC_KEY else 2) or time.time() - t0 > JUDGE_MINUTES * 60:
                break
            if c["id"] in done or per_j.get(st["login"], 0) >= CFG.get("max_per_streamer", 3) * 2:
                continue
            if any(k in (c["title"] or "").lower() for k in BLOCKED):
                print("descartado por tema sensible:", c["title"])
                continue
            try:
                sc = judge(st, c)
            except Exception as ex:
                print("juez fallo", c["id"], ex, flush=True)
                continue
            if sc is not None:
                judged.append((sc, st, c))
                per_j[st["login"]] = per_j.get(st["login"], 0) + 1
        judged.sort(key=lambda x: -x[0])
        # al menos 1 "version propia" de una idea viral por grupo, si hay alguna
        trend = [x for x in judged if x[2].get("tendencia")]
        if trend and trend[0] not in judged[:quota]:
            judged.remove(trend[0])
            judged.insert(0, trend[0])
        # 2) se fabrican solo los mejores
        made, per = 0, {}
        for sc, st, c in judged:
            if made >= quota:
                break
            if per.get(st["login"], 0) >= CFG.get("max_per_streamer", 3):
                continue
            try:
                if process(st, c, manifest):
                    made += 1
                    per[st["login"]] = per.get(st["login"], 0) + 1
                    done.add(c["id"])
                    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
            except Exception as ex:
                print("clip fallo", c["id"], ex, flush=True)
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    build_index()
    new = [m for m in manifest if m["id"] not in before]
    pathlib.Path("issue.md").write_text(build_issue(new), encoding="utf-8")
    pathlib.Path("new_count").write_text(str(len(new)))
    print(f"listo: {len(manifest)} clips en {DAY}")


if __name__ == "__main__":
    main()
