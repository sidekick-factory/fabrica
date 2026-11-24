"""Revisor: vuelve a revisar con la IA una lista de aprobacion ya creada.
Mejora los titulos (que digan que pasa) y marca con ⚠️ los clips que no conviene publicar
(desnudos, temas sexuales, poco graciosos o sin contexto). No publica nada."""
import json
import os
import re
import subprocess
import sys
import urllib.request

sys.argv = [sys.argv[0], "site"]
sys.path.insert(0, os.path.dirname(__file__))
import make_clips as m  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "sidekick-factory/fabrica")
NUM = os.environ["ISSUE_NUMBER"]
RAW = f"https://raw.githubusercontent.com/{REPO}/gh-pages/clips"
LINE = re.compile(r"^- \[ \] `(?P<day>[^/`]+)/(?P<file>[^`|]+)\|(?P<ch>daily|diario)`(?P<rest>.*)$", re.M)


def gh(*args):
    return subprocess.run(["gh", *args, "--repo", REPO], capture_output=True, text=True, check=True).stdout


def main():
    body = json.loads(gh("issue", "view", NUM, "--json", "body"))["body"]
    streamers = {s["login"]: s for s in m.CFG["streamers"]}
    manifests, verdicts = {}, {}
    m.WORK.mkdir(exist_ok=True)
    for ln in LINE.finditer(body):
        day, fname = ln.group("day"), ln.group("file")
        if "⚠️" in ln.group("rest") or "🤖" in ln.group("rest"):
            continue
        if day not in manifests:
            manifests[day] = json.loads(urllib.request.urlopen(f"{RAW}/{day}/manifest.json", timeout=30).read())
        clip = next((c for c in manifests[day] if any(o["file"] == fname for o in c["outputs"])), None)
        if not clip or clip["id"] in verdicts:
            continue
        st = streamers.get(clip.get("login")) or {"name": clip.get("streamer"), "login": clip.get("login"), "hashtags": []}
        path = m.WORK / fname
        urllib.request.urlretrieve(f"{RAW}/{day}/{fname}", path)
        text = (clip.get("streamer_text") or "") + " " + (clip.get("game_text") or "")
        words = [{"start": i * 0.4, "end": i * 0.4 + 0.3, "word": w} for i, w in enumerate(text.split()[:300])]
        j = m.ai_judge(path, st, {"title": clip.get("title"), "views": clip.get("views", 0)}, words)
        print("IA:", clip["id"], json.dumps(j, ensure_ascii=False)[:300], flush=True)
        verdicts[clip["id"]] = (j, st)

    def fix(match):
        day, fname, rest = match.group("day"), match.group("file"), match.group("rest")
        clip = next((c for c in manifests.get(day, []) if any(o["file"] == fname for o in c["outputs"])), None)
        if not clip or clip["id"] not in verdicts or not verdicts[clip["id"]][0]:
            return match.group(0)
        j, st = verdicts[clip["id"]]
        out = next(o for o in clip["outputs"] if o["file"] == fname)
        new_title = m.clean_title(j.get(f"titulo_{out['lang']}"), st, out["lang"])
        if new_title and "Título:" in rest:
            rest = rest.split("Título:", 1)[0] + "Título: " + new_title
        bad = []
        if j.get("desnudos") == "explicito" or j.get("apto_monetizar") is False:
            bad.append("no monetizable")
        if (j.get("gracioso") or 0) < 5:
            bad.append("poco gracioso")
        if (j.get("contexto") or 0) < 4:
            bad.append("sin contexto")
        tag = f" ⚠️ no recomendado: {', '.join(bad)} ·" if bad else f" 🤖 gracia {j.get('gracioso')}/10 ·"
        return f"- [ ] `{day}/{fname}|{match.group('ch')}`{tag}{rest}"

    new_body = LINE.sub(fix, body)
    if new_body != body:
        path = m.WORK / "issue.md"
        path.write_text(new_body, encoding="utf-8")
        gh("issue", "edit", NUM, "--body-file", str(path))
        gh("issue", "comment", NUM, "--body", f"🤖 Lista revisada con IA: {len(verdicts)} clips evaluados. "
           "Títulos mejorados; los marcados ⚠️ no conviene publicarlos.")
    print("revisados:", len(verdicts))


if __name__ == "__main__":
    main()
