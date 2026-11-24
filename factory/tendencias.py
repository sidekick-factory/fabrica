"""Busca los Shorts de GTA 6 que se estan volviendo virales HOY y saca la IDEA de cada uno
(no el video): tipo de momento, gancho y estilo de titulo. La fabrica usa esas ideas para
buscar momentos parecidos en los clips de Twitch y hacer su "version propia"."""
import json
import os
import pathlib
import subprocess
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "tendencias.json"
# busquedas de Shorts (sp=EgIYAQ = filtro "Shorts"; CAIS... = ultimas 24 h / esta semana)
QUERIES = ["GTA 6", "GTA 6 que pasa si", "GTA 6 funny", "GTA 6 secret", "GTA VI streamer", "GTA 6 gracioso"]


def search(q, n=25):
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q) + "&sp=EgQIAxgB"
    r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", str(n), url],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        print("busqueda fallo", q, r.stderr[-300:])
        return []
    out = []
    for e in json.loads(r.stdout).get("entries", []):
        if e.get("title"):
            out.append({"title": e["title"], "views": e.get("view_count") or 0, "channel": e.get("channel") or e.get("uploader"),
                        "id": e.get("id")})
    return out


PROMPT = """Estos son Shorts de GTA 6 que estan circulando esta semana (titulo, vistas, canal):
{lista}

Tu tarea: detectar las IDEAS que se repiten en los mas vistos (no copiar videos). Para cada idea:
el tipo de momento (ej. "mecanica curiosa: dar el arma al cajero", "streamer se asusta", "fail con autos"),
el gancho del titulo y palabras clave para encontrar momentos parecidos en clips de streamers de Twitch.
Responde SOLO un JSON:
{{"ideas": [
  {{"idea": "descripcion corta del tipo de momento",
    "por_que_funciona": "una frase",
    "buscar": ["palabras clave en ingles y espanol que aparecerian en el titulo o en lo que se dice en el clip"],
    "titulo_modelo_en": "plantilla de titulo en ingles con el mismo gancho",
    "titulo_modelo_es": "plantilla de titulo en espanol con el mismo gancho"}}
 ]}}
Maximo 8 ideas, ordenadas de mas a menos prometedora. Nada de contenido sexual."""


def ask(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", method="POST",
            data=json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"), "max_tokens": 2000,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return "".join(b.get("text", "") for b in r.get("content", []))
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
    return client.models.generate_content(model="gemini-3.8-flash", contents=prompt,
                                          config=types.GenerateContentConfig(response_mime_type="application/json")).text


def main():
    seen, rows = set(), []
    for q in QUERIES:
        for v in search(q):
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            rows.append(v)
    rows.sort(key=lambda v: -v["views"])
    print(len(rows), "shorts encontrados")
    if len(rows) < 5:
        return
    lista = "\n".join(f"- {v['title'][:100]} | {v['views']} vistas | {v['channel']}" for v in rows[:80])
    txt = ask(PROMPT.format(lista=lista))
    i, j = txt.find("{"), txt.rfind("}")
    data = json.loads(txt[i:j + 1])
    data["top"] = rows[:15]
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(json.dumps(data["ideas"], ensure_ascii=False, indent=1))
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
        f.write("## Ideas en tendencia hoy\n\n" + "\n".join(f"- **{d['idea']}**: {d.get('titulo_modelo_es', '')}" for d in data["ideas"]) + "\n")


if __name__ == "__main__":
    import urllib.parse  # noqa
    main()
