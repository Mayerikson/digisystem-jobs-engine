"""Publica no LinkedIn o post agendado para hoje (API oficial, perfil pessoal)."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ARQUIVO = Path("posts.json")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"


def publicar(texto: str):
    pid = os.environ["LINKEDIN_PERSON_ID"]
    r = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {os.environ['LINKEDIN_TOKEN']}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        },
        json={
            "author": f"urn:li:person:{pid}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": texto},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
        timeout=60,
    )
    if r.status_code != 201:
        sys.exit(f"LinkedIn {r.status_code}: {r.text[:300]}  (token expirado? rode linkedin_auth.py)")


def main():
    if not ARQUIVO.exists():
        sys.exit("posts.json não existe ainda.")
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo")).date().isoformat()
    posts = json.loads(ARQUIVO.read_text(encoding="utf-8"))
    pendentes = [p for p in posts if p["data"] == hoje and not p["publicado"]]
    if not pendentes:
        print(f"Nada para publicar em {hoje} (sem post ou feriado).")
        return
    for p in pendentes:
        print(f"[{'DRY-RUN' if DRY_RUN else 'PUBLICANDO'}] {p['data']} | {p['area']}\n{p['texto']}\n")
        if not DRY_RUN:
            publicar(p["texto"])
            p["publicado"] = True
    ARQUIVO.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
