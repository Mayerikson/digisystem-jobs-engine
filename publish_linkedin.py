"""Valida as vagas do post do dia e publica no LinkedIn com estado persistido.

Seguranca:
- DRY_RUN e true por padrao.
- Somente vagas ACTIVE podem aparecer no post final.
- CLOSED e UNKNOWN sao removidas; se restarem vagas ACTIVE, o texto e regenerado.
- Se nao houver nenhuma vaga ACTIVE e houver UNKNOWN, a execucao falha e gera alerta.
- Antes de enviar ao LinkedIn, grava PUBLISHING. Resultado de rede incerto nunca e reenviado automaticamente.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

from vagas_pipeline import MARCADOR_DADOS, verificar_disponibilidade_vagas

ARQUIVO_POSTS = Path("posts.json")
ARQUIVO_CSV = Path("vagas_digisystem.csv")
ARQUIVO_PROMPT = Path("prompt_pronto.txt")
ARQUIVO_RESUMO = Path("run_summary.json")
PORTAL = "https://jobs.recrutei.com.br/digisystem"
LINKEDIN_POSTS_URL = "https://api.linkedin.com/rest/posts"
TIMEZONE = ZoneInfo("America/Sao_Paulo")
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() != "false"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
LINKEDIN_VERSION = os.getenv("LINKEDIN_VERSION", "202611").strip()
MAX_TENTATIVAS_REGENERACAO = 2
MAX_TENTATIVAS_VERIFICACAO_NORMAL = 2  # uma consulta inicial + uma repeticao
MAX_TENTATIVAS_VERIFICACAO_RISCO = 3   # Dados/Governanca: uma inicial + duas repeticoes
LIMITE_POST = 2900


class PublicacaoError(RuntimeError):
    """Falha operacional que deve deixar o workflow com status failed."""


def agora_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def normalizar_texto(valor: str) -> str:
    valor = unicodedata.normalize("NFKD", valor or "")
    valor = "".join(ch for ch in valor if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", valor).strip().lower()


def normalizar_link(link: str) -> str:
    valor = (link or "").strip().rstrip(".,;:!?)\"]}")
    partes = urlsplit(valor)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        return valor.rstrip("/")
    caminho = partes.path.rstrip("/")
    return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), caminho, partes.query, ""))


def links_no_texto(texto: str) -> list[str]:
    encontrados = re.findall(r"https?://[^\s<>\]\[\"']+", texto or "")
    return [normalizar_link(item) for item in encontrados]


def salvar_json_atomico(caminho: Path, dados: Any) -> None:
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporario.replace(caminho)


def log_evento(evento: str, **campos: Any) -> None:
    registro = {"timestamp": agora_iso(), "event": evento, **campos}
    print(json.dumps(registro, ensure_ascii=False, default=str))


def carregar_vagas_csv() -> dict[str, dict[str, str]]:
    if not ARQUIVO_CSV.exists():
        raise FileNotFoundError(f"Arquivo obrigatorio ausente: {ARQUIVO_CSV}")

    with ARQUIVO_CSV.open("r", encoding="utf-8-sig", newline="") as arquivo:
        leitor = csv.DictReader(arquivo)
        obrigatorias = {"ID", "Vaga", "Detalhes", "Link de Inscrição"}
        colunas = set(leitor.fieldnames or [])
        faltantes = obrigatorias - colunas
        if faltantes:
            raise ValueError(f"Colunas ausentes em {ARQUIVO_CSV}: {', '.join(sorted(faltantes))}")

        vagas: dict[str, dict[str, str]] = {}
        for numero, linha in enumerate(leitor, start=2):
            vaga_id = (linha.get("ID") or "").strip()
            titulo = (linha.get("Vaga") or "").strip()
            link = (linha.get("Link de Inscrição") or "").strip()
            if not vaga_id or not titulo or not link:
                raise ValueError(f"Registro incompleto na linha {numero} de {ARQUIVO_CSV}")
            if vaga_id in vagas:
                raise ValueError(f"ID duplicado no CSV: {vaga_id}")
            vagas[vaga_id] = {
                "ID": vaga_id,
                "Vaga": titulo,
                "Detalhes": (linha.get("Detalhes") or "").strip(),
                "Link de Inscrição": link,
            }
    return vagas


def area_de_risco_alto(area: str) -> bool:
    normalizada = normalizar_texto(area)
    return "dados" in normalizada or "governanca" in normalizada


async def verificar_com_retentativas(vagas: list[dict[str, Any]], area: str) -> list[dict[str, Any]]:
    """Repete somente resultados UNKNOWN; nunca converte UNKNOWN em ACTIVE."""
    if not vagas:
        return []

    limite = (
        MAX_TENTATIVAS_VERIFICACAO_RISCO
        if area_de_risco_alto(area)
        else MAX_TENTATIVAS_VERIFICACAO_NORMAL
    )
    por_id = {str(vaga["ID"]): dict(vaga) for vaga in vagas}

    for tentativa in range(1, limite + 1):
        pendentes = [
            vaga for vaga in por_id.values()
            if vaga.get("status_disponibilidade") in (None, "UNKNOWN")
        ]
        if not pendentes:
            break
        if tentativa > 1:
            espera = min(2 ** (tentativa - 1), 8)
            log_evento("verification_retry", area=area, attempt=tentativa, count=len(pendentes), wait_seconds=espera)
            await asyncio.sleep(espera)
        resultados = await verificar_disponibilidade_vagas(pendentes)
        for resultado in resultados:
            por_id[str(resultado["ID"])] = resultado

    return list(por_id.values())


def carregar_prompt_base() -> str:
    if not ARQUIVO_PROMPT.exists():
        raise FileNotFoundError(f"Arquivo obrigatorio ausente: {ARQUIVO_PROMPT}")
    conteudo = ARQUIVO_PROMPT.read_text(encoding="utf-8")
    if MARCADOR_DADOS in conteudo:
        conteudo = conteudo.split(MARCADOR_DADOS, 1)[0]
    return conteudo.strip()


def regenerar_texto_post(post: dict[str, Any], vagas_ativas: list[dict[str, str]]) -> str:
    """Reescreve um post somente quando uma ou mais vagas foram retiradas."""
    chave = os.getenv("GEMINI_API_KEY", "").strip()
    if not chave:
        raise PublicacaoError(
            "Uma vaga do post encerrou ou nao pode ser confirmada, mas GEMINI_API_KEY nao esta configurada para reconstruir o texto."
        )

    ids_ativos = [vaga["ID"] for vaga in vagas_ativas]
    dados_vagas = [
        {
            "ID": vaga["ID"],
            "Vaga": vaga["Vaga"],
            "Detalhes": vaga.get("Detalhes", ""),
            "Link de Inscrição": vaga["Link de Inscrição"],
        }
        for vaga in vagas_ativas
    ]
    prompt = (
        carregar_prompt_base()
        + "\n\n=== ATUALIZACAO OBRIGATORIA ANTES DA PUBLICACAO ===\n"
        + "O post original continha oportunidades que nao podem mais ser divulgadas. Reescreva o post usando EXCLUSIVAMENTE as vagas ativas abaixo.\n"
        + f"Data da publicacao: {post['data']}\n"
        + f"Area editorial a preservar: {post['area']}\n"
        + "Vagas confirmadas como ACTIVE:\n"
        + json.dumps(dados_vagas, ensure_ascii=False, indent=2)
        + "\nResponda SOMENTE com um objeto JSON contendo data, area, vacancy_ids e texto.\n"
        + "Use exatamente todos os IDs fornecidos em vacancy_ids, sem adicionar ou remover IDs.\n"
        + "Mantenha a area editorial original. Use os links diretos exatos fornecidos para as vagas ativas.\n"
        + "Nao mencione vagas removidas, encerradas ou nao verificadas. Nao invente informacoes.\n"
        + f"O texto deve ter no maximo {LIMITE_POST} caracteres.\n"
        + f"IDs obrigatorios: {json.dumps(ids_ativos, ensure_ascii=False)}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    corpo = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.45},
    }

    ultimo_erro = "resposta nao recebida"
    for tentativa in range(1, MAX_TENTATIVAS_REGENERACAO + 1):
        try:
            resposta = requests.post(
                url,
                headers={"x-goog-api-key": chave},
                json=corpo,
                timeout=90,
            )
        except requests.RequestException as exc:
            ultimo_erro = f"Falha de conexao com Gemini: {exc}"
            if tentativa < MAX_TENTATIVAS_REGENERACAO:
                time.sleep(5 * tentativa)
                continue
            break

        if resposta.status_code in {408, 429, 500, 502, 503, 504}:
            ultimo_erro = f"Gemini HTTP {resposta.status_code}: {resposta.text[:250]}"
            if tentativa < MAX_TENTATIVAS_REGENERACAO:
                retry_after = resposta.headers.get("Retry-After", "")
                try:
                    espera = min(max(int(retry_after), 1), 20) if retry_after else 5 * tentativa
                except ValueError:
                    espera = 5 * tentativa
                time.sleep(espera)
                continue
            break
        if resposta.status_code != 200:
            raise PublicacaoError(f"Gemini recusou a regeneracao: HTTP {resposta.status_code}: {resposta.text[:400]}")

        try:
            texto_json = resposta.json()["candidates"][0]["content"]["parts"][0]["text"]
            objeto = json.loads(texto_json)
            if isinstance(objeto, list) and len(objeto) == 1:
                objeto = objeto[0]
            if not isinstance(objeto, dict):
                raise ValueError("A resposta nao e um objeto JSON.")
            if str(objeto.get("data", "")) != str(post["data"]):
                raise ValueError("A data retornada pelo Gemini foi alterada.")
            if str(objeto.get("area", "")).strip() != str(post["area"]).strip():
                raise ValueError("A area editorial retornada pelo Gemini foi alterada.")
            ids = objeto.get("vacancy_ids")
            if not isinstance(ids, list) or set(ids) != set(ids_ativos) or len(ids) != len(ids_ativos):
                raise ValueError("Os vacancy_ids retornados nao correspondem exatamente as vagas ativas.")
            texto_final = str(objeto.get("texto", "")).strip()
            if not texto_final or len(texto_final) > LIMITE_POST:
                raise ValueError(f"Texto vazio ou acima de {LIMITE_POST} caracteres.")

            links_permitidos = {normalizar_link(v["Link de Inscrição"]) for v in vagas_ativas}
            links_permitidos.add(normalizar_link(PORTAL))
            links_usados = links_no_texto(texto_final)
            if not links_usados:
                raise ValueError("O post regenerado nao contem link.")
            invalidos = set(links_usados) - links_permitidos
            if invalidos:
                raise ValueError("O post regenerado contem links que nao pertencem as vagas ativas: " + ", ".join(sorted(invalidos)))
            if not (set(links_usados) & {normalizar_link(v["Link de Inscrição"]) for v in vagas_ativas}):
                raise ValueError("O post regenerado nao contem link direto de nenhuma vaga ativa.")
            return texto_final
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
            ultimo_erro = f"Resposta de regeneracao invalida: {exc}"
            if tentativa < MAX_TENTATIVAS_REGENERACAO:
                time.sleep(3 * tentativa)
                continue
            break

    raise PublicacaoError(
        f"Nao foi possivel reconstruir com seguranca o post apos retirar vagas indisponiveis: {ultimo_erro}"
    )


def publicar_no_linkedin(texto: str) -> tuple[str | None, int]:
    token = os.getenv("LINKEDIN_TOKEN", "").strip()
    person_id = os.getenv("LINKEDIN_PERSON_ID", "").strip()
    if not token or not person_id:
        raise PublicacaoError("LINKEDIN_TOKEN ou LINKEDIN_PERSON_ID nao configurado nos GitHub Secrets.")
    if not re.fullmatch(r"\d{6}", LINKEDIN_VERSION):
        raise PublicacaoError("LINKEDIN_VERSION invalida. Use o formato YYYYMM, por exemplo 202611.")

    corpo = {
        "author": f"urn:li:person:{person_id}",
        "commentary": texto,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    try:
        resposta = requests.post(
            LINKEDIN_POSTS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": LINKEDIN_VERSION,
                "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json",
            },
            json=corpo,
            timeout=60,
        )
    except requests.RequestException as exc:
        # O servidor pode ter recebido o post antes de a conexao cair.
        raise PublicacaoError(f"RESULTADO_INCERTO: falha de rede durante a publicacao: {exc}") from exc

    if resposta.status_code == 201:
        return resposta.headers.get("x-restli-id"), resposta.status_code
    if resposta.status_code >= 500:
        raise PublicacaoError(
            f"RESULTADO_INCERTO: LinkedIn HTTP {resposta.status_code}: {resposta.text[:350]}"
        )
    raise PublicacaoError(f"LinkedIn rejeitou a publicacao: HTTP {resposta.status_code}: {resposta.text[:500]}")


def processar_post(post: dict[str, Any], vagas_csv: dict[str, dict[str, str]]) -> str:
    status_atual = str(post.get("status", "")).upper()
    if post.get("publicado") or status_atual == "PUBLISHED":
        return "already_published"
    if status_atual in {"SKIPPED_ALL_CLOSED", "UNKNOWN_PUBLISH_RESULT"}:
        return status_atual.lower()
    if status_atual == "PUBLISHING":
        post["status"] = "UNKNOWN_PUBLISH_RESULT"
        post["last_error"] = (
            "A execucao anterior foi interrompida depois de iniciar o envio. Verifique o LinkedIn antes de qualquer reenvio."
        )
        post["updated_at"] = agora_iso()
        raise PublicacaoError(post["last_error"])

    ids = post.get("vacancy_ids")
    if not isinstance(ids, list) or not ids:
        post["status"] = "BLOCKED_MISSING_VACANCY_IDS"
        post["last_error"] = "Post sem vacancy_ids; nao e seguro publicar sem vincular o texto as vagas originais."
        post["updated_at"] = agora_iso()
        raise PublicacaoError(post["last_error"])

    ids = [str(item).strip() for item in ids]
    if len(ids) != len(set(ids)):
        raise PublicacaoError("O post contem vacancy_ids duplicados.")
    ausentes = [vaga_id for vaga_id in ids if vaga_id not in vagas_csv]
    if ausentes:
        post["status"] = "BLOCKED_MISSING_VACANCY_DATA"
        post["last_error"] = "IDs ausentes no CSV de origem: " + ", ".join(ausentes)
        post["updated_at"] = agora_iso()
        raise PublicacaoError(post["last_error"])

    vagas_do_post = [vagas_csv[vaga_id] for vaga_id in ids]
    resultados = asyncio.run(verificar_com_retentativas(vagas_do_post, str(post.get("area", ""))))
    por_id = {str(item["ID"]): item for item in resultados}
    ativas = [por_id[vaga_id] for vaga_id in ids if por_id.get(vaga_id, {}).get("status_disponibilidade") == "ACTIVE"]
    fechadas = [vaga_id for vaga_id in ids if por_id.get(vaga_id, {}).get("status_disponibilidade") == "CLOSED"]
    desconhecidas = [vaga_id for vaga_id in ids if por_id.get(vaga_id, {}).get("status_disponibilidade") not in {"ACTIVE", "CLOSED"}]

    post["last_verified_at"] = agora_iso()
    post["verification"] = {
        vaga_id: {
            "status": por_id.get(vaga_id, {}).get("status_disponibilidade", "UNKNOWN"),
            "reason": por_id.get(vaga_id, {}).get("motivo_verificacao", "Resultado ausente"),
            "http_status": por_id.get(vaga_id, {}).get("http_status"),
            "last_verified_at": por_id.get(vaga_id, {}).get("last_verified_at"),
        }
        for vaga_id in ids
    }

    if not ativas:
        if desconhecidas:
            post["status"] = "BLOCKED_UNVERIFIED"
            post["last_error"] = "Nenhuma vaga foi confirmada como ativa; status inconclusivo para: " + ", ".join(desconhecidas)
            post["updated_at"] = agora_iso()
            raise PublicacaoError(post["last_error"])
        post["status"] = "SKIPPED_ALL_CLOSED"
        post["publicado"] = False
        post["last_error"] = None
        post["updated_at"] = agora_iso()
        log_evento("post_skipped_all_vacancies_closed", date=post.get("data"), area=post.get("area"), vacancy_ids=ids)
        return "skipped_all_closed"

    removidas = fechadas + desconhecidas
    if removidas:
        log_evento(
            "vacancies_removed_before_publish",
            date=post.get("data"),
            area=post.get("area"),
            removed_closed=fechadas,
            removed_unknown=desconhecidas,
            kept_active=[vaga["ID"] for vaga in ativas],
        )
        texto_novo = regenerar_texto_post(post, ativas)
        post["texto_original"] = post.get("texto", "")
        post["texto"] = texto_novo
        post["vacancy_ids"] = [vaga["ID"] for vaga in ativas]
        post["qtd_vagas"] = len(ativas)
        post["removed_vacancy_ids"] = removidas
        post["content_updated_at"] = agora_iso()

    # Verificacao final local dos links e do tamanho, inclusive quando nao houve regeneracao.
    texto = str(post.get("texto", "")).strip()
    if not texto or len(texto) > LIMITE_POST:
        post["status"] = "BLOCKED_INVALID_TEXT"
        post["last_error"] = f"Texto vazio ou acima de {LIMITE_POST} caracteres."
        post["updated_at"] = agora_iso()
        raise PublicacaoError(post["last_error"])

    ids_finais = [str(item) for item in post["vacancy_ids"]]
    links_permitidos = {normalizar_link(vagas_csv[vaga_id]["Link de Inscrição"]) for vaga_id in ids_finais}
    links_permitidos.add(normalizar_link(PORTAL))
    links_usados = links_no_texto(texto)
    if not links_usados or set(links_usados) - links_permitidos:
        post["status"] = "BLOCKED_INVALID_LINKS"
        post["last_error"] = "O texto contem link ausente ou que nao corresponde as vagas ativas do post."
        post["updated_at"] = agora_iso()
        raise PublicacaoError(post["last_error"])

    if DRY_RUN:
        post["status"] = "DRY_RUN_VALIDATED"
        post["publicado"] = False
        post["last_error"] = None
        post["updated_at"] = agora_iso()
        log_evento("dry_run_validated", date=post.get("data"), area=post.get("area"), vacancy_ids=ids_finais, qtd_vagas=len(ids_finais))
        print(f"[DRY-RUN] {post['data']} | {post['area']} | {len(ids_finais)} vaga(s)\n{texto}\n")
        return "dry_run_validated"

    # Persistir PUBLISHING antes da chamada evita reenvio cego apos interrupcao.
    post["status"] = "PUBLISHING"
    post["attempts"] = int(post.get("attempts", 0) or 0) + 1
    post["publish_started_at"] = agora_iso()
    post["last_error"] = None
    salvar_json_atomico(ARQUIVO_POSTS, POSTS_EM_MEMORIA)

    try:
        urn, http_status = publicar_no_linkedin(texto)
    except PublicacaoError as exc:
        if "RESULTADO_INCERTO:" in str(exc):
            post["status"] = "UNKNOWN_PUBLISH_RESULT"
        else:
            post["status"] = "FAILED"
        post["last_error"] = str(exc)
        post["updated_at"] = agora_iso()
        salvar_json_atomico(ARQUIVO_POSTS, POSTS_EM_MEMORIA)
        raise

    post["status"] = "PUBLISHED"
    post["publicado"] = True
    post["linkedin_post_urn"] = urn
    post["published_at"] = agora_iso()
    post["linkedin_http_status"] = http_status
    post["last_error"] = None
    post["updated_at"] = agora_iso()
    salvar_json_atomico(ARQUIVO_POSTS, POSTS_EM_MEMORIA)
    log_evento("linkedin_post_published", date=post.get("data"), area=post.get("area"), linkedin_post_urn=urn, vacancy_ids=ids_finais)
    return "published"


POSTS_EM_MEMORIA: list[dict[str, Any]] = []


def main() -> int:
    inicio = agora_iso()
    hoje = datetime.now(TIMEZONE).date().isoformat()
    resumo: dict[str, Any] = {
        "started_at": inicio,
        "date": hoje,
        "mode": "DRY_RUN" if DRY_RUN else "LIVE",
        "status": "RUNNING",
        "posts_processed": [],
    }
    global POSTS_EM_MEMORIA

    try:
        if not ARQUIVO_POSTS.exists():
            raise FileNotFoundError(f"Arquivo obrigatorio ausente: {ARQUIVO_POSTS}")
        POSTS_EM_MEMORIA = json.loads(ARQUIVO_POSTS.read_text(encoding="utf-8"))
        if not isinstance(POSTS_EM_MEMORIA, list):
            raise ValueError("posts.json precisa conter um array JSON.")

        posts_hoje = [post for post in POSTS_EM_MEMORIA if str(post.get("data", "")) == hoje]
        if not posts_hoje:
            resumo.update({"status": "SUCCESS_NO_POST_SCHEDULED", "finished_at": agora_iso()})
            salvar_json_atomico(ARQUIVO_RESUMO, resumo)
            log_evento("no_post_scheduled", date=hoje)
            return 0

        pendentes = [
            post for post in posts_hoje
            if not post.get("publicado")
            and str(post.get("status", "")).upper() not in {
                "PUBLISHED", "SKIPPED_ALL_CLOSED", "UNKNOWN_PUBLISH_RESULT"
            }
        ]
        if not pendentes:
            resumo.update({"status": "SUCCESS_NO_PENDING_POST", "finished_at": agora_iso()})
            salvar_json_atomico(ARQUIVO_RESUMO, resumo)
            log_evento("no_pending_post", date=hoje)
            return 0

        vagas_csv = carregar_vagas_csv()
        for post in pendentes:
            resultado = processar_post(post, vagas_csv)
            resumo["posts_processed"].append({
                "date": post.get("data"),
                "area": post.get("area"),
                "result": resultado,
                "status": post.get("status"),
                "vacancy_ids": post.get("vacancy_ids", []),
            })
            salvar_json_atomico(ARQUIVO_POSTS, POSTS_EM_MEMORIA)

        resumo.update({"status": "SUCCESS", "finished_at": agora_iso()})
        salvar_json_atomico(ARQUIVO_RESUMO, resumo)
        return 0

    except Exception as exc:
        resumo.update({
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "finished_at": agora_iso(),
        })
        try:
            salvar_json_atomico(ARQUIVO_POSTS, POSTS_EM_MEMORIA)
            salvar_json_atomico(ARQUIVO_RESUMO, resumo)
        except Exception as erro_salvamento:
            print(f"FALHA ADICIONAL AO SALVAR ESTADO: {erro_salvamento}", file=sys.stderr)
        log_evento("workflow_failed", error_type=type(exc).__name__, error=str(exc), date=hoje)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
