"""La fabrica aprende de sus propios resultados.

1. Junta todos los Shorts que se subieron (las lineas con ✅ de las listas de aprobacion).
2. Pide a YouTube cuantas vistas, likes y comentarios tiene cada uno.
3. Le pasa la tabla a la IA (Claude; Gemini de respaldo) y le pide reglas cortas:
   que titulos, angulos y streamers funcionan y cuales no.
4. Guarda esas reglas en factory/receta.json. La fabrica las lee cada manana,
   asi cada dia elige y titula mejor sin gastar mas.
"""
import base64
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "sidekick-factory/fabrica")
ROOT = pathlib.Path(__file__).parent
RECETA = ROOT / "receta.json"
HIST = ROOT / "historial.json"
RAW = f"https://raw.githubusercontent.com/{REPO}/gh-pages/clips"
OK = re.compile(r"^- \[[xX]\] `(?P<day>[^/`]+)/(?P<file>[^`|]+)\|(?P<ch>daily|diario)` ✅ https://youtube\.com/shorts/(?P<vid>[\w-]+)(?P<rest>.*)$", re.M)


def gh(*args):
    return subprocess.run(["gh", *args, "--repo", REPO], capture_output=True, text=True, check=True).stdout


def refresh_token(channel):
    env = os.environ.get(f"YT_REFRESH_{channel.upper()}")
    if env:
        return env
    from cryptography.fernet import Fernet
    k = base64.urlsafe_b64encode(hashlib.sha256((os.environ["YT_CLIENT_SECRET"] + "|sidekick").encode()).digest())
    return Fernet(k).decrypt(pathlib.Path(f"tokens/{channel}.enc").read_bytes()).decode()


def youtube(channel):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=refresh_token(channel), token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"].strip(), client_secret=os.environ["YT_CLIENT_SECRET"].strip())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def collect():
    hist = json.loads(HIST.read_text()) if HIST.exists() else {}
    issues = json.loads(gh("issue", "list", "--label", "aprobacion", "--state", "all", "--limit", "60", "--json", "body"))
    manifests = {}
    for it in issues:
        for m in OK.finditer(it["body"] or ""):
            vid = m.group("vid")
            rec = hist.get(vid, {})
            title = m.group("rest").split("Título:", 1)[-1].strip() if "Título:" in m.group("rest") else rec.get("title", "")
            rec.update({"vid": vid, "channel": m.group("ch"), "day": m.group("day"), "file": m.group("file"), "title": title})
            if "ia" not in rec:
                day = m.group("day")
                if day not in manifests:
                    try:
                        manifests[day] = json.loads(urllib.request.urlopen(f"{RAW}/{day}/manifest.json", timeout=30).read())
                    except Exception:
                        manifests[day] = []
                for clip in manifests[day]:
                    if any(o["file"] == m.group("file") for o in clip["outputs"]):
                        ia = clip.get("ia") or {}
                        rec.update({"streamer": clip.get("login"), "twitch_views": clip.get("views"),
                                    "ia": {k: ia.get(k) for k in ("gracioso", "contexto", "angulo", "que_pasa")}})
            hist[vid] = rec
    return hist


def add_stats(hist):
    now = datetime.datetime.utcnow()
    for ch in ("daily", "diario"):
        yt = youtube(ch)
        # tambien cuenta los Shorts subidos a mano (por ejemplo el de "que pasa si le das tu arma al cajero")
        try:
            up = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            token = None
            for _ in range(4):
                pl = yt.playlistItems().list(part="contentDetails", playlistId=up, maxResults=50, pageToken=token).execute()
                for it in pl.get("items", []):
                    vid = it["contentDetails"]["videoId"]
                    hist.setdefault(vid, {"vid": vid, "channel": ch, "manual": True})
                token = pl.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            print("no se pudo listar el canal", ch, e)
        ids = [v for v, r in hist.items() if r["channel"] == ch]
        if not ids:
            continue
        for i in range(0, len(ids), 50):
            res = yt.videos().list(part="statistics,snippet", id=",".join(ids[i:i + 50])).execute()
            for item in res.get("items", []):
                s, sn = item["statistics"], item["snippet"]
                pub = datetime.datetime.strptime(sn["publishedAt"][:19], "%Y-%m-%dT%H:%M:%S")
                hist[item["id"]].update({"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                                         "comments": int(s.get("commentCount", 0)),
                                         "hours": round((now - pub).total_seconds() / 3600, 1), "title": sn["title"]})


PROMPT = """Eres el analista de dos canales de YouTube Shorts de momentos de GTA 6 (Sidekick Daily en ingles, Sidekick Diario en espanol).
Estos son todos los Shorts publicados con sus resultados reales (horas = tiempo desde que se publico):
{tabla}

Reglas que la fabrica usa hoy:
{receta}

Analiza que patrones (tipo de titulo, angulo del clip, streamer, idioma, notas de la IA) se relacionan con mas vistas por hora.
Se concreto y no inventes: si hay pocos datos, dilo y propon una prueba (por ejemplo "probar 2 clips de curiosidad del juego por canal").
Responde SOLO un JSON:
{{
 "lecciones": ["max 10 reglas cortas y accionables para elegir clips y escribir titulos"],
 "evitar": ["max 6 cosas que no funcionan"],
 "peso_streamer": {{"login_de_twitch": numero entre 0.5 y 1.5 (1 = normal)}},
 "pruebas": ["max 3 experimentos para la proxima semana"],
 "resumen": "2 frases en espanol simple para Stephano: que funciono y que cambia"
}}"""


def ask_ai(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", method="POST",
            data=json.dumps({"model": os.environ.get("CLAUDE_MODEL_ANALISIS", "claude-sonnet-5-5"), "max_tokens": 2000,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return "".join(b.get("text", "") for b in r.get("content", []))
    gkey = os.environ.get("GEMINI_API_KEY", "").strip()
    if gkey:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=gkey)
        r = client.models.generate_content(model=os.environ.get("GEMINI_MODELS", "gemini-3.8-flash").split(",")[0],
                                           contents=prompt,
                                           config=types.GenerateContentConfig(response_mime_type="application/json"))
        return r.text
    raise RuntimeError("no hay clave de IA")


def main():
    hist = collect()
    add_stats(hist)
    HIST.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
    rows = sorted(hist.values(), key=lambda r: -(r.get("views", 0) / max(r.get("hours", 1), 1)))
    tabla = "\n".join(
        f"- [{r['channel']}] vistas={r.get('views', '?')} horas={r.get('hours', '?')} likes={r.get('likes', '?')} "
        f"streamer={r.get('streamer', '?')} angulo={(r.get('ia') or {}).get('angulo')} gracia={(r.get('ia') or {}).get('gracioso')} "
        f"| {r.get('title', '')[:90]}" for r in rows[:120])
    print(tabla)
    if len(rows) < 5:
        print("todavia hay pocos Shorts para aprender")
        return
    old = json.loads(RECETA.read_text()) if RECETA.exists() else {}
    txt = ask_ai(PROMPT.format(tabla=tabla, receta=json.dumps(old, ensure_ascii=False)))
    i, j = txt.find("{"), txt.rfind("}")
    new = json.loads(txt[i:j + 1])
    new["actualizado"] = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    new["shorts_analizados"] = len(rows)
    RECETA.write_text(json.dumps(new, ensure_ascii=False, indent=1))
    print(json.dumps(new, ensure_ascii=False, indent=1))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
        f.write("## Lo que aprendio la fabrica\n\n" + new.get("resumen", "") + "\n\n- " + "\n- ".join(new.get("lecciones", [])) + "\n")


if __name__ == "__main__":
    main()
