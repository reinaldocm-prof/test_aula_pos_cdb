"""
CLOUD: API da nuvem (datacenter remoto).

A nuvem ENXERGA TODAS AS FÁBRICAS, mas está LONGE (WAN de 100 a 300 ms) e
recebe os dados em LOTES (ex.: a cada 30 s). Por isso ela serve para:
  - histórico completo e de longo prazo;
  - análises entre fábricas (dashboards no Grafana);
e NÃO serve para decisões que precisam de resposta em segundos.

Ponto para a aula: compare no Grafana a "idade" dos dados quando chegam
à nuvem. Com a WAN fora, os lotes atrasam, mas nada se perde
(store-and-forward no servidor da fábrica).

Rotas:
  POST /lotes                      recebe um lote do servidor da fábrica
  POST /decidir/superaquecimento   modo "decisão na nuvem" (ANTIPADRÃO didático)
  POST /decidir/demanda            modo "demanda decidida na nuvem"
  GET  /saude                      verificação de funcionamento

As rotas /decidir/... só são chamadas quando o painel move a decisão para a
nuvem. Elas mostram o custo de decidir longe: cada decisão paga o RTT da WAN,
e com a WAN fora simplesmente não há decisão.
"""

import json
import os
import time
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Request

BANCO_URL = os.getenv("BANCO_URL", "postgresql://nuvem:nuvem@nuvem-db:5432/nuvem")

# Mesma regra de demanda do servidor da fábrica (cada serviço tem seu código).
LIMITE_DEMANDA_KW = float(os.getenv("LIMITE_DEMANDA_KW", "110"))
PRIORIDADE_LINHAS = [int(x) for x in os.getenv("PRIORIDADE_LINHAS", "1,2").split(",")]
FATOR_REDUCAO = float(os.getenv("FATOR_REDUCAO", "0.5"))
MARGEM_RESTAURAR = 0.95

ESQUEMA = """
CREATE TABLE IF NOT EXISTS resumos (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,          -- quando o gateway gerou o resumo
    recebido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- quando chegou à nuvem
    origem       TEXT NOT NULL,                 -- qual fábrica (servidor) enviou
    linha        INTEGER NOT NULL,
    consumo_total_kw  REAL,
    temp_max     REAL,
    dados        JSONB
);
CREATE TABLE IF NOT EXISTS alertas (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    recebido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
    origem       TEXT NOT NULL,
    linha        INTEGER NOT NULL,
    maquina      TEXT,
    tipo         TEXT,
    dados        JSONB
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
    for tentativa in range(30):
        try:
            with conectar() as conexao:
                conexao.execute(ESQUEMA)
            print("[nuvem] banco pronto")
            break
        except psycopg.OperationalError:
            time.sleep(1)
    yield


app = FastAPI(title="Nuvem da Fábrica", lifespan=ciclo_de_vida)


@app.get("/saude")
def saude():
    return {"status": "ok"}


@app.post("/lotes")
async def receber_lote(request: Request):
    """Recebe um lote do servidor da fábrica e grava no histórico."""
    corpo = await request.body()
    lote = json.loads(corpo)
    origem = lote["origem"]
    itens = lote["itens"]

    with conectar() as conexao:
        with conexao.transaction():
            for item in itens:
                dados = item["dados"]
                if item["tipo"] == "resumo":
                    temps = [m["temp_max"] for m in dados["maquinas"].values()]
                    conexao.execute(
                        """INSERT INTO resumos (ts, origem, linha, consumo_total_kw, temp_max, dados)
                           VALUES (to_timestamp(%s), %s, %s, %s, %s, %s)""",
                        (dados["ts"], origem, dados["linha"], dados["consumo_total_kw"],
                         max(temps) if temps else None, json.dumps(dados)))
                elif item["tipo"] == "alerta":
                    conexao.execute(
                        """INSERT INTO alertas (ts, origem, linha, maquina, tipo, dados)
                           VALUES (to_timestamp(%s), %s, %s, %s, %s, %s)""",
                        (dados["ts"], origem, dados["linha"], dados.get("maquina"),
                         dados.get("tipo"), json.dumps(dados)))
            conexao.execute("INSERT INTO lotes (origem, itens, bytes) VALUES (%s, %s, %s)",
                            (origem, len(itens), len(corpo)))

    print(f"[nuvem] lote de {origem}: {len(itens)} itens ({len(corpo)} B)")
    # Só depois desta resposta o servidor remove os itens da fila local.
    return {"recebidos": len(itens)}


# -----------------------------------------------------------------------------
# Decisões tomadas na nuvem (modos "nuvem" do painel)
# -----------------------------------------------------------------------------
@app.post("/decidir/superaquecimento")
async def decidir_superaquecimento(request: Request):
    """Recebe o alerta de superaquecimento e devolve o comando de desligar.

    A decisão em si é trivial; o problema é o caminho até aqui e de volta.
    """
    alerta = await request.json()
    print(f"[nuvem] decisão: desligar L{alerta['linha']} {alerta['maquina']}")
    return {"comando": {"acao": "desligar", "maquina": alerta["maquina"], "motivo": "superaquecimento"}}


@app.post("/decidir/demanda")
async def decidir_demanda(request: Request):
    """Recebe o consumo das linhas e as linhas já reduzidas; devolve a decisão."""
    estado = await request.json()
    consumo_linha = {int(k): v for k, v in estado["consumo_linha"].items()}
    reduzidas = estado["reduzidas"]
    total = sum(consumo_linha.values())

    decisao = None
    if total > LIMITE_DEMANDA_KW:
        for candidata in reversed(PRIORIDADE_LINHAS):
            if candidata not in reduzidas:
                decisao = {"linha": candidata,
                           "comando": {"acao": "reduzir_carga", "fator": FATOR_REDUCAO}}
                break
    elif reduzidas:
        candidata = reduzidas[-1]
        atual = consumo_linha.get(candidata, 0.0)
        if total - atual + atual / FATOR_REDUCAO < LIMITE_DEMANDA_KW * MARGEM_RESTAURAR:
            decisao = {"linha": candidata, "comando": {"acao": "restaurar_carga"}}

    if decisao:
        print(f"[nuvem] decisão de demanda: linha {decisao['linha']} {decisao['comando']['acao']}")
    return {"decisao": decisao}
