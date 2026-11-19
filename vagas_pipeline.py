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


def _classificar_pagina(status_http: int | None, url_final: str, texto: str) -> tuple[str, str]:
    normalizado = _normalizar(texto)
    if status_http in {404, 410}:
        return "CLOSED", f"Página retornou HTTP {status_http}"
    if status_http is not None and (status_http == 429 or status_http >= 500 or status_http in {401, 403}):
        return "UNKNOWN", f"Resposta HTTP inconclusiva: {status_http}"
    for marcador in MARCADORES_ENCERRADA:
        if _normalizar(marcador) in normalizado:
            return "CLOSED", f"Mensagem explícita de encerramento: {marcador}"
    caminho_final = urlparse(url_final).path.rstrip("/")
    if not ("/vacancy/" in caminho_final or "/vaga/" in caminho_final):
        return "UNKNOWN", "A página redirecionou para um endereço que não identifica a vaga"
    for marcador in MARCADORES_ABERTA:
        if _normalizar(marcador) in normalizado:
            return "ACTIVE", f"Indicador de candidatura encontrado: {marcador}"
    return "UNKNOWN", "A página não apresentou indicador conclusivo de abertura ou encerramento"


async def verificar_disponibilidade_vagas(vagas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verifica páginas individuais; resultado UNKNOWN nunca equivale a ACTIVE.

    Esta função fica disponível para ser chamada pelo publicador diário na próxima
    etapa. A integração automática com publish_linkedin.py ainda não foi feita.
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
                try:
                    url = _url_canonica(url)
                    page = await context.new_page()
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(700)
                    texto = await page.locator("body").inner_text(timeout=5000)
                    status_http = response.status if response else None
                    situacao, motivo = _classificar_pagina(status_http, page.url, texto)
                    await page.close()
                except PlaywrightTimeoutError:
                    situacao, motivo, status_http = "UNKNOWN", "Timeout ao consultar a página", None
                except Exception as exc:
                    situacao, motivo, status_http = "UNKNOWN", f"Falha de consulta: {type(exc).__name__}: {exc}", None
                resultado.update({
                    "status_disponibilidade": situacao,
                    "motivo_verificacao": motivo,
                    "http_status": status_http,
                    "last_verified_at": agora,
                })
                resultados.append(resultado)
                print(f"{resultado.get('ID', url)}: {situacao} — {motivo}")
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


