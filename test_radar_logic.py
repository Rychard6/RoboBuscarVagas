import unittest
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

    def test_radar_scoring_matches_the_requested_priorities(self):
        self.assertEqual(
            CONFIG["radars"]["radar_a"]["schedule_scores"],
            {
                "night_confirmed": 95,
                "mixed": 75,
                "unknown": 65,
                "business_hours": 45,
            },
        )
        self.assertEqual(CONFIG["radars"]["radar_a"]["priority_thresholds"]["very_high"], 90)
        self.assertEqual(CONFIG["radars"]["radar_b"]["base_score"], 80)

    def test_radar_a_accepts_brasilia_night_support_and_scores_high(self):
        result = self.evaluate(
            "radar_a",
            "Analista de Suporte Júnior",
            "Contrato CLT, presencial em Brasília - DF, escala 12x36 no período "
            "noturno, atendimento de chamados e suporte a usuários, "
            "salário R$ 4.500,00 a R$ 5.000,00.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["priority"], "Muito alta prioridade")
        self.assertEqual(result["schedule"], "Noturno confirmado")
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
        self.assertEqual(result["priority"], "Média prioridade")
        self.assertIn("Horário não informado", result["fit_reason"])

    def test_radar_a_keeps_remote_job_pending_without_confirmed_brazil_scope(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, trabalho remoto. Atendimento técnico.",
        )

        self.assertFalse(result["eligible"])
        self.assertTrue(result["pending_confirmation"])
        self.assertIn(
            "Abrangência remota no Brasil não confirmada",
            result["confirmation_reasons"],
        )

    def test_radar_a_rejects_hybrid_location_outside_brasilia(self):
        result = self.evaluate(
            "radar_a",
            "Analista de Suporte Pleno",
            "Contrato PJ, modelo híbrido em São Paulo.",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("fora do DF", result["reason"])

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

    def test_unknown_location_does_not_override_an_explicit_outside_df_location(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, presencial em São Paulo, local a definir.",
        )

        self.assertFalse(result["eligible"])
        self.assertIn("fora do DF", result["reason"])

    def test_radar_a_accepts_remote_from_anywhere_in_brazil(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto em todo o Brasil. Atendimento de chamados.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["location"], "Remoto no Brasil")

    def test_radar_a_recognizes_remote_candidates_from_any_brazilian_region(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, vaga remota para candidatos de qualquer região do Brasil.",
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["location"], "Remoto no Brasil")

    def test_radar_a_accepts_each_contract_and_modality_without_contract_preference(self):
        examples = (
            ("CLT", "remoto no Brasil"),
            ("PJ", "híbrido em Brasília"),
            ("estágio", "presencial em Taguatinga"),
        )
        for contract, modality in examples:
            with self.subTest(contract=contract, modality=modality):
                result = self.evaluate(
                    "radar_a",
                    "Técnico de Suporte",
                    f"Contrato {contract}, {modality}. Atendimento técnico.",
                )
                self.assertTrue(result["eligible"])

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

        self.assertTrue(missing_contract["pending_confirmation"])
        self.assertTrue(missing_modality["pending_confirmation"])

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
        self.assertTrue(not_pj["pending_confirmation"])
        self.assertTrue(contract_options["eligible"])
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
        self.assertEqual(result["schedule_status"], "explicit_negative")
        self.assertIn("nega jornada noturna", result["incompatibilities"][0])
        self.assertNotIn("Horário noturno confirmado", result["fit_reason"])

    def test_contradictory_night_shift_remains_pending_for_confirmation(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto no Brasil. Horário não noturno, das 22h às 06h.",
        )
        self.assertFalse(result["eligible"])
        self.assertTrue(result["pending_confirmation"])
        self.assertEqual(result["schedule"], "Horário não informado")
        self.assertEqual(result["schedule_status"], "contradictory")
        self.assertIn(
            "Horário informado de forma contraditória",
            result["confirmation_reasons"],
        )

    def test_radar_a_accepts_specialized_security_role_in_technology(self):
        result = self.evaluate(
            "radar_a",
            "Analista Sênior de Segurança da Informação",
            "Contrato PJ, remoto no Brasil, turno noturno.",
        )

        self.assertTrue(result["eligible"])
        self.assertIn("security", result["fit_reason"])

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

        self.assertGreaterEqual(radar_a["score"], 60)
        self.assertEqual(radar_a["priority"], "Média prioridade")
        self.assertGreaterEqual(radar_b["score"], 80)
        self.assertEqual(radar_b["priority"], "Alta prioridade")

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

        self.assertEqual(with_growth["score"], base["score"] + 5)

    def test_publication_time_does_not_make_work_shift_night(self):
        result = self.evaluate(
            "radar_a",
            "Help Desk Júnior",
            "Contrato CLT, remoto no Brasil.",
            published_at="Publicado às 23:45",
        )

        self.assertEqual(result["schedule"], "Horário não informado")

    def test_twelve_hour_shift_without_hours_remains_unconfirmed(self):
        result = self.evaluate(
            "radar_a",
            "Operador de Sistemas Pleno",
            "Contrato CLT, remoto no Brasil, escala 12x36.",
        )

        self.assertTrue(result["eligible"])
        self.assertEqual(result["schedule"], "Horário não informado")

    def test_radar_a_classifies_overnight_ranges_and_12x36(self):
        examples = (
            "17h às 00h",
            "18h às 02h",
            "19h às 07h",
            "22:00 às 06:00",
            "escala 12x36 das 19h às 07h",
        )
        for schedule in examples:
            with self.subTest(schedule=schedule):
                result = self.evaluate(
                    "radar_a",
                    "Operador de Sistemas",
                    f"Contrato CLT, remoto no Brasil. {schedule}.",
                )
                self.assertEqual(result["schedule"], "Noturno confirmado")
                self.assertEqual(result["priority"], "Muito alta prioridade")
        for term in ("overnight", "graveyard shift", "turno da noite"):
            with self.subTest(term=term):
                result = self.evaluate(
                    "radar_a",
                    "Operador de Sistemas",
                    f"Contrato CLT, remoto no Brasil. {term}.",
                )
                self.assertEqual(result["schedule"], "Noturno confirmado")

    def test_radar_a_classifies_business_hours_mixed_and_unknown(self):
        examples = (
            ("das 09h às 18h", "Horário comercial", "Baixa prioridade"),
            (
                "turno diurno das 09h às 17h e turno noturno das 22h às 06h",
                "Misto",
                "Média prioridade",
            ),
            ("escala 12x36", "Horário não informado", "Média prioridade"),
        )
        for schedule, expected_classification, expected_priority in examples:
            with self.subTest(schedule=schedule):
                result = self.evaluate(
                    "radar_a",
                    "Técnico de Suporte",
                    f"Contrato CLT, remoto no Brasil. {schedule}.",
                )
                self.assertTrue(result["eligible"])
                self.assertEqual(result["schedule"], expected_classification)
                self.assertEqual(result["priority"], expected_priority)

    def test_radar_a_keeps_unknown_city_pending_but_rejects_explicit_outside_df(self):
        unknown = self.evaluate(
            "radar_a",
            "Analista de Suporte",
            "Contrato PJ, presencial, local a definir.",
        )
        outside = self.evaluate(
            "radar_a",
            "Analista de Suporte",
            "Contrato PJ, presencial em Goiânia, Goiás.",
        )
        self.assertTrue(unknown["pending_confirmation"])
        self.assertFalse(outside["eligible"])
        self.assertFalse(outside.get("pending_confirmation", False))

    def test_radar_b_missing_contract_or_modality_is_pending_not_assumed(self):
        missing_contract = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Remoto no Brasil. React e TypeScript.",
        )
        missing_modality = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ. React e TypeScript.",
        )
        not_pj = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Não PJ, remoto. React e TypeScript.",
        )
        self.assertTrue(missing_contract["pending_confirmation"])
        self.assertIn("Tipo de contrato não confirmado", missing_contract["confirmation_reasons"])
        self.assertTrue(missing_modality["pending_confirmation"])
        self.assertFalse(not_pj["eligible"])

    def test_radar_b_does_not_infer_seniority_from_low_experience(self):
        result = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end",
            "Contrato PJ, remoto. Experiência mínima com React.",
        )
        self.assertFalse(result["eligible"])
        self.assertTrue(result["pending_confirmation"])
        self.assertIn("Senioridade não confirmada", result["confirmation_reasons"])

    def test_radar_b_does_not_infer_pj_from_generic_contractor_type(self):
        result = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato contractor, remoto. React e TypeScript.",
        )
        self.assertFalse(result["eligible"])
        self.assertTrue(result["pending_confirmation"])

    def test_radar_b_rejects_explicitly_disallowed_or_conflicting_signals(self):
        contract_options = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ ou CLT, remoto. React.",
        )
        modality_options = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, remoto ou presencial. React.",
        )
        self.assertFalse(contract_options["eligible"])
        self.assertFalse(modality_options["eligible"])

    def test_radar_b_accepts_internship_terms_and_rejects_non_remote(self):
        internship = self.evaluate(
            "radar_b",
            "Internship em Suporte de TI",
            "Remoto. Atendimento de chamados.",
        )
        onsite = self.evaluate(
            "radar_b",
            "Desenvolvedor Back-end Pleno",
            "Contrato PJ, presencial em Brasília. Node.js.",
        )
        self.assertTrue(internship["eligible"])
        self.assertEqual(internship["contract"], "Estágio")
        self.assertEqual(internship["priority"], "Alta prioridade")
        self.assertFalse(onsite["eligible"])

    def test_radar_b_business_hours_add_score_without_changing_eligibility(self):
        commercial = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, remoto, horário comercial. React e TypeScript.",
        )
        unknown = self.evaluate(
            "radar_b",
            "Desenvolvedor Front-end Pleno",
            "Contrato PJ, remoto. React e TypeScript.",
        )
        self.assertTrue(commercial["eligible"])
        self.assertTrue(unknown["eligible"])
        self.assertEqual(commercial["score"], unknown["score"] + 5)

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
