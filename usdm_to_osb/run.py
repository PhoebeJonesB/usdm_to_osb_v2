#!/usr/bin/env python3
"""
USDM 4.0 -> OpenStudyBuilder Upload Script
==========================================
A single-file module that mirrors the working notebook cell-by-cell.

Two-phase approach:
  Phase 1 - VALIDATION:  Parse the USDM JSON, check every required section,
            log what is present/missing, and ask the user before proceeding.
  Phase 2 - UPLOAD:      Dynamically resolve all CT terms from the frontend,
            then create the study and upload every section.

Run:
    python -m usdm_to_osb
    python -m usdm_to_osb --usdm path/to/file.json

Dependencies:
    pip install requests beautifulsoup4 pandas
"""

# ==============================================================================
# CELL 0 - IMPORTS & LOGGING SETUP
# ==============================================================================

import getpass
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # criteria text stripping will fall back to regex

LOG_FILE = f"usdm_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    force=True,
)
log = logging.getLogger("usdm_upload")
log.info("Log file: %s", LOG_FILE)


# ==============================================================================
# CELL 1 - CONFIGURATION  (edit these values or pass via CLI)
# ==============================================================================

IDP_URL            = os.environ.get("OSB_IDP_URL", "https://your-idp-instance")
API_BASE_URL       = os.environ.get("OSB_API_URL", "https://your-osb-instance/api")
USDM_FILE_PATH     = os.environ.get("USDM_FILE_PATH", "")

# OAuth2 credentials — do NOT hardcode secrets here.
# Set via env vars, --config file, CLI args, or interactive prompt at runtime.
OAUTH_CLIENT_ID     = os.environ.get("OSB_CLIENT_ID", "osbidp")
OAUTH_CLIENT_SECRET = os.environ.get("OSB_CLIENT_SECRET", "")
OAUTH_USERNAME      = os.environ.get("OSB_USERNAME", "")
OAUTH_PASSWORD      = os.environ.get("OSB_PASSWORD", "")

# Leave as None to auto-detect from studyIdentifiers[0].text
PROJECT_NUMBER_OVERRIDE = None  # e.g. "999"


# ==============================================================================
# CELL 2 - TOKEN MANAGER
# ==============================================================================

class TokenManager:
    """OAuth2 password-grant token manager with auto-refresh.

    When ``no_auth=True``, all methods become no-ops and ``get_headers()``
    returns only Content-Type — requests go out without an Authorization
    header (for OSB instances running without an IDP).
    """

    def __init__(self, idp_url, client_id, client_secret, username, password, no_auth=False):
        self.no_auth = bool(no_auth)
        if self.no_auth:
            log.info("[TokenManager] --no-auth mode (no Authorization header will be sent)")
            self.token_url = ""
            self.client_id = ""
            self.client_secret = ""
            self.username = ""
            self.password = ""
        else:
            self.token_url = f"{(idp_url or '').rstrip('/')}/o/token/"
            self.client_id = client_id
            self.client_secret = client_secret
            self.username = username
            self.password = password
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    def _send(self, data, context):
        try:
            resp = requests.post(self.token_url, data=data, timeout=30)
            if resp.status_code == 200:
                r = resp.json()
                self._access_token = r["access_token"]
                expires_in = r.get("expires_in", 300)
                self._expires_at = time.time() + max(expires_in - 60, 10)
                if r.get("refresh_token"):
                    self._refresh_token = r["refresh_token"]
                log.info("[TokenManager] %s OK (expires in %ds)", context, expires_in)
                return True
            log.error("[TokenManager] %s FAILED (%d): %s", context, resp.status_code, resp.text[:200])
        except Exception as exc:
            log.error("[TokenManager] %s exception: %s", context, exc)
        return False

    def _authenticate(self):
        if self.no_auth:
            return True
        return self._send({
            "grant_type": "password",
            "client_id": self.client_id, "client_secret": self.client_secret,
            "username": self.username, "password": self.password,
        }, "password-grant")

    def _refresh(self):
        if self.no_auth:
            return True
        if not self._refresh_token:
            return False
        return self._send({
            "grant_type": "refresh_token",
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self._refresh_token,
        }, "refresh")

    def get_token(self):
        if self.no_auth:
            return ""
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        if self._refresh_token and self._refresh():
            return self._access_token
        if self._authenticate():
            return self._access_token
        raise RuntimeError("Unable to obtain access token")

    def get_headers(self):
        if self.no_auth:
            return {"Content-Type": "application/json"}
        return {"Authorization": f"Bearer {self.get_token()}", "Content-Type": "application/json"}


token_mgr = None  # Initialized in main()


# ==============================================================================
# CELL 3 - API HELPERS
# ==============================================================================

def api_get(path, params=None, timeout=60):
    url = f"{API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    return requests.get(url, headers=token_mgr.get_headers(), params=params, timeout=timeout)


def api_post(path, json_body=None, params=None, timeout=60):
    url = f"{API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    return requests.post(url, headers=token_mgr.get_headers(), json=json_body, params=params, timeout=timeout)


def api_patch(path, json_body=None, timeout=60):
    url = f"{API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    return requests.patch(url, headers=token_mgr.get_headers(), json=json_body, timeout=timeout)


def api_get_all_pages(path, page_size=1000, extra_params=None):
    """Paginate through a list endpoint until exhausted."""
    all_items = []
    page = 1
    while True:
        params = {"page_number": page, "page_size": page_size}
        if extra_params:
            params.update(extra_params)
        resp = api_get(path, params=params)
        if resp.status_code != 200:
            log.warning("GET %s page %d -> %d", path, page, resp.status_code)
            break
        items = resp.json().get("items", [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        page += 1
    return all_items


# ==============================================================================
# CELL 4 - USDM VALIDATION  (Phase 1)
# ==============================================================================

VALIDATION_RULES = [
    ("study.name",              lambda u, v, d: u.get("study", {}).get("name"),                True),
    ("versions[0]",             lambda u, v, d: v,                                              True),
    ("titles",                  lambda u, v, d: v.get("titles"),                                True),
    ("studyIdentifiers",        lambda u, v, d: v.get("studyIdentifiers"),                      True),
    ("studyDesigns[0]",         lambda u, v, d: d,                                              True),
    ("studyType",               lambda u, v, d: d.get("studyType"),                             False),
    ("studyPhase",              lambda u, v, d: d.get("studyPhase"),                            False),
    ("subTypes (trial types)",  lambda u, v, d: d.get("subTypes"),                              False),
    ("population",              lambda u, v, d: d.get("population"),                            False),
    ("model (intervention)",    lambda u, v, d: d.get("model"),                                 False),
    ("blindingSchema",          lambda u, v, d: d.get("blindingSchema"),                        False),
    ("intentTypes",             lambda u, v, d: d.get("intentTypes"),                           False),
    ("therapeuticAreas",        lambda u, v, d: d.get("therapeuticAreas"),                      False),
    ("arms",                    lambda u, v, d: d.get("arms"),                                  False),
    ("epochs",                  lambda u, v, d: d.get("epochs"),                                False),
    ("elements",                lambda u, v, d: d.get("elements"),                              False),
    ("studyCells",              lambda u, v, d: d.get("studyCells"),                            False),
    ("encounters (visits)",     lambda u, v, d: d.get("encounters"),                            False),
    ("scheduleTimelines",       lambda u, v, d: d.get("scheduleTimelines"),                    False),
    ("objectives",              lambda u, v, d: d.get("objectives"),                            False),
    ("eligibilityCriteria",     lambda u, v, d: d.get("eligibilityCriteria"),                   False),
    ("eligibilityCriterionItems", lambda u, v, d: v.get("eligibilityCriterionItems"),           False),
    ("activities",              lambda u, v, d: d.get("activities"),                            False),
    ("indications",             lambda u, v, d: d.get("indications"),                          False),
    ("biomedicalConcepts",      lambda u, v, d: v.get("biomedicalConcepts"),                   False),
]


def validate_usdm(usdm_data: dict) -> tuple:
    log.info("=" * 70)
    log.info("PHASE 1: USDM VALIDATION")
    log.info("=" * 70)

    study = usdm_data.get("study", {})
    versions = study.get("versions", [])
    version = versions[0] if versions else {}
    designs = version.get("studyDesigns", [])
    design = designs[0] if designs else {}

    present = []
    missing_critical = []
    missing_optional = []

    for label, accessor, critical in VALIDATION_RULES:
        try:
            value = accessor(usdm_data, version, design)
        except Exception:
            value = None

        has_data = bool(value)
        if isinstance(value, list) and len(value) == 0:
            has_data = False

        count_str = ""
        if isinstance(value, list) and has_data:
            count_str = f" ({len(value)} items)"

        if has_data:
            log.info("  [OK]      %-35s %s", label, count_str)
            present.append(label)
        elif critical:
            log.error(" [CRITICAL] %-35s MISSING - upload cannot proceed", label)
            missing_critical.append(label)
        else:
            log.warning("  [SKIP]    %-35s MISSING - section will be skipped", label)
            missing_optional.append(label)

    log.info("-" * 70)
    log.info("Present:  %d sections", len(present))
    if missing_optional:
        log.info("Skippable: %d sections: %s", len(missing_optional), ", ".join(missing_optional))
    if missing_critical:
        log.error("CRITICAL:  %d sections missing: %s", len(missing_critical), ", ".join(missing_critical))

    can_proceed = len(missing_critical) == 0
    if can_proceed:
        log.info("Validation PASSED - ready to upload.")
    else:
        log.error("Validation FAILED - fix the critical sections above before uploading.")

    identifiers = version.get("studyIdentifiers", [])
    id_texts = [i.get("text", "") for i in identifiers]
    log.info("Study name:     %s", study.get("name", "(none)"))
    log.info("Identifiers:    %s", id_texts)
    log.info("project_number: %s (from studyIdentifiers[0])", id_texts[0] if id_texts else "(none)")

    parsed = {
        "study_name": study.get("name", ""),
        "version": version,
        "design": design,
        "titles": version.get("titles", []),
        "identifiers": identifiers,
        "present_sections": set(present),
        "usdm_data": usdm_data,
    }

    return can_proceed, present, missing_critical + missing_optional, parsed


# ==============================================================================
# CELL 6 - CT RESOLVER
# ==============================================================================

import re as _re

def _normalize_codelist_key(name: str) -> str:
    return _re.sub(r"\s+", "", name).lower().strip()


_CODELIST_ALIASES = {
    # SAFE aliases: short name ↔ canonical OSB display name (same codelist).
    # REMOVED dangerous cross-codelist links:
    #   studytype ↔ trialtype, studyphase ↔ trialphase,
    #   studyblindingschema ↔ trialblindingschema, studyintenttype ↔ trialintenttype,
    #   endpointlevel ↔ endpointsublevel
    # These caused term_uids to leak between DIFFERENT codelists with
    # overlapping term names ("Primary", concept_id C79372, …). If a metadata
    # field needs a different codelist, change the call site to use the
    # precise name (e.g. "Study Type Response") rather than adding an alias.
    "studytype":            ["studytyperesponse"],
    "trialtype":            ["trialtyperesponse"],
    "studyphase":           ["studyphaseresponse"],
    "trialphase":           ["trialphaseresponse"],
    "studyblindingschema":  ["studyblindingschemaresponse"],
    "trialblindingschema":  ["trialblindingschemaresponse"],
    "studyintenttype":      ["studyintenttyperesponse"],
    "trialintenttype":      ["trialintenttyperesponse"],
    "interventionmodel":    ["interventionmodelresponse"],
    "interventiontype":     ["interventiontyperesponse"],
    "controltype":          ["controltyperesponse"],
    "sex":                  ["sexofparticipantsresponse"],
}


class CTResolver:
    """
    Fetches ALL CT terms once from GET /ct/terms, builds in-memory indexes.
    Identical logic to the notebook cell 6.
    """

    def __init__(self):
        self._by_codelist_concept = {}
        self._by_codelist_name = {}
        self._by_codelist_submission = {}
        self._codelist_uids = {}
        self._codelist_display_names = {}
        self._unit_cache = None
        self._activity_cache = None

        self._alias_map = {}
        for k, aliases in _CODELIST_ALIASES.items():
            for a in aliases:
                self._alias_map.setdefault(k, []).append(a)
                self._alias_map.setdefault(a, []).append(k)

        self._load()

    def _load(self):
        log.info("Fetching ALL CT terms from frontend (may take a moment)...")
        all_terms = api_get_all_pages("ct/terms")
        log.info("Fetched %d CT terms total.", len(all_terms))

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
                if not cl_name:
                    continue

                cl_key = _normalize_codelist_key(cl_name)
                self._codelist_uids.setdefault(cl_key, cl_uid)
                self._codelist_display_names.setdefault(cl_key, cl_name)

                cl_info = {"term_uid": term_uid, "name": sponsor_name, "submission_value": sub_val}

                if concept_id:
                    self._by_codelist_concept.setdefault(cl_key, {})[concept_id] = cl_info
                if sponsor_name:
                    self._by_codelist_name.setdefault(cl_key, {})[sponsor_name.lower().strip()] = cl_info
                if sub_val:
                    self._by_codelist_submission.setdefault(cl_key, {})[sub_val.lower().strip()] = cl_info

        log.info("Indexed %d codelists.", len(self._codelist_uids))
        # Audit log so callers can verify critical codelists were loaded.
        for critical in ("Endpoint Level", "Objective Level", "Criteria Type",
                         "Visit Type", "Visit Contact Mode"):
            key = _normalize_codelist_key(critical)
            present = key in self._codelist_uids
            display = self._codelist_display_names.get(key, "—")
            n = len(self._by_codelist_name.get(key, {}))
            log.info("  CT codelist '%s' loaded=%s (display=%r, %d terms)",
                     critical, present, display, n)

    def _codelist_keys(self, codelist_name, strict=False):
        """Return primary normalized key + aliases.

        When strict=False (default) and the primary key isn't loaded, also
        fuzzy-matches against known codelist names. strict=True disables that
        fallback — required for fields where the wrong codelist returns a
        valid-looking but wrong term (e.g. Endpoint Level vs Objective Level).
        """
        primary = _normalize_codelist_key(codelist_name)
        keys = [primary]

        if not strict and primary not in self._codelist_uids:
            known = list(self._codelist_uids.keys())
            fuzzy = get_close_matches(primary, known, n=1, cutoff=0.75)
            if fuzzy and fuzzy[0] not in keys:
                display = self._codelist_display_names.get(fuzzy[0], fuzzy[0])
                log.info("Codelist '%s' not found exactly; fuzzy matched to '%s'", codelist_name, display)
                keys.append(fuzzy[0])

        for alt in self._alias_map.get(primary, []):
            if alt not in keys:
                keys.append(alt)
        return keys

    def term_is_in_codelist(self, term_uid, codelist_name):
        """Verify ``term_uid`` is registered under EXACTLY ``codelist_name``.

        Only the primary normalized key is checked — aliases are not expanded,
        because they may point at different codelists with overlapping term
        names. We need the strict 'is this term in THIS specific codelist' answer.
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

    def _resolve_by_partial_submission(self, cl_key, text):
        bucket = self._by_codelist_submission.get(cl_key, {})
        if not bucket:
            return None
        search = text.lower().strip()
        best, best_len = None, 0
        for sub_val, info in bucket.items():
            if sub_val in search or search in sub_val:
                if len(sub_val) > best_len:
                    best, best_len = info, len(sub_val)
        if best:
            log.info("  Partial submission match in '%s': '%s' (len=%d)",
                     self._codelist_display_names.get(cl_key, cl_key), text, best_len)
        return best

    def _fuzzy_submission(self, cl_key, text, cutoff=0.6):
        bucket = self._by_codelist_submission.get(cl_key, {})
        if not bucket:
            return None
        matches = get_close_matches(text.lower().strip(), list(bucket.keys()), n=1, cutoff=cutoff)
        if matches:
            log.info("  Fuzzy submission match in '%s': '%s' -> '%s'",
                     self._codelist_display_names.get(cl_key, cl_key), text, matches[0])
            return bucket[matches[0]]
        return None

    def resolve(self, codelist_name, code="", decode="", strict=False):
        primary = _normalize_codelist_key(codelist_name)

        for cl_key in self._codelist_keys(codelist_name, strict=strict):
            cl_display = self._codelist_display_names.get(cl_key, cl_key)

            if code:
                bucket = self._by_codelist_concept.get(cl_key, {})
                if code in bucket:
                    if cl_key != primary:
                        log.info("  Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return bucket[code]

            if decode:
                d = decode.lower().strip()

                bucket = self._by_codelist_name.get(cl_key, {})
                if d in bucket:
                    if cl_key != primary:
                        log.info("  Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return bucket[d]

                bucket2 = self._by_codelist_submission.get(cl_key, {})
                if d in bucket2:
                    if cl_key != primary:
                        log.info("  Resolved via codelist '%s' (asked for '%s')", cl_display, codelist_name)
                    return bucket2[d]

                result = self._resolve_by_partial_submission(cl_key, decode)
                if result:
                    return result

                if bucket:
                    matches = get_close_matches(d, list(bucket.keys()), n=1, cutoff=0.6)
                    if matches:
                        log.info("  Fuzzy name match in '%s': '%s' -> '%s'", cl_display, decode, matches[0])
                        return bucket[matches[0]]

                result = self._fuzzy_submission(cl_key, decode)
                if result:
                    return result

        log.warning("  Codelist '%s' (+ aliases) had no match for code='%s' decode='%s'",
                    codelist_name, code, decode)
        return None

    def resolve_multiple(self, codelist_name, usdm_code_objects, strict=False):
        results = []
        for obj in usdm_code_objects:
            r = self.resolve(codelist_name, code=obj.get("code", ""),
                             decode=obj.get("decode", ""), strict=strict)
            if r:
                results.append(r)
            else:
                log.warning("  Could not resolve code=%s decode='%s' in codelist '%s'",
                            obj.get("code", ""), obj.get("decode", ""), codelist_name)
        return results

    def resolve_unit(self, unit_name):
        if self._unit_cache is None:
            self._unit_cache = {}
            for item in api_get_all_pages("concepts/unit-definitions"):
                n = item.get("name", "")
                if n:
                    self._unit_cache[n.lower().strip()] = {"uid": item.get("uid", ""), "name": n}
            log.info("Cached %d unit definitions.", len(self._unit_cache))
        return self._unit_cache.get(unit_name.lower().strip())

    def list_terms_in_codelist(self, codelist_name):
        """Return all terms in a codelist as [{"term_uid": ..., "name": ...}, ...]."""
        for key in self._codelist_keys(codelist_name):
            bucket = self._by_codelist_name.get(key, {})
            if bucket:
                return list(bucket.values())
        return []

    def resolve_by_submission_value(self, codelist_name, value):
        """Exact submission_value match within one codelist."""
        if not value:
            return None
        target = value.lower().strip()
        for key in self._codelist_keys(codelist_name):
            bucket = self._by_codelist_submission.get(key, {})
            if target in bucket:
                return bucket[target]
        return None

    def resolve_by_submission_global(self, value):
        """
        Search the given submission_value across every indexed codelist.
        Exact match first, then partial/contains.
        Returns {"term_uid": ..., "name": ..., "submission_value": ...} or None.
        """
        if not value:
            return None
        target = value.lower().strip()

        # Exact across all codelists
        for cl_key, bucket in self._by_codelist_submission.items():
            if target in bucket:
                hit = bucket[target]
                log.info("  Submission '%s' resolved in codelist '%s' -> %s",
                         value, self._codelist_display_names.get(cl_key, cl_key), hit["term_uid"])
                return hit

        # Partial/contains across all codelists, longest match wins
        best = None
        best_len = 0
        best_cl = None
        for cl_key, bucket in self._by_codelist_submission.items():
            for sub_val, info in bucket.items():
                if sub_val in target or target in sub_val:
                    if len(sub_val) > best_len:
                        best, best_len, best_cl = info, len(sub_val), cl_key
        if best:
            log.info("  Submission '%s' resolved partially in codelist '%s' -> %s",
                     value, self._codelist_display_names.get(best_cl, best_cl), best["term_uid"])
        return best


ct = None  # Initialized in main()


# ==============================================================================
# CELL 7 - HELPER FUNCTIONS
# ==============================================================================

def _code_obj(d):
    if not d:
        return ("", "")
    return (d.get("code", ""), d.get("decode", ""))


def _alias_code(d):
    if not d:
        return ("", "")
    sc = d.get("standardCode", {})
    return (sc.get("code", ""), sc.get("decode", ""))


def strip_html(html):
    if not html:
        return ""
    if BeautifulSoup:
        return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()
    return re.sub(r"<[^>]+>", "", html).strip()


def _pick_titles_by_position(titles):
    """Pick (study_title, study_short_title) POSITIONALLY from version.titles.

    Rules (type.decode is intentionally ignored):
      - 0 titles  -> ("",          "")
      - 1 title   -> (titles[0],   "")
      - 2+ titles -> (titles[0],   titles[1])    # extras ignored
    Empty/whitespace text values are treated as missing.
    """
    cleaned = [
        (t.get("text") or "").strip()
        for t in (titles or [])
        if isinstance(t, dict)
    ]
    cleaned = [t for t in cleaned if t]
    title = cleaned[0] if len(cleaned) >= 1 else ""
    short = cleaned[1] if len(cleaned) >= 2 else ""
    return title, short


def sanitize_name(text):
    return text.replace("[", "(").replace("]", ")")


def get_next_study_number():
    try:
        active_items = api_get_all_pages("studies")
        deleted_items = api_get_all_pages("studies", extra_params={"deleted": "true"})
        items = active_items + deleted_items
        used_numbers = set()
        for s in items:
            sn = s.get("current_metadata", {}).get("identification_metadata", {}).get("study_number", "")
            digits = "".join(c for c in str(sn) if c.isdigit())
            if digits:
                used_numbers.add(int(digits))
        if not used_numbers:
            log.info("No existing studies found, starting at 0001")
            return "0001"
        max_num = max(used_numbers)
        nxt = None
        for candidate in range(1, max_num + 2):
            if candidate not in used_numbers:
                nxt = candidate
                break
        nxt_str = str(nxt).zfill(4)
        log.info("Next study_number: %s (used: %d numbers, max: %d, active: %d, deleted: %d, gap-filled: %s)",
                 nxt_str, len(used_numbers), max_num, len(active_items), len(deleted_items),
                 nxt < max_num)
        return nxt_str
    except Exception as exc:
        log.warning("Could not detect next study_number: %s - defaulting to 0001", exc)
        return "0001"


# ==============================================================================
# CELL 8 - STUDY CREATION
# ==============================================================================

def create_study(parsed_refs):
    version = parsed_refs["version"]
    identifiers = parsed_refs["identifiers"]
    titles = parsed_refs["titles"]

    project_number = PROJECT_NUMBER_OVERRIDE
    if not project_number:
        project_number = identifiers[0].get("text", "999") if identifiers else "999"
    log.info("Using project_number: %s (from studyIdentifiers[0])", project_number)

    # Pick title positionally (first item in version.titles) — ignore decode.
    title1, _ = _pick_titles_by_position(titles)
    log.info("study_title picked positionally from version.titles[0]: '%s'",
             (title1[:80] + "…") if len(title1) > 80 else title1)

    study_number = get_next_study_number()

    payload = {
        "study_number": study_number,
        "study_acronym": parsed_refs["study_name"],
        "study_subpart_acronym": None,
        "description": title1,
        "study_parent": None,
        "study_parent_part_uid": None,
        "study_description": {"study_title": title1},
        "project_number": "CDISC DEV"
    }

    log.info("Creating study: acronym='%s', number='%s', project='%s'",
             payload["study_acronym"], study_number, project_number)
    log.info("  Payload: %s", json.dumps(payload, indent=2)[:500])

    resp = api_post("studies", json_body=payload)
    if resp.status_code == 201:
        study_uid = resp.json().get("uid")
        log.info("SUCCESS: Study created with UID: %s", study_uid)
        return study_uid
    else:
        log.error("FAILED to create study (%d): %s", resp.status_code, resp.text[:500])
        return None


# ==============================================================================
# CELL 9 - METADATA PATCH
# ==============================================================================

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


def _classify_registry_identifier(ident):
    """Two-pass routing: org-name keywords first, then text prefix/regex,
    finally text-keyword fallback. Prevents 'NCT-NAT-...' from being misrouted
    to ct_gov_id just because it starts with 'NCT'."""
    text = (ident.get("text") or "").strip()
    scope = ident.get("scope") or {}
    org = scope.get("organization") if isinstance(scope, dict) else None
    org_name = (org.get("name") if isinstance(org, dict) else "") or ""
    org_haystack = org_name.lower()

    for field, rule in _REGISTRY_SCOPE_RULES:
        for kw in rule.get("keywords", ()):
            if kw in org_haystack:
                return field

    for field, rule in _REGISTRY_SCOPE_RULES:
        for prefix in rule.get("prefix", ()):
            if text.startswith(prefix):
                return field
        rgx = rule.get("regex")
        if rgx and re.match(rgx, text):
            return field

    text_haystack = text.lower()
    for field, rule in _REGISTRY_SCOPE_RULES:
        for kw in rule.get("keywords", ()):
            if kw in text_haystack:
                return field
    return None


# ── metadata-patch term resolution (strict + cross-codelist validation) ────
#
# Map each OSB metadata field to the EXACT codelist name to resolve against.
# Using the precise "...Response" name + strict=True ensures the resolver
# never falls through to a similarly-named but DIFFERENT codelist
# ("SEND Study Type", "Endpoint Sub Level", …) which would produce an OSB
# 400 "The term identified by uid ... was not found in the codelist ...".
METADATA_FIELD_CODELIST = {
    "study_type_code":             "Study Type Response",
    "trial_phase_code":            "Trial Phase Response",
    "trial_types_codes":           "Trial Type Response",
    "intervention_model_code":     "Intervention Model Response",
    "trial_blinding_schema_code":  "Trial Blinding Schema Response",
    "trial_intent_types_codes":    "Trial Intent Type Response",
    "intervention_type_code":      "Intervention Type Response",
    "control_type_code":           "Control Type Response",
    "sex_of_participants_code":    "Sex of Participants Response",
}


def _resolve_metadata_term(response_codelist, code, decode):
    """Strict resolve + cross-codelist validation for a single metadata field.

    Returns the term dict if the resolved term actually belongs to the exact
    ``response_codelist``; otherwise None (so OSB receives null and doesn't
    400 on a "term not in codelist" error).
    """
    if not code and not decode:
        return None
    if ct is None:
        return None
    resolved = ct.resolve(response_codelist, code=code, decode=decode, strict=True)
    if not resolved:
        log.warning("  metadata: '%s' did not match in '%s' (code=%s decode='%s')",
                    decode or code, response_codelist, code, decode)
        return None
    term_uid = resolved.get("term_uid")
    if not ct.term_is_in_codelist(term_uid, response_codelist):
        log.warning(
            "  metadata: term %s ('%s') is NOT in '%s' codelist — discarding "
            "to prevent OSB 400 (term not found in codelist)",
            term_uid, resolved.get("name"), response_codelist,
        )
        return None
    log.info("  metadata: '%s' resolved in '%s' -> %s (%s)",
             decode or code, response_codelist, resolved.get("name"), term_uid)
    return resolved


def _resolve_metadata_multiple(response_codelist, items):
    """List variant; drops any cross-codelist leaks."""
    out = []
    for item in items or []:
        r = _resolve_metadata_term(response_codelist,
                                   item.get("code", ""), item.get("decode", ""))
        if r:
            out.append(r)
    return out


def build_metadata_patch(parsed_refs):
    version = parsed_refs["version"]
    design = parsed_refs["design"]
    titles = parsed_refs["titles"]
    identifiers = parsed_refs["identifiers"]
    present = parsed_refs["present_sections"]

    metadata = {}

    # Verbose registry_identifiers — 13 fields + *_null_value_code companions.
    registry = {}
    for f in _REGISTRY_ID_FIELDS:
        registry[f] = None
        registry[f"{f}_null_value_code"] = None
    for ident in identifiers:
        field = _classify_registry_identifier(ident)
        if field and not registry.get(field):
            registry[field] = (ident.get("text") or "").strip() or None
    metadata["identification_metadata"] = {"registry_identifiers": registry}

    # Titles picked POSITIONALLY (decode is intentionally ignored).
    title_text, short_text = _pick_titles_by_position(titles)
    log.info("study_description titles (positional): title='%s' short='%s'",
             (title_text[:60] + "…") if len(title_text) > 60 else title_text,
             (short_text[:60] + "…") if len(short_text) > 60 else short_text)
    metadata["study_description"] = {
        "study_title":       title_text or None,
        "study_short_title": short_text or None,
    }

    hlsd = {}
    if "studyType" in present or design.get("studyType"):
        code, decode = _code_obj(design.get("studyType"))
        hlsd["study_type_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["study_type_code"], code, decode)

    if "studyPhase" in present or design.get("studyPhase"):
        code, decode = _alias_code(design.get("studyPhase"))
        hlsd["trial_phase_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["trial_phase_code"], code, decode)

    sub_types = design.get("subTypes", [])
    if sub_types:
        hlsd["trial_types_codes"] = _resolve_metadata_multiple(
            METADATA_FIELD_CODELIST["trial_types_codes"], sub_types) or None

    if hlsd:
        metadata["high_level_study_design"] = hlsd

    pop = {}
    population = design.get("population", {})

    ta_list = design.get("therapeuticAreas", [])
    if ta_list:
        pop["therapeutic_area_codes"] = None

    indications = design.get("indications", [])
    if indications:
        codes = indications[0].get("codes", [])
        if codes:
            sc = codes[0].get("standardCode", codes[0])
            pop["disease_conditions_or_indications_codes"] = [
                {"term_uid": sc.get("code", ""), "name": sc.get("decode", "")}
            ]

    planned_sex = population.get("plannedSex", [])
    if planned_sex and planned_sex[0]:
        code, decode = _code_obj(planned_sex[0])
        pop["sex_of_participants_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["sex_of_participants_code"], code, decode)

    enrollment = population.get("plannedEnrollmentNumber", population.get("plannedEnrollmentNumberQuantity", {}))
    if enrollment and enrollment.get("value") is not None:
        pop["number_of_expected_subjects"] = int(enrollment["value"])

    planned_age = population.get("plannedAge")
    if planned_age:
        unit_info = ct.resolve_unit("years") or {"uid": None, "name": "years"}
        min_v = planned_age.get("minValue", {})
        if min_v and min_v.get("value") is not None:
            pop["planned_minimum_age_of_subjects"] = {"duration_value": min_v["value"], "duration_unit_code": unit_info}
        max_v = planned_age.get("maxValue", {})
        if max_v and max_v.get("value") is not None:
            pop["planned_maximum_age_of_subjects"] = {"duration_value": max_v["value"], "duration_unit_code": unit_info}

    healthy = population.get("includesHealthySubjects")
    if healthy is not None:
        pop["healthy_subject_indicator"] = healthy

    if pop:
        metadata["study_population"] = pop

    interv = {}
    if design.get("model"):
        code, decode = _code_obj(design["model"])
        interv["intervention_model_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["intervention_model_code"], code, decode)

    if design.get("blindingSchema"):
        code, decode = _alias_code(design["blindingSchema"])
        interv["trial_blinding_schema_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["trial_blinding_schema_code"], code, decode)

    intent_types = design.get("intentTypes", [])
    if intent_types:
        interv["trial_intent_types_codes"] = _resolve_metadata_multiple(
            METADATA_FIELD_CODELIST["trial_intent_types_codes"], intent_types) or None

    # intervention_type_code: studyDesign.interventionType wins, else first
    # version.studyInterventions[*].type.
    int_type_obj = design.get("interventionType")
    if int_type_obj:
        if isinstance(int_type_obj, dict) and "standardCode" in int_type_obj:
            code, decode = _alias_code(int_type_obj)
        else:
            code, decode = _code_obj(int_type_obj)
    else:
        code, decode = ("", "")
        for si in version.get("studyInterventions", []) or []:
            code, decode = _code_obj(si.get("type"))
            if code or decode:
                break
    if code or decode:
        interv["intervention_type_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["intervention_type_code"], code, decode)

    # control_type_code: studyDesign.controlType (synthesizer-provided)
    ct_type_obj = design.get("controlType")
    if ct_type_obj:
        if isinstance(ct_type_obj, dict) and "standardCode" in ct_type_obj:
            code, decode = _alias_code(ct_type_obj)
        else:
            code, decode = _code_obj(ct_type_obj)
        interv["control_type_code"] = _resolve_metadata_term(
            METADATA_FIELD_CODELIST["control_type_code"], code, decode)

    if interv:
        metadata["study_intervention"] = interv

    return metadata


def patch_metadata(study_uid, parsed_refs):
    log.info("Building metadata patch...")
    metadata = build_metadata_patch(parsed_refs)
    log.info("Patching study %s with metadata...", study_uid)
    log.info("  Metadata keys: %s", list(metadata.keys()))

    resp = api_patch(f"studies/{study_uid}", json_body={"current_metadata": metadata})
    if resp.status_code == 200:
        log.info("SUCCESS: Metadata patched for study %s", study_uid)
        return True
    else:
        log.error("FAILED to patch metadata (%d): %s", resp.status_code, resp.text[:500])
        return False


# ==============================================================================
# CELL 10 - STUDY ARMS
# ==============================================================================

def upload_study_arms(study_uid, design):
    arms = design.get("arms", [])
    if not arms:
        log.info("No arms to upload.")
        return {}

    log.info("Uploading %d study arms...", len(arms))
    arm_map = {}
    for arm in arms:
        arm_type_code, arm_type_decode = _code_obj(arm.get("type"))
        resolved = ct.resolve("Arm Type", code=arm_type_code, decode=arm_type_decode)

        if not resolved and "treatment" in arm_type_decode.lower():
            log.info("  Arm '%s': 'Treatment' not in Arm Type codelist, trying 'Investigational'",
                     arm.get("name", ""))
            resolved = ct.resolve("Arm Type", decode="Investigational")

        log.info("  Arm '%s': type code=%s decode='%s' -> resolved=%s",
                 arm.get("name", ""), arm_type_code, arm_type_decode, resolved)

        payload = {
            "name": arm.get("name", ""),
            "short_name": arm.get("name", ""),
            "code": arm.get("name", ""),
            "description": arm.get("description", ""),
            "arm_colour": "",
            "randomization_group": arm.get("id", ""),
            "number_of_subjects": 0,
            "arm_type_uid": resolved["term_uid"] if resolved else None,
        }
        resp = api_post(f"studies/{study_uid}/study-arms", json_body=payload)
        if resp.status_code == 201:
            uid = resp.json().get("arm_uid", resp.json().get("uid", ""))
            arm_map[arm.get("name", "")] = uid
            log.info("  SUCCESS: arm '%s' -> %s", arm.get("name", ""), uid)
        else:
            log.error("  FAILED: arm '%s' (%d): %s", arm.get("name", ""), resp.status_code, resp.text[:300])

    log.info("Arms: %d/%d created.", len(arm_map), len(arms))
    return arm_map


# ==============================================================================
# CELL 11 - EPOCHS
# ==============================================================================

def upload_epochs(study_uid, design):
    import pandas as pd

    epochs = design.get("epochs", [])
    elements = design.get("elements", [])
    if not epochs:
        log.info("No epochs to upload.")
        return {}

    # Load mapping CSV - try multiple locations
    csv_path = None
    for candidate in [
        Path(__file__).parent / "epoch_mapping_updated.csv",
        Path(__file__).parent.parent / "epoch_mapping_updated.csv",
        Path("epoch_mapping_updated.csv"),
    ]:
        if candidate.exists():
            csv_path = candidate
            break

    if not csv_path:
        log.error("epoch_mapping_updated.csv not found!")
        return {}

    mapping_df = pd.read_csv(csv_path)
    log.info("Loaded epoch_mapping_updated.csv with %d rows", len(mapping_df))

    mapping_dict = {
        row["GEN_EPOCH_SUB_TYPE"].strip().lower(): row["GEN_EPOCH_TYPE"].replace(" EPOCH TYPE", "")
        for _, row in mapping_df.iterrows()
    }

    ct_cd_to_subtype_text = {}
    name_to_ct_cd = {}
    for _, row in mapping_df.iterrows():
        ct_cd = str(row.get("CT_CD", "")).strip()
        subtype_text = str(row.get("GEN_EPOCH_SUB_TYPE", "")).strip()
        if ct_cd and ct_cd != "nan":
            ct_cd_to_subtype_text[ct_cd] = subtype_text
        if subtype_text:
            name_to_ct_cd[subtype_text.lower()] = ct_cd

    log.info("Uploading %d epochs...", len(epochs))
    log.info("  mapping_dict: %s", mapping_dict)
    log.info("  ct_cd_to_subtype_text: %s", ct_cd_to_subtype_text)
    epoch_map = {}

    for index, epoc in enumerate(sorted(epochs, key=lambda e: int(re.search(r'\d+', e.get("id", "0")).group()))):
        epoch_id = epoc.get("id")
        epoch_order = index + 1
        label = epoc.get("name", "").strip()

        idx = next((i for i, elem in enumerate(elements) if elem.get("id") == epoch_id), None)
        start_rule = elements[idx].get("transitionStartRule", {}).get("text") if idx is not None else None
        end_rule = elements[idx].get("transitionEndRule", {}).get("text") if idx is not None else None

        usdm_code = epoc.get("type", {}).get("code", "")
        log.info("  Epoch '%s': USDM type.code='%s'", label, usdm_code)

        ct_cd = ""
        matched_row = mapping_df[mapping_df["CT_CD"] == usdm_code]
        if not matched_row.empty:
            ct_cd = str(matched_row.iloc[0]["CT_CD"]).strip()
            log.info("    Matched by CT_CD column: '%s' -> CT_CD='%s'", usdm_code, ct_cd)

        if not ct_cd or ct_cd == "nan":
            ct_cd = name_to_ct_cd.get(label.lower(), "")
            if ct_cd:
                log.info("    Matched by name '%s' -> CT_CD='%s'", label, ct_cd)

        epoch_subtype_text = ct_cd_to_subtype_text.get(ct_cd, label)
        epoch_type_name = mapping_dict.get(epoch_subtype_text.lower(), "UNKNOWN")

        log.info("  Epoch '%s': CT_CD='%s', subtype_text='%s', type_name='%s'",
                 label, ct_cd, epoch_subtype_text, epoch_type_name)

        payload = {
            "study_uid": study_uid,
            "epoch": ct_cd or label,
            "epoch_subtype": ct_cd or epoch_subtype_text,
            "epoch_type_name": epoch_type_name,
            "description": epoc.get("description", ""),
            "start_rule": start_rule,
            "end_rule": end_rule,
            "color_hash": "",
            "duration_unit": None,
            "order": epoch_order,
            "duration": 0,
        }
        log.info("  Epoch '%s': payload epoch='%s' epoch_subtype='%s' epoch_type_name='%s'",
                 label, payload["epoch"], payload["epoch_subtype"], payload["epoch_type_name"])
        resp = api_post(f"studies/{study_uid}/study-epochs", json_body=payload)
        if resp.status_code < 400:
            uid = resp.json().get("uid", resp.json().get("study_epoch_uid", ""))
            if uid:
                epoch_map[label] = uid
                epoch_map[epoch_id] = uid
                log.info("  SUCCESS: epoch '%s' -> %s", label, uid)
        else:
            log.error("  FAILED: epoch '%s' (%d): %s", label, resp.status_code, resp.text[:300])

    created = len(set(epoch_map.values())) if epoch_map else 0
    log.info("Epochs: %d/%d created.", created, len(epochs))
    return epoch_map


# ==============================================================================
# CELL 12 - STUDY ELEMENTS
# ==============================================================================

def upload_study_elements(study_uid, design):
    elements = design.get("elements", [])
    if not elements:
        log.info("No elements to upload.")
        return {}

    log.info("Uploading %d study elements...", len(elements))
    elem_map = {}
    for elem in elements:
        subtype = ct.resolve("Element Sub Type", decode=elem.get("name", ""))
        transition = elem.get("transitionEndRule", {})
        start_rule = elem.get("transitionStartRule", {})

        payload = {
            "name": elem.get("name", ""),
            "short_name": elem.get("name", ""),
            "code": elem.get("id", ""),
            "description": elem.get("description", ""),
            "planned_duration": None,
            "start_rule": start_rule.get("text", "") if isinstance(start_rule, dict) else str(start_rule or ""),
            "end_rule": transition.get("text", "") if isinstance(transition, dict) else str(transition or ""),
            "element_colour": "",
            "element_subtype_uid": subtype["term_uid"] if subtype else None,
        }
        resp = api_post(f"studies/{study_uid}/study-elements", json_body=payload)
        if resp.status_code == 201:
            uid = resp.json().get("uid", resp.json().get("element_uid", ""))
            elem_map[elem.get("name", "")] = uid
            log.info("  SUCCESS: element '%s' -> %s", elem.get("name", ""), uid)
        else:
            log.error("  FAILED: element '%s' (%d): %s", elem.get("name", ""), resp.status_code, resp.text[:300])

    log.info("Elements: %d/%d created.", len(elem_map), len(elements))
    return elem_map


# ==============================================================================
# CELL 13 - DESIGN CELLS
# ==============================================================================

def upload_design_cells(study_uid, design, arm_map, epoch_map, element_map):
    study_cells = design.get("studyCells", [])
    if not study_cells:
        log.info("No study cells to upload.")
        return

    log.info("Uploading %d design cells...", len(study_cells))
    ok = 0
    fail = 0

    arm_id_name = {a["id"]: a.get("name", "") for a in design.get("arms", [])}
    epoch_id_name = {e["id"]: e.get("name", "") for e in design.get("epochs", [])}
    elem_id_name = {e["id"]: e.get("name", "") for e in design.get("elements", [])}

    for i, cell in enumerate(study_cells):
        arm_name = arm_id_name.get(cell.get("armId", ""), "")
        epoch_name = epoch_id_name.get(cell.get("epochId", ""), "")
        element_ids = cell.get("elementIds", [])

        arm_uid = arm_map.get(arm_name)
        epoch_uid = epoch_map.get(epoch_name)

        for elem_id in element_ids:
            elem_name = elem_id_name.get(elem_id, "")
            elem_uid = element_map.get(elem_name)

            if arm_uid and epoch_uid and elem_uid:
                payload = {
                    "study_arm_uid": arm_uid,
                    "study_epoch_uid": epoch_uid,
                    "study_element_uid": elem_uid,
                    "transition_rule": "",
                    "order": i + 1,
                }
                resp = api_post(f"studies/{study_uid}/study-design-cells", json_body=payload)
                if resp.status_code == 201:
                    ok += 1
                    log.info("  SUCCESS: cell %s/%s/%s", arm_name, epoch_name, elem_name)
                else:
                    fail += 1
                    log.error("  FAILED: cell %s/%s/%s (%d): %s",
                              arm_name, epoch_name, elem_name, resp.status_code, resp.text[:200])
            else:
                fail += 1
                log.warning("  SKIPPED: cell %s/%s/%s - missing UID(s)", arm_name, epoch_name, elem_name)

    log.info("Design cells: %d succeeded, %d failed.", ok, fail)


# ==============================================================================
# CELL 14 - VISITS
# ==============================================================================

def _parse_iso8601_duration(value):
    if not value:
        return (None, "day")
    m = re.match(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", value)
    if not m:
        return (None, "day")
    years, months, weeks, days, hours, minutes = m.groups()
    if weeks:   return (int(weeks), "week")
    if days:    return (int(days), "day")
    if hours:   return (int(hours), "hour")
    if months:  return (int(months), "month")
    if years:   return (int(years), "year")
    return (None, "day")


# Default OSB contact mode UID for "On Site Visit" — last-resort fallback when
# the dynamic ONSITE submission lookup fails.
_DEFAULT_ONSITE_CONTACT_MODE_UID = "CTTerm_000139"


def _normalize_phrase(s):
    return (s or "").lower().replace("-", " ").replace("_", " ").strip()


def _resolve_visit_type_for_epoch(epoch_name):
    """
    Find a Visit Type term whose sponsor_preferred_name relates to the epoch name.
    See usdm_to_osb.mappers._resolve_visit_type_for_epoch for full docs.
    """
    if not epoch_name or ct is None:
        return None
    hit = ct.resolve("Visit Type", decode=epoch_name)
    if hit:
        return hit
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
        if target_words and common == target_words and len(common) > best_score:
            best, best_score = term, len(common)
            continue
        if best is None and (target in name or name in target):
            best = term
    if best:
        log.info("  Visit type matched for epoch '%s' -> '%s' (%s)",
                 epoch_name, best.get("name"), best.get("term_uid"))
    return best


def _classify_criterion_category(decode):
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


def _resolve_criteria_type_uid(cat_code, cat_decode):
    """Resolve a Criteria Type term_uid via code, decode, then canonical decode."""
    if ct is None:
        return None, "other"
    crit_type = _classify_criterion_category(cat_decode)
    resolved = ct.resolve("Criteria Type", code=cat_code, decode=cat_decode, strict=True)
    if not resolved:
        fallback = {
            "inclusion": "Inclusion Criteria",
            "exclusion": "Exclusion Criteria",
            "withdrawal": "Withdrawal Criteria",
            "run-in": "Run-in Criteria",
        }.get(crit_type)
        if fallback:
            resolved = ct.resolve("Criteria Type", decode=fallback, strict=True)
    if resolved and not ct.term_is_in_codelist(resolved.get("term_uid"), "Criteria Type"):
        log.warning("  Criteria type term %s ('%s') is NOT in Criteria Type codelist — discarding",
                    resolved.get("term_uid"), resolved.get("name"))
        resolved = None
    if resolved:
        log.info("  Criterion category code=%s decode='%s' -> '%s' (%s)",
                 cat_code, cat_decode, resolved.get("name"), resolved.get("term_uid"))
    else:
        log.warning("  Criterion category code=%s decode='%s' (type=%s) NOT RESOLVED in Criteria Type codelist",
                    cat_code, cat_decode, crit_type)
    return (resolved["term_uid"] if resolved else None), crit_type


def _resolve_endpoint_level_uid(decode):
    """
    Resolve endpoint-level term_uid dynamically from the Endpoint Level
    codelist using strict mode (no fuzzy codelist fallback) and a
    post-resolve term-in-codelist validation. Catches the "term valid but in
    wrong codelist" case that triggers OSB 400s.
    """
    if ct is None:
        return None, decode or ""
    decode_lower = (decode or "").lower()
    if "primary" in decode_lower:
        canonical_candidates = ["Primary Outcome Measure", "Primary"]
        label = "Primary"
    elif "exploratory" in decode_lower:
        canonical_candidates = ["Exploratory Outcome Measure", "Exploratory"]
        label = "Exploratory"
    else:
        canonical_candidates = ["Secondary Outcome Measure", "Secondary"]
        label = "Secondary"

    candidates = ([decode] if decode else []) + canonical_candidates
    for cand in candidates:
        resolved = ct.resolve("Endpoint Level", decode=cand, strict=True)
        if not resolved:
            continue
        term_uid = resolved.get("term_uid")
        if not ct.term_is_in_codelist(term_uid, "Endpoint Level"):
            log.warning(
                "    Endpoint level term %s ('%s') is NOT in Endpoint Level codelist — discarding",
                term_uid, resolved.get("name"))
            continue
        log.info("    Endpoint level resolved (strict): decode='%s' -> '%s' (%s)",
                 cand, resolved.get("name"), term_uid)
        return term_uid, label

    log.warning(
        "    Endpoint level NOT RESOLVED for decode='%s' — Endpoint Level "
        "codelist may be missing in this OSB instance, or none of %s match.",
        decode, canonical_candidates)
    return None, label


# ── label/description parser (mirrors mappers.parse_visit_label_text) ───────
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


def _normalize_time_unit(u):
    if not u:
        return None
    u = u.lower().rstrip("s")
    if u in ("min", "minute"):
        return "minute"
    return u


def parse_visit_label_text(text):
    """Parse a slash-delimited visit label/description.

    Returns dict with: epoch_name, visit_number, time_value, time_value_end,
    time_unit, window_lower, window_upper, window_unit, raw_segments.
    See usdm_to_osb.mappers.parse_visit_label_text for full docs.
    """
    out = {
        "epoch_name": None, "visit_number": None,
        "time_value": None, "time_value_end": None, "time_unit": None,
        "window_lower": None, "window_upper": None, "window_unit": None,
        "raw_segments": [],
    }
    if not text:
        return out
    segments = [s.strip() for s in text.split("/") if s.strip()]
    out["raw_segments"] = segments
    if not segments:
        return out
    classified = [False] * len(segments)

    for i, seg in enumerate(segments):
        m = _VISIT_NUM_RE.match(seg)
        if m and out["visit_number"] is None:
            out["visit_number"] = int(m.group(1))
            classified[i] = True
            break

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

    for i, seg in enumerate(segments):
        if classified[i]:
            continue
        if _NA_RE.match(seg):
            classified[i] = True
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

    for i, seg in enumerate(segments):
        if not classified[i] and out["epoch_name"] is None:
            out["epoch_name"] = seg
            classified[i] = True
            break

    return out


def _pick_label_text(instance, encounter):
    """Prefer slash-delimited (more structured) labels; instance over encounter."""
    sources = []
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
    for src in sources:
        for key in ("label", "description"):
            text = (src.get(key) or "").strip()
            if text:
                return text
    return ""


def _resolve_onsite_contact_mode_uid(fallback=_DEFAULT_ONSITE_CONTACT_MODE_UID):
    """
    Look up the contact-mode term whose submission_value is "ONSITE".

    Tries (in order):
      1. exact submission_value "ONSITE" within the Visit Contact Mode codelist
      2. global submission_value search across all codelists (exact + partial)
      3. hardcoded fallback (CTTerm_000139)
    """
    if ct is not None:
        hit = ct.resolve_by_submission_value("Visit Contact Mode", "ONSITE")
        if hit and hit.get("term_uid"):
            log.info("ONSITE contact mode resolved within Visit Contact Mode: %s", hit["term_uid"])
            return hit["term_uid"]
        hit = ct.resolve_by_submission_global("ONSITE")
        if hit and hit.get("term_uid"):
            log.info("ONSITE contact mode resolved via global submission: %s", hit["term_uid"])
            return hit["term_uid"]
    log.info("ONSITE contact mode not found dynamically — using fallback %s", fallback)
    return fallback


def _build_visit_link_index(design):
    """
    Single source of truth for the epoch ↔ encounter ↔ timeline ↔ timing ↔
    instance graph. Walks every scheduleTimeline (main + sub) once.

    Returns a dict — see usdm_to_osb.mappers.build_visit_link_index for keys.
    """
    encounters_by_id = {e["id"]: e for e in design.get("encounters", [])}
    epoch_order = [ep.get("id", "") for ep in design.get("epochs", [])]

    instances_by_id = {}
    timing_by_from_instance = {}
    main_instance_to_encounter = {}
    main_instance_to_epoch = {}
    encounter_to_main_instance = {}
    encounter_to_epoch = {}
    sub_timeline_by_main_instance = {}
    main_timeline_instance_order = []
    global_anchor_instance_id = None

    for tl in design.get("scheduleTimelines", []):
        is_main = bool(tl.get("mainTimeline", False))

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
                    encounter_to_main_instance.setdefault(enc_id, inst_id)
                    encounter_to_epoch.setdefault(enc_id, epoch_id)
                    main_instance_to_encounter[inst_id] = enc_id
                    main_instance_to_epoch[inst_id] = epoch_id
                sub_tl_id = inst.get("timelineId") or None
                if sub_tl_id:
                    sub_timeline_by_main_instance[inst_id] = sub_tl_id

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
        log.info("Global anchor: instance=%s encounter=%s (Fixed Reference)",
                 global_anchor_instance_id, global_anchor_encounter_id)

    # Parse slash-delimited labels/descriptions on each encounter (and its
    # linking instance) so we can fall back to label-derived linkage when
    # the structural fields are missing.
    parsed_label_by_encounter = {}
    for enc_id, enc in encounters_by_id.items():
        link_inst = instances_by_id.get(encounter_to_main_instance.get(enc_id, ""))
        parsed_label_by_encounter[enc_id] = parse_visit_label_text(
            _pick_label_text(link_inst, enc)
        )

    epochs_by_id = {ep["id"]: ep for ep in design.get("epochs", [])}
    epoch_name_to_id = {}
    for ep_id, ep in epochs_by_id.items():
        for key in ("label", "name"):
            n = (ep.get(key) or "").strip().lower()
            if n:
                epoch_name_to_id.setdefault(n, ep_id)

    for enc_id, parsed in parsed_label_by_encounter.items():
        if enc_id in encounter_to_epoch and encounter_to_epoch[enc_id]:
            continue
        ep_name = parsed.get("epoch_name")
        if not ep_name:
            continue
        ep_id = epoch_name_to_id.get(ep_name.strip().lower())
        if not ep_id:
            for known_name, known_id in epoch_name_to_id.items():
                if known_name in ep_name.lower() or ep_name.lower() in known_name:
                    ep_id = known_id
                    break
        if ep_id:
            encounter_to_epoch[enc_id] = ep_id
            log.info("Encounter '%s' linked to epoch '%s' via parsed label epoch_name='%s'",
                     enc_id, ep_id, ep_name)

    epoch_encounters = {eid: [] for eid in epoch_order}
    seen_encs = set()
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

    for enc_id in encounters_by_id:
        if enc_id not in encounter_to_main_instance and enc_id not in encounter_to_epoch:
            log.warning("Encounter '%s' not linked via main-timeline instance OR parsed label", enc_id)

    return {
        "encounters_by_id": encounters_by_id,
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


def upload_visits(study_uid, design, epoch_map):
    idx = _build_visit_link_index(design)
    encounters_by_id = idx["encounters_by_id"]
    if not encounters_by_id:
        log.info("No encounters/visits to upload.")
        return []

    timing_by_from_instance = idx["timing_by_from_instance"]
    encounter_to_main_instance = idx["encounter_to_main_instance"]
    epoch_order = idx["epoch_order"]
    epoch_encounters = idx["epoch_encounters"]
    global_anchor_enc_id = idx["global_anchor_encounter_id"]
    parsed_label_by_encounter = idx["parsed_label_by_encounter"]

    global_anchor_ref = ct.resolve("Time Reference", decode="Global Anchor Visit Reference")
    if not global_anchor_ref:
        global_anchor_ref = ct.resolve_by_submission_global("GLOBAL ANCHOR VISIT REFERENCE")
    global_anchor_ref_uid = global_anchor_ref["term_uid"] if global_anchor_ref else None
    log.info("Global anchor time_reference_uid: %s", global_anchor_ref_uid)

    onsite_fallback_uid = _resolve_onsite_contact_mode_uid()

    log.info("Uploading visits (%d encounters across %d epochs)...",
             len(encounters_by_id), len(epoch_order))

    visit_uids = []
    for epoch_id in epoch_order:
        epoch_uid = epoch_map.get(epoch_id)
        enc_ids = epoch_encounters.get(epoch_id, [])
        if not enc_ids:
            continue

        epoch_name = next((e["name"] for e in design.get("epochs", []) if e["id"] == epoch_id), epoch_id)
        log.info("--- Epoch '%s': %d visits ---", epoch_name, len(enc_ids))

        epoch_obj = next((e for e in design.get("epochs", []) if e["id"] == epoch_id), {})
        epoch_name = epoch_obj.get("label") or epoch_obj.get("name") or ""

        for enc_id in enc_ids:
            enc = encounters_by_id.get(enc_id)
            if not enc:
                continue
            label = enc.get("label", enc.get("name", ""))

            # Visit type: encounter type code/decode -> label -> epoch name fallback
            type_code, type_decode = _code_obj(enc.get("type"))
            visit_type = ct.resolve("Visit Type", code=type_code, decode=type_decode)
            if not visit_type:
                visit_type = ct.resolve("Visit Type", decode=label)
            if not visit_type and epoch_name:
                visit_type = _resolve_visit_type_for_epoch(epoch_name)
            log.info("  Visit '%s' (epoch='%s'): type code=%s decode='%s' -> %s",
                     label, epoch_name, type_code, type_decode, visit_type)

            # Contact mode: prefer per-encounter codelist match, fall back to ONSITE.
            contact_mode_uid = None
            contact_modes = enc.get("contactModes", [])
            if contact_modes:
                cm_code, cm_decode = _code_obj(contact_modes[0])
                cm = ct.resolve("Visit Contact Mode", code=cm_code, decode=cm_decode)
                contact_mode_uid = cm["term_uid"] if cm else None
                log.info("  Visit '%s': contactMode code=%s decode='%s' -> %s", label, cm_code, cm_decode, cm)
            if not contact_mode_uid:
                contact_mode_uid = onsite_fallback_uid
                log.info("  Visit '%s': contactMode fallback -> %s", label, contact_mode_uid)

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
                raw_value = timing.get("value", "")
                parsed_val, parsed_unit = _parse_iso8601_duration(raw_value)
                if parsed_val is not None:
                    timing_type_decode = timing.get("type", {}).get("decode", "")
                    if timing_type_decode.lower() == "before":
                        time_value = -abs(int(parsed_val))
                    else:
                        time_value = int(parsed_val)
                    time_unit = parsed_unit
                    time_from_record = True

                wl = timing.get("windowLower", "")
                wu = timing.get("windowUpper", "")
                if wl:
                    wl_val, wl_unit = _parse_iso8601_duration(wl)
                    if wl_val is not None:
                        min_window = -abs(int(wl_val))
                        window_unit = wl_unit
                        window_from_record = True
                if wu:
                    wu_val, wu_unit = _parse_iso8601_duration(wu)
                    if wu_val is not None:
                        max_window = int(wu_val)
                        window_unit = window_unit or wu_unit
                        window_from_record = True

                log.info("  Visit '%s': timing=%s -> %d %s, window=[%s, %s]",
                         label, raw_value, time_value, time_unit, min_window, max_window)

            # ── Fallback to parsed label/description when timing is missing ──
            if not time_from_record and parsed.get("time_value") is not None:
                time_value = int(parsed["time_value"])
                time_unit = parsed.get("time_unit") or time_unit
                log.info("  Visit '%s': time recovered from label -> %d %s",
                         label, time_value, time_unit)
            if not window_from_record and parsed.get("window_lower") is not None:
                min_window = parsed["window_lower"]
                max_window = parsed["window_upper"]
                window_unit = parsed.get("window_unit") or window_unit
                log.info("  Visit '%s': window recovered from label -> [%s, %s] %s",
                         label, min_window, max_window, window_unit)

            unit_def = ct.resolve_unit(time_unit)
            time_unit_uid = unit_def["uid"] if unit_def else None

            window_unit_uid = None
            if window_unit:
                wu_def = ct.resolve_unit(window_unit)
                window_unit_uid = wu_def["uid"] if wu_def else None

            is_anchor = (enc_id == global_anchor_enc_id)
            if is_anchor:
                log.info("  Visit '%s': IS GLOBAL ANCHOR VISIT", label)

            time_ref_uid = global_anchor_ref_uid

            payload = {
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
            }
            resp = api_post(f"studies/{study_uid}/study-visits", json_body=payload)
            if resp.status_code == 201:
                uid = resp.json().get("uid", resp.json().get("study_visit_uid", ""))
                visit_uids.append(uid)
                anchor_tag = " [GLOBAL ANCHOR]" if is_anchor else ""
                log.info("  SUCCESS: visit '%s' -> %s%s", label, uid, anchor_tag)
            else:
                log.error("  FAILED: visit '%s' (%d): %s", label, resp.status_code, resp.text[:300])

    log.info("Visits: %d/%d created.", len(visit_uids), len(encounters_by_id))
    return visit_uids


# ==============================================================================
# CELL 15 - OBJECTIVES & ENDPOINTS
# ==============================================================================

def upload_objectives_and_endpoints(study_uid, design):
    objectives = design.get("objectives", [])
    if not objectives:
        log.info("No objectives to upload.")
        return []

    log.info("Uploading %d objectives (with endpoints)...", len(objectives))
    results = []

    existing_obj_templates = api_get_all_pages("objective-templates")
    log.info("  Found %d existing objective templates in frontend", len(existing_obj_templates))
    obj_tmpl_by_name = {t.get("name", "").strip().lower(): t for t in existing_obj_templates}

    existing_ep_templates = api_get_all_pages("endpoint-templates")
    log.info("  Found %d existing endpoint templates in frontend", len(existing_ep_templates))
    ep_tmpl_by_name = {t.get("name", "").strip().lower(): t for t in existing_ep_templates}

    for obj_idx, obj in enumerate(objectives):
        obj_text = sanitize_name(obj.get("text", ""))
        if not obj_text.strip():
            continue

        level_decode = obj.get("level", {}).get("decode", "")
        level_code = obj.get("level", {}).get("code", "")
        resolved_level = ct.resolve("Objective Level", code=level_code, decode=level_decode, strict=True)
        if resolved_level and not ct.term_is_in_codelist(resolved_level.get("term_uid"), "Objective Level"):
            log.warning("  Objective level term %s ('%s') is NOT in Objective Level codelist — discarding",
                        resolved_level.get("term_uid"), resolved_level.get("name"))
            resolved_level = None
        level_uid = resolved_level["term_uid"] if resolved_level else None
        level_name = resolved_level["name"] if resolved_level else level_decode
        log.info("  [Obj %d] '%s...' level='%s' -> uid=%s (%s)",
                 obj_idx+1, obj_text[:50], level_decode, level_uid, level_name)

        # Step 1: Get or create OBJECTIVE TEMPLATE
        obj_tmpl_uid = None
        existing_tmpl = obj_tmpl_by_name.get(obj_text.strip().lower())
        if existing_tmpl:
            obj_tmpl_uid = existing_tmpl.get("uid")
            log.info("    REUSING existing objective template '%s' -> %s", obj_text[:50], obj_tmpl_uid)
        else:
            tmpl = {
                "name": obj_text,
                "guidance_text": None,
                "study_uid": study_uid,
                "library_name": "User Defined",
                "indication_uids": None,
                "is_confirmatory_testing": False,
                "category_uids": None,
            }
            r = api_post("objective-templates", json_body=tmpl)
            if r.status_code != 201:
                log.error("    FAILED: objective template creation (%d): %s", r.status_code, r.text[:300])
                results.append({"step": "objective-template", "text": obj_text[:60], "status": "failed", "error": r.text[:200]})
                continue
            obj_tmpl_uid = r.json().get("uid")
            log.info("    CREATED objective template -> %s", obj_tmpl_uid)

            r_approve = api_post(f"objective-templates/{obj_tmpl_uid}/approvals", params={"cascade": "false"})
            if r_approve.status_code >= 400:
                log.warning("    Objective template approval failed (%d): %s", r_approve.status_code, r_approve.text[:200])
            else:
                log.info("    APPROVED objective template %s", obj_tmpl_uid)
            obj_tmpl_by_name[obj_text.strip().lower()] = {"uid": obj_tmpl_uid, "name": obj_text}

        # Step 2: Create STUDY OBJECTIVE
        obj_payload = {
            "objective_level_uid": level_uid,
            "objective_data": {"objective_template_uid": obj_tmpl_uid, "library_name": "User Defined"},
        }
        log.info("    Creating study objective with template_uid=%s, level_uid=%s", obj_tmpl_uid, level_uid)
        r2 = api_post(f"studies/{study_uid}/study-objectives", json_body=obj_payload, params={"create_objective": "true"})
        if r2.status_code >= 400:
            log.error("    FAILED: study objective creation (%d): %s", r2.status_code, r2.text[:300])
            results.append({"step": "study-objective", "text": obj_text[:60], "status": "failed", "error": r2.text[:200]})
            continue

        study_obj_uid = r2.json().get("study_objective_uid") or r2.json().get("uid")
        if not study_obj_uid:
            existing = api_get_all_pages(f"studies/{study_uid}/study-objectives")
            for ex in existing:
                if ex.get("objective", {}).get("name", "") == obj_text:
                    study_obj_uid = ex.get("study_objective_uid")
                    break
        if not study_obj_uid:
            log.error("    Could not find study_objective_uid after creation for '%s'", obj_text[:60])
            continue

        log.info("    SUCCESS: study objective -> %s (template=%s)", study_obj_uid, obj_tmpl_uid)
        results.append({"step": "objective", "text": obj_text[:60], "uid": study_obj_uid, "status": "success"})

        # Step 3: ENDPOINTS
        endpoints = obj.get("endpoints", [])
        log.info("    Processing %d endpoints for objective '%s...'", len(endpoints), obj_text[:40])

        for ep_idx, ep in enumerate(endpoints):
            ep_text = sanitize_name(ep.get("text", ""))
            if not ep_text.strip():
                continue

            ep_level_decode = ep.get("level", {}).get("decode", "")
            ep_level_uid, ep_level_label = _resolve_endpoint_level_uid(ep_level_decode)
            log.info("      [Ep %d] '%s...' level='%s' -> %s (%s)",
                     ep_idx+1, ep_text[:50], ep_level_decode, ep_level_uid, ep_level_label)

            ep_tmpl_uid = None
            existing_ep = ep_tmpl_by_name.get(ep_text.strip().lower())
            if existing_ep:
                ep_tmpl_uid = existing_ep.get("uid")
                log.info("      REUSING existing endpoint template '%s' -> %s", ep_text[:50], ep_tmpl_uid)
            else:
                ep_tmpl = {
                    "name": ep_text,
                    "guidance_text": None,
                    "study_uid": study_uid,
                    "library_name": "User Defined",
                    "indication_uids": None,
                    "category_uids": None,
                    "sub_category_uids": None,
                }
                r3 = api_post("endpoint-templates", json_body=ep_tmpl)
                if r3.status_code != 201:
                    log.error("      FAILED: endpoint template creation (%d): %s", r3.status_code, r3.text[:300])
                    results.append({"step": "endpoint-template", "text": ep_text[:60], "status": "failed", "error": r3.text[:200]})
                    continue
                ep_tmpl_uid = r3.json().get("uid")
                log.info("      CREATED endpoint template -> %s", ep_tmpl_uid)

                r3a = api_post(f"endpoint-templates/{ep_tmpl_uid}/approvals", params={"cascade": "false"})
                if r3a.status_code >= 400:
                    log.warning("      Endpoint template approval failed (%d): %s", r3a.status_code, r3a.text[:200])
                else:
                    log.info("      APPROVED endpoint template %s", ep_tmpl_uid)
                ep_tmpl_by_name[ep_text.strip().lower()] = {"uid": ep_tmpl_uid, "name": ep_text}

            ep_payload = {
                "study_objective_uid": study_obj_uid,
                "endpoint_level_uid": ep_level_uid,
                "endpoint_sublevel_uid": None,
                "endpoint_data": {
                    "parameter_terms": [],
                    "endpoint_template_uid": ep_tmpl_uid,
                    "library_name": "User Defined",
                },
                "endpoint_units": {
                    "units": [],
                    "separator": None,
                },
                "timeframe_uid": None,
            }
            log.info("      Creating study endpoint: obj_uid=%s, ep_tmpl_uid=%s, level=%s",
                     study_obj_uid, ep_tmpl_uid, ep_level_uid)
            r4 = api_post(f"studies/{study_uid}/study-endpoints", json_body=ep_payload, params={"create_endpoint": "true"})
            if r4.status_code >= 400:
                log.error("      FAILED: study endpoint (%d): %s", r4.status_code, r4.text[:300])
                log.error("      Payload was: %s", str(ep_payload)[:400])
                results.append({"step": "study-endpoint", "text": ep_text[:60], "status": "failed",
                                "error": r4.text[:200]})
            else:
                ep_uid = r4.json().get("study_endpoint_uid") or r4.json().get("uid", "")
                log.info("      SUCCESS: study endpoint '%s...' -> %s (level=%s, obj=%s, tmpl=%s)",
                         ep_text[:40], ep_uid, ep_level_label, study_obj_uid, ep_tmpl_uid)
                results.append({"step": "endpoint", "text": ep_text[:60], "uid": ep_uid, "status": "success"})

    ok = sum(1 for r in results if r.get("status") == "success")
    failed = sum(1 for r in results if r.get("status") == "failed")
    log.info("Objectives/endpoints: %d succeeded, %d failed out of %d total steps.", ok, failed, len(results))
    return results


# ==============================================================================
# CELL 16 - ELIGIBILITY CRITERIA
# ==============================================================================

def upload_criteria(study_uid, version, design):
    criteria_items = version.get("eligibilityCriterionItems", [])
    text_map = {c["id"]: c.get("text", "") for c in criteria_items}
    elig_criteria = design.get("eligibilityCriteria", [])
    if not elig_criteria:
        log.info("No eligibility criteria to upload.")
        return []

    log.info("Uploading %d eligibility criteria...", len(elig_criteria))
    results = []

    for crit in elig_criteria:
        cat_decode = crit.get("category", {}).get("decode", "")
        cat_code = crit.get("category", {}).get("code", "")
        type_uid, crit_type = _resolve_criteria_type_uid(cat_code, cat_decode)

        item_id = crit.get("criterionItemId", crit.get("id", ""))
        raw_text = text_map.get(item_id, crit.get("text", ""))
        plain = strip_html(raw_text)
        safe = sanitize_name(plain)
        if not safe.strip():
            continue

        tmpl = {
            "name": safe,
            "guidance_text": None,
            "study_uid": study_uid,
            "library_name": "User Defined",
            "type_uid": type_uid,
            "indication_uids": None,
            "category_uids": None,
            "sub_category_uids": None,
        }
        r = api_post("criteria-templates", json_body=tmpl)
        if r.status_code != 201:
            log.error("  FAILED: criteria template for '%s...' (%d): %s", safe[:40], r.status_code, r.text[:200])
            results.append({"id": item_id, "error": r.text[:200]})
            continue
        tmpl_uid = r.json().get("uid")
        api_post(f"criteria-templates/{tmpl_uid}/approvals", params={"cascade": "false"})

        crit_payload = {
            "criteria_data": {
                "parameter_terms": [],
                "criteria_template_uid": tmpl_uid,
                "library_name": "User Defined",
            }
        }
        r2 = api_post(f"studies/{study_uid}/study-criteria", json_body=crit_payload, params={"create_criteria": "true"})
        if r2.status_code >= 400:
            log.error("  FAILED: study criteria for '%s...' (%d): %s", safe[:40], r2.status_code, r2.text[:200])
            results.append({"id": item_id, "error": r2.text[:200]})
        else:
            log.info("  SUCCESS: criteria '%s...' (%s)", safe[:40], crit_type)
            results.append({"id": item_id, "status": "success"})

    ok = sum(1 for r in results if r.get("status") == "success")
    log.info("Criteria: %d/%d created.", ok, len(results))
    return results


# ==============================================================================
# CELL 17 - ACTIVITIES (full decision-tree)
# ==============================================================================

def _resolve_soa_group_uid():
    resolved = ct.resolve("Flowchart Group", decode="Subject Related Information")
    if resolved:
        log.info("Resolved SoA group: %s (%s)", resolved["name"], resolved["term_uid"])
        return resolved["term_uid"]
    resolved = ct.resolve("Flowchart Group", decode="Procedures")
    if resolved:
        log.warning("SoA group fallback to Procedures: %s", resolved["term_uid"])
        return resolved["term_uid"]
    log.error("Could not resolve any SoA group from Flowchart Group codelist")
    return None


def _search_frontend_activity(name, activity_cache):
    target = name.lower().strip()
    if not target:
        return None
    for item in activity_cache:
        if item.get("name", "").lower().strip() == target:
            log.info("    EXACT MATCH: '%s' -> '%s' (uid=%s)", name, item.get("name"), item.get("uid"))
            return item
    names_lower = [i.get("name", "").lower() for i in activity_cache]
    match = get_close_matches(target, names_lower, n=1, cutoff=0.6)
    if match:
        for item in activity_cache:
            if item.get("name", "").lower() == match[0]:
                log.info("    FUZZY MATCH: '%s' -> '%s' (uid=%s)", name, item.get("name"), item.get("uid"))
                return item
    return None


def _search_frontend_activity_multi(name, activity_cache):
    whole = _search_frontend_activity(name, activity_cache)
    if whole:
        return [whole]
    parts = re.split(r'[,/&]+|\band\b', name, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        return []
    log.info("    Splitting '%s' into %d parts: %s", name, len(parts), parts)
    matches = []
    seen = set()
    for part in parts:
        m = _search_frontend_activity(part, activity_cache)
        if m:
            uid = m.get("uid", "")
            if uid not in seen:
                matches.append(m)
                seen.add(uid)
        else:
            log.info("    NO MATCH for split part: '%s'", part)
    return matches


def _match_synonym(synonyms, activity_cache):
    syn_names = []
    for s in synonyms:
        if isinstance(s, str):
            syn_names.append(s.lower())
        elif isinstance(s, dict):
            syn_names.append(s.get("name", "").lower())
    for item in activity_cache:
        name = item.get("name", "").lower()
        if get_close_matches(name, syn_names, n=1, cutoff=0.6):
            return item
    return None


def _extract_group_subgroup(matched_activity):
    groupings = matched_activity.get("activity_groupings", [])
    if groupings and isinstance(groupings, list) and len(groupings) > 0:
        g = groupings[0]
        if isinstance(g, dict):
            return g.get("activity_group_uid") or "", g.get("activity_subgroup_uid") or ""
    return "", ""


def _get_or_create_group(group_name, group_cache):
    clean = group_name.lower().replace("grouping activity", "").strip()
    target = group_name.upper() if clean.startswith("tbd") else clean
    if target.lower() in group_cache:
        return group_cache[target.lower()]
    try:
        resp = api_get_all_pages("concepts/activities/activity-groups")
        for g in resp:
            if g.get("name", "").lower().strip() == target.lower().strip():
                uid = g.get("uid")
                group_cache[target.lower()] = uid
                log.info("  Found existing group '%s' -> %s", g.get("name"), uid)
                return uid
    except Exception:
        pass
    payload = {
        "name": target,
        "name_sentence_case": clean.lower(),
        "definition": f"Auto-generated group for {clean}",
        "abbreviation": clean[:3].upper(),
        "library_name": "Requested",
    }
    resp = api_post("concepts/activities/activity-groups", json_body=payload)
    if resp.status_code == 201:
        uid = resp.json().get("uid")
        api_post(f"concepts/activities/activity-groups/{uid}/approvals",
                 params={"cascade": "false"})
        group_cache[target.lower()] = uid
        log.info("  CREATED + APPROVED group '%s' -> %s", target, uid)
        return uid
    log.error("  Failed to create group '%s': %s", target, resp.text[:200])
    return ""


def _get_or_create_subgroup(name, group_uid, subgroup_cache):
    clean = name.lower().replace("grouping activity", "").strip()
    target = name.upper() if clean.startswith("tbd") else clean
    if target.lower() in subgroup_cache:
        return subgroup_cache[target.lower()]
    try:
        resp = api_get_all_pages("concepts/activities/activity-sub-groups")
        for sg in resp:
            if sg.get("name", "").lower().strip() == target.lower().strip():
                uid = sg.get("uid")
                subgroup_cache[target.lower()] = uid
                log.info("  Found existing subgroup '%s' -> %s", sg.get("name"), uid)
                return uid
    except Exception:
        pass
    payload = {
        "name": target,
        "name_sentence_case": clean.lower(),
        "definition": f"Auto-generated subgroup for {clean}",
        "abbreviation": clean[:3].upper(),
        "library_name": "Requested",
        "activity_groups": [group_uid],
    }
    resp = api_post("concepts/activities/activity-sub-groups", json_body=payload)
    if resp.status_code == 201:
        uid = resp.json().get("uid")
        api_post(f"concepts/activities/activity-sub-groups/{uid}/approvals",
                 params={"cascade": "false"})
        subgroup_cache[target.lower()] = uid
        log.info("  CREATED + APPROVED subgroup '%s' -> %s", target, uid)
        return uid
    log.error("  Failed to create subgroup '%s': %s", target, resp.text[:200])
    return ""


def _create_activity_in_library(name, label, group_uid, subgroup_uid, study_number):
    log.info("    CREATING new activity '%s' (group=%s, subgroup=%s)", name, group_uid, subgroup_uid)
    payload = {
        "name": name,
        "name_sentence_case": name.lower(),
        "definition": label,
        "abbreviation": None,
        "library_name": "Requested",
        "activity_groupings": [
            {"activity_group_uid": group_uid, "activity_subgroup_uid": subgroup_uid}
        ],
        "synonyms": [],
        "request_rationale": f"Needed for study {study_number}",
        "is_request_final": False,
        "is_data_collected": False,
        "is_multiple_selection_allowed": False,
    }
    resp = api_post("concepts/activities/activities", json_body=payload)
    if resp.status_code == 201:
        uid = resp.json().get("uid")
        log.info("    Created activity '%s' -> %s, now approving...", name, uid)
        approve_resp = api_post(f"concepts/activities/activities/{uid}/approvals",
                               params={"cascade": "false"})
        if approve_resp.status_code < 400:
            log.info("    APPROVED activity '%s' -> %s", name, uid)
        else:
            log.warning("    Created '%s' -> %s but APPROVAL FAILED (%d): %s",
                        name, uid, approve_resp.status_code, approve_resp.text[:200])
        return uid
    log.error("    FAILED to create activity '%s' (%d): %s", name, resp.status_code, resp.text[:200])
    return None


def upload_activities(study_uid, design, usdm_data=None, study_number=""):
    activities = design.get("activities", [])
    if not activities:
        log.info("No activities to upload.")
        return []

    soa_uid = _resolve_soa_group_uid()

    bcs = []
    if usdm_data:
        version = usdm_data.get("study", {}).get("versions", [{}])[0]
        bcs = version.get("biomedicalConcepts", [])

    activity_cache = api_get_all_pages("concepts/activities/activities")
    log.info("Cached %d activities from frontend library.", len(activity_cache))
    sample = [a.get("name", "?") for a in activity_cache[:10]]
    log.info("  Sample activity names: %s%s", sample, "..." if len(activity_cache) > 10 else "")

    existing = api_get_all_pages(f"studies/{study_uid}/study-activities")
    posted_uids = {item.get("activity_uid", "") for item in existing}
    log.info("  Already posted to this study: %d activities", len(posted_uids))

    group_cache = {}
    subgroup_cache = {}
    results = []

    def _refresh_cache():
        nonlocal activity_cache
        activity_cache = api_get_all_pages("concepts/activities/activities")
        log.info("  Refreshed activity cache: %d activities", len(activity_cache))

    def _post_study_activity(group_uid, subgroup_uid, activity_uid, act_name=""):
        if not activity_uid:
            log.error("  Cannot post study-activity for '%s': no activity_uid", act_name)
            results.append({"name": act_name, "error": "no uid", "status": "failed"})
            return False
        if activity_uid in posted_uids:
            log.debug("  Skipping already-posted activity %s ('%s')", activity_uid, act_name)
            results.append({"name": act_name, "uid": activity_uid, "status": "skipped"})
            return True

        found = any(a.get("uid") == activity_uid for a in activity_cache)
        if not found:
            log.warning("  Activity %s not in cache, refreshing...", activity_uid)
            _refresh_cache()
            found = any(a.get("uid") == activity_uid for a in activity_cache)
            if not found:
                log.error("  Activity %s STILL not in frontend after refresh!", activity_uid)

        payload = {
            "soa_group_term_uid": soa_uid,
            "activity_uid": activity_uid,
            "activity_subgroup_uid": subgroup_uid or None,
            "activity_group_uid": group_uid or None,
            "activity_instance_uid": None,
        }
        resp = api_post(f"studies/{study_uid}/study-activities", json_body=payload)
        if resp.status_code == 201:
            posted_uids.add(activity_uid)
            log.info("  POSTED study-activity '%s' (%s)", act_name, activity_uid)
            results.append({"name": act_name, "uid": activity_uid, "status": "success"})
            return True
        else:
            log.error("  FAILED to post study-activity '%s' (%s) [%d]: %s",
                      act_name, activity_uid, resp.status_code, resp.text[:200])
            results.append({"name": act_name, "uid": activity_uid, "status": "failed",
                            "error": resp.text[:200]})
            return False

    def _resolve_and_post_leaf(act, fallback_group):
        act_name = act.get("name") or act.get("label") or act.get("description", "")
        act_label = act.get("label") or act.get("description") or act.get("name", "")
        bc_ids = act.get("biomedicalConceptIds") or []

        log.info("  LEAF: '%s' (id=%s, %d BCs, group='%s')",
                 act_label, act.get("id", "?"), len(bc_ids),
                 fallback_group or "standalone")

        if bc_ids:
            log.info("    -> Path A: has %d biomedicalConceptIds", len(bc_ids))
            _resolve_and_post_all_bcs(bc_ids, bcs, fallback_group, act_label)
            return

        log.info("    -> Path B: name-based resolution for '%s'", act_label)
        matches = _search_frontend_activity_multi(act_label, activity_cache)
        if not matches and act_name != act_label:
            log.info("    Label didn't match, trying name: '%s'", act_name)
            matches = _search_frontend_activity_multi(act_name, activity_cache)

        if matches:
            log.info("    RESOLVED: '%s' -> %d frontend activit%s",
                     act_label, len(matches), "y" if len(matches) == 1 else "ies")
            for matched in matches:
                grp_uid, sgrp_uid = _extract_group_subgroup(matched)
                log.info("    POSTING matched: '%s' (uid=%s, group=%s, subgroup=%s)",
                         matched.get("name"), matched.get("uid"), grp_uid, sgrp_uid)
                _post_study_activity(grp_uid, sgrp_uid, matched["uid"], matched.get("name", ""))
            return

        log.info("    NO MATCH in frontend for '%s' -> CREATING NEW", act_label)

        usdm_grouping = None
        for dg in act.get("definedGroupings", act.get("activityGroupings", [])):
            if isinstance(dg, dict):
                usdm_grouping = dg.get("name") or dg.get("description") or dg.get("label")
                if usdm_grouping:
                    break
            elif isinstance(dg, str):
                usdm_grouping = dg
                break

        if usdm_grouping:
            log.info("    Using USDM grouping: '%s'", usdm_grouping)
            g_uid = _get_or_create_group(usdm_grouping, group_cache)
            sg_uid = _get_or_create_subgroup(usdm_grouping, g_uid, subgroup_cache)
        elif fallback_group:
            log.info("    Using parent group: '%s'", fallback_group)
            g_uid = _get_or_create_group(fallback_group, group_cache)
            sg_uid = _get_or_create_subgroup(fallback_group, g_uid, subgroup_cache)
        else:
            tbd = f"TBD_{study_number}"
            log.info("    Using TBD group: '%s'", tbd)
            g_uid = _get_or_create_group(tbd, group_cache)
            sg_uid = _get_or_create_subgroup(tbd, g_uid, subgroup_cache)

        new_uid = _create_activity_in_library(act_label, act_label, g_uid, sg_uid, study_number)
        if new_uid:
            _refresh_cache()
            _post_study_activity(g_uid, sg_uid, new_uid, act_label)
        else:
            log.error("    Activity creation failed for '%s' - cannot post to study", act_label)

    def _resolve_and_post_all_bcs(bc_ids, all_bcs, fallback_group, parent_label):
        log.info("    Resolving %d biomedicalConcepts for '%s'...", len(bc_ids), parent_label)
        matched_results = []
        unmatched = []
        first_g = first_sg = None

        for bc_id in bc_ids:
            bc = next((b for b in all_bcs if b.get("id") == bc_id), None)
            if not bc:
                log.warning("    BC id '%s' not found in biomedicalConcepts list", bc_id)
                continue
            bc_name = bc.get("name", "")
            log.info("    BC: '%s' (id=%s)", bc_name, bc_id)
            match = None

            synonyms = bc.get("synonyms", [])
            if synonyms:
                match = _match_synonym(synonyms, activity_cache)
            if not match and bc_name:
                match = _search_frontend_activity(bc_name, activity_cache)

            if match:
                matched_results.append((bc_name, match))
                if first_g is None:
                    first_g, first_sg = _extract_group_subgroup(match)
                log.info("      MATCHED: '%s' -> '%s' (uid=%s)",
                         bc_name, match.get("name"), match.get("uid"))
            else:
                unmatched.append((bc_id, bc_name))
                log.info("      NOT FOUND in frontend: '%s'", bc_name)

        log.info("    BC summary: %d matched, %d unmatched out of %d",
                 len(matched_results), len(unmatched), len(bc_ids))
        for bc_name, match in matched_results:
            grp_uid, sgrp_uid = _extract_group_subgroup(match)
            _post_study_activity(grp_uid, sgrp_uid, match["uid"], bc_name)

        if unmatched:
            log.info("    Creating %d unmatched BCs as new activities...", len(unmatched))
            if first_g and first_sg:
                cg, csg = first_g, first_sg
            elif fallback_group:
                cg = _get_or_create_group(fallback_group, group_cache)
                csg = _get_or_create_subgroup(fallback_group, cg, subgroup_cache)
            else:
                tbd = f"TBD_{study_number}"
                cg = _get_or_create_group(tbd, group_cache)
                csg = _get_or_create_subgroup(tbd, cg, subgroup_cache)

            for bc_id, bc_name in unmatched:
                name = bc_name or f"{parent_label}_{bc_id}"
                new_uid = _create_activity_in_library(name, name, cg, csg, study_number)
                if new_uid:
                    _refresh_cache()
                    _post_study_activity(cg, csg, new_uid, name)

    # Main processing
    all_child_ids = set()
    for act in activities:
        for cid in act.get("childIds", []):
            all_child_ids.add(cid)

    log.info("Uploading %d activities (%d children, %d top-level)...",
             len(activities), len(all_child_ids), len(activities) - len(all_child_ids))
    log.info("  biomedicalConcepts available: %d", len(bcs))

    for act in activities:
        act_id = act.get("id", "")
        if act_id in all_child_ids:
            continue

        child_ids = act.get("childIds", [])
        if child_ids:
            group_name = act.get("description") or act.get("name") or act.get("label", "")
            log.info("GROUPING: '%s' (%d children)", group_name, len(child_ids))
            for child_id in child_ids:
                child = next((a for a in activities if a.get("id") == child_id), None)
                if child:
                    _resolve_and_post_leaf(child, fallback_group=group_name)
                else:
                    log.warning("  Child ID %s not found in activities list", child_id)
        else:
            _resolve_and_post_leaf(act, fallback_group=None)

    posted = sum(1 for r in results if r.get("status") == "success")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    failed = sum(1 for r in results if r.get("status") == "failed")
    log.info("Activities: %d posted, %d skipped, %d failed.", posted, skipped, failed)
    return results


# ==============================================================================
# CELL 18 - FULL UPLOAD ORCHESTRATOR (Phase 2)
# ==============================================================================

def run_upload(parsed_refs):
    log.info("=" * 70)
    log.info("PHASE 2: UPLOAD")
    log.info("=" * 70)

    version = parsed_refs["version"]
    design = parsed_refs["design"]
    present = parsed_refs["present_sections"]
    usdm_data = parsed_refs.get("usdm_data")
    summary = {}

    # 1. Create study
    study_uid = create_study(parsed_refs)
    if not study_uid:
        log.error("Study creation failed. Aborting upload.")
        return None
    summary["study_uid"] = study_uid

    study_number = parsed_refs.get("study_number", "0001")

    # 2. Metadata patch
    ok = patch_metadata(study_uid, parsed_refs)
    summary["metadata"] = "success" if ok else "FAILED"

    # 3. Arms
    arm_map = {}
    if "arms" in present:
        arm_map = upload_study_arms(study_uid, design)
        summary["arms"] = f"{len(arm_map)} created"
    else:
        log.info("SKIP: arms (not in USDM)")

    # 4. Epochs
    epoch_map = {}
    if "epochs" in present:
        epoch_map = upload_epochs(study_uid, design)
        summary["epochs"] = f"{len([k for k in epoch_map if not k.startswith('StudyEpoch')])} created"
    else:
        log.info("SKIP: epochs (not in USDM)")

    # 5. Elements
    element_map = {}
    if "elements" in present:
        element_map = upload_study_elements(study_uid, design)
        summary["elements"] = f"{len(element_map)} created"
    else:
        log.info("SKIP: elements (not in USDM)")

    # 6. Design cells
    if "studyCells" in present and arm_map and epoch_map and element_map:
        upload_design_cells(study_uid, design, arm_map, epoch_map, element_map)
        summary["design_cells"] = "done"
    else:
        log.info("SKIP: design cells (dependencies missing)")

    # 7. Visits
    if "encounters (visits)" in present and epoch_map:
        visit_uids = upload_visits(study_uid, design, epoch_map)
        summary["visits"] = f"{len(visit_uids)} created"
    else:
        log.info("SKIP: visits (not in USDM or no epochs)")

    # 8. Objectives & endpoints
    if "objectives" in present:
        obj_results = upload_objectives_and_endpoints(study_uid, design)
        ok = sum(1 for r in obj_results if r.get("status") == "success")
        summary["objectives_endpoints"] = f"{ok}/{len(obj_results)} succeeded"
    else:
        log.info("SKIP: objectives (not in USDM)")

    # 9. Criteria
    if "eligibilityCriteria" in present:
        crit_results = upload_criteria(study_uid, version, design)
        ok = sum(1 for r in crit_results if r.get("status") == "success")
        summary["criteria"] = f"{ok}/{len(crit_results)} created"
    else:
        log.info("SKIP: criteria (not in USDM)")

    # 10. Activities
    if "activities" in present:
        act_results = upload_activities(study_uid, design,
                                        usdm_data=usdm_data,
                                        study_number=study_number)
        ok = sum(1 for r in act_results if r.get("status") == "success")
        summary["activities"] = f"{ok}/{len(act_results)} posted"
    else:
        log.info("SKIP: activities (not in USDM)")

    # Summary
    log.info("")
    log.info("=" * 70)
    log.info("UPLOAD COMPLETE")
    log.info("=" * 70)
    log.info("Study UID: %s", study_uid)
    for section, status in summary.items():
        if section != "study_uid":
            log.info("  %-30s %s", section, status)
    log.info("=" * 70)
    log.info("Full log: %s", LOG_FILE)

    return summary


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================

def _load_config_file(path):
    """Load credentials/settings from a JSON config file."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_credentials(cfg_path=None, no_auth=False):
    """Return (idp_url, api_url, client_id, client_secret, username, password, no_auth).

    Resolution order for each value:
      1. Config file (if --config / cfg_path supplied)
      2. Environment variables (OSB_IDP_URL, OSB_API_URL, OSB_CLIENT_ID,
         OSB_CLIENT_SECRET, OSB_USERNAME, OSB_PASSWORD, OSB_NO_AUTH)
      3. Module-level defaults (non-secret fields only)
      4. Interactive prompt (secret fields that are still blank) — SKIPPED
         when no_auth is True.
    """
    cfg = {}
    if cfg_path:
        cfg = _load_config_file(cfg_path)
        log.info("Loaded config from: %s", cfg_path)

    # no_auth: CLI flag wins, then config file, then env var
    no_auth = bool(no_auth) or bool(cfg.get("no_auth", False)) \
              or os.environ.get("OSB_NO_AUTH", "").lower() in ("1", "true", "yes")

    idp_url     = cfg.get("idp_url")     or os.environ.get("OSB_IDP_URL")     or IDP_URL
    api_url     = cfg.get("api_base_url") or os.environ.get("OSB_API_URL")     or API_BASE_URL
    client_id   = cfg.get("client_id")   or os.environ.get("OSB_CLIENT_ID")   or OAUTH_CLIENT_ID
    secret      = cfg.get("client_secret") or os.environ.get("OSB_CLIENT_SECRET") or OAUTH_CLIENT_SECRET
    username    = cfg.get("username")    or os.environ.get("OSB_USERNAME")    or OAUTH_USERNAME
    password    = cfg.get("password")    or os.environ.get("OSB_PASSWORD")    or OAUTH_PASSWORD

    if no_auth:
        log.info("Credentials resolved with --no-auth (skipping any interactive prompts)")
    else:
        # Prompt for any secrets still missing
        if not username:
            username = input("OSB username (email): ").strip()
        if not secret:
            secret = getpass.getpass("OAuth2 client secret: ")
        if not password:
            password = getpass.getpass("OSB password: ")

    return idp_url, api_url, client_id, secret, username, password, no_auth


def main(usdm_path=None, cfg_path=None, no_auth=False):
    """Main entry point - mirrors the notebook execution flow.

    ``no_auth=True`` (or OSB_NO_AUTH env var, or "no_auth": true in the
    config file) skips OAuth and sends requests without an Authorization
    header — for local OSB instances that don't gate on an IDP.
    """
    global token_mgr, ct, USDM_FILE_PATH, API_BASE_URL

    if usdm_path:
        USDM_FILE_PATH = usdm_path

    idp_url, api_url, client_id, secret, username, password, no_auth = _resolve_credentials(cfg_path, no_auth=no_auth)
    API_BASE_URL = api_url

    # Initialize token manager
    token_mgr = TokenManager(idp_url, client_id, secret, username, password, no_auth=no_auth)

    # Phase 1: Validation
    log.info("Loading USDM file: %s", USDM_FILE_PATH)
    with open(USDM_FILE_PATH, encoding="utf-8") as _f:
        usdm_data = json.load(_f)
    log.info("USDM file loaded successfully.")

    can_proceed, present_sections, missing_sections, parsed = validate_usdm(usdm_data)

    if not can_proceed:
        log.error("")
        log.error("Please update the USDM JSON to include the critical sections listed above,")
        log.error("then re-run this script.")
        log.error("")
        sys.exit(1)

    print("\n" + "=" * 70)
    user_input = input("Validation passed. Proceed with upload? (yes/no): ").strip().lower()
    if user_input not in ("yes", "y"):
        log.info("User chose not to proceed. Exiting.")
        sys.exit(0)
    log.info("User confirmed - starting upload.")

    # Initialize CT resolver
    ct = CTResolver()

    # Phase 2: Upload
    upload_summary = run_upload(parsed)
    return upload_summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="USDM 4.0 -> OpenStudyBuilder Upload")
    parser.add_argument("--usdm", type=str, default=None,
                        help="Path to the USDM JSON file")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to JSON config file with credentials "
                             "(see config_template.json for format)")
    parser.add_argument("--api-url", type=str, default=None, help="OSB API base URL")
    parser.add_argument("--idp-url", type=str, default=None, help="OAuth2 IDP URL")
    parser.add_argument("--client-id", type=str, default=None, help="OAuth2 client ID")
    parser.add_argument("--client-secret", type=str, default=None, help="OAuth2 client secret")
    parser.add_argument("--username", type=str, default=None, help="OSB username (email)")
    parser.add_argument("--password", type=str, default=None, help="OSB password")
    args = parser.parse_args()

    # CLI args override env vars — push them into env so _resolve_credentials picks them up
    if args.api_url:
        os.environ["OSB_API_URL"] = args.api_url
    if args.idp_url:
        os.environ["OSB_IDP_URL"] = args.idp_url
    if args.client_id:
        os.environ["OSB_CLIENT_ID"] = args.client_id
    if args.client_secret:
        os.environ["OSB_CLIENT_SECRET"] = args.client_secret
    if args.username:
        os.environ["OSB_USERNAME"] = args.username
    if args.password:
        os.environ["OSB_PASSWORD"] = args.password

    main(usdm_path=args.usdm, cfg_path=args.config)
