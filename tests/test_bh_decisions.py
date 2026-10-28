import datetime
import os
import unittest
from unittest import mock

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
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


if __name__ == "__main__":
    unittest.main()
