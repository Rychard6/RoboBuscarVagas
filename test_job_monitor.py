import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import job_monitor
from job_monitor import JobSiteScraper


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

    def test_daily_digest_is_due_once_at_2330_utc_and_date_is_persisted(self):
        now = datetime(2025, 1, 2, 23, 30, tzinfo=timezone.utc)
        self.assertTrue(job_monitor.is_daily_digest_due(now, "", "schedule"))
        self.assertFalse(
            job_monitor.is_daily_digest_due(now, "2025-01-02", "schedule")
        )
        self.assertFalse(job_monitor.is_daily_digest_due(now, "", "workflow_dispatch"))
        self.assertFalse(
            job_monitor.is_daily_digest_due(
                datetime(2025, 1, 2, 23, 29, tzinfo=timezone.utc),
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

    def test_workflow_uses_requested_schedule_and_attempt_specific_cache_key(self):
        workflow = (
            Path(__file__).parent / ".github" / "workflows" / "job-monitor.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('cron: "30 3,7,11,15,19,23 * * *"', workflow)
        self.assertIn("github.run_id }}-${{ github.run_attempt", workflow)


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
        self.assertFalse(matches[0]["is_high_priority"])
        self.assertEqual(matches[0]["published_at"], "hoje")
        self.assertEqual(repeated, [])
        self.assertTrue(
            all(
                call.kwargs["payload"]["gl"] == "br"
                and call.kwargs["payload"]["hl"] == "pt-br"
                for call in post.await_args_list
            )
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


if __name__ == "__main__":
    unittest.main()
