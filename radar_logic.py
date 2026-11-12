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
                        r"[\s,:-]*(?:(?:em|no|na|para|por|todo|toda|o|a|territorio|nacional)\s+)*",
                        connector,
                    ):
                        return True
    return False


def matching_terms(text: str, terms: list[str]) -> list[str]:
    return _positive_terms(text, terms)


def _matched_groups(text: str, groups: dict[str, list[str]]) -> list[str]:
    return [
        group
        for group, terms in groups.items()
        if matching_terms(text, terms)
    ]


def _priority(score: int, config: dict[str, Any]) -> str:
    thresholds = config.get("priority_thresholds", {})
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
    contract_conflict = any(
        contracts.get(kind) and _has_negated_term(text, terms)
        for kind, terms in contract_signals.items()
    )

    modality_signals = signals.get("modalities", {})
    modalities = {
        kind: matching_terms(text, terms)
        for kind, terms in modality_signals.items()
    }
    modalities = {kind: terms for kind, terms in modalities.items() if terms}
    modality_conflict = any(
        modalities.get(kind) and _has_negated_term(text, terms)
        for kind, terms in modality_signals.items()
    )

    levels = {
        level: matching_terms(title, terms)
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
    brazil_locations = matching_terms(text, location_terms.get("brazil", []))
    night_terms = matching_terms(text, signals.get("night_shifts", []))
    other_off_hours_terms = matching_terms(text, signals.get("other_off_hours", []))
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

    schedule = "Horário não informado"
    if night_terms:
        schedule = "Noturno / madrugada: " + ", ".join(night_terms[:3])
    elif other_off_hours_terms:
        if matching_terms(text, ["fora do horário comercial"]):
            schedule = "Fora do horário comercial indicado"
        elif matching_terms(text, ["12x36"]):
            schedule = "Horário não informado; escala 12x36 indicada"
        else:
            schedule = "Horário não informado; escala alternada indicada"

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
        "contract_label": " / ".join(
            contract_labels.get(kind, kind) for kind in contracts.keys()
        ) or "Não confirmado",
        "modalities": set(modalities),
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
        "brazil_locations": brazil_locations,
        "night_terms": night_terms,
        "other_off_hours_terms": other_off_hours_terms,
        "schedule": schedule,
        "incompatible_titles": incompatible_titles,
        "contract_ambiguous": len(contracts) > 1,
        "contract_conflict": contract_conflict,
        "modality_ambiguous": len(modalities) > 1,
        "modality_conflict": modality_conflict,
        "night_negated": _has_negated_term(
            text, signals.get("night_shifts", [])
        ),
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

    contracts = facts["contracts"]
    modalities = facts["modalities"]
    levels = facts["levels"]
    areas = facts["areas"]
    allowed_contracts = set(radar["allowed_contracts"])
    if facts["contract_conflict"]:
        return {"eligible": False, "reason": "Contrato contraditório ou ambíguo"}
    if not contracts:
        return {"eligible": False, "reason": "Tipo de contrato não confirmado"}
    if not contracts.issubset(allowed_contracts):
        return {"eligible": False, "reason": "Contrato não permitido para este radar"}
    if len(contracts) > 1:
        return {"eligible": False, "reason": "Contrato ambíguo"}

    if facts["modality_conflict"]:
        return {"eligible": False, "reason": "Modalidade contraditória ou ambígua"}
    if not modalities:
        return {"eligible": False, "reason": "Modalidade não confirmada"}
    if len(modalities) != 1:
        return {"eligible": False, "reason": "Modalidade ambígua"}
    modality = next(iter(modalities))
    if modality not in radar["allowed_modalities"]:
        return {"eligible": False, "reason": "Modalidade não permitida para este radar"}

    if radar_id == "radar_a":
        if facts["incompatible_titles"]:
            return {
                "eligible": False,
                "reason": "Cargo especializado acima do perfil informado",
            }
        if not areas:
            return {"eligible": False, "reason": "Área não compatível com o Radar A"}
        if modality == "remote":
            if facts["brazil_negated"] or not facts["remote_brazil_confirmed"]:
                return {
                    "eligible": False,
                    "reason": "Abrangência remota no Brasil não confirmada",
                }
        elif (
            facts["unconfirmed_work_location"]
            or not facts["brasilia_locations"]
        ):
            return {
                "eligible": False,
                "reason": "Local presencial/híbrido fora da região de Brasília",
            }
        location_label = (
            "Remoto no Brasil"
            if modality == "remote"
            else ", ".join(facts["brasilia_locations"])
        )
    else:
        if modality != "remote":
            return {"eligible": False, "reason": "O Radar B aceita apenas vagas remotas"}
        if not levels:
            return {"eligible": False, "reason": "Senioridade não confirmada"}
        if len(levels) != 1:
            return {"eligible": False, "reason": "Senioridade ambígua"}
        level = next(iter(levels))
        if level not in radar["allowed_seniority"]:
            return {"eligible": False, "reason": "Senioridade não permitida para o Radar B"}
        is_frontend = bool(facts["senior_frontend_titles"]) and not any(
            area in areas for area in ("backend", "fullstack")
        )
        if level == "senior" and not is_frontend:
            return {
                "eligible": False,
                "reason": "Sênior permitido apenas para Front-end no Radar B",
            }
        if not areas:
            return {"eligible": False, "reason": "Área de TI não confirmada"}
        if level == "internship" and not any(
            area in radar["internship_areas"] for area in areas
        ):
            return {
                "eligible": False,
                "reason": "Estágio sem relação com TI ou desenvolvimento",
            }
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

    if radar_id == "radar_a":
        if facts["night_terms"] and not facts["night_negated"]:
            night_score = 100
            reasons.append("Turno noturno/madrugada indicado na descrição")
        elif facts["other_off_hours_terms"] and not facts["night_negated"]:
            if matching_terms(text, ["fora do horário comercial"]):
                night_score = 90
                reasons.append("Disponibilidade fora do horário comercial indicada")
            else:
                night_score = 70
                reasons.append("Escala flexível indicada; turno não confirmado")
                incompatibilities.append("Turno não confirmado; escala indicada")
        else:
            night_score = 0
            incompatibilities.append(
                "Horário não informado; não foi presumido turno noturno"
            )
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
        experience_scores = {
            "internship": 75,
            "junior": 95,
            "mid": 100,
            "senior": 45,
        }
        experience_score = experience_scores.get(seniority, 60)
        if seniority == "senior":
            incompatibilities.append(
                "Requisitos Sênior podem exceder a experiência Pleno informada"
            )
        elif not seniority:
            incompatibilities.append("Senioridade e experiência exigida não informadas")
        if len(levels) > 1:
            incompatibilities.append("Senioridade indicada de forma ambígua")
        contract_mode_score = 100
        salary = _salary_label(salary_text)
        compensation_score = 100 if salary != "Não divulgado" else 60
        weights = radar["weights"]
        score = round(
            night_score * weights["night_shift"]
            + technical_score * weights["activities"]
            + experience_score * weights["experience"]
            + contract_mode_score * weights["contract_and_modality"]
            + compensation_score * weights["compensation"]
        )
        if modality == "remote":
            reasons.append("Vaga remota com abrangência no Brasil indicada")
        else:
            reasons.append("Localidade na região de Brasília indicada")
    else:
        level = next(iter(levels))
        seniority_score = {
            "internship": 100,
            "junior": 100,
            "mid": 100,
            "senior": 80,
        }[level]
        contract_score = 100
        remote_score = 100
        career_score = 100 if facts["career_growth_evidence"] else 0
        weights = radar["weights"]
        score = round(
            technical_score * weights["technical"]
            + seniority_score * weights["seniority"]
            + contract_score * weights["contract"]
            + remote_score * weights["remote"]
            + career_score * weights["career_growth"]
        )
        salary = _salary_label(salary_text)
        reasons.append("Remoto confirmado")

    raw_requirements = str(job.get("description", "")).strip()
    requirements = raw_requirements or "Não informado no resultado público"
    technologies = facts["technologies"] or ["Não identificadas no resultado público"]
    if salary == "Não divulgado":
        incompatibilities.append("Salário não divulgado")

    return {
        "eligible": True,
        "radar_id": radar_id,
        "radar_label": radar["label"],
        "score": max(0, min(100, score)),
        "priority": _priority(score, config),
        "contract": facts["contract_label"],
        "modality": facts["modality_label"],
        "location": location_label,
        "schedule": facts["schedule"],
        "salary": salary,
        "technologies": technologies,
        "requirements": requirements,
        "fit_reason": "; ".join(reasons) or "Compatibilidade identificada nos dados públicos",
        "incompatibilities": incompatibilities or ["Nenhum ponto de atenção identificado"],
        "published_at": str(job.get("published_at") or "Não informada"),
        "tracking_status": "nova",
    }
