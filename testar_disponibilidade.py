"""Diagnostica a disponibilidade de uma amostra de vagas da Digisystem.

Nao publica no LinkedIn e nao chama o Gemini. Inspeciona a pagina da vaga,
localiza links oficiais de candidatura da Recrutei e, quando encontrados,
abre a pagina de candidatura sem preencher nem enviar formularios.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_JSON = Path("verificacao_amostra.json")
ARQUIVO_RESULTADO_CSV = Path("verificacao_amostra.csv")

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

HOST_VAGA = "jobs.recrutei.com.br"
HOST_CANDIDATURA = "talent.recrutei.com.br"
TERMOS_ENCERRAMENTO = (
    "parece que essa vaga nao esta mais disponivel",
    "essa vaga nao esta mais disponivel",
    "esta vaga nao esta mais disponivel",
    "vaga nao esta mais disponivel",
    "vaga nao encontrada",
    "essa oportunidade nao esta mais disponivel",
    "inscricoes encerradas",
    "processo seletivo encerrado",
    "nao aceitamos mais candidaturas",
    "o prazo de inscricao expirou",
    "prazo de inscricao encerrado",
    "essa oportunidade foi encerrada",
)
TERMOS_CANDIDATURA = (
    "candidatar",
    "candidate-se",
    "inscreva-se",
    "inscricao",
    "apply",
    "signup",
    "talent.recrutei.com.br",
)

# Sinais de campos de candidatura. Valores digitados por candidatos nunca sao lidos.
SINAIS_CAMPOS = {
    "nome": ("nome", "name", "full_name", "fullname"),
    "email": ("email", "e-mail", "mail"),
    "telefone": ("telefone", "celular", "phone", "mobile"),
    "documento": ("cpf", "documento"),
    "curriculo": ("curriculo", "curriculum", "resume", "cv"),
    "arquivo": ("upload", "arquivo", "anexo", "file"),
}


def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", texto).strip().lower()


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


async def aguardar_conteudo(page) -> None:
    """Aguarda JS e requisicoes da pagina sem depender indefinidamente de networkidle."""
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(1200)


async def extrair_links_candidatura(page) -> list[dict[str, str]]:
    links = await page.locator("a[href]").evaluate_all(
        """els => els.map(a => ({
          texto: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim(),
          href: a.href || ''
        }))"""
    )
    encontrados = []
    vistos = set()
    for item in links:
        texto = str(item.get("texto", ""))
        href = str(item.get("href", ""))
        combinado = normalizar(texto + " " + href)
        if not any(termo in combinado for termo in TERMOS_CANDIDATURA):
            continue
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.hostname not in {HOST_CANDIDATURA, HOST_VAGA}:
            continue
        if href not in vistos:
            vistos.add(href)
            encontrados.append({"texto": texto[:200], "href": href})
    return encontrados


async def inspecionar_formulario(context, url: str) -> dict[str, Any]:
    page = await context.new_page()
    resultado: dict[str, Any] = {
        "application_page_url": url,
        "application_http_status": None,
        "application_final_url": "",
        "application_title": "",
        "application_form_count": 0,
        "application_field_signals": [],
        "application_body_excerpt": "",
        "application_error": "",
    }
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        resultado["application_http_status"] = response.status if response else None
        await aguardar_conteudo(page)
        resultado["application_final_url"] = page.url
        resultado["application_title"] = (await page.title())[:300]
        body = await page.locator("body").inner_text(timeout=7000)
        resultado["application_body_excerpt"] = body[:2500]
        resultado["application_form_count"] = await page.locator("form").count()

        # Coleta somente metadados dos campos; nunca lê valores digitados.
        campos = await page.locator("input, textarea, select").evaluate_all(
            """els => els.map(el => ({
              name: el.getAttribute('name') || '',
              type: el.getAttribute('type') || el.tagName.toLowerCase(),
              placeholder: el.getAttribute('placeholder') || '',
              aria: el.getAttribute('aria-label') || '',
              autocomplete: el.getAttribute('autocomplete') || ''
            }))"""
        )
        metadados = " ".join(
            " ".join(str(v) for v in campo.values()) for campo in campos
        )
        metadados_norm = normalizar(metadados)
        sinais = [
            categoria
            for categoria, termos in SINAIS_CAMPOS.items()
            if any(normalizar(termo) in metadados_norm for termo in termos)
        ]
        # Alguns formulários rotulam campos fora do input; inclui o texto visível.
        texto_norm = normalizar(body)
        for categoria, termos in SINAIS_CAMPOS.items():
            if categoria not in sinais and any(normalizar(t) in texto_norm for t in termos):
                sinais.append(categoria)
        resultado["application_field_signals"] = sorted(set(sinais))
        resultado["application_input_metadata"] = campos[:30]
    except PlaywrightTimeoutError:
        resultado["application_error"] = "Timeout ao abrir ou ler a pagina de candidatura"
    except Exception as exc:
        resultado["application_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await page.close()
    return resultado


def classificar(resultado: dict[str, Any]) -> tuple[str, str]:
    status = resultado.get("http_status")
    if status in {404, 410}:
        return "CLOSED", f"Pagina da vaga retornou HTTP {status}"
    if status in {401, 403, 429} or (isinstance(status, int) and status >= 500):
        return "UNKNOWN", f"Resposta inconclusiva da pagina da vaga: HTTP {status}"

    textos = [resultado.get("body_text", ""), resultado.get("application_body_excerpt", "")]
    for texto in textos:
        texto_norm = normalizar(str(texto))
        for termo in TERMOS_ENCERRAMENTO:
            if normalizar(termo) in texto_norm:
                return "CLOSED", f"Mensagem explicita de encerramento: {termo}"

    app_status = resultado.get("application_http_status")
    if app_status in {404, 410}:
        return "CLOSED", f"Pagina de candidatura retornou HTTP {app_status}"
    if app_status in {401, 403, 429} or (isinstance(app_status, int) and app_status >= 500):
        return "UNKNOWN", f"Pagina de candidatura inconclusiva: HTTP {app_status}"

    sinais = set(resultado.get("application_field_signals", []))
    if app_status == 200 and resultado.get("application_form_count", 0) > 0:
        if len(sinais) >= 2 and ("nome" in sinais or "email" in sinais):
            return "ACTIVE", "Pagina de candidatura acessivel com formulario e campos de candidato"

    if resultado.get("application_links"):
        return "UNKNOWN", "Link de candidatura encontrado, mas formulario nao foi confirmado"
    return "UNKNOWN", "Nao foi possivel confirmar link ou formulario de candidatura"


async def diagnosticar_vaga(context, vaga: dict[str, Any]) -> dict[str, Any]:
    resultado = dict(vaga)
    url = str(vaga.get("Link de Inscrição", "")).strip()
    resultado.update({
        "status_disponibilidade": "UNKNOWN",
        "motivo_verificacao": "Verificacao nao concluida",
        "http_status": None,
        "page_final_url": "",
        "page_title": "",
        "body_text": "",
        "body_excerpt": "",
        "application_links": [],
        "application_page_url": "",
        "application_http_status": None,
        "application_final_url": "",
        "application_title": "",
        "application_form_count": 0,
        "application_field_signals": [],
        "application_body_excerpt": "",
        "application_error": "",
    })
    page = await context.new_page()
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != HOST_VAGA:
            raise ValueError("URL nao pertence ao portal oficial jobs.recrutei.com.br")
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        resultado["http_status"] = response.status if response else None
        await aguardar_conteudo(page)
        resultado["page_final_url"] = page.url
        resultado["page_title"] = (await page.title())[:300]
        body = await page.locator("body").inner_text(timeout=7000)
        resultado["body_text"] = body[:6000]
        resultado["body_excerpt"] = body[:1800]
        resultado["application_links"] = await extrair_links_candidatura(page)

        # Segue somente links de candidatura no subdominio oficial da Recrutei.
        app_link = next(
            (
                item["href"]
                for item in resultado["application_links"]
                if urlparse(item["href"]).hostname == HOST_CANDIDATURA
            ),
            None,
        )
        if app_link:
            app_result = await inspecionar_formulario(context, app_link)
            resultado.update(app_result)

        # Evidencia visual apenas para o caso manual reportado.
        if str(vaga.get("ID", "")) == VAGA_RECLAMADA["ID"]:
            try:
                await page.screenshot(path="diagnostico_governanca.png", full_page=True)
            except Exception:
                pass

    except PlaywrightTimeoutError:
        resultado["application_error"] = "Timeout ao consultar a pagina da vaga"
    except Exception as exc:
        resultado["application_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await page.close()

    status, motivo = classificar(resultado)
    resultado["status_disponibilidade"] = status
    resultado["motivo_verificacao"] = motivo
    print(
        f"{resultado.get('ID', url)}: {status} — {motivo}; "
        f"HTTP vaga={resultado.get('http_status')}; "
        f"HTTP candidatura={resultado.get('application_http_status')}"
    )
    return resultado


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
    link_manual = VAGA_RECLAMADA["Link de Inscrição"]
    if not amostra["Link de Inscrição"].astype(str).eq(link_manual).any():
        amostra = pd.concat([amostra, pd.DataFrame([VAGA_RECLAMADA])], ignore_index=True, sort=False)

    print(f"Total de vagas na coleta: {len(df)}")
    print(f"Vagas selecionadas para diagnostico: {len(amostra)}")
    print("Caso manual: Analista de Governanca de Dados SPS0001946")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="pt-BR",
                timezone_id="America/Sao_Paulo",
            )
            resultados = []
            for vaga in amostra.to_dict(orient="records"):
                resultados.append(await diagnosticar_vaga(context, vaga))
            await context.close()
        finally:
            await browser.close()

    resultado_df = pd.DataFrame(resultados)
    resultado_df.to_csv(ARQUIVO_RESULTADO_CSV, index=False, encoding="utf-8-sig")
    ARQUIVO_JSON.write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    contagens = resultado_df["status_disponibilidade"].value_counts().to_dict()
    print("RESUMO DA VERIFICACAO:")
    print(json.dumps(contagens, ensure_ascii=False))
    manual = resultado_df[resultado_df["ID"].astype(str) == VAGA_RECLAMADA["ID"]]
    if not manual.empty:
        item = manual.iloc[0]
        print(
            "CASO SPS0001946: "
            f"{item['status_disponibilidade']} — {item['motivo_verificacao']}"
        )
        print(f"Links de candidatura encontrados: {item.get('application_links', [])}")
        print(f"Campos detectados no formulario: {item.get('application_field_signals', [])}")
    print(f"Resultados salvos em {ARQUIVO_JSON} e {ARQUIVO_RESULTADO_CSV}.")
    print("Nenhum formulario foi preenchido ou enviado; nenhuma publicacao foi realizada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
