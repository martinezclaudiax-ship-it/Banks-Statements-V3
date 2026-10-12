from pydantic import BaseModel, Field
from typing import List

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
