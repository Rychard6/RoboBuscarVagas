import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from radar_logic import evaluate_job


ROOT = Path(__file__).parent
with open(ROOT / "sites_config.yaml", encoding="utf-8") as config_file:
    CONFIG = yaml.safe_load(config_file)["radars"]


class RadarEligibilityTests(unittest.TestCase):
    def evaluate(self, radar_id, title, description, published_at=""):
        return evaluate_job(
            {
                "title": title,
                "description": description,
                "published_at": published_at,
            },
            radar_id,
            CONFIG,
        )

    def test_radar_weights_match_the_requested_percentages(self):
        self.assertEqual(
            CONFIG["radars"]["radar_a"]["weights"],
            {
                "night_shift": 0.30,
                "activities": 0.30,
                "experience": 0.20,
                "contract_and_modality": 0.10,
                "compensation": 0.10,
            },
        )
        self.assertEqual(
            CONFIG["radars"]["radar_b"]["weights"],
            {
                "technical": 0.35,
                "seniority": 0.25,
                "contract": 0.20,
                "remote": 0.10,
                "career_growth": 0.10,
            },
        )

    def test_radar_a_accepts_brasilia_night_support_and_scores_high(self):
        result = self.evaluate(
            "radar_a",
            "Analista de Suporte Júnior",
            "Contrato CLT, presencial em Brasília - DF, escala 12x36 no período "
            "noturno, atendimento de chamados e suporte a usuários, "
            "salário R$ 4.500,00 a R$ 5.000,00.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["priority"], "Alta prioridade")
        self.assertEqual(result["schedule"], "Noturno / madrugada: noturno")
        self.assertEqual(result["contract"], "CLT")
        self.assertEqual(result["modality"], "Presencial")
        self.assertEqual(result["salary"], "R$ 4.500,00 a R$ 5.000,00")
        self.assertIn("Brasília", result["location"])

    def test_radar_a_keeps_compatible_remote_job_without_inventing_shift_or_salary(self):
        result = self.evaluate(
            "radar_a",
            "Técnico de Informática Júnior",
            "Contrato CLT, trabalho remoto em todo o Brasil. Suporte técnico.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["schedule"], "Horário não informado")
        self.assertEqual(result["salary"], "Não divulgado")
        self.assertEqual(result["priority"], "Baixa prioridade")
        self.assertIn("não foi presumido turno noturno", result["incompatibilities"][0])

    def test_radar_a_rejects_remote_job_without_confirmed_brazil_scope(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, trabalho remoto. Atendimento técnico.",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("Brasil não confirmada", result["reason"])

    def test_radar_a_rejects_hybrid_location_outside_brasilia(self):
        result = self.evaluate(
            "radar_a",
            "Analista de Suporte Pleno",
            "Contrato PJ, modelo híbrido em São Paulo.",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("fora da região de Brasília", result["reason"])

    def test_radar_a_rejects_entorno_and_disambiguates_planaltina(self):
        entorno = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, presencial em Valparaíso de Goiás.",
        )
        planaltina_go = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, presencial em Planaltina de Goiás.",
        )
        planaltina_df = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, presencial em Planaltina-DF.",
        )

        self.assertFalse(entorno["eligible"])
        self.assertFalse(planaltina_go["eligible"])
        self.assertTrue(planaltina_df["eligible"])
        self.assertEqual(planaltina_df["location"], "Planaltina-DF")

    def test_radar_a_requires_work_location_not_a_project_reference_to_df(self):
        project_reference = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato PJ, híbrido em São Paulo. O projeto atende o Distrito Federal.",
        )
        ambiguous_locations = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato PJ, presencial em Brasília ou São Paulo.",
        )

        self.assertFalse(project_reference["eligible"])
        self.assertFalse(ambiguous_locations["eligible"])

    def test_negated_work_location_does_not_confirm_a_df_vacancy(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, presencial em Brasília não disponível.",
        )

        self.assertFalse(result["eligible"])

    def test_radar_a_accepts_remote_from_anywhere_in_brazil(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto em todo o Brasil. Atendimento de chamados.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["location"], "Remoto no Brasil")

    def test_radar_a_rejects_unconfirmed_contract_or_modality(self):
        missing_contract = self.evaluate(
            "radar_a",
            "Operador de Sistemas",
            "Remoto no Brasil. Horário noturno.",
        )
        missing_modality = self.evaluate(
            "radar_a",
            "Operador de Sistemas",
            "Contrato CLT no Brasil. Horário noturno.",
        )

        self.assertFalse(missing_contract["eligible"])
        self.assertFalse(missing_modality["eligible"])

    def test_negated_and_conflicting_mandatory_signals_are_not_eligible(self):
        not_remote = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, a vaga não é remota. Atendimento de chamados.",
        )
        not_pj = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato não PJ, remoto no Brasil. React e TypeScript.",
        )
        contract_options = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato PJ ou CLT, presencial em Brasília.",
        )
        contradictory_remote = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, não remoto, mas remoto no Brasil. React.",
        )
        postposed_denial = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ não disponível, remoto no Brasil. React e TypeScript.",
        )

        self.assertFalse(not_remote["eligible"])
        self.assertFalse(not_pj["eligible"])
        self.assertFalse(contract_options["eligible"])
        self.assertFalse(contradictory_remote["eligible"])
        self.assertFalse(postposed_denial["eligible"])

    def test_not_available_in_brazil_does_not_confirm_national_remote_scope(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto. Não disponível no Brasil.",
        )

        self.assertFalse(result["eligible"])

    def test_negated_night_shift_does_not_score_as_night_work(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto no Brasil. A vaga não é noturna; atendimento a usuários.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["schedule"], "Horário não informado")
        self.assertNotIn("Turno noturno/madrugada indicado", result["fit_reason"])

    def test_radar_a_rejects_specialized_senior_security_role(self):
        result = self.evaluate(
            "radar_a",
            "Analista Sênior de Segurança da Informação",
            "Contrato PJ, remoto no Brasil, turno noturno.",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("especializado", result["reason"])

    def test_radar_b_accepts_remote_pj_mid_level_development_role(self):
        result = self.evaluate(
            "radar_b",
            "Desenvolvedor Back-end Pleno",
            "Contrato PJ, 100% remoto. Node.js, NestJS, APIs e microsserviços. "
            "Desenvolvimento de APIs, mentoria, projetos corporativos e formação "
            "em Engenharia de Computação.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["priority"], "Alta prioridade")
        self.assertEqual(result["contract"], "PJ")
        self.assertEqual(result["modality"], "Remoto")
        self.assertIn("Node.js", result["technologies"])
        self.assertIn("projetos corporativos", result["fit_reason"])
        self.assertIn("Formação relacionada", result["fit_reason"])

    def test_radar_b_rejects_clt_and_hybrid_roles_before_scoring(self):
        clt = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato CLT, remoto. React e TypeScript.",
        )
        hybrid = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, híbrido em Brasília. React e TypeScript.",
        )

        self.assertFalse(clt["eligible"])
        self.assertFalse(hybrid["eligible"])

    def test_radar_b_allows_senior_frontend_but_not_senior_fullstack(self):
        frontend = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Sênior",
            "Contrato PJ, remoto. React e TypeScript.",
        )
        fullstack = self.evaluate(
            "radar_b",
            "Desenvolvedor Full Stack Sênior",
            "Contrato PJ, remoto. React e Node.js.",
        )

        self.assertTrue(frontend["eligible"])
        self.assertFalse(fullstack["eligible"])

    def test_radar_b_accepts_remote_technology_internship(self):
        result = self.evaluate(
            "radar_b",
            "Estágio em Tecnologia da Informação",
            "Contrato de estágio, remoto no Brasil.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["contract"], "Estágio")

    def test_title_alone_cannot_create_high_technical_or_career_score(self):
        radar_a = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto no Brasil.",
        )
        radar_b = self.evaluate(
            "radar_b",
            "Estágio em Tecnologia da Informação",
            "Contrato de estágio, remoto no Brasil.",
        )

        self.assertLess(radar_a["score"], 60)
        self.assertLess(radar_b["score"], 60)

    def test_career_growth_score_requires_explicit_announcement_evidence(self):
        base = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, remoto no Brasil. React e TypeScript.",
        )
        with_growth = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, remoto no Brasil. React e TypeScript. Mentoria e plano de desenvolvimento.",
        )

        self.assertEqual(with_growth["score"], base["score"] + 10)

    def test_publication_time_does_not_make_work_shift_night(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto no Brasil.",
            published_at="Publicado às 23:45",
        )

        self.assertEqual(result["schedule"], "Horário não informado")

    def test_twelve_hour_shift_is_not_reported_as_confirmed_night_work(self):
        result = self.evaluate(
            "radar_a",
            "Operador de Sistemas Pleno",
            "Contrato CLT, remoto no Brasil, escala 12x36.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(
            result["schedule"], "Horário não informado; escala 12x36 indicada"
        )
        self.assertIn("Turno não confirmado", result["incompatibilities"][0])

    def test_javascript_does_not_match_java_technology(self):
        result = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, remoto. JavaScript e TypeScript.",
        )

        self.assertTrue(result["eligible"])
        self.assertNotIn("Java", result["technologies"])


if __name__ == "__main__":
    unittest.main()
