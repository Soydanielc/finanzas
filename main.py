"""
Backend de Finanzas Personales — Finanzas del Caballero (v2.1)
FastAPI + Supabase + Tasa BCV + Multimoneda (BS/USD/USDT) + Módulo de Deudas
"""

import os
from datetime import UTC, datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_KEY.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(
    title="Finanzas del Caballero API",
    description="API personal de ingresos/gastos, deudas (Cashea) y tasas de cambio",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_TASA = {"tasa": 0.0, "fecha": "", "fuente": ""}


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------
class TransaccionCreate(BaseModel):
    tipo: str = Field(..., pattern="^(ingreso|gasto)$")
    moneda: str = Field("BS", pattern="^(BS|USD|USDT)$")
    monto_original: float = Field(..., gt=0)
    tasa_cambio: float | None = Field(1.0, ge=0)
    monto_bs: float = Field(..., gt=0)
    descripcion: str = Field(..., min_length=1, max_length=255)
    categoria: str | None = Field(None, max_length=100)


class Transaccion(BaseModel):
    id: str
    tipo: str
    moneda: str
    monto_original: float
    tasa_cambio: float
    monto_bs: float
    descripcion: str
    categoria: str | None
    fecha_creacion: str


class DeudaCreate(BaseModel):
    acreedor: str = Field(..., min_length=1, max_length=100)
    monto_total_usd: float = Field(..., gt=0)
    fecha_limite: str  # YYYY-MM-DD


class AbonoDeuda(BaseModel):
    monto_usd: float = Field(..., gt=0)


class BalanceResponse(BaseModel):
    saldo_bs: float
    total_ingresos_bs: float
    total_gastos_bs: float
    saldo_usdt_estimado: float
    total_deudas_usd: float


class TasaBCV(BaseModel):
    tasa: float
    fecha: str
    fuente: str


# ---------------------------------------------------------------------------
# Consulta Tasa BCV
# ---------------------------------------------------------------------------
async def consultar_dolarapi(client: httpx.AsyncClient) -> TasaBCV:
    resp = await client.get("https://ve.dolarapi.com/v1/dolares/oficial")
    resp.raise_for_status()
    data = resp.json()
    tasa = float(data.get("promedio", 0))
    fecha_raw = data.get("fechaActualizacion", datetime.now(UTC).strftime("%Y-%m-%d"))
    if tasa <= 0:
        raise ValueError("Tasa inválida")
    return TasaBCV(
        tasa=round(tasa, 4), fecha=str(fecha_raw)[:10], fuente="BCV (DolarApi)"
    )


async def consultar_pydolarve(client: httpx.AsyncClient) -> TasaBCV:
    resp = await client.get("https://pydolarve.org/api/v1/dollar?page=bcv")
    resp.raise_for_status()
    data = resp.json()
    monitors = data.get("monitors") or data.get("data") or {}
    usd = monitors.get("usd") or monitors.get("dollar") or {}
    tasa = float(usd.get("price") or usd.get("precio") or 0)
    fecha_raw = usd.get("last_update") or datetime.now(UTC).strftime("%Y-%m-%d")
    if tasa <= 0:
        raise ValueError("Tasa inválida")
    return TasaBCV(
        tasa=round(tasa, 4), fecha=str(fecha_raw)[:10], fuente="BCV (PyDolarVe)"
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"mensaje": "API Finanzas del Caballero v2.1 Activa", "docs": "/docs"}


@app.get("/tasa-bcv", response_model=TasaBCV)
async def obtener_tasa_bcv():
    global CACHE_TASA
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await consultar_dolarapi(client)
            CACHE_TASA = res.model_dump()
            return res
        except Exception:
            pass

        try:
            res = await consultar_pydolarve(client)
            CACHE_TASA = res.model_dump()
            return res
        except Exception:
            pass

    if CACHE_TASA["tasa"] > 0:
        return TasaBCV(
            tasa=CACHE_TASA["tasa"],
            fecha=CACHE_TASA["fecha"],
            fuente=f"{CACHE_TASA['fuente']} (Caché)",
        )

    raise HTTPException(status_code=503, detail="No se pudo obtener la tasa BCV.")


@app.get("/balance", response_model=BalanceResponse)
async def obtener_balance():
    try:
        res_trans = supabase.table("transacciones").select("*").execute()
        total_ingresos_bs = 0.0
        total_gastos_bs = 0.0
        usdt_balance = 0.0

        for row in res_trans.data:
            m_bs = float(row["monto_bs"])
            m_orig = float(row["monto_original"])
            moneda = row.get("moneda", "BS")

            if row["tipo"] == "ingreso":
                total_ingresos_bs += m_bs
                if moneda == "USDT":
                    usdt_balance += m_orig
            else:
                total_gastos_bs += m_bs
                if moneda == "USDT":
                    usdt_balance -= m_orig

        res_deudas = (
            supabase.table("deudas").select("*").eq("completado", False).execute()
        )
        deudas_usd = sum(
            float(d["monto_total_usd"]) - float(d["monto_pagado_usd"])
            for d in res_deudas.data
        )

        return BalanceResponse(
            saldo_bs=round(total_ingresos_bs - total_gastos_bs, 2),
            total_ingresos_bs=round(total_ingresos_bs, 2),
            total_gastos_bs=round(total_gastos_bs, 2),
            saldo_usdt_estimado=round(usdt_balance, 2),
            total_deudas_usd=round(deudas_usd, 2),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al calcular balance: {e!s}")


@app.post(
    "/transacciones", response_model=Transaccion, status_code=status.HTTP_201_CREATED
)
async def crear_transaccion(data: TransaccionCreate):
    try:
        payload = {
            "tipo": data.tipo.lower(),
            "moneda": data.moneda,
            "monto_original": data.monto_original,
            "tasa_cambio": data.tasa_cambio,
            "monto_bs": data.monto_bs,
            "descripcion": data.descripcion.strip(),
            "categoria": data.categoria.strip() if data.categoria else None,
        }
        result = supabase.table("transacciones").insert(payload).execute()
        row = result.data[0]
        return Transaccion(
            id=str(row["id"]),
            tipo=row["tipo"],
            moneda=row["moneda"],
            monto_original=float(row["monto_original"]),
            tasa_cambio=float(row["tasa_cambio"]),
            monto_bs=float(row["monto_bs"]),
            descripcion=row["descripcion"],
            categoria=row.get("categoria"),
            fecha_creacion=row["fecha_creacion"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear: {e!s}")


@app.get("/transacciones", response_model=list[Transaccion])
async def listar_transacciones():
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
                moneda=r.get("moneda", "BS"),
                monto_original=float(r.get("monto_original", r["monto_bs"])),
                tasa_cambio=float(r.get("tasa_cambio", 1.0)),
                monto_bs=float(r["monto_bs"]),
                descripcion=r["descripcion"],
                categoria=r.get("categoria"),
                fecha_creacion=r["fecha_creacion"],
            )
            for r in result.data
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al listar: {e!s}")


@app.delete("/transacciones/{id}")
async def eliminar_transaccion(id: str):
    try:
        result = supabase.table("transacciones").delete().eq("id", id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="No encontrada")
        return {"mensaje": "Transacción eliminada", "id": id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar: {e!s}")


# ---------------------------------------------------------------------------
# Módulo de Deudas (Cashea)
# ---------------------------------------------------------------------------
@app.get("/deudas")
async def listar_deudas():
    try:
        result = (
            supabase.table("deudas")
            .select("*")
            .order("fecha_limite", desc=False)
            .execute()
        )
        return result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al listar deudas: {e!s}")


@app.post("/deudas", status_code=status.HTTP_201_CREATED)
async def crear_deuda(data: DeudaCreate):
    try:
        payload = {
            "acreedor": data.acreedor.strip(),
            "monto_total_usd": data.monto_total_usd,
            "monto_pagado_usd": 0.0,
            "fecha_limite": data.fecha_limite,
            "completado": False,
        }
        result = supabase.table("deudas").insert(payload).execute()
        return result.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear deuda: {e!s}")


@app.post("/deudas/{deuda_id}/abonar")
async def abonar_deuda(deuda_id: str, abono: AbonoDeuda):
    try:
        res = supabase.table("deudas").select("*").eq("id", deuda_id).single().execute()
        deuda = res.data
        if not deuda:
            raise HTTPException(status_code=404, detail="Deuda no encontrada")

        nuevo_pagado = float(deuda["monto_pagado_usd"]) + abono.monto_usd
        total = float(deuda["monto_total_usd"])
        completado = nuevo_pagado >= total

        supabase.table("deudas").update(
            {"monto_pagado_usd": nuevo_pagado, "completado": completado}
        ).eq("id", deuda_id).execute()

        return {
            "mensaje": "Abono registrado",
            "restante_usd": max(0.0, total - nuevo_pagado),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al abonar: {e!s}")


@app.delete("/deudas/{deuda_id}")
async def eliminar_deuda(deuda_id: str):
    try:
        supabase.table("deudas").delete().eq("id", deuda_id).execute()
        return {"mensaje": "Deuda eliminada"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al eliminar deuda: {e!s}")
