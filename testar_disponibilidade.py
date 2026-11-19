"""Testa a mesma verificacao de disponibilidade usada pelo publicador diario.

Nao publica no LinkedIn e nao chama o Gemini. Consulta uma amostra de vagas
atuais e inclui a oportunidade SPS0001946 como caso de encerramento conhecido.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from playwright.async_api import async_playwright

from vagas_pipeline import verificar_disponibilidade_vagas

ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_JSON = Path("verificacao_amostra.json")
ARQUIVO_RESULTADO_CSV = Path("verificacao_amostra.csv")
ARQUIVO_CAPTURA = Path("diagnostico_governanca.png")
URL_RECLAMADA = (
    "https://jobs.recrutei.com.br/digisystem/vacancy/"
    "156893-analista-de-governanca-de-dados-sps0001946"
)
VAGA_RECLAMADA = {
    "ID": "TESTE-MANUAL-SPS0001946",
    "Vaga": "Analista de Governanca de Dados (SPS0001946)",
    "Detalhes": "Caso de reclamacao no LinkedIn; teste pontual de disponibilidade",
    "Link de Inscrição": URL_RECLAMADA,
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
    return selecionadas.drop_duplicates(subset=["ID"]).head(11)


async def capturar_vaga_reclamada() -> None:
    """Salva evidencia visual; nao interage com formularios."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        try:
            page = await browser.new_page(locale="pt-BR")
            await page.goto(URL_RECLAMADA, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.wait_for_load_state("networkidle", timeout=7000)
            except Exception:
                pass
            await page.wait_for_timeout(700)
            await page.screenshot(path=str(ARQUIVO_CAPTURA), full_page=True)
            await page.close()
        finally:
            await browser.close()


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
    if not amostra["Link de Inscrição"].astype(str).eq(URL_RECLAMADA).any():
        amostra = pd.concat([amostra, pd.DataFrame([VAGA_RECLAMADA])], ignore_index=True, sort=False)
    amostra = amostra.fillna("")

    print(f"Total de vagas na coleta: {len(df)}")
    print(f"Vagas selecionadas para diagnostico: {len(amostra)}")
    print("Verificador utilizado: vagas_pipeline.verificar_disponibilidade_vagas")
    print("Caso manual esperado: SPS0001946 = CLOSED")

    resultados = await verificar_disponibilidade_vagas(amostra.to_dict(orient="records"))
    resultado_df = pd.DataFrame(resultados).fillna("")
    resultado_df.to_csv(ARQUIVO_RESULTADO_CSV, index=False, encoding="utf-8-sig")
    ARQUIVO_JSON.write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    contagens = resultado_df["status_disponibilidade"].value_counts().to_dict()
    print("RESUMO DA VERIFICACAO:")
    print(json.dumps(contagens, ensure_ascii=False))
    manual = [r for r in resultados if str(r.get("ID")) == VAGA_RECLAMADA["ID"]]
    if not manual:
        print("ERRO: o caso manual SPS0001946 nao apareceu no resultado.", file=sys.stderr)
        return 1
    item = manual[0]
    print(f"CASO SPS0001946: {item.get('status_disponibilidade')} - {item.get('motivo_verificacao')}")
    print(f"Links especificos de candidatura: {item.get('application_links', [])}")
    print(f"Links do banco de talentos: {item.get('talent_pool_links', [])}")
    if item.get("status_disponibilidade") != "CLOSED":
        print("ERRO: o caso de encerramento conhecido nao foi classificado como CLOSED.", file=sys.stderr)
        return 1

    await capturar_vaga_reclamada()
    print(f"Resultados salvos em {ARQUIVO_JSON} e {ARQUIVO_RESULTADO_CSV}.")
    print("Nenhum formulario foi preenchido ou enviado; nenhuma publicacao foi realizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
