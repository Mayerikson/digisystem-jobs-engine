"""Testes de seguranca do publicador e do classificador Recrutei.
Nao acessa a internet, nao consulta o LinkedIn e nao publica conteudo.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import publish_linkedin as publisher
from vagas_pipeline import _classificar_resultado_disponibilidade

VAGA = {
    "ID": "TESTE-DIGI-001",
    "Vaga": "Analista de Governanca de Dados",
    "Detalhes": "CLT | Brasilia, DF",
    "Link de Inscrição": "https://jobs.recrutei.com.br/digisystem/vacancy/156893-analista-de-governanca-de-dados-sps0001946",
}


def resultado(status: str, vaga: dict | None = None) -> dict:
    return {
        **(vaga or VAGA),
        "status_disponibilidade": status,
        "motivo_verificacao": f"Resultado simulado de teste: {status}",
        "http_status": 200 if status != "UNKNOWN" else None,
        "last_verified_at": "2026-11-19T12:00:00-03:00",
    }


def resultado_classificador(**overrides):
    base = {
        "http_status": 200,
        "body_text": "Oportunidade na Digisystem",
        "application_body_excerpt": "",
        "application_http_status": 200,
        "vacancy_id": "156893",
        "page_final_url": VAGA["Link de Inscrição"],
        "application_final_url": "https://talent.recrutei.com.br/digisystem/156893/signup",
        "application_links": [{"href": "https://talent.recrutei.com.br/digisystem/156893/signup"}],
        "talent_pool_links": [],
        "application_form_count": 1,
        "application_field_signals": ["nome", "email", "telefone"],
    }
    base.update(overrides)
    return base


class TestClassificadorRecrutei(unittest.TestCase):
    def test_http_404_classifica_closed(self):
        status, _ = _classificar_resultado_disponibilidade(resultado_classificador(http_status=404))
        self.assertEqual(status, "CLOSED")

    def test_mensagem_explicita_classifica_closed(self):
        status, _ = _classificar_resultado_disponibilidade(
            resultado_classificador(body_text="Vaga encerrada. Esta vaga nao recebe mais candidaturas.")
        )
        self.assertEqual(status, "CLOSED")

    def test_apenas_banco_de_talentos_classifica_unknown(self):
        status, _ = _classificar_resultado_disponibilidade(
            resultado_classificador(
                application_links=[],
                talent_pool_links=[{"href": "https://talent.recrutei.com.br/digisystem/signup"}],
                application_final_url="https://talent.recrutei.com.br/digisystem/signup",
                application_form_count=1,
            )
        )
        self.assertEqual(status, "UNKNOWN")

    def test_formulario_especifico_vinculado_classifica_active(self):
        status, _ = _classificar_resultado_disponibilidade(resultado_classificador())
        self.assertEqual(status, "ACTIVE")


class TestBloqueiosPublicador(unittest.TestCase):
    def setUp(self) -> None:
        publisher.DRY_RUN = True
        self.vagas_csv = {VAGA["ID"]: VAGA.copy()}
        self.post = {
            "data": "2026-12-21",
            "area": "Dados & Governanca",
            "texto": (
                "A @Digisystem abriu oportunidade para Analista de Governanca de Dados.\n"
                + VAGA["Link de Inscrição"]
            ),
            "vacancy_ids": [VAGA["ID"]],
            "qtd_vagas": 1,
            "publicado": False,
            "status": "PENDING",
        }
        self.linkedin = patch.object(
            publisher,
            "publicar_no_linkedin",
            side_effect=AssertionError("LinkedIn nao pode ser chamado no teste"),
        )
        self.linkedin_mock = self.linkedin.start()
        self.addCleanup(self.linkedin.stop)

    def executar_com_resultados(self, resultados, *, espera_verificador=True):
        fake = AsyncMock(return_value=resultados)
        with patch.object(publisher, "verificar_com_retentativas", new=fake):
            retorno = None
            erro = None
            try:
                retorno = publisher.processar_post(self.post, self.vagas_csv)
            except publisher.PublicacaoError as exc:
                erro = exc
        if espera_verificador:
            fake.assert_awaited_once()
        else:
            fake.assert_not_awaited()
        self.linkedin_mock.assert_not_called()
        return retorno, erro

    def test_closed_bloqueia_post_sem_vagas_ativas(self):
        retorno, erro = self.executar_com_resultados([resultado("CLOSED")])
        self.assertIsNone(erro)
        self.assertEqual(retorno, "skipped_all_closed")
        self.assertEqual(self.post["status"], "SKIPPED_ALL_CLOSED")
        self.assertFalse(self.post["publicado"])

    def test_unknown_bloqueia_post_sem_vagas_confirmadas(self):
        retorno, erro = self.executar_com_resultados([resultado("UNKNOWN")])
        self.assertIsNone(retorno)
        self.assertIsInstance(erro, publisher.PublicacaoError)
        self.assertEqual(self.post["status"], "BLOCKED_UNVERIFIED")
        self.assertFalse(self.post["publicado"])

    def test_active_permite_apenas_validacao_em_dry_run(self):
        retorno, erro = self.executar_com_resultados([resultado("ACTIVE")])
        self.assertIsNone(erro)
        self.assertEqual(retorno, "dry_run_validated")
        self.assertEqual(self.post["status"], "DRY_RUN_VALIDATED")
        self.assertFalse(self.post["publicado"])

    def test_link_de_outra_vaga_bloqueia_post(self):
        self.post["texto"] = "Oportunidade: https://jobs.recrutei.com.br/digisystem/vacancy/999-outra-vaga"
        retorno, erro = self.executar_com_resultados([resultado("ACTIVE")])
        self.assertIsNone(retorno)
        self.assertIsInstance(erro, publisher.PublicacaoError)
        self.assertEqual(self.post["status"], "BLOCKED_INVALID_LINKS")

    def test_ids_duplicados_bloqueiam_post(self):
        self.post["vacancy_ids"] = [VAGA["ID"], VAGA["ID"]]
        retorno, erro = self.executar_com_resultados([resultado("ACTIVE")], espera_verificador=False)
        self.assertIsNone(retorno)
        self.assertIsInstance(erro, publisher.PublicacaoError)
        self.assertIn("duplicados", str(erro))

    def test_vaga_closed_removida_quando_outra_active(self):
        vaga_ativa = {**VAGA, "ID": "TESTE-DIGI-002", "Vaga": "Analista de Dados Pleno", "Link de Inscrição": "https://jobs.recrutei.com.br/digisystem/vacancy/222222-analista-dados"}
        self.vagas_csv[vaga_ativa["ID"]] = vaga_ativa
        self.post["vacancy_ids"] = [VAGA["ID"], vaga_ativa["ID"]]
        self.post["texto"] += "\n" + vaga_ativa["Link de Inscrição"]
        fake_results = [resultado("CLOSED", VAGA), resultado("ACTIVE", vaga_ativa)]
        texto_regenerado = "A @Digisystem abriu oportunidade para Analista de Dados Pleno.\n" + vaga_ativa["Link de Inscrição"]
        with patch.object(publisher, "verificar_com_retentativas", new=AsyncMock(return_value=fake_results)):
            with patch.object(publisher, "regenerar_texto_post", return_value=texto_regenerado):
                retorno = publisher.processar_post(self.post, self.vagas_csv)
        self.assertEqual(retorno, "dry_run_validated")
        self.assertEqual(self.post["vacancy_ids"], [vaga_ativa["ID"]])
        self.assertEqual(self.post["removed_vacancy_ids"], [VAGA["ID"]])
        self.linkedin_mock.assert_not_called()

    def test_estado_publishing_anterior_bloqueia_reenvio(self):
        self.post["status"] = "PUBLISHING"
        retorno, erro = self.executar_com_resultados([], espera_verificador=False)
        self.assertIsNone(retorno)
        self.assertIsInstance(erro, publisher.PublicacaoError)
        self.assertEqual(self.post["status"], "UNKNOWN_PUBLISH_RESULT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
