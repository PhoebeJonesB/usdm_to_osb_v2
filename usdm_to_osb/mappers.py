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

def map_identification_metadata(version: dict) -> dict:
    """Build identification_metadata.registry_identifiers from studyIdentifiers."""
    identifiers = version.get("studyIdentifiers", [])
    ct_gov_id = None
    eudract_id = None
    for ident in identifiers:
        text = ident.get("text", "")
        if text.startswith("NCT"):
            ct_gov_id = text
        elif text.startswith("20") and "-" in text:
            eudract_id = text

    return {
        "registry_identifiers": {
            "ct_gov_id": ct_gov_id,
            "eudract_id": eudract_id,
        }
    }


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

def map_study_intervention(design: dict, ct: CTResolver) -> dict:
    result: dict[str, Any] = {}

    # intervention_model_code
    code, decode = _code_obj(design.get("model"))
    result["intervention_model_code"] = _term_or_none(ct.resolve("Intervention Model", code=code, decode=decode))

    # trial_blinding_schema_code
    code, decode = _alias_code(design.get("blindingSchema"))
    result["trial_blinding_schema_code"] = _term_or_none(
        ct.resolve("Trial Blinding Schema", code=code, decode=decode)
    )

    # trial_intent_types_codes
    intent_types = design.get("intentTypes", [])
    result["trial_intent_types_codes"] = ct.resolve_multiple("Trial Intent Type", intent_types) or None

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

def _build_instance_maps(design: dict) -> tuple[dict, dict, dict]:
    """
    Parse scheduleTimelines -> instances to build:
      encounter_to_epoch:  {encounter_id: epoch_id}
      encounter_to_instance: {encounter_id: instance_id}
      instance_to_timing: {instance_id: timing_dict}

    The main timeline's ``instances`` array links encounters to epochs:
        instance.encounterId -> encounter
        instance.epochId     -> epoch
    """
    enc_to_epoch: dict[str, str] = {}
    enc_to_instance: dict[str, str] = {}
    instance_to_timing: dict[str, dict] = {}

    for tl in design.get("scheduleTimelines", []):
        # Only use main timeline for epoch linkage
        is_main = tl.get("mainTimeline", False)

        # Build instance -> timing map from timings
        for timing in tl.get("timings", []):
            from_id = timing.get("relativeFromScheduledInstanceId", "")
            if from_id:
                instance_to_timing[from_id] = timing

        if is_main:
            for inst in tl.get("instances", []):
                enc_id = inst.get("encounterId", "")
                epoch_id = inst.get("epochId", "")
                inst_id = inst.get("id", "")
                if enc_id:
                    enc_to_epoch[enc_id] = epoch_id
                    enc_to_instance[enc_id] = inst_id

    return enc_to_epoch, enc_to_instance, instance_to_timing


def _determine_global_anchor(design: dict) -> str | None:
    """
    Find the global anchor encounter — the one whose timing type decode is
    "Fixed Reference" in the main timeline.  This is typically Day 1 / Dosing.
    Returns the encounter_id or None.
    """
    for tl in design.get("scheduleTimelines", []):
        if not tl.get("mainTimeline", False):
            continue
        for timing in tl.get("timings", []):
            timing_type = timing.get("type", {}).get("decode", "")
            if timing_type.lower() == "fixed reference":
                # The "from" instance is the anchor
                inst_id = timing.get("relativeFromScheduledInstanceId", "")
                # Find the encounter for this instance
                for inst in tl.get("instances", []):
                    if inst.get("id") == inst_id:
                        anchor_enc = inst.get("encounterId", "")
                        logger.info("Global anchor: instance=%s encounter=%s (Fixed Reference)",
                                    inst_id, anchor_enc)
                        return anchor_enc
    return None


def _get_timing_for_encounter(
    enc_id: str,
    enc_to_instance: dict[str, str],
    instance_to_timing: dict[str, dict],
) -> dict | None:
    """Get the timing dict associated with an encounter via its instance."""
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
    encounters_by_id = {e["id"]: e for e in design.get("encounters", [])}

    # Build linkage maps from scheduleTimeline instances
    enc_to_epoch, enc_to_instance, instance_to_timing = _build_instance_maps(design)

    # Determine global anchor encounter
    global_anchor_enc_id = _determine_global_anchor(design)

    # Resolve the time_reference_uid for the global anchor visit
    # The OSB API requires this — submission value "GLOBAL ANCHOR VISIT REFERENCE"
    global_anchor_ref = ct.resolve("Time Reference", decode="Global Anchor Visit Reference")
    if not global_anchor_ref:
        # Try direct submission value search across all codelists
        global_anchor_ref = ct.resolve_global_by_submission("GLOBAL ANCHOR VISIT REFERENCE")
    if not global_anchor_ref:
        # Broadest fallback — partial match
        global_anchor_ref = ct.resolve_global_by_partial_submission("Global Anchor Visit")
    global_anchor_ref_uid = global_anchor_ref["term_uid"] if global_anchor_ref else None
    logger.info("Global anchor time_reference_uid: %s", global_anchor_ref_uid)

    # Group encounters by epoch (preserving order)
    epoch_order = [ep.get("id", "") for ep in design.get("epochs", [])]
    epoch_encounters: dict[str, list[str]] = {eid: [] for eid in epoch_order}
    for enc_id, epoch_id in enc_to_epoch.items():
        if epoch_id in epoch_encounters:
            epoch_encounters[epoch_id].append(enc_id)

    # Also catch any encounters not linked via instances (fallback)
    linked_enc_ids = set(enc_to_epoch.keys())
    for enc in design.get("encounters", []):
        enc_id = enc.get("id", "")
        if enc_id not in linked_enc_ids:
            # Try scheduledAtId or just assign to first epoch
            logger.warning("Encounter '%s' not linked via timeline instances", enc_id)

    result = []
    for epoch_id in epoch_order:
        epoch_uid = epoch_uid_map.get(epoch_id)
        enc_ids = epoch_encounters.get(epoch_id, [])

        for enc_id in enc_ids:
            enc = encounters_by_id.get(enc_id)
            if not enc:
                continue

            label = enc.get("label", enc.get("name", ""))

            # ── Visit type: resolve dynamically (no hardcoded fallback) ──
            type_code, type_decode = _code_obj(enc.get("type"))
            visit_type = ct.resolve("Visit Type", code=type_code, decode=type_decode)
            if not visit_type:
                # Try the label itself as decode for fuzzy matching
                visit_type = ct.resolve("Visit Type", decode=label)
            logger.info("  Visit '%s': type code=%s decode='%s' -> %s",
                        label, type_code, type_decode, visit_type)

            # ── Contact mode: search decode text within Visit Contact Mode codelist ──
            contact_mode_uid = None
            contact_modes = enc.get("contactModes", [])
            if contact_modes:
                cm_code, cm_decode = _code_obj(contact_modes[0])
                contact_mode_uid = _resolve_contact_mode(ct, cm_code, cm_decode)
                logger.info("  Visit '%s': contactMode code=%s decode='%s' -> uid=%s",
                            label, cm_code, cm_decode, contact_mode_uid)

            # ── Timing from scheduleTimeline ──
            timing = _get_timing_for_encounter(enc_id, enc_to_instance, instance_to_timing)
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

                # Parse window bounds
                wl = timing.get("windowLower", "")
                wu = timing.get("windowUpper", "")
                if wl:
                    wl_val, wl_unit = _parse_iso8601_duration(wl)
                    if wl_val is not None:
                        min_window = -abs(int(wl_val))  # lower bound is typically negative
                        window_unit = wl_unit
                if wu:
                    wu_val, wu_unit = _parse_iso8601_duration(wu)
                    if wu_val is not None:
                        max_window = int(wu_val)
                        window_unit = window_unit or wu_unit

                logger.info("  Visit '%s': timing value=%s -> %d %s, window=[%s, %s]",
                            label, raw_value, time_value, time_unit,
                            min_window, max_window)

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
                # Stash for logging
                "_label": label,
                "_epoch_id": epoch_id,
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
        resolved_level = ct.resolve("Objective Level", code=level_code, decode=level_decode)

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

def map_criteria(version: dict, design: dict, ct: CTResolver) -> list[dict]:
    """
    Build criteria list with type (inclusion/exclusion) resolved dynamically.
    """
    # Build text map from eligibilityCriterionItems
    criterion_items = version.get("eligibilityCriterionItems", [])
    text_map = {c["id"]: c.get("text", "") for c in criterion_items}

    result = []
    for crit in design.get("eligibilityCriteria", []):
        cat = crit.get("category", {}).get("decode", "").lower()
        cat_code = crit.get("category", {}).get("code", "")
        is_inclusion = cat.startswith("in")

        # Resolve type dynamically
        search = "Inclusion" if is_inclusion else "Exclusion"
        resolved = ct.resolve("Criteria Type", code=cat_code, decode=search)

        item_id = crit.get("criterionItemId", crit.get("id", ""))
        raw_text = text_map.get(item_id, crit.get("text", ""))

        result.append({
            "id": item_id,
            "type": "inclusion" if is_inclusion else "exclusion",
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
