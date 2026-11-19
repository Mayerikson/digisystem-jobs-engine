"""Testes de seguranca do publicador; nao acessam LinkedIn nem Recrutei."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import publish_linkedin as publisher

VAGA = {
    "ID": "TESTE-DIGI-001",
    "Vaga": "Analista de Governanca de Dados",
    "Detalhes": "CLT | Brasilia, DF",
    "Link de Inscrição": "https://jobs.recrutei.com.br/digisystem/vacancy/156893-analista-de-governanca-de-dados-sps0001946",
}


def resultado(status: str) -> dict:
    return {
        **VAGA,
        "status_disponibilidade": status,
        "motivo_verificacao": f"Resultado simulado de teste: {status}",
        "http_status": 200 if status != "UNKNOWN" else None,
        "last_verified_at": "2026-11-19T12:00:00-03:00",
    }


class TestBloqueiosPublicador(unittest.TestCase):
    def setUp(self) -> None:
        publisher.DRY_RUN = True
        self.vagas_csv = {VAGA["ID"]: VAGA.copy()}
        self.post = {
            "data": "2026-12-21",
            "area": "Dados & Governanca",
            "texto": (
                "A @Digisystem abriu oportunidade para Analista de Governanca de Dados.\n"
                "https://jobs.recrutei.com.br/digisystem/vacancy/156893-analista-de-governanca-de-dados-sps0001946"
            ),
            "vacancy_ids": [VAGA["ID"]],
            "qtd_vagas": 1,
            "publicado": False,
            "status": "PENDING",
        }
        self.linkedin = patch.object(
            publisher, "publicar_no_linkedin", side_effect=AssertionError("LinkedIn nao pode ser chamado no teste")
        )
        self.linkedin_mock = self.linkedin.start()
        self.addCleanup(self.linkedin.stop)

    def executar_com_status(self, status: str):
        fake = AsyncMock(return_value=[resultado(status)])
        with patch.object(publisher, "verificar_com_retentativas", new=fake):
            retorno = None
            erro = None
            try:
                retorno = publisher.processar_post(self.post, self.vagas_csv)
            except publisher.PublicacaoError as exc:
                erro = exc
        fake.assert_awaited_once()
        self.linkedin_mock.assert_not_called()
        return retorno, erro

    def test_closed_bloqueia_post_sem_vagas_ativas(self) -> None:
        retorno, erro = self.executar_com_status("CLOSED")
        self.assertIsNone(erro)
        self.assertEqual(retorno, "skipped_all_closed")
        self.assertEqual(self.post["status"], "SKIPPED_ALL_CLOSED")
        self.assertFalse(self.post["publicado"])

    def test_unknown_bloqueia_post_sem_vagas_confirmadas(self) -> None:
        retorno, erro = self.executar_com_status("UNKNOWN")
        self.assertIsNone(retorno)
        self.assertIsInstance(erro, publisher.PublicacaoError)
        self.assertEqual(self.post["status"], "BLOCKED_UNVERIFIED")
        self.assertFalse(self.post["publicado"])

    def test_active_permite_apenas_validacao_em_dry_run(self) -> None:
        retorno, erro = self.executar_com_status("ACTIVE")
        self.assertIsNone(erro)
        self.assertEqual(retorno, "dry_run_validated")
        self.assertEqual(self.post["status"], "DRY_RUN_VALIDATED")
        self.assertFalse(self.post["publicado"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
