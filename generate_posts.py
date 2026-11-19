"""Gera posts mensais da Digisystem em lotes pequenos e retomaveis."""

from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
import unicodedata
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
MAX_TENTATIVAS_REDE = 1  # Falhou uma vez? Aciona fallback sem repetir chamadas caras.
MAX_TENTATIVAS_GROQ = 2
MAX_TENTATIVAS_VALIDACAO = 2
GRUPOS_POR_LOTE = 2
TAMANHO_LOTE_PLANEJAMENTO = 25  # Planejamento nunca envia todas as vagas de uma vez.
GROQ_MAX_COMPLETION_TOKENS = 3072
GROQ_PLANEJAMENTO_TOKENS = 2048
GROQ_TPM_BUDGET = 6500  # Margem abaixo do limite observado de 8.000 TPM.
GROQ_CHARS_PER_TOKEN_ESTIMATE = 2.5  # Estimativa conservadora para controlar TPM.
GROQ_WINDOW_SECONDS = 60
_GROQ_REQUESTS: list[tuple[float, int]] = []
_GEMINI_FALLBACK_ACTIVE = False  # Circuit breaker: se Gemini falhar, Groq assume o restante da execucao.
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


def _aguardar_limite_tpm_groq(prompt: str, max_completion_tokens: int) -> None:
    """Aplica janela local conservadora para ficar abaixo do TPM gratuito observado."""
    estimativa = math.ceil(len(prompt) / GROQ_CHARS_PER_TOKEN_ESTIMATE) + max_completion_tokens
    if estimativa > GROQ_TPM_BUDGET:
        raise ErroGeracao(
            f"Solicitacao Groq ainda grande ({estimativa} tokens estimados). "
            "Reduza o lote de entrada antes de chamar o provedor."
        )

    while True:
        agora = time.monotonic()
        _GROQ_REQUESTS[:] = [
            (momento, tokens)
            for momento, tokens in _GROQ_REQUESTS
            if agora - momento < GROQ_WINDOW_SECONDS
        ]
        usados = sum(tokens for _, tokens in _GROQ_REQUESTS)
        if usados + estimativa <= GROQ_TPM_BUDGET:
            _GROQ_REQUESTS.append((agora, estimativa))
            return

        mais_antiga = min(momento for momento, _ in _GROQ_REQUESTS)
        espera = max(1, math.ceil(GROQ_WINDOW_SECONDS - (agora - mais_antiga)) + 1)
        print(f"Groq: aguardando {espera}s para respeitar o limite local de {GROQ_TPM_BUDGET} tokens/minuto.")
        time.sleep(espera)


def _chamar_groq_json(prompt: str, *, etapa: str, temperatura: float) -> Any:
    if not GROQ_CHAVE:
        raise ErroGeracao("GROQ_API_KEY nao foi encontrada nos Secrets do GitHub.")
    if not GROQ_FREE_TIER_CONFIRMED:
        raise ErroGeracao(
            "Fallback Groq bloqueado: confirme que a conta esta no plano Free e defina "
            "GROQ_FREE_TIER_CONFIRMED=true no GitHub."
        )

    max_saida = GROQ_PLANEJAMENTO_TOKENS if etapa.startswith("Planejamento editorial") else GROQ_MAX_COMPLETION_TOKENS
    prompt_groq = (
        prompt
        + "\n\nINSTRUCAO FINAL DE FORMATO: responda somente com um objeto JSON valido, "
        + 'sem Markdown, no formato {"items": [ ... ]}. A propriedade items deve conter '
        + "exatamente o array solicitado nas instrucoes anteriores. Nao omita nem altere IDs."
    )
    # GPT-OSS no Groq: use Structured Outputs estritos em vez do JSON Object Mode.
    # Isso evita depender da validação best-effort que falhou com json_validate_failed.
    if etapa.startswith("Planejamento editorial"):
        propriedades_item = {
            "area": {"type": "string"},
            "vacancy_ids": {"type": "array", "items": {"type": "string"}},
        }
        obrigatorios_item = ["area", "vacancy_ids"]
        nome_schema = "digisystem_vacancy_groups"
    else:
        propriedades_item = {
            "data": {"type": "string"},
            "area": {"type": "string"},
            "vacancy_ids": {"type": "array", "items": {"type": "string"}},
            "texto": {"type": "string"},
        }
        obrigatorios_item = ["data", "area", "vacancy_ids", "texto"]
        nome_schema = "digisystem_linkedin_posts"

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": propriedades_item,
                    "required": obrigatorios_item,
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    instrucoes = (
        "Voce e o assistente editorial da Digisystem. Use somente os dados recebidos; "
        "nao invente fatos. Preserve literalmente todos os IDs e datas. "
        "A resposta deve obedecer ao schema JSON estrito configurado na API.\n\n"
        + prompt_groq
    )
    corpo = {
        "model": GROQ_MODELO,
        # A documentacao do Groq recomenda concentrar as instrucoes no user message.
        "messages": [{"role": "user", "content": instrucoes}],
        "temperature": max(0.1, min(float(temperatura), 2.0)),
        "max_completion_tokens": max_saida,
        "reasoning_effort": "low",
        "include_reasoning": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": nome_schema,
                "strict": True,
                "schema": schema,
            },
        },
    }

    for tentativa in range(1, MAX_TENTATIVAS_GROQ + 1):
        _aguardar_limite_tpm_groq(prompt_groq, max_saida)
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
    """Usa Gemini como principal e Groq como fallback; ativa circuit breaker por execucao."""
    global _GEMINI_FALLBACK_ACTIVE
    erros: list[str] = []

    if CHAVE and not _GEMINI_FALLBACK_ACTIVE:
        try:
            return _chamar_gemini_json(prompt, etapa=etapa, temperatura=temperatura)
        except ErroGeracao as exc:
            erros.append(f"Gemini: {exc}")
            _GEMINI_FALLBACK_ACTIVE = True
            print(
                f"{etapa}: Gemini indisponivel; ativando fallback Groq para o restante desta execucao.",
                file=sys.stderr,
            )
    elif not CHAVE:
        erros.append("Gemini: GEMINI_API_KEY ausente.")
        _GEMINI_FALLBACK_ACTIVE = True
    else:
        print(f"{etapa}: circuit breaker ativo; usando Groq sem nova chamada ao Gemini.")

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
        "max_completion_tokens": 128,
        "reasoning_effort": "low",
        "include_reasoning": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "groq_connection_test",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"status": {"type": "string"}},
                    "required": ["status"],
                    "additionalProperties": False,
                },
            },
        },
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
    """Prompt compacto: somente ID e titulo; detalhes e links nao sao necessarios no plano."""
    dados_compactos = [{"id": v["ID"], "vaga": v["Vaga"]} for v in vagas]
    return (
        "ETAPA 1 - CLASSIFICACAO EDITORIAL DE UM LOTE DE VAGAS DA DIGISYSTEM.\n"
        "Agrupe por area funcional, nunca por setor ou cliente. Categorias de referencia: "
        "Desenvolvimento, Dados & IA, Suporte, Infraestrutura, Arquitetura, Audio & Video, "
        "Produto & Comercial, Saude - Tasy, Vagas Afirmativas PCD, Governanca & Processos "
        "e outras areas funcionais quando necessario.\n"
        "Nao invente nivel, cidade, modalidade ou caracteristica. Use apenas os titulos.\n"
        "Vagas de grande volume do mesmo tipo (20 ou mais) devem ser divididas em 2 ou 3 grupos, "
        "preferencialmente por nivel quando o titulo indicar. Nao crie um post por vaga.\n"
        f"O calendario completo tem {total_datas} datas; este e apenas um lote parcial. "
        "Agrupe todas as vagas deste lote e devolva cada ID exatamente uma vez.\n"
        'Responda somente com um objeto JSON valido no formato '
        '{"items":[{"area":"Dados & IA","vacancy_ids":["ID1","ID2"]}]}. '
        "A chave items deve conter o array de grupos. Nao use array na raiz.\n"
        "Nao inclua texto, links, datas, detalhes ou contagens.\n"
        "VAGAS: "
        + json.dumps(dados_compactos, ensure_ascii=False, separators=(",", ":"))
    )


def inferir_area_por_titulo(titulo: str) -> str:
    """Classificacao deterministica conservadora para recuperar IDs omitidos pelo modelo."""
    texto = unicodedata.normalize("NFKD", str(titulo or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch)).lower()

    regras = [
        (("governanca", "compliance", "processos", "auditoria", "controles internos", "risco"), "Governanca & Processos"),
        (("dados", "data", "power bi", "business intelligence", "analytics", "cientista", "machine learning", "inteligencia artificial", "ia "), "Dados & IA"),
        (("desenvolvedor", "desenvolvimento", "programador", "software", "full stack", "frontend", "front-end", "backend", "back-end", "java", "python", "react", ".net", "node.js"), "Desenvolvimento"),
        (("suporte", "service desk", "help desk", "field service", "atendimento tecnico"), "Suporte"),
        (("infraestrutura", "infra", "cloud", "nuvem", "devops", "redes", "network", "linux", "windows server", "administrador de banco", "dba", "banco de dados"), "Infraestrutura"),
        (("arquiteto", "arquitetura", "solution architect", "solucoes"), "Arquitetura"),
        (("audio", "video", "audiovisual", "a&v", "multimidia"), "Audio & Video"),
        (("tasy", "saude", "hospitalar", "enfermagem", "medico"), "Saude - Tasy"),
        (("pcd", "pessoa com deficiencia", "afirmativa"), "Vagas Afirmativas PCD"),
        (("produto", "product owner", "product manager", "comercial", "vendas", "ux", "ui", "designer"), "Produto & Comercial"),
    ]
    for termos, area in regras:
        if any(termo in texto for termo in termos):
            return area
    return "Outras areas funcionais"


def validar_plano(plano: Any, vagas: list[dict[str, str]], limite_grupos: int) -> list[dict[str, Any]]:
    """Valida o plano e repara IDs repetidos/omitidos sem perder oportunidades.

    Quando o modelo coloca o mesmo ID em grupos diferentes, o Python mantém uma
    única ocorrência: prefere o grupo cuja área coincide com a classificação
    determinística pelo título; se não houver correspondência, mantém a primeira.
    """
    plano = extrair_lista_resposta(plano, "Planejamento editorial")
    if not isinstance(plano, list) or not plano:
        raise ValueError("O planejamento precisa ser um array JSON nao vazio.")

    vagas_por_id = {v["ID"]: v for v in vagas}
    ids_validos = set(vagas_por_id)
    grupos: list[dict[str, Any]] = []

    # Primeiro valida a estrutura e remove repeticoes internas ao mesmo grupo.
    for indice, item in enumerate(plano, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Grupo {indice} nao e um objeto JSON.")
        area = str(item.get("area", "")).strip()
        ids = item.get("vacancy_ids")
        if not area or not isinstance(ids, list) or not ids:
            raise ValueError(f"Grupo {indice} precisa de area e vacancy_ids.")
        ids_norm = [str(x).strip() for x in ids]
        if any(not x for x in ids_norm):
            raise ValueError(f"Grupo {indice} contem ID vazio.")
        desconhecidos = set(ids_norm) - ids_validos
        if desconhecidos:
            raise ValueError(f"Grupo {indice} contem IDs desconhecidos: {', '.join(sorted(desconhecidos))}")
        ids_unicos = list(dict.fromkeys(ids_norm))
        if len(ids_unicos) != len(ids_norm):
            repetidos_internos = len(ids_norm) - len(ids_unicos)
            print(f"AVISO: grupo {indice} repetiu {repetidos_internos} ID(s) internamente; Python removeu as repeticoes.")
        grupos.append({"area": area, "vacancy_ids": ids_unicos})

    # Resolve IDs que o modelo distribuiu em mais de um grupo.
    ocorrencias: dict[str, list[int]] = {}
    for indice, grupo in enumerate(grupos):
        for vaga_id in grupo["vacancy_ids"]:
            ocorrencias.setdefault(vaga_id, []).append(indice)

    removidos_por_grupo: dict[int, set[str]] = {}
    conflitos: list[str] = []
    for vaga_id, indices in ocorrencias.items():
        if len(indices) <= 1:
            continue
        area_deterministica = _chave_area(inferir_area_por_titulo(vagas_por_id[vaga_id]["Vaga"]))
        correspondentes = [i for i in indices if _chave_area(grupos[i]["area"]) == area_deterministica]
        escolhido = correspondentes[0] if correspondentes else indices[0]
        for indice in indices:
            if indice != escolhido:
                removidos_por_grupo.setdefault(indice, set()).add(vaga_id)
        conflitos.append(f"{vaga_id} (mantido em '{grupos[escolhido]['area']}')")

    if conflitos:
        for indice, ids_remover in removidos_por_grupo.items():
            grupos[indice]["vacancy_ids"] = [
                vaga_id for vaga_id in grupos[indice]["vacancy_ids"] if vaga_id not in ids_remover
            ]
        print(
            f"AVISO: o modelo repetiu {len(conflitos)} ID(s) entre grupos; "
            "Python manteve cada vaga uma unica vez. " + "; ".join(conflitos[:8])
        )

    grupos = [grupo for grupo in grupos if grupo["vacancy_ids"]]
    ids_usados = {vaga_id for grupo in grupos for vaga_id in grupo["vacancy_ids"]}

    # Recupera omissoes com classificacao deterministica pelo titulo.
    faltantes = sorted(ids_validos - ids_usados)
    if faltantes:
        recuperados_por_area: dict[str, list[str]] = {}
        for vaga_id in faltantes:
            area = inferir_area_por_titulo(vagas_por_id[vaga_id]["Vaga"])
            recuperados_por_area.setdefault(area, []).append(vaga_id)
        for area, ids_recuperados in recuperados_por_area.items():
            grupo_existente = next(
                (g for g in grupos if _chave_area(g["area"]) == _chave_area(area)), None
            )
            if grupo_existente:
                grupo_existente["vacancy_ids"].extend(ids_recuperados)
            else:
                grupos.append({"area": area, "vacancy_ids": ids_recuperados})
        print(
            f"AVISO: o modelo omitiu {len(faltantes)} vaga(s); Python recuperou todos os IDs "
            f"por classificacao deterministica: {', '.join(faltantes[:8])}."
        )

    todos_ids = [vaga_id for grupo in grupos for vaga_id in grupo["vacancy_ids"]]
    if len(todos_ids) != len(set(todos_ids)) or set(todos_ids) != ids_validos:
        raise ValueError("A validacao do planejamento nao preservou exatamente todos os IDs de vagas.")
    if len(grupos) > limite_grupos:
        raise ValueError(
            f"A validacao produziu {len(grupos)} grupos, mas ha somente {limite_grupos} datas disponiveis."
        )
    return grupos



def _chave_area(area: str) -> str:
    texto = unicodedata.normalize("NFKD", str(area or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def _chave_titulo(titulo: str) -> str:
    texto = unicodedata.normalize("NFKD", str(titulo or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()



def extrair_localidade(detalhes: str) -> str:
    """Extrai apenas o sufixo Cidade, UF; nunca inclui modalidade CLT/PJ."""
    texto = str(detalhes or "").strip()
    encontrados = re.findall(r"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{1,45},\s*[A-Z]{2})\s*$", texto)
    return encontrados[-1].strip() if encontrados else ""


def hashtags_por_area(area: str) -> str:
    chave = _chave_area(area)
    regras = [
        (("dados", "governanca"), "#Dados #DataScience #Governanca"),
        (("desenvolvimento", "dev"), "#Desenvolvimento #Dev #TI"),
        (("suporte",), "#Suporte #TI #Tecnologia"),
        (("infraestrutura", "infra"), "#Infraestrutura #Cloud #TI"),
        (("audio", "video", "audiovisual"), "#AudioVisual #AV #Tecnologia"),
        (("saude", "tasy"), "#Saude #Tasy #Tecnologia"),
        (("produto", "comercial", "ux"), "#Produto #Comercial #Tecnologia"),
        (("arquitetura",), "#Arquitetura #TI #Tecnologia"),
    ]
    for termos, tags in regras:
        if any(termo in chave for termo in termos):
            return tags
    return "#Carreira #Tecnologia #Oportunidades"


def montar_post_deterministico(
    grupo: dict[str, Any],
    vagas_por_id: dict[str, dict[str, str]],
) -> str:
    """Gera um post de contingencia com links exclusivamente vindos do CSV."""
    area = str(grupo["area"]).strip()
    ids = list(grupo["vacancy_ids"])
    registros = [vagas_por_id[vaga_id] for vaga_id in ids]
    localidades = [extrair_localidade(v.get("Detalhes", "")) for v in registros]
    localidades_unicas = sorted({local for local in localidades if local})

    linhas = [f"A Digisystem abriu oportunidades para profissionais de {area}.", ""]
    if len(localidades_unicas) == 1:
        linhas.extend([f"📍 {localidades_unicas[0]}", ""])

    for vaga, localidade in zip(registros, localidades):
        titulo = str(vaga["Vaga"]).strip()
        if len(localidades_unicas) > 1 and localidade:
            titulo += f" — {localidade}"
        linhas.append(f"{titulo} → {normalizar_link(vaga['Link de Inscrição'])}")

    linhas.extend([
        "",
        "#Vagas #Oportunidade #Emprego #Digier " + hashtags_por_area(area),
        "Compartilhe com quem possa se interessar! 🙌",
    ])
    return "\n".join(linhas).strip()


def dividir_grupos_por_limite_caracteres(
    grupos: list[dict[str, Any]],
    vagas_por_id: dict[str, dict[str, str]],
    limite_datas: int,
) -> list[dict[str, Any]]:
    """Divide previamente grupos cujo post de contingencia ultrapassaria 2.900 caracteres."""
    resultado: list[dict[str, Any]] = []
    for grupo in grupos:
        parte: list[str] = []
        for vaga_id in grupo["vacancy_ids"]:
            candidato = parte + [vaga_id]
            teste = {"area": grupo["area"], "vacancy_ids": candidato}
            texto = montar_post_deterministico(teste, vagas_por_id)
            if len(texto) <= LIMITE:
                parte = candidato
                continue

            if not parte:
                raise ErroGeracao(
                    f"A vaga {vaga_id} sozinha ultrapassa o limite de {LIMITE} caracteres no post de contingencia."
                )

            resultado.append({"area": grupo["area"], "vacancy_ids": parte})
            parte = [vaga_id]
            texto_individual = montar_post_deterministico(
                {"area": grupo["area"], "vacancy_ids": parte}, vagas_por_id
            )
            if len(texto_individual) > LIMITE:
                raise ErroGeracao(
                    f"A vaga {vaga_id} sozinha ultrapassa o limite de {LIMITE} caracteres no post de contingencia."
                )

        if parte:
            resultado.append({"area": grupo["area"], "vacancy_ids": parte})

    ids_antes = [x for grupo in grupos for x in grupo["vacancy_ids"]]
    ids_depois = [x for grupo in resultado for x in grupo["vacancy_ids"]]
    if len(ids_depois) != len(set(ids_depois)) or set(ids_depois) != set(ids_antes):
        raise ValueError("Divisao por caracteres perdeu ou duplicou IDs de vagas.")
    if len(resultado) > limite_datas:
        raise ErroGeracao(
            f"A divisao por limite de caracteres produziu {len(resultado)} posts, "
            f"mas existem apenas {limite_datas} datas disponiveis."
        )
    if len(resultado) > len(grupos):
        print(
            f"Divisao preventiva por caracteres: {len(grupos)} grupo(s) viraram "
            f"{len(resultado)} post(s), preservando todos os links."
        )
    return resultado

def mesclar_e_dividir_grupos(
    grupos_lotes: list[dict[str, Any]],
    vagas: list[dict[str, str]],
    limite_grupos: int,
) -> list[dict[str, Any]]:
    """Une areas repetidas entre lotes e divide grande volume do mesmo titulo em 2-3 posts."""
    vagas_por_id = {v["ID"]: v for v in vagas}
    mesclados: dict[str, dict[str, Any]] = {}
    for grupo in grupos_lotes:
        chave = _chave_area(grupo["area"])
        if chave not in mesclados:
            mesclados[chave] = {"area": grupo["area"], "vacancy_ids": []}
        mesclados[chave]["vacancy_ids"].extend(grupo["vacancy_ids"])

    resultado: list[dict[str, Any]] = []
    for grupo in mesclados.values():
        por_titulo: dict[str, list[str]] = {}
        for vaga_id in grupo["vacancy_ids"]:
            chave = _chave_titulo(vagas_por_id[vaga_id]["Vaga"])
            por_titulo.setdefault(chave, []).append(vaga_id)

        ids_alto_volume = {vaga_id for ids in por_titulo.values() if len(ids) >= 20 for vaga_id in ids}
        restantes = [vaga_id for vaga_id in grupo["vacancy_ids"] if vaga_id not in ids_alto_volume]
        if restantes:
            if len(restantes) >= 40:
                partes_restantes = 3
            elif len(restantes) > 20:
                partes_restantes = 2
            else:
                partes_restantes = 1
            tamanho_restantes = math.ceil(len(restantes) / partes_restantes)
            for inicio in range(0, len(restantes), tamanho_restantes):
                resultado.append({"area": grupo["area"], "vacancy_ids": restantes[inicio:inicio + tamanho_restantes]})

        for ids in por_titulo.values():
            if len(ids) < 20:
                continue
            partes = 3 if len(ids) >= 40 else 2
            tamanho = math.ceil(len(ids) / partes)
            for inicio in range(0, len(ids), tamanho):
                resultado.append({"area": grupo["area"], "vacancy_ids": ids[inicio:inicio + tamanho]})

    todos_ids = [vaga_id for grupo in resultado for vaga_id in grupo["vacancy_ids"]]
    ids_origem = [vaga["ID"] for vaga in vagas]
    if len(todos_ids) != len(set(todos_ids)) or set(todos_ids) != set(ids_origem):
        raise ValueError("Mesclagem do planejamento perdeu ou duplicou IDs de vagas.")
    if len(resultado) > limite_grupos:
        raise ValueError(
            f"O planejamento produziu {len(resultado)} posts, mas o calendario tem "
            f"somente {limite_grupos} datas disponiveis."
        )
    return resultado


def planejar_grupos_em_lotes(vagas: list[dict[str, str]], total_datas: int) -> list[dict[str, Any]]:
    """Planeja em lotes pequenos para evitar exceder o TPM do provedor gratuito."""
    grupos_lotes: list[dict[str, Any]] = []
    total_lotes = math.ceil(len(vagas) / TAMANHO_LOTE_PLANEJAMENTO)
    for indice, inicio in enumerate(range(0, len(vagas), TAMANHO_LOTE_PLANEJAMENTO), start=1):
        lote = vagas[inicio:inicio + TAMANHO_LOTE_PLANEJAMENTO]
        prompt = montar_prompt_plano("", lote, total_datas)
        resposta = chamar_gemini_json(
            prompt,
            etapa=f"Planejamento editorial lote {indice}/{total_lotes}",
            temperatura=0.1,
        )
        grupos = validar_plano(resposta, lote, len(lote))
        grupos_lotes.extend(grupos)
        print(f"Planejamento lote {indice}/{total_lotes} validado: {len(lote)} vagas em {len(grupos)} grupo(s).")

    resultado = mesclar_e_dividir_grupos(grupos_lotes, vagas, total_datas)
    return validar_plano(resultado, vagas, total_datas)


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
        + 'Responda SOMENTE com um objeto JSON valido no formato '
        + '{"items":[{"data":"AAAA-MM-DD","area":"Dados & IA","vacancy_ids":["ID1"],"texto":"..."}]}. '
        + "A chave items deve conter o array de posts. Nao use array na raiz.\n"
        + "Nao gere HTML, widget ou Markdown fora dos textos.\n\nGRUPOS: \n"
        + json.dumps(entrada, ensure_ascii=False, separators=(",", ":"))
    )



def estimar_tokens_requisicao_groq(prompt: str, max_saida: int = GROQ_MAX_COMPLETION_TOKENS) -> int:
    prompt_com_formato = prompt + "\nINSTRUCAO FINAL: retorne somente JSON valido no objeto {\"items\": [...]} sem alterar IDs."
    return math.ceil(len(prompt_com_formato) / GROQ_CHARS_PER_TOKEN_ESTIMATE) + max_saida


def dividir_grupos_por_orcamento(
    grupos: list[dict[str, Any]],
    vagas_por_id: dict[str, dict[str, str]],
    editorial: str,
    limite_datas: int,
) -> list[dict[str, Any]]:
    """Divide grupos grandes em subposts sem perder IDs, respeitando o TPM estimado."""
    expandidos: list[dict[str, Any]] = []
    data_teste = "2000-01-01"

    for grupo in grupos:
        ids = list(grupo["vacancy_ids"])
        parte_atual: list[str] = []

        for vaga_id in ids:
            candidato_ids = parte_atual + [vaga_id]
            candidato = [{
                "data": grupo.get("data", data_teste),
                "area": grupo["area"],
                "vacancy_ids": candidato_ids,
            }]
            estimativa = estimar_tokens_requisicao_groq(
                montar_prompt_redacao(editorial, candidato, vagas_por_id)
            )

            if estimativa <= GROQ_TPM_BUDGET:
                parte_atual = candidato_ids
                continue

            if not parte_atual:
                raise ErroGeracao(
                    f"A vaga {vaga_id} da area {grupo['area']} excede sozinha o limite seguro "
                    f"de tokens ({estimativa} estimados). Reduza os detalhes dessa vaga na entrada."
                )

            expandidos.append({"area": grupo["area"], "vacancy_ids": parte_atual})
            parte_atual = [vaga_id]
            individual = [{
                "data": grupo.get("data", data_teste),
                "area": grupo["area"],
                "vacancy_ids": parte_atual,
            }]
            estimativa_individual = estimar_tokens_requisicao_groq(
                montar_prompt_redacao(editorial, individual, vagas_por_id)
            )
            if estimativa_individual > GROQ_TPM_BUDGET:
                raise ErroGeracao(
                    f"A vaga {vaga_id} da area {grupo['area']} excede sozinha o limite seguro "
                    f"de tokens ({estimativa_individual} estimados)."
                )

        if parte_atual:
            expandidos.append({"area": grupo["area"], "vacancy_ids": parte_atual})

    ids_originais = [vaga_id for grupo in grupos for vaga_id in grupo["vacancy_ids"]]
    ids_expandidos = [vaga_id for grupo in expandidos for vaga_id in grupo["vacancy_ids"]]
    if len(ids_expandidos) != len(set(ids_expandidos)) or set(ids_expandidos) != set(ids_originais):
        raise ValueError("A divisao por tokens perdeu ou duplicou IDs de vagas.")
    if len(expandidos) > limite_datas:
        raise ErroGeracao(
            f"A divisao por limite de tokens produziu {len(expandidos)} posts, "
            f"mas existem apenas {limite_datas} datas disponiveis."
        )
    if len(expandidos) > len(grupos):
        print(
            f"Divisao automatica por limite de tokens: {len(grupos)} grupo(s) viraram "
            f"{len(expandidos)} post(s), sem perder vagas."
        )
    return expandidos


def montar_lotes_redacao(
    grupos: list[dict[str, Any]],
    vagas_por_id: dict[str, dict[str, str]],
    editorial: str,
) -> list[list[dict[str, Any]]]:
    """Monta lotes de um ou dois posts respeitando o orcamento TPM estimado do Groq."""
    lotes: list[list[dict[str, Any]]] = []
    atual: list[dict[str, Any]] = []
    for grupo in grupos:
        candidato = atual + [grupo]
        estimativa = estimar_tokens_requisicao_groq(
            montar_prompt_redacao(editorial, candidato, vagas_por_id)
        )
        if estimativa <= GROQ_TPM_BUDGET:
            atual = candidato
            if len(atual) >= GRUPOS_POR_LOTE:
                lotes.append(atual)
                atual = []
            continue

        if atual:
            lotes.append(atual)
            atual = []

        estimativa_individual = estimar_tokens_requisicao_groq(
            montar_prompt_redacao(editorial, [grupo], vagas_por_id)
        )
        if estimativa_individual > GROQ_TPM_BUDGET:
            raise ErroGeracao(
                f"Grupo da area {grupo['area']} continua acima do limite seguro "
                f"({estimativa_individual} tokens estimados) apos divisao automatica."
            )
        atual = [grupo]

    if atual:
        lotes.append(atual)
    return lotes

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

    for item in resposta:
        if not isinstance(item, dict):
            raise ValueError("Um post do lote nao e um objeto JSON.")
        data_post = str(item.get("data", "")).strip()
        grupo = esperados.get(data_post)
        if grupo is None or data_post in recebidos:
            raise ValueError(f"Data ausente, inesperada ou duplicada: {data_post}")

        area_esperada = grupo["area"]
        ids_esperados = list(grupo["vacancy_ids"])
        area_modelo = str(item.get("area", "")).strip()
        ids_modelo = item.get("vacancy_ids")
        texto = str(item.get("texto", "")).strip()
        motivos_reparo: list[str] = []

        if area_modelo != area_esperada:
            motivos_reparo.append("area alterada pelo modelo")
        if not isinstance(ids_modelo, list) or set(str(x).strip() for x in ids_modelo) != set(ids_esperados):
            motivos_reparo.append("IDs alterados ou omitidos pelo modelo")
        if not texto:
            motivos_reparo.append("texto vazio")
        if len(texto) > LIMITE:
            motivos_reparo.append(f"texto excede {LIMITE} caracteres")

        links_no_texto = re.findall(r"https?://[^\s<>\]\[\"']+", texto)
        links_permitidos = {
            normalizar_link(vagas_por_id[vaga_id]["Link de Inscrição"])
            for vaga_id in ids_esperados
        }
        links_encontrados = {normalizar_link(link.rstrip(").,;!?")) for link in links_no_texto}
        links_nao_permitidos = links_encontrados - links_permitidos - {normalizar_link(PORTAL)}
        links_faltantes = links_permitidos - links_encontrados
        if links_nao_permitidos:
            motivos_reparo.append("o modelo incluiu link de outra vaga")
        if links_faltantes:
            motivos_reparo.append("o modelo omitiu link(s) direto(s) de candidatura")

        # Reparo deterministico: nao repete chamada paga e nao confia em links produzidos pela IA.
        if motivos_reparo:
            texto = montar_post_deterministico(
                {"area": area_esperada, "vacancy_ids": ids_esperados}, vagas_por_id
            )
            print(
                f"REPARO AUTOMATICO {data_post}: {', '.join(motivos_reparo)}. "
                "Texto reconstruido pelo Python com titulos e links originais."
            )

        if len(texto) > LIMITE:
            raise ValueError(
                f"Post de contingencia de {data_post} excede {LIMITE} caracteres; "
                "o grupo precisa ser dividido antes da redacao."
            )

        # Verificacao final independente do modelo.
        links_finais = {
            normalizar_link(link.rstrip(").,;!?"))
            for link in re.findall(r"https?://[^\s<>\]\[\"']+", texto)
        }
        if not links_permitidos.issubset(links_finais) or (links_finais - links_permitidos - {normalizar_link(PORTAL)}):
            raise ValueError(f"Reparo deterministico de {data_post} nao preservou exatamente os links permitidos.")

        recebidos.add(data_post)
        resultado.append({
            "data": data_post,
            "area": area_esperada,
            "vacancy_ids": ids_esperados,
            "qtd_vagas": len(ids_esperados),
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
        grupos_planejados = planejar_grupos_em_lotes(vagas, len(datas))
        vagas_por_id = {v["ID"]: v for v in vagas}
        grupos = dividir_grupos_por_orcamento(
            grupos_planejados, vagas_por_id, editorial, len(datas)
        )
        grupos = dividir_grupos_por_limite_caracteres(grupos, vagas_por_id, len(datas))
        for indice, grupo in enumerate(grupos):
            grupo["data"] = datas[indice]

        lotes = montar_lotes_redacao(grupos, vagas_por_id, editorial)
        posts_gerados: list[dict[str, Any]] = []
        print(f"Plano validado: {len(grupos)} posts em {len(lotes)} lote(s) de redacao com limite de tokens.")

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
