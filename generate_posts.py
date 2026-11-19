"""Gera os posts mensais da Digisystem com IDs de vagas rastreáveis.

Responsabilidades:
- Ler as vagas coletadas pelo vagas_pipeline.py.
- Solicitar ao Gemini apenas a redação e a organização editorial.
- Validar datas, IDs e links com Python.
- Calcular qtd_vagas sem confiar no número informado pela IA.
- Salvar posts.json em estado DRAFT, ainda não publicados.

A verificação de disponibilidade e a publicação são etapas separadas.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from vagas_pipeline import gerar_calendario

MODELO = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
CHAVE = os.getenv("GEMINI_API_KEY", "").strip()
PORTAL = "https://jobs.recrutei.com.br/digisystem"
LIMITE = 2900
TIMEZONE = ZoneInfo("America/Sao_Paulo")
MAX_TENTATIVAS_GEMINI = 3
TIMEOUT_GEMINI = 120
ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_PROMPT = Path("prompt_pronto.txt")
ARQUIVO_SAIDA = Path("posts.json")


class ErroGeracao(RuntimeError):
    """Falha na geração, com indicação se uma nova tentativa pode ajudar."""

    def __init__(self, mensagem: str, *, retryable: bool = False, retry_after: int | None = None):
        super().__init__(mensagem)
        self.retryable = retryable
        self.retry_after = retry_after


def normalizar_link(link: str) -> str:
    """Remove pontuação final e normaliza host/caminho para comparação."""
    valor = (link or "").strip().rstrip(".,;:!?")
    partes = urlsplit(valor)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        return valor.rstrip("/")
    caminho = partes.path.rstrip("/")
    return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), caminho, partes.query, ""))


def carregar_vagas() -> list[dict[str, str]]:
    if not ARQUIVO_CSV.exists():
        raise FileNotFoundError(f"Arquivo obrigatório não encontrado: {ARQUIVO_CSV}")

    df = pd.read_csv(ARQUIVO_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    obrigatorias = {"ID", "Vaga", "Detalhes", "Link de Inscrição"}
    faltantes = obrigatorias - set(df.columns)
    if faltantes:
        raise ValueError(f"Colunas ausentes em {ARQUIVO_CSV}: {', '.join(sorted(faltantes))}")

    vagas: list[dict[str, str]] = []
    ids_vistos: set[str] = set()
    for linha, registro in enumerate(df.to_dict(orient="records"), start=2):
        vaga_id = str(registro.get("ID", "")).strip()
        titulo = str(registro.get("Vaga", "")).strip()
        link = normalizar_link(str(registro.get("Link de Inscrição", "")))
        if not vaga_id or not titulo or not link:
            raise ValueError(f"Registro incompleto na linha {linha} de {ARQUIVO_CSV}")
        if vaga_id in ids_vistos:
            raise ValueError(f"ID de vaga duplicado na planilha: {vaga_id}")
        ids_vistos.add(vaga_id)
        registro["ID"] = vaga_id
        registro["Vaga"] = titulo
        registro["Detalhes"] = str(registro.get("Detalhes", "")).strip()
        registro["Link de Inscrição"] = link
        vagas.append(registro)

    return vagas


def validar_posts(
    posts: Any,
    datas_ok: set[str],
    vagas: list[dict[str, str]],
) -> list[dict[str, Any]]:
    if not isinstance(posts, list):
        raise ValueError("A resposta do Gemini precisa ser um array JSON.")

    if not vagas:
        if posts:
            raise ValueError("Não existem vagas coletadas; a resposta precisa ser um array vazio.")
        return []
    if not posts:
        raise ValueError("Existem vagas coletadas, mas o Gemini não criou nenhum post.")

    vagas_por_id = {vaga["ID"]: vaga for vaga in vagas}
    portal_normalizado = normalizar_link(PORTAL)
    datas_vistas: set[str] = set()
    ids_usados: set[str] = set()
    resultado: list[dict[str, Any]] = []

    for indice, post in enumerate(posts, start=1):
        if not isinstance(post, dict):
            raise ValueError(f"Post {indice} não é um objeto JSON.")

        data_post = str(post.get("data", "")).strip()
        area = str(post.get("area", "")).strip()
        texto = str(post.get("texto", "")).strip()
        ids = post.get("vacancy_ids")

        if data_post not in datas_ok:
            raise ValueError(f"Data inválida ou fora do calendário no post {indice}: {data_post}")
        if data_post in datas_vistas:
            raise ValueError(f"Mais de um post foi programado para {data_post}")
        if not area:
            raise ValueError(f"Área não informada no post {indice}")
        if not texto:
            raise ValueError(f"Texto vazio no post {indice}")
        if len(texto) > LIMITE:
            raise ValueError(f"Post {data_post} excede {LIMITE} caracteres: {len(texto)}")
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"Post {data_post} precisa conter vacancy_ids não vazios.")
        if any(not isinstance(vaga_id, str) or not vaga_id.strip() for vaga_id in ids):
            raise ValueError(f"Post {data_post} contém ID de vaga inválido.")
        if len(ids) != len(set(ids)):
            raise ValueError(f"Post {data_post} repete um vacancy_id.")

        ids_normalizados = [vaga_id.strip() for vaga_id in ids]
        desconhecidos = set(ids_normalizados) - set(vagas_por_id)
        if desconhecidos:
            raise ValueError(
                f"Post {data_post} contém IDs que não existem na coleta: {', '.join(sorted(desconhecidos))}"
            )

        repetidos_em_outros_posts = set(ids_normalizados) & ids_usados
        if repetidos_em_outros_posts:
            raise ValueError(
                f"Vagas repetidas em posts diferentes: {', '.join(sorted(repetidos_em_outros_posts))}"
            )

        links_do_post = {
            normalizar_link(vagas_por_id[vaga_id]["Link de Inscrição"])
            for vaga_id in ids_normalizados
        }
        links_permitidos = links_do_post | {portal_normalizado}
        links_no_texto = re.findall(r"https?://[^\s<>\]\[\"']+", texto)
        if not links_no_texto:
            raise ValueError(f"Post {data_post} não contém nenhum link de inscrição ou portal.")
        for link in links_no_texto:
            link_normalizado = normalizar_link(link).rstrip(")")
            if link_normalizado not in links_permitidos:
                raise ValueError(
                    f"Link não relacionado às vagas do post {data_post}: {link}"
                )

        datas_vistas.add(data_post)
        ids_usados.update(ids_normalizados)
        # A quantidade é calculada pelo Python, nunca aceita diretamente da IA.
        resultado.append({
            "data": data_post,
            "area": area,
            "vacancy_ids": ids_normalizados,
            "qtd_vagas": len(ids_normalizados),
            "texto": texto,
            "status": "PENDING_VALIDATION",
            "publicado": False,
            "linkedin_post_urn": None,
            "attempts": 0,
            "last_error": None,
        })

    vagas_nao_publicadas = set(vagas_por_id) - ids_usados
    if vagas_nao_publicadas:
        raise ValueError(
            "O Gemini deixou vagas de fora do calendário: "
            + ", ".join(sorted(vagas_nao_publicadas))
        )

    return sorted(resultado, key=lambda item: item["data"])


def chamar_gemini(prompt: str) -> list:
    if not CHAVE:
        raise ErroGeracao("GEMINI_API_KEY não foi configurada nos Secrets do GitHub.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO}:generateContent"
    corpo = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.9,
        },
    }

    try:
        resposta = requests.post(
            url,
            headers={"x-goog-api-key": CHAVE},
            json=corpo,
            timeout=TIMEOUT_GEMINI,
        )
    except requests.RequestException as exc:
        raise ErroGeracao(
            f"Falha de conexão com Gemini: {exc}", retryable=True
        ) from exc

    if resposta.status_code in {408, 429, 500, 502, 503, 504}:
        retry_after = resposta.headers.get("Retry-After", "")
        try:
            espera = min(max(int(retry_after), 1), 60) if retry_after else None
        except ValueError:
            espera = None
        raise ErroGeracao(
            f"Gemini retornou HTTP {resposta.status_code}: {resposta.text[:300]}",
            retryable=True,
            retry_after=espera,
        )

    if resposta.status_code != 200:
        raise ErroGeracao(
            f"Gemini retornou HTTP {resposta.status_code}: {resposta.text[:500]}"
        )

    try:
        dados = resposta.json()
        texto = dados["candidates"][0]["content"]["parts"][0]["text"]
        resultado = json.loads(texto)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ErroGeracao(f"Resposta JSON do Gemini inválida: {exc}", retryable=True) from exc

    if not isinstance(resultado, list):
        raise ErroGeracao("O Gemini não retornou um array JSON.", retryable=True)
    return resultado


def montar_prompt(datas: list[str]) -> str:
    if not ARQUIVO_PROMPT.exists():
        raise FileNotFoundError(f"Arquivo obrigatório não encontrado: {ARQUIVO_PROMPT}")

    base = ARQUIVO_PROMPT.read_text(encoding="utf-8")
    extra = (
        "\n\n=== REGRAS TÉCNICAS DE ENTREGA (PREVALECEM SOBRE O ITEM 8) ===\n"
        "Não gere HTML, widget ou Markdown. Responda SOMENTE com um array JSON válido.\n"
        "Cada item deve conter exatamente os campos: data, area, vacancy_ids e texto.\n"
        '{"data":"YYYY-MM-DD","area":"nome da área","vacancy_ids":["ID_EXATO_DA_VAGA"],"texto":"post final"}\n'
        "Use somente IDs que aparecem nos dados fornecidos. Cada vaga pode aparecer em apenas um post.\n"
        "Não invente nem altere títulos, cidades, modalidades, quantidades, requisitos ou links.\n"
        "O campo vacancy_ids deve listar todas as vagas representadas naquele post.\n"
        "O Python calculará qtd_vagas; não tente calcular nem incluir esse campo.\n"
        f"Datas permitidas: {json.dumps(datas, ensure_ascii=False)}. Use no máximo um post por data.\n"
        f"O texto deve ter no máximo {LIMITE} caracteres, em texto puro.\n"
        "Use somente links diretos das vagas associadas ao vacancy_ids do próprio post, "
        f"ou o portal geral {PORTAL}. Nunca invente links.\n"
        "Se não houver vagas válidas, responda somente []."
    )
    return base.rstrip() + extra


def main() -> int:
    try:
        vagas = carregar_vagas()
        dias_uteis, _, inicio, fim = gerar_calendario(
            datetime.now(TIMEZONE).date()
        )
        datas = [dia.isoformat() for dia in dias_uteis]

        if not vagas:
            ARQUIVO_SAIDA.write_text("[]\n", encoding="utf-8")
            print("Nenhuma vaga coletada. posts.json criado vazio; nenhuma chamada ao Gemini foi feita.")
            return 0

        prompt = montar_prompt(datas)
        erros_validacao: list[str] = []
        posts_validos: list[dict[str, Any]] | None = None

        for tentativa in range(1, MAX_TENTATIVAS_GEMINI + 1):
            prompt_tentativa = prompt
            if erros_validacao:
                prompt_tentativa += (
                    "\n\nCORRIJA A RESPOSTA ANTERIOR. Erros encontrados pelo validador Python:\n"
                    + "\n".join(f"- {erro}" for erro in erros_validacao)
                    + "\nResponda novamente somente com o array JSON completo."
                )
            try:
                resposta = chamar_gemini(prompt_tentativa)
                posts_validos = validar_posts(resposta, set(datas), vagas)
                break
            except ErroGeracao as exc:
                if not exc.retryable or tentativa == MAX_TENTATIVAS_GEMINI:
                    raise
                espera = exc.retry_after or min(15 * (2 ** (tentativa - 1)), 60)
                print(f"{exc}. Nova tentativa {tentativa + 1}/{MAX_TENTATIVAS_GEMINI} em {espera}s.")
                time.sleep(espera)
            except ValueError as exc:
                erros_validacao.append(str(exc))
                print(f"Validação editorial {tentativa}/{MAX_TENTATIVAS_GEMINI}: {exc}")

        if posts_validos is None:
            ultimo_erro = erros_validacao[-1] if erros_validacao else "resposta não validada"
            raise ErroGeracao(
                "Gemini não produziu posts válidos após "
                f"{MAX_TENTATIVAS_GEMINI} tentativas. Último erro: {ultimo_erro}"
            )

        # Mantém o formato de array JSON que publish_linkedin.py já consome.
        ARQUIVO_SAIDA.write_text(
            json.dumps(posts_validos, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"Geração concluída: {len(posts_validos)} post(s), "
            f"{sum(p['qtd_vagas'] for p in posts_validos)} vaga(s) associada(s)."
        )
        return 0

    except Exception as exc:
        print(f"ERRO FATAL NA GERAÇÃO: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
