"""Testa disponibilidade de vagas atuais e de uma vaga informada manualmente.

Nao publica no LinkedIn e nao chama o Gemini.
Inclui sempre a vaga de Governanca de Dados SPS0001946 para investigar
uma reclamacao recebida no LinkedIn, mesmo que ela nao apareca na listagem atual.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pandas as pd

from vagas_pipeline import verificar_disponibilidade_vagas

ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_JSON = Path("verificacao_amostra.json")
ARQUIVO_RESULTADO_CSV = Path("verificacao_amostra.csv")

# Caso real informado pelo usuario. Incluido mesmo se nao estiver na listagem atual.
VAGA_RECLAMADA = {
    "ID": "TESTE-MANUAL-SPS0001946",
    "Vaga": "Analista de Governanca de Dados (SPS0001946)",
    "Detalhes": "Caso de reclamacao no LinkedIn; teste pontual de disponibilidade",
    "Link de Inscrição": (
        "https://jobs.recrutei.com.br/digisystem/vacancy/"
        "156893-analista-de-governanca-de-dados-sps0001946"
    ),
    "Tipo de teste": "Vaga reclamada - verificacao manual",
}


def selecionar_amostra(df: pd.DataFrame) -> pd.DataFrame:
    dados_governanca = df["Vaga"].astype(str).str.contains(
        r"dados|governan", case=False, regex=True, na=False
    )
    selecionadas = df.loc[dados_governanca].copy()
    ids_selecionados = set(selecionadas["ID"].astype(str))

    grupos = [
        r"suporte|atendimento",
        r"desenvolvedor|desenvolvimento|programador",
        r"infraestrutura|devops|nuvem",
        r"tasy|saude",
        r"lider|projetos|analista de sistemas",
    ]
    for padrao in grupos:
        candidatas = df[
            df["Vaga"].astype(str).str.contains(
                padrao, case=False, regex=True, na=False
            )
            & ~df["ID"].astype(str).isin(ids_selecionados)
        ]
        if not candidatas.empty:
            linha = candidatas.iloc[[0]]
            selecionadas = pd.concat([selecionadas, linha], ignore_index=True)
            ids_selecionados.add(str(linha.iloc[0]["ID"]))

    selecionadas = selecionadas.drop_duplicates(subset=["ID"])
    return selecionadas.head(12)


async def main() -> int:
    if not ARQUIVO_CSV.exists():
        print(f"ERRO: arquivo nao encontrado: {ARQUIVO_CSV}", file=sys.stderr)
        return 1

    df = pd.read_csv(ARQUIVO_CSV, encoding="utf-8-sig", dtype=str).fillna("")
    obrigatorias = {"ID", "Vaga", "Detalhes", "Link de Inscrição"}
    faltantes = obrigatorias - set(df.columns)
    if faltantes:
        print(f"ERRO: colunas ausentes: {sorted(faltantes)}", file=sys.stderr)
        return 1

    amostra = selecionar_amostra(df)
    if amostra.empty:
        print("ERRO: nenhuma vaga selecionada para verificacao.", file=sys.stderr)
        return 1

    # Acrescenta o caso reclamado sem depender de ele continuar na listagem.
    link_manual = VAGA_RECLAMADA["Link de Inscrição"]
    if not amostra["Link de Inscrição"].astype(str).eq(link_manual).any():
        amostra = pd.concat(
            [amostra, pd.DataFrame([VAGA_RECLAMADA])], ignore_index=True, sort=False
        )

    print(f"Total de vagas na coleta: {len(df)}")
    print(f"Vagas selecionadas para verificacao: {len(amostra)}")
    print("CASO MANUAL: Analista de Governanca de Dados (SPS0001946)")
    print(f"URL: {link_manual}")

    for _, vaga in amostra.iterrows():
        print(f"AMOSTRA: {vaga['ID']} | {vaga['Vaga']} | {vaga['Link de Inscrição']}")

    resultados = await verificar_disponibilidade_vagas(
        amostra.to_dict(orient="records")
    )
    resultado_df = pd.DataFrame(resultados)
    resultado_df.to_csv(ARQUIVO_RESULTADO_CSV, index=False, encoding="utf-8-sig")
    ARQUIVO_JSON.write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    contagens = resultado_df["status_disponibilidade"].value_counts().to_dict()
    print("RESUMO DA VERIFICACAO:")
    print(json.dumps(contagens, ensure_ascii=False))
    manual = resultado_df[resultado_df["ID"] == VAGA_RECLAMADA["ID"]]
    if not manual.empty:
        registro = manual.iloc[0]
        print(
            "RESULTADO CASO MANUAL: "
            f"{registro['status_disponibilidade']} — {registro['motivo_verificacao']}"
        )
    print(f"Resultados salvos em {ARQUIVO_JSON} e {ARQUIVO_RESULTADO_CSV}.")
    print("ATENCAO: UNKNOWN nao significa vaga aberta; exige nova verificacao.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
