"""Teste integrado do decisor do publicador com consultas reais à Recrutei.

Não chama a API do LinkedIn. Exige DRY_RUN=true e não carrega credenciais LinkedIn.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Trava local: este teste nunca pode executar publicação real.
os.environ["DRY_RUN"] = "true"

import publish_linkedin as publisher  # noqa: E402
from vagas_pipeline import _id_estavel  # noqa: E402

TIMEZONE = ZoneInfo("America/Sao_Paulo")
ARQUIVO_RELATORIO = Path("teste_publicador_integrado_recrutei.json")
DATA_TESTE = "2026-12-21"

URL_ATIVA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "153542-planejamento-e-gestao-de-projetos"
)
URL_ENCERRADA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "156893-analista-de-governanca-de-dados-sps0001946"
)
URL_UNKNOWN = "https://example.com/not-a-digisystem-vacancy"


def criar_vaga(titulo: str, url: str) -> dict[str, str]:
    return {
        "ID": _id_estavel(url),
        "Vaga": titulo,
        "Detalhes": "Teste controlado de disponibilidade",
        "Link de Inscrição": url,
    }


def criar_post(area: str, vaga: dict[str, str]) -> dict:
    return {
        "data": DATA_TESTE,
        "area": area,
        "vacancy_ids": [vaga["ID"]],
        "qtd_vagas": 1,
        "texto": (
            f"A Digisystem abriu oportunidade para {vaga['Vaga']}.\n"
            f"{vaga['Vaga']} → {vaga['Link de Inscrição']}\n"
            "#Vagas #Oportunidade #Emprego #Digier"
        ),
        "publicado": False,
        "status": "PENDING",
    }


def executar_caso(nome: str, esperado: str, vaga: dict[str, str]) -> dict:
    post = criar_post(f"Teste integrado - {nome}", vaga)
    try:
        resultado = publisher.processar_post(post, {vaga["ID"]: vaga})
        erro = None
    except publisher.PublicacaoError as exc:
        resultado = "blocked"
        erro = str(exc)

    status = str(post.get("status", ""))
    aprovado = status == esperado
    if nome == "vaga_ativa_recrutei":
        aprovado = aprovado and resultado == "dry_run_validated" and post.get("publicado") is False
    elif nome == "vaga_encerrada_recrutei":
        aprovado = aprovado and resultado == "skipped_all_closed" and post.get("publicado") is False
    elif nome == "unknown_controlado":
        aprovado = aprovado and resultado == "blocked" and post.get("publicado") is False

    return {
        "cenario": nome,
        "vaga_id": vaga["ID"],
        "titulo": vaga["Vaga"],
        "esperado": esperado,
        "status_publicador": status,
        "resultado_processar_post": resultado,
        "aprovado": bool(aprovado),
        "erro": erro,
        "verification": post.get("verification", {}),
        "publicado": post.get("publicado", False),
    }


def main() -> int:
    inicio = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    if publisher.DRY_RUN is not True:
        raise RuntimeError("Trava de seguranca: publisher.DRY_RUN precisa ser true.")
    if os.getenv("LINKEDIN_TOKEN") or os.getenv("LINKEDIN_PERSON_ID"):
        raise RuntimeError("Este workflow nao deve receber credenciais do LinkedIn.")

    vaga_ativa = criar_vaga("Planejamento e Gestão de Projetos", URL_ATIVA)
    vaga_encerrada = criar_vaga("Analista de Governança de Dados - SPS0001946", URL_ENCERRADA)
    vaga_unknown = criar_vaga("Cenário controlado UNKNOWN", URL_UNKNOWN)

    casos = [
        executar_caso("vaga_ativa_recrutei", "DRY_RUN_VALIDATED", vaga_ativa),
        executar_caso("vaga_encerrada_recrutei", "SKIPPED_ALL_CLOSED", vaga_encerrada),
        executar_caso("unknown_controlado", "BLOCKED_UNVERIFIED", vaga_unknown),
    ]

    aprovados = sum(1 for caso in casos if caso["aprovado"])
    relatorio = {
        "inicio": inicio,
        "fim": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "status": "SUCCESS" if aprovados == len(casos) else "FAILED",
        "mode": "DRY_RUN",
        "linkedin_publication_attempted": False,
        "linkedin_credentials_provided": False,
        "cases_total": len(casos),
        "cases_passed": aprovados,
        "cases_failed": len(casos) - aprovados,
        "observacao_unknown": (
            "UNKNOWN e um caso controlado: URL fora do portal e rejeitada pela validacao local; "
            "nenhuma navegacao externa ocorre nesse caso."
        ),
        "results": casos,
    }
    ARQUIVO_RELATORIO.write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(relatorio, ensure_ascii=False, indent=2))
    return 0 if aprovados == len(casos) else 1


if __name__ == "__main__":
    raise SystemExit(main())
