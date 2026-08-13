import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


from backend.settings.secrets import KeyringSecretStore


LEGACY_ENV = {
    "document_mcp_url": "DINGTALK_MCP_URL",
    "spreadsheet_mcp_url": "DINGTALK_SPREADSHEET_MCP_URL",
    "minimax_api_key": "MINIMAX_API_KEY",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly migrate legacy environment credentials to keyring."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="confirm reading legacy environment values and writing missing keys",
    )
    args = parser.parse_args(argv)
    if not args.confirm:
        print("No credential migration performed; rerun with --confirm.")
        return 2

    statuses: list[tuple[str, bool]] = []
    try:
        store = KeyringSecretStore()
        for name, env_name in LEGACY_ENV.items():
            saved = store.get(name)
            value = os.environ.get(env_name, "")
            if value and not saved:
                store.set(name, value)
                saved = value
            statuses.append((name, bool(saved)))
    except Exception:
        # No raw keyring/provider exception may reach a CLI traceback.
        print("Credential migration failed safely.", file=sys.stderr)
        return 1
    for name, configured in statuses:
        print(f"{name}: configured={configured}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
