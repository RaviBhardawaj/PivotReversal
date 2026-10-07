# #!/usr/bin/env python3
# """
# Dhan HQ login helper.
# Credentials are stored in dhan_config.json and reused until changed.
# """

# import json
# import os
# import sys
# from pathlib import Path

# import pyotp
# import requests

# CONFIG_FILE = Path(__file__).parent / "config.json"
# BASE_URL = "https://api.dhan.co/v2"

# REQUIRED_KEYS = ["client_id", "pin", "app_id", "app_secret", "totp_secret"]
# PROMPTS = {
#     "client_id":   "Dhan Client ID",
#     "pin":         "Dhan PIN",
#     "app_id":      "Dhan App ID",
#     "app_secret":  "Dhan App Secret",
#     "totp_secret": "Google Authenticator TOTP secret (base32, no spaces)",
# }


# # ----------------------------------------------------------------------
# # Config helpers
# # ----------------------------------------------------------------------
# def load_config() -> dict:
#     if CONFIG_FILE.exists():
#         with open(CONFIG_FILE, "r") as f:
#             cfg = json.load(f)
#     else:
#         cfg = {}

#     changed = False
#     for key in REQUIRED_KEYS:
#         if key not in cfg:
#             cfg[key] = ""
#             changed = True

#     if changed or not CONFIG_FILE.exists():
#         save_config(cfg)
#     return cfg


# def save_config(cfg: dict) -> None:
#     with open(CONFIG_FILE, "w") as f:
#         json.dump(cfg, f, indent=2)
#     try:
#         os.chmod(CONFIG_FILE, 0o600)  # keep secrets private
#     except OSError:
#         pass


# def prompt_missing(cfg: dict) -> dict:
#     """Ask for any blank value; save only if something was entered."""
#     changed = False
#     for key in REQUIRED_KEYS:
#         if not cfg.get(key):
#             val = input(f"Enter {PROMPTS[key]}: ").strip()
#             if val:
#                 cfg[key] = val
#                 changed = True
#     if changed:
#         save_config(cfg)
#         print(f"[+] Credentials saved to {CONFIG_FILE}")
#     return cfg


# def update_config(**kwargs) -> dict:
#     """Change one or more stored params. e.g. update_config(pin='1234')"""
#     cfg = load_config()
#     changed = False
#     for key, value in kwargs.items():
#         if key in REQUIRED_KEYS and value and cfg.get(key) != value:
#             cfg[key] = value
#             changed = True
#     if changed:
#         save_config(cfg)
#         print("[+] Config updated.")
#     return cfg


# # ----------------------------------------------------------------------
# # Dhan auth flow
# # ----------------------------------------------------------------------
# def _headers(cfg: dict) -> dict:
#     return {
#         "app_id": cfg["app_id"],
#         "app_secret": cfg["app_secret"],
#         "Content-Type": "application/json",
#         "Accept": "application/json",
#     }


# def generate_consent(cfg: dict) -> str:
#     url = f"{BASE_URL}/auth/generate-consent"
#     body = {"dhanClientId": cfg["client_id"], "pin": cfg["pin"]}
#     r = requests.post(url, headers=_headers(cfg), json=body, timeout=30)
#     r.raise_for_status()
#     return r.json()["consentId"]


# def consume_consent(cfg: dict, consent_id: str) -> dict:
#     url = f"{BASE_URL}/auth/consume-consent"
#     totp = pyotp.TOTP(cfg["totp_secret"]).now()
#     body = {"consentId": consent_id, "totp": totp}
#     r = requests.post(url, headers=_headers(cfg), json=body, timeout=30)
#     r.raise_for_status()
#     return r.json()


# def dhan_login() -> dict:
#     cfg = load_config()
#     cfg = prompt_missing(cfg)

#     if not all(cfg.get(k) for k in REQUIRED_KEYS):
#         raise SystemExit("[-] Missing required credentials. Aborting.")

#     print("[*] Requesting consent...")
#     consent_id = generate_consent(cfg)
#     print(f"[+] consentId = {consent_id}")

#     print("[*] Consuming consent with TOTP...")
#     result = consume_consent(cfg, consent_id)
#     print("[+] Login successful.")
#     return result


# # ----------------------------------------------------------------------
# if __name__ == "__main__":
#     try:
#         auth = dhan_login()
#         print(json.dumps(auth, indent=2))

#         # Example: persist the access token next to config for reuse
#         token_file = CONFIG_FILE.with_name("dhan_token.json")
#         with open(token_file, "w") as f:
#             json.dump(auth, f, indent=2)
#         os.chmod(token_file, 0o600)
#         print(f"[+] Access token saved to {token_file}")

#     except requests.HTTPError as e:
#         print(f"[-] HTTP error: {e.response.status_code} - {e.response.text}")
#         sys.exit(1)
#     except Exception as e:
#         print(f"[-] Error: {e}")
#         sys.exit(1)

import json
import os
import time
from pathlib import Path

import pyotp
import requests

CONFIG_FILE = Path(__file__).parent / "config.json"
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"

REQUIRED_KEYS = ["client_id", "pin", "app_id", "app_secret", "totp_secret"]
PROMPTS = {
    "client_id":   "Dhan Client ID",
    "pin":         "Dhan PIN",
    "app_id":      "Dhan App ID",
    "app_secret":  "Dhan App Secret",
    "totp_secret": "Google Authenticator TOTP secret (base32, no spaces)",
}


def load_config() -> dict:
    """Loads config from JSON, creating it if it doesn't exist."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    changed = False
    for key in REQUIRED_KEYS:
        if key not in cfg:
            cfg[key] = ""
            changed = True

    if changed or not CONFIG_FILE.exists():
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    """Saves config to JSON and sets restrictive file permissions."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def prompt_missing(cfg: dict) -> dict:
    """Prompts for any missing configuration values and saves them."""
    changed = False
    for key in REQUIRED_KEYS:
        if not cfg.get(key):
            val = input(f"Enter {PROMPTS[key]}: ").strip()
            if val:
                cfg[key] = val
                changed = True
    if changed:
        save_config(cfg)
        print(f"[+] Credentials saved to {CONFIG_FILE}")
    return cfg


def update_config(**kwargs) -> dict:
    """Updates one or more configuration parameters and saves them."""
    cfg = load_config()
    changed = False
    for key, value in kwargs.items():
        if key in REQUIRED_KEYS and value and cfg.get(key) != value:
            cfg[key] = value
            changed = True
    if changed:
        save_config(cfg)
        print("[+] Config updated.")
    return cfg


def check_clock_skew(totp_secret: str) -> bool:
    """
    Checks if the local clock is in sync with the TOTP server.
    Returns True if the skew is acceptable, False otherwise.
    """
    try:
        totp = pyotp.TOTP(totp_secret)
        # Generate a code and see if it matches the current time step.
        # This is a simplified check. A more robust method would compare
        # against a known time server, but this is a good heuristic.
        current_code = totp.now()
        # Verify the code against a small window. If it fails, the clock is likely skewed.
        if not totp.verify(current_code, valid_window=1):
            print("[!] Warning: Local clock may be skewed. Please sync your system time.")
            return False
        return True
    except Exception as e:
        print(f"[!] Could not verify clock skew: {e}")
        return True  # Proceed with caution


def dhan_login() -> dict:
    """
    Main login function. Handles config loading, TOTP generation,
    and API call to Dhan's authentication endpoint.
    """
    cfg = load_config()
    cfg = prompt_missing(cfg)

    if not all(cfg.get(k) for k in REQUIRED_KEYS):
        raise SystemExit("[-] Missing required credentials. Aborting.")

    # Optional: Check for clock skew before proceeding
    # check_clock_skew(cfg["totp_secret"])

    # Generate TOTP code
    totp_secret = cfg["totp_secret"].strip().upper().replace(" ", "")
    totp_code = pyotp.TOTP(totp_secret).now()
    print(f"[*] Generated TOTP: {totp_code}")

    # Prepare request to the correct Dhan auth endpoint
    params = {
        "dhanClientId": cfg["client_id"],
        "pin": cfg["pin"],
        "totp": totp_code,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    print("[*] Requesting access token...")
    try:
        response = requests.post(AUTH_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()

        if result.get("status") == "error":
            print(f"[-] Dhan API Error: {result.get('message', 'Unknown error')}")
            # Common error handling
            if "Invalid TOTP" in result.get("message", ""):
                print("[-] TOTP is invalid. Please check:")
                print("    1. Your system and phone clock are synced.")
                print("    2. The TOTP secret in your config file is correct.")
                print("    3. You are using the latest TOTP secret from Dhan.")
            return result

        print("[+] Login successful.")
        return result

    except requests.HTTPError as e:
        print(f"[-] HTTP error: {e.response.status_code} - {e.response.text}")
        raise
    except Exception as e:
        print(f"[-] Error: {e}")
        raise


if __name__ == "__main__":
    try:
        auth = dhan_login()
        if auth.get("accessToken"):
            print(f"[+] Access Token: {auth['accessToken'][:20]}...")
            # Save token for later use
            token_file = CONFIG_FILE.with_name("dhan_token.json")
            with open(token_file, "w") as f:
                json.dump(auth, f, indent=2)
            os.chmod(token_file, 0o600)
            print(f"[+] Token saved to {token_file}")
    except Exception as e:
        print(f"[-] Login failed: {e}")