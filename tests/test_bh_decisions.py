import datetime
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from urllib.parse import urlparse

test_database_url = os.environ.get("BH_TEST_DATABASE_URL")
if test_database_url:
    parsed = urlparse(test_database_url)
    if (not parsed.scheme.startswith("postgres") or
            parsed.hostname not in {"127.0.0.1", "localhost"} or
            parsed.path != "/postgres"):
        raise RuntimeError("BH_TEST_DATABASE_URL deve apontar para PostgreSQL local isolado.")
os.environ["DATABASE_URL"] = test_database_url or "sqlite:///:memory:"
os.environ["FLASK_ENV"] = "development"
os.environ["SECRET_KEY"] = "bh-test-key"
os.environ["SECURITY_PASSWORD_SALT"] = "bh-test-salt"

import app as system


class BancoDeHorasDecisionsTest(unittest.TestCase):
    def setUp(self):
        system.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.context = system.app.app_context()
        self.context.push()
        system.db.create_all()

        self.admin = self._user("Admin", "admin", "administrador")
        self.staff = self._user("Servidor", "staff", "funcionario", saldo=120)
        system.db.session.add_all((self.admin, self.staff))
        system.db.session.commit()

        self.client = system.app.test_client()
        self.patches = (
            mock.patch.object(system, "gerar_protocolo_agendamento_pdf"),
            mock.patch.object(system, "enviar_email"),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        system.db.session.remove()
        system.db.drop_all()
        self.context.pop()

    @staticmethod
    def _user(nome, key, tipo, saldo=0):
        return system.User(
            nome=nome,
            registro=key,
            email=f"{key}@example.invalid",
            senha="test",
            tipo=tipo,
            status="aprovado",
            ativo=True,
            cpf=f"cpf-{key}",
            rg=f"rg-{key}",
            banco_horas=saldo,
            aceitou_termo=True,
            versao_termo=system.TERMO_VERSION,
        )

    def _login(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True

    def _agendamento(self, status="em_espera"):
        agendamento = system.Agendamento(
            funcionario_id=self.staff.id,
            status=status,
            data=datetime.date(2026, 10, 28),
            motivo="BH",
            tipo_folga="BH",
            horas=1,
            minutos=0,
        )
        system.db.session.add(agendamento)
        system.db.session.commit()
        return agendamento

    def test_repeated_folga_decision_does_not_debit_twice(self):
        agendamento = self._agendamento()
        self._login(self.admin)
        payload = {"folga_id": agendamento.id, "status": "deferido"}

        first = self.client.post("/deferir_folgas", data=payload)
        second = self.client.post("/deferir_folgas", data=payload)

        self.assertLess(first.status_code, 400)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 60)
        self.assertEqual(
            system.BancoDeHoras.query.filter_by(funcionario_id=self.staff.id, motivo="BH").count(),
            1,
        )

    def test_reversal_refunds_once(self):
        agendamento = self._agendamento()
        self._login(self.admin)
        self.client.post(
            "/deferir_folgas",
            data={"folga_id": agendamento.id, "status": "deferido"},
        )
        payload = {"folga_id": agendamento.id, "status": "indeferido"}

        first = self.client.post("/deferir_folgas", data=payload)
        second = self.client.post("/deferir_folgas", data=payload)

        self.assertLess(first.status_code, 400)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 120)

    def test_repeated_credit_decision_does_not_add_twice(self):
        credit = system.BancoDeHoras(
            funcionario_id=self.staff.id,
            horas=1,
            minutos=0,
            total_minutos=60,
            data_realizacao=datetime.date(2026, 10, 28),
            status="Horas a Serem Deferidas",
            motivo="Trabalho adicional",
        )
        system.db.session.add(credit)
        system.db.session.commit()
        self._login(self.admin)
        payload = {"registro_id": credit.id, "action": "deferir"}

        first = self.client.post("/banco_horas/deferir", data=payload)
        second = self.client.post("/banco_horas/deferir", data=payload)

        self.assertLess(first.status_code, 400)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 180)

    def test_staff_cannot_decide_or_delete_deferred_bh(self):
        agendamento = self._agendamento(status="deferido")
        credit = system.BancoDeHoras(
            funcionario_id=self.staff.id,
            horas=1,
            minutos=0,
            total_minutos=60,
            data_realizacao=datetime.date(2026, 10, 28),
            status="Horas a Serem Deferidas",
            motivo="Trabalho adicional",
        )
        system.db.session.add(credit)
        system.db.session.commit()
        self._login(self.staff)

        get_response = self.client.get('/deferir_folgas')
        self.assertEqual(get_response.status_code, 302)

        self.client.post(
            "/deferir_folgas",
            data={"folga_id": agendamento.id, "status": "indeferido"},
        )
        self.client.post(
            "/banco_horas/deferir",
            data={"registro_id": credit.id, "action": "deferir"},
        )
        self.client.post(f"/delete_agendamento/{agendamento.id}")

        self.assertIsNotNone(system.db.session.get(system.Agendamento, agendamento.id))
        self.assertEqual(
            system.db.session.get(system.BancoDeHoras, credit.id).status,
            "Horas a Serem Deferidas",
        )
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 120)

    def test_admin_delete_refunds_deferred_bh_once(self):
        agendamento = self._agendamento(status="deferido")
        self.staff.banco_horas = 60
        system.db.session.commit()
        self._login(self.admin)

        with mock.patch.object(system, "_csrf_ok_from_header", return_value=True):
            response = self.client.post(f"/admin/delete_agendamento/{agendamento.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(system.db.session.get(system.Agendamento, agendamento.id))
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 120)

    def test_admin_credit_recalculation_only_increases_stale_balance(self):
        self.staff.banco_horas = 10
        credit = system.BancoDeHoras(
            funcionario_id=self.staff.id,
            horas=2,
            minutos=0,
            total_minutos=120,
            data_realizacao=datetime.date(2026, 10, 27),
            status="Deferido",
            motivo="Trabalho adicional",
        )
        system.db.session.add(credit)
        system.db.session.commit()
        self._login(self.admin)

        response = self.client.post(
            "/banco_horas/inserir",
            data={
                "funcionario_id": self.staff.id,
                "data_realizacao": "2026-10-28",
                "motivo": "Ajuste administrativo",
                "status": "Indeferido",
                "horas": "1",
                "minutos": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 120)
        self.assertEqual(system.BancoDeHoras.query.filter_by(funcionario_id=self.staff.id).count(), 2)

        self.staff.banco_horas = 180
        system.db.session.commit()
        self.client.post(
            "/banco_horas/inserir",
            data={
                "funcionario_id": self.staff.id,
                "data_realizacao": "2026-10-28",
                "motivo": "Outro ajuste administrativo",
                "status": "Indeferido",
                "horas": "1",
                "minutos": "0",
            },
        )
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 180)

    def test_balance_calculation_preserves_higher_saved_amount(self):
        credit = system.BancoDeHoras(
            funcionario_id=self.staff.id,
            horas=2,
            minutos=0,
            total_minutos=120,
            data_realizacao=datetime.date(2026, 10, 27),
            status="Deferido",
            motivo="Trabalho adicional",
        )
        system.db.session.add(credit)
        system.db.session.commit()

        shadow = system.BancoDeHoras(
            funcionario_id=self.staff.id,
            horas=1,
            minutos=0,
            total_minutos=60,
            data_realizacao=datetime.date(2026, 10, 28),
            status="Deferida",
            motivo="BH",
        )
        system.db.session.add(shadow)
        system.db.session.commit()

        self.staff.banco_horas = 10
        system.db.session.commit()
        low = system._calcular_bh_usuario(self.staff.id, self.staff)
        self.assertEqual((low['saldo_calculado_min'], low['saldo_utilizavel_min']), (120, 120))

        self.staff.banco_horas = 180
        system.db.session.commit()
        high = system._calcular_bh_usuario(self.staff.id, self.staff)
        self.assertEqual((high['saldo_calculado_min'], high['saldo_utilizavel_min']), (120, 180))

    def test_audit_is_admin_only(self):
        self._login(self.staff)
        self.assertEqual(self.client.get('/banco_horas/auditoria').status_code, 302)

    def test_audit_is_read_only(self):
        self.staff.banco_horas = 10
        system.db.session.add(system.BancoDeHoras(
            funcionario_id=self.staff.id,
            horas=2,
            minutos=0,
            total_minutos=120,
            data_realizacao=datetime.date(2026, 10, 28),
            status="Deferido",
            motivo="Trabalho adicional",
        ))
        system.db.session.commit()
        self._login(self.admin)
        response = self.client.get('/banco_horas/auditoria?filtro=gravado_menor&q=Servidor')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<span class="name">Servidor</span>', response.data)
        self.assertIn(b'class="state state-gravado_menor"', response.data)
        self.assertNotIn(b'method="POST"', response.data)
        self.assertEqual(self.client.post('/banco_horas/auditoria').status_code, 405)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 10)
        self.assertNotIn(
            b'<span class="name">Servidor</span>',
            self.client.get('/banco_horas/auditoria?filtro=ok').data,
        )
        self.assertIn(b'/banco_horas/auditoria', self.client.get('/banco_horas').data)

    def test_repeated_staff_bh_request_creates_one_appointment(self):
        self._login(self.staff)
        payload = {
            'tipo_folga': 'BH', 'motivo': 'BH', 'data': '2026-10-28',
            'quantidade_horas': '1', 'quantidade_minutos': '0',
        }
        first = self.client.post('/agendar', data=payload)
        second = self.client.post('/agendar', data=payload)
        self.assertLess(first.status_code, 400)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(system.Agendamento.query.filter_by(funcionario_id=self.staff.id, motivo='BH').count(), 1)

    def test_repeated_admin_bh_request_debits_once(self):
        self._login(self.admin)
        payload = {
            'user_id': self.staff.id, 'tipo_folga': 'BH', 'motivo': 'BH',
            'data': '2026-10-28', 'quantidade_horas': '1', 'quantidade_minutos': '0',
        }
        first = self.client.post('/admin/agendar_para', data=payload)
        second = self.client.post('/admin/agendar_para', data=payload)
        self.assertLess(first.status_code, 400)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(system.Agendamento.query.filter_by(funcionario_id=self.staff.id, motivo='BH').count(), 1)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 60)

    def test_second_identical_pending_request_cannot_be_deferred(self):
        first = self._agendamento(status='deferido')
        second = self._agendamento(status='em_espera')
        self.staff.banco_horas = 60
        system.db.session.commit()
        self._login(self.admin)

        response = self.client.post(
            '/deferir_folgas', data={'folga_id': second.id, 'status': 'deferido'}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(system.db.session.get(system.Agendamento, first.id).status, 'deferido')
        self.assertEqual(system.db.session.get(system.Agendamento, second.id).status, 'em_espera')
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 60)

    def test_parallel_decisions_debit_once_on_postgresql(self):
        if system.db.engine.dialect.name != "postgresql":
            self.skipTest("FOR UPDATE exige PostgreSQL para teste de concorrencia")

        agendamento = self._agendamento()
        agendamento_id = agendamento.id
        clients = [system.app.test_client(), system.app.test_client()]
        for client in clients:
            with client.session_transaction() as session:
                session["_user_id"] = str(self.admin.id)
                session["_fresh"] = True

        def decide(client):
            return client.post(
                "/deferir_folgas",
                data={"folga_id": agendamento_id, "status": "deferido"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(decide, clients))

        system.db.session.expire_all()
        self.assertEqual(sorted(statuses), [200, 302])
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 60)
        self.assertEqual(
            system.BancoDeHoras.query.filter_by(funcionario_id=self.staff.id, motivo="BH").count(),
            1,
        )

    def test_parallel_admin_requests_create_one_bh_appointment_on_postgresql(self):
        if system.db.engine.dialect.name != "postgresql":
            self.skipTest("FOR UPDATE exige PostgreSQL para teste de concorrencia")

        clients = [system.app.test_client(), system.app.test_client()]
        for client in clients:
            with client.session_transaction() as session:
                session["_user_id"] = str(self.admin.id)
                session["_fresh"] = True
        payload = {
            "user_id": self.staff.id, "tipo_folga": "BH", "motivo": "BH",
            "data": "2026-10-28", "quantidade_horas": "1", "quantidade_minutos": "0",
        }

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda client: client.post(
                "/admin/agendar_para", data=payload,
            ).status_code, clients))

        system.db.session.expire_all()
        self.assertTrue(all(status < 400 for status in statuses))
        self.assertEqual(system.Agendamento.query.filter_by(funcionario_id=self.staff.id, motivo="BH").count(), 1)
        self.assertEqual(system.db.session.get(system.User, self.staff.id).banco_horas, 60)


if __name__ == "__main__":
    unittest.main()
