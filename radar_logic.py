"""Eligibility and ranking rules for the independent job-search radars."""

import re
import unicodedata
from typing import Any


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(char for char in text if not unicodedata.combining(char))


def contains_term(text: str, term: str) -> bool:
    normalized_text = normalize_text(text)
    normalized_term = normalize_text(term).strip()
    if not normalized_term:
        return False
    pattern = re.escape(normalized_term).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<!\w){pattern}(?!\w)", normalized_text))


def _term_matches(text: str, term: str) -> list[re.Match[str]]:
    normalized_term = normalize_text(term).strip()
    if not normalized_term:
        return []
    pattern = re.escape(normalized_term).replace(r"\ ", r"\s+")
    return list(
        re.finditer(rf"(?<!\w){pattern}(?!\w)", normalize_text(text))
    )


def _is_negated(text: str, match: re.Match[str]) -> bool:
    return _is_negated_span(text, match.start(), match.end())


def _is_negated_span(text: str, start: int, end: int) -> bool:
    normalized = normalize_text(text)
    prefix = normalized[:start]
    suffix = normalized[end:]
    return bool(
        re.search(r"\b(?:nao|nunca|jamais|sem)\b(?:\s+\w+){0,3}\s+$", prefix)
        or re.match(
            r"\s*[,;:-]?\s*(?:nao\b(?:\s+\w+){0,3}|"
            r"indisponivel\b|negado\b|proibido\b|inexistente\b)",
            suffix,
        )
    )


def _positive_terms(text: str, terms: list[str]) -> list[str]:
    positive = []
    for term in terms:
        matches = _term_matches(text, term)
        if any(not _is_negated(text, match) for match in matches):
            positive.append(term)
    return positive


def _has_negated_term(text: str, terms: list[str]) -> bool:
    return any(
        _is_negated(text, match)
        for term in terms
        for match in _term_matches(text, term)
    )


_WORK_LOCATION_CONTEXT = re.compile(
    r"\b(?:presencial|hibrid[oa]|local(?:idade|izacao)?"
    r"(?:\s+de\s+trabalho)?)"
    r"(?P<connector>\s+(?:em|no|na)\s+|\s*[:,-]\s*)?"
)


def _location_pattern(location: str) -> str:
    if location == "planaltina-df":
        return r"\s*planaltina\s*[-,]\s*(?:df|distrito federal)"
    pattern = r"\s*" + re.escape(location).replace(r"\ ", r"\s+")
    if location not in {"df", "distrito federal"}:
        pattern += r"(?:\s*[,/-]?\s*(?:df|distrito federal))?"
    return pattern


def _confirmed_brasilia_locations(text: str, allowed_locations: list[str]) -> list[str]:
    normalized = normalize_text(text)
    location_labels = {
        normalize_text(location): location for location in allowed_locations
    }
    confirmed: list[str] = []
    for context_match in _WORK_LOCATION_CONTEXT.finditer(normalized):
        location_text = normalized[context_match.end():]
        for location, label in location_labels.items():
            location_match = re.match(
                _location_pattern(location) + r"(?=\b|[,.;)]|$)", location_text
            )
            if location_match and not _is_negated_span(
                normalized,
                context_match.start(),
                context_match.end() + location_match.end(),
            ):
                confirmed.append(label)
                break
    return list(dict.fromkeys(confirmed))


def _has_unconfirmed_work_location(text: str, allowed_locations: list[str]) -> bool:
    normalized = normalize_text(text)
    allowed = [normalize_text(location) for location in allowed_locations]
    for context_match in _WORK_LOCATION_CONTEXT.finditer(normalized):
        tail = normalized[context_match.end():]
        recognized = False
        for location in allowed:
            match = re.match(
                _location_pattern(location) + r"(?=\b|[,.;)]|$)", tail
            )
            alternative_location = bool(
                match
                and re.match(
                    r"\s+(?:ou|e)\s+[a-z][a-z-]+|"
                    r"\s*/\s*[a-z]{2}\b",
                    tail[match.end():],
                )
            )
            if (
                match
                and not alternative_location
                and not _is_negated_span(
                    normalized,
                    context_match.start(),
                    context_match.end() + match.end(),
                )
            ):
                recognized = True
                break
        if not recognized:
            if context_match.group("connector"):
                return True
    return False


def _unknown_work_location(text: str) -> bool:
    unknown_terms = [
        "a definir",
        "a combinar",
        "a confirmar",
        "localidade não informada",
        "local não informado",
        "localidade não divulgada",
        "local não divulgado",
        "to be determined",
        "tbd",
        "unidade a definir",
    ]
    normalized = normalize_text(text)
    return any(
        matching_terms(normalized[match.end():match.end() + 120], unknown_terms)
        for match in _WORK_LOCATION_CONTEXT.finditer(normalized)
    )


def _has_explicit_outside_work_location(
    text: str, allowed_locations: list[str]
) -> bool:
    normalized = normalize_text(text)
    unknown_terms = [
        "a definir",
        "a combinar",
        "a confirmar",
        "localidade não informada",
        "local não informado",
        "localidade não divulgada",
        "local não divulgado",
        "to be determined",
        "tbd",
        "unidade a definir",
        "uma das unidades",
    ]
    generic_location_prefixes = {
        "",
        "local",
        "localidade",
        "local de trabalho",
        "localidade de trabalho",
        "unidade",
        "uma das unidades",
        "em local",
        "no local",
    }
    for context_match in _WORK_LOCATION_CONTEXT.finditer(normalized):
        tail = normalized[context_match.end():]
        for location in allowed_locations:
            location_match = re.match(
                _location_pattern(normalize_text(location))
                + r"(?=\b|[,.;)]|$)",
                tail,
            )
            if not location_match:
                continue
            alternative_location = bool(
                re.match(
                    r"\s+(?:ou|e)\s+[a-z][a-z-]+|\s*/\s*[a-z]{2}\b",
                    tail[location_match.end():],
                )
            )
            if alternative_location or _is_negated_span(
                normalized,
                context_match.start(),
                context_match.end() + location_match.end(),
            ):
                return True
            break
        else:
            if not context_match.group("connector"):
                continue
            segment = re.split(r"[.;\n]", tail, maxsplit=1)[0]
            unresolved = next(
                (
                    match
                    for term in unknown_terms
                    for match in _term_matches(segment, term)
                ),
                None,
            )
            if unresolved:
                prefix = segment[:unresolved.start()].strip(" ,:-")
                if prefix in generic_location_prefixes:
                    continue
            return True
    return False


def _remote_brazil_confirmed(
    text: str, remote_terms: list[str], brazil_terms: list[str]
) -> bool:
    normalized = normalize_text(text)
    for remote_term in remote_terms:
        for remote_match in _term_matches(normalized, remote_term):
            if _is_negated(normalized, remote_match):
                continue
            for brazil_term in brazil_terms:
                for brazil_match in _term_matches(normalized, brazil_term):
                    if _is_negated(normalized, brazil_match):
                        continue
                    if brazil_match.start() < remote_match.end():
                        continue
                    connector = normalized[remote_match.end():brazil_match.start()]
                    if len(connector) <= 35 and re.fullmatch(
                        r"[\s,:-]*(?:(?:em|no|na|para|por|todo|toda|o|a|do|da|dos|"
                        r"das|territorio|nacional|candidatos?|candidatas?|qualquer|"
                        r"regiao|estado|lugar|brasileiro|brasileira|anywhere|in)\s+)*",
                        connector,
                    ):
                        return True
    brazil_wide_scope = [
        "qualquer região do Brasil",
        "qualquer estado do Brasil",
        "qualquer lugar do Brasil",
        "candidatos de qualquer região do Brasil",
        "candidatos de qualquer estado brasileiro",
        "qualquer região brasileira",
        "Brasil inteiro",
        "abrangência nacional",
        "todo o território nacional",
        "todo território brasileiro",
        "anywhere in Brazil",
    ]
    return bool(
        matching_terms(normalized, remote_terms)
        and matching_terms(normalized, brazil_wide_scope)
    )


def matching_terms(text: str, terms: list[str]) -> list[str]:
    return _positive_terms(text, terms)


def _matched_groups(text: str, groups: dict[str, list[str]]) -> list[str]:
    return [
        group
        for group, terms in groups.items()
        if matching_terms(text, terms)
    ]


def _schedule_classification(
    text: str, signals: dict[str, Any]
) -> tuple[str, bool]:
    normalized = normalize_text(text)
    night_terms = signals.get("night_shifts", [])
    business_terms = signals.get("business_hours", [])
    mixed_terms = signals.get("mixed_shifts", [])
    positive_night = matching_terms(normalized, night_terms)
    positive_business = matching_terms(normalized, business_terms)
    mixed_explicit = matching_terms(normalized, mixed_terms)
    night_negated = _has_negated_term(normalized, night_terms)
    time_ranges = re.compile(
        r"(?<!\d)(?P<start>2[0-3]|[01]?\d)\s*"
        r"(?:(?::|h)\s*(?P<start_min>[0-5]\d))?\s*h?\s*"
        r"(?:às|as|ate|a|[-–])\s*"
        r"(?P<end>2[0-3]|[01]?\d)\s*"
        r"(?:(?::|h)\s*(?P<end_min>[0-5]\d))?\s*h?(?!\d)"
    )
    has_night_range = False
    has_business_range = False
    for match in time_ranges.finditer(normalized):
        if _is_negated(normalized, match):
            continue
        start = int(match.group("start")) * 60 + int(match.group("start_min") or 0)
        end = int(match.group("end")) * 60 + int(match.group("end_min") or 0)
        if end <= start:
            end += 24 * 60
        range_has_night = False
        range_has_business = False
        for day in range(-1, 2):
            offset = day * 24 * 60
            if start < offset + 5 * 60 and end > offset:
                range_has_night = True
            if start < offset + 24 * 60 and end > offset + 22 * 60:
                range_has_night = True
            if start < offset + 18 * 60 and end > offset + 8 * 60:
                range_has_business = True
        if range_has_night:
            has_night_range = True
        elif range_has_business:
            has_business_range = True

    has_night = bool(positive_night) or has_night_range
    has_business = bool(positive_business) or has_business_range
    schedule_conflict = night_negated and has_night
    if schedule_conflict:
        return "Horário não informado", True
    if mixed_explicit or (has_night and has_business):
        return "Misto", False
    if has_night:
        return "Noturno confirmado", False
    if has_business:
        return "Horário comercial", False
    return "Horário não informado", False


def _priority(score: int, config: dict[str, Any], radar_id: str) -> str:
    thresholds = config["radars"][radar_id].get(
        "priority_thresholds", config.get("priority_thresholds", {})
    )
    if score >= int(thresholds.get("very_high", 101)):
        return "Muito alta prioridade"
    if score >= int(thresholds.get("high", 80)):
        return "Alta prioridade"
    if score >= int(thresholds.get("medium", 60)):
        return "Média prioridade"
    return "Baixa prioridade"


def _job_facts(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    signals = config.get("signals", {})
    title = normalize_text(job.get("title", ""))
    description = normalize_text(job.get("description", ""))
    text = f"{title} {description}"

    contract_signals = signals.get("contracts", {})
    contracts = {
        kind: matching_terms(text, terms)
        for kind, terms in contract_signals.items()
    }
    contracts = {kind: terms for kind, terms in contracts.items() if terms}
    negated_contracts = {
        kind
        for kind, terms in contract_signals.items()
        if _has_negated_term(text, terms)
    }
    contract_conflict = any(
        kind in contracts and kind in negated_contracts
        for kind in contract_signals
    )

    modality_signals = signals.get("modalities", {})
    modalities = {
        kind: matching_terms(text, terms)
        for kind, terms in modality_signals.items()
    }
    modalities = {kind: terms for kind, terms in modalities.items() if terms}
    negated_modalities = {
        kind
        for kind, terms in modality_signals.items()
        if _has_negated_term(text, terms)
    }
    modality_conflict = any(
        kind in modalities and kind in negated_modalities
        for kind in modality_signals
    )

    levels = {
        level: matching_terms(
            text if level == "internship" else title, terms
        )
        for level, terms in signals.get("seniority", {}).items()
    }
    levels = {level: terms for level, terms in levels.items() if terms}
    areas = list(dict.fromkeys(
        _matched_groups(title, signals.get("role_areas", {}))
        + _matched_groups(description, signals.get("role_areas", {}))
    ))
    description_areas = _matched_groups(description, signals.get("role_areas", {}))
    senior_frontend_titles = matching_terms(
        title, signals.get("senior_frontend_titles", [])
    )
    technology_matches = matching_terms(description, signals.get("technologies", []))
    technologies: list[str] = []
    for term in sorted(technology_matches, key=len, reverse=True):
        if not any(
            normalize_text(term) in normalize_text(existing)
            for existing in technologies
        ):
            technologies.append(term)
    location_terms = signals.get("locations", {})
    brasilia_locations = _confirmed_brasilia_locations(
        text, location_terms.get("brasilia_region", [])
    )
    unconfirmed_work_location = _has_unconfirmed_work_location(
        text, location_terms.get("brasilia_region", [])
    )
    unknown_work_location = _unknown_work_location(text)
    outside_work_location = _has_explicit_outside_work_location(
        text, location_terms.get("brasilia_region", [])
    )
    brazil_locations = matching_terms(text, location_terms.get("brazil", []))
    schedule, schedule_conflict = _schedule_classification(text, signals)
    incompatible_titles = matching_terms(
        title, signals.get("radar_a_incompatible_titles", [])
    )
    profile = config.get("profile", {})
    profile_experience_matches = matching_terms(
        description, profile.get("experience_terms", [])
    )
    education_matches = matching_terms(description, profile.get("education_terms", []))
    research_matches = matching_terms(description, profile.get("research_terms", []))
    activities = _matched_groups(description, signals.get("activity_evidence", {}))
    career_growth_evidence = matching_terms(
        description, signals.get("career_growth_evidence", [])
    )

    contract_labels = {
        "clt": "CLT",
        "pj": "PJ",
        "internship": "Estágio",
    }
    modality_labels = {
        "remote": "Remoto",
        "hybrid": "Híbrido",
        "onsite": "Presencial",
    }
    level_labels = {
        "internship": "Estágio",
        "junior": "Júnior",
        "mid": "Pleno",
        "senior": "Sênior",
    }

    return {
        "text": text,
        "contracts": set(contracts),
        "negated_contracts": negated_contracts,
        "contract_label": " / ".join(
            contract_labels.get(kind, kind) for kind in contracts.keys()
        ) or "Não confirmado",
        "modalities": set(modalities),
        "negated_modalities": negated_modalities,
        "modality_label": " / ".join(
            modality_labels.get(kind, kind) for kind in modalities.keys()
        ) or "Não confirmado",
        "levels": set(levels),
        "level_label": " / ".join(
            level_labels.get(level, level) for level in levels.keys()
        ) or "Não confirmado",
        "areas": areas,
        "description_areas": description_areas,
        "activities": activities,
        "senior_frontend_titles": senior_frontend_titles,
        "technologies": technologies,
        "profile_experience_matches": profile_experience_matches,
        "education_matches": education_matches,
        "research_matches": research_matches,
        "career_growth_evidence": career_growth_evidence,
        "brasilia_locations": brasilia_locations,
        "unconfirmed_work_location": unconfirmed_work_location,
        "unknown_work_location": unknown_work_location,
        "outside_work_location": outside_work_location,
        "brazil_locations": brazil_locations,
        "schedule": schedule,
        "schedule_conflict": schedule_conflict,
        "incompatible_titles": incompatible_titles,
        "contract_ambiguous": len(contracts) > 1,
        "contract_conflict": contract_conflict,
        "modality_ambiguous": len(modalities) > 1,
        "modality_conflict": modality_conflict,
        "night_negated": _has_negated_term(text, signals.get("night_shifts", [])),
        "remote_negated": _has_negated_term(
            text, signals.get("modalities", {}).get("remote", [])
        ),
        "brazil_negated": _has_negated_term(
            text, location_terms.get("brazil", [])
        ),
        "remote_brazil_confirmed": _remote_brazil_confirmed(
            text,
            signals.get("modalities", {}).get("remote", []),
            location_terms.get("brazil", []),
        ),
    }


def _salary_label(text: str) -> str:
    amount = r"\d+(?:\.\d{3})*(?:,\d{2})?"
    salary_range = (
        rf"R\$\s*{amount}(?:\s*(?:-|a)\s*(?:R\$\s*)?{amount})?"
        rf"(?:\s*(?:por|a)\s*m[eê]s)?"
    )
    match = re.search(salary_range, text, re.IGNORECASE) or re.search(
        rf"\b(?:salário|salario|remuneração|remuneracao)\s+(?:de\s+)?{amount}",
        text,
        re.IGNORECASE,
    )
    return match.group(0).strip() if match else "Não divulgado"


def evaluate_job(
    job: dict[str, Any], radar_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Apply mandatory rules first, then return a radar-specific score and explanation."""
    radar = config["radars"][radar_id]
    facts = _job_facts(job, config)
    title = normalize_text(job.get("title", ""))
    description = normalize_text(job.get("description", ""))
    text = f"{title} {description}"
    salary_text = f"{job.get('title', '')} {job.get('description', '')}"
    reasons: list[str] = []
    incompatibilities: list[str] = []
    confirmation_reasons: list[str] = []

    def rejected(reason: str) -> dict[str, Any]:
        return {"eligible": False, "reason": reason}

    def needs_confirmation(reason: str):
        if reason not in confirmation_reasons:
            confirmation_reasons.append(reason)

    contracts = facts["contracts"]
    modalities = facts["modalities"]
    levels = facts["levels"]
    allowed_areas = set(radar.get("allowed_areas", []))
    areas = [area for area in facts["areas"] if area in allowed_areas]
    allowed_contracts = set(radar["allowed_contracts"])
    if facts["contract_conflict"]:
        return rejected("Contrato contraditório ou ambíguo")
    if not contracts:
        needs_confirmation(
            "Contrato permitido não confirmado; alternativa explicitamente negada"
            if facts["negated_contracts"]
            else "Tipo de contrato não confirmado"
        )
    if not contracts.issubset(allowed_contracts):
        return rejected("Contrato não permitido para este radar")

    if facts["modality_conflict"]:
        return rejected("Modalidade contraditória ou ambígua")
    if not modalities:
        modality = ""
        needs_confirmation(
            "Modalidade alternativa não confirmada após negação explícita"
            if facts["negated_modalities"]
            else "Modalidade não confirmada"
        )
        if radar_id == "radar_b" and facts["remote_negated"]:
            return rejected("Trabalho remoto explicitamente negado")
    elif len(modalities) != 1:
        return rejected("Modalidade ambígua")
    else:
        modality = next(iter(modalities))
        if modality not in radar["allowed_modalities"]:
            return rejected("Modalidade não permitida para este radar")

    if radar_id == "radar_a":
        if not areas:
            if facts["areas"]:
                return rejected("Área não compatível com o Radar A")
            needs_confirmation("Área de TI não confirmada")
        if modality == "remote":
            if facts["brazil_negated"]:
                return rejected("Vaga remota explicitamente fora do Brasil")
            if not facts["remote_brazil_confirmed"]:
                needs_confirmation("Abrangência remota no Brasil não confirmada")
            location_label = (
                "Remoto no Brasil"
                if facts["remote_brazil_confirmed"]
                else "Remoto — abrangência Brasil pendente"
            )
        elif modality in {"hybrid", "onsite"}:
            if facts["outside_work_location"]:
                return rejected("Local presencial/híbrido explicitamente fora do DF")
            elif facts["brasilia_locations"]:
                location_label = ", ".join(facts["brasilia_locations"])
            else:
                needs_confirmation("Local de trabalho no Distrito Federal não confirmado")
                location_label = "Localidade no DF pendente"
        else:
            location_label = "Modalidade e localidade pendentes"
    else:
        if modality and modality != "remote":
            return rejected("O Radar B aceita apenas vagas remotas")
        if not levels:
            needs_confirmation("Senioridade não confirmada")
        if len(levels) > 1:
            return rejected("Senioridade ambígua")
        level = next(iter(levels), "")
        if level and level not in radar["allowed_seniority"]:
            return rejected("Senioridade não permitida para o Radar B")
        is_frontend = bool(facts["senior_frontend_titles"]) and not any(
            area in facts["areas"] for area in ("backend", "fullstack")
        )
        if level == "senior" and not is_frontend:
            return rejected("Sênior permitido apenas para Front-end no Radar B")
        if not areas:
            if facts["areas"]:
                return rejected("Área sem relação com TI para o Radar B")
            needs_confirmation("Área de TI não confirmada")
        location_label = "Remoto"

    technology_count = len(facts["technologies"])
    activity_score = 100 if facts["activities"] else 0
    technical_score = min(
        100,
        activity_score * 0.4 + min(technology_count, 3) * 20,
    )
    if technology_count:
        reasons.append(
            "Tecnologias compatíveis: " + ", ".join(facts["technologies"][:6])
        )
    if areas:
        reasons.append("Área compatível: " + ", ".join(areas))
    if facts["profile_experience_matches"]:
        reasons.append(
            "Contexto corporativo compatível: "
            + ", ".join(facts["profile_experience_matches"][:3])
        )
    if facts["education_matches"]:
        reasons.append("Formação relacionada indicada")
    if facts["research_matches"]:
        reasons.append(
            "Pesquisa/IA compatível: " + ", ".join(facts["research_matches"][:3])
        )

    if facts["schedule_conflict"]:
        needs_confirmation("Horário informado de forma contraditória")
    elif facts["night_negated"]:
        incompatibilities.append(
            "Anúncio nega jornada noturna; horário comercial não foi confirmado"
        )
    if radar_id == "radar_a":
        schedule_scores = radar.get("schedule_scores", {})
        schedule_key = {
            "Noturno confirmado": "night_confirmed",
            "Misto": "mixed",
            "Horário comercial": "business_hours",
            "Horário não informado": "unknown",
        }[facts["schedule"]]
        score = int(schedule_scores.get(schedule_key, 50))
        if facts["schedule"] == "Noturno confirmado":
            reasons.append("Horário noturno confirmado")
        elif facts["schedule"] == "Misto":
            reasons.append("Jornada inclui períodos diurnos e noturnos")
        elif facts["schedule"] == "Horário comercial":
            reasons.append("Horário comercial identificado")
        else:
            reasons.append("Horário não informado; não foi presumido")
        score += min(4, technical_score // 25)
        seniority = min(
            levels,
            key=lambda level: {
                "internship": 75,
                "junior": 95,
                "mid": 100,
                "senior": 45,
            }.get(level, 60),
            default="",
        )
        score += 2 if seniority in {"junior", "mid"} else 0
        salary = _salary_label(salary_text)
        score += 2 if salary != "Não divulgado" else 0
        if modality == "remote":
            if facts["remote_brazil_confirmed"]:
                reasons.append("Vaga remota com abrangência no Brasil indicada")
        else:
            if facts["brasilia_locations"]:
                reasons.append("Localidade no Distrito Federal indicada")
    else:
        score = int(radar.get("base_score", 80))
        if confirmation_reasons:
            score = min(score, 70)
        score += min(10, round(technical_score * 0.1))
        if facts["schedule"] == "Horário comercial":
            score += int(radar.get("business_hours_bonus", 5))
            reasons.append("Horário comercial priorizado")
        if facts["career_growth_evidence"]:
            score += int(radar.get("career_growth_bonus", 5))
        if level == "internship":
            reasons.append("Estágio remoto em TI")
        elif contracts:
            reasons.append("Vaga PJ remota em tecnologia")
        if facts["schedule"] in {"Noturno confirmado", "Misto"}:
            incompatibilities.append("Jornada fora do horário comercial")
        if confirmation_reasons:
            score = min(score, 79)
        salary = _salary_label(salary_text)
        reasons.append("Remoto confirmado")

    raw_requirements = str(job.get("description", "")).strip()
    requirements = raw_requirements or "Não informado no resultado público"
    technologies = facts["technologies"] or ["Não identificadas no resultado público"]
    if salary == "Não divulgado":
        incompatibilities.append("Salário não divulgado")
    if confirmation_reasons:
        incompatibilities.extend(confirmation_reasons)

    return {
        "eligible": not confirmation_reasons,
        "pending_confirmation": bool(confirmation_reasons),
        "confirmation_status": "pending" if confirmation_reasons else "confirmed",
        "confirmation_reasons": confirmation_reasons,
        "radar_id": radar_id,
        "radar_label": radar["label"],
        "score": max(0, min(100, score)),
        "priority": _priority(score, config, radar_id),
        "contract": facts["contract_label"],
        "modality": facts["modality_label"],
        "location": location_label,
        "schedule": facts["schedule"],
        "schedule_status": (
            "contradictory"
            if facts["schedule_conflict"]
            else "explicit_negative"
            if facts["night_negated"]
            else "missing"
            if facts["schedule"] == "Horário não informado"
            else "confirmed"
        ),
        "salary": salary,
        "technologies": technologies,
        "requirements": requirements,
        "fit_reason": "; ".join(reasons) or "Compatibilidade identificada nos dados públicos",
        "incompatibilities": incompatibilities or ["Nenhum ponto de atenção identificado"],
        "published_at": str(job.get("published_at") or "Não informada"),
        "tracking_status": "nova",
    }
