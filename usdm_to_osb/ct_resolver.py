"""
Dynamic CT (Controlled Terminology) resolver.

Fetches all CT terms from the OSB frontend once, builds in-memory indexes,
and provides lookup methods so that no codelist UID or term UID ever needs
to be hardcoded.

Key design choices:
  - Codelist name lookup is **space+case insensitive**: "Visit Type", "VisitType",
    "visittype", and "visit type" all resolve to the same codelist.
  - Submission value matching includes **partial/contains** matching so
    "Interventional Study" finds "Interventional".
  - Codelist aliases allow transparent synonym lookup (e.g. "Study Type" <-> "Trial Type").
  - concept_id (e.g. C202487) is used as term_uid directly when it matches.
"""

import logging
import re
from difflib import get_close_matches
from typing import Any

from .api_client import APIClient

logger = logging.getLogger(__name__)


# ── codelist name aliases ────────────────────────────────────────────────
# If a mapper asks for "Study Type" but the OSB instance calls it
# "Trial Type" (or vice-versa), this alias table lets the resolver
# transparently try the synonym.  Both directions are registered automatically.
# Each entry maps a SHORT codelist name to its canonical OSB display name.
# These aliases are SAFE because the short and canonical names refer to the
# SAME codelist in OSB (just renamed). The init code adds them bidirectionally
# so a lookup for either form finds the loaded codelist.
#
# REMOVED (used to be here, dangerous):
#   "study type" → "trial type"  — these are SEPARATE codelists
#   "study phase" → "trial phase" — same
#   "study blinding schema" → "trial blinding schema" — same
#   "study intent type" → "trial intent type" — same
#   "endpoint level" → "endpoint sub level" — same
# A bidirectional alias between two different codelists with overlapping term
# names ("Primary", "Secondary", concept_id C79372, …) causes term_uids to
# leak across codelists and OSB rejects with "term ... not found in codelist".
# If a metadata field needs to map to a different OSB codelist, change the
# call site to use the precise codelist name (e.g. "Study Type Response")
# instead of adding an alias.
CODELIST_ALIASES: dict[str, list[str]] = {
    "study type":              ["study type response"],
    "trial type":              ["trial type response"],
    "study phase":             ["study phase response"],
    "trial phase":             ["trial phase response"],
    "study blinding schema":   ["study blinding schema response"],
    "trial blinding schema":   ["trial blinding schema response"],
    "study intent type":       ["study intent type response"],
    "trial intent type":       ["trial intent type response"],
    "intervention model":      ["intervention model response"],
    "intervention type":       ["intervention type response"],
    "control type":            ["control type response"],
    "sex":                     ["sex of participants response"],
}


def _normalize_codelist_key(name: str) -> str:
    """
    Normalize a codelist name for case+space insensitive lookup.

    "Visit Type", "VisitType", "visit type", "visittype" all become "visittype".
    "Epoch Sub Type", "EpochSubType" -> "epochsubtype".
    """
    return re.sub(r"\s+", "", name).lower().strip()


class CTResolver:
    """
    On construction, fetches every CT term from ``GET /ct/terms`` and builds
    multiple indexes keyed by **normalized** codelist name (spaces removed,
    lowercased):

    * **by_codelist_and_concept** – ``{norm_cl: {concept_id: term_info}}``
    * **by_codelist_and_name** – ``{norm_cl: {sponsor_preferred_name_lower: term_info}}``
    * **by_codelist_and_submission** – ``{norm_cl: {submission_value_lower: term_info}}``
    * **codelist_uids** – ``{norm_cl: codelist_uid}``
    * **_global_by_submission** – ``{submission_value_lower: [{term_uid, name, codelist_name, concept_id}]}``

    Every public method returns ``{"term_uid": ..., "name": ...}`` or ``None``.
    """

    def __init__(self, api: APIClient):
        self.api = api
        self._by_codelist_concept: dict[str, dict[str, dict]] = {}
        self._by_codelist_name: dict[str, dict[str, dict]] = {}
        self._by_codelist_submission: dict[str, dict[str, dict]] = {}
        self._codelist_uids: dict[str, str] = {}
        # Maps: normalized_cl -> [raw_cl_name] so we can reverse-lookup display names
        self._codelist_display_names: dict[str, str] = {}
        self._unit_cache: dict[str, dict] | None = None
        self._activity_cache: list[dict] | None = None

        # Build bidirectional alias map (using normalized keys)
        self._alias_map: dict[str, list[str]] = {}
        for k, aliases in CODELIST_ALIASES.items():
            nk = _normalize_codelist_key(k)
            for a in aliases:
                na = _normalize_codelist_key(a)
                self._alias_map.setdefault(nk, []).append(na)
                self._alias_map.setdefault(na, []).append(nk)

        self._load()

    def _load(self):
        logger.info("Fetching all CT terms from frontend (this may take a moment)...")
        all_terms = self.api.get_all_pages("ct/terms")
        logger.info("Fetched %d CT terms", len(all_terms))

        # Global index: submission_value_lower -> list of {term_uid, name, codelist_name, concept_id}
        self._global_by_submission: dict[str, list[dict]] = {}
        # Global index for partial/contains matching: list of (submission_value_lower, info_dict)
        self._global_all_submissions: list[tuple[str, dict]] = []

        for term in all_terms:
            term_uid = term.get("term_uid", "")
            attrs = term.get("attributes", {})
            name_obj = term.get("name", {})
            concept_id = attrs.get("concept_id", "")
            sponsor_name = name_obj.get("sponsor_preferred_name", "") if isinstance(name_obj, dict) else str(name_obj)

            for cl in term.get("codelists", []):
                cl_uid = cl.get("codelist_uid", "")
                cl_name = cl.get("codelist_name", "")
                sub_val = cl.get("submission_value", "")

                if cl_name:
                    cl_key = _normalize_codelist_key(cl_name)
                    self._codelist_uids.setdefault(cl_key, cl_uid)
                    self._codelist_display_names.setdefault(cl_key, cl_name)

                    # Per-codelist info includes submission_value
                    cl_info = {"term_uid": term_uid, "name": sponsor_name, "submission_value": sub_val}

                    if concept_id:
                        self._by_codelist_concept.setdefault(cl_key, {})[concept_id] = cl_info
                    if sponsor_name:
                        self._by_codelist_name.setdefault(cl_key, {})[sponsor_name.lower().strip()] = cl_info
                    if sub_val:
                        self._by_codelist_submission.setdefault(cl_key, {})[sub_val.lower().strip()] = cl_info
                        # Also index globally so cross-codelist search works
                        global_entry = {
                            "term_uid": term_uid,
                            "name": sponsor_name,
                            "submission_value": sub_val,
                            "codelist_name": cl_name,
                            "concept_id": concept_id,
                        }
                        self._global_by_submission.setdefault(sub_val.lower().strip(), []).append(global_entry)
                        self._global_all_submissions.append((sub_val.lower().strip(), global_entry))

        logger.info("Indexed %d codelists, %d global submission entries",
                     len(self._codelist_uids), len(self._global_all_submissions))
        # Audit log so callers can verify critical codelists were loaded.
        for critical in ("Endpoint Level", "Objective Level", "Criteria Type",
                         "Visit Type", "Visit Contact Mode"):
            key = _normalize_codelist_key(critical)
            present = key in self._codelist_uids
            display = self._codelist_display_names.get(key, "—")
            n = len(self._by_codelist_name.get(key, {}))
            logger.info("  CT codelist '%s' loaded=%s (display=%r, %d terms)",
                        critical, present, display, n)

    # ── codelist key resolution ───────────────────────────────────────────

    def _codelist_keys(self, codelist_name: str, strict: bool = False) -> list[str]:
        """
        Return the normalized key plus any aliases, so resolve() can try them all.

        When ``strict=False`` (default) and the primary key isn't loaded, also
        tries fuzzy matching against known codelist names — convenient for
        typos but DANGEROUS for fields where the wrong codelist returns a
        valid-looking but wrong term (e.g. Endpoint Level vs Objective Level).

        When ``strict=True``, only the primary normalized key plus explicit
        aliases are tried — no fuzzy codelist fallback.
        """
        primary = _normalize_codelist_key(codelist_name)
        keys = [primary]

        if not strict and primary not in self._codelist_uids:
            known_keys = list(self._codelist_uids.keys())
            fuzzy = get_close_matches(primary, known_keys, n=1, cutoff=0.75)
            if fuzzy and fuzzy[0] not in keys:
                display = self._codelist_display_names.get(fuzzy[0], fuzzy[0])
                logger.info("Codelist '%s' not found exactly; fuzzy matched to '%s'", codelist_name, display)
                keys.append(fuzzy[0])

        # Add explicit aliases
        for alt in self._alias_map.get(primary, []):
            if alt not in keys:
                keys.append(alt)

        return keys

    def term_is_in_codelist(self, term_uid: str, codelist_name: str) -> bool:
        """
        Verify that ``term_uid`` is registered under EXACTLY ``codelist_name``.

        Used to validate a resolved term before posting. Only the primary
        normalized key is checked — aliases are deliberately NOT expanded,
        because aliases like "endpoint level" ↔ "endpoint sub level" point
        at different codelists with overlapping term names. We need the
        strict "is this term in THIS specific codelist" answer.
        """
        if not term_uid:
            return False
        cl_key = _normalize_codelist_key(codelist_name)
        for bucket in (self._by_codelist_name.get(cl_key, {}),
                       self._by_codelist_submission.get(cl_key, {}),
                       self._by_codelist_concept.get(cl_key, {})):
            for info in bucket.values():
                if info.get("term_uid") == term_uid:
                    return True
        return False

    def get_codelist_uid(self, codelist_name: str) -> str | None:
        for key in self._codelist_keys(codelist_name):
            uid = self._codelist_uids.get(key)
            if uid:
                return uid
        return None

    # ── single-strategy resolvers ─────────────────────────────────────────

    def resolve_by_concept_id(self, codelist_name: str, concept_id: str) -> dict | None:
        """Look up a term by its CDISC concept ID (e.g. 'C98388') within a codelist."""
        cl_key = _normalize_codelist_key(codelist_name) if codelist_name else None
        if cl_key:
            bucket = self._by_codelist_concept.get(cl_key, {})
            return bucket.get(concept_id)
        return None

    def resolve_by_name(self, codelist_name: str, name: str) -> dict | None:
        """Look up a term by sponsor_preferred_name within a codelist."""
        cl_key = _normalize_codelist_key(codelist_name)
        bucket = self._by_codelist_name.get(cl_key, {})
        return bucket.get(name.lower().strip())

    def resolve_by_submission_value(self, codelist_name: str, value: str) -> dict | None:
        """Look up a term by exact submission_value within a codelist."""
        cl_key = _normalize_codelist_key(codelist_name)
        bucket = self._by_codelist_submission.get(cl_key, {})
        return bucket.get(value.lower().strip())

    def resolve_by_partial_submission(self, codelist_name: str, text: str) -> dict | None:
        """
        Partial/contains match: if text contains a submission_value or vice versa.

        E.g. decode "Interventional Study" contains submission_value "Interventional"
        -> match.  Also handles "Screening Epoch" containing "Screening".
        """
        cl_key = _normalize_codelist_key(codelist_name)
        bucket = self._by_codelist_submission.get(cl_key, {})
        if not bucket:
            return None

        search = text.lower().strip()
        best_match = None
        best_len = 0

        for sub_val, info in bucket.items():
            # Check: does search contain sub_val, or does sub_val contain search?
            if sub_val in search or search in sub_val:
                # Prefer the longest matching submission value (most specific)
                if len(sub_val) > best_len:
                    best_match = info
                    best_len = len(sub_val)

        if best_match:
            logger.info("Partial submission match in '%s': '%s' matched submission_value (len=%d)",
                        codelist_name, text, best_len)
        return best_match

    def fuzzy_resolve(self, codelist_name: str, search_text: str, cutoff: float = 0.6) -> dict | None:
        """Fuzzy match against sponsor_preferred_name within a codelist."""
        cl_key = _normalize_codelist_key(codelist_name)
        bucket = self._by_codelist_name.get(cl_key, {})
        if not bucket:
            return None
        matches = get_close_matches(search_text.lower().strip(), list(bucket.keys()), n=1, cutoff=cutoff)
        if matches:
            logger.info("Fuzzy name match in '%s': '%s' -> '%s'", codelist_name, search_text, matches[0])
            return bucket[matches[0]]
        return None

    def fuzzy_resolve_submission(self, codelist_name: str, search_text: str, cutoff: float = 0.6) -> dict | None:
        """Fuzzy match against submission_value within a codelist."""
        cl_key = _normalize_codelist_key(codelist_name)
        bucket = self._by_codelist_submission.get(cl_key, {})
        if not bucket:
            return None
        matches = get_close_matches(search_text.lower().strip(), list(bucket.keys()), n=1, cutoff=cutoff)
        if matches:
            logger.info("Fuzzy submission match in '%s': '%s' -> '%s'", codelist_name, search_text, matches[0])
            return bucket[matches[0]]
        return None

    # ── global cross-codelist search ──────────────────────────────────────

    def resolve_global_by_submission(self, text: str) -> dict | None:
        """
        Search the decode text as a submission_value across ALL codelists (exact match).
        """
        key = text.lower().strip()
        hits = self._global_by_submission.get(key, [])
        if not hits:
            return None
        if len(hits) > 1:
            cl_names = [h["codelist_name"] for h in hits]
            logger.info("Global exact submission search '%s' found in %d codelists: %s — using first",
                        text, len(hits), cl_names)
        hit = hits[0]
        logger.info("Global exact submission match: '%s' -> term_uid=%s name='%s' (codelist='%s')",
                    text, hit["term_uid"], hit["name"], hit["codelist_name"])
        return {"term_uid": hit["term_uid"], "name": hit["name"], "submission_value": hit.get("submission_value", "")}

    def resolve_global_by_partial_submission(self, text: str) -> dict | None:
        """
        Partial/contains match across ALL codelists.

        E.g. "Interventional Study" will match submission_value "Interventional"
        from any codelist.  Returns the longest (most specific) match.
        """
        search = text.lower().strip()
        best_match = None
        best_len = 0

        for sub_val, info in self._global_all_submissions:
            if sub_val in search or search in sub_val:
                if len(sub_val) > best_len:
                    best_match = info
                    best_len = len(sub_val)

        if best_match:
            logger.info("Global partial submission match: '%s' -> term_uid=%s name='%s' (codelist='%s')",
                        text, best_match["term_uid"], best_match["name"], best_match["codelist_name"])
            return {"term_uid": best_match["term_uid"], "name": best_match["name"], "submission_value": best_match.get("submission_value", "")}
        return None

    def resolve_global_fuzzy_submission(self, text: str, cutoff: float = 0.55) -> dict | None:
        """
        Fuzzy match across ALL codelists' submission_values.
        Last resort before giving up.
        """
        all_sub_vals = list(self._global_by_submission.keys())
        matches = get_close_matches(text.lower().strip(), all_sub_vals, n=1, cutoff=cutoff)
        if matches:
            hits = self._global_by_submission[matches[0]]
            hit = hits[0]
            logger.info("Global fuzzy submission match: '%s' -> '%s' -> term_uid=%s name='%s' (codelist='%s')",
                        text, matches[0], hit["term_uid"], hit["name"], hit["codelist_name"])
            return {"term_uid": hit["term_uid"], "name": hit["name"], "submission_value": hit.get("submission_value", "")}
        return None

    # ── main resolve method ───────────────────────────────────────────────

    def resolve(self, codelist_name: str, code: str = "", decode: str = "",
                strict: bool = False) -> dict | None:
        """
        Resolve within named codelist + aliases only (no global fallback).

        Strategies tried in order:
          1. concept_id match
          2. exact sponsor_preferred_name
          3. exact submission_value
          4. partial/contains submission_value
          5. fuzzy sponsor_preferred_name
          6. fuzzy submission_value

        When ``strict=True``, the fuzzy CODELIST fallback is disabled — useful
        for fields like endpoint_level where matching the wrong codelist
        produces a term that OSB rejects at validation time.

        Returns {"term_uid": ..., "name": ..., "submission_value": ...} or None.
        """
        primary = _normalize_codelist_key(codelist_name)

        for cl_key in self._codelist_keys(codelist_name, strict=strict):
            cl_display = self._codelist_display_names.get(cl_key, cl_key)

            if code:
                result = self.resolve_by_concept_id(cl_key, code)
                if result:
                    if cl_key != primary:
                        logger.info("Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return result

            if decode:
                result = self.resolve_by_name(cl_key, decode)
                if result:
                    if cl_key != primary:
                        logger.info("Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return result

                result = self.resolve_by_submission_value(cl_key, decode)
                if result:
                    if cl_key != primary:
                        logger.info("Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return result

                result = self.resolve_by_partial_submission(cl_key, decode)
                if result:
                    if cl_key != primary:
                        logger.info("Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return result

                result = self.fuzzy_resolve(cl_key, decode)
                if result:
                    if cl_key != primary:
                        logger.info("Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return result

                result = self.fuzzy_resolve_submission(cl_key, decode)
                if result:
                    if cl_key != primary:
                        logger.info("Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return result

        logger.warning("Codelist '%s' (+ aliases) had no match for code='%s' decode='%s'",
                       codelist_name, code, decode)
        return None

    def resolve_multiple(self, codelist_name: str, items: list[dict],
                         strict: bool = False) -> list[dict]:
        """
        Resolve a list of USDM Code objects against a single codelist.

        When ``strict=True``, the fuzzy CODELIST fallback is disabled — use
        this for metadata-patch fields where a wrong-codelist term causes OSB
        to reject the PATCH with a "term not in codelist" 400.
        """
        results = []
        for item in items:
            code = item.get("code", "")
            decode = item.get("decode", "")
            resolved = self.resolve(codelist_name, code=code, decode=decode, strict=strict)
            if resolved:
                results.append(resolved)
            else:
                logger.warning(
                    "Could not resolve term code=%s decode=%s in codelist '%s'",
                    code, decode, codelist_name,
                )
        return results

    # ── unit resolution ───────────────────────────────────────────────────

    def resolve_unit(self, unit_name: str) -> dict | None:
        """
        Resolve a unit name (e.g. 'years', 'day', 'week') to {"uid": ..., "name": ...}.
        Fetches unit definitions on first call.
        """
        if self._unit_cache is None:
            self._unit_cache = {}
            items = self.api.get_all_pages("concepts/unit-definitions")
            for item in items:
                uid = item.get("uid", "")
                name = item.get("name", "")
                if name:
                    self._unit_cache[name.lower().strip()] = {"uid": uid, "name": name}
            logger.info("Cached %d unit definitions", len(self._unit_cache))

        return self._unit_cache.get(unit_name.lower().strip())

    # ── activity resolution ───────────────────────────────────────────────

    def resolve_activity(self, activity_name: str) -> dict | None:
        """
        Search for a SINGLE activity by name using fuzzy matching.
        Returns the full activity dict from the API or None.
        """
        if self._activity_cache is None:
            self._activity_cache = self.api.get_all_pages("concepts/activities/activities")
            logger.info("Cached %d activities", len(self._activity_cache))

        names = [a.get("name", "").lower() for a in self._activity_cache]
        matches = get_close_matches(activity_name.lower().strip(), names, n=1, cutoff=0.6)
        if matches:
            for a in self._activity_cache:
                if a.get("name", "").lower() == matches[0]:
                    logger.info("  Activity match: '%s' -> '%s' (uid=%s)",
                                activity_name, a.get("name"), a.get("uid"))
                    return a
        return None

    def resolve_activity_multi(self, activity_name: str) -> list[dict]:
        """
        Resolve an activity name that may contain multiple activities
        separated by commas, slashes, 'and', or '&'.

        E.g. "Weight, Height" -> [Weight_activity, Height_activity]
        E.g. "Blood Pressure" -> [BloodPressure_activity]

        Returns list of matched activity dicts (may be empty).
        """
        # First try the whole name
        whole = self.resolve_activity(activity_name)
        if whole:
            return [whole]

        # Split on delimiters
        parts = re.split(r'[,/&]+|\band\b', activity_name, flags=re.IGNORECASE)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            return []

        logger.info("  Splitting '%s' into %d parts: %s", activity_name, len(parts), parts)
        results = []
        seen_uids = set()
        for part in parts:
            m = self.resolve_activity(part)
            if m:
                uid = m.get("uid", "")
                if uid not in seen_uids:
                    results.append(m)
                    seen_uids.add(uid)
            else:
                logger.info("  No match for split part: '%s'", part)
        return results

    # ── introspection ─────────────────────────────────────────────────────

    def list_codelists(self) -> dict[str, str]:
        """Return {codelist_name: codelist_uid} for all discovered codelists."""
        # Return with original display names
        return {
            self._codelist_display_names.get(k, k): v
            for k, v in self._codelist_uids.items()
        }

    def list_terms_in_codelist(self, codelist_name: str) -> list[dict]:
        """Return all terms in a codelist as [{"term_uid": ..., "name": ...}, ...]."""
        for key in self._codelist_keys(codelist_name):
            bucket = self._by_codelist_name.get(key, {})
            if bucket:
                return list(bucket.values())
        return []


# ---------------------------------------------------------------------------
# Mapping table: OSB metadata field  ->  codelist name to search
# This is the ONLY place where we define which codelist corresponds to which
# OSB study field. If the OSB instance renames a codelist, update here.
# NOTE: These names are normalized (space+case insensitive) during lookup,
# so "Visit Type" will match "VisitType" in the frontend.
# ---------------------------------------------------------------------------
FIELD_TO_CODELIST = {
    "study_type_code": "Study Type",
    "trial_phase_code": "Trial Phase",
    "trial_types_codes": "Trial Type",
    "sex_of_participants_code": "Sex",
    "intervention_model_code": "Intervention Model",
    "trial_blinding_schema_code": "Trial Blinding Schema",
    "trial_intent_types_codes": "Trial Intent Type",
    "therapeutic_area_codes": "Therapeutic Area",
    "control_type_code": "Control Type",
    "intervention_type_code": "Intervention Type",
    "arm_type": "Arm Type",
    "visit_type": "Visit Type",
    "visit_contact_mode": "Visit Contact Mode",
    "objective_level": "Objective Level",
    "endpoint_level": "Endpoint Level",
    "endpoint_sublevel": "Endpoint Sub Level",
    "criteria_type": "Criteria Type",
    "soa_group": "Flowchart Group",
    "epoch_type": "Epoch Type",
    "element_type": "Element Sub Type",
}
