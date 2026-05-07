"""
USDM 4.0 JSON  ->  OpenStudyBuilder API payload mappers.

Every mapper function takes raw USDM data + a CTResolver and returns
a dict ready to POST/PATCH to the OSB API.  No UIDs are hardcoded.

Key design choices:
  - Arms: resolve type dynamically via code+decode, NO hardcoded fallback.
  - Epochs: resolve via "Epoch Type" codelist (code+decode from USDM type).
  - Visits: linked to epochs via scheduleTimeline instances, epoch-grouped
    ordering, global anchor from Fixed Reference timing, dynamic visit_type
    and contactMode (no hardcoded mapping dicts).
  - Activities: traverse childIds and biomedicalConceptIds; resolve each
    biomedical concept as an activity in the frontend; only create under
    TBD when nothing matches at all.
"""

import logging
import re
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from .ct_resolver import CTResolver

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _code_obj(code_dict: dict | None) -> tuple[str, str]:
    """Extract (code, decode) from a USDM Code object."""
    if not code_dict:
        return ("", "")
    return (code_dict.get("code", ""), code_dict.get("decode", ""))


def _alias_code(alias: dict | None) -> tuple[str, str]:
    """Extract (code, decode) from a USDM AliasCode -> standardCode."""
    if not alias:
        return ("", "")
    sc = alias.get("standardCode", {})
    return (sc.get("code", ""), sc.get("decode", ""))


def _term_or_none(resolved: dict | None) -> dict | None:
    """Return {"term_uid": ..., "name": ...} or None."""
    return resolved


# Known equivalences: USDM/CDISC contact mode decodes -> OSB term names
# Used as last-resort fallback when fuzzy/substring matching fails
_CONTACT_MODE_EQUIVALENCES = {
    "in person": "on site visit",
    "face to face": "on site visit",
    "on site": "on site visit",
    "telephone call": "phone contact",
    "telephone": "phone contact",
    "phone call": "phone contact",
    "virtual": "virtual visit",
    "telemedicine": "virtual visit",
    "remote": "virtual visit",
}


def _resolve_contact_mode(ct: CTResolver, cm_code: str, cm_decode: str) -> str | None:
    """
    Resolve visit contact mode by searching the decode text within the
    Visit Contact Mode codelist.

    Strategy:
      1. ct.resolve() — standard codelist resolution (concept_id, name, fuzzy)
      2. Substring word match — check if any word (len≥3) from the codelist
         term name appears in the decode text, or vice versa
      3. Fuzzy match — get_close_matches with low cutoff (0.3)
      4. Known equivalences — CDISC → OSB terminology mapping

    Returns term_uid or None.
    """
    # Step 1: Standard resolution
    cm = ct.resolve("Visit Contact Mode", code=cm_code, decode=cm_decode)
    if cm:
        logger.info("    Contact mode resolved via ct.resolve: '%s' -> %s", cm_decode, cm["term_uid"])
        return cm["term_uid"]

    # Step 2-4: Search within codelist terms
    all_terms = ct.list_terms_in_codelist("Visit Contact Mode")
    if not all_terms:
        logger.warning("    Visit Contact Mode codelist is empty — cannot resolve '%s'", cm_decode)
        return None

    decode_lower = cm_decode.strip().lower()
    decode_words = [w for w in decode_lower.split() if len(w) >= 3]

    # Step 2: Substring word match
    for term in all_terms:
        term_name_lower = term.get("name", "").lower()
        term_words = [w for w in term_name_lower.split() if len(w) >= 3]
        for tw in term_words:
            for dw in decode_words:
                if tw in dw or dw in tw:  # e.g. "phone" in "telephone"
                    logger.info("    Contact mode matched via word substring: '%s' -> '%s' (%s) "
                                "[word '%s' ↔ '%s']",
                                cm_decode, term["name"], term["term_uid"], tw, dw)
                    return term["term_uid"]

    # Step 3: Fuzzy match with low cutoff
    term_names = [t.get("name", "") for t in all_terms]
    fuzzy = get_close_matches(cm_decode, term_names, n=1, cutoff=0.3)
    if fuzzy:
        matched_term = next(t for t in all_terms if t.get("name") == fuzzy[0])
        logger.info("    Contact mode matched via fuzzy: '%s' -> '%s' (%s)",
                     cm_decode, matched_term["name"], matched_term["term_uid"])
        return matched_term["term_uid"]

    # Step 4: Known equivalences
    equiv_name = _CONTACT_MODE_EQUIVALENCES.get(decode_lower)
    if equiv_name:
        for term in all_terms:
            if term.get("name", "").lower() == equiv_name:
                logger.info("    Contact mode matched via equivalence map: '%s' -> '%s' (%s)",
                             cm_decode, term["name"], term["term_uid"])
                return term["term_uid"]

    # Nothing matched — log available terms for debugging
    logger.warning("    Contact mode NOT FOUND for decode='%s'. Available terms: %s",
                    cm_decode, [(t["name"], t["term_uid"]) for t in all_terms])
    return None


def _parse_iso8601_duration(value: str) -> tuple[float | None, str]:
    """
    Parse an ISO 8601 duration like P42D, P2W, P8D, PT4H into (numeric, unit).
    Returns (None, "day") if unparseable.
    """
    if not value:
        return (None, "day")
    m = re.match(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", value)
    if not m:
        return (None, "day")
    years, months, weeks, days, hours, minutes = m.groups()
    if weeks:
        return (int(weeks), "week")
    if days:
        return (int(days), "day")
    if hours:
        return (int(hours), "hour")
    if months:
        return (int(months), "month")
    if years:
        return (int(years), "year")
    return (None, "day")


# ── study creation ───────────────────────────────────────────────────────────

def map_study_creation(usdm: dict, study_number: str) -> dict:
    """
    Build the POST /studies payload from USDM root.

    - project_number comes from studyIdentifiers[0].text
    - study_parent_part_uid is None (no parent)
    """
    study = usdm.get("study", {})
    versions = study.get("versions", [])
    version = versions[0] if versions else {}

    # Official title
    title = ""
    for t in version.get("titles", []):
        if t.get("type", {}).get("decode", "") == "Official Study Title":
            title = t.get("text", "")
            break

    # Project number = first studyIdentifier text
    identifiers = version.get("studyIdentifiers", [])
    project_number = identifiers[0].get("text", "999") if identifiers else "999"
    logger.info("project_number from studyIdentifiers[0]: %s", project_number)

    return {
        "study_number": study_number,
        "study_acronym": study.get("name", ""),
        "study_subpart_acronym": None,
        "description": title or study.get("description", ""),
        "study_parent": None,
        "study_parent_part_uid": None,
        "study_description": {"study_title": title},
        "project_number": project_number,
    }


# ── identification metadata ─────────────────────────────────────────────────

# OSB registry identifiers — full 13-field structure expected by the metadata
# patch endpoint. Each id has a paired ``*_null_value_code`` used when the id
# is intentionally unknown rather than just absent.
_REGISTRY_ID_FIELDS = (
    "ct_gov_id",
    "eudract_id",
    "universal_trial_number_utn",
    "japanese_trial_registry_id_japic",
    "investigational_new_drug_application_number_ind",
    "eu_trial_number",
    "civ_id_sin_number",
    "national_clinical_trial_number",
    "japanese_trial_registry_number_jrct",
    "national_medical_products_administration_nmpa_number",
    "eudamed_srn_number",
    "investigational_device_exemption_ide_number",
    "eu_pas_number",
)

# Heuristics: scope name keywords / regex hits on text -> registry id field.
# Order matters — first match wins.
_REGISTRY_SCOPE_RULES = (
    ("ct_gov_id",                                          {"keywords": ("clinicaltrials.gov", "ct.gov", "clinicaltrials"), "prefix": ("NCT",)}),
    ("eudract_id",                                         {"keywords": ("eudract",),                                       "regex": r"^\d{4}-\d{6}-\d{2}$"}),
    ("universal_trial_number_utn",                         {"keywords": ("universal trial number", "utn", "who utn"),       "prefix": ("U1111-",)}),
    ("japanese_trial_registry_id_japic",                   {"keywords": ("japic",),                                         "prefix": ("JapicCTI-", "JAPIC")}),
    ("investigational_new_drug_application_number_ind",    {"keywords": ("investigational new drug", " ind ", "ind number")}),
    ("eu_trial_number",                                    {"keywords": ("eu trial number", "ctis", "ct-")}),
    ("civ_id_sin_number",                                  {"keywords": ("civ-id", "sin number")}),
    ("national_clinical_trial_number",                     {"keywords": ("national clinical trial",)}),
    ("japanese_trial_registry_number_jrct",                {"keywords": ("jrct",),                                          "prefix": ("jRCT", "JRCT")}),
    ("national_medical_products_administration_nmpa_number", {"keywords": ("nmpa",)}),
    ("eudamed_srn_number",                                 {"keywords": ("eudamed", "srn")}),
    ("investigational_device_exemption_ide_number",        {"keywords": ("ide number", "investigational device")}),
    ("eu_pas_number",                                      {"keywords": ("eu pas", "encepp", "pas number")}),
)


def _classify_registry_identifier(ident: dict) -> str | None:
    """Determine which OSB registry-identifier field a USDM studyIdentifier maps to.

    Two-pass routing — organization-name keywords (most specific) win over
    text-prefix heuristics (more ambiguous: ``"NCT-NAT-…"`` starts with NCT
    but isn't a ClinicalTrials.gov id).
    """
    text = (ident.get("text") or "").strip()
    scope = ident.get("scope") or {}
    org = scope.get("organization") if isinstance(scope, dict) else None
    org_name = (org.get("name") if isinstance(org, dict) else "") or ""
    org_haystack = org_name.lower()

    # Pass 1: organization-name keyword match
    for field, rule in _REGISTRY_SCOPE_RULES:
        for kw in rule.get("keywords", ()):
            if kw in org_haystack:
                return field

    # Pass 2: text-prefix / regex heuristic
    for field, rule in _REGISTRY_SCOPE_RULES:
        for prefix in rule.get("prefix", ()):
            if text.startswith(prefix):
                return field
        rgx = rule.get("regex")
        if rgx and re.match(rgx, text):
            return field

    # Pass 3: text-keyword fallback (covers identifiers with no scope.organization)
    text_haystack = text.lower()
    for field, rule in _REGISTRY_SCOPE_RULES:
        for kw in rule.get("keywords", ()):
            if kw in text_haystack:
                return field
    return None


def map_identification_metadata(version: dict) -> dict:
    """
    Build ``identification_metadata.registry_identifiers`` in the verbose
    OSB shape — all 13 registry id fields plus their ``*_null_value_code``
    companions, with values populated where the USDM provides them.
    """
    registry: dict[str, Any] = {}
    for field in _REGISTRY_ID_FIELDS:
        registry[field] = None
        registry[f"{field}_null_value_code"] = None

    for ident in version.get("studyIdentifiers", []):
        field = _classify_registry_identifier(ident)
        if field and not registry.get(field):
            registry[field] = (ident.get("text") or "").strip() or None

    return {"registry_identifiers": registry}


# ── study description ───────────────────────────────────────────────────────

def map_study_description(version: dict) -> dict:
    """Build study_description from titles."""
    titles = version.get("titles", [])
    official = None
    brief = None
    for t in titles:
        decode = t.get("type", {}).get("decode", "")
        if decode == "Official Study Title":
            official = t.get("text", "")
        elif decode == "Brief Study Title":
            brief = t.get("text", "")
    return {
        "study_title": official,
        "study_short_title": brief,
    }


# ── high-level study design ─────────────────────────────────────────────────

def map_high_level_design(design: dict, ct: CTResolver) -> dict:
    """Build high_level_study_design from the first studyDesign."""
    result: dict[str, Any] = {}

    # study_type_code: studyType is a Code {code, decode}
    code, decode = _code_obj(design.get("studyType"))
    resolved = ct.resolve("Study Type", code=code, decode=decode)
    result["study_type_code"] = _term_or_none(resolved)
    logger.info("  study_type_code: code=%s decode='%s' -> %s", code, decode, resolved)

    # trial_phase_code: studyPhase is an AliasCode -> standardCode
    code, decode = _alias_code(design.get("studyPhase"))
    resolved = ct.resolve("Trial Phase", code=code, decode=decode)
    result["trial_phase_code"] = _term_or_none(resolved)
    logger.info("  trial_phase_code: code=%s decode='%s' -> %s", code, decode, resolved)

    # trial_types_codes: subTypes is a list of Code objects
    sub_types = design.get("subTypes", [])
    resolved_list = ct.resolve_multiple("Trial Type", sub_types) if sub_types else []
    result["trial_types_codes"] = resolved_list or None
    logger.info("  trial_types_codes: %d resolved from %d subTypes", len(resolved_list), len(sub_types))

    return result


# ── study population ─────────────────────────────────────────────────────────

def map_study_population(design: dict, ct: CTResolver) -> dict:
    result: dict[str, Any] = {}
    population = design.get("population", {})

    # therapeutic_area_codes
    ta_list = design.get("therapeuticAreas", [])
    if ta_list:
        resolved = ct.resolve_multiple("Therapeutic Area", ta_list)
        result["therapeutic_area_codes"] = resolved or None

    # disease_conditions_or_indications_codes from indications
    indications = design.get("indications", [])
    if indications:
        codes = indications[0].get("codes", [])
        if codes:
            standard = codes[0].get("standardCode", codes[0])
            code, decode = standard.get("code", ""), standard.get("decode", "")
            result["disease_conditions_or_indications_codes"] = [{"term_uid": code, "name": decode}]

    # sex_of_participants_code
    planned_sex = population.get("plannedSex", [])
    if planned_sex and planned_sex[0]:
        code, decode = _code_obj(planned_sex[0])
        result["sex_of_participants_code"] = _term_or_none(ct.resolve("Sex", code=code, decode=decode))

    # number_of_expected_subjects
    enrollment = population.get("plannedEnrollmentNumber", population.get("plannedEnrollmentNumberQuantity", {}))
    if enrollment:
        val = enrollment.get("value")
        if val is not None:
            result["number_of_expected_subjects"] = int(val)

    # planned age range
    planned_age = population.get("plannedAge")
    if planned_age:
        unit_info = ct.resolve_unit("years") or {"uid": None, "name": "years"}
        min_val = planned_age.get("minValue", {})
        if min_val and min_val.get("value") is not None:
            result["planned_minimum_age_of_subjects"] = {
                "duration_value": min_val["value"],
                "duration_unit_code": unit_info,
            }
        max_val = planned_age.get("maxValue", {})
        if max_val and max_val.get("value") is not None:
            result["planned_maximum_age_of_subjects"] = {
                "duration_value": max_val["value"],
                "duration_unit_code": unit_info,
            }

    # healthy_subject_indicator
    includes_healthy = population.get("includesHealthySubjects")
    if includes_healthy is not None:
        result["healthy_subject_indicator"] = includes_healthy

    return result


# ── study intervention ───────────────────────────────────────────────────────

def map_study_intervention(design: dict, ct: CTResolver, version: dict | None = None) -> dict:
    """
    Build study_intervention metadata.

    Codelists used (each is aliased to its '<name> Response' variant in the
    resolver so frontend codelist-name variants are picked up automatically):
      - Intervention Model   → 'Intervention Model Response'
      - Trial Blinding Schema→ 'Trial Blinding Schema Response'
      - Trial Intent Type    → 'Trial Intent Type Response'
      - Intervention Type    → 'Intervention Type Response'
      - Control Type         → 'Control Type Response'

    For intervention_type / control_type, we read first from the studyDesign
    itself (``design.interventionType``/``design.controlType``) — added by
    the synthesizer for test data — and fall back to
    ``version.studyInterventions[0].type`` when present.
    """
    result: dict[str, Any] = {}

    # intervention_model_code
    code, decode = _code_obj(design.get("model"))
    result["intervention_model_code"] = _term_or_none(ct.resolve("Intervention Model", code=code, decode=decode))
    logger.info("  intervention_model_code: code=%s decode='%s' -> %s",
                code, decode, result["intervention_model_code"])

    # trial_blinding_schema_code
    code, decode = _alias_code(design.get("blindingSchema"))
    result["trial_blinding_schema_code"] = _term_or_none(
        ct.resolve("Trial Blinding Schema", code=code, decode=decode)
    )
    logger.info("  trial_blinding_schema_code: code=%s decode='%s' -> %s",
                code, decode, result["trial_blinding_schema_code"])

    # trial_intent_types_codes
    intent_types = design.get("intentTypes", [])
    result["trial_intent_types_codes"] = ct.resolve_multiple("Trial Intent Type", intent_types) or None
    logger.info("  trial_intent_types_codes: %d resolved from %d intentTypes",
                len(result.get("trial_intent_types_codes") or []), len(intent_types))

    # intervention_type_code: prefer design.interventionType (Code or AliasCode),
    # else first studyInterventions[*].type from the version.
    int_type_obj = design.get("interventionType")
    if int_type_obj:
        if isinstance(int_type_obj, dict) and "standardCode" in int_type_obj:
            code, decode = _alias_code(int_type_obj)
        else:
            code, decode = _code_obj(int_type_obj)
    else:
        code, decode = ("", "")
        if version:
            for si in version.get("studyInterventions", []) or []:
                code, decode = _code_obj(si.get("type"))
                if code or decode:
                    break
    result["intervention_type_code"] = _term_or_none(
        ct.resolve("Intervention Type", code=code, decode=decode)
    )
    logger.info("  intervention_type_code: code=%s decode='%s' -> %s",
                code, decode, result["intervention_type_code"])

    # control_type_code: studyDesign.controlType (synthesizer-provided)
    ct_type_obj = design.get("controlType")
    if ct_type_obj:
        if isinstance(ct_type_obj, dict) and "standardCode" in ct_type_obj:
            code, decode = _alias_code(ct_type_obj)
        else:
            code, decode = _code_obj(ct_type_obj)
        result["control_type_code"] = _term_or_none(
            ct.resolve("Control Type", code=code, decode=decode)
        )
        logger.info("  control_type_code: code=%s decode='%s' -> %s",
                    code, decode, result["control_type_code"])

    return result


# ── study arms ───────────────────────────────────────────────────────────────

def map_study_arms(design: dict, ct: CTResolver) -> list[dict]:
    """
    Build list of study arm payloads.
    Arm type is resolved dynamically via code + decode.  No hardcoded fallback.
    The ct.resolve global submission fallback will pick the right term
    even if the codelist name differs between USDM and OSB.
    """
    arms = design.get("arms", [])
    result = []
    for arm in arms:
        arm_type_code, arm_type_decode = _code_obj(arm.get("type"))

        # Resolve from Arm Type codelist only (strict — no global fallback)
        resolved = ct.resolve("Arm Type", code=arm_type_code, decode=arm_type_decode)

        # If decode contains "treatment" but didn't match, try "Investigational"
        if not resolved and "treatment" in arm_type_decode.lower():
            logger.info("  Arm '%s': 'Treatment' not in Arm Type codelist, trying 'Investigational'",
                        arm.get("name", ""))
            resolved = ct.resolve("Arm Type", decode="Investigational")

        logger.info("  Arm '%s': type code=%s decode='%s' -> %s",
                     arm.get("name", ""), arm_type_code, arm_type_decode, resolved)

        result.append({
            "name": arm.get("name", ""),
            "short_name": arm.get("name", ""),
            "code": arm.get("name", ""),
            "description": arm.get("description", ""),
            "arm_colour": "",
            "randomization_group": arm.get("id", ""),
            "number_of_subjects": 0,
            "arm_type_uid": resolved["term_uid"] if resolved else None,
        })
    return result


# ── epochs ───────────────────────────────────────────────────────────────────

def map_epochs(design: dict, ct: CTResolver, api=None) -> list[dict]:
    """
    Build epoch payloads from USDM epochs using epoch_mapping.csv for CT resolution.

    The mapping CSV maps USDM epoch type codes (CT_CD) to OSB term UIDs (CT_CD_NEW),
    and provides epoch_type_name via GEN_EPOCH_TYPE column.
    """
    import re as _re
    import pandas as pd

    epochs = design.get("epochs", [])
    elements = design.get("elements", [])
    if not epochs:
        return []

    # Load mapping CSV - relative to this file so it works on any OS
    mapping_csv = Path(__file__).parent / "epoch_mapping.csv"
    if not mapping_csv.exists():
        raise FileNotFoundError(f"epoch_mapping.csv not found at {mapping_csv}")
    mapping_df = pd.read_csv(mapping_csv)
    logger.info("Loaded epoch_mapping.csv with %d rows", len(mapping_df))

    # Build mapping_dict: subtype_name (lower) -> epoch_type_name (cleaned)
    mapping_dict = {
        subtype.strip().lower(): etype.replace(" EPOCH TYPE", "")
        for etype, subtype in zip(mapping_df["GEN_EPOCH_TYPE"], mapping_df["GEN_EPOCH_SUB_TYPE"])
    }

    # Build subtype_cd_to_text: CT_CD_NEW -> GEN_EPOCH_SUB_TYPE text
    subtype_cd_to_text = {}
    for _, row in mapping_df.iterrows():
        cd_new = row.get("CT_CD_NEW")
        if pd.notna(cd_new) and str(cd_new).strip():
            subtype_cd_to_text[str(cd_new).strip()] = row["GEN_EPOCH_SUB_TYPE"].strip()

    # Fetch Epoch Sub Type codelist terms once (if api available)
    epoch_subtype_terms = []
    if api is not None:
        resp = api.get("ct/terms", params={"codelist_uid": "C99079", "page_number": 1, "page_size": 1000})
        if resp.status_code == 200:
            epoch_subtype_terms = resp.json().get("items", [])

    result = []
    for index, epoc in enumerate(sorted(epochs, key=lambda e: int(_re.search(r'\d+', e.get("id", "0")).group()))):
        epoch_id = epoc.get("id")
        label = epoc.get("name", "").strip()

        # Get transition rules from matching element
        idx = next((i for i, elem in enumerate(elements) if elem.get("id") == epoch_id), None)
        start_rule = elements[idx].get("transitionStartRule", {}).get("text") if idx is not None else None
        end_rule = elements[idx].get("transitionEndRule", {}).get("text") if idx is not None else None

        # Get epoch type code from USDM
        epoch_type_codes = epoc.get("type", {}).get("code", "")

        # Look up CT_CD in mapping to get CT_CD_NEW
        matched_row = mapping_df[mapping_df["CT_CD"] == epoch_type_codes]
        if not matched_row.empty:
            epoch_type_codes = matched_row.iloc[0]["CT_CD_NEW"]

        # If still no code, try matching by name against GEN_EPOCH_SUB_TYPE
        if not epoch_type_codes or (isinstance(epoch_type_codes, float) and pd.isna(epoch_type_codes)):
            name_match = mapping_df[mapping_df["GEN_EPOCH_SUB_TYPE"].str.lower() == label.lower()]
            if not name_match.empty:
                epoch_type_codes = name_match.iloc[0]["CT_CD_NEW"]
                logger.info("  Epoch '%s': matched by name -> CT_CD_NEW=%s", label, epoch_type_codes)

        # Resolve term_uid and text from Epoch Sub Type codelist
        epoch_term_uid = ""
        epoch_subtype_text = ""
        epoch_type_name = "UNKNOWN"

        code_str = str(epoch_type_codes).strip() if epoch_type_codes and not (isinstance(epoch_type_codes, float) and pd.isna(epoch_type_codes)) else ""

        if code_str:
            # Get the TEXT from CSV mapping (e.g. "Screening", "Treatment")
            epoch_subtype_text = subtype_cd_to_text.get(code_str, "")
            epoch_type_name = mapping_dict.get(epoch_subtype_text.lower(), "UNKNOWN")

            for item in epoch_subtype_terms:
                if item.get("attributes", {}).get("concept_id", "") == code_str:
                    epoch_term_uid = item.get("term_uid", "")
                    break
            # If concept_id didn't match, try matching by term_uid directly
            if not epoch_term_uid:
                for item in epoch_subtype_terms:
                    if item.get("term_uid", "") == code_str:
                        epoch_term_uid = item.get("term_uid", "")
                        break

        # Fallback: if no text from CSV, use the label
        if not epoch_subtype_text:
            epoch_subtype_text = label

        logger.info("  Epoch '%s': code=%s -> term_uid=%s, subtype_text='%s', type_name=%s",
                     label, code_str, epoch_term_uid, epoch_subtype_text, epoch_type_name)

        result.append({
            "epoch": epoch_term_uid or label,
            "epoch_subtype": code_str or epoch_subtype_text,  # Use CT_CD for subtype
            "epoch_type_name": epoch_type_name,
            "description": epoc.get("description", ""),
            "start_rule": start_rule,
            "end_rule": end_rule,
            "color_hash": "",
            "duration_unit": None,
            "order": index + 1,
            "duration": 0,
            "_usdm_epoch_id": epoc.get("id", ""),
        })
    return result


# ── study elements ───────────────────────────────────────────────────────────

def map_study_elements(design: dict, ct: CTResolver) -> list[dict]:
    """Build element payloads from USDM elements."""
    elements = design.get("elements", [])
    result = []
    for elem in elements:
        transition = elem.get("transitionEndRule", {})
        start_rule = elem.get("transitionStartRule", {})

        # Try to resolve the element sub type dynamically
        elem_desc = elem.get("description", "") or elem.get("name", "")
        subtype = ct.resolve("Element Sub Type", decode=elem_desc)
        subtype_uid = subtype["term_uid"] if subtype else None

        result.append({
            "name": elem.get("name", ""),
            "short_name": elem.get("name", ""),
            "code": elem.get("id", ""),
            "description": elem.get("description", ""),
            "planned_duration": None,
            "start_rule": start_rule.get("text", "") if isinstance(start_rule, dict) else str(start_rule),
            "end_rule": transition.get("text", "") if isinstance(transition, dict) else str(transition),
            "element_colour": "",
            "element_subtype_uid": subtype_uid,
        })
    return result


# ── visits (encounters) ─────────────────────────────────────────────────────

# Default OSB contact mode UID for "On Site Visit" — used as a last-resort
# fallback when the dynamic ONSITE submission lookup fails.
_DEFAULT_ONSITE_CONTACT_MODE_UID = "CTTerm_000139"


def _normalize_phrase(s: str) -> str:
    """Lowercase + replace hyphens/underscores with spaces, for fuzzy compare."""
    return (s or "").lower().replace("-", " ").replace("_", " ").strip()


def _resolve_visit_type_for_epoch(ct: CTResolver, epoch_name: str) -> dict | None:
    """
    Find a Visit Type term whose sponsor_preferred_name (or submission_value)
    relates to the given epoch name.

    Resolution order:
      1. Direct codelist resolution by decode (handles aliases + fuzzy).
      2. Word-set match: all significant words from epoch_name appear in
         a term's sponsor name (or vice-versa).
      3. Substring containment (epoch in term, or term in epoch).
    """
    if not epoch_name:
        return None

    # 1. Direct resolution
    hit = ct.resolve("Visit Type", decode=epoch_name)
    if hit:
        return hit

    # 2/3. Search the codelist's terms ourselves
    terms = ct.list_terms_in_codelist("Visit Type")
    if not terms:
        return None

    target = _normalize_phrase(epoch_name)
    target_words = {w for w in target.split() if len(w) >= 3}

    best = None
    best_score = 0
    for term in terms:
        name = _normalize_phrase(term.get("name", ""))
        if not name:
            continue
        name_words = set(name.split())
        common = target_words & name_words
        # Word-set match: all target words present in term name
        if target_words and common == target_words and len(common) > best_score:
            best, best_score = term, len(common)
            continue
        # Substring containment fallback (only if no word-match found yet)
        if best is None and (target in name or name in target):
            best = term
    if best:
        logger.info("  Visit type matched for epoch '%s' -> '%s' (%s)",
                    epoch_name, best.get("name"), best.get("term_uid"))
    return best


# ── label/description parser ───────────────────────────────────────────────
#
# USDM authors often encode the epoch ↔ encounter ↔ timing relationship
# directly in the slash-delimited label or description on either the
# encounter or the linking instance, e.g.:
#
#   "Screening / Visit Identifier 1 / Visit Day -35 to Day -1 / Visit Window NA"
#   "Screening / 1 / Day -35 to Day -1 / NA"
#   "Visit Identifier 2 / Day 1 of Treatment / Visit Window ±3 days"
#   "Treatment Phase / Visit Identifier 6 / Month 3 / Visit Window ±7 days"
#
# parse_visit_label_text() recovers as much structure as possible.
# The output is consumed by build_visit_link_index() — when the structural
# fields (instance.epochId, timing.relativeFromScheduledInstanceId, …) are
# missing, the parsed values are used as a fallback.

_VISIT_NUM_RE = re.compile(
    r"^\s*(?:visit\s*identifier\s+)?(\d+)\s*$",
    re.IGNORECASE,
)
_TIME_RANGE_RE = re.compile(
    r"(?:visit\s+)?(day|days|week|weeks|month|months|year|years|hour|hours|min|mins|minute|minutes)"
    r"\s+(-?\d+)"
    r"(?:\s+to\s+(?:day|days|week|weeks|month|months|year|years|hour|hours|min|mins|minute|minutes)?\s*(-?\d+))?",
    re.IGNORECASE,
)
_TIME_OF_PHASE_RE = re.compile(
    r"(day|days|week|weeks|month|months|year|years)\s+(-?\d+)\s+of\s+\w+",
    re.IGNORECASE,
)
_WINDOW_RE = re.compile(
    r"(?:visit\s*window\s+)?(?:±|\+/-|\+/\-|\+\-)\s*(\d+)"
    r"(?:\s*(day|days|week|weeks|month|months|hour|hours))?",
    re.IGNORECASE,
)
_NA_RE = re.compile(r"^\s*(?:visit\s*window\s+)?(?:na|n/a)\s*$", re.IGNORECASE)


def _normalize_time_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.lower().rstrip("s")
    if u in ("min", "minute"):
        return "minute"
    return u


def parse_visit_label_text(text: str | None) -> dict:
    """
    Parse a slash-delimited visit label/description. Returns a dict with
    these keys (each value may be None when not present in the text):

        epoch_name          e.g. "Screening", "Treatment Phase"
        visit_number        e.g. 1, 2, 6
        time_value          numeric, signed; e.g. -35, 1, 3
        time_value_end      end of a range like "Day -35 to Day -1" -> -1
        time_unit           "day", "week", "month", "year", "hour", "minute"
        window_lower        signed (negative); e.g. -3, -7
        window_upper        signed (positive); e.g. 3, 7
        window_unit         "day", "week", "month", "hour"
        raw_segments        list of trimmed segments — for diagnostics
    """
    out: dict[str, Any] = {
        "epoch_name": None,
        "visit_number": None,
        "time_value": None,
        "time_value_end": None,
        "time_unit": None,
        "window_lower": None,
        "window_upper": None,
        "window_unit": None,
        "raw_segments": [],
    }
    if not text:
        return out

    segments = [s.strip() for s in text.split("/") if s.strip()]
    out["raw_segments"] = segments
    if not segments:
        return out

    classified = [False] * len(segments)

    # 1. visit number — bare integer or "Visit Identifier N"
    for i, seg in enumerate(segments):
        m = _VISIT_NUM_RE.match(seg)
        if m and out["visit_number"] is None:
            out["visit_number"] = int(m.group(1))
            classified[i] = True
            break

    # 2. time — "Day X of <phase>", "Day X to Day Y", "Day X", "Month 3"
    for i, seg in enumerate(segments):
        if classified[i]:
            continue
        m = _TIME_OF_PHASE_RE.search(seg)
        if m and out["time_value"] is None:
            out["time_unit"] = _normalize_time_unit(m.group(1))
            out["time_value"] = int(m.group(2))
            classified[i] = True
            continue
        m = _TIME_RANGE_RE.search(seg)
        if m and out["time_value"] is None:
            out["time_unit"] = _normalize_time_unit(m.group(1))
            out["time_value"] = int(m.group(2))
            if m.group(3) is not None:
                out["time_value_end"] = int(m.group(3))
            classified[i] = True
            continue

    # 3. window — "Visit Window NA", "NA", "±N days", "Visit Window ±7 days"
    for i, seg in enumerate(segments):
        if classified[i]:
            continue
        if _NA_RE.match(seg):
            classified[i] = True   # explicit NA — leave window None
            continue
        m = _WINDOW_RE.search(seg)
        if m:
            n = int(m.group(1))
            unit = _normalize_time_unit(m.group(2)) if m.group(2) else "day"
            out["window_lower"] = -n
            out["window_upper"] = n
            out["window_unit"] = unit
            classified[i] = True
            continue

    # 4. first remaining segment is treated as the epoch / phase name
    for i, seg in enumerate(segments):
        if not classified[i] and out["epoch_name"] is None:
            out["epoch_name"] = seg
            classified[i] = True
            break

    return out


def _pick_label_text(instance: dict | None, encounter: dict | None) -> str:
    """
    Pick the most structured label/description text for parsing.

    Prefers slash-delimited strings (more segments = more information),
    falling back to whichever non-empty value is available. Looks at the
    linking *instance* first because authors tend to put the structured
    label there; the encounter is the fallback.
    """
    sources: list[dict] = []
    if instance:
        sources.append(instance)
    if encounter:
        sources.append(encounter)

    best = ""
    best_segments = 0
    for src in sources:
        for key in ("label", "description"):
            text = (src.get(key) or "").strip()
            if not text:
                continue
            n_segments = text.count("/")
            if n_segments > best_segments:
                best, best_segments = text, n_segments
    if best:
        return best

    # No slash-delimited candidate — return the first non-empty value found
    for src in sources:
        for key in ("label", "description"):
            text = (src.get(key) or "").strip()
            if text:
                return text
    return ""


def _resolve_onsite_contact_mode_uid(
    ct: CTResolver,
    fallback: str = _DEFAULT_ONSITE_CONTACT_MODE_UID,
) -> str:
    """
    Look up the contact-mode term whose submission_value is "ONSITE".

    Tries (in order):
      1. exact submission_value "ONSITE" within the Visit Contact Mode codelist
      2. global submission_value search across all codelists
      3. partial submission_value match
      4. hardcoded fallback (CTTerm_000139)
    """
    # 1. Within the Visit Contact Mode codelist
    resolver = getattr(ct, "resolve_by_submission_value", None)
    if callable(resolver):
        hit = resolver("Visit Contact Mode", "ONSITE")
        if hit and hit.get("term_uid"):
            logger.info("ONSITE contact mode resolved within Visit Contact Mode: %s", hit["term_uid"])
            return hit["term_uid"]

    # 2. Global exact submission match
    global_resolver = getattr(ct, "resolve_global_by_submission", None)
    if callable(global_resolver):
        hit = global_resolver("ONSITE")
        if hit and hit.get("term_uid"):
            logger.info("ONSITE contact mode resolved via global submission lookup: %s", hit["term_uid"])
            return hit["term_uid"]

    # 3. Global partial submission match
    partial_resolver = getattr(ct, "resolve_global_by_partial_submission", None)
    if callable(partial_resolver):
        hit = partial_resolver("ONSITE")
        if hit and hit.get("term_uid"):
            logger.info("ONSITE contact mode resolved via global partial match: %s", hit["term_uid"])
            return hit["term_uid"]

    logger.info("ONSITE contact mode not found dynamically — using fallback %s", fallback)
    return fallback


def build_visit_link_index(design: dict) -> dict:
    """
    Single source of truth for the epoch ↔ encounter ↔ timeline ↔ timing ↔
    instance graph.

    Walks every scheduleTimeline (main and sub) once and produces an index
    that downstream callers can read without re-parsing the USDM tree.

    Returned keys:
      encounters_by_id              {encounter_id: encounter_dict}
      epochs_by_id                  {epoch_id: epoch_dict}
      epoch_order                   [epoch_id, ...]   declaration order
      instances_by_id               {instance_id: instance_dict}      (all timelines)
      timing_by_from_instance       {instance_id: timing_dict}        (all timelines)
      main_instance_to_encounter    {instance_id: encounter_id}
      main_instance_to_epoch        {instance_id: epoch_id}
      encounter_to_main_instance    {encounter_id: instance_id}
      encounter_to_epoch            {encounter_id: epoch_id}
      epoch_encounters              {epoch_id: [encounter_id, ...]}   in main-timeline order
      global_anchor_instance_id     instance_id of the Fixed Reference timing, or None
      global_anchor_encounter_id    encounter_id linked to that instance, or None
      sub_timeline_by_main_instance {main_instance_id: sub_timeline_id}
                                    where the main instance points at a sub-timeline
                                    (instance.timelineId is set)
    """
    encounters_by_id = {e["id"]: e for e in design.get("encounters", [])}
    epochs_by_id = {ep["id"]: ep for ep in design.get("epochs", [])}
    epoch_order = [ep.get("id", "") for ep in design.get("epochs", [])]

    instances_by_id: dict[str, dict] = {}
    timing_by_from_instance: dict[str, dict] = {}
    main_instance_to_encounter: dict[str, str] = {}
    main_instance_to_epoch: dict[str, str] = {}
    encounter_to_main_instance: dict[str, str] = {}
    encounter_to_epoch: dict[str, str] = {}
    sub_timeline_by_main_instance: dict[str, str] = {}
    main_timeline_instance_order: list[str] = []
    global_anchor_instance_id: str | None = None

    for tl in design.get("scheduleTimelines", []):
        is_main = bool(tl.get("mainTimeline", False))

        # Index every instance (main + sub) by id
        for inst in tl.get("instances", []):
            inst_id = inst.get("id", "")
            if not inst_id:
                continue
            instances_by_id[inst_id] = inst

            if is_main:
                main_timeline_instance_order.append(inst_id)
                enc_id = inst.get("encounterId", "") or ""
                epoch_id = inst.get("epochId", "") or ""
                if enc_id:
                    # First main-timeline instance for an encounter wins
                    encounter_to_main_instance.setdefault(enc_id, inst_id)
                    encounter_to_epoch.setdefault(enc_id, epoch_id)
                    main_instance_to_encounter[inst_id] = enc_id
                    main_instance_to_epoch[inst_id] = epoch_id
                sub_tl_id = inst.get("timelineId") or None
                if sub_tl_id:
                    sub_timeline_by_main_instance[inst_id] = sub_tl_id

        # Index every timing (across ALL timelines) by its "from" instance.
        # The "from" instance is the visit being timed; "to" is the reference.
        for timing in tl.get("timings", []):
            from_id = timing.get("relativeFromScheduledInstanceId", "")
            if from_id:
                timing_by_from_instance[from_id] = timing

            if is_main and global_anchor_instance_id is None:
                ttype = timing.get("type", {}).get("decode", "")
                if ttype.lower() == "fixed reference":
                    global_anchor_instance_id = timing.get("relativeFromScheduledInstanceId") or None

    global_anchor_encounter_id = (
        main_instance_to_encounter.get(global_anchor_instance_id)
        if global_anchor_instance_id else None
    )
    if global_anchor_instance_id:
        logger.info(
            "Global anchor: instance=%s encounter=%s (Fixed Reference)",
            global_anchor_instance_id, global_anchor_encounter_id,
        )

    # ── Label/description parsing ────────────────────────────────────────
    # Parse the slash-delimited label/description on each encounter (and its
    # linking main-timeline instance, if any). This gives us a fallback
    # source for: epoch linkage, time value, and window.
    parsed_label_by_encounter: dict[str, dict] = {}
    for enc_id, enc in encounters_by_id.items():
        link_inst = instances_by_id.get(encounter_to_main_instance.get(enc_id, ""))
        parsed_label_by_encounter[enc_id] = parse_visit_label_text(
            _pick_label_text(link_inst, enc)
        )

    # Build epoch_name -> epoch_id index for the by-name fallback.
    epoch_name_to_id: dict[str, str] = {}
    for ep_id, ep in epochs_by_id.items():
        for key in ("label", "name"):
            n = (ep.get(key) or "").strip().lower()
            if n:
                epoch_name_to_id.setdefault(n, ep_id)

    # Recover epoch linkage for encounters that have no instance.epochId by
    # matching the parsed epoch_name against the design's epochs.
    for enc_id, parsed in parsed_label_by_encounter.items():
        if enc_id in encounter_to_epoch and encounter_to_epoch[enc_id]:
            continue
        ep_name = parsed.get("epoch_name")
        if not ep_name:
            continue
        ep_id = epoch_name_to_id.get(ep_name.strip().lower())
        if not ep_id:
            # Tolerate trailing words like "Screening Phase" vs "Screening"
            for known_name, known_id in epoch_name_to_id.items():
                if known_name in ep_name.lower() or ep_name.lower() in known_name:
                    ep_id = known_id
                    break
        if ep_id:
            encounter_to_epoch[enc_id] = ep_id
            logger.info(
                "Encounter '%s' linked to epoch '%s' via parsed label epoch_name='%s'",
                enc_id, ep_id, ep_name,
            )

    # Group encounters by epoch — main-timeline order first, then any
    # encounters that were linked only via parsed label.
    epoch_encounters: dict[str, list[str]] = {eid: [] for eid in epoch_order}
    seen_encs: set[str] = set()
    for inst_id in main_timeline_instance_order:
        enc_id = main_instance_to_encounter.get(inst_id)
        if not enc_id or enc_id in seen_encs:
            continue
        epoch_id = main_instance_to_epoch.get(inst_id) or encounter_to_epoch.get(enc_id)
        if epoch_id and epoch_id in epoch_encounters:
            epoch_encounters[epoch_id].append(enc_id)
            seen_encs.add(enc_id)
    for enc_id, epoch_id in encounter_to_epoch.items():
        if enc_id in seen_encs or not epoch_id:
            continue
        if epoch_id in epoch_encounters:
            epoch_encounters[epoch_id].append(enc_id)
            seen_encs.add(enc_id)

    # Final pass: encounters still unlinked after structural + label fallback.
    for enc_id in encounters_by_id:
        if enc_id not in encounter_to_main_instance and enc_id not in encounter_to_epoch:
            logger.warning(
                "Encounter '%s' not linked via main-timeline instance OR parsed label",
                enc_id,
            )

    return {
        "encounters_by_id": encounters_by_id,
        "epochs_by_id": epochs_by_id,
        "epoch_order": epoch_order,
        "instances_by_id": instances_by_id,
        "timing_by_from_instance": timing_by_from_instance,
        "main_instance_to_encounter": main_instance_to_encounter,
        "main_instance_to_epoch": main_instance_to_epoch,
        "encounter_to_main_instance": encounter_to_main_instance,
        "encounter_to_epoch": encounter_to_epoch,
        "epoch_encounters": epoch_encounters,
        "global_anchor_instance_id": global_anchor_instance_id,
        "global_anchor_encounter_id": global_anchor_encounter_id,
        "sub_timeline_by_main_instance": sub_timeline_by_main_instance,
        "parsed_label_by_encounter": parsed_label_by_encounter,
    }


# Backward-compatible thin wrappers — keep the old names callable in case
# anything imports them. They delegate to build_visit_link_index().

def _build_instance_maps(design: dict) -> tuple[dict, dict, dict]:
    idx = build_visit_link_index(design)
    return (
        dict(idx["encounter_to_epoch"]),
        dict(idx["encounter_to_main_instance"]),
        dict(idx["timing_by_from_instance"]),
    )


def _determine_global_anchor(design: dict) -> str | None:
    return build_visit_link_index(design)["global_anchor_encounter_id"]


def _get_timing_for_encounter(
    enc_id: str,
    enc_to_instance: dict[str, str],
    instance_to_timing: dict[str, dict],
) -> dict | None:
    inst_id = enc_to_instance.get(enc_id, "")
    return instance_to_timing.get(inst_id)


def map_visits_grouped_by_epoch(
    design: dict,
    ct: CTResolver,
    epoch_uid_map: dict[str, str],
) -> list[dict]:
    """
    Build visit payloads grouped by epoch.

    Uses scheduleTimeline -> instances to determine:
      - Which epoch each encounter belongs to
      - Timing values (from ISO 8601 duration in timing.value)
      - Window lower/upper bounds
      - Which visit is the global anchor (Fixed Reference)

    Visit type and contact mode are resolved dynamically from the USDM
    code+decode — NO hardcoded mapping dictionaries.

    Returns visits ordered by epoch, so the caller can post them
    epoch-by-epoch.
    """
    # Single dynamic linkage index — replaces ad-hoc per-call traversals.
    idx = build_visit_link_index(design)
    encounters_by_id = idx["encounters_by_id"]
    epochs_by_id = idx["epochs_by_id"]
    timing_by_from_instance = idx["timing_by_from_instance"]
    encounter_to_main_instance = idx["encounter_to_main_instance"]
    epoch_order = idx["epoch_order"]
    epoch_encounters = idx["epoch_encounters"]
    global_anchor_enc_id = idx["global_anchor_encounter_id"]
    parsed_label_by_encounter = idx["parsed_label_by_encounter"]

    # Resolve the time_reference_uid for the global anchor visit.
    # The OSB API requires this — submission value "GLOBAL ANCHOR VISIT REFERENCE".
    global_anchor_ref = ct.resolve("Time Reference", decode="Global Anchor Visit Reference")
    if not global_anchor_ref:
        global_anchor_ref = ct.resolve_global_by_submission("GLOBAL ANCHOR VISIT REFERENCE")
    if not global_anchor_ref:
        global_anchor_ref = ct.resolve_global_by_partial_submission("Global Anchor Visit")
    global_anchor_ref_uid = global_anchor_ref["term_uid"] if global_anchor_ref else None
    logger.info("Global anchor time_reference_uid: %s", global_anchor_ref_uid)

    # Resolve the ONSITE fallback once (used when an encounter has no contactMode
    # or the dynamic codelist lookup fails).
    onsite_fallback_uid = _resolve_onsite_contact_mode_uid(ct)

    result = []
    for epoch_id in epoch_order:
        epoch_uid = epoch_uid_map.get(epoch_id)
        enc_ids = epoch_encounters.get(epoch_id, [])

        for enc_id in enc_ids:
            enc = encounters_by_id.get(enc_id)
            if not enc:
                continue

            label = enc.get("label", enc.get("name", ""))
            epoch_obj = epochs_by_id.get(epoch_id, {})
            epoch_name = epoch_obj.get("label") or epoch_obj.get("name") or ""

            # ── Visit type: resolve dynamically ──
            #   1. encounter.type code/decode (often "Visit", rarely useful)
            #   2. encounter label as decode
            #   3. EPOCH NAME match against Visit Type sponsor_preferred_name
            #      (Screening epoch -> SCREEN VISIT TYPE / "Screening", etc.)
            type_code, type_decode = _code_obj(enc.get("type"))
            visit_type = ct.resolve("Visit Type", code=type_code, decode=type_decode)
            if not visit_type:
                visit_type = ct.resolve("Visit Type", decode=label)
            if not visit_type and epoch_name:
                visit_type = _resolve_visit_type_for_epoch(ct, epoch_name)
            logger.info("  Visit '%s' (epoch='%s'): type code=%s decode='%s' -> %s",
                        label, epoch_name, type_code, type_decode, visit_type)

            # ── Contact mode: prefer per-encounter codelist match, fall back to ONSITE ──
            contact_mode_uid = None
            contact_modes = enc.get("contactModes", [])
            if contact_modes:
                cm_code, cm_decode = _code_obj(contact_modes[0])
                contact_mode_uid = _resolve_contact_mode(ct, cm_code, cm_decode)
                logger.info("  Visit '%s': contactMode code=%s decode='%s' -> uid=%s",
                            label, cm_code, cm_decode, contact_mode_uid)
            if not contact_mode_uid:
                contact_mode_uid = onsite_fallback_uid
                logger.info("  Visit '%s': contactMode fallback -> %s", label, contact_mode_uid)

            # ── Timing from scheduleTimeline (via the encounter's main-timeline instance) ──
            inst_id = encounter_to_main_instance.get(enc_id, "")
            timing = timing_by_from_instance.get(inst_id) if inst_id else None
            parsed = parsed_label_by_encounter.get(enc_id, {})
            time_from_record = False
            window_from_record = False
            time_value = 0
            time_unit = "day"
            min_window = None
            max_window = None
            window_unit = None

            if timing:
                # Parse the main timing value (ISO 8601 duration)
                raw_value = timing.get("value", "")
                parsed_val, parsed_unit = _parse_iso8601_duration(raw_value)
                if parsed_val is not None:
                    # If timing type is "Before", the value is negative
                    timing_type_decode = timing.get("type", {}).get("decode", "")
                    if timing_type_decode.lower() == "before":
                        time_value = -abs(int(parsed_val))
                    else:
                        time_value = int(parsed_val)
                    time_unit = parsed_unit
                    time_from_record = True

                # Parse window bounds
                wl = timing.get("windowLower", "")
                wu = timing.get("windowUpper", "")
                if wl:
                    wl_val, wl_unit = _parse_iso8601_duration(wl)
                    if wl_val is not None:
                        min_window = -abs(int(wl_val))  # lower bound is typically negative
                        window_unit = wl_unit
                        window_from_record = True
                if wu:
                    wu_val, wu_unit = _parse_iso8601_duration(wu)
                    if wu_val is not None:
                        max_window = int(wu_val)
                        window_unit = window_unit or wu_unit
                        window_from_record = True

                logger.info("  Visit '%s': timing value=%s -> %d %s, window=[%s, %s]",
                            label, raw_value, time_value, time_unit,
                            min_window, max_window)

            # ── Fallback to parsed label/description when timing is missing ──
            if not time_from_record and parsed.get("time_value") is not None:
                time_value = int(parsed["time_value"])
                time_unit = parsed.get("time_unit") or time_unit
                logger.info("  Visit '%s': time recovered from label -> %d %s",
                            label, time_value, time_unit)
            if not window_from_record and parsed.get("window_lower") is not None:
                min_window = parsed["window_lower"]
                max_window = parsed["window_upper"]
                window_unit = parsed.get("window_unit") or window_unit
                logger.info("  Visit '%s': window recovered from label -> [%s, %s] %s",
                            label, min_window, max_window, window_unit)

            # Resolve time unit
            unit_def = ct.resolve_unit(time_unit)
            time_unit_uid = unit_def["uid"] if unit_def else None

            # Window unit
            window_unit_uid = None
            if window_unit:
                wu_def = ct.resolve_unit(window_unit)
                window_unit_uid = wu_def["uid"] if wu_def else None

            # Is this the global anchor visit?
            is_anchor = (enc_id == global_anchor_enc_id)
            if is_anchor:
                logger.info("  Visit '%s': IS GLOBAL ANCHOR VISIT", label)

            # time_reference_uid:
            #   - Global anchor visit: uses the "GLOBAL ANCHOR VISIT REFERENCE" term
            #   - Non-anchor visits: also reference the global anchor (they are
            #     relative to it via Before/After timing)
            time_ref_uid = global_anchor_ref_uid if global_anchor_ref_uid else None

            result.append({
                "study_epoch_uid": epoch_uid,
                "visit_type_uid": visit_type["term_uid"] if visit_type else None,
                "time_reference_uid": time_ref_uid,
                "time_value": time_value,
                "time_unit_uid": time_unit_uid,
                "visit_sublabel_codelist_uid": None,
                "visit_sublabel_reference": None,
                "consecutive_visit_group": None,
                "show_visit": True,
                "min_visit_window_value": min_window,
                "max_visit_window_value": max_window,
                "visit_window_unit_uid": window_unit_uid,
                "description": enc.get("description", ""),
                "start_rule": "",
                "end_rule": "",
                "visit_contact_mode_uid": contact_mode_uid,
                "epoch_allocation_uid": None,
                "visit_class": "SINGLE_VISIT",
                "visit_subclass": "SINGLE_VISIT",
                "is_global_anchor_visit": is_anchor,
                "is_soa_milestone": False,
                # Stash for logging / downstream diagnostics
                "_label": label,
                "_epoch_id": epoch_id,
                "_parsed_visit_number": parsed.get("visit_number"),
                "_parsed_epoch_name": parsed.get("epoch_name"),
            })

    logger.info("Mapped %d visits across %d epochs", len(result), len(epoch_order))
    return result


# Keep old function name for backward compatibility
def map_visits(design: dict, ct: CTResolver, epoch_uid_map: dict[str, str]) -> list[dict]:
    """Backward-compatible wrapper — delegates to map_visits_grouped_by_epoch."""
    return map_visits_grouped_by_epoch(design, ct, epoch_uid_map)


# ── objectives & endpoints ───────────────────────────────────────────────────

def map_objectives(design: dict, ct: CTResolver) -> list[dict]:
    """
    Build a list of objective dicts with nested endpoint dicts.

    Each objective includes ``level_name`` (the resolved sponsor name) so
    the uploader can derive the endpoint level from the objective level:
      - Primary objective   → endpoint level "Primary Outcome Measure"
      - Secondary objective → endpoint level "Secondary Outcome Measure"
    """
    result = []
    for obj in design.get("objectives", []):
        level_decode = obj.get("level", {}).get("decode", "")
        level_code = obj.get("level", {}).get("code", "")
        resolved_level = ct.resolve("Objective Level", code=level_code, decode=level_decode, strict=True)
        if resolved_level and not ct.term_is_in_codelist(resolved_level.get("term_uid"), "Objective Level"):
            logger.warning("  Objective level term %s ('%s') is NOT in Objective Level codelist — discarding",
                           resolved_level.get("term_uid"), resolved_level.get("name"))
            resolved_level = None

        level_uid = resolved_level["term_uid"] if resolved_level else None
        level_name = resolved_level["name"] if resolved_level else level_decode
        logger.info("  Objective level: code=%s decode='%s' -> uid=%s name='%s'",
                    level_code, level_decode, level_uid, level_name)

        endpoints = []
        for ep in obj.get("endpoints", []):
            endpoints.append({
                "text": ep.get("text", ""),
                "level": ep.get("level", {}),
            })

        result.append({
            "text": obj.get("text", ""),
            "level_uid": level_uid,
            "level_name": level_name,  # used by uploader to derive endpoint level
            "endpoints": endpoints,
        })
    return result


# ── eligibility criteria ─────────────────────────────────────────────────────

def _classify_criterion_category(decode: str) -> str:
    """
    Classify a criterion's category.decode into one of:
      'inclusion', 'exclusion', 'withdrawal', 'run-in', 'other'.

    Recognizes phrases like "Inclusion Criteria", "Exclusion Criteria",
    "Withdrawal Criteria", "Run-in Criteria".
    """
    d = (decode or "").lower().strip()
    if "withdraw" in d:
        return "withdrawal"
    if "run-in" in d or "run in" in d:
        return "run-in"
    if d.startswith("ex") or "exclusion" in d:
        return "exclusion"
    if d.startswith("in") or "inclusion" in d:
        return "inclusion"
    return "other"


def map_criteria(version: dict, design: dict, ct: CTResolver) -> list[dict]:
    """
    Build criteria list with type resolved dynamically from the Criteria Type
    codelist. Supports inclusion / exclusion / withdrawal / run-in.

    Resolution path for each criterion:
      1. ct.resolve("Criteria Type", code=category.code, decode=category.decode)
         — exact concept_id and exact submission_value/sponsor name lookup
      2. Fallback to a sponsor-name decode like "Inclusion Criteria"
      3. None if still no match (logged)
    """
    criterion_items = version.get("eligibilityCriterionItems", [])
    text_map = {c["id"]: c.get("text", "") for c in criterion_items}

    result = []
    for crit in design.get("eligibilityCriteria", []):
        cat_decode = crit.get("category", {}).get("decode", "")
        cat_code = crit.get("category", {}).get("code", "")
        crit_type = _classify_criterion_category(cat_decode)

        # 1. Try with the actual USDM decode (e.g. "Inclusion Criteria")
        resolved = ct.resolve("Criteria Type", code=cat_code, decode=cat_decode, strict=True)

        # 2. Fallback to canonical sponsor-name form
        if not resolved:
            fallback_decode = {
                "inclusion": "Inclusion Criteria",
                "exclusion": "Exclusion Criteria",
                "withdrawal": "Withdrawal Criteria",
                "run-in": "Run-in Criteria",
            }.get(crit_type)
            if fallback_decode:
                resolved = ct.resolve("Criteria Type", decode=fallback_decode, strict=True)

        # 3. Validate the term actually belongs to Criteria Type codelist
        if resolved and not ct.term_is_in_codelist(resolved.get("term_uid"), "Criteria Type"):
            logger.warning("  Criteria type term %s ('%s') is NOT in Criteria Type codelist — discarding",
                           resolved.get("term_uid"), resolved.get("name"))
            resolved = None

        if resolved:
            logger.info("  Criterion category code=%s decode='%s' -> '%s' (%s)",
                        cat_code, cat_decode, resolved.get("name"), resolved.get("term_uid"))
        else:
            logger.warning("  Criterion category code=%s decode='%s' (type=%s) NOT RESOLVED in Criteria Type codelist",
                           cat_code, cat_decode, crit_type)

        item_id = crit.get("criterionItemId", crit.get("id", ""))
        raw_text = text_map.get(item_id, crit.get("text", ""))

        result.append({
            "id": item_id,
            "type": crit_type,
            "type_uid": resolved["term_uid"] if resolved else None,
            "text": raw_text,
        })
    return result


# ── activities with childIds + biomedicalConcepts ────────────────────────────

def _build_bc_map(usdm: dict) -> dict[str, dict]:
    """
    Build {bc_id: bc_dict} from biomedicalConcepts.
    Location varies: can be at study.versions[0].biomedicalConcepts,
    study.biomedicalConcepts, or root.biomedicalConcepts.
    """
    # Try versions[0] first (USDM 4.0 standard location)
    study = usdm.get("study", usdm)
    versions = study.get("versions", [])
    if versions:
        bcs = versions[0].get("biomedicalConcepts", [])
        if bcs:
            return {bc["id"]: bc for bc in bcs}
    # Fallback: study-level or root-level
    bcs = study.get("biomedicalConcepts", usdm.get("biomedicalConcepts", []))
    return {bc["id"]: bc for bc in bcs}


def map_activities(
    design: dict,
    ct: CTResolver,
    usdm: dict | None = None,
    study_number: str = "",
) -> list[dict]:
    """
    Map USDM activities to OSB activity references.

    For each USDM activity:
      1. Try to match by name/synonyms in the OSB activity library.
      2. Traverse childIds — each child activity is also resolved.
      3. Traverse biomedicalConceptIds — look up each BC's name in the
         activity library (biomedical concepts are activities in OSB).
      4. Only create under TBD_<study_number> when fuzzy match fails entirely.

    Returns a list of activity dicts ready for posting.
    """
    bc_map = _build_bc_map(usdm) if usdm else {}
    activities_by_id = {a["id"]: a for a in design.get("activities", [])}

    # Default SoA group
    soa_group = ct.resolve("Flowchart Group", decode="Procedures")
    soa_uid = soa_group["term_uid"] if soa_group else None

    result = []
    seen_uids: set[str] = set()

    def _resolve_and_append(name: str, usdm_id: str, is_bc: bool = False) -> bool:
        """Try to resolve an activity/BC name and append to result. Returns True if matched."""
        matched = ct.resolve_activity(name)
        if not matched:
            return False

        act_uid = matched.get("uid", "")
        if act_uid in seen_uids:
            logger.info("  Already queued activity '%s' (%s), skipping duplicate", name, act_uid)
            return True
        seen_uids.add(act_uid)

        groupings = matched.get("activity_groupings", [{}])
        group_uid = groupings[0].get("activity_group_uid", "") if groupings else ""
        subgroup_uid = groupings[0].get("activity_subgroup_uid", "") if groupings else ""

        result.append({
            "usdm_id": usdm_id,
            "activity_uid": act_uid,
            "activity_group_uid": group_uid,
            "activity_subgroup_uid": subgroup_uid,
            "soa_group_term_uid": soa_uid,
            "activity_instance_uid": None,
            "is_biomedical_concept": is_bc,
        })
        logger.info("  Matched activity '%s' -> uid=%s group=%s", name, act_uid, group_uid)
        return True

    for activity in design.get("activities", []):
        name = activity.get("name", "")
        act_id = activity.get("id", "")

        # 1. Try to match the activity itself
        matched = _resolve_and_append(name, act_id)
        if not matched:
            # Try synonyms
            synonyms = [s.get("name", "") for s in activity.get("synonyms", [])] if activity.get("synonyms") else []
            for syn in synonyms:
                matched = _resolve_and_append(syn, act_id)
                if matched:
                    break

        if not matched:
            logger.warning("  No match for activity '%s' — will create under TBD_%s", name, study_number)
            result.append({
                "usdm_id": act_id,
                "activity_uid": None,
                "activity_name": name,
                "create_under_tbd": True,
                "tbd_study_number": study_number,
                "soa_group_term_uid": soa_uid,
                "activity_instance_uid": None,
                "is_biomedical_concept": False,
            })

        # 2. Traverse childIds — each child is another activity
        child_ids = activity.get("childIds", [])
        for child_id in child_ids:
            child_act = activities_by_id.get(child_id)
            if child_act:
                child_name = child_act.get("name", "")
                child_matched = _resolve_and_append(child_name, child_id)
                if not child_matched:
                    logger.warning("  No match for child activity '%s' (child of '%s')", child_name, name)

        # 3. Traverse biomedicalConceptIds
        bc_ids = activity.get("biomedicalConceptIds", [])
        for bc_id in bc_ids:
            bc = bc_map.get(bc_id)
            if not bc:
                logger.warning("  biomedicalConceptId '%s' not found in JSON biomedicalConcepts", bc_id)
                continue

            bc_name = bc.get("name", "")
            bc_matched = _resolve_and_append(bc_name, bc_id, is_bc=True)
            if not bc_matched:
                # Try the reference code as a search term
                bc_ref = bc.get("reference", "")
                ref_code = bc_ref.split("/")[-1] if bc_ref else ""
                if ref_code:
                    bc_matched = _resolve_and_append(ref_code, bc_id, is_bc=True)

            if not bc_matched:
                logger.warning("  No match for biomedical concept '%s' (BC of '%s')", bc_name, name)

    ok_count = sum(1 for r in result if r.get("activity_uid"))
    tbd_count = sum(1 for r in result if r.get("create_under_tbd"))
    logger.info("Activities mapped: %d matched, %d to create under TBD, %d total",
                ok_count, tbd_count, len(result))
    return result
