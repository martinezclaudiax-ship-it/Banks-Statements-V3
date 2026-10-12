import re
import io
import pikepdf
from config import PDF_PASSWORD

def normalizar_monto(monto_raw) -> str:
    """ Convierte cualquier formato de monto a float de 2 decimales respetando el signo original. """
    monto_texto = str(monto_raw).strip()
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
    """ Construye la clave base matemática e inmune a errores de IA. """
    ultimos_4 = obtener_ultimos_4(producto)
    f = str(fecha).strip()
    m = monto_normalizado
    mon = normalizar_texto(moneda)
    return f"{ultimos_4}/{f}/{m}/{mon}"

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
