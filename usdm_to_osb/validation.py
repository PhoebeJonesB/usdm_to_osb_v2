"""
USDM 4.0 JSON validation.

Checks that the file has every section the upload pipeline needs.
Each rule is marked critical (upload cannot proceed) or optional (section skipped).
"""

import json
import logging
import sys

logger = logging.getLogger(__name__)

# ── validation rules ─────────────────────────────────────────────────────────
# (human_label, accessor(usdm, version, design), critical?)

RULES = [
    # Top-level / version-level — critical
    ("study.name",                 lambda u, v, d: u.get("study", {}).get("name"),          True),
    ("versions[0]",                lambda u, v, d: v,                                        True),
    ("titles",                     lambda u, v, d: v.get("titles"),                          True),
    ("studyIdentifiers",           lambda u, v, d: v.get("studyIdentifiers"),                True),
    ("studyDesigns[0]",            lambda u, v, d: d,                                        True),
    # Design-level — optional (section skipped when missing)
    ("studyType",                  lambda u, v, d: d.get("studyType"),                       False),
    ("studyPhase",                 lambda u, v, d: d.get("studyPhase"),                      False),
    ("subTypes (trial types)",     lambda u, v, d: d.get("subTypes"),                        False),
    ("population",                 lambda u, v, d: d.get("population"),                      False),
    ("model (intervention model)", lambda u, v, d: d.get("model"),                           False),
    ("blindingSchema",             lambda u, v, d: d.get("blindingSchema"),                  False),
    ("intentTypes",                lambda u, v, d: d.get("intentTypes"),                     False),
    ("therapeuticAreas",           lambda u, v, d: d.get("therapeuticAreas"),                False),
    ("arms",                       lambda u, v, d: d.get("arms"),                            False),
    ("epochs",                     lambda u, v, d: d.get("epochs"),                          False),
    ("elements",                   lambda u, v, d: d.get("elements"),                        False),
    ("studyCells",                 lambda u, v, d: d.get("studyCells"),                      False),
    ("encounters (visits)",        lambda u, v, d: d.get("encounters"),                      False),
    ("scheduleTimelines",          lambda u, v, d: d.get("scheduleTimelines"),              False),
    ("objectives",                 lambda u, v, d: d.get("objectives"),                      False),
    ("eligibilityCriteria",        lambda u, v, d: d.get("eligibilityCriteria"),             False),
    ("eligibilityCriterionItems",  lambda u, v, d: v.get("eligibilityCriterionItems"),       False),
    ("activities",                 lambda u, v, d: d.get("activities"),                      False),
    ("indications",                lambda u, v, d: d.get("indications"),                    False),
    ("biomedicalConcepts",         lambda u, v, d: v.get("biomedicalConcepts"),              False),
]


def _has_data(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict)) and len(value) == 0:
        return False
    return bool(value)


def validate_usdm(usdm_path: str) -> dict:
    """
    Validate a USDM JSON file against all sections the upload pipeline requires.

    Returns a result dict::

        {
            "can_proceed": bool,
            "present": ["section", ...],
            "missing_critical": ["section", ...],
            "missing_optional": ["section", ...],
            "study_name": str,
            "identifiers_preview": [str, ...],
        }
    """
    logger.info("=" * 70)
    logger.info("VALIDATION: %s", usdm_path)
    logger.info("=" * 70)

    with open(usdm_path, encoding="utf-8") as f:
        usdm = json.load(f)

    study = usdm.get("study", {})
    versions = study.get("versions", [])
    version = versions[0] if versions else {}
    designs = version.get("studyDesigns", [])
    design = designs[0] if designs else {}

    present = []
    missing_critical = []
    missing_optional = []

    for label, accessor, critical in RULES:
        try:
            value = accessor(usdm, version, design)
        except Exception:
            value = None

        has = _has_data(value)
        count_str = f" ({len(value)} items)" if isinstance(value, list) and has else ""

        if has:
            logger.info("  [OK]       %-35s%s", label, count_str)
            present.append(label)
        elif critical:
            logger.error("  [CRITICAL] %-35s MISSING — upload cannot proceed", label)
            missing_critical.append(label)
        else:
            logger.warning("  [SKIP]     %-35s MISSING — section will be skipped", label)
            missing_optional.append(label)

    # Summary
    logger.info("-" * 70)
    logger.info("Present:          %d sections", len(present))
    if missing_optional:
        logger.info("Skippable:        %d — %s", len(missing_optional), ", ".join(missing_optional))
    if missing_critical:
        logger.error("CRITICAL MISSING: %d — %s", len(missing_critical), ", ".join(missing_critical))

    can_proceed = len(missing_critical) == 0

    if can_proceed:
        logger.info("")
        logger.info("RESULT: PASSED — all critical sections present. Ready to upload.")
    else:
        logger.error("")
        logger.error("RESULT: FAILED — fix the critical sections above, then re-run validation.")

    # Preview identifiers
    identifiers = version.get("studyIdentifiers", [])
    id_preview = [i.get("text", "") for i in identifiers]

    logger.info("")
    logger.info("Study name:     %s", study.get("name", "(none)"))
    logger.info("Identifiers:    %s", id_preview)
    logger.info("project_number will be: %s (from studyIdentifiers[0])", id_preview[0] if id_preview else "(none)")
    logger.info("=" * 70)

    return {
        "can_proceed": can_proceed,
        "present": present,
        "missing_critical": missing_critical,
        "missing_optional": missing_optional,
        "study_name": study.get("name", ""),
        "identifiers_preview": id_preview,
    }
