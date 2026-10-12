import os
import json
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import google.genai as genai

# Configuración de URLs y Pestañas
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

DRIVE_FOLDER_ID = "1FkYukMBPnHhA2B3RjbAtyVrLGTAWIBye"
SPREADSHEET_ID = "1gcfPwuYSgPEO9tGA4JkviQZBAOe1QQuQk1LSi3YmEXc"
TAB_TRANSACCIONES = "Transacciones"
TAB_BALANCES = "Balances_Registrados"
TAB_PROCESADOS = "Archivos_Procesados"

PDF_PASSWORD = os.environ.get("PDF_PASSWORD")
GCP_JSON = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GCP_JSON:
    print("[-] Error: No se encontró GCP_SERVICE_ACCOUNT_JSON.")
    exit(1)

if not GEMINI_API_KEY:
    print("[-] Error: No se encontró GEMINI_API_KEY.")
    exit(1)

# Inicializar Clientes
service_account_info = json.loads(GCP_JSON)
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)

drive_service = build('drive', 'v3', credentials=creds)
gc = gspread.authorize(creds)
ai_client = genai.Client(api_key=GEMINI_API_KEY)
