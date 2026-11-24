"""Convierte el codigo de autorizacion de Google en un permiso permanente para un canal
y lo guarda cifrado en tokens/<canal>.enc (solo se puede descifrar con YT_CLIENT_SECRET)."""
import base64
import hashlib
import json
import os
import pathlib
import urllib.parse
import urllib.request

from cryptography.fernet import Fernet

REDIRECT = "https://developers.google.com/oauthplayground"


def key():
    return base64.urlsafe_b64encode(hashlib.sha256((os.environ["YT_CLIENT_SECRET"] + "|sidekick").encode()).digest())


def main():
    canal = os.environ["CANAL"].strip().lower()
    assert canal in ("daily", "diario"), "canal debe ser daily o diario"
    code = os.environ["CODIGO"].strip()
    if "code=" in code:  # pegaron la direccion completa
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
    data = urllib.parse.urlencode({"code": code, "client_id": os.environ["YT_CLIENT_ID"].strip(),
                                   "client_secret": os.environ["YT_CLIENT_SECRET"].strip(),
                                   "redirect_uri": REDIRECT, "grant_type": "authorization_code"}).encode()
    tok = json.loads(urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data).read())
    refresh = tok.get("refresh_token")
    assert refresh, "Google no devolvio permiso permanente; repite la autorizacion"
    req = urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                 headers={"Authorization": f"Bearer {tok['access_token']}"})
    ch = json.loads(urllib.request.urlopen(req).read())["items"][0]["snippet"]["title"]
    pathlib.Path("tokens").mkdir(exist_ok=True)
    pathlib.Path(f"tokens/{canal}.enc").write_bytes(Fernet(key()).encrypt(refresh.encode()))
    msg = f"Canal '{canal}' conectado a: **{ch}**"
    print(msg)
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f:
        f.write(msg + "\n")
    expected = {"daily": "Sidekick Daily", "diario": "Sidekick Diario"}[canal]
    assert ch.strip().lower() == expected.lower(), f"OJO: autorizaste '{ch}' pero esperaba '{expected}'"


if __name__ == "__main__":
    main()
