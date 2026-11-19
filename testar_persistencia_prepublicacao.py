"""Testes isolados da barreira PUBLISHING antes da API do LinkedIn.

Nao acessa LinkedIn, Recrutei, Gemini, Groq ou Git remoto.
Os comandos Git sao simulados para verificar a ordem e o bloqueio em falha.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import publish_linkedin as pub


class TestPersistenciaPrePublicacao(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.arquivo_original = pub.ARQUIVO_POSTS
        self.posts_original = pub.POSTS_EM_MEMORIA
        self.dry_run_original = pub.DRY_RUN
        pub.ARQUIVO_POSTS = Path(self.temp.name) / "posts.json"
        pub.DRY_RUN = False

    def tearDown(self):
        pub.ARQUIVO_POSTS = self.arquivo_original
        pub.POSTS_EM_MEMORIA = self.posts_original
        pub.DRY_RUN = self.dry_run_original
        self.temp.cleanup()

    def _dados(self):
        link = "https://jobs.recrutei.com.br/digisystem/vacancy/123456-analista-teste"
        vaga = {
            "ID": "DIGI-TESTE-001",
            "Vaga": "Analista de Testes",
            "Detalhes": "CLT | Sao Paulo, SP",
            "Link de Inscrição": link,
            "status_disponibilidade": "ACTIVE",
            "motivo_verificacao": "Teste controlado ACTIVE",
            "http_status": 200,
        }
        post = {
            "data": "2026-12-01",
            "area": "Testes controlados",
            "vacancy_ids": [vaga["ID"]],
            "qtd_vagas": 1,
            "texto": f"A Digisystem abriu oportunidade.\nAnalista de Testes -> {link}",
            "status": "PENDING_VALIDATION",
            "publicado": False,
            "attempts": 0,
        }
        return link, vaga, post

    def test_comandos_git_sao_persistidos_na_ordem(self):
        comandos = []

        def fake_run(comando, **kwargs):
            comandos.append(comando)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), patch.object(pub.subprocess, "run", side_effect=fake_run):
            pub.persistir_estado_remoto_antes_da_publicacao()

        git_add = next(i for i, c in enumerate(comandos) if c[:2] == ["git", "add"])
        git_commit = next(i for i, c in enumerate(comandos) if c[:2] == ["git", "commit"])
        git_push = next(i for i, c in enumerate(comandos) if c[:2] == ["git", "push"])
        self.assertLess(git_add, git_commit)
        self.assertLess(git_commit, git_push)

    def test_falha_no_push_bloqueia_publicacao(self):
        _, vaga, post = self._dados()
        pub.POSTS_EM_MEMORIA = [post]
        pub.ARQUIVO_POSTS.write_text("[]", encoding="utf-8")
        eventos = []

        async def verificar(_vagas, _area):
            return [dict(vaga)]

        def falhar_persistencia():
            eventos.append("persistencia")
            raise pub.PublicacaoError("push simulado recusado")

        def publicar(_texto):
            eventos.append("linkedin")
            return "urn:li:share:nao-deve-ocorrer", 201

        with patch.object(pub, "verificar_com_retentativas", new=verificar), \
             patch.object(pub, "persistir_estado_remoto_antes_da_publicacao", side_effect=falhar_persistencia), \
             patch.object(pub, "publicar_no_linkedin", side_effect=publicar):
            with self.assertRaises(pub.PublicacaoError):
                pub.processar_post(post, {vaga["ID"]: vaga})

        self.assertEqual(eventos, ["persistencia"])
        self.assertEqual(post["status"], "FAILED")

    def test_persistencia_precede_chamada_linkedin(self):
        _, vaga, post = self._dados()
        pub.POSTS_EM_MEMORIA = [post]
        pub.ARQUIVO_POSTS.write_text("[]", encoding="utf-8")
        eventos = []

        async def verificar(_vagas, _area):
            return [dict(vaga)]

        def persistir():
            eventos.append("persistencia")

        def publicar(_texto):
            eventos.append("linkedin")
            return "urn:li:share:teste-controlado", 201

        with patch.object(pub, "verificar_com_retentativas", new=verificar), \
             patch.object(pub, "persistir_estado_remoto_antes_da_publicacao", side_effect=persistir), \
             patch.object(pub, "publicar_no_linkedin", side_effect=publicar):
            resultado = pub.processar_post(post, {vaga["ID"]: vaga})

        self.assertEqual(resultado, "published")
        self.assertEqual(eventos, ["persistencia", "linkedin"])
        self.assertEqual(post["status"], "PUBLISHED")
        self.assertTrue(post["publicado"])

    def test_bloqueia_persistencia_fora_do_github_actions(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            with self.assertRaises(pub.PublicacaoError):
                pub.persistir_estado_remoto_antes_da_publicacao()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPersistenciaPrePublicacao)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
