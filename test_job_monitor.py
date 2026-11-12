import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import job_monitor
from job_monitor import (
    JobSiteScraper,
    _extract_job_page_details,
    _job_page_host_allowed,
)


class JobHistoryTests(unittest.TestCase):
    def test_legacy_seen_ids_are_loaded_and_written_in_versioned_format(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            legacy_seen_at = time.time()
            state_path.write_text(
                json.dumps({"legacy-job-id": legacy_seen_at}), encoding="utf-8"
            )

            scraper = JobSiteScraper(str(state_path))

            self.assertEqual(scraper.seen_jobs["legacy-job-id"], legacy_seen_at)
            self.assertEqual(scraper.job_history, {})
            scraper.save_seen_jobs()
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertIn("legacy-job-id", saved["seen_jobs"])
            self.assertEqual(saved["jobs"], {})

    def test_pending_opportunities_persist_reload_and_print_per_radar(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.job_history["pending-job-id"] = {
                "title": "Desenvolvedor Front-end",
                "url": "https://board.example/vaga/pendente",
                "radars": {
                    "radar_a": {
                        "radar_label": "RADAR A — SEGUNDA RENDA",
                        "tracking_status": "nova",
                        "confirmation_status": "pending",
                        "confirmation_reasons": [
                            "Abrangência remota no Brasil não confirmada"
                        ],
                    },
                    "radar_b": {
                        "radar_label": "RADAR B — CARREIRA EM TECNOLOGIA",
                        "tracking_status": "interessada",
                        "confirmation_status": "pending",
                        "confirmation_reasons": ["Tipo de contrato não confirmado"],
                    },
                },
            }

            self.assertTrue(scraper.save_seen_jobs())
            restored = JobSiteScraper(str(state_path))
            output = io.StringIO()
            with redirect_stdout(output):
                job_monitor.print_history(restored)

            history = output.getvalue()
            self.assertIn("Desenvolvedor Front-end", history)
            self.assertIn("https://board.example/vaga/pendente", history)
            self.assertIn("RADAR A — SEGUNDA RENDA", history)
            self.assertIn("RADAR B — CARREIRA EM TECNOLOGIA", history)
            self.assertIn("Status de acompanhamento: nova", history)
            self.assertIn("Status de acompanhamento: interessada", history)
            self.assertEqual(history.count("Status de confirmação: Pendente"), 2)
            self.assertIn(
                "Motivo da pendência: Abrangência remota no Brasil não confirmada",
                history,
            )
            self.assertIn(
                "Motivo da pendência: Tipo de contrato não confirmado",
                history,
            )

    def test_one_url_can_be_listed_once_with_multiple_radar_scores(self):
        matches = [
            {
                "id": "same-job",
                "radar_id": "radar_a",
                "radar_label": "RADAR A — SEGUNDA RENDA",
                "score": 82,
                "priority": "Alta prioridade",
                "fit_reason": "Turno noturno",
                "incompatibilities": [],
            },
            {
                "id": "same-job",
                "radar_id": "radar_b",
                "radar_label": "RADAR B — CARREIRA EM TECNOLOGIA",
                "score": 75,
                "priority": "Média prioridade",
                "fit_reason": "Compatibilidade técnica",
                "incompatibilities": [],
            },
        ]

        combined = JobSiteScraper._combine_radar_matches(matches)

        self.assertEqual(len(combined), 1)
        self.assertEqual(set(combined[0]["radar_ids"]), {"radar_a", "radar_b"})
        self.assertTrue(combined[0]["is_high_priority"])

    def test_pending_confirmation_does_not_trigger_high_priority_delivery(self):
        combined = JobSiteScraper._combine_radar_matches(
            [
                {
                    "id": "same-job",
                    "radar_id": "radar_a",
                    "radar_label": "RADAR A",
                    "score": 100,
                    "priority": "Muito alta prioridade",
                    "confirmation_status": "pending",
                    "confirmation_reasons": ["Modalidade não confirmada"],
                }
            ]
        )

        self.assertFalse(combined[0]["is_high_priority"])

    def test_tracking_status_is_independent_for_each_radar(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.job_history["abcdef123456"] = {
                "tracking_status": "nova",
                "radars": {
                    "radar_a": {"tracking_status": "nova"},
                    "radar_b": {"tracking_status": "nova"},
                },
            }

            self.assertTrue(
                scraper.update_radar_tracking_status(
                    "abcdef12", "radar_a", "candidatura_enviada"
                )
            )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            record = saved["jobs"]["abcdef123456"]
            self.assertEqual(
                record["radars"]["radar_a"]["tracking_status"],
                "candidatura_enviada",
            )
            self.assertEqual(
                record["radars"]["radar_b"]["tracking_status"], "nova"
            )
            self.assertEqual(
                record["radars"]["radar_a"]["status_history"][-1]["status"],
                "candidatura_enviada",
            )

    def test_legacy_shared_status_migrates_without_overwriting_target_history(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.job_history["abcdef123456"] = {
                "tracking_status": "entrevista",
                "radars": {
                    "radar_a": {"tracking_status": "nova"},
                    "radar_b": {},
                },
            }

            self.assertTrue(
                scraper.update_radar_tracking_status(
                    "abcdef12", "radar_b", "interessada"
                )
            )
            record = json.loads(state_path.read_text(encoding="utf-8"))["jobs"][
                "abcdef123456"
            ]
            self.assertEqual(record["radars"]["radar_a"]["tracking_status"], "nova")
            self.assertEqual(
                record["radars"]["radar_b"]["tracking_status"], "interessada"
            )
            self.assertEqual(
                record["radars"]["radar_a"]["status_history"], []
            )

    def test_shared_opportunity_requires_explicit_radar_for_status_update(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.job_history["abcdef123456"] = {
                "radars": {"radar_a": {}, "radar_b": {}},
            }

            self.assertTrue(
                not scraper.update_tracking_status("abcdef12", "entrevista")
            )

    def test_short_status_id_is_not_allowed_to_match_an_opportunity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.job_history["abcdef123456"] = {
                "tracking_status": "nova",
                "radars": {},
            }

            self.assertFalse(scraper.update_tracking_status("abc", "entrevista"))
            self.assertEqual(
                scraper.job_history["abcdef123456"]["tracking_status"], "nova"
            )

    def test_digest_groups_pending_radar_results_for_one_opportunity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.job_history["same-job"] = {
                "title": "Desenvolvedor",
                "url": "https://example.test/jobs/1",
                "radars": {
                    "radar_a": {
                        "radar_id": "radar_a",
                        "radar_label": "RADAR A — SEGUNDA RENDA",
                        "score": 65,
                        "priority": "Média prioridade",
                        "delivery_status": "pending_digest",
                        "fit_reason": "Compatível",
                        "incompatibilities": [],
                    },
                    "radar_b": {
                        "radar_id": "radar_b",
                        "radar_label": "RADAR B — CARREIRA EM TECNOLOGIA",
                        "score": 55,
                        "priority": "Baixa prioridade",
                        "delivery_status": "pending_digest",
                        "fit_reason": "Compatível",
                        "incompatibilities": [],
                    },
                },
            }

            digest = scraper.get_pending_digest()

            self.assertEqual(len(digest), 1)
            self.assertEqual(len(digest[0]["radar_results"]), 2)
            self.assertFalse(digest[0]["is_high_priority"])
            scraper.mark_jobs_as_seen(digest)
            self.assertEqual(scraper.get_pending_digest(), [])

    def test_daily_digest_is_due_once_at_2355_utc_and_date_is_persisted(self):
        now = datetime(2025, 1, 2, 23, 55, tzinfo=timezone.utc)
        self.assertTrue(job_monitor.is_daily_digest_due(now, "", "schedule"))
        self.assertFalse(
            job_monitor.is_daily_digest_due(now, "2025-01-02", "schedule")
        )
        self.assertFalse(job_monitor.is_daily_digest_due(now, "", "workflow_dispatch"))
        self.assertFalse(
            job_monitor.is_daily_digest_due(
                datetime(2025, 1, 2, 23, 54, tzinfo=timezone.utc),
                "",
                "schedule",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            scraper.last_digest_date = "2025-01-02"
            self.assertTrue(scraper.save_seen_jobs())
            restored = JobSiteScraper(str(state_path))
            self.assertEqual(restored.last_digest_date, "2025-01-02")

    def test_status_message_reports_rejections_by_radar_and_reason(self):
        message = job_monitor.build_status_message(
            {
                "google_queries_executed": 6,
                "google_queries_available": 6,
                "jobs_rejected_radar": 3,
                "jobs_rejected_by_radar": {"radar_a": 1, "radar_b": 2},
                "job_rejection_reasons": {
                    "radar_a": {"Local fora do DF": 1},
                    "radar_b": {"Contrato não permitido": 2},
                },
                "jobs_pending_confirmation_by_radar": {"radar_a": 1, "radar_b": 0},
            },
            0,
            [],
        )

        self.assertIn("RADAR A: 1", message)
        self.assertIn("Local fora do DF: 1", message)
        self.assertIn("RADAR B: 2", message)
        self.assertIn("Contrato não permitido: 2", message)
        self.assertIn("RADAR A: 1", message.split("Pending confirmation — ")[1])

    def test_workflow_uses_requested_schedule_and_attempt_specific_cache_key(self):
        workflow = (
            Path(__file__).parent / ".github" / "workflows" / "job-monitor.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "55 3,7,11,15,19,23 * * *"', workflow)
        self.assertIn("github.run_id }}-${{ github.run_attempt", workflow)
        self.assertIn("branches:\n      - developer", workflow)
        self.assertEqual(workflow.count("key: seen-jobs-${{ github.run_id }}-${{ github.run_attempt }}"), 2)


class RadarSearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_search_result_is_deduplicated_and_tagged_for_both_radars(self):
        search_config = {
            "settings": {
                "enabled": True,
                "max_results_per_query": 10,
                "min_seconds_between_queries": 0,
                "query_jitter_max_seconds": 0,
                "max_consecutive_failures": 3,
                "serper_max_retries_per_query": 1,
                "serper_stop_on_rate_limit": True,
                "serper_time_filter": "",
                "serper_language": "pt-br",
                "serper_countries": ["br"],
            },
            "queries": {"radar_a": ["suporte"], "radar_b": ["desenvolvimento"]},
            "sites": [{"domain": "board.example", "name": "Quadro"}],
        }
        result = {
            "title": "Desenvolvedor Front-end Pleno",
            "link": "https://board.example/vaga/123?utm_source=google",
            "snippet": (
                "Contrato PJ, trabalho remoto em todo o Brasil, turno noturno. "
                "React e TypeScript."
            ),
            "date": "hoje",
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            scraper = JobSiteScraper(str(state_path))
            post = AsyncMock(return_value={"organic": [result]})
            with (
                patch.object(job_monitor, "SERPER_API_KEY", "test-key"),
                patch.object(
                    job_monitor, "load_google_search_config", return_value=search_config
                ),
                patch.object(job_monitor.http_client, "post_json", new=post),
            ):
                matches = await scraper.scrape_serper_search()
                scraper.mark_jobs_as_seen(matches)
                scraper.save_seen_jobs()
                restored = JobSiteScraper(str(state_path))
                repeated = await restored.scrape_serper_search()

        self.assertEqual(post.await_count, 4)
        self.assertEqual(len(matches), 1)
        self.assertEqual(set(matches[0]["radar_ids"]), {"radar_a", "radar_b"})
        self.assertTrue(matches[0]["is_high_priority"])
        self.assertEqual(matches[0]["published_at"], "hoje")
        self.assertEqual(repeated, [])
        self.assertTrue(
            all(
                call.kwargs["payload"]["gl"] == "br"
                and call.kwargs["payload"]["hl"] == "pt-br"
                for call in post.await_args_list
            )
        )

    async def test_missing_signals_are_persisted_pending_with_per_radar_stats(self):
            search_config = {
                "settings": {
                    "enabled": True,
                    "max_results_per_query": 10,
                    "min_seconds_between_queries": 0,
                    "query_jitter_max_seconds": 0,
                    "max_consecutive_failures": 3,
                    "serper_max_retries_per_query": 1,
                    "serper_stop_on_rate_limit": True,
                    "serper_time_filter": "",
                    "serper_language": "pt-br",
                    "serper_countries": ["br"],
                    "job_page_max_fetches": 5,
                },
                "queries": {"radar_a": ["suporte"], "radar_b": ["tecnologia"]},
                "sites": [{"domain": "board.example", "name": "Quadro"}],
            }
            result = {
                "title": "Desenvolvedor Front-end Pleno",
                "link": "https://board.example/vaga/pendente",
                "snippet": "React e TypeScript.",
            }
            with tempfile.TemporaryDirectory() as directory:
                scraper = JobSiteScraper(str(Path(directory) / "seen_jobs.json"))
                post = AsyncMock(return_value={"organic": [result]})
                get_page = AsyncMock(return_value=None)
                with (
                    patch.object(job_monitor, "SERPER_API_KEY", "test-key"),
                    patch.object(
                        job_monitor, "load_google_search_config", return_value=search_config
                    ),
                    patch.object(job_monitor.http_client, "post_json", new=post),
                    patch.object(job_monitor.http_client, "get_job_page", new=get_page),
                    self.assertLogs(job_monitor.logger, level="INFO") as logs,
                ):
                    matches = await scraper.scrape_serper_search()
                    scraper.log_operational_metrics()

            self.assertEqual(len(matches), 1)
            self.assertEqual(set(matches[0]["radar_ids"]), {"radar_a", "radar_b"})
            self.assertFalse(matches[0]["is_high_priority"])
            self.assertEqual(
                scraper.metrics["jobs_pending_confirmation_by_radar"],
                {"radar_a": 1, "radar_b": 1},
            )
            self.assertEqual(get_page.await_count, 1)
            self.assertIn("jobs_pending_confirmation_by_radar=", "\n".join(logs.output))
            self.assertIn("Pending confirmation radar=radar_a", "\n".join(logs.output))
            self.assertIn("https://board.example/vaga/pendente", "\n".join(logs.output))
            self.assertEqual(
                set(scraper.job_history[matches[0]["id"]]["radars"]),
                {"radar_a", "radar_b"},
            )

    async def test_failed_page_fetch_retries_and_successful_fetch_is_reused(self):
        search_config = {
            "settings": {
                "enabled": True,
                "max_results_per_query": 10,
                "min_seconds_between_queries": 0,
                "query_jitter_max_seconds": 0,
                "max_consecutive_failures": 3,
                "serper_max_retries_per_query": 1,
                "serper_stop_on_rate_limit": True,
                "serper_time_filter": "",
                "serper_language": "pt-br",
                "serper_countries": ["br"],
                "job_page_max_fetches": 5,
            },
            "queries": {"radar_a": ["vaga"], "radar_b": ["vaga"]},
            "sites": [{"domain": "board.example", "name": "Quadro"}],
        }
        result = {
            "title": "Desenvolvedor Front-end Pleno",
            "link": "https://board.example/vaga/retry",
            "snippet": "React e TypeScript.",
        }
        successful_page = """
        <html><head><title>Desenvolvedor Front-end Pleno</title></head>
        <body>Contrato CLT, remoto. Não disponível no Brasil.</body>
        </html>
        """
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen_jobs.json"
            state_path.write_text("{}", encoding="utf-8")
            post = AsyncMock(return_value={"organic": [result]})
            get_page = AsyncMock(side_effect=[None, successful_page])
            with (
                patch.object(job_monitor, "SERPER_API_KEY", "test-key"),
                patch.object(
                    job_monitor, "load_google_search_config", return_value=search_config
                ),
                patch.object(job_monitor.http_client, "post_json", new=post),
                patch.object(job_monitor.http_client, "get_job_page", new=get_page),
            ):
                first = JobSiteScraper(str(state_path))
                await first.scrape_serper_search()
                first_record = next(iter(first.job_history.values()))
                self.assertEqual(first_record["job_page_status"], "failed")
                self.assertGreater(first_record["job_page_last_attempt_at"], 0)
                self.assertEqual(first_record["job_page_succeeded_at"], 0)
                self.assertTrue(first.save_seen_jobs())

                second = JobSiteScraper(str(state_path))
                second_matches = await second.scrape_serper_search()
                cached_page = next(iter(second.job_page_cache.values()))
                self.assertEqual(cached_page["status"], "success")
                self.assertEqual(
                    cached_page["succeeded_at"],
                    cached_page["last_attempt_at"],
                )
                self.assertTrue(second.save_seen_jobs())

                third = JobSiteScraper(str(state_path))
                third_matches = await third.scrape_serper_search()

            self.assertEqual(get_page.await_count, 2)
            self.assertEqual(third.metrics["job_pages_attempted"], 0)
            self.assertEqual(
                third.metrics["job_pages_skipped_successfully_fetched"], 1
            )
            self.assertEqual(second_matches, [])
            self.assertEqual(third_matches, [])

    async def test_rejections_log_title_url_reason_and_per_radar_counts(self):
            search_config = {
                "settings": {
                    "enabled": True,
                    "max_results_per_query": 10,
                    "min_seconds_between_queries": 0,
                    "query_jitter_max_seconds": 0,
                    "max_consecutive_failures": 3,
                    "serper_max_retries_per_query": 1,
                    "serper_stop_on_rate_limit": True,
                    "serper_time_filter": "",
                    "serper_language": "pt-br",
                    "serper_countries": ["br"],
                },
                "queries": {"radar_a": ["vaga"], "radar_b": ["vaga"]},
                "sites": [{"domain": "board.example", "name": "Quadro"}],
            }
            result = {
                "title": "Assistente administrativo",
                "link": "https://board.example/vaga/fora-do-perfil",
                "snippet": "Contrato CLT, remoto.",
            }
            with tempfile.TemporaryDirectory() as directory:
                scraper = JobSiteScraper(str(Path(directory) / "seen_jobs.json"))
                with (
                    patch.object(job_monitor, "SERPER_API_KEY", "test-key"),
                    patch.object(
                        job_monitor, "load_google_search_config", return_value=search_config
                    ),
                    patch.object(
                        job_monitor.http_client,
                        "post_json",
                        new=AsyncMock(return_value={"organic": [result]}),
                    ),
                    self.assertLogs(job_monitor.logger, level="INFO") as logs,
                ):
                    matches = await scraper.scrape_serper_search()
                    scraper.log_operational_metrics()

            self.assertEqual(matches, [])
            self.assertEqual(
                scraper.metrics["jobs_rejected_by_radar"],
                {"radar_a": 1, "radar_b": 1},
            )
            log_text = "\n".join(logs.output)
            self.assertIn("Rejected radar=radar_a", log_text)
            self.assertIn("Rejected radar=radar_b", log_text)
            self.assertIn("Assistente administrativo", log_text)
            self.assertIn("https://board.example/vaga/fora-do-perfil", log_text)
            self.assertIn("reason=", log_text)
            self.assertIn("job_rejection_reasons=", log_text)

    async def test_original_job_page_can_confirm_missing_contract_modality_and_hours(self):
            search_config = {
                "settings": {
                    "enabled": True,
                    "max_results_per_query": 10,
                    "min_seconds_between_queries": 0,
                    "query_jitter_max_seconds": 0,
                    "max_consecutive_failures": 3,
                    "serper_max_retries_per_query": 1,
                    "serper_stop_on_rate_limit": True,
                    "serper_time_filter": "",
                    "serper_language": "pt-br",
                    "serper_countries": ["br"],
                    "job_page_max_fetches": 5,
                },
                "queries": {"radar_a": ["vaga"], "radar_b": ["vaga"]},
                "sites": [{"domain": "board.example", "name": "Quadro"}],
            }
            result = {
                "title": "Analista de Suporte",
                "link": "https://board.example/vaga/1",
                "snippet": "Atendimento de chamados.",
            }
            page = """
            <html><head><script type="application/ld+json">
            {"@type":"JobPosting","title":"Analista de Suporte Júnior",
             "description":"Contrato CLT, trabalho remoto em todo o Brasil. Escala 12x36 das 19h às 07h. Atendimento de chamados."}
            </script></head><body></body></html>
            """
            with tempfile.TemporaryDirectory() as directory:
                scraper = JobSiteScraper(str(Path(directory) / "seen_jobs.json"))
                with (
                    patch.object(job_monitor, "SERPER_API_KEY", "test-key"),
                    patch.object(
                        job_monitor, "load_google_search_config", return_value=search_config
                    ),
                    patch.object(
                        job_monitor.http_client,
                        "post_json",
                        new=AsyncMock(return_value={"organic": [result]}),
                    ),
                    patch.object(
                        job_monitor.http_client,
                        "get_job_page",
                        new=AsyncMock(return_value=page),
                    ) as get_page,
                ):
                    matches = await scraper.scrape_serper_search()

            self.assertEqual(get_page.await_count, 1)
            self.assertEqual(matches[0]["radar_ids"], ["radar_a"])
            self.assertEqual(matches[0]["schedule"], "Noturno confirmado")
            self.assertEqual(matches[0]["contract"], "CLT")
            self.assertEqual(matches[0]["modality"], "Remoto")
            self.assertEqual(scraper.metrics["job_pages_succeeded"], 1)

    def test_original_page_parser_reads_jobposting_fields_without_guessing_pj(self):
            page = """
            <html><head><title>Listing</title></head><body>
            <script type="application/ld+json">
            {"@type":"https://schema.org/JobPosting","title":"Estágio em Suporte",
             "employmentType":"INTERN","jobLocationType":"TELECOMMUTE",
             "workHours":"22h às 06h","jobLocation":{"address":{
               "addressLocality":"Brasília","addressRegion":"DF","addressCountry":"BR"}},
             "description":"Atendimento de chamados"}
            </script></body></html>
            """
            details = _extract_job_page_details(page)

            self.assertEqual(details["title"], "Estágio em Suporte")
            self.assertIn("Estágio", details["description"])
            self.assertIn("trabalho remoto", details["description"])
            self.assertIn("22h às 06h", details["description"])
            self.assertIn("Local de trabalho em Brasília, DF, Brasil", details["description"])
            self.assertNotIn("PJ", details["description"])

    def test_original_page_fetch_requires_https_allowlisted_host(self):
            domains = ["board.example", "linkedin.com/jobs"]
            self.assertTrue(_job_page_host_allowed("https://jobs.board.example/1", domains))
            self.assertTrue(_job_page_host_allowed("https://www.linkedin.com/jobs/1", domains))
            self.assertFalse(_job_page_host_allowed("http://board.example/1", domains))
            self.assertFalse(_job_page_host_allowed("https://untrusted.example/1", domains))
            self.assertFalse(
                _job_page_host_allowed("https://board.example@untrusted.example/1", domains)
            )

    async def test_telegram_message_includes_per_radar_scores_and_opportunity_fields(self):
        opportunity = {
            "id": "abcdef1234567890",
            "title": "Desenvolvedor <Front-end>",
            "url": "https://example.test/jobs/123",
            "contract": "PJ",
            "modality": "Remoto",
            "location": "Remoto no Brasil",
            "schedule": "Horário não informado",
            "salary": "Não divulgado",
            "technologies": ["React", "TypeScript"],
            "requirements": "Experiência com React",
            "published_at": "hoje",
            "tracking_status": "nova",
            "source": "Serper-Quadro",
            "radar_results": [
                {
                    "radar_label": "RADAR A — SEGUNDA RENDA",
                    "score": 82,
                    "priority": "Alta prioridade",
                    "fit_reason": "Turno conciliável",
                    "incompatibilities": ["Salário não divulgado"],
                },
                {
                    "radar_label": "RADAR B — CARREIRA EM TECNOLOGIA",
                    "score": 75,
                    "priority": "Média prioridade",
                    "fit_reason": "Experiência técnica compatível",
                    "incompatibilities": ["Horário não informado"],
                    "confirmation_status": "pending",
                    "confirmation_reasons": ["Senioridade não confirmada"],
                },
            ],
        }
        delivered = AsyncMock(return_value=True)
        with (
            patch.object(job_monitor, "TELEGRAM_BOT_TOKEN", "token"),
            patch.object(job_monitor, "TELEGRAM_CHAT_ID", "chat"),
            patch.object(job_monitor.http_client, "get_session", new=AsyncMock()),
            patch.object(job_monitor, "_deliver_telegram_message", new=delivered),
            patch.object(job_monitor.asyncio, "sleep", new=AsyncMock()),
        ):
            sent = await job_monitor.send_telegram_notification([opportunity])

        self.assertTrue(sent)
        message = delivered.await_args.args[2]["text"]
        self.assertIn("[RADAR A — SEGUNDA RENDA]", message)
        self.assertIn("[RADAR B — CARREIRA EM TECNOLOGIA]", message)
        self.assertIn("82/100", message)
        self.assertIn("75/100", message)
        self.assertIn("Empresa: Não informada", message)
        self.assertIn("Não divulgado", message)
        self.assertIn("Candidatura original", message)
        self.assertIn("&lt;Front-end&gt;", message)
        self.assertIn("Pendente de confirmação: Senioridade não confirmada", message)


if __name__ == "__main__":
    unittest.main()
