import io
from googleapiclient.http import MediaIoBaseDownload
from config import (
    gc, drive_service, SPREADSHEET_ID, TAB_TRANSACCIONES, 
    TAB_BALANCES, TAB_PROCESADOS
)
from utils import (
    normalizar_monto, normalizar_texto, obtener_ultimos_4, 
    formatear_producto_4_digitos, construir_clave_base, abrir_pdf_hibrido
)
from gemini_service import procesar_pdf_con_gemini

def ejecutar_proceso_drive_sheets(subfolders, drive_folder_id):
    carpetas_a_procesar = subfolders if subfolders else [{'id': drive_folder_id, 'name': 'Carpeta Principal'}]
    
    ENCABEZADOS_TRANSACCIONES = ["Subcarpeta", "Documento PDF", "Producto / Cuenta", "Fecha Transacción", "Descripción", "Moneda", "Monto", "ID / Autorización"]
    ENCABEZADOS_BALANCES = ["Subcarpeta", "Documento PDF", "Producto / Cuenta", "Fecha Corte", "Moneda", "Balance Registrado (Oficial)"]
    ENCABEZADOS_PROCESADOS = ["ID Archivo Drive", "Nombre del Documento", "Subcarpeta", "Estado"]

    sht = gc.open_by_key(SPREADSHEET_ID)
    
    try:
        ws_proc = sht.worksheet(TAB_PROCESADOS)
    except Exception:
        ws_proc = sht.add_worksheet(title=TAB_PROCESADOS, rows=500, cols=5)
        ws_proc.append_row(ENCABEZADOS_PROCESADOS, value_input_option='USER_ENTERED')

    registros_proc = ws_proc.get_all_values() or [ENCABEZADOS_PROCESADOS]
    estados_archivos, filas_archivos_map = {}, {}
    for i, fila in enumerate(registros_proc[1:], start=2):
        if len(fila) >= 4 and fila[0].strip():
            estados_archivos[fila[0].strip()] = fila[3].strip().lower()
            filas_archivos_map[fila[0].strip()] = i

    try:
        ws_tx = sht.worksheet(TAB_TRANSACCIONES)
    except Exception:
        ws_tx = sht.add_worksheet(title=TAB_TRANSACCIONES, rows=1000, cols=10)
        ws_tx.append_row(ENCABEZADOS_TRANSACCIONES, value_input_option='USER_ENTERED')

    registros_tx = ws_tx.get_all_values() or [ENCABEZADOS_TRANSACCIONES]
    ids_registrados_set = {fila[7].strip().lower().replace("\r", "").replace("\n", "") for fila in registros_tx[1:] if len(fila) >= 8 and fila[7].strip()}

    try:
        ws_bal = sht.worksheet(TAB_BALANCES)
    except Exception:
        ws_bal = sht.add_worksheet(title=TAB_BALANCES, rows=500, cols=10)
        ws_bal.append_row(ENCABEZADOS_BALANCES, value_input_option='USER_ENTERED')

    registros_bal = ws_bal.get_all_values() or [ENCABEZADOS_BALANCES]
    balances_existentes_set = {f"{obtener_ultimos_4(fila[2])}_{fila[3].strip()}_{normalizar_texto(fila[4])}" for fila in registros_bal[1:] if len(fila) >= 6}

    filas_transacciones_nuevas, filas_balances_nuevas, filas_procesados_nuevos, celdas_a_actualizar = [], [], [], []

    for subfolder in carpetas_a_procesar:
        subfolder_id, subfolder_name = subfolder['id'], subfolder['name']
        print(f"\n==================================================\n[+] Entrando a: {subfolder_name}\n==================================================")
        
        pdf_query = f"'{subfolder_id}' in parents and mimeType = 'application/pdf' and trashed = false"
        pdfs = drive_service.files().list(q=pdf_query, fields="files(id, name, originalFilename)", orderBy="name").execute().get('files', [])
        
        if not pdfs:
            print("   ↳ Sin PDFs en esta carpeta.")
            continue

        pdf_file = pdfs[0]
        pdf_id = pdf_file['id']
        pdf_name = pdf_file.get('name', pdf_file.get('originalFilename', 'Extracto.pdf'))
        estado_actual = estados_archivos.get(pdf_id, None)

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
            producto = formatear_producto_4_digitos(resultado.get("producto", "Desconocido"))
            fecha_corte = resultado.get("fecha_corte", "")
            ultimos4_prod = obtener_ultimos_4(producto)
            
            for sec in resultado["secciones_moneda"]:
                moneda, balance = sec.get("moneda", "COP"), sec.get("balance_registrado", 0.0)
                clave_bal_check = f"{ultimos4_prod}_{fecha_corte}_{normalizar_texto(moneda)}"
                
                if clave_bal_check not in balances_existentes_set:
                    filas_balances_nuevas.append([subfolder_name, pdf_name, producto, fecha_corte, moneda, balance])
                    balances_existentes_set.add(clave_bal_check)
                
                ocurrencias_lote = {}
                for tx in sec.get("transacciones", []):
                    fecha_tx, desc_tx = tx.get("fecha_transaccion", ""), tx.get("descripcion", "")
                    monto_norm = normalizar_monto(tx.get("monto", 0.0))
                    
                    clave_base = construir_clave_base(producto, fecha_tx, monto_norm, moneda)
                    idx = ocurrencias_lote.get(clave_base, 0)
                    ocurrencias_lote[clave_base] = idx + 1
                    id_final = f"{clave_base}/{idx}"
                    
                    if id_final not in ids_registrados_set:
                        filas_transacciones_nuevas.append([subfolder_name, pdf_name, producto, fecha_tx, desc_tx, moneda, float(monto_norm), id_final])
                        ids_registrados_set.add(id_final)
                    
            print(f"      [✓] Ok: {len(resultado['secciones_moneda'])} sección(es) procesadas.")
            
            if pdf_id not in estados_archivos:
                filas_procesados_nuevos.append([pdf_id, pdf_name, subfolder_name, "No leer"])
                estados_archivos[pdf_id] = "no leer"
            elif estado_actual == "leer":
                celdas_a_actualizar.append({'range': f'D{filas_archivos_map[pdf_id]}', 'values': [['No leer']]})
                estados_archivos[pdf_id] = "no leer"

    if filas_transacciones_nuevas:
        ws_tx.append_rows(filas_transacciones_nuevas, value_input_option='USER_ENTERED')
        print(f"\n[✓] Se agregaron {len(filas_transacciones_nuevas)} transacciones nuevas abajo.")

    if filas_balances_nuevas:
        ws_bal.append_rows(filas_balances_nuevas, value_input_option='USER_ENTERED')
        print(f"[✓] Se agregaron {len(filas_balances_nuevas)} balances nuevos abajo.")

    if filas_procesados_nuevos:
        ws_proc.append_rows(filas_procesados_nuevos, value_input_option='USER_ENTERED')
        print(f"[✓] Se registraron {len(filas_procesados_nuevos)} archivos nuevos en Archivos_Procesados.")

    if celdas_a_actualizar:
        ws_proc.batch_update(celdas_a_actualizar)
        print(f"[✓] Se actualizaron {len(celdas_a_actualizar)} estado(s) a 'No leer'.")

    print("\n[✓] ¡Proceso completado con éxito!")
