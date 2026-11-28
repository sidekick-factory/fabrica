"""Publica en TikTok (sidekickdaily y sidekickdiario) los clips aprobados, usando la API gratis de Buffer.

Buffer gratis: 10 publicaciones en cola por cuenta a la vez (se rellena cuando salen), 25 por dia.
Este script corre cada pocas horas: mira los clips aprobados de los ultimos 2 dias y rellena la cola
de cada cuenta en horarios fijos, sin repetir videos.

Secretos: BUFFER_API_KEY (lo crea Stephano en Buffer > Settings > API).
Estado: un issue con etiqueta "estado-tiktok" guarda que videos ya se mandaron (no hace falta tocarlo).
"""
import datetime as dt
import json
import os
import re
import subprocess
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
OWNER, NAME = REPO.split("/")
PAGES = f"https://{OWNER}.github.io/{NAME}/clips"
KEY = os.environ.get("BUFFER_API_KEY", "").strip()
API = "https://api.buffer.com"

# cuenta de TikTok (nombre de usuario en Buffer) para cada canal de la fabrica
ACCOUNT = {"daily": "sidekickdaily", "diario": "sidekickdiario"}
HASHTAGS = {"daily": " #GTA #gaming #fyp", "diario": " #GTA #gaming #parati"}
# horarios de publicacion (hora UTC). 13,16,19,22,01 UTC = 10,13,16,19,22 h en Argentina
SLOTS_UTC = [13, 16, 19, 22, 1]
PER_DAY = len(SLOTS_UTC)
MAX_PENDING = 9      # Buffer gratis deja 10 en cola por cuenta; dejamos 1 de margen
MAX_AGE_DAYS = 2     # los videos de la galeria se borran a los 3 dias: solo usamos los de 2 dias

LINE = re.compile(r"^- \[[xX]\] `(?P<key>[^`|]+)\|(?P<ch>daily|diario)`(?P<rest>.*)$", re.M)
STATE_TITLE = "Estado TikTok (lo usa la fábrica, no tocar)"


def gh(*args, inp=None):
    return subprocess.run(["gh", *args, "--repo", REPO], input=inp, capture_output=True,
                          text=True, check=True).stdout


def gql(query, variables=None):
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"])[:500])
    return out["data"]


def buffer_channels():
    orgs = gql("query { account { organizations { id } } }")["account"]["organizations"]
    found = {}
    for org in orgs:
        chans = gql("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
                    "{ id service name displayName } }", {"o": org["id"]})["channels"]
        for c in chans:
            if str(c["service"]).lower() != "tiktok":
                continue
            names = {(c.get("name") or "").lower().lstrip("@"), (c.get("displayName") or "").lower().lstrip("@")}
            for ch, acc in ACCOUNT.items():
                if any(n.startswith(acc) for n in names):  # en TikTok la cuenta en ingles es sidekickdaily7
                    found[ch] = c["id"]
    return found


def load_state():
    out = gh("issue", "list", "--label", "estado-tiktok", "--state", "all", "--json", "number,body", "--limit", "1")
    items = json.loads(out or "[]")
    if not items:
        try:
            gh("label", "create", "estado-tiktok", "--color", "000000", "--description", "estado de TikTok")
        except subprocess.CalledProcessError:
            pass
        url = gh("issue", "create", "--title", STATE_TITLE, "--label", "estado-tiktok", "--body", "```json\n{}\n```")
        num = url.strip().rsplit("/", 1)[-1]
        return num, {}
    body = items[0]["body"] or ""
    m = re.search(r"```json\n(.*)\n```", body, re.S)
    return str(items[0]["number"]), (json.loads(m.group(1)) if m else {})


def save_state(num, state):
    # solo guardamos lo de los ultimos 10 dias para que no crezca
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
    state = {k: v for k, v in state.items() if v.get("due", "") >= cutoff}
    gh("issue", "edit", num, "--body-file", "-", inp="```json\n" + json.dumps(state, indent=1, ensure_ascii=False) + "\n```")


def approved_clips():
    """[(fecha, archivo_key, canal, titulo)] de las listas de los ultimos dias, del mas nuevo al mas viejo."""
    out = gh("issue", "list", "--label", "aprobacion", "--state", "all", "--json", "number,body", "--limit", "6")
    today = dt.datetime.now(dt.timezone.utc).date()
    clips = []
    for it in json.loads(out or "[]"):
        for m in LINE.finditer(it["body"] or ""):
            key, ch, rest = m.group("key"), m.group("ch"), m.group("rest")
            try:
                day = dt.date.fromisoformat(key.split("/")[0])
            except ValueError:
                continue
            if (today - day).days > MAX_AGE_DAYS or "❌" in rest:
                continue
            t = re.search(r"Título:\s*(.+)$", rest)
            if not t:
                continue
            title = re.sub(r"\s*#Shorts\b", "", t.group(1)).strip()
            clips.append((day, key, ch, title))
    clips.sort(key=lambda c: c[0], reverse=True)
    return clips


def url_ok(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def next_slots(taken, n):
    """Los proximos n horarios libres (desde dentro de 30 min)."""
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    out, day = [], now.date()
    while len(out) < n:
        for h in sorted(SLOTS_UTC):
            t = dt.datetime.combine(day, dt.time(h, 0), dt.timezone.utc)
            if t > now and t.isoformat() not in taken:
                out.append(t)
                if len(out) == n:
                    break
        day += dt.timedelta(days=1)
    return out


CREATE = """mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}"""


def main():
    if not KEY:
        print("Falta el secreto BUFFER_API_KEY: TikTok por Buffer todavia no esta activo.")
        return
    chans = buffer_channels()
    print("cuentas de TikTok en Buffer:", chans)
    num, state = load_state()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    clips = approved_clips()
    sent = 0
    for ch, chan_id in chans.items():
        mine = {k: v for k, v in state.items() if v.get("ch") == ch}
        pending = [v for v in mine.values() if v.get("due", "") > now]
        room = MAX_PENDING - len(pending)
        if room <= 0:
            print(ch, "cola llena")
            continue
        todo = [c for c in clips if c[2] == ch and c[1] not in state][:room]
        slots = next_slots({v["due"] for v in pending}, len(todo))
        for (day, key, _, title), when in zip(todo, slots):
            media = f"{PAGES}/{key}"
            if not url_ok(media):
                print("el video ya no esta en la galeria:", key)
                state[key] = {"ch": ch, "due": now, "err": "sin archivo"}
                continue
            text = (title + HASHTAGS[ch])[:2150]
            inp = {"text": text, "channelId": chan_id, "schedulingType": "automatic", "mode": "customScheduled",
                   "dueAt": when.isoformat().replace("+00:00", "Z"), "assets": {"videos": [{"url": media}]}}
            try:
                res = gql(CREATE, {"input": inp})["createPost"]
            except Exception as e:
                print("Buffer rechazo", key, e)
                break
            if res.get("__typename") != "PostActionSuccess":
                print("Buffer rechazo", key, res.get("message"))
                break
            state[key] = {"ch": ch, "due": when.isoformat(), "post": res["post"]["id"], "title": title[:80]}
            sent += 1
            print("en cola", ch, when.isoformat(), title)
    save_state(num, state)
    print("listo:", sent, "videos nuevos en la cola de TikTok")


if __name__ == "__main__":
    main()
