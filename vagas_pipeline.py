"""Coletor e organizador de vagas da Digisystem.

Gera vagas_digisystem.csv, vagas_digisystem.xlsx e acrescenta os dados
coletados ao prompt_pronto.txt. Tambem expoe gerar_calendario(), usado por
 generate_posts.py, e verificadores conservadores para uso na pre-publicacao.

Status de disponibilidade:
- ACTIVE: formulario de candidatura acessivel e vinculado ao ID especifico da vaga.
- CLOSED: pagina informa explicitamente encerramento ou retorna HTTP 404/410.
- UNKNOWN: qualquer resultado inconclusivo ou apenas banco geral de talentos.

A verificacao e compartilhada com o publicador diario.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import unicodedata
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

try:
    import holidays
except ImportError:  # dependencia declarada em requirements.txt
    holidays = None

PORTAL = "https://jobs.recrutei.com.br/digisystem/"
HOST_PERMITIDO = "jobs.recrutei.com.br"
HOST_CANDIDATURA = "talent.recrutei.com.br"
SELETOR_VAGAS = "a[href*='/vacancy/'], a[href*='/vaga/']"
ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_XLSX = Path("vagas_digisystem.xlsx")
ARQUIVO_PROMPT = Path("prompt_pronto.txt")
MARCADOR_DADOS = "\n\n=== DADOS DA COLETA DIGISYSTEM (GERADO AUTOMATICAMENTE) ==="
TIMEZONE = ZoneInfo("America/Sao_Paulo")

COLUNAS = [
    "ID",
    "Vaga",
    "Detalhes",
    "Link de Inscrição",
    "Status da coleta",
    "Data da coleta",
]

# Apenas mensagens inequívocas de encerramento. Termos genéricos como
# "encerrado" isoladamente não são suficientes para evitar falsos positivos.
MARCADORES_ENCERRADA = (
    "vaga encerrada",
    "esta vaga nao recebe mais candidaturas",
    "essa vaga nao recebe mais candidaturas",
    "esta vaga nao recebe mais candidatura",
    "essa vaga nao recebe mais candidatura",
    "nao recebe mais candidaturas",
    "esta vaga nao aceita mais candidaturas",
    "essa vaga nao aceita mais candidaturas",
    "parece que essa vaga nao esta mais disponivel",
    "essa vaga nao esta mais disponivel",
    "esta vaga nao esta mais disponivel",
    "vaga nao esta mais disponivel",
    "vaga nao encontrada",
    "essa oportunidade nao esta mais disponivel",
    "inscricoes encerradas",
    "processo seletivo encerrado",
    "o processo seletivo foi encerrado",
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

# Sao lidos apenas nomes/atributos dos campos; nunca valores digitados pelo candidato.
SINAIS_CAMPOS = {
    "nome": ("nome", "name", "full_name", "fullname"),
    "email": ("email", "e-mail", "mail"),
    "telefone": ("telefone", "celular", "phone", "mobile"),
    "documento": ("cpf", "documento"),
    "curriculo": ("curriculo", "curriculum", "resume", "cv"),
    "arquivo": ("upload", "arquivo", "anexo", "file"),
}

MENSAGENS_SEM_VAGAS = (
    "nenhuma vaga encontrada",
    "nenhuma vaga disponível",
    "nenhuma vaga disponivel",
    "não há vagas abertas",
    "nao ha vagas abertas",
    "não encontramos vagas",
    "nao encontramos vagas",
)


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", texto).strip().lower()


def _url_canonica(href: str) -> str:
    url = urljoin(PORTAL, href.strip())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != HOST_PERMITIDO:
        raise ValueError(f"Link fora do portal oficial da Digisystem: {url}")
    if not ("/vacancy/" in parsed.path or "/vaga/" in parsed.path):
        raise ValueError(f"Link não parece ser de uma vaga individual: {url}")
    # Query strings e fragmentos de rastreamento não fazem parte da identidade.
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", parsed.netloc.lower(), path, "", "", ""))


def _id_estavel(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16].upper()
    return f"DIGI-{digest}"


def gerar_calendario(data_referencia: date | None = None):
    """Retorna (dias_uteis, feriados_do_mes, inicio_mes, fim_mes).

    A assinatura de quatro retornos preserva a compatibilidade com
    generate_posts.py. Considera segunda a sexta e feriados nacionais do Brasil.
    Quando executado depois do primeiro dia do mês, não inclui datas passadas.
    """
    if holidays is None:
        raise RuntimeError(
            "Dependência 'holidays' ausente. Execute: pip install -r requirements.txt"
        )

    referencia = data_referencia or datetime.now(TIMEZONE).date()
    inicio = date(referencia.year, referencia.month, 1)
    fim = date(referencia.year, referencia.month, monthrange(referencia.year, referencia.month)[1])
    feriados_ano = holidays.country_holidays("BR", years=[referencia.year])
    feriados_mes = {
        dia: nome
        for dia, nome in feriados_ano.items()
        if dia.year == referencia.year and dia.month == referencia.month
    }

    primeiro_dia = max(inicio, referencia)
    dias_uteis = []
    for numero in range(primeiro_dia.day, fim.day + 1):
        dia = date(referencia.year, referencia.month, numero)
        if dia.weekday() < 5 and dia not in feriados_mes:
            dias_uteis.append(dia)

    return dias_uteis, feriados_mes, inicio, fim


async def _extrair_texto_cartao(elemento) -> dict[str, str]:
    """Obtém texto do link e do menor contêiner de cartão que o envolve."""
    return await elemento.evaluate(
        """(a) => {
          const clean = (v) => (v || '').replace(/\\u00a0/g, ' ').trim();
          const href = a.getAttribute('href') || '';
          const anchorText = clean(a.innerText || a.textContent || '');
          let node = a;
          let bestText = anchorText;
          let bestTitle = '';
          for (let i = 0; i < 6 && node; i++) {
            const heading = node.querySelector('h1,h2,h3,h4,[class*="title"],[class*="Title"]');
            const headingText = heading ? clean(heading.innerText || heading.textContent) : '';
            const text = clean(node.innerText || node.textContent || '');
            const jobLinks = node.querySelectorAll("a[href*='/vacancy/'], a[href*='/vaga/']");
            if (text && text.length <= 1800 && jobLinks.length <= 1 && text.length >= bestText.length) {
              bestText = text;
              if (headingText) bestTitle = headingText;
            }
            if (jobLinks.length > 1) break;
            node = node.parentElement;
          }
          return { href, anchorText, cardText: bestText, headingText: bestTitle };
        }"""
    )


def _separar_modalidade_do_titulo(titulo_bruto: str) -> tuple[str, list[str]]:
    """Remove CLT/PJ quando o portal os injeta como linha separada no título."""
    linhas = [re.sub(r"\s+", " ", linha).strip() for linha in (titulo_bruto or "").splitlines()]
    linhas = [linha for linha in linhas if linha]
    modalidades: list[str] = []
    while linhas and _normalizar(linhas[-1]) in {"clt", "pj"}:
        modalidades.insert(0, linhas.pop())
    return " ".join(linhas).strip(), modalidades


def _interpretar_cartao(item: dict[str, str], indice: int, coletado_em: str) -> dict[str, str] | None:
    href = (item.get("href") or "").strip()
    if not href:
        return None
    try:
        link = _url_canonica(href)
    except ValueError:
        return None

    texto_cartao = item.get("cardText") or item.get("anchorText") or ""
    titulo_bruto = (item.get("headingText") or "").strip()
    if not titulo_bruto:
        titulo_bruto = (item.get("anchorText") or "").strip().splitlines()[0].strip()

    titulo, modalidades_titulo = _separar_modalidade_do_titulo(titulo_bruto)
    if not titulo:
        titulo = f"Vaga sem título (item {indice})"

    # O Recrutei pode renderizar o título em várias linhas e repetir o nome
    # dentro do cartão. Divide também metadados separados por barras verticais.
    segmentos: list[str] = []
    for linha in texto_cartao.splitlines():
        linha = re.sub(r"\s+", " ", linha).strip(" \t•|-")
        if not linha:
            continue
        segmentos.extend(
            parte.strip(" \t•-")
            for parte in re.split(r"\s*\|\s*", linha)
            if parte.strip(" \t•-")
        )

    # Remove do início do cartão todas as linhas que, juntas, formam o título.
    # Isso preserva títulos legítimos em duas linhas, como "Vaga afirmativa".
    tamanho_titulo = 0
    for n in range(1, min(len(segmentos), 6) + 1):
        candidato = " ".join(segmentos[:n])
        if _normalizar(candidato) == _normalizar(titulo):
            tamanho_titulo = n
            break
    if tamanho_titulo:
        segmentos = segmentos[tamanho_titulo:]
    else:
        # Fallback para cartões cujo texto não repete o título exatamente.
        segmentos = [
            parte for parte in segmentos
            if _normalizar(parte) != _normalizar(titulo)
        ]

    ignorar = {
        "saiba mais", "ver vaga", "candidatar-se", "candidate-se",
        "quero me candidatar", "inscreva-se", "detalhes da vaga",
    }
    detalhes_linhas = [
        linha for linha in segmentos
        if _normalizar(linha) not in ignorar
    ]

    # Se CLT/PJ apareceu no heading, garante que continue nos detalhes,
    # sem contaminar o campo do título.
    existentes = {_normalizar(linha) for linha in detalhes_linhas}
    for modalidade in reversed(modalidades_titulo):
        if _normalizar(modalidade) not in existentes:
            detalhes_linhas.insert(0, modalidade)

    return {
        "ID": _id_estavel(link),
        "Vaga": titulo,
        "Detalhes": " | ".join(detalhes_linhas),
        "Link de Inscrição": link,
        "Status da coleta": "LISTED",
        "Data da coleta": coletado_em,
    }


async def extrair_vagas_completas() -> list[dict[str, str]]:
    """Coleta os cartões listados no portal oficial e grava CSV/XLSX."""
    coletado_em = datetime.now(TIMEZONE).isoformat(timespec="seconds")
    dados: list[dict[str, str]] = []
    urls_vistas: set[str] = set()

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
            page = await context.new_page()
            print(f"Acessando o portal oficial: {PORTAL}")
            resposta = await page.goto(PORTAL, wait_until="domcontentloaded", timeout=45000)
            if resposta and resposta.status >= 400:
                raise RuntimeError(f"Portal retornou HTTP {resposta.status}")

            await page.wait_for_timeout(2500)
            # Rola gradualmente para carregar listas dinâmicas/lazy loading.
            contagem_anterior = -1
            estabilidade = 0
            for _ in range(10):
                contagem_atual = await page.locator(SELETOR_VAGAS).count()
                altura_anterior = await page.evaluate("document.body.scrollHeight")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(800)
                altura_atual = await page.evaluate("document.body.scrollHeight")
                contagem_nova = await page.locator(SELETOR_VAGAS).count()
                if contagem_nova == contagem_anterior == contagem_atual and altura_atual == altura_anterior:
                    estabilidade += 1
                else:
                    estabilidade = 0
                contagem_anterior = contagem_nova
                if estabilidade >= 2:
                    break

            elementos = page.locator(SELETOR_VAGAS)
            quantidade = await elementos.count()
            if quantidade == 0:
                corpo = _normalizar(await page.locator("body").inner_text())
                if any(_normalizar(frase) in corpo for frase in MENSAGENS_SEM_VAGAS):
                    print("O portal informou que não há vagas abertas no momento.")
                else:
                    await page.screenshot(path="screenshot_erro.png", full_page=True)
                    raise RuntimeError(
                        "Nenhum cartão de vaga foi encontrado e o portal não confirmou lista vazia. "
                        "Pode ter ocorrido mudança no site ou falha de carregamento. Veja screenshot_erro.png."
                    )

            for indice in range(quantidade):
                item = await _extrair_texto_cartao(elementos.nth(indice))
                vaga = _interpretar_cartao(item, indice + 1, coletado_em)
                if not vaga:
                    continue
                if vaga["Link de Inscrição"] in urls_vistas:
                    continue
                urls_vistas.add(vaga["Link de Inscrição"])
                dados.append(vaga)
                print(f"{len(dados)}. {vaga['Vaga']} | {vaga['ID']}")

            await context.close()
        except Exception:
            # Tenta preservar evidência visual sem esconder o erro original.
            try:
                await page.screenshot(path="screenshot_erro.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            await browser.close()

    df = pd.DataFrame(dados, columns=COLUNAS)
    df.to_csv(ARQUIVO_CSV, index=False, encoding="utf-8-sig")
    df.to_excel(ARQUIVO_XLSX, index=False)
    _atualizar_prompt(dados, coletado_em)
    print(f"Coleta concluída: {len(dados)} vaga(s). Arquivos CSV e XLSX gerados.")
    return dados


def _atualizar_prompt(vagas: list[dict[str, str]], coletado_em: str) -> None:
    """Anexa os dados ao prompt sem duplicar blocos em execuções locais."""
    if not ARQUIVO_PROMPT.exists():
        raise FileNotFoundError(
            "prompt_pronto.txt não encontrado. Coloque o prompt editorial na raiz do repositório."
        )
    conteudo = ARQUIVO_PROMPT.read_text(encoding="utf-8")
    if MARCADOR_DADOS in conteudo:
        conteudo = conteudo.split(MARCADOR_DADOS, 1)[0]

    linhas = [
        MARCADOR_DADOS,
        f"Data/hora da coleta: {coletado_em}",
        f"Quantidade de vagas listadas no portal: {len(vagas)}",
        "Fonte exclusiva: portal oficial da Digisystem.",
        "IMPORTANTE: LISTED significa que o cartão apareceu na listagem. Isso ainda não comprova que a candidatura continua aberta. Não invente dados.",
    ]
    if not vagas:
        linhas.append(
            "NENHUMA VAGA FOI LISTADA. Retorne somente um array JSON vazio: []. Não crie posts genéricos nem invente oportunidades."
        )
    else:
        linhas.append("Dados estruturados das oportunidades:")
        linhas.append(json.dumps(vagas, ensure_ascii=False, indent=2))
    ARQUIVO_PROMPT.write_text(conteudo.rstrip() + "\n" + "\n".join(linhas) + "\n", encoding="utf-8")


def _extrair_id_vaga(url: str) -> str | None:
    """Extrai o identificador numerico da URL /vacancy/<id>-slug."""
    match = re.search(r"/vacancy/(\d+)(?:-|/|$)", urlparse(url).path)
    return match.group(1) if match else None


async def _extrair_links_candidatura(page, vacancy_id: str | None) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Separa candidatura especifica da vaga do cadastro geral no banco de talentos."""
    links = await page.locator("a[href]").evaluate_all(
        r"""els => els.map(a => ({
          texto: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim(),
          href: a.href || ''
        }))"""
    )
    candidaturas_vaga: list[dict[str, str]] = []
    banco_talentos: list[dict[str, str]] = []
    vistos: set[str] = set()
    for item in links:
        texto = str(item.get("texto", ""))
        href = str(item.get("href", ""))
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or parsed.hostname != HOST_CANDIDATURA:
            continue
        path = parsed.path.rstrip("/")
        if href in vistos:
            continue
        vistos.add(href)
        registro = {"texto": texto[:200], "href": href}
        match = re.fullmatch(r"/digisystem/(\d+)/signup", path)
        if match and vacancy_id and match.group(1) == vacancy_id:
            combinado = _normalizar(texto + " " + href)
            if any(termo in combinado for termo in TERMOS_CANDIDATURA):
                candidaturas_vaga.append(registro)
        elif path in {"/digisystem/signup", "/digisystem"}:
            banco_talentos.append(registro)
    return candidaturas_vaga, banco_talentos


async def _inspecionar_formulario_candidatura(context, url: str) -> dict[str, Any]:
    """Inspeciona formulario especifico sem preencher ou ler valores pessoais."""
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
        try:
            await page.wait_for_load_state("networkidle", timeout=7000)
        except PlaywrightTimeoutError:
            pass
        await page.wait_for_timeout(900)
        resultado["application_final_url"] = page.url
        resultado["application_title"] = (await page.title())[:300]
        body = await page.locator("body").inner_text(timeout=7000)
        resultado["application_body_excerpt"] = body[:2500]
        resultado["application_form_count"] = await page.locator("form").count()
        campos = await page.locator("input, textarea, select").evaluate_all(
            """els => els.map(el => ({
              name: el.getAttribute('name') || '',
              type: el.getAttribute('type') || el.tagName.toLowerCase(),
              placeholder: el.getAttribute('placeholder') || '',
              aria: el.getAttribute('aria-label') || '',
              autocomplete: el.getAttribute('autocomplete') || ''
            }))"""
        )
        metadados = " ".join(" ".join(str(v) for v in campo.values()) for campo in campos)
        metadados_norm = _normalizar(metadados)
        sinais = [
            categoria
            for categoria, termos in SINAIS_CAMPOS.items()
            if any(_normalizar(termo) in metadados_norm for termo in termos)
        ]
        texto_norm = _normalizar(body)
        for categoria, termos in SINAIS_CAMPOS.items():
            if categoria not in sinais and any(_normalizar(t) in texto_norm for t in termos):
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


def _classificar_resultado_disponibilidade(resultado: dict[str, Any]) -> tuple[str, str]:
    """ACTIVE exige formulario da vaga; banco geral de talentos nunca basta."""
    status = resultado.get("http_status")
    if status in {404, 410}:
        return "CLOSED", f"Pagina da vaga retornou HTTP {status}"
    if status is None or status in {401, 403, 429} or (isinstance(status, int) and status >= 500):
        return "UNKNOWN", f"Resposta inconclusiva da pagina da vaga: HTTP {status}"

    textos = [resultado.get("body_text", ""), resultado.get("application_body_excerpt", "")]
    for texto in textos:
        texto_norm = _normalizar(str(texto))
        for termo in MARCADORES_ENCERRADA:
            if _normalizar(termo) in texto_norm:
                return "CLOSED", f"Mensagem explicita de encerramento: {termo}"

    app_status = resultado.get("application_http_status")
    if app_status in {404, 410}:
        return "CLOSED", f"Pagina de candidatura retornou HTTP {app_status}"
    if app_status in {401, 403, 429} or (isinstance(app_status, int) and app_status >= 500):
        return "UNKNOWN", f"Pagina de candidatura inconclusiva: HTTP {app_status}"

    vacancy_id = str(resultado.get("vacancy_id") or "")
    page_final = urlparse(str(resultado.get("page_final_url") or ""))
    page_final_id = _extrair_id_vaga(str(resultado.get("page_final_url") or ""))
    if (
        page_final.scheme not in {"http", "https"}
        or page_final.hostname != HOST_PERMITIDO
        or not vacancy_id
        or page_final_id != vacancy_id
    ):
        return "UNKNOWN", "A pagina final nao corresponde ao ID da vaga consultada"

    app_final = urlparse(str(resultado.get("application_final_url") or ""))
    app_path_match = re.fullmatch(r"/digisystem/(\d+)/signup", app_final.path.rstrip("/"))
    app_url_vinculada = bool(
        app_final.scheme in {"http", "https"}
        and app_final.hostname == HOST_CANDIDATURA
        and app_path_match
        and vacancy_id
        and app_path_match.group(1) == vacancy_id
    )
    sinais = set(resultado.get("application_field_signals", []))
    if (
        resultado.get("application_links")
        and app_url_vinculada
        and app_status == 200
        and int(resultado.get("application_form_count") or 0) > 0
        and len(sinais) >= 2
        and ("nome" in sinais or "email" in sinais)
    ):
        return "ACTIVE", "Formulario acessivel e vinculado ao ID especifico da vaga, com campos de candidato"

    if resultado.get("application_links"):
        return "UNKNOWN", "Link especifico encontrado, mas formulario vinculado nao foi confirmado"
    if resultado.get("talent_pool_links"):
        return "UNKNOWN", "Somente banco geral de talentos; nao comprova candidatura para esta vaga"
    return "UNKNOWN", "Nao foi possivel confirmar formulario especifico de candidatura"


async def verificar_disponibilidade_vagas(vagas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verifica a pagina e o formulario especifico; UNKNOWN nunca equivale a ACTIVE.

    Esta funcao e compartilhada pelo teste de disponibilidade e pelo publicador diario.
    """
    if not vagas:
        return []

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
            resultados: list[dict[str, Any]] = []
            for vaga in vagas:
                resultado = dict(vaga)
                url = str(vaga.get("Link de Inscrição", "")).strip()
                agora = datetime.now(TIMEZONE).isoformat(timespec="seconds")
                resultado.update({
                    "status_disponibilidade": "UNKNOWN",
                    "motivo_verificacao": "Verificacao nao concluida",
                    "http_status": None,
                    "page_final_url": "",
                    "page_title": "",
                    "body_text": "",
                    "application_links": [],
                    "talent_pool_links": [],
                    "application_page_url": "",
                    "application_http_status": None,
                    "application_final_url": "",
                    "application_title": "",
                    "application_form_count": 0,
                    "application_field_signals": [],
                    "application_error": "",
                    "last_verified_at": agora,
                })
                page = None
                try:
                    url = _url_canonica(url)
                    vacancy_id = _extrair_id_vaga(url)
                    resultado["vacancy_id"] = vacancy_id
                    if not vacancy_id:
                        raise ValueError("Nao foi possivel extrair o ID numerico da vaga")
                    page = await context.new_page()
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    resultado["http_status"] = response.status if response else None
                    try:
                        await page.wait_for_load_state("networkidle", timeout=7000)
                    except PlaywrightTimeoutError:
                        pass
                    await page.wait_for_timeout(900)
                    resultado["page_final_url"] = page.url
                    resultado["page_title"] = (await page.title())[:300]
                    body = await page.locator("body").inner_text(timeout=7000)
                    resultado["body_text"] = body[:6000]
                    links_vaga, links_banco = await _extrair_links_candidatura(page, vacancy_id)
                    resultado["application_links"] = links_vaga
                    resultado["talent_pool_links"] = links_banco

                    body_norm = _normalizar(resultado["body_text"])
                    encerrada = any(_normalizar(t) in body_norm for t in MARCADORES_ENCERRADA)
                    if links_vaga and not encerrada:
                        app_result = await _inspecionar_formulario_candidatura(context, links_vaga[0]["href"])
                        resultado.update(app_result)
                except PlaywrightTimeoutError:
                    resultado["application_error"] = "Timeout ao consultar a pagina da vaga"
                except Exception as exc:
                    resultado["application_error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    if page is not None:
                        await page.close()

                status, motivo = _classificar_resultado_disponibilidade(resultado)
                resultado["status_disponibilidade"] = status
                resultado["motivo_verificacao"] = motivo
                print(
                    f"{resultado.get('ID', url)}: {status} - {motivo}; "
                    f"HTTP vaga={resultado.get('http_status')}; "
                    f"HTTP candidatura={resultado.get('application_http_status')}"
                )
                resultados.append(resultado)
            await context.close()
            return resultados
        finally:
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Coletor de vagas da Digisystem")
    parser.add_argument(
        "--somente-calendario",
        action="store_true",
        help="Imprime o calendário do mês sem acessar o portal.",
    )
    args = parser.parse_args()

    if args.somente_calendario:
        dias, feriados_mes, inicio, fim = gerar_calendario()
        print(f"Período: {inicio} a {fim}")
        print(f"Dias de publicação: {len(dias)}")
        for dia in dias:
            print(dia.isoformat())
        if feriados_mes:
            print("Feriados:", json.dumps({d.isoformat(): n for d, n in feriados_mes.items()}, ensure_ascii=False))
        return 0

    try:
        vagas = asyncio.run(extrair_vagas_completas())
        # Garante que a função de calendário exigida pelo gerador está operacional.
        dias, _, _, _ = gerar_calendario()
        print(f"Calendário preparado: {len(dias)} dia(s) útil(eis) para publicação.")
        if not vagas:
            print("Aviso: nenhuma vaga listada. O gerador deverá produzir posts.json vazio.")
        return 0
    except Exception as exc:
        print(f"ERRO FATAL NO COLETOR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
