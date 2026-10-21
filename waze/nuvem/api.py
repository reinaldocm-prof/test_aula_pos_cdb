"""
CLOUD: API da nuvem (datacenter distante).

A nuvem ENXERGA TODAS AS REGIÕES, mas está LONGE (WAN) e recebe os dados em
LOTES (ex.: a cada 30 s). Ela serve para o que precisa de visão ampla e
tempo longo:
  - histórico ("essa via trava sexta às 18h");
  - modelos de previsão (ex.: tempo de chegada), comparações entre regiões;
e NÃO serve para avisar, em segundos, quem está chegando a um acidente.

Rotas:
  POST /lotes              recebe um lote de um fog regional
  POST /decidir/acidente   modo "alerta decidido na nuvem"
  GET  /saude              verificação de funcionamento
"""

import json
import os
import time
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request

BANCO_URL = os.getenv("BANCO_URL", "postgresql://nuvem:nuvem@nuvem-db:5432/nuvem")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS trechos (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,          -- fim da janela no fog
    recebido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
    regiao       TEXT NOT NULL,
    trecho       TEXT NOT NULL,
    vel_media    REAL,
    amostras     INTEGER,
    estado       TEXT
);
CREATE TABLE IF NOT EXISTS alertas (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    recebido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
    regiao       TEXT NOT NULL,
    trecho       TEXT,
    tipo         TEXT,
    decidido_em  TEXT
);
CREATE TABLE IF NOT EXISTS trilhas (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    recebido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
    regiao       TEXT NOT NULL,
    carro        TEXT,
    pontos       INTEGER
);
CREATE TABLE IF NOT EXISTS lotes (
    id           BIGSERIAL PRIMARY KEY,
    recebido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
    origem       TEXT NOT NULL,
    itens        INTEGER,
    bytes        INTEGER
);
"""


def conectar():
    return psycopg.connect(BANCO_URL, autocommit=True)


@asynccontextmanager
async def ciclo_de_vida(app):
    # O Postgres pode demorar a aceitar conexões: tenta por até 30 s.
    for _ in range(30):
        try:
            with conectar() as conexao:
                conexao.execute(ESQUEMA)
            print("[nuvem] banco pronto")
            break
        except psycopg.OperationalError:
            time.sleep(1)
    yield


app = FastAPI(title="Nuvem do Waze", lifespan=ciclo_de_vida)


@app.get("/saude")
def saude():
    return {"status": "ok"}


@app.post("/lotes")
async def receber_lote(request: Request):
    """Recebe um lote de um fog regional e grava no histórico."""
    corpo = await request.body()
    lote = json.loads(corpo)
    regiao, itens = lote["regiao"], lote["itens"]

    with conectar() as conexao:
        with conexao.transaction():
            for item in itens:
                d = item["dados"]
                if item["tipo"] == "transito":
                    for trecho, info in d["trechos"].items():
                        conexao.execute(
                            """INSERT INTO trechos (ts, regiao, trecho, vel_media, amostras, estado)
                               VALUES (to_timestamp(%s), %s, %s, %s, %s, %s)""",
                            (d["ts"], regiao, trecho, info["vel_media"], info["amostras"], info["estado"]))
                elif item["tipo"] == "alerta":
                    conexao.execute(
                        """INSERT INTO alertas (ts, regiao, trecho, tipo, decidido_em)
                           VALUES (to_timestamp(%s), %s, %s, %s, %s)""",
                        (d["ts"], regiao, d["trecho"], d["tipo"], d["decidido_em"]))
                elif item["tipo"] == "trilha":
                    conexao.execute(
                        "INSERT INTO trilhas (ts, regiao, carro, pontos) VALUES (to_timestamp(%s), %s, %s, %s)",
                        (d["ts"], regiao, d["carro"], d["pontos"]))
            conexao.execute("INSERT INTO lotes (origem, itens, bytes) VALUES (%s, %s, %s)",
                            (lote["origem"], len(itens), len(corpo)))

    print(f"[nuvem] lote de {lote['origem']}: {len(itens)} itens ({len(corpo)} B)")
    return {"recebidos": len(itens)}


@app.post("/decidir/acidente")
async def decidir_acidente(request: Request):
    """Modo nuvem: o fog pergunta se deve alertar a região sobre um acidente.

    A decisão é trivial; o custo está no caminho até aqui e de volta.
    """
    report = await request.json()
    print(f"[nuvem] decisão: alertar região {report['regiao']} sobre acidente em {report['trecho']}")
    return {"alerta": "acidente"}
