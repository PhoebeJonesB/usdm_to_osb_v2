"""
Upload functions – each handles one section of the OSB study.

Every function calls the OSB API directly via the APIClient,
logs results, and returns structured success/failure info.
"""

import logging
from difflib import get_close_matches
from typing import Any
from bs4 import BeautifulSoup

from .api_client import APIClient
from .ct_resolver import CTResolver

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """Strip HTML tags, return plain text."""
    if not html:
        return ""
    try:
        return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()
    except Exception:
        return html


def _sanitize_template_name(text: str) -> str:
    """Replace brackets that the template API rejects."""
    return text.replace("[", "(").replace("]", ")")


def _resolve_soa_group_uid(ct: CTResolver) -> str | None:
    """
    Resolve the SoA group term_uid dynamically from the "Flowchart Group"
    codelist where submission value contains "subject related information".
    NO hardcoded CTTerm_000067.
    """
    resolved = ct.resolve("Flowchart Group", decode="Subject Related Information")
    if resolved:
        logger.info("Resolved SoA group: %s (%s)", resolved["name"], resolved["term_uid"])
        return resolved["term_uid"]
    # Broader fallback: try partial match
    resolved = ct.resolve("Flowchart Group", decode="Procedures")
    if resolved:
        logger.warning("SoA group 'Subject Related Information' not found, "
                       "fell back to 'Procedures': %s", resolved["term_uid"])
        return resolved["term_uid"]
    logger.error("Could not resolve any SoA group from Flowchart Group codelist")
    return None


# ── study creation ───────────────────────────────────────────────────────────

def create_study(api: APIClient, payload: dict) -> str | None:
    """POST /studies. Returns study_uid or None."""
    resp = api.post("studies", json=payload)
    if resp.status_code == 201:
        uid = resp.json().get("uid")
        logger.info("Created study %s", uid)
        return uid
    logger.error("Failed to create study (%d): %s", resp.status_code, resp.text)
    return None


# ── metadata patch ───────────────────────────────────────────────────────────

def patch_study_metadata(api: APIClient, study_uid: str, metadata: dict) -> bool:
    """PATCH /studies/{uid} with the current_metadata payload."""
    resp = api.patch(f"studies/{study_uid}", json={"current_metadata": metadata})
    if resp.status_code == 200:
        logger.info("Patched study metadata for %s", study_uid)
        return True
    logger.error("Metadata patch failed (%d): %s", resp.status_code, resp.text)
    return False


# ── study arms ───────────────────────────────────────────────────────────────

def post_study_arms(api: APIClient, study_uid: str, arms: list[dict]) -> dict[str, str]:
    """POST each arm to /studies/{uid}/study-arms. Returns {arm_name: arm_uid}."""
    arm_map: dict[str, str] = {}
    for arm in arms:
        resp = api.post(f"studies/{study_uid}/study-arms", json=arm)
        if resp.status_code == 201:
            arm_uid = resp.json().get("arm_uid", resp.json().get("uid", ""))
            arm_map[arm["name"]] = arm_uid
            logger.info("Created arm '%s' -> %s", arm["name"], arm_uid)
        else:
            logger.error("Failed to create arm '%s' (%d): %s",
                         arm["name"], resp.status_code, resp.text)
    return arm_map


# ── epochs ───────────────────────────────────────────────────────────────────

def post_epochs(api: APIClient, study_uid: str, epochs: list[dict]) -> dict[str, str]:
    """POST each epoch. Returns {epoch_name: epoch_uid} and {usdm_epoch_id: epoch_uid}."""
    epoch_map: dict[str, str] = {}
    for epoch in epochs:
        payload = {k: v for k, v in epoch.items() if not k.startswith("_")}
        payload["study_uid"] = study_uid
        resp = api.post(f"studies/{study_uid}/study-epochs", json=payload)
        if resp.status_code == 201:
            uid = resp.json().get("uid", resp.json().get("study_epoch_uid", ""))
            epoch_map[epoch["epoch"]] = uid
            usdm_id = epoch.get("_usdm_epoch_id", "")
            if usdm_id:
                epoch_map[usdm_id] = uid
            logger.info("Created epoch '%s' -> %s", epoch["epoch"], uid)
        else:
            logger.error("Failed to create epoch '%s' (%d): %s",
                         epoch["epoch"], resp.status_code, resp.text)
    return epoch_map


# ── study elements ───────────────────────────────────────────────────────────

def post_study_elements(api: APIClient, study_uid: str, elements: list[dict]) -> dict[str, str]:
    """POST each element. Returns {element_name: element_uid}."""
    elem_map: dict[str, str] = {}
    for elem in elements:
        resp = api.post(f"studies/{study_uid}/study-elements", json=elem)
        if resp.status_code == 201:
            uid = resp.json().get("uid", resp.json().get("element_uid", ""))
            elem_map[elem["name"]] = uid
            logger.info("Created element '%s' -> %s", elem["name"], uid)
        else:
            logger.error("Failed to create element '%s' (%d): %s",
                         elem["name"], resp.status_code, resp.text)
    return elem_map


# ── visits (epoch-grouped) ──────────────────────────────────────────────────

def post_visits(api: APIClient, study_uid: str, visits: list[dict]) -> list[str]:
    """POST each visit. Returns list of visit UIDs."""
    visit_uids = []
    current_epoch = None

    for visit in visits:
        payload = {k: v for k, v in visit.items() if not k.startswith("_")}
        label = visit.get("_label", "")
        epoch_id = visit.get("_epoch_id", "")

        if epoch_id != current_epoch:
            current_epoch = epoch_id
            logger.info("--- Posting visits for epoch '%s' ---", epoch_id)

        resp = api.post(f"studies/{study_uid}/study-visits", json=payload)
        if resp.status_code == 201:
            uid = resp.json().get("uid", resp.json().get("study_visit_uid", ""))
            visit_uids.append(uid)
            anchor_tag = " [GLOBAL ANCHOR]" if visit.get("is_global_anchor_visit") else ""
            logger.info("  Created visit '%s' -> %s%s", label, uid, anchor_tag)
        else:
            logger.error("  Failed to create visit '%s' (%d): %s",
                         label, resp.status_code, resp.text)
    return visit_uids


# ── objectives & endpoints ───────────────────────────────────────────────────

def _resolve_endpoint_level_from_objective(ct: CTResolver, obj_level_name: str) -> str | None:
    """
    Derive endpoint level from objective level:
      - objective is "Primary"   → endpoint level with "Primary Outcome Measure"
      - objective is "Secondary" → endpoint level with "Secondary Outcome Measure"
      - otherwise               → try to resolve directly

    Searches the "Endpoint Level" codelist using partial matching so
    "Primary Outcome Measure" or "Primary Outcome Measures" both match.
    """
    if not obj_level_name:
        return None

    obj_lower = obj_level_name.lower().strip()
    if "primary" in obj_lower:
        search = "Primary Outcome Measure"
    elif "secondary" in obj_lower:
        search = "Secondary Outcome Measure"
    elif "exploratory" in obj_lower:
        search = "Exploratory Outcome Measure"
    else:
        search = obj_level_name

    resolved = ct.resolve("Endpoint Level", decode=search, strict=True)
    if resolved and not ct.term_is_in_codelist(resolved.get("term_uid"), "Endpoint Level"):
        logger.warning("  Endpoint level term %s ('%s') is NOT in Endpoint Level codelist — discarding",
                       resolved.get("term_uid"), resolved.get("name"))
        resolved = None
    if resolved:
        logger.info("  Endpoint level derived from objective '%s' -> '%s' (%s)",
                    obj_level_name, resolved["name"], resolved["term_uid"])
        return resolved["term_uid"]
    return None


def _resolve_endpoint_level_uid(ct: CTResolver, decode: str) -> tuple[str | None, str]:
    """
    Resolve endpoint-level term_uid dynamically from the Endpoint Level
    codelist, using the endpoint's own level.decode (Primary/Secondary/etc).

    Uses ``strict=True`` so a missing 'Endpoint Level' codelist does NOT
    fuzzy-fall-back to Objective Level (which would return a valid-looking
    but wrong term — OSB rejects it with "term not found in codelist").
    Also validates the returned term actually belongs to Endpoint Level.

    Returns (term_uid_or_None, label).
    """
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
            logger.warning(
                "    Endpoint level term %s ('%s') is NOT in Endpoint Level codelist — discarding",
                term_uid, resolved.get("name"))
            continue
        logger.info("    Endpoint level resolved (strict): decode='%s' -> '%s' (%s)",
                    cand, resolved.get("name"), term_uid)
        return term_uid, label

    logger.warning(
        "    Endpoint level NOT RESOLVED for decode='%s' — Endpoint Level "
        "codelist may be missing in this OSB instance, or none of %s match.",
        decode, canonical_candidates)
    return None, label


def post_objectives_and_endpoints(
    api: APIClient, study_uid: str, objectives: list[dict], ct: CTResolver
) -> list[dict]:
    """
    For each objective:
      1. Check if objective template exists in frontend — reuse if found
      2. If not, create + approve objective template
      3. Create study objective using template_uid
      4. For each endpoint:
         a. Check if endpoint template exists in frontend — reuse if found
         b. If not, create + approve endpoint template
         c. Create study endpoint
    """
    results = []

    # Fetch existing templates from frontend ONCE for deduplication
    existing_obj_templates = api.get_all_pages("objective-templates")
    logger.info("  Found %d existing objective templates in frontend", len(existing_obj_templates))
    obj_tmpl_by_name = {t.get("name", "").strip().lower(): t for t in existing_obj_templates}

    existing_ep_templates = api.get_all_pages("endpoint-templates")
    logger.info("  Found %d existing endpoint templates in frontend", len(existing_ep_templates))
    ep_tmpl_by_name = {t.get("name", "").strip().lower(): t for t in existing_ep_templates}

    for obj_idx, obj in enumerate(objectives):
        obj_text = _sanitize_template_name(obj["text"])
        if not obj_text.strip():
            continue

        obj_level_name = obj.get("level_name", "")
        obj_level_uid = obj.get("level_uid")

        logger.info("  [Obj %d] '%s...' level='%s' -> uid=%s",
                     obj_idx + 1, obj_text[:50], obj_level_name, obj_level_uid)

        # ── Step 1: Get or create OBJECTIVE TEMPLATE ──
        obj_tmpl_uid = None
        existing_tmpl = obj_tmpl_by_name.get(obj_text.strip().lower())
        if existing_tmpl:
            obj_tmpl_uid = existing_tmpl.get("uid")
            logger.info("    REUSING existing objective template '%s' -> %s", obj_text[:50], obj_tmpl_uid)
        else:
            tmpl_payload = {
                "name": obj_text,
                "guidance_text": None,
                "study_uid": study_uid,
                "library_name": "User Defined",
                "indication_uids": None,
                "is_confirmatory_testing": False,
                "category_uids": None,
            }
            tmpl_resp = api.post("objective-templates", json=tmpl_payload)
            if tmpl_resp.status_code != 201:
                logger.error("    FAILED: objective template creation (%d): %s",
                             tmpl_resp.status_code, tmpl_resp.text[:300])
                results.append({"step": "objective-template", "text": obj_text[:60],
                                "status": "failed", "error": tmpl_resp.text[:200]})
                continue
            obj_tmpl_uid = tmpl_resp.json().get("uid")
            logger.info("    CREATED objective template -> %s", obj_tmpl_uid)

            # Approve
            approve_resp = api.post(f"objective-templates/{obj_tmpl_uid}/approvals",
                                    params={"cascade": "false"})
            if approve_resp.status_code >= 400:
                logger.warning("    Objective template approval failed (%d): %s",
                               approve_resp.status_code, approve_resp.text[:200])
            else:
                logger.info("    APPROVED objective template %s", obj_tmpl_uid)
            # Add to cache
            obj_tmpl_by_name[obj_text.strip().lower()] = {"uid": obj_tmpl_uid, "name": obj_text}

        # ── Step 2: Create STUDY OBJECTIVE ──
        obj_payload = {
            "objective_level_uid": obj_level_uid,
            "objective_data": {
                "objective_template_uid": obj_tmpl_uid,
                "library_name": "User Defined",
            },
        }
        logger.info("    Creating study objective with template_uid=%s, level_uid=%s",
                     obj_tmpl_uid, obj_level_uid)
        obj_resp = api.post(f"studies/{study_uid}/study-objectives", json=obj_payload,
                            params={"create_objective": "true"})
        if obj_resp.status_code >= 400:
            logger.error("    FAILED: study objective creation (%d): %s",
                         obj_resp.status_code, obj_resp.text[:300])
            results.append({"step": "study-objective", "text": obj_text[:60],
                            "status": "failed", "error": obj_resp.text[:200]})
            continue

        # Find study_objective_uid
        study_obj_uid = obj_resp.json().get("study_objective_uid") or obj_resp.json().get("uid")
        if not study_obj_uid:
            existing = api.get_all_pages(f"studies/{study_uid}/study-objectives")
            for ex in existing:
                if ex.get("objective", {}).get("name", "") == obj_text:
                    study_obj_uid = ex.get("study_objective_uid")
                    break

        if not study_obj_uid:
            logger.error("    Could not find study_objective_uid after creation for '%s'",
                         obj_text[:60])
            results.append({"step": "find-objective", "text": obj_text[:60],
                            "error": "not found after creation"})
            continue

        logger.info("    SUCCESS: study objective -> %s (template=%s)", study_obj_uid, obj_tmpl_uid)
        results.append({"step": "objective", "text": obj_text[:60],
                        "status": "success", "uid": study_obj_uid})

        # ── Step 3: ENDPOINTS for this objective ──
        endpoints = obj.get("endpoints", [])
        logger.info("    Processing %d endpoints for objective '%s...'", len(endpoints), obj_text[:40])

        for ep_idx, ep in enumerate(endpoints):
            # Sanitize: strip newlines, excess whitespace, and brackets
            ep_text = _sanitize_template_name(ep["text"])
            ep_text = " ".join(ep_text.split())  # collapse newlines/tabs/multi-spaces
            if not ep_text.strip():
                continue

            # Resolve endpoint level dynamically from the Endpoint Level codelist
            ep_level_decode = ep.get("level", {}).get("decode", "")
            final_ep_level_uid, ep_level_label = _resolve_endpoint_level_uid(ct, ep_level_decode)

            logger.info("      [Ep %d] '%s...' level='%s' -> %s (%s)",
                         ep_idx + 1, ep_text[:50], ep_level_decode, final_ep_level_uid, ep_level_label)

            # ── Step 3a: Get or create ENDPOINT TEMPLATE (same logic as objectives) ──
            ep_tmpl_uid = None
            existing_ep = ep_tmpl_by_name.get(ep_text.strip().lower())
            if existing_ep:
                ep_tmpl_uid = existing_ep.get("uid")
                logger.info("      REUSING existing endpoint template '%s' -> %s",
                            ep_text[:50], ep_tmpl_uid)
            else:
                ep_tmpl_payload = {
                    "name": ep_text,
                    "guidance_text": None,
                    "study_uid": study_uid,
                    "library_name": "User Defined",
                    "indication_uids": None,
                    "category_uids": None,
                    "sub_category_uids": None,
                }
                ep_tmpl_resp = api.post("endpoint-templates", json=ep_tmpl_payload)
                if ep_tmpl_resp.status_code != 201:
                    logger.error("      FAILED: endpoint template creation (%d): %s",
                                 ep_tmpl_resp.status_code, ep_tmpl_resp.text[:300])
                    results.append({"step": "endpoint-template", "text": ep_text[:60],
                                    "status": "failed", "error": ep_tmpl_resp.text[:200]})
                    continue

                ep_resp_data = ep_tmpl_resp.json()
                ep_tmpl_uid = ep_resp_data.get("uid") or ep_resp_data.get("endpoint_template_uid")
                logger.info("      CREATED endpoint template -> %s (full response keys: %s)",
                            ep_tmpl_uid, list(ep_resp_data.keys()))

                # Approve (same pattern as objective templates)
                approve_resp = api.post(f"endpoint-templates/{ep_tmpl_uid}/approvals",
                                        params={"cascade": "false"})
                if approve_resp.status_code >= 400:
                    logger.warning("      Endpoint template approval failed (%d): %s",
                                   approve_resp.status_code, approve_resp.text[:500])
                    # Re-fetch from API to verify UID and check template status
                    verify_resp = api.get(f"endpoint-templates/{ep_tmpl_uid}")
                    if verify_resp.status_code == 200:
                        verify_data = verify_resp.json()
                        logger.info("      Template status after creation: uid=%s, status=%s, name='%s'",
                                    verify_data.get("uid"), verify_data.get("status"),
                                    verify_data.get("name", "")[:60])
                        # Use the verified UID from re-fetch
                        ep_tmpl_uid = verify_data.get("uid") or ep_tmpl_uid
                else:
                    logger.info("      APPROVED endpoint template %s", ep_tmpl_uid)
                ep_tmpl_by_name[ep_text.strip().lower()] = {"uid": ep_tmpl_uid, "name": ep_text}

            # ── Step 3b: Create STUDY ENDPOINT ──
            ep_payload = {
                "study_objective_uid": study_obj_uid,
                "endpoint_level_uid": final_ep_level_uid,
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
            logger.info("      Creating study endpoint: obj_uid=%s, ep_tmpl_uid=%s, level=%s",
                         study_obj_uid, ep_tmpl_uid, final_ep_level_uid)
            ep_resp = api.post(
                f"studies/{study_uid}/study-endpoints",
                json=ep_payload,
                params={"create_endpoint": "true"},
            )
            if ep_resp.status_code >= 400:
                logger.error("      FAILED: study endpoint (%d): %s",
                             ep_resp.status_code, ep_resp.text[:300])
                logger.error("      Payload was: %s", str(ep_payload)[:400])
                results.append({"step": "study-endpoint", "text": ep_text[:60],
                                "status": "failed", "error": ep_resp.text[:200]})
            else:
                ep_uid = ep_resp.json().get("study_endpoint_uid") or ep_resp.json().get("uid", "")
                logger.info("      SUCCESS: study endpoint '%s...' -> %s (level=%s, obj=%s, tmpl=%s)",
                            ep_text[:40], ep_uid, ep_level_label, study_obj_uid, ep_tmpl_uid)
                results.append({"step": "endpoint", "text": ep_text[:60],
                                "uid": ep_uid, "status": "success"})

    ok = sum(1 for r in results if r.get("status") == "success")
    failed = sum(1 for r in results if r.get("status") == "failed")
    logger.info("Objectives/endpoints: %d succeeded, %d failed out of %d total steps.", ok, failed, len(results))
    return results


# ── criteria ─────────────────────────────────────────────────────────────────

def post_criteria(api: APIClient, study_uid: str, criteria: list[dict]) -> list[dict]:
    """Create + approve criteria templates, then create study criteria."""
    results = []

    for crit in criteria:
        raw_text = crit.get("text", "")
        plain = _strip_html(raw_text)
        safe_name = _sanitize_template_name(plain)
        if not safe_name.strip():
            continue

        tmpl_payload = {
            "name": safe_name,
            "guidance_text": None,
            "study_uid": study_uid,
            "library_name": "User Defined",
            "type_uid": crit.get("type_uid"),
            "indication_uids": None,
            "category_uids": None,
            "sub_category_uids": None,
        }

        tmpl_resp = api.post("criteria-templates", json=tmpl_payload)
        if tmpl_resp.status_code != 201:
            results.append({"step": "criteria-template", "id": crit["id"],
                            "error": tmpl_resp.text})
            continue
        tmpl_uid = tmpl_resp.json().get("uid")

        api.post(f"criteria-templates/{tmpl_uid}/approvals",
                 params={"cascade": "false"})

        crit_payload = {
            "criteria_data": {
                "parameter_terms": [],
                "criteria_template_uid": tmpl_uid,
                "library_name": "User Defined",
            }
        }
        crit_resp = api.post(
            f"studies/{study_uid}/study-criteria",
            json=crit_payload,
            params={"create_criteria": "true"},
        )
        if crit_resp.status_code >= 400:
            results.append({"step": "study-criteria", "id": crit["id"],
                            "error": crit_resp.text})
        else:
            results.append({"step": "criteria", "id": crit["id"],
                            "status": "success"})

    return results


# ── activities (full decision-tree uploader) ─────────────────────────────────

class ActivitiesUploader:
    """
    Matches and posts study activities to OSB, creating groups/subgroups as needed.

    Key behaviours:
      - Searches ALL frontend activities (including TBD groups from prior studies)
      - If matched -> reuse uid + its existing group/subgroup (cross-study reuse)
      - If NOT matched -> CREATE + APPROVE in library, then post to study
      - Uses USDM definedGroupings/activityGroupings if present, else TBD_{study_number}
      - Detailed logging at every step
    """

    def __init__(self, api: APIClient, ct: CTResolver):
        self.api = api
        self.ct = ct
        self._posted_uids: set = set()
        self._activity_cache: list[dict] | None = None
        self._group_cache: dict[str, str] = {}       # {name_lower: uid}
        self._subgroup_cache: dict[str, str] = {}     # {name_lower: uid}
        # Counters
        self.posted_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.created_count = 0

    def upload(self, usdm: dict, study_uid: str, study_number: str) -> dict[str, int]:
        """Upload activities and return stats."""
        self.posted_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        self.created_count = 0
        self._study_uid = study_uid
        self._study_number = study_number

        version = usdm["study"]["versions"][0]
        designs = version.get("studyDesigns", [])
        self._posted_uids = self._get_posted_activity_uids(study_uid)

        # Resolve SoA group dynamically (NO hardcoded CTTerm_000067)
        self._soa_group_uid = _resolve_soa_group_uid(self.ct)

        for design in designs:
            activities = design.get("activities", [])
            bcs = version.get("biomedicalConcepts", [])
            self._process_activities(activities, bcs, study_uid, study_number)

        return {
            "posted": self.posted_count,
            "skipped": self.skipped_count,
            "failed": self.failed_count,
            "created": self.created_count,
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _extract_group_subgroup(matched_activity: dict) -> tuple[str, str]:
        """Safely extract group_uid and subgroup_uid from a matched frontend activity."""
        groupings = matched_activity.get("activity_groupings", [])
        if groupings and isinstance(groupings, list) and len(groupings) > 0:
            g = groupings[0]
            if isinstance(g, dict):
                return (g.get("activity_group_uid") or "",
                        g.get("activity_subgroup_uid") or "")
        return ("", "")

    def _refresh_cache(self):
        """Refresh the activity cache from frontend (after creating new activities)."""
        self._activity_cache = self.api.get_all_pages(
            "concepts/activities/activities")
        logger.info("  Refreshed activity cache: %d activities", len(self._activity_cache))

    # ── Main processing ───────────────────────────────────────────────────

    def _process_activities(self, activities, bcs, study_uid, study_number):
        """Process activities following the decision tree."""
        # Step 1: Identify all child IDs to avoid double-processing
        all_child_ids = set()
        for act in activities:
            for cid in act.get("childIds", []):
                all_child_ids.add(cid)

        logger.info("Uploading %d activities (%d children, %d top-level)...",
                     len(activities), len(all_child_ids),
                     len(activities) - len(all_child_ids))
        logger.info("  biomedicalConcepts available: %d", len(bcs))

        # Step 2: Process only top-level activities
        for act in activities:
            act_id = act.get("id", "")
            if act_id in all_child_ids:
                continue  # Will be handled by its parent grouping

            child_ids = act.get("childIds", [])
            if child_ids:
                # GROUPING activity — process each child
                group_name = (act.get("description") or act.get("name")
                              or act.get("label", ""))
                logger.info("GROUPING: '%s' (%d children)", group_name, len(child_ids))
                self._process_grouping(act, activities, bcs, study_uid,
                                       study_number, group_name)
            else:
                # Standalone LEAF — resolve directly
                self._resolve_and_post_leaf(act, bcs, study_uid, study_number,
                                            fallback_group=None)

    def _process_grouping(self, grouping_act, all_activities, bcs,
                          study_uid, study_number, group_name):
        """Process each child of a grouping activity."""
        for child_id in grouping_act.get("childIds", []):
            child = next(
                (a for a in all_activities if a.get("id") == child_id), None
            )
            if not child:
                logger.warning("  Child ID %s not found in activities list", child_id)
                continue
            self._resolve_and_post_leaf(
                child, bcs, study_uid, study_number,
                fallback_group=group_name,
            )

    def _resolve_and_post_leaf(self, act, bcs, study_uid, study_number,
                               fallback_group: str | None):
        """
        Resolve a single leaf activity and post it.

        Path A: Has BCs → resolve each BC individually
        Path B: No BCs → name-based resolution (splits "Weight, Height" etc.)
        If no match → CREATE + APPROVE using USDM groupings or TBD fallback
        """
        act_name = act.get("name") or act.get("label") or act.get("description", "")
        act_label = act.get("label") or act.get("description") or act.get("name", "")
        bc_ids = act.get("biomedicalConceptIds") or []

        logger.info("  LEAF: '%s' (id=%s, %d BCs, group='%s')",
                    act_label, act.get("id", "?"), len(bc_ids),
                    fallback_group or "standalone")

        # ── Path A: Activity has BCs — resolve each BC individually ───
        if bc_ids:
            logger.info("    -> Path A: has %d biomedicalConceptIds", len(bc_ids))
            self._resolve_and_post_all_bcs(
                bc_ids, bcs, study_uid, study_number, fallback_group, act_label, act
            )
            return

        # ── Path B: No BCs — name-based resolution with splitting ─────
        logger.info("    -> Path B: name-based resolution for '%s'", act_label)

        # Try multi-match (handles "Weight, Height" -> 2 activities)
        matches = self._search_frontend_activity_multi(act_label)
        if not matches and act_name != act_label:
            logger.info("    Label didn't match, trying name: '%s'", act_name)
            matches = self._search_frontend_activity_multi(act_name)

        if matches:
            logger.info("    RESOLVED: '%s' -> %d frontend activit%s",
                        act_label, len(matches), "y" if len(matches) == 1 else "ies")
            for matched in matches:
                grp_uid, sgrp_uid = self._extract_group_subgroup(matched)
                logger.info("    POSTING matched: '%s' (uid=%s, group=%s, subgroup=%s)",
                            matched.get("name"), matched.get("uid"), grp_uid, sgrp_uid)
                self._post_study_activity(
                    study_uid, grp_uid, sgrp_uid, matched["uid"],
                )
            return

        # ── No match: CREATE new activity ──
        logger.info("    NO MATCH in frontend for '%s' -> CREATING NEW", act_label)
        group_uid, subgroup_uid = self._determine_group_subgroup(
            act, fallback_group, study_number)

        new_uid = self._create_activity(act_label, act_label, group_uid,
                                        subgroup_uid, study_number)
        if new_uid:
            self._refresh_cache()
            self._post_study_activity(study_uid, group_uid, subgroup_uid, new_uid)
        else:
            logger.error("    Activity creation failed for '%s' — cannot post to study", act_label)

    def _determine_group_subgroup(self, act, fallback_group, study_number):
        """Determine group/subgroup: check USDM for definedGroupings first, then fallback."""
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
            logger.info("    Using USDM grouping: '%s'", usdm_grouping)
            g_uid = self._get_or_create_group(usdm_grouping)
            sg_uid = self._get_or_create_subgroup(usdm_grouping, g_uid)
        elif fallback_group:
            logger.info("    Using parent group: '%s'", fallback_group)
            g_uid = self._get_or_create_group(fallback_group)
            sg_uid = self._get_or_create_subgroup(fallback_group, g_uid)
        else:
            tbd = f"TBD_{study_number}"
            logger.info("    Using TBD group: '%s'", tbd)
            g_uid = self._get_or_create_group(tbd)
            sg_uid = self._get_or_create_subgroup(tbd, g_uid)

        return g_uid, sg_uid

    def _resolve_and_post_all_bcs(self, bc_ids, bcs, study_uid, study_number,
                                   fallback_group, parent_label, act=None):
        """
        For an activity with multiple BCs, resolve and post EACH BC
        as a separate study activity.

        Pass 1: Match each BC → record matched/unmatched + capture first group
        Pass 2: Create activities for unmatched BCs under first-found group
                 or fallback group / TBD
        """
        logger.info("    Resolving %d biomedicalConcepts for '%s'...",
                    len(bc_ids), parent_label)
        matched_results = []
        unmatched_bcs = []
        first_group_uid = None
        first_subgroup_uid = None

        # ── Pass 1: Match each BC ────────────────────────────────────
        for bc_id in bc_ids:
            bc = next((b for b in bcs if b.get("id") == bc_id), None)
            if not bc:
                logger.warning("    BC id '%s' not found in USDM biomedicalConcepts", bc_id)
                continue

            bc_name = bc.get("name", "")
            logger.info("    BC: '%s' (id=%s) — searching frontend...", bc_name, bc_id)
            match = None

            # Try synonyms first
            synonyms = bc.get("synonyms", [])
            if synonyms:
                syn_display = [s if isinstance(s, str) else s.get("name", "") for s in synonyms]
                logger.info("      Trying %d synonyms: %s", len(synonyms), syn_display[:5])
                match = self._match_synonym(synonyms)

            # Try BC name search
            if not match and bc_name:
                logger.info("      Trying name match: '%s'", bc_name)
                match = self._search_frontend_activity(bc_name)

            if match:
                matched_results.append((bc_name, match))
                if first_group_uid is None:
                    first_group_uid, first_subgroup_uid = self._extract_group_subgroup(match)
                logger.info("      MATCHED: '%s' -> '%s' (uid=%s)",
                            bc_name, match.get("name"), match.get("uid"))
            else:
                unmatched_bcs.append((bc_id, bc_name))
                logger.info("      NOT FOUND in frontend: '%s'", bc_name)

        # ── Post all matched BCs ─────────────────────────────────────
        logger.info("    BC summary: %d matched, %d unmatched out of %d",
                    len(matched_results), len(unmatched_bcs), len(bc_ids))
        for bc_name, match in matched_results:
            grp_uid, sgrp_uid = self._extract_group_subgroup(match)
            logger.info("    POSTING BC: '%s' -> '%s' (uid=%s)",
                        bc_name, match.get("name"), match.get("uid"))
            self._post_study_activity(
                study_uid, grp_uid, sgrp_uid, match["uid"],
            )

        # ── Pass 2: Create activities for unmatched BCs ──────────────
        if unmatched_bcs:
            logger.info("    Creating %d unmatched BCs as new activities...", len(unmatched_bcs))
            if first_group_uid and first_subgroup_uid:
                create_group = first_group_uid
                create_subgroup = first_subgroup_uid
            else:
                # Use USDM groupings or fallback
                create_group, create_subgroup = self._determine_group_subgroup(
                    act or {}, fallback_group, study_number)

            for bc_id, bc_name in unmatched_bcs:
                name = bc_name or f"{parent_label}_{bc_id}"
                new_uid = self._create_activity(
                    name, name, create_group, create_subgroup, study_number)
                if new_uid:
                    self._refresh_cache()
                    self._post_study_activity(
                        study_uid, create_group, create_subgroup, new_uid)

    # ── API helpers ──────────────────────────────────────────────────────

    def _get_all_activities(self) -> list[dict]:
        """Cached fetch of ALL activities from the frontend library.

        Frontend-first contract: every USDM activity is checked against the
        full library (which includes activities created by previous studies
        under their own TBD_<study_number> groups). A new activity is created
        under TBD_<current study_number> ONLY when no library match is found.
        """
        if self._activity_cache is not None:
            return self._activity_cache
        self._activity_cache = self.api.get_all_pages(
            "concepts/activities/activities")
        # Audit log: how many TBD groups exist across all studies vs. our own
        tbd_groups: set[str] = set()
        for a in self._activity_cache:
            for g in a.get("activity_groupings", []) or []:
                gname = (g.get("activity_group_name") or "").strip()
                if gname.upper().startswith("TBD_"):
                    tbd_groups.add(gname)
        logger.info("Cached %d activities from frontend library "
                    "(%d distinct TBD_* groups visible across all studies)",
                    len(self._activity_cache), len(tbd_groups))
        sample = [a.get("name", "?") for a in self._activity_cache[:10]]
        logger.info("  Sample activity names: %s%s", sample,
                     "..." if len(self._activity_cache) > 10 else "")
        return self._activity_cache

    def _search_frontend_activity(self, name: str) -> dict | None:
        """Search frontend: exact match first, then fuzzy (cutoff=0.6)."""
        items = self._get_all_activities()
        target = name.lower().strip()
        if not target:
            return None

        # 1. Exact match
        for item in items:
            if item.get("name", "").lower().strip() == target:
                logger.info("    EXACT MATCH: '%s' -> '%s' (uid=%s)",
                            name, item.get("name"), item.get("uid"))
                return item

        # 2. Fuzzy match
        names_lower = [i.get("name", "").lower() for i in items]
        match = get_close_matches(target, names_lower, n=1, cutoff=0.6)
        if match:
            for item in items:
                if item.get("name", "").lower() == match[0]:
                    logger.info("    FUZZY MATCH: '%s' -> '%s' (uid=%s)",
                                name, item.get("name"), item.get("uid"))
                    return item
        return None

    def _search_frontend_activity_multi(self, name: str) -> list[dict]:
        """Try whole name first, then split on comma/slash/and/& and match each part."""
        whole = self._search_frontend_activity(name)
        if whole:
            return [whole]

        import re
        parts = re.split(r'[,/&]+|\band\b', name, flags=re.IGNORECASE)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            return []

        logger.info("    Splitting '%s' into %d parts: %s", name, len(parts), parts)
        matches = []
        seen = set()
        for part in parts:
            m = self._search_frontend_activity(part)
            if m:
                uid = m.get("uid", "")
                if uid not in seen:
                    matches.append(m)
                    seen.add(uid)
            else:
                logger.info("    NO MATCH for split part: '%s'", part)
        return matches

    def _match_synonym(self, synonyms: list) -> dict | None:
        """Match BC synonyms against frontend activities."""
        items = self._get_all_activities()
        syn_names = []
        for s in synonyms:
            if isinstance(s, str):
                syn_names.append(s.lower())
            elif isinstance(s, dict):
                syn_names.append(s.get("name", "").lower())

        for item in items:
            name = item.get("name", "").lower()
            if get_close_matches(name, syn_names, n=1, cutoff=0.6):
                return item
        return None

    def _get_posted_activity_uids(self, study_uid: str) -> set:
        """Get UIDs already posted to avoid duplicates."""
        try:
            existing = self.api.get_all_pages(
                f"studies/{study_uid}/study-activities")
            uids = {item.get("activity_uid", "") for item in existing
                    if "activity_uid" in item}
            logger.info("  Already posted to this study: %d activities", len(uids))
            return uids
        except Exception:
            return set()

    def _post_study_activity(self, study_uid, group_uid, subgroup_uid,
                             activity_uid):
        """POST a single study activity (skip if already posted)."""
        if not activity_uid:
            self.failed_count += 1
            logger.error("  Cannot post study-activity: no activity_uid")
            return

        if activity_uid in self._posted_uids:
            self.skipped_count += 1
            logger.debug("Skipping already-posted activity %s", activity_uid)
            return

        # Verify exists in frontend cache
        items = self._get_all_activities()
        found = any(a.get("uid") == activity_uid for a in items)
        if not found:
            logger.warning("  Activity %s not in cache, refreshing...", activity_uid)
            self._refresh_cache()
            items = self._get_all_activities()
            found = any(a.get("uid") == activity_uid for a in items)
            if not found:
                logger.error("  Activity %s STILL not in frontend after refresh!", activity_uid)

        payload = {
            "soa_group_term_uid": self._soa_group_uid,
            "activity_uid": activity_uid,
            "activity_subgroup_uid": subgroup_uid or None,
            "activity_group_uid": group_uid or None,
            "activity_instance_uid": None,
        }

        resp = self.api.post(
            f"studies/{study_uid}/study-activities", json=payload)
        if resp.status_code == 201:
            self._posted_uids.add(activity_uid)
            self.posted_count += 1
            logger.info("  POSTED study-activity %s", activity_uid)
        else:
            self.failed_count += 1
            logger.error("  FAILED to post study-activity %s (%d): %s",
                         activity_uid, resp.status_code, resp.text[:200])

    def _get_or_create_group(self, group_name: str) -> str:
        """Find or create an activity group, approve it."""
        clean = group_name.lower().replace("grouping activity", "").strip()
        target = group_name.upper() if clean.startswith("tbd") else clean

        # Check cache
        if target.lower() in self._group_cache:
            return self._group_cache[target.lower()]

        # Check existing
        try:
            groups = self.api.get_all_pages(
                "concepts/activities/activity-groups")
            for g in groups:
                if g.get("name", "").lower().strip() == target.lower().strip():
                    uid = g.get("uid")
                    self._group_cache[target.lower()] = uid
                    logger.info("  Found existing group '%s' -> %s", g.get("name"), uid)
                    return uid
        except Exception:
            pass

        # Create + approve
        payload = {
            "name": target,
            "name_sentence_case": clean.lower(),
            "definition": f"Auto-generated group for {clean}",
            "abbreviation": clean[:3].upper(),
            "library_name": "Requested",
        }
        resp = self.api.post("concepts/activities/activity-groups", json=payload)
        if resp.status_code == 201:
            uid = resp.json().get("uid")
            self.api.post(
                f"concepts/activities/activity-groups/{uid}/approvals",
                params={"cascade": "false"})
            self._group_cache[target.lower()] = uid
            logger.info("  CREATED + APPROVED group '%s' -> %s", target, uid)
            return uid
        logger.error("  Failed to create group '%s': %s", target, resp.text[:200])
        return ""

    def _get_or_create_subgroup(self, name: str, group_uid: str) -> str:
        """Find or create an activity subgroup, approve it."""
        clean = name.lower().replace("grouping activity", "").strip()
        target = name.upper() if clean.startswith("tbd") else clean

        if target.lower() in self._subgroup_cache:
            return self._subgroup_cache[target.lower()]

        try:
            subgroups = self.api.get_all_pages(
                "concepts/activities/activity-sub-groups")
            for sg in subgroups:
                if sg.get("name", "").lower().strip() == target.lower().strip():
                    uid = sg.get("uid")
                    self._subgroup_cache[target.lower()] = uid
                    logger.info("  Found existing subgroup '%s' -> %s", sg.get("name"), uid)
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
        resp = self.api.post(
            "concepts/activities/activity-sub-groups", json=payload)
        if resp.status_code == 201:
            uid = resp.json().get("uid")
            self.api.post(
                f"concepts/activities/activity-sub-groups/{uid}/approvals",
                params={"cascade": "false"})
            self._subgroup_cache[target.lower()] = uid
            logger.info("  CREATED + APPROVED subgroup '%s' -> %s", target, uid)
            return uid
        logger.error("  Failed to create subgroup '%s': %s", target, resp.text[:200])
        return ""

    def _create_activity(self, name, label, group_uid, subgroup_uid,
                         study_number) -> str | None:
        """Create a new activity in the library, APPROVE it, return uid."""
        logger.info("    CREATING new activity '%s' (group=%s, subgroup=%s)",
                     name, group_uid, subgroup_uid)
        payload = {
            "name": name,
            "name_sentence_case": name.lower(),
            "definition": label,
            "abbreviation": None,
            "library_name": "Requested",
            "activity_groupings": [
                {"activity_group_uid": group_uid,
                 "activity_subgroup_uid": subgroup_uid}
            ],
            "synonyms": [],
            "request_rationale": f"Needed for study {study_number}",
            "is_request_final": False,
            "is_data_collected": False,
            "is_multiple_selection_allowed": False,
        }
        resp = self.api.post("concepts/activities/activities", json=payload)
        if resp.status_code == 201:
            uid = resp.json().get("uid")
            logger.info("    Created activity '%s' -> %s, now approving...", name, uid)
            # Approve so it is visible in frontend and linkable to study
            approve_resp = self.api.post(
                f"concepts/activities/activities/{uid}/approvals",
                params={"cascade": "false"})
            if approve_resp.status_code < 400:
                logger.info("    APPROVED activity '%s' -> %s", name, uid)
            else:
                logger.warning("    Created '%s' -> %s but APPROVAL FAILED (%d): %s",
                               name, uid, approve_resp.status_code, approve_resp.text[:200])
            self.created_count += 1
            return uid
        logger.error("    FAILED to create activity '%s' (%d): %s",
                     name, resp.status_code, resp.text[:200])
        self.failed_count += 1
        return None


# ── design cells ─────────────────────────────────────────────────────────────

def post_design_cells(
    api: APIClient,
    study_uid: str,
    design: dict,
    arm_map: dict[str, str],
    epoch_map: dict[str, str],
    element_map: dict[str, str],
) -> list[dict]:
    """Create study design cells linking arms x epochs x elements."""
    results = []
    study_cells = design.get("studyCells", [])

    for i, cell in enumerate(study_cells):
        arm_id = cell.get("armId", "")
        epoch_id = cell.get("epochId", "")
        element_ids = cell.get("elementIds", [])

        arm_name = ""
        for arm in design.get("arms", []):
            if arm.get("id") == arm_id:
                arm_name = arm.get("name", "")
                break
        arm_uid = arm_map.get(arm_name)

        epoch_name = ""
        for epoch in design.get("epochs", []):
            if epoch.get("id") == epoch_id:
                epoch_name = epoch.get("name", "")
                break
        epoch_uid = epoch_map.get(epoch_name) or epoch_map.get(epoch_id)

        for elem_id in element_ids:
            elem_name = ""
            for elem in design.get("elements", []):
                if elem.get("id") == elem_id:
                    elem_name = elem.get("name", "")
                    break
            elem_uid = element_map.get(elem_name)

            if arm_uid and epoch_uid and elem_uid:
                payload = {
                    "study_arm_uid": arm_uid,
                    "study_epoch_uid": epoch_uid,
                    "study_element_uid": elem_uid,
                    "transition_rule": "",
                    "order": i + 1,
                }
                resp = api.post(f"studies/{study_uid}/study-design-cells",
                                json=payload)
                if resp.status_code == 201:
                    results.append({
                        "cell": f"{arm_name}/{epoch_name}/{elem_name}",
                        "status": "success"})
                else:
                    results.append({
                        "cell": f"{arm_name}/{epoch_name}/{elem_name}",
                        "error": resp.text})

    return results
