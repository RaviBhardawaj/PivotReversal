import sys
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1TNuwFBf1xtTq7xyu_d32MPoWhSp7HzMdacNSnFV6i2U"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

try:
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    print(f"Service Account: {creds.service_account_email}")
    
    ss = gc.open_by_key(SPREADSHEET_ID)
    print(f"SUCCESS: Connected to '{ss.title}'!")
    print(f"Worksheets found: {[w.title for w in ss.worksheets()]}")
    sys.exit(0)
except Exception as ex:
    cause = getattr(ex, "__cause__", None)
    if cause:
        print(f"Error Details: {cause}")
    else:
        print(f"Error Details: {ex}")
    sys.exit(1)
