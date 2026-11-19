"""Gera posts mensais da Digisystem em lotes pequenos e retomaveis."""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from vagas_pipeline import gerar_calendario

MODELO = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
CHAVE = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_CHAVE = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODELO = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_FREE_TIER_CONFIRMED = os.getenv("GROQ_FREE_TIER_CONFIRMED", "false").strip().lower() == "true"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PORTAL = "https://jobs.recrutei.com.br/digisystem"
LIMITE = 2900
TIMEZONE = ZoneInfo("America/Sao_Paulo")
TIMEOUT_GEMINI = (15, 75)
TIMEOUT_GROQ = (10, 60)
MAX_TENTATIVAS_REDE = 2
MAX_TENTATIVAS_GROQ = 2
MAX_TENTATIVAS_VALIDACAO = 2
GRUPOS_POR_LOTE = 2  # Lotes menores reduzem o consumo de tokens por requisicao.
GROQ_MAX_COMPLETION_TOKENS = 2048  # Evita reservar 8192 tokens e ultrapassar o TPM gratuito.
MAX_ESPERA = 30
MARCADOR_DADOS = "=== DADOS DA COLETA DIGISYSTEM (GERADO AUTOMATICAMENTE) ==="
ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_PROMPT = Path("prompt_pronto.txt")
ARQUIVO_SAIDA = Path("posts.json")
ARQUIVO_PARCIAL = Path("posts_parciais.json")


class ErroGeracao(RuntimeError):
    pass


def normalizar_link(link: str) -> str:
    valor = (link or "").strip().rstrip(".,;:!?")
    partes = urlsplit(valor)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        return valor.rstrip("/")
    return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), partes.path.rstrip("/"), partes.query, ""))


def interpretar_retry_after(valor: str | None) -> int | None:
    if not valor:
        return None
    try:
        segundos = int(valor.strip())
    except (TypeError, ValueError):
        try:
            data = parsedate_to_datetime(valor)
            if data.tzinfo is None:
                data = data.replace(tzinfo=timezone.utc)
            segundos = int((data - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(segundos, 1), MAX_ESPERA)


def espera_retentativa(tentativa: int, retry_after: int | None = None) -> int:
    base = retry_after if retry_after is not None else min(8 * (2 ** (tentativa - 1)), MAX_ESPERA)
    base = min(max(base, 1), MAX_ESPERA)
    return int(round(base + random.uniform(0, min(5, max(1, base * 0.2)))))


def extrair_json_resposta(texto: str) -> Any:
    """Extrai JSON mesmo quando o modelo acrescenta cercas Markdown ou texto curto."""
    bruto = (texto or "").strip()
    bruto = re.sub(r"^```(?:json)?\s*", "", bruto, flags=re.IGNORECASE)
    bruto = re.sub(r"\s*```$", "", bruto)
    try:
        return json.loads(bruto)
    except json.JSONDecodeError:
        pass

    for inicio, caractere in enumerate(bruto):
        if caractere not in "[{":
            continue
        pilha: list[str] = []
        em_string = False
        escape = False
        pares = {"[": "]", "{": "}"}
        for fim in range(inicio, len(bruto)):
            atual = bruto[fim]
            if em_string:
                if escape:
                    escape = False
                elif atual == "\\":
                    escape = True
                elif atual == '"':
                    em_string = False
                continue
            if atual == '"':
                em_string = True
            elif atual in pares:
                pilha.append(pares[atual])
            elif atual in "]}":
                if not pilha or pilha[-1] != atual:
                    break
                pilha.pop()
                if not pilha:
                    trecho = bruto[inicio:fim + 1]
                    try:
                        return json.loads(trecho)
                    except json.JSONDecodeError:
                        break
    raise ErroGeracao("O provedor nao retornou JSON valido.")


def extrair_lista_resposta(resposta: Any, etapa: str) -> list[Any]:
    """Aceita array direto ou objeto JSON com array em uma chave conhecida."""
    if isinstance(resposta, list):
        return resposta
    if isinstance(resposta, dict):
        for chave in ("items", "grupos", "groups", "posts", "data", "result"):
            valor = resposta.get(chave)
            if isinstance(valor, list):
                return valor
    raise ValueError(f"{etapa}: a resposta precisa conter um array JSON.")


def carregar_vagas() -> list[dict[str, str]]:
    if not ARQUIVO_CSV.exists():
        raise FileNotFoundError(f"Arquivo obrigatorio nao encontrado: {ARQUIVO_CSV}")

    df = pd.read_csv(ARQUIVO_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    obrigatorias = {"ID", "Vaga", "Detalhes", "Link de Inscrição"}
    faltantes = obrigatorias - set(df.columns)
    if faltantes:
        raise ValueError(f"Colunas ausentes no CSV: {', '.join(sorted(faltantes))}")

    vagas: list[dict[str, str]] = []
    ids_vistos: set[str] = set()
    for numero, linha in enumerate(df.to_dict(orient="records"), start=2):
        vaga_id = str(linha.get("ID", "")).strip()
        titulo = str(linha.get("Vaga", "")).strip()
        link = normalizar_link(str(linha.get("Link de Inscrição", "")))
        if not vaga_id or not titulo or not link:
            raise ValueError(f"Registro incompleto na linha {numero} do CSV")
        if vaga_id in ids_vistos:
            raise ValueError(f"ID duplicado no CSV: {vaga_id}")
        if urlsplit(link).hostname != "jobs.recrutei.com.br":
            raise ValueError(f"Link fora do portal oficial na linha {numero}: {link}")
        ids_vistos.add(vaga_id)
        vagas.append({
            "ID": vaga_id,
            "Vaga": titulo,
            "Detalhes": str(linha.get("Detalhes", "")).strip(),
            "Link de Inscrição": link,
        })
    return vagas


def carregar_prompt_editorial() -> str:
    if not ARQUIVO_PROMPT.exists():
        raise FileNotFoundError(f"Arquivo obrigatorio nao encontrado: {ARQUIVO_PROMPT}")
    conteudo = ARQUIVO_PROMPT.read_text(encoding="utf-8")
    if MARCADOR_DADOS in conteudo:
        conteudo = conteudo.split(MARCADOR_DADOS, 1)[0]
    conteudo = conteudo.strip()
    if not conteudo:
        raise ValueError("O prompt editorial esta vazio.")
    return conteudo


def _chamar_gemini_json(prompt: str, *, etapa: str, temperatura: float) -> Any:
    if not CHAVE:
        raise ErroGeracao("GEMINI_API_KEY nao foi encontrada nos Secrets do GitHub.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    corpo = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": temperatura,
            "maxOutputTokens": 8192,
        },
    }

    for tentativa in range(1, MAX_TENTATIVAS_REDE + 1):
        inicio = time.monotonic()
        try:
            resposta = requests.post(
                url,
                headers={"x-goog-api-key": CHAVE},
                json=corpo,
                timeout=TIMEOUT_GEMINI,
            )
        except requests.RequestException as exc:
            if tentativa == MAX_TENTATIVAS_REDE:
                raise ErroGeracao(f"{etapa}: falha de conexao com Gemini apos {tentativa} tentativas: {exc}") from exc
            pausa = espera_retentativa(tentativa)
            print(f"{etapa}: Gemini timeout/conexao. Nova tentativa em {pausa}s.")
            time.sleep(pausa)
            continue

        duracao = round(time.monotonic() - inicio, 1)
        if resposta.status_code in {408, 429, 500, 502, 503, 504}:
            if tentativa == MAX_TENTATIVAS_REDE:
                raise ErroGeracao(
                    f"{etapa}: Gemini HTTP {resposta.status_code} apos {tentativa} tentativas: {resposta.text[:350]}"
                )
            retry_after = interpretar_retry_after(resposta.headers.get("Retry-After"))
            pausa = espera_retentativa(tentativa, retry_after)
            print(f"{etapa}: Gemini HTTP {resposta.status_code} em {duracao}s. Nova tentativa em {pausa}s.")
            time.sleep(pausa)
            continue

        if resposta.status_code != 200:
            raise ErroGeracao(f"{etapa}: Gemini HTTP {resposta.status_code}: {resposta.text[:500]}")

        try:
            dados = resposta.json()
            texto = dados["candidates"][0]["content"]["parts"][0]["text"]
            resultado = extrair_json_resposta(texto)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ErroGeracao(f"{etapa}: resposta JSON do Gemini invalida: {exc}") from exc

        print(f"{etapa}: resposta recebida em {duracao}s usando Gemini ({MODELO}).")
        return resultado

    raise ErroGeracao(f"{etapa}: tentativas Gemini esgotadas.")


def _chamar_groq_json(prompt: str, *, etapa: str, temperatura: float) -> Any:
    if not GROQ_CHAVE:
        raise ErroGeracao("GROQ_API_KEY nao foi encontrada nos Secrets do GitHub.")
    if not GROQ_FREE_TIER_CONFIRMED:
        raise ErroGeracao(
            "Fallback Groq bloqueado: confirme que a conta esta no plano Free e defina "
            "a variavel GROQ_FREE_TIER_CONFIRMED=true no GitHub."
        )

    # O modo JSON do Groq exige um objeto na raiz. O array esperado fica em items.
    prompt_groq = (
        prompt
        + "\n\nINSTRUCAO FINAL DE FORMATO: responda somente com um objeto JSON valido, "
        + 'sem Markdown, no formato {"items": [ ... ]}. A propriedade items deve conter '
        + "exatamente o array solicitado nas instrucoes anteriores. Nao omita nem altere IDs."
    )
    corpo = {
        "model": GROQ_MODELO,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Voce e um assistente editorial. Siga os dados fornecidos sem inventar fatos. "
                    "Responda em JSON valido e preserve literalmente todos os IDs e datas."
                ),
            },
            {"role": "user", "content": prompt_groq},
        ],
        "temperature": max(0.1, min(float(temperatura), 2.0)),
        "max_completion_tokens": GROQ_MAX_COMPLETION_TOKENS,
        "response_format": {"type": "json_object"},
    }

    for tentativa in range(1, MAX_TENTATIVAS_GROQ + 1):
        inicio = time.monotonic()
        try:
            resposta = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_CHAVE}",
                    "Content-Type": "application/json",
                },
                json=corpo,
                timeout=TIMEOUT_GROQ,
            )
        except requests.RequestException as exc:
            if tentativa == MAX_TENTATIVAS_GROQ:
                raise ErroGeracao(f"{etapa}: falha de conexao com Groq apos {tentativa} tentativas: {exc}") from exc
            pausa = espera_retentativa(tentativa)
            print(f"{etapa}: Groq timeout/conexao. Nova tentativa em {pausa}s.")
            time.sleep(pausa)
            continue

        duracao = round(time.monotonic() - inicio, 1)
        if resposta.status_code in {408, 429, 500, 502, 503, 504}:
            if tentativa == MAX_TENTATIVAS_GROQ:
                raise ErroGeracao(
                    f"{etapa}: Groq HTTP {resposta.status_code} apos {tentativa} tentativas: {resposta.text[:350]}"
                )
            retry_after = interpretar_retry_after(resposta.headers.get("Retry-After"))
            pausa = espera_retentativa(tentativa, retry_after)
            print(f"{etapa}: Groq HTTP {resposta.status_code} em {duracao}s. Nova tentativa em {pausa}s.")
            time.sleep(pausa)
            continue

        if resposta.status_code != 200:
            raise ErroGeracao(f"{etapa}: Groq HTTP {resposta.status_code}: {resposta.text[:500]}")

        try:
            dados = resposta.json()
            texto = dados["choices"][0]["message"]["content"]
            resultado = extrair_json_resposta(texto)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ErroGeracao(f"{etapa}: resposta JSON do Groq invalida: {exc}") from exc

        if isinstance(resultado, dict) and isinstance(resultado.get("items"), list):
            resultado = resultado["items"]
        print(f"{etapa}: resposta recebida em {duracao}s usando Groq ({GROQ_MODELO}).")
        return resultado

    raise ErroGeracao(f"{etapa}: tentativas Groq esgotadas.")


def chamar_gemini_json(prompt: str, *, etapa: str, temperatura: float) -> Any:
    """Usa Gemini como principal e Groq como fallback, somente no Free confirmado."""
    erros: list[str] = []

    if CHAVE:
        try:
            return _chamar_gemini_json(prompt, etapa=etapa, temperatura=temperatura)
        except ErroGeracao as exc:
            erros.append(f"Gemini: {exc}")
            print(f"{etapa}: Gemini indisponivel; avaliando fallback Groq.", file=sys.stderr)
    else:
        erros.append("Gemini: GEMINI_API_KEY ausente.")
        print(f"{etapa}: GEMINI_API_KEY ausente; avaliando fallback Groq.", file=sys.stderr)

    if GROQ_CHAVE and GROQ_FREE_TIER_CONFIRMED:
        try:
            return _chamar_groq_json(prompt, etapa=etapa, temperatura=temperatura)
        except ErroGeracao as exc:
            erros.append(f"Groq: {exc}")
    elif GROQ_CHAVE:
        erros.append(
            "Groq: fallback bloqueado para evitar custo; confirme o plano Free e defina "
            "GROQ_FREE_TIER_CONFIRMED=true."
        )
    else:
        erros.append("Groq: GROQ_API_KEY ausente.")

    raise ErroGeracao("Todos os provedores falharam. " + " | ".join(erros))


def testar_groq() -> int:
    """Teste pequeno de autenticacao/modelo; nao coleta vagas nem chama o Gemini."""
    if not GROQ_CHAVE:
        raise ErroGeracao("GROQ_API_KEY nao foi encontrada nos Secrets do GitHub.")
    if not GROQ_FREE_TIER_CONFIRMED:
        raise ErroGeracao(
            "Teste bloqueado para proteger o custo. Confirme o plano Free no Groq e defina "
            "GROQ_FREE_TIER_CONFIRMED=true em Settings > Secrets and variables > Actions > Variables."
        )

    corpo = {
        "model": GROQ_MODELO,
        "messages": [
            {"role": "system", "content": "Responda somente JSON valido."},
            {"role": "user", "content": 'Retorne exatamente este objeto JSON: {"status":"ok"}.'},
        ],
        "temperature": 0.1,
        "max_completion_tokens": 64,
        "response_format": {"type": "json_object"},
    }
    try:
        resposta = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_CHAVE}", "Content-Type": "application/json"},
            json=corpo,
            timeout=(10, 30),
        )
    except requests.RequestException as exc:
        raise ErroGeracao(f"Falha de conexao com Groq: {exc}") from exc

    if resposta.status_code != 200:
        raise ErroGeracao(f"Groq HTTP {resposta.status_code}: {resposta.text[:500]}")
    try:
        texto = resposta.json()["choices"][0]["message"]["content"]
        resultado = extrair_json_resposta(texto)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ErroGeracao(f"Resposta de teste do Groq invalida: {exc}") from exc
    if not isinstance(resultado, dict) or resultado.get("status") != "ok":
        raise ErroGeracao(f"Groq respondeu, mas o JSON de teste nao correspondeu: {resultado!r}")
    print(f"Conexao Groq aprovada. Modelo: {GROQ_MODELO}. Nenhuma vaga foi processada.")
    return 0


def montar_prompt_plano(editorial: str, vagas: list[dict[str, str]], total_datas: int) -> str:
    dados_compactos = [
        {"id": v["ID"], "vaga": v["Vaga"], "detalhes": v["Detalhes"]}
        for v in vagas
    ]
    return (
        editorial
        + "\n\nETAPA 1 - PLANEJAMENTO EDITORIAL, SEM REDIGIR POSTS.\n"
        + "Agrupe todas as oportunidades por area funcional, nunca por setor.\n"
        + "Cada grupo sera um post e deve conter somente uma area funcional.\n"
        + "Vagas de grande volume do mesmo tipo podem ser divididas em 2 ou 3 grupos por nivel, quando os dados permitirem.\n"
        + f"O calendario possui {total_datas} datas disponiveis. Crie no maximo {total_datas} grupos.\n"
        + "Nao crie grupos vazios. Nao invente nivel, cidade, modalidade ou caracteristica.\n"
        + "Cada ID deve aparecer exatamente uma vez em todo o plano. Nao omita nem duplique IDs.\n"
        + "Retorne SOMENTE um array JSON no formato: [{\"area\":\"Dados & IA\",\"vacancy_ids\":[\"ID1\",\"ID2\"]}].\n"
        + "Nao inclua datas, textos de post, links ou contagem. O Python atribuirá as datas.\n\n"
        + "OPORTUNIDADES: \n"
        + json.dumps(dados_compactos, ensure_ascii=False, separators=(",", ":"))
    )


def validar_plano(plano: Any, vagas: list[dict[str, str]], limite_grupos: int) -> list[dict[str, Any]]:
    plano = extrair_lista_resposta(plano, "Planejamento editorial")
    if not isinstance(plano, list) or not plano:
        raise ValueError("O planejamento precisa ser um array JSON nao vazio.")
    if len(plano) > limite_grupos:
        raise ValueError(f"O Gemini criou {len(plano)} grupos, mas ha somente {limite_grupos} datas disponiveis.")

    ids_validos = {v["ID"] for v in vagas}
    ids_usados: set[str] = set()
    grupos: list[dict[str, Any]] = []
    for indice, item in enumerate(plano, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Grupo {indice} nao e um objeto JSON.")
        area = str(item.get("area", "")).strip()
        ids = item.get("vacancy_ids")
        if not area or not isinstance(ids, list) or not ids:
            raise ValueError(f"Grupo {indice} precisa de area e vacancy_ids.")
        ids_norm = [str(x).strip() for x in ids]
        if any(not x for x in ids_norm) or len(ids_norm) != len(set(ids_norm)):
            raise ValueError(f"Grupo {indice} contem IDs vazios ou duplicados.")
        desconhecidos = set(ids_norm) - ids_validos
        if desconhecidos:
            raise ValueError(f"Grupo {indice} contem IDs desconhecidos: {', '.join(sorted(desconhecidos))}")
        repetidos = set(ids_norm) & ids_usados
        if repetidos:
            raise ValueError(f"IDs repetidos entre grupos: {', '.join(sorted(repetidos))}")
        ids_usados.update(ids_norm)
        grupos.append({"area": area, "vacancy_ids": ids_norm})

    faltantes = ids_validos - ids_usados
    if faltantes:
        raise ValueError(f"O plano omitiu {len(faltantes)} vaga(s): {', '.join(sorted(faltantes)[:12])}")
    return grupos


def montar_prompt_redacao(
    editorial: str,
    grupos: list[dict[str, Any]],
    vagas_por_id: dict[str, dict[str, str]],
) -> str:
    entrada = []
    for grupo in grupos:
        registros = [vagas_por_id[vaga_id] for vaga_id in grupo["vacancy_ids"]]
        entrada.append({
            "data": grupo["data"],
            "area": grupo["area"],
            "vacancy_ids": grupo["vacancy_ids"],
            "vagas": registros,
        })
    return (
        editorial
        + "\n\nETAPA 2 - REDACAO DOS POSTS DESTE LOTE.\n"
        + "Redija exatamente um post para cada grupo recebido. Nao junte, divida, troque ou remova grupos.\n"
        + "Preserve exatamente data, area e vacancy_ids de cada grupo.\n"
        + "Use apenas os dados e links das vagas daquele grupo. Nao invente informacoes.\n"
        + f"Cada texto deve ter no maximo {LIMITE} caracteres, em portugues brasileiro.\n"
        + "Responda SOMENTE com array JSON. Cada item deve ter os campos data, area, vacancy_ids e texto.\n"
        + "Nao gere HTML, widget ou Markdown fora dos textos.\n\nGRUPOS: \n"
        + json.dumps(entrada, ensure_ascii=False, separators=(",", ":"))
    )


def validar_lote_redigido(
    resposta: Any,
    grupos_esperados: list[dict[str, Any]],
    vagas_por_id: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    resposta = extrair_lista_resposta(resposta, "Redacao do lote")
    if not isinstance(resposta, list) or len(resposta) != len(grupos_esperados):
        raise ValueError("A resposta nao contem exatamente um post para cada grupo deste lote.")

    esperados = {g["data"]: g for g in grupos_esperados}
    recebidos: set[str] = set()
    resultado: list[dict[str, Any]] = []
    portal_norm = normalizar_link(PORTAL)

    for item in resposta:
        if not isinstance(item, dict):
            raise ValueError("Um post do lote nao e um objeto JSON.")
        data_post = str(item.get("data", "")).strip()
        grupo = esperados.get(data_post)
        if grupo is None or data_post in recebidos:
            raise ValueError(f"Data ausente, inesperada ou duplicada: {data_post}")
        area = str(item.get("area", "")).strip()
        ids = item.get("vacancy_ids")
        texto = str(item.get("texto", "")).strip()
        if area != grupo["area"]:
            raise ValueError(f"A area foi alterada no post de {data_post}.")
        if not isinstance(ids, list) or set(str(x).strip() for x in ids) != set(grupo["vacancy_ids"]):
            raise ValueError(f"Os IDs do post de {data_post} nao correspondem ao plano aprovado.")
        if not texto:
            raise ValueError(f"Texto vazio no post de {data_post}.")
        if len(texto) > LIMITE:
            raise ValueError(f"Post de {data_post} excede {LIMITE} caracteres.")

        links_no_texto = re.findall(r"https?://[^\s<>\]\[\"']+", texto)
        if not links_no_texto:
            raise ValueError(f"Post de {data_post} nao contem link de candidatura ou portal.")
        links_permitidos = {
            normalizar_link(vagas_por_id[vaga_id]["Link de Inscrição"])
            for vaga_id in grupo["vacancy_ids"]
        } | {portal_norm}
        for link in links_no_texto:
            link_norm = normalizar_link(link.rstrip(")"))
            if link_norm not in links_permitidos:
                raise ValueError(f"Link nao relacionado ao grupo de {data_post}: {link}")

        recebidos.add(data_post)
        ids_ordenados = list(grupo["vacancy_ids"])
        resultado.append({
            "data": data_post,
            "area": grupo["area"],
            "vacancy_ids": ids_ordenados,
            "qtd_vagas": len(ids_ordenados),
            "texto": texto,
            "status": "PENDING_VALIDATION",
            "publicado": False,
            "linkedin_post_urn": None,
            "attempts": 0,
            "last_error": None,
        })

    return sorted(resultado, key=lambda p: p["data"])


def salvar_json(caminho: Path, dados: Any) -> None:
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    if "--test-groq" in sys.argv:
        try:
            return testar_groq()
        except Exception as exc:
            print(f"ERRO NO TESTE GROQ: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    inicio_total = time.monotonic()
    try:
        vagas = carregar_vagas()
        hoje = datetime.now(TIMEZONE).date()
        # Execucao manual apos o dia 1 prepara o proximo mes completo.
        if hoje.day > 1:
            if hoje.month == 12:
                referencia_calendario = hoje.replace(year=hoje.year + 1, month=1, day=1)
            else:
                referencia_calendario = hoje.replace(month=hoje.month + 1, day=1)
        else:
            referencia_calendario = hoje
        dias_uteis, _, _, _ = gerar_calendario(referencia_calendario)
        datas = [dia.isoformat() for dia in dias_uteis]
        print(f"Calendario de publicacao: {datas[0]} ate {datas[-1]} ({len(datas)} dia(s) util(eis)).")
        editorial = carregar_prompt_editorial()

        if not vagas:
            salvar_json(ARQUIVO_SAIDA, [])
            print("Nenhuma vaga listada. posts.json criado vazio; Gemini nao foi chamado.")
            return 0
        if not datas:
            raise ValueError("Nao ha datas uteis disponiveis no calendario deste mes.")
        if not CHAVE and not (GROQ_CHAVE and GROQ_FREE_TIER_CONFIRMED):
            raise ErroGeracao(
                "Nenhum provedor de IA esta habilitado. Configure GEMINI_API_KEY ou "
                "GROQ_API_KEY com GROQ_FREE_TIER_CONFIRMED=true apos confirmar o plano Free."
            )

        if ARQUIVO_PARCIAL.exists():
            ARQUIVO_PARCIAL.unlink()

        print(
            f"Inicio da geracao: {len(vagas)} vagas, {len(datas)} datas. "
            f"Gemini={MODELO if CHAVE else 'desabilitado'}; "
            f"Groq={GROQ_MODELO if GROQ_CHAVE and GROQ_FREE_TIER_CONFIRMED else 'fallback bloqueado/ausente'}."
        )
        plano_prompt = montar_prompt_plano(editorial, vagas, len(datas))
        plano_bruto = chamar_gemini_json(plano_prompt, etapa="Planejamento editorial", temperatura=0.1)
        grupos = validar_plano(plano_bruto, vagas, len(datas))
        for indice, grupo in enumerate(grupos):
            grupo["data"] = datas[indice]

        vagas_por_id = {v["ID"]: v for v in vagas}
        lotes = [grupos[i:i + GRUPOS_POR_LOTE] for i in range(0, len(grupos), GRUPOS_POR_LOTE)]
        posts_gerados: list[dict[str, Any]] = []
        print(f"Plano validado: {len(grupos)} posts em {len(lotes)} lote(s) de redacao.")

        for indice, lote in enumerate(lotes, start=1):
            prompt_lote = montar_prompt_redacao(editorial, lote, vagas_por_id)
            erros_validacao: list[str] = []
            posts_lote: list[dict[str, Any]] | None = None
            for tentativa_validacao in range(1, MAX_TENTATIVAS_VALIDACAO + 1):
                prompt_tentativa = prompt_lote
                if erros_validacao:
                    prompt_tentativa += (
                        "\n\nCORRIJA A RESPOSTA ANTERIOR. Problema apontado pelo validador: "
                        + erros_validacao[-1]
                        + ". Preserve exatamente os grupos e IDs. Retorne somente o JSON correto."
                    )
                resposta = chamar_gemini_json(
                    prompt_tentativa,
                    etapa=f"Redacao lote {indice}/{len(lotes)}",
                    temperatura=0.65,
                )
                try:
                    posts_lote = validar_lote_redigido(resposta, lote, vagas_por_id)
                    break
                except ValueError as exc:
                    erros_validacao.append(str(exc))
                    print(f"Validacao do lote {indice}, tentativa {tentativa_validacao}: {exc}")
            if posts_lote is None:
                raise ErroGeracao(f"Lote {indice} nao passou na validacao editorial.")

            posts_gerados.extend(posts_lote)
            salvar_json(ARQUIVO_PARCIAL, posts_gerados)
            print(f"Checkpoint salvo: {len(posts_gerados)}/{len(grupos)} posts concluidos.")

        posts_finais = sorted(posts_gerados, key=lambda p: p["data"])
        ids_finais = [vaga_id for post in posts_finais for vaga_id in post["vacancy_ids"]]
        ids_origem = [vaga["ID"] for vaga in vagas]
        if len(ids_finais) != len(set(ids_finais)) or set(ids_finais) != set(ids_origem):
            raise ValueError("Validacao final falhou: ha IDs ausentes ou duplicados entre os posts.")
        if len({p["data"] for p in posts_finais}) != len(posts_finais):
            raise ValueError("Validacao final falhou: ha mais de um post na mesma data.")

        salvar_json(ARQUIVO_SAIDA, posts_finais)
        print(
            f"Geracao concluida: {len(posts_finais)} post(s), "
            f"{len(ids_finais)} vaga(s) associada(s), "
            f"{round(time.monotonic() - inicio_total, 1)}s no total."
        )
        return 0

    except Exception as exc:
        print(f"ERRO FATAL NA GERACAO: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
