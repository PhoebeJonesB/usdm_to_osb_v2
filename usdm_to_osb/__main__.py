"""Allow running as: python -m usdm_to_osb <args>

Two canonical run modes:

  1. UNAUTHENTICATED OSB (no IDP gating — e.g. local dev sandbox)
       python -m usdm_to_osb \\
           --usdm    "C:/path/to/Study.json" \\
           --api-url "https://devsandbox.htp42.site/api" \\
           --no-auth

  2. AUTHENTICATED OSB (OAuth2 via config file)
       python -m usdm_to_osb \\
           --usdm   "C:/path/to/Study.json" \\
           --config "C:/path/to/config.json"

     config.json keys: api_base_url, idp_url, client_id, client_secret,
                       username, password, project_number

Individual OAuth CLI args (--idp-url, --client-id, --client-secret, --username,
--password) are also accepted as overrides; if anything is still blank in
non-no-auth mode, you'll be prompted interactively.
"""
import argparse
import os
import textwrap
from .run import main

parser = argparse.ArgumentParser(
    prog="python -m usdm_to_osb",
    description="USDM 4.0 -> OpenStudyBuilder Upload",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=textwrap.dedent("""
        Two canonical run modes:

          1. UNAUTHENTICATED OSB (--no-auth):
             python -m usdm_to_osb \\
                 --usdm    "C:/path/to/Study.json" \\
                 --api-url "https://devsandbox.htp42.site/api" \\
                 --no-auth

          2. AUTHENTICATED OSB (--config):
             python -m usdm_to_osb \\
                 --usdm   "C:/path/to/Study.json" \\
                 --config "C:/path/to/config.json"
    """).strip(),
)
parser.add_argument("--usdm", type=str, default=None, help="Path to the USDM JSON file")
parser.add_argument("--config", type=str, default=None,
                    help="Path to JSON config file with credentials "
                         "(see config_template.json) — required for AUTH mode unless "
                         "the individual --client-secret/--username/--password CLI args are given")
parser.add_argument("--api-url", type=str, default=None, help="OSB API base URL")
parser.add_argument("--no-auth", action="store_true",
                    help="UNAUTHENTICATED mode — skip OAuth entirely. No "
                         "Authorization header is sent and credentials are not required. "
                         "Use this for OSB instances running without an IDP "
                         "(e.g. local dev sandboxes).")

# Auth-mode CLI overrides (used when --config isn't supplied)
auth_group = parser.add_argument_group(
    "Auth-mode overrides (used when --config is not given; ignored under --no-auth)"
)
auth_group.add_argument("--idp-url", type=str, default=None, help="OAuth2 IDP URL")
auth_group.add_argument("--client-id", type=str, default=None, help="OAuth2 client ID")
auth_group.add_argument("--client-secret", type=str, default=None, help="OAuth2 client secret")
auth_group.add_argument("--username", type=str, default=None, help="OSB username (email)")
auth_group.add_argument("--password", type=str, default=None, help="OSB password")

args = parser.parse_args()

# --no-auth and --config are mutually exclusive in spirit; if both given,
# --no-auth wins (you've explicitly opted out of auth).
if args.no_auth and args.config:
    print("NOTE: --no-auth supersedes --config (no credentials will be used)")

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
if args.no_auth:
    os.environ["OSB_NO_AUTH"] = "1"

main(
    usdm_path=args.usdm,
    cfg_path=None if args.no_auth else args.config,
    no_auth=args.no_auth,
)
