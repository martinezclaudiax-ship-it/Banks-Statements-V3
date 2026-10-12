import os
import io
import json
import time
import re
import pikepdf
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import google.genai as genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List

# --- CONFIGURACIÓN ---
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

# Inicializar credenciales de Google Drive y Sheets
service_account_info = json.loads(GCP_JSON)
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)

drive_service = build('drive', 'v3', credentials=creds)
gc = gspread.authorize(creds)

# Inicializar Cliente Gemini
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# --- FUNCIONES DE NORMALIZACIÓN Y GENERACIÓN DE ID ---
def normalizar_monto(monto_raw) -> str:
    """ Convierte cualquier formato de monto a float de 2 decimales respetando el signo original. """
    monto_texto = str(monto_raw).strip()
    
    # Detecta si el banco le puso formato negativo como (1.000) o 1.000-
    es_negativo = '-' in monto_texto or '(' in monto_texto
    
    monto_texto = monto_texto.replace(',', '.')
    monto_limpio = re.sub(r'[^\d.]', '', monto_texto)
    
    try:
        monto_float = float(monto_limpio)
        if es_negativo:
            monto_float = -abs(monto_float)
        return f"{monto_float:.2f}"
    except ValueError:
        return "0.00"

def normalizar_texto(texto: str) -> str:
    """ Pasa texto a minúsculas y elimina espacios extras. """
    if not texto:
        return ""
    return re.sub(r'\s+', ' ', str(texto).strip().lower())

def obtener_ultimos_4(producto: str) -> str:
    """ Extrae únicamente los últimos 4 dígitos numéricos del producto. """
    digitos = re.sub(r'\D', '', str(producto))
    return digitos[-4:] if len(digitos) >= 4 else normalizar_texto(producto)

def formatear_producto_4_digitos(producto_raw: str) -> str:
    """ Garantiza que el producto quede strictly como '[Tipo] *[Últimos 4 dígitos]' """
    if not producto_raw:
        return "Desconocido"
    match_tipo = re.search(r'(Ahorros|Corriente|Mastercard|Visa|American Express)', producto_raw, re.IGNORECASE)
    tipo = match_tipo.group(1).capitalize() if match_tipo else "Producto"
    ultimos_4 = obtener_ultimos_4(producto_raw)
    return f"{tipo} *{ultimos_4}"

def construir_clave_base(producto: str, fecha: str, monto_normalizado: str, moneda: str) -> str:
    """ 
    Construye la clave base matemática e inmune a errores de IA (sin descripción).
    Ejemplo: 3661/2025-10-01/-25.10/cop
    """
    ultimos_4 = obtener_ultimos_4(producto)
    f = str(fecha).strip()
    m = monto_normalizado
    mon = normalizar_texto(moneda)
    return f"{ultimos_4}/{f}/{m}/{mon}"


# --- ESQUEMAS PYDANTIC PARA ESTRUCTURA JSON ---
class TransaccionItem(BaseModel):
    fecha_transaccion: str = Field(description="Fecha YYYY-MM-DD")
    descripcion: str = Field(description="Detalle o comercio")
    monto: float = Field(description="Monto flotante exacto como aparece en el extracto, sin invertir ni forzar signos.")

class SeccionMoneda(BaseModel):
    moneda: str = Field(description="COP o USD")
    balance_registrado: float = Field(description="Saldo oficial de corte")
    transacciones: List[TransaccionItem]

class ResultadoExtracto(BaseModel):
    producto: str = Field(description="Nombre del producto, ej: Mastercard *1739 o Ahorros *3661")
    fecha_corte: str = Field(description="Fecha final del periodo YYYY-MM-DD")
    secciones_moneda: List[SeccionMoneda]


def abrir_pdf_hibrido(stream_bytes):
    pdf_bytes_io = io.BytesIO()
    try:
        stream_bytes.seek(0)
        pdf_unlocked = pikepdf.open(stream_bytes, password=PDF_PASSWORD)
        pdf_unlocked.save(pdf_bytes_io)
        pdf_bytes_io.seek(0)
        return pdf_bytes_io
    except Exception:
        stream_bytes.seek(0)
        return stream_bytes

def procesar_pdf_con_gemini(pdf_bytes_io, pdf_name):
    pdf_bytes_io.seek(0)
    pdf_bytes = pdf_bytes_io.read()

    prompt_instrucciones = """
    Eres un sistema de auditoría financiera para procesar extractos de Bancolombia.
    Extrae la información del producto, saldos de corte y lista de movimientos.

    REGLAS PRODUCTO Y MONEDA:
    1. Identifica Producto siguiendo strictly el formato '[Tipo] *[Últimos 4 dígitos]' (ejemplos: 'Ahorros *3661', 'Corriente *1234', 'Mastercard *1739', 'Visa *6438'). Toma EXACTAMENTE los últimos 4 dígitos numéricos del número de cuenta/tarjeta, NUNCA 5 ni 3. NUNCA incluyas la palabra 'Cuenta de' ni el nombre del titular.
       MANTÉN LA MISMA FRANQUICIA/TIPO DE PRODUCTO PARA TODAS LAS TRANSACCIONES EXTRAÍDAS DEL MISMO DOCUMENTO.
    2. Identifica Moneda ("COP" o "USD").
    3. Si hay secciones en Pesos y Dólares en el mismo PDF, genera un bloque 'secciones_moneda' por cada una.

    REGLAS SALDO CORTE (balance_registrado):
    - Extrae el saldo buscando: "= Pagos total", "Pago Total:", "Deuda a la fecha de corte:", "SALDO ACTUAL".

    REGLAS TRANSACCIONES:
    - Extrae 1 sola fila por cada movimiento real del extracto.
    - SEGUNDA LÍNEA DE DESCRIPCIÓN: Si una transacción tiene un texto secundario en el renglón de abajo (ejemplo: 'VR MONEDA ORIG...'), ÚNELO en una sola celda con la descripción principal. NUNCA generes una fila extra ni una transacción suelta para esa segunda línea.
    - MONTO (DEUDA ORIGINAL): En tablas con varias columnas de monto (ej: 'Valor Original' / 'Valor movimiento' vs 'Cargos y Abonos' / 'Valor Cuota/Abono'), TOMA ÚNICAMENTE la columna de la deuda total/original ('Valor Original' o 'Valor movimiento'). IGNORA por completo las columnas de cuotas o abonos ('Cargos y Abonos' o 'Valor Cuota/Abono').
    - MONTO SIGNO: Conserva el valor numérico con el signo exacto tal como figura en la celda del PDF sin aplicar inversiones artificiales.
    - FECHA: YYYY-MM-DD. Si solo viene DD/MM, usa el año del documento.
    """
    
    modelos_a_probar = [
        'models/gemini-3.5-flash-lite',
    ]

    for modelo in modelos_a_probar:
        for intento in range(3):
            try:
                response = ai_client.models.generate_content(
                    model=modelo,
                    contents=[
                        types.Part.from_bytes(
                            data=pdf_bytes,
                            mime_type='application/pdf',
                        ),
                        prompt_instrucciones
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ResultadoExtracto,
                        temperature=0.1
                    ),
                )
                return json.loads(response.text)
            except Exception as e:
                err_msg = str(e)
                if "503" in err_msg or "UNAVAILABLE" in err_msg or "429" in err_msg:
                    tiempo_espera = (intento + 1) * 6
                    print(f"      [!] {modelo} saturado. Esperando {tiempo_espera}s... (Intento {intento+1}/3)")
                    time.sleep(tiempo_espera)
                    continue
                elif "404" in err_msg or "NOT_FOUND" in err_msg:
                    print(f"      [!] El modelo {modelo} no existe o no está habilitado, probando siguiente...")
                    break
                else:
                    print(f"      [-] Error no controlado procesando {pdf_name} con {modelo}: {e}")
                    break

    print(f"      [-] No se pudo procesar {pdf_name} con ninguno de los modelos probados.")
    return None


def main():
    print("[+] Iniciando proceso MODO PRUEBA con Control por Pestaña Archivos_Procesados...")
    
    # --- DIAGNÓSTICO DE MODELOS DISPONIBLES EN TU API KEY ---
    print("\n--- CONSULTANDO MODELOS DISPONIBLES EN TU API KEY ---")
    try:
        for m in ai_client.models.list():
            print("  ↳ Modelo disponible:", m.name)
    except Exception as e:
        print("  [-] No se pudo listar modelos:", e)
    print("------------------------------------------------------\n")

    subfolder_query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    subfolders = drive_service.files().list(q=subfolder_query, fields="files(id, name)", orderBy="name").execute().get('files', [])
    
    carpetas_a_procesar = subfolders if subfolders else [{'id': DRIVE_FOLDER_ID, 'name': 'Carpeta Principal'}]
    
    ENCABEZADOS_TRANSACCIONES = [
        "Subcarpeta", "Documento PDF", "Producto / Cuenta", "Fecha Transacción", 
        "Descripción", "Moneda", "Monto", "ID / Autorización"
    ]
    
    ENCABEZADOS_BALANCES = [
        "Subcarpeta", "Documento PDF", "Producto / Cuenta", "Fecha Corte", 
        "Moneda", "Balance Registrado (Oficial)"
    ]

    ENCABEZADOS_PROCESADOS = [
        "ID Archivo Drive", "Nombre del Documento", "Subcarpeta", "Estado"
    ]

    sht = gc.open_by_key(SPREADSHEET_ID)
    
    # --- PASO 1: LEER HOJA ARCHIVOS_PROCESADOS ---
    try:
        ws_proc = sht.worksheet(TAB_PROCESADOS)
    except Exception:
        ws_proc = sht.add_worksheet(title=TAB_PROCESADOS, rows=500, cols=5)
        ws_proc.append_row(ENCABEZADOS_PROCESADOS, value_input_option='USER_ENTERED')

    registros_proc_existentes = ws_proc.get_all_values()
    if not registros_proc_existentes:
        ws_proc.append_row(ENCABEZADOS_PROCESADOS, value_input_option='USER_ENTERED')
        registros_proc_existentes = [ENCABEZADOS_PROCESADOS]

    estados_archivos = {}
    filas_archivos_map = {}

    for i, fila in enumerate(registros_proc_existentes[1:], start=2):
        if len(fila) >= 4:
            file_id_sheet = str(fila[0]).strip()
            estado_sheet = str(fila[3]).strip().lower()
            if file_id_sheet:
                estados_archivos[file_id_sheet] = estado_sheet
                filas_archivos_map[file_id_sheet] = i

    # --- PASO 2: LEER IDS EXISTENTES EN TRANSACCIONES Y BALANCES ---
    # 2.1 Transacciones
    try:
        ws_tx = sht.worksheet(TAB_TRANSACCIONES)
    except Exception:
        ws_tx = sht.add_worksheet(title=TAB_TRANSACCIONES, rows=1000, cols=10)
        ws_tx.append_row(ENCABEZADOS_TRANSACCIONES, value_input_option='USER_ENTERED')

    registros_tx_existentes = ws_tx.get_all_values()
    if not registros_tx_existentes:
        ws_tx.append_row(ENCABEZADOS_TRANSACCIONES, value_input_option='USER_ENTERED')
        registros_tx_existentes = [ENCABEZADOS_TRANSACCIONES]

    ids_registrados_set = set()
    for fila in registros_tx_existentes[1:]:
        if len(fila) >= 8:
            id_existente = str(fila[7]).strip().lower().replace("\r", "").replace("\n", "")
            if id_existente:
                ids_registrados_set.add(id_existente)

    # 2.2 Balances
    try:
        ws_bal = sht.worksheet(TAB_BALANCES)
    except Exception:
        ws_bal = sht.add_worksheet(title=TAB_BALANCES, rows=500, cols=10)
        ws_bal.append_row(ENCABEZADOS_BALANCES, value_input_option='USER_ENTERED')

    registros_bal_existentes = ws_bal.get_all_values()
    if not registros_bal_existentes:
        ws_bal.append_row(ENCABEZADOS_BALANCES, value_input_option='USER_ENTERED')

    balances_existentes_set = set()
    for fila in registros_bal_existentes[1:]:
        if len(fila) >= 6:
            ultimos4_bal = obtener_ultimos_4(fila[2])
            fecha_corte_bal = str(fila[3]).strip()
            moneda_bal = normalizar_texto(fila[4])
            clave_bal = f"{ultimos4_bal}_{fecha_corte_bal}_{moneda_bal}"
            balances_existentes_set.add(clave_bal)

    # --- PASO 3: EXTRAER DATOS Y APLICAR FILTRO "LEER / NO LEER" ---
    filas_transacciones_nuevas = []
    filas_balances_nuevas = []
    filas_procesados_nuevos = []
    celdas_a_actualizar = []

    for subfolder in carpetas_a_procesar:
        subfolder_id = subfolder['id']
        subfolder_name = subfolder['name']
        
        print(f"\n==================================================")
        print(f"[+] Entrando a: {subfolder_name}")
        print(f"==================================================")
        
        pdf_query = f"'{subfolder_id}' in parents and mimeType = 'application/pdf' and trashed = false"
        pdfs = drive_service.files().list(
            q=pdf_query, 
            fields="files(id, name, originalFilename)", 
            orderBy="name"
        ).execute().get('files', [])
        
        if not pdfs:
            print("   ↳ Sin PDFs en esta carpeta.")
            continue

        # MODO PRUEBA: Procesa solo el 1er PDF de la subcarpeta
        pdf_file = pdfs[0]
        pdf_id = pdf_file['id']
        pdf_name = pdf_file.get('name', pdf_file.get('originalFilename', 'Extracto.pdf'))
        
        estado_actual = estados_archivos.get(pdf_id, None)

        # SI EL ARCHIVO DICE 'No leer', SE OMITE AL INSTANTE (0.001s, $0.00 USD)
        if estado_actual == "no leer":
            print(f"   ↳ [SALTADO EN 0s] {pdf_name} (Marcado como 'No leer')")
            continue

        print(f"   ↳ [MODO PRUEBA] Procesando únicamente el 1er PDF: {pdf_name}")
        
        request = drive_service.files().get_media(fileId=pdf_id)
        stream = io.BytesIO()
        downloader = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        
        pdf_desbloqueado = abrir_pdf_hibrido(stream)
        resultado = procesar_pdf_con_gemini(pdf_desbloqueado, pdf_name)
        
        if resultado and "secciones_moneda" in resultado:
            producto_raw = resultado.get("producto", "Desconocido")
            producto = formatear_producto_4_digitos(producto_raw)  # Forzar estrictamente 4 dígitos
            fecha_corte = resultado.get("fecha_corte", "")
            ultimos4_prod = obtener_ultimos_4(producto)
            
            for sec in resultado["secciones_moneda"]:
                moneda = sec.get("moneda", "COP")
                balance = sec.get("balance_registrado", 0.0)
                
                clave_bal_check = f"{ultimos4_prod}_{fecha_corte}_{normalizar_texto(moneda)}"
                if clave_bal_check not in balances_existentes_set:
                    filas_balances_nuevas.append([
                        subfolder_name, pdf_name, producto, fecha_corte, moneda, balance
                    ])
                    balances_existentes_set.add(clave_bal_check)
                
                ocurrencias_lote = {}

                for tx in sec.get("transacciones", []):
                    fecha_tx = tx.get("fecha_transaccion", "")
                    desc_tx = tx.get("descripcion", "")
                    monto_raw = tx.get("monto", 0.0)
                    monto_norm = normalizar_monto(monto_raw)
                    
                    clave_base = construir_clave_base(producto, fecha_tx, monto_norm, moneda)
                    idx = ocurrencias_lote.get(clave_base, 0)
                    ocurrencias_lote[clave_base] = idx + 1
                    
                    id_final = f"{clave_base}/{idx}"
                    
                    if id_final not in ids_registrados_set:
                        filas_transacciones_nuevas.append([
                            subfolder_name, 
                            pdf_name, 
                            producto, 
                            fecha_tx, 
                            desc_tx, 
                            moneda, 
                            float(monto_norm), 
                            id_final
                        ])
                        ids_registrados_set.add(id_final)
                    
            print(f"      [✓] Ok: {len(resultado['secciones_moneda'])} sección(es) procesadas.")
            
            # Registrar nuevo ID en 'Archivos_Procesados' o restaurarlo a 'No leer'
            if pdf_id not in estados_archivos:
                filas_procesados_nuevos.append([
                    pdf_id, pdf_name, subfolder_name, "No leer"
                ])
                estados_archivos[pdf_id] = "no leer"
            elif estado_actual == "leer":
                num_fila = filas_archivos_map[pdf_id]
                celdas_a_actualizar.append({
                    'range': f'D{num_fila}',
                    'values': [['No leer']]
                })
                estados_archivos[pdf_id] = "no leer"

        else:
            print("      [-] Error procesando este archivo.")

    # --- PASO 4: GUARDAR FILAS Y ACTUALIZAR ESTADOS AL FINAL ---
    if filas_transacciones_nuevas:
        ws_tx.append_rows(filas_transacciones_nuevas, value_input_option='USER_ENTERED')
        print(f"\n[✓] Se agregaron {len(filas_transacciones_nuevas)} transacciones nuevas abajo.")
    else:
        print("\n[i] No hay transacciones nuevas para agregar.")

    if filas_balances_nuevas:
        ws_bal.append_rows(filas_balances_nuevas, value_input_option='USER_ENTERED')
        print(f"[✓] Se agregaron {len(filas_balances_nuevas)} balances nuevos abajo.")
    else:
        print("[i] No hay balances nuevos para agregar.")

    if filas_procesados_nuevos:
        ws_proc.append_rows(filas_procesados_nuevos, value_input_option='USER_ENTERED')
        print(f"[✓] Se registraron {len(filas_procesados_nuevos)} archivos nuevos en Archivos_Procesados.")

    if celdas_a_actualizar:
        ws_proc.batch_update(celdas_a_actualizar)
        print(f"[✓] Se actualizaron {len(celdas_a_actualizar)} estado(s) a 'No leer'.")

    print("\n[✓] ¡Proceso completado con éxito!")

if __name__ == "__main__":
    main()
