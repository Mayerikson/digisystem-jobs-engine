"""Teste integrado de disponibilidade Digisystem na Recrutei.

Consulta duas oportunidades reais previamente identificadas e valida um caso
UNKNOWN controlado. Este script NAO importa o publicador e NAO chama o LinkedIn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from vagas_pipeline import verificar_disponibilidade_vagas

TIMEZONE = ZoneInfo("America/Sao_Paulo")
ARQUIVO_RELATORIO = Path("verificacao_integrada_recrutei.json")


def id_estavel(url: str) -> str:
    return "DIGI-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16].upper()


URL_ATIVA_HISTORICA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "153542-planejamento-e-gestao-de-projetos"
)
URL_ENCERRADA_HISTORICA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "156893-analista-de-governanca-de-dados-sps0001946"
)

CASOS = [
    {
        "cenario": "vaga_ativa_recrutei",
        "expectativa": "ACTIVE",
        "vaga": {
            "ID": id_estavel(URL_ATIVA_HISTORICA),
            "Vaga": "Planejamento e Gestão de Projetos",
            "Detalhes": "Vaga anteriormente confirmada como ativa em teste real",
            "Link de Inscrição": URL_ATIVA_HISTORICA,
        },
    },
    {
        "cenario": "vaga_encerrada_recrutei",
        "expectativa": "CLOSED",
        "vaga": {
            "ID": id_estavel(URL_ENCERRADA_HISTORICA),
            "Vaga": "Analista de Governança de Dados - SPS0001946",
            "Detalhes": "Vaga anteriormente identificada como encerrada em teste real",
            "Link de Inscrição": URL_ENCERRADA_HISTORICA,
        },
    },
    {
        "cenario": "unknown_controlado_url_fora_do_portal",
        "expectativa": "UNKNOWN",
        "vaga": {
            "ID": "TEST-UNKNOWN-CONTROLADO",
            "Vaga": "Cenário controlado de URL não permitida",
            "Detalhes": "Não acessa domínio externo; deve ser classificado como UNKNOWN",
            "Link de Inscrição": "https://example.com/not-a-digisystem-vacancy",
        },
    },
]


async def executar() -> int:
    inicio = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    vagas = [caso["vaga"] for caso in CASOS]
    resultados = await verificar_disponibilidade_vagas(vagas)
    por_id = {str(item.get("ID")): item for item in resultados}

    verificacoes = []
    falhas = []
    for caso in CASOS:
        vaga = caso["vaga"]
        resultado = por_id.get(vaga["ID"], {})
        obtido = str(resultado.get("status_disponibilidade", "MISSING")).upper()
        esperado = caso["expectativa"]
        aprovado = obtido == esperado
        item = {
            "cenario": caso["cenario"],
            "vaga_id": vaga["ID"],
            "titulo": vaga["Vaga"],
            "url": vaga["Link de Inscrição"],
            "esperado": esperado,
            "obtido": obtido,
            "aprovado": aprovado,
            "motivo": resultado.get("motivo_verificacao", "Resultado ausente"),
            "http_vaga": resultado.get("http_status"),
            "http_candidatura": resultado.get("application_http_status"),
            "url_final_vaga": resultado.get("page_final_url", ""),
            "url_final_candidatura": resultado.get("application_final_url", ""),
            "last_verified_at": resultado.get("last_verified_at", ""),
        }
        verificacoes.append(item)
        print(
            f"{'OK' if aprovado else 'FALHA'} | {caso['cenario']} | "
            f"esperado={esperado} | obtido={obtido} | motivo={item['motivo']}"
        )
        if not aprovado:
            falhas.append(item)

    fim = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    relatorio = {
        "inicio": inicio,
        "fim": fim,
        "status": "SUCCESS" if not falhas else "FAILED",
        "linkedin_publication_attempted": False,
        "cases_total": len(CASOS),
        "cases_passed": len(verificacoes) - len(falhas),
        "cases_failed": len(falhas),
        "observacao_unknown": (
            "O caso UNKNOWN e uma simulacao controlada: a URL fora do portal e rejeitada "
            "pela validacao local antes de qualquer navegacao externa."
        ),
        "results": verificacoes,
    }
    ARQUIVO_RELATORIO.write_text(
        json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({k: relatorio[k] for k in ("status", "cases_total", "cases_passed", "cases_failed", "linkedin_publication_attempted")}, ensure_ascii=False))
    return 0 if not falhas else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(executar()))
    except Exception as exc:
        relatorio = {
            "inicio": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            "status": "FAILED",
            "linkedin_publication_attempted": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        ARQUIVO_RELATORIO.write_text(
            json.dumps(relatorio, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"ERRO NO TESTE INTEGRADO: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
