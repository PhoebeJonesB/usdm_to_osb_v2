#!/usr/bin/env python3
"""
USDM 4.0 -> OpenStudyBuilder upload script.

Two-phase workflow:
  Step 1:  python -m usdm_to_osb validate  study.json
  Step 2:  python -m usdm_to_osb upload    study.json --config config.json

Step 1 checks the USDM JSON has every section the upload needs.
Step 2 runs only after Step 1 passes — it authenticates, fetches CT terms
dynamically, creates the study, and uploads all entities.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, TokenManager
from .api_client import APIClient
from .ct_resolver import CTResolver
from .validation import validate_usdm
from . import mappers
from . import uploaders

logger = logging.getLogger("usdm_to_osb")


def setup_logging(log_dir: str = ".") -> str:
    log_file = Path(log_dir) / f"usdm_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-18s | %(levelname)-8s | %(message)s",
        handlers=[
            logging.FileHandler(str(log_file), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return str(log_file)


def get_next_study_number(api: APIClient) -> str:
    """Query existing studies (including deleted) and return the next available number, gap-filling."""
    try:
        active_items = api.get_all_pages("studies")
        deleted_items = api.get_all_pages("studies", extra_params={"deleted": "true"})
        items = active_items + deleted_items
        used_numbers = set()
        for study in items:
            sn = (
                study.get("current_metadata", {})
                .get("identification_metadata", {})
                .get("study_number", "")
            )
            digits = "".join(c for c in str(sn) if c.isdigit())
            if digits:
                used_numbers.add(int(digits))
        if not used_numbers:
            logger.info("No existing studies found, starting at 0001")
            return "0001"
        # Find first gap: start from 1 (or min+1) and walk forward
        max_num = max(used_numbers)
        next_num = None
        for candidate in range(1, max_num + 2):
            if candidate not in used_numbers:
                next_num = candidate
                break
        next_str = str(next_num).zfill(4)
        logger.info("Next study_number: %s (used: %d numbers, max: %d, active: %d, deleted: %d, gap-filled: %s)",
                     next_str, len(used_numbers), max_num, len(active_items), len(deleted_items),
                     next_num < max_num)
        return next_str
    except Exception as exc:
        logger.warning("Could not determine next study number: %s — defaulting to 0001", exc)
        return "0001"


def _build_config(args) -> Config:
    """Build Config from --config file or CLI args.

    Honors ``--no-auth`` (and config file's ``"no_auth": true``) to skip
    OAuth entirely. In that mode credentials are not required and OSB
    requests go out without an Authorization header.
    """
    no_auth_cli = bool(getattr(args, "no_auth", False))

    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        no_auth = bool(cfg.get("no_auth", False)) or no_auth_cli
        return Config(
            api_base_url=cfg.get("api_base_url", args.api_url),
            idp_url=cfg.get("idp_url", args.idp_url) or "",
            client_id=cfg.get("client_id", args.client_id) or "",
            client_secret=cfg.get("client_secret", "") or "",
            username=cfg.get("username", "") or "",
            password=cfg.get("password", "") or "",
            no_auth=no_auth,
        )

    if not no_auth_cli and (not args.client_secret or not args.username or not args.password):
        print("ERROR: --client-secret, --username, and --password are required "
              "(or use --config, or --no-auth for an unauthenticated OSB instance)")
        sys.exit(1)

    return Config(
        api_base_url=args.api_url,
        idp_url=args.idp_url or "",
        client_id=args.client_id or "",
        client_secret=args.client_secret or "",
        username=args.username or "",
        password=args.password or "",
        no_auth=no_auth_cli,
    )


# ── upload pipeline ──────────────────────────────────────────────────────────

def run_upload(config: Config, usdm_path: str, skip_sections: set[str] | None = None):
    """Full upload pipeline — only call after validation passes."""
    skip = skip_sections or set()

    # ── re-validate inline (safety check) ────────────────────────────────
    result = validate_usdm(usdm_path)
    if not result["can_proceed"]:
        logger.error("Validation failed. Run 'validate' first and fix the issues.")
        return None

    # ── setup ────────────────────────────────────────────────────────────
    token_mgr = TokenManager(config)
    api = APIClient(config, token_mgr)

    logger.info("Loading CT terms from frontend (dynamic codelist resolution)...")
    ct = CTResolver(api)
    codelists = ct.list_codelists()
    logger.info("Discovered %d codelists", len(codelists))

    # ── load USDM ────────────────────────────────────────────────────────
    with open(usdm_path, encoding="utf-8") as f:
        usdm = json.load(f)

    version = usdm["study"]["versions"][0]
    design = version["studyDesigns"][0]
    present = set(result["present"])
    results = {"study_uid": None, "sections": {}}

    # ── 1. create study ──────────────────────────────────────────────────
    study_number = get_next_study_number(api)
    creation_payload = mappers.map_study_creation(usdm, study_number)
    logger.info("Creating study '%s' (number=%s, project=%s)...",
                creation_payload["study_acronym"], study_number, creation_payload["project_number"])
    study_uid = uploaders.create_study(api, creation_payload)
    if not study_uid:
        logger.error("Study creation failed, aborting.")
        return results
    results["study_uid"] = study_uid
    logger.info("Study created: %s", study_uid)

    # ── 2. patch metadata ────────────────────────────────────────────────
    if "metadata" not in skip:
        logger.info("Mapping and patching study metadata...")
        metadata = {
            "identification_metadata": mappers.map_identification_metadata(version),
            "study_description": mappers.map_study_description(version),
            "high_level_study_design": mappers.map_high_level_design(design, ct),
            "study_population": mappers.map_study_population(design, ct),
            "study_intervention": mappers.map_study_intervention(design, ct, version),
        }
        ok = uploaders.patch_study_metadata(api, study_uid, metadata)
        results["sections"]["metadata"] = "success" if ok else "FAILED"

    # ── 3. study arms ────────────────────────────────────────────────────
    arm_map: dict[str, str] = {}
    if "arms" not in skip and "arms" in present:
        logger.info("Posting study arms...")
        arm_payloads = mappers.map_study_arms(design, ct)
        arm_map = uploaders.post_study_arms(api, study_uid, arm_payloads)
        results["sections"]["arms"] = f"{len(arm_map)}/{len(arm_payloads)} created"
    elif "arms" in skip:
        logger.info("SKIP: arms (--skip)")
    else:
        logger.info("SKIP: arms (not in USDM)")

    # ── 4. epochs ────────────────────────────────────────────────────────
    epoch_map: dict[str, str] = {}
    if "epochs" not in skip and "epochs" in present:
        logger.info("Posting epochs...")
        epoch_payloads = mappers.map_epochs(design, ct, api=api)
        epoch_map = uploaders.post_epochs(api, study_uid, epoch_payloads)
        results["sections"]["epochs"] = f"{len(epoch_map)}/{len(epoch_payloads)} created"
    elif "epochs" in skip:
        logger.info("SKIP: epochs (--skip)")
    else:
        logger.info("SKIP: epochs (not in USDM)")

    # ── 5. study elements ────────────────────────────────────────────────
    element_map: dict[str, str] = {}
    if "elements" not in skip and "elements" in present:
        logger.info("Posting study elements...")
        element_payloads = mappers.map_study_elements(design, ct)
        element_map = uploaders.post_study_elements(api, study_uid, element_payloads)
        results["sections"]["elements"] = f"{len(element_map)}/{len(element_payloads)} created"
    elif "elements" in skip:
        logger.info("SKIP: elements (--skip)")
    else:
        logger.info("SKIP: elements (not in USDM)")

    # ── 6. design cells ──────────────────────────────────────────────────
    if "cells" not in skip and arm_map and epoch_map and element_map:
        logger.info("Posting design cells...")
        cell_results = uploaders.post_design_cells(api, study_uid, design, arm_map, epoch_map, element_map)
        ok_count = sum(1 for r in cell_results if r.get("status") == "success")
        results["sections"]["design_cells"] = f"{ok_count}/{len(cell_results)} created"
    else:
        logger.info("SKIP: design cells (dependencies not met or --skip)")

    # ── 7. visits (linked to epochs via scheduleTimeline instances) ─────
    if "visits" not in skip and "encounters (visits)" in present and epoch_map:
        logger.info("Posting visits (grouped by epoch, with global anchor)...")
        # epoch_map now contains both {name: uid} and {usdm_id: uid}
        visit_payloads = mappers.map_visits_grouped_by_epoch(design, ct, epoch_map)
        visit_uids = uploaders.post_visits(api, study_uid, visit_payloads)
        results["sections"]["visits"] = f"{len(visit_uids)}/{len(visit_payloads)} created"
    else:
        logger.info("SKIP: visits (not in USDM, no epochs, or --skip)")

    # ── 8. objectives & endpoints ────────────────────────────────────────
    if "objectives" not in skip and "objectives" in present:
        logger.info("Posting objectives and endpoints...")
        obj_data = mappers.map_objectives(design, ct)
        obj_results = uploaders.post_objectives_and_endpoints(api, study_uid, obj_data, ct)
        ok_count = sum(1 for r in obj_results if r.get("status") == "success")
        results["sections"]["objectives_endpoints"] = f"{ok_count}/{len(obj_results)} steps succeeded"
    else:
        logger.info("SKIP: objectives (not in USDM or --skip)")

    # ── 9. criteria ──────────────────────────────────────────────────────
    if "criteria" not in skip and "eligibilityCriteria" in present:
        logger.info("Posting eligibility criteria...")
        criteria_data = mappers.map_criteria(version, design, ct)
        crit_results = uploaders.post_criteria(api, study_uid, criteria_data)
        ok_count = sum(1 for r in crit_results if r.get("status") == "success")
        results["sections"]["criteria"] = f"{ok_count}/{len(crit_results)} created"
    else:
        logger.info("SKIP: criteria (not in USDM or --skip)")

    # ── 10. activities (full decision-tree: childIds, BCs, group creation) ─
    if "activities" not in skip and "activities" in present:
        logger.info("Matching and posting activities (childIds + BCs + group creation)...")
        act_uploader = uploaders.ActivitiesUploader(api, ct)
        act_stats = act_uploader.upload(usdm, study_uid, study_number)
        results["sections"]["activities"] = (
            f"{act_stats['posted']} posted, {act_stats['created']} created, "
            f"{act_stats['skipped']} skipped, {act_stats['failed']} failed"
        )
    else:
        logger.info("SKIP: activities (not in USDM or --skip)")

    # ── summary ──────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 70)
    logger.info("UPLOAD COMPLETE")
    logger.info("=" * 70)
    logger.info("Study UID: %s", study_uid)
    for section, status in results["sections"].items():
        logger.info("  %-30s %s", section, status)
    logger.info("=" * 70)
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

def _add_common_args(p):
    """Add args shared by validate and upload."""
    p.add_argument("usdm_file", help="Path to USDM 4.0 JSON file")
    p.add_argument("--log-dir", default=".", help="Directory for log files")


def main():
    parser = argparse.ArgumentParser(
        prog="usdm_to_osb",
        description="USDM 4.0 -> OpenStudyBuilder uploader (two-phase: validate, then upload)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Workflow:
  Step 1 — validate your JSON:
    python -m usdm_to_osb validate  CDISC_Pilot_Study.json

  Step 2 — upload (only after validation passes):
    python -m usdm_to_osb upload  CDISC_Pilot_Study.json --config config.json

  List all codelists in your OSB instance:
    python -m usdm_to_osb list-codelists --config config.json
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── validate ─────────────────────────────────────────────────────────
    p_val = sub.add_parser("validate", help="Check USDM JSON has all required sections (no API needed)")
    _add_common_args(p_val)

    # ── upload ───────────────────────────────────────────────────────────
    p_up = sub.add_parser("upload", help="Upload USDM JSON to OpenStudyBuilder (runs validation first)")
    _add_common_args(p_up)
    p_up.add_argument("--api-url", default=None, help="OSB API base URL")
    p_up.add_argument("--idp-url", default=None, help="OAuth2 IDP URL")
    p_up.add_argument("--client-id", default="osbidp", help="OAuth2 client ID")
    p_up.add_argument("--client-secret", help="OAuth2 client secret")
    p_up.add_argument("--username", help="OSB username")
    p_up.add_argument("--password", help="OSB password")
    p_up.add_argument("--config", help="Path to JSON config file (overrides CLI args)")
    p_up.add_argument("--no-auth", action="store_true",
                      help="Skip OAuth — talk to an OSB instance with no IDP gating "
                           "(credentials become optional)")
    p_up.add_argument(
        "--skip", nargs="*", default=[],
        choices=["metadata", "arms", "epochs", "elements", "cells", "visits", "objectives", "criteria", "activities"],
        help="Sections to skip during upload",
    )

    # ── list-codelists ───────────────────────────────────────────────────
    p_cl = sub.add_parser("list-codelists", help="Fetch and display all codelists from OSB")
    p_cl.add_argument("--api-url", default=None, help="OSB API base URL")
    p_cl.add_argument("--idp-url", default=None, help="OAuth2 IDP URL")
    p_cl.add_argument("--client-id", default="osbidp", help="OAuth2 client ID")
    p_cl.add_argument("--client-secret", help="OAuth2 client secret")
    p_cl.add_argument("--username", help="OSB username")
    p_cl.add_argument("--password", help="OSB password")
    p_cl.add_argument("--config", help="Path to JSON config file")
    p_cl.add_argument("--no-auth", action="store_true",
                      help="Skip OAuth — talk to an OSB instance with no IDP gating")
    p_cl.add_argument("--log-dir", default=".", help="Directory for log files")

    args = parser.parse_args()
    log_file = setup_logging(getattr(args, "log_dir", "."))
    logger.info("Log file: %s", log_file)

    # ── VALIDATE ─────────────────────────────────────────────────────────
    if args.command == "validate":
        result = validate_usdm(args.usdm_file)
        if result["can_proceed"]:
            print("\nValidation PASSED. You can now run:")
            print(f"  python -m usdm_to_osb upload {args.usdm_file} --config config.json")
            sys.exit(0)
        else:
            print("\nValidation FAILED. Fix the critical sections above, then re-run:")
            print(f"  python -m usdm_to_osb validate {args.usdm_file}")
            sys.exit(1)

    # ── UPLOAD ───────────────────────────────────────────────────────────
    elif args.command == "upload":
        config = _build_config(args)
        results = run_upload(config, args.usdm_file, skip_sections=set(args.skip))
        if results and results.get("study_uid"):
            print(f"\nStudy UID: {results['study_uid']}")
            logger.info("Log file: %s", log_file)
            sys.exit(0)
        else:
            print("\nUpload failed. Check the log for details.")
            logger.info("Log file: %s", log_file)
            sys.exit(1)

    # ── LIST-CODELISTS ───────────────────────────────────────────────────
    elif args.command == "list-codelists":
        config = _build_config(args)
        token_mgr = TokenManager(config)
        api = APIClient(config, token_mgr)
        ct = CTResolver(api)
        codelists = ct.list_codelists()
        print(f"\n{'Codelist Name':<45} {'UID':<20}")
        print("-" * 65)
        for name, uid in sorted(codelists.items()):
            print(f"{name:<45} {uid:<20}")
        print(f"\nTotal: {len(codelists)} codelists")


if __name__ == "__main__":
    main()
