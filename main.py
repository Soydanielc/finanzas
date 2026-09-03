"""
Backend de Finanzas Personales - Estética Hollow Knight
FastAPI + Supabase + Tasa BCV
"""

import os
from datetime import datetime, timezone
from typing import Optional, List
from uuid import UUID

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from supabase import create_client, Client
import httpx

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://njpamhmgqneazmqbyewn.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5qcGFtaG1ncW5lYXptcWJ5ZXduIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzODg4OTUsImV4cCI6MjEwMzk2NDg5NX0.VHGrq1FFVLal9o8iI4UG6wdPyCL8s-OrA-FGMhBzddI"

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Faltan SUPABASE_URL o SUPABASE_KEY. Configúralas en variables de entorno."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(
    title="Finanzas Hollow Knight",
    description="API personal de ingresos/gastos con conversión BCV",
    version="1.0.0",
)

# CORS abierto (frontend en GitHub Pages)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class TransaccionCreate(BaseModel):
    tipo: str = Field(..., pattern="^(ingreso|gasto)$")
    monto_bs: float = Field(..., gt=0)
    descripcion: str = Field(..., min_length=1, max_length=255)
    categoria: Optional[str] = Field(None, max_length=100)


class Transaccion(BaseModel):
    id: str
    tipo: str
    monto_bs: float
    descripcion: str
    categoria: Optional[str]
    fecha_creacion: str


class BalanceResponse(BaseModel):
    saldo_bs: float
    total_ingresos: float
    total_gastos: float


class TasaBCV(BaseModel):
    tasa: float
    fecha: str
    fuente: str = "BCV / pydolarve"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "mensaje": "API Finanzas Hollow Knight activa",
        "docs": "/docs",
    }


@app.post(
    "/transacciones", response_model=Transaccion, status_code=status.HTTP_201_CREATED
)
async def crear_transaccion(data: TransaccionCreate):
    """Guarda una nueva transacción en Supabase."""
    try:
        payload = {
            "tipo": data.tipo.lower(),
            "monto_bs": data.monto_bs,
            "descripcion": data.descripcion.strip(),
            "categoria": data.categoria.strip() if data.categoria else None,
        }
        result = supabase.table("transacciones").insert(payload).execute()

        if not result.data:
            raise HTTPException(
                status_code=500,
                detail="No se pudo insertar la transacción",
            )

        row = result.data[0]
        return Transaccion(
            id=str(row["id"]),
            tipo=row["tipo"],
            monto_bs=float(row["monto_bs"]),
            descripcion=row["descripcion"],
            categoria=row.get("categoria"),
            fecha_creacion=row["fecha_creacion"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear: {str(e)}")


@app.get("/transacciones", response_model=List[Transaccion])
async def listar_transacciones():
    """Historial completo ordenado por fecha descendente."""
    try:
        result = (
            supabase.table("transacciones")
            .select("*")
            .order("fecha_creacion", desc=True)
            .execute()
        )
        return [
            Transaccion(
                id=str(r["id"]),
                tipo=r["tipo"],
                monto_bs=float(r["monto_bs"]),
                descripcion=r["descripcion"],
                categoria=r.get("categoria"),
                fecha_creacion=r["fecha_creacion"],
            )
            for r in result.data
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al listar: {str(e)}")


@app.delete("/transacciones/{id}")
async def eliminar_transaccion(id: str):
    """Elimina una transacción por UUID."""
    try:
        result = supabase.table("transacciones").delete().eq("id", id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Transacción no encontrada")
        return {"mensaje": "Transacción eliminada", "id": id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar: {str(e)}")


@app.get("/balance", response_model=BalanceResponse)
async def obtener_balance():
    """Calcula saldo = ingresos - gastos con consulta a Supabase."""
    try:
        result = supabase.table("transacciones").select("tipo, monto_bs").execute()

        total_ingresos = 0.0
        total_gastos = 0.0

        for row in result.data:
            monto = float(row["monto_bs"])
            if row["tipo"] == "ingreso":
                total_ingresos += monto
            else:
                total_gastos += monto

        return BalanceResponse(
            saldo_bs=round(total_ingresos - total_gastos, 2),
            total_ingresos=round(total_ingresos, 2),
            total_gastos=round(total_gastos, 2),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error al calcular balance: {str(e)}"
        )


@app.get("/tasa-bcv", response_model=TasaBCV)
async def obtener_tasa_bcv():
    """
    Obtiene la tasa oficial BCV (Bs/USD) del día.
    Usa la API pública de pydolarve (fuente confiable y mantenida).
    """
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            # Endpoint oficial de pydolarve para tasa BCV
            resp = await client.get("https://pydolarve.org/api/v1/dollar?page=bcv")
            resp.raise_for_status()
            data = resp.json()

            # Estructura típica: {"monitors": {"usd": {"price": 36.50, "last_update": "..."}}}
            monitors = data.get("monitors") or data.get("data") or {}
            usd = monitors.get("usd") or monitors.get("dollar") or {}

            tasa = float(usd.get("price") or usd.get("precio") or 0)
            fecha_raw = (
                usd.get("last_update")
                or usd.get("fecha")
                or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            )

            if tasa <= 0:
                raise ValueError("Tasa inválida recibida de la API")

            # Normalizar fecha a YYYY-MM-DD
            fecha = str(fecha_raw)[:10]

            return TasaBCV(tasa=round(tasa, 4), fecha=fecha, fuente="pydolarve (BCV)")
    except Exception as e:
        # Fallback de emergencia (puedes cambiarlo si falla)
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo obtener la tasa BCV: {str(e)}. Intenta más tarde.",
        )


# Health check útil para Render
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
