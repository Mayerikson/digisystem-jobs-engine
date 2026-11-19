"""Lê prompt_pronto.txt, gera os posts com a API gratuita do Gemini e salva posts.json."""
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from vagas_pipeline import gerar_calendario

MODELO = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
CHAVE = os.environ["GEMINI_API_KEY"]
PORTAL = "https://jobs.recrutei.com.br/digisystem"
LIMITE = 2900  # LinkedIn aceita 3000 caracteres


def chamar_gemini(prompt: str) -> list:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    corpo = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    for tentativa in range(4):
        r = requests.post(url, params={"key": CHAVE}, json=corpo, timeout=180)
        if r.status_code == 200:
            texto = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(texto)
        print(f"Gemini {r.status_code}: {r.text[:200]} (tentativa {tentativa + 1})")
        time.sleep(30 * (tentativa + 1))  # respeita o limite do free tier
    sys.exit("Falha ao gerar posts com o Gemini.")


def validar(posts: list, datas_ok: set, links_ok: set):
    vistos = set()
    for p in posts:
        assert p["data"] in datas_ok, f"Data fora do calendário: {p['data']}"
        assert p["data"] not in vistos, f"Data repetida: {p['data']}"
        vistos.add(p["data"])
        assert len(p["texto"]) <= LIMITE, f"Post muito longo em {p['data']}"
        for link in re.findall(r"https?://[^\s)]+", p["texto"]):
            assert link.rstrip("/.,") in links_ok, f"Link inventado: {link}"


def main():
    uteis, _, _, _ = gerar_calendario(date.today())
    datas = [d.isoformat() for d in uteis]

    extra = (
        "\n\n=== SOBRESCREVE O ITEM 8 (ENTREGA) ===\n"
        "NÃO gere HTML nem widget. Responda SOMENTE com um array JSON. Cada item: "
        '{"data":"YYYY-MM-DD","area":"...","qtd_vagas":0,"texto":"..."}.\n'
        f"Datas permitidas (use só estas, no máximo 1 post por data, em ordem): {datas}\n"
        f"O campo texto é o post final em texto puro, sem markdown, com no máximo {LIMITE} caracteres. "
        "Use SOMENTE links que existam na tabela de vagas ou o portal geral "
        f"{PORTAL}. Nunca invente vagas ou links."
    )
    prompt = Path("prompt_pronto.txt").read_text(encoding="utf-8") + extra

    links_ok = set(pd.read_csv("vagas_digisystem.csv")["Link de Inscrição"]) | {PORTAL}
    links_ok = {l.rstrip("/") for l in links_ok}

    for tentativa in range(3):
        try:
            posts = chamar_gemini(prompt)
            validar(posts, set(datas), links_ok)
            break
        except (AssertionError, KeyError, json.JSONDecodeError) as e:
            print(f"Validação falhou: {e} (tentativa {tentativa + 1})")
    else:
        sys.exit("Não foi possível gerar posts válidos.")

    for p in posts:
        p["publicado"] = False
    Path("posts.json").write_text(
        json.dumps(sorted(posts, key=lambda p: p["data"]), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"{len(posts)} posts gerados em posts.json")


if __name__ == "__main__":
    main()
