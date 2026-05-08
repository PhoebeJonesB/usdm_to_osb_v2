"""Allow running as: python -m usdm_to_osb <args>"""
import argparse
import os
from .run import main

parser = argparse.ArgumentParser(description="USDM 4.0 -> OpenStudyBuilder Upload")
parser.add_argument("--usdm", type=str, default=None, help="Path to the USDM JSON file")
parser.add_argument("--config", type=str, default=None,
                    help="Path to JSON config file with credentials (see config_template.json)")
parser.add_argument("--api-url", type=str, default=None, help="OSB API base URL")
parser.add_argument("--idp-url", type=str, default=None, help="OAuth2 IDP URL")
parser.add_argument("--client-id", type=str, default=None, help="OAuth2 client ID")
parser.add_argument("--client-secret", type=str, default=None, help="OAuth2 client secret")
parser.add_argument("--username", type=str, default=None, help="OSB username (email)")
parser.add_argument("--password", type=str, default=None, help="OSB password")
parser.add_argument("--no-auth", action="store_true",
                    help="Connect to an OSB instance that has no authentication "
                         "(skips OAuth2; only --api-url / config api_base_url is needed)")
args = parser.parse_args()

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

main(usdm_path=args.usdm, cfg_path=args.config, no_auth=args.no_auth)
