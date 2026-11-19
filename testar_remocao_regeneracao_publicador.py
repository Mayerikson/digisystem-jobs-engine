"""Teste integrado: remove vaga CLOSED, regenera texto com Gemini e valida DRY_RUN.

Consulta as duas vagas reais na Recrutei e chama a regeneracao real do Gemini.
Nunca envia publicacao ao LinkedIn: DRY_RUN e forcado e a funcao de publicacao
fica bloqueada por uma trava adicional.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Travas aplicadas antes de importar o publicador.
os.environ["DRY_RUN"] = "true"
os.environ.pop("LINKEDIN_TOKEN", None)
os.environ.pop("LINKEDIN_PERSON_ID", None)

import publish_linkedin as publisher  # noqa: E402
from vagas_pipeline import _id_estavel  # noqa: E402

TIMEZONE = ZoneInfo("America/Sao_Paulo")
RELATORIO = Path("teste_remocao_regeneracao_publicador.json")
DATA_TESTE = "2026-12-21"

URL_ATIVA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "153542-planejamento-e-gestao-de-projetos"
)
URL_ENCERRADA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "156893-analista-de-governanca-de-dados-sps0001946"
)


def agora() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def vaga(titulo: str, url: str) -> dict[str, str]:
    return {
        "ID": _id_estavel(url),
        "Vaga": titulo,
        "Detalhes": "CLT | Brasília, DF",
        "Link de Inscrição": url,
    }


def bloquear_publicacao_real(*args, **kwargs):
    raise AssertionError("BLOQUEIO DE SEGURANCA: tentativa de chamar LinkedIn durante teste.")


def main() -> int:
    inicio = agora()
    resultado: dict = {
        "started_at": inicio,
        "mode": "DRY_RUN",
        "test": "mixed_active_closed_with_gemini_regeneration",
        "linkedin_publication_attempted": False,
        "linkedin_credentials_provided": False,
        "gemini_regeneration_attempted": False,
        "status": "RUNNING",
    }

    try:
        if publisher.DRY_RUN is not True:
            raise RuntimeError("DRY_RUN nao esta true; teste interrompido por seguranca.")
        if os.getenv("LINKEDIN_TOKEN") or os.getenv("LINKEDIN_PERSON_ID"):
            raise RuntimeError("Credenciais LinkedIn nao podem estar presentes neste workflow.")
        if not os.getenv("GEMINI_API_KEY", "").strip():
            raise RuntimeError("GEMINI_API_KEY ausente; nao e possivel testar a regeneracao real.")
        if not publisher.ARQUIVO_PROMPT.exists():
            raise FileNotFoundError("prompt_pronto.txt nao encontrado na raiz do repositorio.")

        # Trava adicional: qualquer tentativa de publicar causa falha imediata.
        publisher.publicar_no_linkedin = bloquear_publicacao_real

        vaga_ativa = vaga("Planejamento e Gestão de Projetos", URL_ATIVA)
        vaga_fechada = vaga("Analista de Governança de Dados - SPS0001946", URL_ENCERRADA)
        vagas = {vaga_ativa["ID"]: vaga_ativa, vaga_fechada["ID"]: vaga_fechada}

        post = {
            "data": DATA_TESTE,
            "area": "Governança & Processos",
            "vacancy_ids": [vaga_ativa["ID"], vaga_fechada["ID"]],
            "qtd_vagas": 2,
            "texto": (
                "A Digisystem abriu oportunidades para profissionais de Governança & Processos.\n"
                f"{vaga_ativa['Vaga']} → {vaga_ativa['Link de Inscrição']}\n"
                f"{vaga_fechada['Vaga']} → {vaga_fechada['Link de Inscrição']}\n"
                "#Vagas #Oportunidade #Emprego #Digier"
            ),
            "publicado": False,
            "status": "PENDING",
        }

        regeneracoes = {"count": 0}
        regenerar_original = publisher.regenerar_texto_post

        def contar_regeneracao(post_arg, vagas_ativas_arg):
            regeneracoes["count"] += 1
            return regenerar_original(post_arg, vagas_ativas_arg)

        publisher.regenerar_texto_post = contar_regeneracao

        acao = publisher.processar_post(post, vagas)

        verificacoes = post.get("verification", {})
        status_ativa = verificacoes.get(vaga_ativa["ID"], {}).get("status")
        status_fechada = verificacoes.get(vaga_fechada["ID"], {}).get("status")
        texto_final = str(post.get("texto", ""))
        links_finais = publisher.links_no_texto(texto_final)

        checks = {
            "vaga_ativa_confirmada": status_ativa == "ACTIVE",
            "vaga_encerrada_confirmada": status_fechada == "CLOSED",
            "regeneracao_gemini_executada": regeneracoes["count"] == 1,
            "somente_id_ativo_preservado": post.get("vacancy_ids") == [vaga_ativa["ID"]],
            "id_encerrado_removido": vaga_fechada["ID"] in post.get("removed_vacancy_ids", []),
            "link_ativo_preservado": publisher.normalizar_link(vaga_ativa["Link de Inscrição"]) in links_finais,
            "link_encerrado_removido": publisher.normalizar_link(vaga_fechada["Link de Inscrição"]) not in links_finais,
            "texto_dentro_limite": 0 < len(texto_final) <= publisher.LIMITE_POST,
            "resultado_dry_run": acao == "dry_run_validated" and post.get("status") == "DRY_RUN_VALIDATED",
            "nao_publicado": post.get("publicado") is False,
        }
        resultado.update({
            "finished_at": agora(),
            "status": "SUCCESS" if all(checks.values()) else "FAILED",
            "gemini_regeneration_attempted": regeneracoes["count"] > 0,
            "action_result": acao,
            "post_status": post.get("status"),
            "area": post.get("area"),
            "verification": verificacoes,
            "original_vacancy_ids": [vaga_ativa["ID"], vaga_fechada["ID"]],
            "final_vacancy_ids": post.get("vacancy_ids", []),
            "removed_vacancy_ids": post.get("removed_vacancy_ids", []),
            "final_text": texto_final,
            "final_text_length": len(texto_final),
            "final_links": links_finais,
            "checks": checks,
            "linkedin_publication_attempted": False,
            "publicado": post.get("publicado", False),
        })
        return 0 if all(checks.values()) else 1

    except Exception as exc:
        resultado.update({
            "finished_at": agora(),
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return_code = 1
    else:
        return_code = 0
    finally:
        RELATORIO.write_text(json.dumps(resultado, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(resultado, ensure_ascii=False, indent=2))

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
