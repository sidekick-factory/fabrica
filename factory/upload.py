"""Sube a YouTube los clips que Stephano marco en la lista de aprobacion (issue de GitHub)."""
import json
import os
import re
import subprocess
import tempfile
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
NUM = os.environ["ISSUE_NUMBER"]
RAW = f"https://raw.githubusercontent.com/{REPO}/gh-pages/clips"
LINE = re.compile(r"^- \[[xX]\] `(?P<key>[^`|]+)\|(?P<ch>daily|diario)`(?P<rest>.*)$", re.M)
TODO = re.compile(r"^- \[[xX]\] `todo`", re.M)


def gh(*args, inp=None):
    return subprocess.run(["gh", *args, "--repo", REPO], input=inp, capture_output=True, text=True, check=True).stdout


def refresh_token(channel):
    env = os.environ.get(f"YT_REFRESH_{channel.upper()}")
    if env:
        return env
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    k = base64.urlsafe_b64encode(hashlib.sha256((os.environ["YT_CLIENT_SECRET"] + "|sidekick").encode()).digest())
    with open(f"tokens/{channel}.enc", "rb") as f:
        return Fernet(k).decrypt(f.read()).decode()


def youtube(channel):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(None, refresh_token=refresh_token(channel),
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"].strip(), client_secret=os.environ["YT_CLIENT_SECRET"].strip(),
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def campaigns():
    """login de Twitch -> nombre de la campana de Content Rewards (factory/streamers.json)."""
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(__file__), "streamers.json")))
        return {s["login"]: s["campaign"] for s in cfg["streamers"] if s.get("campaign")}
    except Exception as e:
        print("no se pudo leer streamers.json", e)
        return {}


def upload(channel, path, title, desc, privacy="public"):
    from googleapiclient.http import MediaFileUpload
    tags = [t.lstrip("#") for t in re.findall(r"#\w+", title + " " + desc)][:15]
    body = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": tags, "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    req = youtube(channel).videos().insert(part="snippet,status", body=body,
                                           media_body=MediaFileUpload(path, chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


RETRY = re.compile(r"^\s*(⏳[^·]*|❌ error:[^·]*(exceeded|quota|uploadLimit)[^·]*)·\s?")


def main():
    body = json.loads(gh("issue", "view", NUM, "--json", "body"))["body"]
    all_ok = bool(TODO.search(body))
    if all_ok:
        body = re.sub(r"^- \[ \] `(?!todo)", "- [x] `", body, flags=re.M)
    manifests, results = {}, []
    camp = campaigns()
    for m in list(LINE.finditer(body)):
        orig_rest = rest = m.group("rest")
        if RETRY.match(rest):  # quedo pendiente por el tope diario de YouTube: se reintenta
            rest = " " + RETRY.sub("", rest, count=1)
        elif "✅" in rest or "❌" in rest or "🔒" in rest:
            continue
        key, ch = m.group("key"), m.group("ch")
        day, fname = key.split("/", 1)
        if day not in manifests:
            manifests[day] = json.loads(urllib.request.urlopen(f"{RAW}/{day}/manifest.json").read())
        clip = next((it for it in manifests[day] if any(o["file"] == fname for o in it["outputs"])), None)
        out = next((o for o in (clip or {}).get("outputs", []) if o["file"] == fname), None)
        if not out:
            continue
        campaign = camp.get(clip.get("login"))
        title = rest.split("Título:", 1)[1].strip() if "Título:" in rest else out["title"]
        try:
            tmp = os.path.join(tempfile.gettempdir(), fname)
            urllib.request.urlretrieve(f"{RAW}/{key}", tmp)
            # Content Rewards solo acepta clips enviados en los 30 min despues de publicarse:
            # los de campana se suben en privado y se publican + envian juntos a la hora fija.
            vid = upload(ch, tmp, title, out["desc"], privacy="private" if campaign else "public")
            if campaign:
                mark = f"🔒 https://youtube.com/shorts/{vid} · privado, se publica y envia a la campana \"{campaign}\" a la hora fija"
            else:
                mark = f"✅ https://youtube.com/shorts/{vid}"
            results.append(f"- {mark} · {title}")
        except Exception as e:
            err = str(e)
            if re.search(r"exceeded|quota|uploadLimit", err):
                mark = "⏳ pendiente: tope diario de YouTube, se reintenta solo mañana"
            else:
                mark = f"❌ error: {err[:150]}"
            results.append(f"- {mark} · {fname}")
        body = body.replace(m.group(0), m.group(0).replace("`" + orig_rest, "`" + f" {mark} ·" + rest, 1), 1)
        if all_ok is False:
            pass
    if results:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body)
        gh("issue", "edit", NUM, "--body-file", f.name)
        gh("issue", "comment", NUM, "--body", "Subidos:\n" + "\n".join(results))
    print("\n".join(results) or "nada nuevo que subir")


if __name__ == "__main__":
    main()
