import json
import time
from google.genai import types
from config import ai_client
from models import ResultadoExtracto

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
    
    modelos_a_probar = ['models/gemini-3.5-flash-lite']

    for modelo in modelos_a_probar:
        for intento in range(3):
            try:
                response = ai_client.models.generate_content(
                    model=modelo,
                    contents=[
                        types.Part.from_bytes(data=pdf_bytes, mime_type='application/pdf'),
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
