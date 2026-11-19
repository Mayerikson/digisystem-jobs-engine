"""Coletor e organizador de vagas da Digisystem.

Gera vagas_digisystem.csv, vagas_digisystem.xlsx e acrescenta os dados
coletados ao prompt_pronto.txt. Tambem expoe gerar_calendario(), usado por
 generate_posts.py, e verificadores conservadores para uso na pre-publicacao.

Status de disponibilidade:
- ACTIVE: pagina individual apresenta um indicador explicito de candidatura.
- CLOSED: pagina informa explicitamente encerramento ou retorna HTTP 404/410.
- UNKNOWN: qualquer resultado inconclusivo; nunca deve ser publicado como aberto.

A verificacao individual ainda precisa ser conectada ao publicador diario.
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

MARCADORES_ABERTA = (
    "candidatar-se",
    "quero me candidatar",
    "candidate-se",
    "inscreva-se",
    "enviar candidatura",
    "candidatar",
    "apply now",
)

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
