"""
Job Monitor Bot - searches Google (via Serper.dev) for jobs matching two independent
radars and sends ranked Telegram notifications.
Features: async search, retry logic, YAML config, structured logging, heartbeat alerts
"""

import argparse
import asyncio
import aiohttp
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from radar_logic import evaluate_job

# Keep emoji and Portuguese text in console reports usable on Windows terminals.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ============= LOGGING SETUP =============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('job_monitor')

# ============= CONFIGURATION FROM ENVIRONMENT =============
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Serper.dev — Google search results via a single API key (no Cloud project/CSE).
SERPER_API_KEY = os.getenv('SERPER_API_KEY', '')

# ============= LOAD YAML CONFIG =============
CONFIG_PATH = Path(__file__).parent / 'sites_config.yaml'
GOOGLE_SEARCH_CONFIG_PATH = Path(__file__).parent / 'google_search_sites.yaml'

def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        logger.warning(f"Config file not found at {CONFIG_PATH}, using defaults")
        return {'sites': {}, 'request': {}}
    except Exception as e:
        logger.error(f"Error loading config: {e}")
        return {'sites': {}, 'request': {}}

def load_google_search_config() -> dict:
    try:
        if GOOGLE_SEARCH_CONFIG_PATH.exists():
            with open(GOOGLE_SEARCH_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        logger.warning(f"Google search config not found at {GOOGLE_SEARCH_CONFIG_PATH}")
        return {'settings': {'enabled': False}, 'queries': {}, 'sites': []}
    except Exception as e:
        logger.error(f"Error loading Google search config: {e}")
        return {'settings': {'enabled': False}, 'queries': {}, 'sites': []}

CONFIG = load_config()
REQUEST_CONFIG = CONFIG.get('request', {})
RADAR_CONFIG = CONFIG.get('radars', {})
NOTIFICATIONS_CONFIG = CONFIG.get('notifications', {})
TIMEOUT = REQUEST_CONFIG.get('timeout', 15)
MAX_RETRIES = REQUEST_CONFIG.get('max_retries', 3)
RETRY_BASE_DELAY = REQUEST_CONFIG.get('retry_base_delay', 1.0)
RETRY_MAX_DELAY = REQUEST_CONFIG.get('retry_max_delay', 10.0)
CONCURRENT_LIMIT = REQUEST_CONFIG.get('concurrent_limit', 10)
SEEN_JOBS_MAX = REQUEST_CONFIG.get('seen_jobs_max', 5000)
SEEN_JOBS_TTL_DAYS = REQUEST_CONFIG.get('seen_jobs_ttl_days', 90)
JOB_HISTORY_MAX = REQUEST_CONFIG.get('job_history_max', SEEN_JOBS_MAX)
TELEGRAM_MAX_RETRIES = REQUEST_CONFIG.get('telegram_max_retries', 3)
TELEGRAM_RETRY_BASE_DELAY = REQUEST_CONFIG.get('telegram_retry_base_delay', 1.0)
TELEGRAM_RETRY_MAX_DELAY = REQUEST_CONFIG.get('telegram_retry_max_delay', 10.0)

SENSITIVE_QUERY_KEYS = {
    'access_token',
    'api_key',
    'authorization',
    'key',
    'password',
    'token',
}


def normalize_job_url(url: str) -> str:
    """Canonicalize a URL for dedup: lowercase scheme/host, drop the fragment, the trailing
    slash, and tracking query params. Two links to the same posting collapse to one string."""
    if not url:
        return ''
    try:
        parts = urlsplit(url.strip())
        netloc = parts.netloc.lower()
        path = parts.path.rstrip('/') or '/'
        clean_query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.lower()
            if lowered.startswith('utm_') or lowered in {'ref', 'source', 'fbclid', 'gclid'}:
                continue
            clean_query.append((key, value))
        return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(clean_query), ''))
    except Exception:
        return url.strip()


def redact_url(url: str) -> str:
    """Mask sensitive query parameters before logging."""
    if not url:
        return ''
    try:
        parts = urlsplit(url)
        redacted_query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key.lower() in SENSITIVE_QUERY_KEYS:
                redacted_query.append((key, 'REDACTED'))
            else:
                redacted_query.append((key, value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted_query), ''))
    except Exception:
        return url


def coerce_string_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        values = [str(item).strip().lower() for item in value if str(item).strip()]
        return values if values else default
    return default


class _JobPageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.visible_text: list[str] = []
        self.json_ld: list[str] = []
        self._title_depth = 0
        self._skip_depth = 0
        self._json_ld_depth = 0
        self._skipped_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            key = attributes.get("name", "") or attributes.get("property", "")
            content = attributes.get("content", "").strip()
            if key and content and key.lower() in {
                "description", "og:title", "og:description", "twitter:title",
                "twitter:description",
            }:
                self.meta.setdefault(key.lower(), content)
        elif tag == "title":
            self._title_depth += 1
        elif tag in {"script", "style", "noscript", "svg"}:
            if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
                self._json_ld_depth += 1
            else:
                self._skip_depth += 1
                self._skipped_tags.append(tag)

    def handle_endtag(self, tag: str):
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        elif tag == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1
        elif self._skipped_tags and self._skipped_tags[-1] == tag:
            self._skipped_tags.pop()
            self._skip_depth -= 1

    def handle_data(self, data: str):
        value = " ".join(data.split())
        if not value:
            return
        if self._json_ld_depth:
            self.json_ld.append(data)
        elif self._title_depth:
            self.title_parts.append(value)
        elif not self._skip_depth:
            self.visible_text.append(value)


def _plain_page_text(value: Any) -> str:
    return " ".join(re.sub(r"<[^>]*>", " ", html.unescape(str(value or ""))).split())


def _extract_job_page_details(page: str) -> dict[str, str]:
    parser = _JobPageParser()
    parser.feed(page)
    title = parser.meta.get("og:title") or parser.meta.get("twitter:title")
    if not title:
        title = " ".join(parser.title_parts)
    description_parts = [
        parser.meta.get("description", ""),
        parser.meta.get("og:description", ""),
        parser.meta.get("twitter:description", ""),
    ]
    structured_parts: list[str] = []
    structured_title = ""

    def visit(value: Any):
        nonlocal structured_title
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(str(job_type).rsplit("/", 1)[-1] == "JobPosting" for job_type in types):
            structured_title = structured_title or str(value.get("title") or "")
            if value.get("description"):
                description_parts.append(str(value["description"]))
            employment_type = value.get("employmentType", [])
            if isinstance(employment_type, str):
                employment_type = [employment_type]
            if any("intern" in str(item).lower() for item in employment_type):
                structured_parts.append("Estágio")
            elif any(str(item).strip().upper() == "PJ" for item in employment_type):
                structured_parts.append("PJ")
            remote_listing = str(value.get("jobLocationType", "")).upper() in {
                "TELECOMMUTE", "REMOTE",
            }
            if remote_listing:
                structured_parts.append("trabalho remoto")
            work_hours = value.get("workHours")
            if work_hours:
                structured_parts.append(str(work_hours))
            locations = value.get("jobLocation", [])
            if isinstance(locations, dict):
                locations = [locations]
            remote_in_brazil = False
            for location in locations:
                if not isinstance(location, dict):
                    continue
                address = location.get("address", {})
                if isinstance(address, dict):
                    locality = address.get("addressLocality")
                    region = address.get("addressRegion")
                    country = address.get("addressCountry")
                    country_name = (
                        "Brasil"
                        if str(country).upper() in {"BR", "BRA", "BRASIL", "BRAZIL"}
                        else str(country or "")
                    )
                    if remote_listing and country_name == "Brasil":
                        remote_in_brazil = True
                    address_parts = [
                        str(part) for part in (locality, region, country_name) if part
                    ]
                    if address_parts:
                        structured_parts.append(
                            "Local de trabalho em " + ", ".join(address_parts)
                        )
            if remote_in_brazil:
                structured_parts.append("Vaga remota no Brasil")
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                visit(nested)

    for raw_json in parser.json_ld:
        try:
            visit(json.loads(raw_json))
        except json.JSONDecodeError:
            continue

    body_text = " ".join(parser.visible_text)
    description_parts = structured_parts + description_parts + [body_text[:12000]]
    return {
        "title": structured_title or title,
        "description": " ".join(
            part for part in (_plain_page_text(value) for value in description_parts) if part
        )[:6000],
    }


def _job_page_host_allowed(url: str, domains: list[str]) -> bool:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            return False
        host = parts.hostname.lower().rstrip(".")
    except ValueError:
        return False
    return any(
        host == configured.split("/", 1)[0].lower()
        or host.endswith("." + configured.split("/", 1)[0].lower())
        for configured in domains
    )


# ============= SCRAPER HEALTH TRACKING =============
class ScraperHealth:
    def __init__(self):
        self.stats: dict[str, dict] = {}
    
    def record_success(self, site_name: str, job_count: int):
        if site_name not in self.stats:
            self.stats[site_name] = {'success': 0, 'failure': 0, 'jobs_found': 0}
        self.stats[site_name]['success'] += 1
        self.stats[site_name]['jobs_found'] += job_count
    
    def record_failure(self, site_name: str, error: str):
        if site_name not in self.stats:
            self.stats[site_name] = {'success': 0, 'failure': 0, 'jobs_found': 0, 'last_error': ''}
        self.stats[site_name]['failure'] += 1
        self.stats[site_name]['last_error'] = error
    
    def get_summary(self) -> str:
        lines = ["Scraper Health Summary:"]
        for site, stats in sorted(self.stats.items()):
            status = "✓" if stats['success'] > 0 else "✗"
            lines.append(f"  {status} {site}: {stats['jobs_found']} jobs, {stats['failure']} failures")
        return "\n".join(lines)
    
    def get_failed_sites(self) -> list[dict]:
        """Returns list of failed sites with their error reasons."""
        failed = []
        for site, stats in sorted(self.stats.items()):
            if stats['success'] == 0 and stats['failure'] > 0:
                failed.append({
                    'site': site,
                    'error': stats.get('last_error', 'Unknown error'),
                    'failures': stats['failure']
                })
        return failed
    
    def get_working_sites(self) -> list[dict]:
        """Returns list of working sites with job counts."""
        working = []
        for site, stats in sorted(self.stats.items()):
            if stats['success'] > 0:
                working.append({
                    'site': site,
                    'jobs_found': stats['jobs_found']
                })
        return working

health_tracker = ScraperHealth()

# ============= ASYNC HTTP CLIENT WITH RETRY =============
class AsyncHTTPClient:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
        self._per_domain_min_interval = REQUEST_CONFIG.get('per_domain_min_interval', 0.2)
        self._domain_last_request: dict[str, float] = {}
        self._domain_backoff_until: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
    
    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT)
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _apply_domain_throttle(self, domain: str):
        if not domain:
            return
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.time()
            next_allowed = max(
                self._domain_last_request.get(domain, 0) + self._per_domain_min_interval,
                self._domain_backoff_until.get(domain, 0)
            )
            delay = next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._domain_last_request[domain] = time.time()
    
    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
        max_retries_override: Optional[int] = None,
        fail_fast_on_rate_limit: bool = False,
        error_state: Optional[dict[str, Any]] = None,
    ) -> Optional[dict]:
        """POST a JSON body and return the parsed JSON response (or None on failure)."""
        attempts = max(1, int(max_retries_override if max_retries_override is not None else MAX_RETRIES))
        safe_url = redact_url(url)
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)
        if error_state is not None:
            error_state.clear()
            error_state.update({'last_error': None, 'status': None, 'rate_limited': False})

        async with self._semaphore:
            session = await self.get_session()
            last_error = None
            domain = urlsplit(url).netloc.lower()

            for attempt in range(attempts):
                try:
                    await self._apply_domain_throttle(domain)
                    async with session.post(url, json=payload, headers=merged_headers) as response:
                        if error_state is not None:
                            error_state.update({'status': response.status})
                        if response.status == 200:
                            text = await response.text()
                            if not text or not text.strip():
                                logger.warning(f"Empty response from {safe_url}")
                                return None
                            try:
                                return json.loads(text)
                            except json.JSONDecodeError as e:
                                logger.warning(f"Invalid JSON response from {safe_url}: {e}")
                                return None
                        elif response.status == 429:
                            last_error = "rate_limited"
                            if error_state is not None:
                                error_state.update({'last_error': last_error, 'rate_limited': True})
                            delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                            retry_after = response.headers.get('Retry-After')
                            if retry_after:
                                try:
                                    delay = max(delay, float(retry_after))
                                except ValueError:
                                    pass
                            if fail_fast_on_rate_limit:
                                logger.warning(f"Rate limited on {safe_url}; fail-fast enabled")
                                return None
                            logger.warning(f"Rate limited on {safe_url}, waiting {delay}s")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            last_error = f"http_{response.status}"
                            body_snippet = ''
                            try:
                                body_text = await response.text()
                                body_snippet = ' '.join((body_text or '').split())[:300]
                            except Exception:
                                pass
                            if error_state is not None:
                                error_state.update({'last_error': last_error, 'body': body_snippet})
                            logger.warning(
                                f"HTTP {response.status} for {safe_url}"
                                + (f": {body_snippet}" if body_snippet else "")
                            )
                            return None
                except asyncio.TimeoutError:
                    last_error = "timeout"
                    if error_state is not None:
                        error_state.update({'last_error': last_error})
                    logger.warning(f"Timeout posting to {safe_url} (attempt {attempt + 1}/{attempts})")
                except aiohttp.ClientError as e:
                    last_error = str(e)
                    if error_state is not None:
                        error_state.update({'last_error': last_error})
                    logger.warning(f"Client error posting to {safe_url}: {e} (attempt {attempt + 1}/{attempts})")
                except Exception as e:
                    logger.error(f"Unexpected error posting to {safe_url}: {e}")
                    return None

                if attempt < attempts - 1:
                    await asyncio.sleep(min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY))

            logger.error(f"Failed to POST {safe_url} after {attempts} attempts: {last_error}")
            if error_state is not None:
                error_state.update({'last_error': last_error})
            return None

    async def get_job_page(self, url: str, max_bytes: int = 1_000_000) -> Optional[str]:
        safe_url = redact_url(url)
        async with self._semaphore:
            session = await self.get_session()
            domain = urlsplit(url).netloc.lower()
            try:
                await self._apply_domain_throttle(domain)
                async with session.get(url, allow_redirects=False) as response:
                    content_type = response.headers.get("Content-Type", "").lower()
                    if response.status != 200 or not (
                        "text/html" in content_type or "application/xhtml+xml" in content_type
                    ):
                        logger.info(
                            "Could not read job page %s (status=%s, content_type=%s)",
                            safe_url,
                            response.status,
                            content_type or "unknown",
                        )
                        return None
                    body = await response.content.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        logger.info("Skipped oversized job page %s", safe_url)
                        return None
                    return body.decode(response.charset or "utf-8", errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                logger.info("Could not read job page %s: %s", safe_url, error)
                return None

http_client = AsyncHTTPClient()

# ============= JOB SITE SCRAPER =============
class JobSiteScraper:
    def __init__(self, seen_jobs_file: str = 'seen_jobs.json'):
        self.seen_jobs_file = seen_jobs_file
        self.job_history: dict[str, dict[str, Any]] = {}
        self.job_page_cache: dict[str, dict[str, Any]] = {}
        self.last_digest_date = ''
        self.seen_jobs = self.load_seen_jobs()
        self.metrics = {
            'google_queries_available': 0,
            'google_queries_executed': 0,
            'google_queries_skipped_by_budget': 0,
            'google_rate_limited': 0,
            'google_stopped_early_reason': '',
            'google_last_error': '',
            'jobs_rejected_radar': 0,
            'jobs_rejected_by_radar': {'radar_a': 0, 'radar_b': 0},
            'job_rejection_reasons': {'radar_a': {}, 'radar_b': {}},
            'jobs_pending_confirmation_by_radar': {'radar_a': 0, 'radar_b': 0},
            'job_pages_available': 0,
            'job_pages_attempted': 0,
            'job_pages_succeeded': 0,
            'job_pages_skipped_budget': 0,
            'job_pages_skipped_successfully_fetched': 0,
        }
    
    def load_seen_jobs(self) -> dict[str, float]:
        try:
            if os.path.exists(self.seen_jobs_file):
                with open(self.seen_jobs_file, 'r', encoding='utf-8') as f:
                    payload = json.load(f)
                    now = time.time()
                    if isinstance(payload, list):
                        return {job_id: now for job_id in payload if isinstance(job_id, str)}
                    if isinstance(payload, dict):
                        if 'seen_jobs' in payload:
                            self.job_history = (
                                payload.get('jobs', {})
                                if isinstance(payload.get('jobs'), dict)
                                else {}
                            )
                            self.job_page_cache = (
                                payload.get('job_page_cache', {})
                                if isinstance(payload.get('job_page_cache'), dict)
                                else {}
                            )
                            self.last_digest_date = str(
                                payload.get('last_digest_date') or ''
                            )
                            payload = payload.get('seen_jobs', {})
                            if not isinstance(payload, dict):
                                return {}
                        seen: dict[str, float] = {}
                        for job_id, ts in payload.items():
                            if isinstance(job_id, str):
                                try:
                                    seen[job_id] = float(ts)
                                except (TypeError, ValueError):
                                    seen[job_id] = now
                        return seen
            return {}
        except Exception as e:
            logger.error(f"Error loading seen jobs: {e}")
            return {}
    
    def save_seen_jobs(self) -> bool:
        try:
            self._prune_seen_jobs()
            with open(self.seen_jobs_file, 'w', encoding='utf-8') as f:
                json.dump(
                    {
                        'version': 2,
                        'seen_jobs': self.seen_jobs,
                        'jobs': self.job_history,
                        'job_page_cache': self.job_page_cache,
                        'last_digest_date': self.last_digest_date,
                    },
                    f,
                    ensure_ascii=False,
                )
            logger.info(
                f"Saved {len(self.seen_jobs)} deduplication records and "
                f"{len(self.job_history)} opportunity records"
            )
            return True
        except Exception as e:
            logger.error(f"Error saving seen jobs: {e}")
            return False

    def _prune_seen_jobs(self):
        now = time.time()
        ttl_seconds = SEEN_JOBS_TTL_DAYS * 24 * 60 * 60
        self.seen_jobs = {
            job_id: ts
            for job_id, ts in self.seen_jobs.items()
            if now - ts <= ttl_seconds
        }
        if len(self.seen_jobs) > SEEN_JOBS_MAX:
            newest_first = sorted(self.seen_jobs.items(), key=lambda item: item[1], reverse=True)
            self.seen_jobs = dict(newest_first[:SEEN_JOBS_MAX])
        self.job_history = {
            job_id: record
            for job_id, record in self.job_history.items()
            if now - float(record.get('updated_at', now)) <= ttl_seconds
        }
        if len(self.job_history) > JOB_HISTORY_MAX:
            newest_first = sorted(
                self.job_history.items(),
                key=lambda item: float(item[1].get('updated_at', 0)),
                reverse=True,
            )
            self.job_history = dict(newest_first[:JOB_HISTORY_MAX])
        self.job_page_cache = {
            job_id: record
            for job_id, record in self.job_page_cache.items()
            if now - float(record.get('updated_at', now)) <= ttl_seconds
        }
        if len(self.job_page_cache) > JOB_HISTORY_MAX:
            newest_first = sorted(
                self.job_page_cache.items(),
                key=lambda item: float(item[1].get('updated_at', 0)),
                reverse=True,
            )
            self.job_page_cache = dict(newest_first[:JOB_HISTORY_MAX])
    
    def generate_job_id(self, url: str) -> str:
        # Dedup on the normalized URL alone — it uniquely identifies a posting and is
        # immune to Google title/snippet drift and trailing-slash/host-case variants.
        return hashlib.md5(normalize_job_url(url).encode()).hexdigest()
    
    def mark_jobs_as_seen(self, jobs: list[dict[str, Any]]):
        now = time.time()
        for job in jobs:
            job_id = job.get('id', '')
            record = self.job_history.get(job_id, {})
            radar_ids = job.get('radar_ids') or [job.get('radar_id', '')]
            for radar_id in radar_ids:
                radar_record = record.get('radars', {}).get(radar_id)
                if radar_record:
                    radar_record['delivery_status'] = 'notified'
                    radar_record['notified_at'] = now
            record['updated_at'] = now
            record['notification_sent_at'] = now
            self.seen_jobs[job_id] = now

    def get_pending_digest(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for job_id, record in self.job_history.items():
            for radar_id, radar_record in record.get('radars', {}).items():
                if radar_record.get('delivery_status') != 'pending_digest':
                    continue
                pending.append(
                    {
                        **record,
                        **radar_record,
                        'id': job_id,
                        'radar_id': radar_id,
                    }
                )
        return self._combine_radar_matches(pending)

    @staticmethod
    def _combine_radar_matches(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            grouped.setdefault(job['id'], []).append(job)

        combined: list[dict[str, Any]] = []
        for matches in grouped.values():
            best = max(matches, key=lambda match: match.get('score', 0))
            radar_results = [
                {
                    key: match[key]
                    for key in (
                        'radar_id', 'radar_label', 'score', 'priority',
                        'fit_reason', 'incompatibilities', 'tracking_status',
                        'confirmation_status', 'confirmation_reasons',
                    )
                    if key in match
                }
                for match in matches
            ]
            combined.append(
                {
                    **best,
                    'radar_ids': [match['radar_id'] for match in matches],
                    'radar_results': radar_results,
                    'is_high_priority': any(
                        match.get('confirmation_status', 'confirmed') == 'confirmed'
                        and match.get('priority') in {
                            'Alta prioridade', 'Muito alta prioridade'
                        }
                        for match in matches
                    ),
                }
            )
        return combined

    def update_tracking_status(self, job_id_prefix: str, status: str) -> bool:
        return self.update_radar_tracking_status(job_id_prefix, None, status)

    def update_radar_tracking_status(
        self, job_id_prefix: str, radar_id: Optional[str], status: str
    ) -> bool:
        if len(job_id_prefix) < 8:
            return False
        matches = [
            job_id for job_id in self.job_history
            if job_id.startswith(job_id_prefix)
        ]
        if len(matches) != 1:
            return False
        job_id = matches[0]
        record = self.job_history[job_id]
        radar_records = record.get('radars', {})
        if radar_id is None:
            if len(radar_records) != 1:
                return False
            radar_id = next(iter(radar_records))
        if radar_id not in radar_records:
            return False
        radar_record = radar_records[radar_id]
        legacy_status = record.get('tracking_status')
        for existing in radar_records.values():
            if legacy_status:
                existing.setdefault('tracking_status', legacy_status)
            if not isinstance(existing.get('status_history'), list):
                existing['status_history'] = []
        radar_record['tracking_status'] = status
        radar_record['status_history'].append(
            {'status': status, 'updated_at': time.time()}
        )
        record['updated_at'] = time.time()
        return self.save_seen_jobs()

    def log_operational_metrics(self):
        logger.info(
            "Operational Metrics:\n"
            f"  google_queries_available={self.metrics['google_queries_available']}\n"
            f"  google_queries_executed={self.metrics['google_queries_executed']}\n"
            f"  google_queries_skipped_by_budget={self.metrics['google_queries_skipped_by_budget']}\n"
            f"  google_rate_limited={self.metrics['google_rate_limited']}\n"
            f"  google_stopped_early_reason={self.metrics['google_stopped_early_reason'] or 'none'}\n"
            f"  google_last_error={self.metrics['google_last_error'] or 'none'}\n"
            f"  jobs_rejected_radar={self.metrics['jobs_rejected_radar']}\n"
            f"  jobs_rejected_by_radar={json.dumps(self.metrics['jobs_rejected_by_radar'], ensure_ascii=False, sort_keys=True)}\n"
            f"  job_rejection_reasons={json.dumps(self.metrics['job_rejection_reasons'], ensure_ascii=False, sort_keys=True)}\n"
            f"  jobs_pending_confirmation_by_radar={json.dumps(self.metrics['jobs_pending_confirmation_by_radar'], ensure_ascii=False, sort_keys=True)}\n"
            f"  job_pages_available={self.metrics['job_pages_available']}\n"
            f"  job_pages_attempted={self.metrics['job_pages_attempted']}\n"
            f"  job_pages_succeeded={self.metrics['job_pages_succeeded']}\n"
            f"  job_pages_skipped_budget={self.metrics['job_pages_skipped_budget']}\n"
            f"  job_pages_skipped_successfully_fetched={self.metrics['job_pages_skipped_successfully_fetched']}"
        )

    # ============= SERPER.DEV (GOOGLE RESULTS VIA SINGLE API KEY) =============
    async def scrape_serper_search(self) -> list[dict]:
        """Search both radars, deduplicate URLs, then apply their mandatory rules and scores."""
        candidates: list[dict[str, Any]] = []
        jobs_by_id: dict[str, dict[str, Any]] = {}
        site_name = "SerperSearch"

        if not SERPER_API_KEY:
            logger.debug(f"{site_name}: Skipped (no SERPER_API_KEY)")
            return candidates

        search_config = load_google_search_config()
        settings = search_config.get('settings', {})

        if not settings.get('enabled', False):
            logger.debug(f"{site_name}: Disabled in config")
            return candidates

        queries_by_radar = search_config.get('queries', {})
        sites = search_config.get('sites', [])
        max_results = settings.get('max_results_per_query', 10)
        max_job_page_fetches = max(0, int(settings.get('job_page_max_fetches', 20)))
        min_seconds_between_queries = max(0.0, float(settings.get('min_seconds_between_queries', 1.0)))
        max_consecutive_failures = max(1, int(settings.get('max_consecutive_failures', 3)))
        serper_max_retries_per_query = max(1, int(settings.get('serper_max_retries_per_query', 2)))
        serper_stop_on_rate_limit = bool(settings.get('serper_stop_on_rate_limit', True))
        # Serper time filter: qdr:h/d/w/m/y (hour/day/week/month/year). Empty = no filter.
        serper_time_filter = str(settings.get('serper_time_filter', 'qdr:d')).strip()
        serper_language = str(settings.get('serper_language', '')).strip()
        # Country bias(es) for Google ranking (gl). One request is sent per (query, country),
        # so total searches = configured query groups × countries. Accepts a list or string,
        # and the legacy single 'serper_country'. Empty => one unbiased search.
        raw_countries = settings.get('serper_countries', settings.get('serper_country', ''))
        if isinstance(raw_countries, str):
            countries = [c.strip().lower() for c in raw_countries.split(',') if c.strip()]
        elif isinstance(raw_countries, list):
            countries = [str(c).strip().lower() for c in raw_countries if str(c).strip()]
        else:
            countries = []
        if not countries:
            countries = ['']  # single unbiased query
        negative_terms = coerce_string_list(
            settings.get('query_negative_terms'),
            []
        )
        jitter_max_seconds = max(0.0, min(float(settings.get('query_jitter_max_seconds', 0.4)), 2.0))

        if not queries_by_radar or not sites:
            logger.warning(f"{site_name}: No radar queries or sites configured")
            return candidates

        # Map result domains back to friendly board names for the alert "source" label.
        domain_to_name = {
            s['domain']: s.get('name', s['domain'])
            for s in sites if s.get('domain')
        }
        domains = list(domain_to_name.keys())

        try:
            site_clause = "(" + " OR ".join(f"site:{d}" for d in domains) + ")"
            negative_clause = " ".join(
                f"-{term.lstrip('-').strip()}"
                for term in negative_terms
                if term and term.lstrip('-').strip()
            )
            query_specs: list[tuple[str, str, str]] = []
            for radar_id, radar_queries in queries_by_radar.items():
                if radar_id not in RADAR_CONFIG.get('radars', {}):
                    logger.warning(f"{site_name}: Ignoring unknown radar '{radar_id}'")
                    continue
                for radar_query in radar_queries or []:
                    query_parts = [str(radar_query), site_clause]
                    if negative_clause:
                        query_parts.append(negative_clause)
                    for gl in countries:
                        query_specs.append((radar_id, " ".join(query_parts), gl))
            self.metrics['google_queries_available'] = len(query_specs)

            total_queries = 0
            consecutive_failures = 0

            def next_query_delay() -> float:
                return min_seconds_between_queries + random.uniform(0.0, jitter_max_seconds)

            for radar_id, query, gl in query_specs:
                payload: dict[str, Any] = {'q': query, 'num': max_results}
                if serper_time_filter:
                    payload['tbs'] = serper_time_filter
                if gl:
                    payload['gl'] = gl
                if serper_language:
                    payload['hl'] = serper_language

                error_state: dict[str, Any] = {}
                data = await http_client.post_json(
                    "https://google.serper.dev/search",
                    payload=payload,
                    headers={'X-API-KEY': SERPER_API_KEY},
                    max_retries_override=serper_max_retries_per_query,
                    fail_fast_on_rate_limit=serper_stop_on_rate_limit,
                    error_state=error_state,
                )
                total_queries += 1
                self.metrics['google_queries_executed'] = total_queries

                if error_state.get('rate_limited'):
                    self.metrics['google_rate_limited'] += 1
                    if serper_stop_on_rate_limit:
                        self.metrics['google_stopped_early_reason'] = 'rate_limited'
                        logger.warning(f"{site_name}: Stopping early due to Serper rate-limit response")
                        break

                if not data:
                    status = error_state.get('status')
                    detail = error_state.get('body') or error_state.get('last_error') or 'no response'
                    self.metrics['google_last_error'] = f"HTTP {status}: {detail}"
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        if not self.metrics['google_stopped_early_reason']:
                            self.metrics['google_stopped_early_reason'] = 'consecutive_failures'
                        logger.warning(
                            f"{site_name}: Stopping early after {consecutive_failures} consecutive failed queries"
                        )
                        break
                    await asyncio.sleep(next_query_delay())
                    continue

                consecutive_failures = 0
                for item in data.get('organic', []) or []:
                    link = item.get('link', '')
                    title = str(item.get('title', '')).strip()
                    job_url = normalize_job_url(link)
                    if not title or not job_url:
                        continue
                    company = ''
                    if ' - ' in title:
                        title, company = (part.strip() for part in title.rsplit(' - ', 1))
                    elif ' | ' in title:
                        title, company = (part.strip() for part in title.rsplit(' | ', 1))
                    job_id = self.generate_job_id(job_url)
                    # Preserve the old ID-only state without treating this run's duplicates
                    # across source/radar queries as already notified.
                    if job_id in self.seen_jobs and job_id not in self.job_history:
                        continue
                    netloc = urlsplit(link).netloc.lower()
                    board = next(
                        (
                            name for dom, name in domain_to_name.items()
                            if dom.split('/')[0] in netloc
                        ),
                        'Search',
                    )
                    source = f"Serper-{board}"
                    description = str(item.get('snippet', '')).strip()
                    if job_id not in jobs_by_id:
                        previous_record = self.job_history.get(job_id, {})
                        cached_page = self.job_page_cache.get(job_id, {})
                        page_was_fetched = (
                            cached_page.get('status') == 'success'
                            and cached_page.get('succeeded_at')
                        ) or (
                            previous_record.get('job_page_status') == 'success'
                            and previous_record.get('job_page_succeeded_at')
                        )
                        page_details = (
                            cached_page
                            if cached_page.get('status') == 'success'
                            else previous_record
                        )
                        jobs_by_id[job_id] = {
                            'id': job_id,
                            'title': (
                                page_details.get('title', title)
                                if page_was_fetched else title
                            ),
                            'company': (
                                page_details.get('company', company)
                                if page_was_fetched else company
                            ),
                            'url': job_url,
                            'source': source,
                            'sources': [source],
                            'description': (
                                page_details.get('description', description)
                                if page_was_fetched else description
                            ),
                            'job_page_checked_at': (
                                cached_page.get(
                                    'last_attempt_at',
                                    previous_record.get('job_page_checked_at', 0),
                                )
                                if page_was_fetched else 0
                            ),
                            'job_page_last_attempt_at': (
                                cached_page.get(
                                    'last_attempt_at',
                                    previous_record.get('job_page_last_attempt_at', 0),
                                )
                                if page_was_fetched else 0
                            ),
                            'job_page_status': 'success' if page_was_fetched else '',
                            'job_page_succeeded_at': (
                                cached_page.get(
                                    'succeeded_at',
                                    previous_record.get('job_page_succeeded_at', 0),
                                )
                                if page_was_fetched else 0
                            ),
                            'published_at': item.get('date') or item.get('datePublished') or '',
                        }
                    else:
                        existing = jobs_by_id[job_id]
                        if source not in existing['sources']:
                            existing['sources'].append(source)
                        if len(description) > len(existing['description']):
                            existing['description'] = description
                        if not existing.get('published_at'):
                            existing['published_at'] = (
                                item.get('date') or item.get('datePublished') or ''
                            )

                await asyncio.sleep(next_query_delay())

            page_candidates = []
            for job in jobs_by_id.values():
                previous_job_record = self.job_history.get(job['id'], {})
                cached_page = self.job_page_cache.get(job['id'], {})
                if (
                    (
                        cached_page.get('status') == 'success'
                        and cached_page.get('succeeded_at')
                    )
                    or (
                        previous_job_record.get('job_page_status') == 'success'
                        and previous_job_record.get('job_page_succeeded_at')
                    )
                ):
                    self.metrics['job_pages_skipped_successfully_fetched'] += 1
                    continue
                evaluations = [
                    evaluate_job(job, radar_id, RADAR_CONFIG)
                    for radar_id in queries_by_radar
                    if radar_id in RADAR_CONFIG.get('radars', {})
                ]
                needs_page = any(
                    result.get('pending_confirmation')
                    or (
                        result.get('radar_id') == 'radar_a'
                        and result.get('schedule') == 'Horário não informado'
                    )
                    for result in evaluations
                )
                if not needs_page:
                    continue
                if _job_page_host_allowed(job['url'], domains):
                    page_candidates.append(
                        (
                            0 if any(
                                result.get('pending_confirmation')
                                for result in evaluations
                            ) else 1,
                            job,
                        )
                    )
            self.metrics['job_pages_available'] = len(page_candidates)
            page_candidates.sort(key=lambda item: item[0])
            page_attempts = [
                job for _, job in page_candidates[:max_job_page_fetches]
            ]
            self.metrics['job_pages_attempted'] = len(page_attempts)
            self.metrics['job_pages_skipped_budget'] = (
                len(page_candidates) - len(page_attempts)
            )
            page_html_results = await asyncio.gather(
                *(
                    http_client.get_job_page(job['url'])
                    for job in page_attempts
                )
            )
            for job, page_html in zip(page_attempts, page_html_results):
                attempted_at = time.time()
                page_cache_record = self.job_page_cache.get(job['id'], {}).copy()
                job['job_page_checked_at'] = attempted_at
                job['job_page_last_attempt_at'] = attempted_at
                job['job_page_status'] = 'success' if page_html else 'failed'
                page_cache_record.update(
                    {
                        'status': job['job_page_status'],
                        'last_attempt_at': attempted_at,
                        'updated_at': attempted_at,
                    }
                )
                if page_html:
                    job['job_page_succeeded_at'] = attempted_at
                    page_cache_record['succeeded_at'] = attempted_at
                    self.metrics['job_pages_succeeded'] += 1
                else:
                    job['job_page_succeeded_at'] = page_cache_record.get(
                        'succeeded_at', 0
                    )
                page_cache_record.update(
                    {'title': job['title'], 'description': job['description']}
                )
                self.job_page_cache[job['id']] = page_cache_record
                if not page_html:
                    continue
                try:
                    details = _extract_job_page_details(page_html)
                except (RecursionError, ValueError) as error:
                    logger.info(
                        "Could not parse job page %s: %s",
                        redact_url(job['url']),
                        error,
                    )
                    continue
                if details['title']:
                    job['title'] = details['title']
                if details['description']:
                    job['description'] = " ".join(
                        dict.fromkeys(
                            part
                            for part in (job['description'], details['description'])
                            if part
                        )
                    )
                if details['title'] or details['description']:
                    page_cache_record.update(
                        {'title': job['title'], 'description': job['description']}
                    )

            for job in jobs_by_id.values():
                job['source'] = ', '.join(job['sources'])
                for radar_id in queries_by_radar:
                    if radar_id not in RADAR_CONFIG.get('radars', {}):
                        continue
                    result = evaluate_job(job, radar_id, RADAR_CONFIG)
                    if not result.get('eligible') and not result.get(
                        'pending_confirmation'
                    ):
                        self.metrics['jobs_rejected_radar'] += 1
                        self.metrics['jobs_rejected_by_radar'][radar_id] += 1
                        reason = result.get('reason', 'not eligible')
                        reasons = self.metrics['job_rejection_reasons'][radar_id]
                        reasons[reason] = reasons.get(reason, 0) + 1
                        previous_record = self.job_history.get(job['id'], {})
                        previous_radar = previous_record.get('radars', {}).get(radar_id, {})
                        if previous_radar.get('delivery_status') == 'pending_digest':
                            previous_radar['delivery_status'] = 'ineligible'
                            previous_record['updated_at'] = time.time()
                        logger.info(
                            "Rejected radar=%s title=%r url=%s reason=%s",
                            radar_id,
                            job['title'],
                            redact_url(job['url']),
                            reason,
                        )
                        continue
                    if result.get('pending_confirmation'):
                        self.metrics['jobs_pending_confirmation_by_radar'][radar_id] += 1
                        logger.info(
                            "Pending confirmation radar=%s title=%r url=%s reasons=%s",
                            radar_id,
                            job['title'],
                            redact_url(job['url']),
                            "; ".join(result.get('confirmation_reasons', [])),
                        )

                    record = self.job_history.setdefault(
                        job['id'],
                        {
                            'title': job['title'],
                            'company': job['company'],
                            'url': job['url'],
                            'source': job['source'],
                            'sources': job['sources'],
                            'description': job['description'],
                            'published_at': job['published_at'],
                            'job_page_checked_at': job.get('job_page_checked_at', 0),
                            'job_page_last_attempt_at': job.get(
                                'job_page_last_attempt_at', 0
                            ),
                            'job_page_status': job.get('job_page_status', ''),
                            'job_page_succeeded_at': job.get(
                                'job_page_succeeded_at',
                                self.job_page_cache.get(job['id'], {}).get(
                                    'succeeded_at', 0
                                ),
                            ),
                            'tracking_status': 'nova',
                            'radars': {},
                            'created_at': time.time(),
                        },
                    )
                    record.update(
                        {
                            'title': job['title'],
                            'company': job['company'],
                            'url': job['url'],
                            'source': job['source'],
                            'sources': job['sources'],
                            'description': job['description'],
                            'published_at': job['published_at'] or record.get('published_at', ''),
                            'job_page_checked_at': (
                                job.get('job_page_checked_at')
                                or record.get('job_page_checked_at', 0)
                            ),
                            'job_page_last_attempt_at': job.get(
                                'job_page_last_attempt_at',
                                record.get('job_page_last_attempt_at', 0),
                            ),
                            'job_page_status': job.get(
                                'job_page_status', record.get('job_page_status', '')
                            ),
                            'job_page_succeeded_at': job.get(
                                'job_page_succeeded_at',
                                self.job_page_cache.get(job['id'], {}).get(
                                    'succeeded_at',
                                    record.get('job_page_succeeded_at', 0),
                                ),
                            ),
                            'updated_at': time.time(),
                        }
                    )
                    previous = record.setdefault('radars', {}).get(radar_id, {})
                    radar_record = {
                        key: value
                        for key, value in result.items()
                        if key not in {'eligible', 'tracking_status'}
                    }
                    radar_record.update(
                        {
                            'delivery_status': (
                                'pending_digest'
                                if previous.get('delivery_status') in {None, 'ineligible'}
                                else previous['delivery_status']
                            ),
                            'tracking_status': previous.get(
                                'tracking_status', record.get('tracking_status', 'nova')
                            ),
                            'first_seen_at': previous.get('first_seen_at', time.time()),
                        }
                    )
                    record['radars'][radar_id] = radar_record
                    if record.get('notification_sent_at'):
                        radar_record['delivery_status'] = 'notified'
                        continue
                    if radar_record['delivery_status'] == 'notified':
                        continue
                    candidates.append(
                        {
                            **job,
                            **result,
                            'tracking_status': radar_record['tracking_status'],
                        }
                    )

            health_tracker.record_success(site_name, len(jobs_by_id))
            candidates = self._combine_radar_matches(candidates)
            logger.info(
                f"{site_name}: {len(jobs_by_id)} unique results from {total_queries} queries; "
                f"{len(candidates)} radar matches"
            )
        except Exception as e:
            health_tracker.record_failure(site_name, str(e))
            logger.error(f"{site_name} error: {e}")

        return candidates

# ============= TELEGRAM NOTIFICATION =============
async def _deliver_telegram_message(session, url: str, payload: dict) -> bool:
    """Send a single Telegram message with retry/backoff. Returns True on success."""
    for attempt in range(TELEGRAM_MAX_RETRIES):
        try:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    return True

                error_text = await response.text()
                retriable = response.status in (429, 500, 502, 503, 504)
                if not retriable:
                    logger.error(f"Telegram API error (status {response.status}): {error_text}")
                    return False

                delay = min(TELEGRAM_RETRY_BASE_DELAY * (2 ** attempt), TELEGRAM_RETRY_MAX_DELAY)
                if response.status == 429:
                    try:
                        error_payload = json.loads(error_text)
                        retry_after = float(error_payload.get('parameters', {}).get('retry_after', 0))
                        delay = max(delay, retry_after)
                    except Exception:
                        pass
                logger.warning(
                    f"Telegram API temporary error (status {response.status}), "
                    f"retrying in {delay}s (attempt {attempt + 1}/{TELEGRAM_MAX_RETRIES})"
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            delay = min(TELEGRAM_RETRY_BASE_DELAY * (2 ** attempt), TELEGRAM_RETRY_MAX_DELAY)
            logger.warning(
                f"Telegram delivery failed ({e}), retrying in {delay}s "
                f"(attempt {attempt + 1}/{TELEGRAM_MAX_RETRIES})"
            )
        except Exception as e:
            logger.error(f"Unexpected Telegram error: {e}")
            return False

        if attempt < TELEGRAM_MAX_RETRIES - 1:
            await asyncio.sleep(delay)

    logger.error("Telegram delivery failed after retries")
    return False


async def send_telegram_notification(
    jobs: list[dict],
    daily_digest: bool = False,
    on_delivered: Optional[Callable[[list[dict]], Any]] = None,
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured")
        return False
    
    if not jobs:
        logger.info("No new jobs to notify")
        return True
    
    try:
        session = await http_client.get_session()

        def _escape_text(value: str, max_length: int, fallback: str = 'Unknown') -> str:
            normalized = str(value or '').strip()
            if not normalized:
                normalized = fallback
            return html.escape(normalized[:max_length], quote=False)

        header_title = (
            f"📬 <b>Resumo diário — {len(jobs)} oportunidade(s)</b>"
            if daily_digest
            else f"🚨 <b>{len(jobs)} vaga(s) de alta prioridade</b>"
        )
        header = f"{header_title}\n"
        header += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        header += "─" * 30 + "\n\n"

        messages: list[tuple[str, list[dict]]] = []
        current_message = header
        current_jobs: list[dict] = []

        for i, job in enumerate(jobs, 1):
            title = _escape_text(job.get('title', ''), 100)
            company = _escape_text(job.get('company', ''), 50, fallback='Não informada')
            source = _escape_text(job.get('source', ''), 60)
            contract = _escape_text(job.get('contract', ''), 40)
            modality = _escape_text(job.get('modality', ''), 40)
            location = _escape_text(job.get('location', ''), 100)
            schedule = _escape_text(job.get('schedule', ''), 100)
            salary = _escape_text(job.get('salary', ''), 60)
            technologies = _escape_text(
                ', '.join(job.get('technologies', [])), 180, fallback='Não identificadas'
            )
            requirements = _escape_text(
                job.get('requirements', ''), 350, fallback='Não informado no resultado público'
            )
            published_at = _escape_text(job.get('published_at', ''), 60, fallback='Não informada')
            url = html.escape(job.get('url', ''), quote=True)
            short_id = _escape_text(job.get('id', '')[:12], 12)

            job_text = f"<b>{i}. {title}</b>\n"
            job_text += f"🏢 Empresa: {company}\n"
            for result in job.get('radar_results', []):
                radar_label = _escape_text(result.get('radar_label', ''), 80)
                priority = _escape_text(result.get('priority', ''), 40)
                fit_reason = _escape_text(result.get('fit_reason', ''), 300)
                incompatibilities = _escape_text(
                    '; '.join(result.get('incompatibilities', [])), 250, fallback='Nenhum'
                )
                tracking_status = _escape_text(
                    result.get('tracking_status', job.get('tracking_status', 'nova')), 30
                )
                score = int(result.get('score', 0))
                job_text += f"<b>[{radar_label}]</b> ⭐ {score}/100 — {priority}\n"
                if result.get('confirmation_status') == 'pending':
                    pending_reasons = _escape_text(
                        '; '.join(result.get('confirmation_reasons', [])),
                        220,
                        fallback='Verifique os dados da vaga',
                    )
                    job_text += f"⏳ Pendente de confirmação: {pending_reasons}\n"
                job_text += f"📌 Status neste radar: {tracking_status}\n"
                job_text += f"✅ Compatibilidade: {fit_reason}\n"
                job_text += f"⚠️ Atenção: {incompatibilities}\n"
            job_text += f"📄 Contrato: {contract} | Modalidade: {modality}\n"
            job_text += f"📍 Localidade: {location}\n"
            job_text += f"🕒 Horário/escala: {schedule}\n"
            job_text += f"💰 Salário: {salary}\n"
            job_text += f"🧰 Tecnologias: {technologies}\n"
            job_text += f"📋 Requisitos: {requirements}\n"
            job_text += f"📅 Publicada: {published_at}\n"
            if source:
                job_text += f"🌐 Fonte: {source}\n"
            if short_id:
                job_text += f"🆔 ID: <code>{short_id}</code>\n"
            if url:
                job_text += f"🔗 <a href=\"{url}\">Candidatura original</a>\n\n"
            else:
                job_text += "🔗 Link não informado\n\n"

            if len(current_message) + len(job_text) > 4000:
                messages.append((current_message, current_jobs))
                current_message = header + job_text
                current_jobs = [job]
            else:
                current_message += job_text
                current_jobs.append(job)

        if current_message:
            messages.append((current_message, current_jobs))

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

        for msg, message_jobs in messages:
            payload = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': msg,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }

            if not await _deliver_telegram_message(session, url, payload):
                return False
            if on_delivered:
                await on_delivered(message_jobs)

            await asyncio.sleep(0.5)

        logger.info(f"Successfully sent {len(messages)} Telegram message(s)")
        return True
    except Exception as e:
        logger.error(f"Error sending Telegram notification: {e}")
        return False


async def send_telegram_status(text: str) -> bool:
    """Send a standalone status/heartbeat/alert message (not a job listing)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not configured; skipping status message")
        return False
    try:
        session = await http_client.get_session()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        return await _deliver_telegram_message(session, url, payload)
    except Exception as e:
        logger.error(f"Error sending Telegram status: {e}")
        return False


def detect_run_issues(metrics: dict) -> list[str]:
    """Return human-readable problems for this run (empty list = healthy)."""
    issues: list[str] = []
    for failed in health_tracker.get_failed_sites():
        issues.append(f"{failed['site']} failing: {failed['error']}")
    if metrics.get('google_rate_limited', 0) > 0:
        issues.append(f"Search rate-limited ×{metrics['google_rate_limited']}")
    if metrics.get('google_queries_available', 0) > 0 and metrics.get('google_queries_executed', 0) == 0:
        issues.append("Search ran 0 queries (check API key / quota)")
    reason = metrics.get('google_stopped_early_reason')
    if reason:
        detail = metrics.get('google_last_error', '')
        issues.append(f"Search stopped early: {reason}" + (f" — {detail}" if detail else ""))
    elif metrics.get('google_last_error'):
        issues.append(f"Search error: {metrics['google_last_error']}")
    return issues


def build_status_message(metrics: dict, new_job_count: int, issues: list[str]) -> str:
    """Compose a heartbeat (healthy) or alert (issues present) status message."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    if issues:
        lines = ["⚠️ <b>Job Monitor — Issues Detected</b>", f"📅 {now}", ""]
        for issue in issues:
            lines.append(f"• {html.escape(issue, quote=False)}")
    else:
        lines = ["✅ <b>Job Monitor — Heartbeat</b>", f"📅 {now}"]

    lines.append("")
    lines.append(f"🆕 New jobs this run: {new_job_count}")
    lines.append(
        f"🔎 Search queries: {metrics.get('google_queries_executed', 0)}"
        f"/{metrics.get('google_queries_available', 0)}"
    )
    rejected = metrics.get('jobs_rejected_radar', 0)
    if rejected:
        lines.append(f"🚫 Rejected by radar rules: {rejected}")
        radar_labels = {
            'radar_a': 'RADAR A',
            'radar_b': 'RADAR B',
        }
        counts = metrics.get('jobs_rejected_by_radar', {})
        reasons = metrics.get('job_rejection_reasons', {})
        for radar_id, label in radar_labels.items():
            radar_count = counts.get(radar_id, 0)
            if not radar_count:
                continue
            reason_summary = ", ".join(
                f"{html.escape(reason, quote=False)}: {count}"
                for reason, count in sorted(reasons.get(radar_id, {}).items())
            )
            lines.append(
                f"• {label}: {radar_count}"
                + (f" ({reason_summary})" if reason_summary else "")
            )
    pending = metrics.get('jobs_pending_confirmation_by_radar', {})
    pending_summary = ", ".join(
        f"{label}: {pending.get(radar_id, 0)}"
        for radar_id, label in (('radar_a', 'RADAR A'), ('radar_b', 'RADAR B'))
        if pending.get(radar_id, 0)
    )
    if pending_summary:
        lines.append(f"⏳ Pending confirmation — {pending_summary}")
    working = health_tracker.get_working_sites()
    if working:
        summary = ", ".join(f"{w['site']}({w['jobs_found']})" for w in working)
        lines.append(f"🌐 Sources: {html.escape(summary, quote=False)}")
    return "\n".join(lines)


def is_daily_digest_due(
    now: datetime,
    last_digest_date: str,
    event_name: str,
    heartbeat_hour_utc: int = 23,
    heartbeat_minute_utc: int = 30,
) -> bool:
    return (
        event_name == 'schedule'
        and now.hour == heartbeat_hour_utc
        and now.minute >= heartbeat_minute_utc
        and now.date().isoformat() != last_digest_date
    )


# ============= CLI ARGUMENT PARSING =============
def parse_args():
    parser = argparse.ArgumentParser(
        description='Monitor de vagas com os radares de segunda renda e carreira em tecnologia'
    )
    commands = parser.add_mutually_exclusive_group()
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Test mode: search but skip Telegram notifications and seen_jobs.json updates. Prints a report of what was found.'
    )
    commands.add_argument(
        '--history',
        action='store_true',
        help='List saved opportunities and their radar/delivery/tracking statuses.'
    )
    commands.add_argument(
        '--set-status',
        nargs='+',
        metavar='VALUE',
        help='Update status: JOB_ID RADAR STATUS (RADAR is radar_a or radar_b).'
    )
    return parser.parse_args()


def print_history(scraper: JobSiteScraper):
    print(f"\nHISTÓRICO DE OPORTUNIDADES ({len(scraper.job_history)})")
    for job_id, record in sorted(
        scraper.job_history.items(),
        key=lambda item: float(item[1].get('updated_at', 0)),
        reverse=True,
    ):
        print(f"\n[{job_id[:12]}] {record.get('title', 'Cargo não informado')}")
        print(f"  URL: {record.get('url', '')}")
        for radar_id, radar in record.get('radars', {}).items():
            radar_label = radar.get('radar_label', radar_id)
            tracking_status = radar.get(
                'tracking_status', record.get('tracking_status', 'nova')
            )
            confirmation_status = radar.get('confirmation_status', 'Não registrado')
            if confirmation_status == 'pending':
                confirmation_status = 'Pendente'
            elif confirmation_status == 'confirmed':
                confirmation_status = 'Confirmada'
            print(f"  {radar_label}:")
            print(f"    Status de acompanhamento: {tracking_status}")
            print(f"    Status de confirmação: {confirmation_status}")
            if radar.get('confirmation_status') == 'pending':
                reasons = radar.get('confirmation_reasons') or [
                    'Motivo não registrado'
                ]
                print(f"    Motivo da pendência: {'; '.join(reasons)}")
            print(
                f"    Status de notificação: "
                f"{radar.get('delivery_status', 'pendente')}"
            )


def print_dry_run_report(jobs: list[dict]):
    """Print detailed report for dry-run mode."""
    print("\n" + "=" * 60)
    print("DRY RUN REPORT")
    print("=" * 60)
    
    # Working sites
    working = health_tracker.get_working_sites()
    print(f"\n✅ WORKING SITES ({len(working)}):")
    print("-" * 40)
    if working:
        for site in working:
            print(f"  ✓ {site['site']}: {site['jobs_found']} jobs found")
    else:
        print("  No working sites found")
    
    # Failed sites
    failed = health_tracker.get_failed_sites()
    print(f"\n❌ FAILED SITES ({len(failed)}):")
    print("-" * 40)
    if failed:
        for site in failed:
            print(f"  ✗ {site['site']}")
            print(f"    Reason: {site['error']}")
            print(f"    Failures: {site['failures']}")
            print()
    else:
        print("  All sites working!")
    
    # Jobs found
    print(f"\n📋 JOBS FOUND ({len(jobs)}):")
    print("-" * 40)
    if jobs:
        for i, job in enumerate(jobs[:20], 1):  # Show first 20
            title = job.get('title', 'Unknown')[:60]
            company = job.get('company', 'Unknown')[:30]
            source = job.get('source', 'Unknown')
            print(f"  {i}. [{source}] {title}")
            print(f"     Company: {company}")
            for result in job.get('radar_results', []):
                print(
                    f"     [{result.get('radar_label', '')}] "
                    f"{result.get('score', 0)}/100 — {result.get('priority', '')}"
                )
        if len(jobs) > 20:
            print(f"\n  ... and {len(jobs) - 20} more jobs")
    else:
        print("  No matching jobs found")
    
    print("\n" + "=" * 60)
    print("END OF DRY RUN REPORT")
    print("=" * 60 + "\n")

# ============= MAIN =============
async def main(
    dry_run: bool = False,
    show_history: bool = False,
    set_status: Optional[list[str]] = None,
):
    logger.info("=" * 50)
    logger.info("Job Monitor Bot Starting")
    if dry_run:
        logger.info("🧪 DRY RUN MODE - No notifications, no seen_jobs.json updates")
    logger.info(f"Concurrent limit: {CONCURRENT_LIMIT}")
    logger.info("=" * 50)

    scraper = JobSiteScraper()

    try:
        if show_history:
            print_history(scraper)
            return
        if set_status:
            if len(set_status) == 2:
                job_id, status = set_status
                radar_id = None
            elif len(set_status) == 3:
                job_id, radar_id, status = set_status
            else:
                raise ValueError(
                    "Use --set-status JOB_ID RADAR STATUS; RADAR deve ser radar_a ou radar_b"
                )
            valid_statuses = {
                'interessada', 'candidatura_enviada', 'entrevista', 'recusada', 'encerrada'
            }
            if radar_id is not None and radar_id not in {'radar_a', 'radar_b'}:
                raise ValueError("RADAR deve ser radar_a ou radar_b")
            if status not in valid_statuses:
                raise ValueError(
                    "Status inválido. Use: " + ", ".join(sorted(valid_statuses))
                )
            if not scraper.update_radar_tracking_status(job_id, radar_id, status):
                raise ValueError(
                    f"ID '{job_id}' ou radar não encontrado, ou ID ambíguo no histórico"
                )
            logger.info(
                f"Updated opportunity {job_id[:12]} on {radar_id or 'single radar'} to '{status}'"
            )
            return

        start_time = datetime.now()
        new_jobs = await scraper.scrape_serper_search()
        elapsed = (datetime.now() - start_time).total_seconds()
        
        logger.info(f"Scraping completed in {elapsed:.2f} seconds")
        scraper.log_operational_metrics()
        
        if dry_run:
            print_dry_run_report(new_jobs)
        else:
            async def persist_delivered(opportunities: list[dict]):
                scraper.mark_jobs_as_seen(opportunities)
                if not scraper.save_seen_jobs():
                    raise RuntimeError("Could not persist delivered opportunity status")

            high_priority_jobs = [
                job for job in new_jobs if job.get('is_high_priority')
            ]
            if high_priority_jobs:
                logger.info(
                    f"Found {len(high_priority_jobs)} high-priority opportunities"
                )
                notification_sent = await send_telegram_notification(
                    high_priority_jobs,
                    on_delivered=persist_delivered,
                )
                if not notification_sent:
                    raise RuntimeError(
                        "Notification failed. Delivered batches were saved; remaining jobs will retry."
                    )
            else:
                logger.info("No high-priority opportunities found")

            now_utc = datetime.now(timezone.utc)
            digest_due = is_daily_digest_due(
                now_utc,
                scraper.last_digest_date,
                os.getenv('GITHUB_EVENT_NAME', ''),
                int(NOTIFICATIONS_CONFIG.get('heartbeat_hour_utc', 23)),
                int(NOTIFICATIONS_CONFIG.get('heartbeat_minute_utc', 30)),
            )
            digest_jobs = scraper.get_pending_digest() if digest_due else []
            if digest_jobs:
                if not await send_telegram_notification(
                    digest_jobs,
                    daily_digest=True,
                    on_delivered=persist_delivered,
                ):
                    raise RuntimeError(
                        "Daily digest failed. Delivered batches were saved; remaining opportunities will retry."
                    )
            if digest_due:
                scraper.last_digest_date = now_utc.date().isoformat()

            # Heartbeat / failure alert: separate from job notifications so silence
            # means "no new jobs", not "the bot broke". Failures never block the run.
            issues = detect_run_issues(scraper.metrics)
            if not SERPER_API_KEY:
                issues.append("SERPER_API_KEY missing — no jobs can be found")
            alert_on_failures = bool(NOTIFICATIONS_CONFIG.get('alert_on_failures', True))
            heartbeat_enabled = bool(NOTIFICATIONS_CONFIG.get('heartbeat_enabled', True))
            should_alert = bool(issues) and alert_on_failures
            should_heartbeat = (
                heartbeat_enabled
                and not should_alert
                and not new_jobs
                and not digest_jobs
                and digest_due
            )
            if should_alert or should_heartbeat:
                status_text = build_status_message(scraper.metrics, len(new_jobs), issues)
                if not await send_telegram_status(status_text):
                    logger.warning("Failed to send status/heartbeat message")

            if not scraper.save_seen_jobs():
                raise RuntimeError("Could not persist opportunity history")
        
    except Exception as e:
        logger.error(f"Error in main: {e}")
        raise
    finally:
        await http_client.close()
    
    logger.info("Job Monitor Bot Finished")

if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        main(
            dry_run=args.dry_run,
            show_history=args.history,
            set_status=args.set_status,
        )
    )
