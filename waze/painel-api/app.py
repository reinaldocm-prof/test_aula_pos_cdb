"""
painel-api: backend de controle da demonstração.

O painel (no navegador) chama esta API para manipular os enlaces de rede:
derrubar/religar, mudar a latência e limitar a banda.

Por que não chamar o Toxiproxy direto do navegador? A API do Toxiproxy não
envia cabeçalhos CORS, então o navegador bloqueia as chamadas. Esta API faz
a ponte e ainda traduz para termos da aula (RTT em ms, banda em kbps).

Enlaces controlados (ver toxiproxy/toxiproxy.json):
  celular_a    celulares da região A -> fog-a   (rede 4G/5G da região inteira)
  celular_b    celulares da região B -> fog-b   (rede 4G/5G da região inteira)
  wan          fogs regionais        -> nuvem   (WAN, lotes e decisões HTTP)

Rotas:
  GET  /api/enlaces              estado de todos os enlaces
  POST /api/enlaces/{nome}       altera um enlace: {"ativo", "rtt_ms", "banda_kbps"}
  POST /api/enlaces/restaurar    volta todos ao perfil padrão
"""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

TOXIPROXY_URL = os.getenv("TOXIPROXY_URL", "http://toxiproxy:8474")

# Perfil padrão de cada enlace (RTT = ida e volta).
ENLACES = {
    "celular_a": {"descricao": "Celulares da região A → fog-a (4G/5G)", "rtt_ms": 30},
    "celular_b": {"descricao": "Celulares da região B → fog-b (4G/5G)", "rtt_ms": 30},
    "wan":       {"descricao": "Fogs regionais → nuvem (WAN)", "rtt_ms": 250},
}

toxiproxy = httpx.AsyncClient(base_url=TOXIPROXY_URL, timeout=5)


# -----------------------------------------------------------------------------
# Operações no Toxiproxy
# -----------------------------------------------------------------------------
# No Toxiproxy, um "tóxico" é uma degradação aplicada a um proxy, em um
# sentido do tráfego (upstream = ida, downstream = volta).

async def definir_toxico(enlace, nome, tipo, sentido, atributos):
    """Cria ou substitui um tóxico."""
    await toxiproxy.delete(f"/proxies/{enlace}/toxics/{nome}")
    resposta = await toxiproxy.post(f"/proxies/{enlace}/toxics", json={
        "name": nome, "type": tipo, "stream": sentido, "attributes": atributos})
    resposta.raise_for_status()


async def remover_toxico(enlace, nome):
    await toxiproxy.delete(f"/proxies/{enlace}/toxics/{nome}")


async def definir_rtt(enlace, rtt_ms):
    """RTT dividido entre ida e volta, com variação (jitter) de 10%."""
    metade = max(0, rtt_ms // 2)
    jitter = metade // 10
    for sentido in ("upstream", "downstream"):
        await definir_toxico(enlace, f"latencia_{sentido}", "latency", sentido,
                             {"latency": metade, "jitter": jitter})


async def definir_banda(enlace, banda_kbps):
    """Limite de banda nos dois sentidos (Toxiproxy usa KB/s)."""
    for sentido in ("upstream", "downstream"):
        if banda_kbps:
            await definir_toxico(enlace, f"banda_{sentido}", "bandwidth", sentido,
                                 {"rate": max(1, banda_kbps // 8)})
        else:
            await remover_toxico(enlace, f"banda_{sentido}")


async def definir_ativo(enlace, ativo):
    """Derruba ou religa o enlace (conexões abertas são encerradas)."""
    resposta = await toxiproxy.patch(f"/proxies/{enlace}", json={"enabled": ativo})
    resposta.raise_for_status()


async def aplicar_perfil_padrao():
    for enlace, perfil in ENLACES.items():
        await definir_ativo(enlace, True)
        await definir_rtt(enlace, perfil["rtt_ms"])
        await definir_banda(enlace, None)


# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------
@asynccontextmanager
async def ciclo_de_vida(app):
    # O Toxiproxy pode ainda estar subindo: tenta por até 30 s.
    for _ in range(30):
        try:
            await aplicar_perfil_padrao()
            print("[painel-api] perfil padrão aplicado aos enlaces")
            break
        except httpx.HTTPError:
            await asyncio.sleep(1)
    yield


app = FastAPI(title="painel-api", lifespan=ciclo_de_vida)


class AlteracaoEnlace(BaseModel):
    ativo: bool | None = None
    rtt_ms: int | None = None
    banda_kbps: int | None = None   # 0 = sem limite


@app.get("/api/enlaces")
async def listar_enlaces():
    proxies = (await toxiproxy.get("/proxies")).json()
    resultado = []
    for nome, perfil in ENLACES.items():
        proxy = proxies.get(nome, {})
        toxicos = {t["name"]: t["attributes"] for t in proxy.get("toxics", [])}
        latencia = sum(toxicos.get(f"latencia_{s}", {}).get("latency", 0)
                       for s in ("upstream", "downstream"))
        banda = toxicos.get("banda_upstream", {}).get("rate")
        resultado.append({
            "nome": nome,
            "descricao": perfil["descricao"],
            "ativo": proxy.get("enabled", False),
            "rtt_ms": latencia,
            "banda_kbps": banda * 8 if banda else 0,
        })
    return resultado


@app.post("/api/enlaces/restaurar")
async def restaurar():
    await aplicar_perfil_padrao()
    print("[painel-api] enlaces restaurados ao perfil padrão")
    return await listar_enlaces()


@app.post("/api/enlaces/{nome}")
async def alterar_enlace(nome: str, alteracao: AlteracaoEnlace):
    if nome not in ENLACES:
        raise HTTPException(404, f"enlace desconhecido: {nome}")
    if alteracao.ativo is not None:
        await definir_ativo(nome, alteracao.ativo)
    if alteracao.rtt_ms is not None:
        await definir_rtt(nome, alteracao.rtt_ms)
    if alteracao.banda_kbps is not None:
        await definir_banda(nome, alteracao.banda_kbps)
    print(f"[painel-api] {nome}: {alteracao.model_dump(exclude_none=True)}")
    return await listar_enlaces()
