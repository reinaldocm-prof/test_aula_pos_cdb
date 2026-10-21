"""
FOG: processador do datacenter regional (um por região).

Roda no mesmo nó que o broker da região (mesmo IP, fala com ele por localhost).
O fog ENXERGA TODOS OS CARROS DA REGIÃO (e só dela). Ele:
  1. agrega a velocidade dos carros por trecho, em janelas (ex.: 10 s);
  2. DETECTA CONGESTIONAMENTO: velocidade média baixa com vários carros no
     trecho. Nenhum celular sozinho consegue saber isso;
  3. redistribui ALERTAS na região, em segundos (acidente, congestionamento);
  4. envia resumos à nuvem em lotes, com STORE-AND-FORWARD (SQLite).

ONDE DECIDIR o alerta de acidente (painel, tópico config/modo):
  fog   -> o fog alerta a região assim que recebe o report
  nuvem -> o fog pergunta à nuvem (HTTP) e só alerta quando ela responde
O congestionamento é sempre detectado aqui: só o fog vê a região inteira.

Tópicos MQTT usados:
  regiao/{r}/carro/+/posicao     assina   (1 Hz por carro)
  regiao/{r}/report/acidente     assina   (reports dos motoristas)
  regiao/{r}/carro/+/trilha      assina   (rastro enviado depois de um túnel)
  config/modo                    assina   (onde decidir, retido)
  regiao/{r}/alerta              publica  (para os celulares da região)
  regiao/{r}/transito            publica  (retido: estado de cada trecho)
  viz/eventos                    publica  (painel)
"""

import json
import os
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import paho.mqtt.client as mqtt

REGIAO = os.getenv("REGIAO", "a")
BROKER_HOST = os.getenv("BROKER_HOST", "localhost")   # mesmo nó: localhost
JANELA_S = float(os.getenv("JANELA_S", "10"))
NUVEM_URL = os.getenv("NUVEM_URL", "http://nuvem:8000")
LOTE_S = float(os.getenv("LOTE_S", "30"))
BANCO = os.getenv("BANCO", "/dados/fog.db")

NOME_NO = f"fog-{REGIAO}"
VEL_LIVRE_KMH = 50
MIN_CARROS = 2                    # um carro parado sozinho pode ser só um semáforo
DEDUP_ACIDENTE_S = 60             # vários motoristas reportam o mesmo acidente
TIMEOUT_NUVEM_S = 10              # maior que o RTT máximo da WAN no painel (3000 ms)

modo = {"acidente": "fog"}
tarefas = ThreadPoolExecutor(max_workers=4)


# -----------------------------------------------------------------------------
# Store-and-forward para a nuvem (SQLite)
# -----------------------------------------------------------------------------
class FilaNuvem:

    def __init__(self, caminho):
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        self.conexao = sqlite3.connect(caminho, check_same_thread=False)
        self.trava = threading.Lock()
        with self.trava:
            self.conexao.execute("CREATE TABLE IF NOT EXISTS saida (id INTEGER PRIMARY KEY, tipo TEXT, dados TEXT)")

    def enfileirar(self, tipo, dados):
        with self.trava:
            self.conexao.execute("INSERT INTO saida (tipo, dados) VALUES (?, ?)", (tipo, json.dumps(dados)))
            self.conexao.commit()

    def proximo_lote(self):
        with self.trava:
            return self.conexao.execute("SELECT id, tipo, dados FROM saida ORDER BY id LIMIT 500").fetchall()

    def confirmar(self, ultimo_id):
        with self.trava:
            self.conexao.execute("DELETE FROM saida WHERE id <= ?", (ultimo_id,))
            self.conexao.commit()

    def tamanho(self):
        with self.trava:
            return self.conexao.execute("SELECT COUNT(*) FROM saida").fetchone()[0]


# -----------------------------------------------------------------------------
# Janela de agregação por trecho
# -----------------------------------------------------------------------------
class Janela:

    def __init__(self):
        self.trava = threading.Lock()
        self.reiniciar()

    def reiniciar(self):
        self.velocidades = {}     # trecho -> [km/h, ...]
        self.carros = {}          # trecho -> {carros distintos vistos}
        self.mensagens = 0
        self.bytes = 0

    def adicionar(self, posicao, tamanho):
        with self.trava:
            self.velocidades.setdefault(posicao["trecho"], []).append(posicao["vel_kmh"])
            self.carros.setdefault(posicao["trecho"], set()).add(posicao["carro"])
            self.mensagens += 1
            self.bytes += tamanho

    def fechar(self):
        with self.trava:
            conteudo = (self.velocidades, self.carros, self.mensagens, self.bytes)
            self.reiniciar()
        return conteudo


def classificar(vel_media, carros):
    """Estado do trecho a partir da velocidade média dos carros."""
    if carros < MIN_CARROS:
        return "livre"
    razao = vel_media / VEL_LIVRE_KMH
    if razao < 0.3:
        return "congestionado"
    if razao < 0.6:
        return "lento"
    return "livre"


fila = None
janela = Janela()
estado_trechos = {}            # trecho -> estado anterior
ultimo_alerta_acidente = {}    # trecho -> ts (deduplicação)


# -----------------------------------------------------------------------------
# Comunicação
# -----------------------------------------------------------------------------
def publicar_viz(cliente, tipo, **dados):
    evento = {"ts": time.time(), "no": NOME_NO, "camada": "fog", "regiao": REGIAO, "tipo": tipo, **dados}
    cliente.publish("viz/eventos", json.dumps(evento), qos=0)


def alertar(cliente, tipo, trecho, decidido_em, ts_evento):
    """Alerta para TODOS os celulares da região."""
    alerta = {"ts": time.time(), "ts_evento": ts_evento, "regiao": REGIAO,
              "tipo": tipo, "trecho": trecho, "decidido_em": decidido_em}
    cliente.publish(f"regiao/{REGIAO}/alerta", json.dumps(alerta), qos=1)
    fila.enfileirar("alerta", alerta)
    publicar_viz(cliente, "alerta_publicado", alerta=tipo, trecho=trecho, decidido_em=decidido_em)
    print(f"[{NOME_NO}] ALERTA {tipo} em {trecho} (decidido no {decidido_em})")


def perguntar_a_nuvem(rota, corpo):
    requisicao = urllib.request.Request(f"{NUVEM_URL}{rota}", data=json.dumps(corpo).encode(),
                                        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(requisicao, timeout=TIMEOUT_NUVEM_S) as resposta:
        return json.loads(resposta.read())


def acidente_na_nuvem(cliente, report):
    """Modo nuvem: o fog pergunta; só alerta quando a nuvem responde."""
    for tentativa in range(10):
        try:
            resposta = perguntar_a_nuvem("/decidir/acidente", {**report, "regiao": REGIAO})
            alertar(cliente, resposta["alerta"], report["trecho"], "nuvem", report["ts"])
            return
        except Exception as erro:
            print(f"[{NOME_NO}] nuvem não respondeu ({erro.__class__.__name__}); região sem alerta")
            publicar_viz(cliente, "decisao_nuvem_falhou", trecho=report["trecho"], tentativa=tentativa + 1)
            time.sleep(1)


# -----------------------------------------------------------------------------
# Callbacks MQTT (rodam na thread de rede do paho)
# -----------------------------------------------------------------------------
def ao_conectar(cliente, userdata, flags, reason_code, properties):
    print(f"[{NOME_NO}] conectado ao broker local ({reason_code})")
    cliente.subscribe([(f"regiao/{REGIAO}/carro/+/posicao", 0), (f"regiao/{REGIAO}/report/acidente", 1),
                       (f"regiao/{REGIAO}/carro/+/trilha", 1), ("config/modo", 1)])


def ao_receber(cliente, userdata, msg):
    dados = json.loads(msg.payload)

    if msg.topic.endswith("/posicao"):
        janela.adicionar(dados, len(msg.payload))

    elif msg.topic.endswith("/report/acidente"):
        trecho = dados["trecho"]
        atraso_ms = round((time.time() - dados["ts"]) * 1000, 1)
        publicar_viz(cliente, "report_recebido", carro=dados["carro"], trecho=trecho,
                     atraso_ms=atraso_ms, atrasado=dados.get("atrasado", False))
        # Deduplicação: vários motoristas reportam o mesmo acidente.
        if time.time() - ultimo_alerta_acidente.get(trecho, 0) < DEDUP_ACIDENTE_S:
            return
        ultimo_alerta_acidente[trecho] = time.time()
        if modo["acidente"] == "fog":
            alertar(cliente, "acidente", trecho, "fog", dados["ts"])
        else:
            tarefas.submit(acidente_na_nuvem, cliente, dados)

    elif msg.topic.endswith("/trilha"):
        fila.enfileirar("trilha", {"carro": dados["carro"], "pontos": len(dados["pontos"]), "ts": dados["ts"]})
        publicar_viz(cliente, "trilha_recebida", carro=dados["carro"], pontos=len(dados["pontos"]),
                     bytes=len(msg.payload))

    elif msg.topic == "config/modo":
        modo.update({k: v for k, v in dados.items() if k in modo})
        print(f"[{NOME_NO}] onde decidir: {modo}")


# -----------------------------------------------------------------------------
# Laços periódicos
# -----------------------------------------------------------------------------
def fechar_janelas(cliente):
    """A cada janela: estado de cada trecho; alertas quando o estado muda."""
    while True:
        time.sleep(JANELA_S)
        velocidades, carros, mensagens, bytes_recebidos = janela.fechar()
        trechos = {}
        for trecho, lista in velocidades.items():
            media = sum(lista) / len(lista)
            n_carros = len(carros[trecho])
            trechos[trecho] = {"vel_media": round(media, 1), "amostras": len(lista), "carros": n_carros,
                               "estado": classificar(media, n_carros)}

        transito = {"ts": time.time(), "regiao": REGIAO, "janela_s": JANELA_S, "trechos": trechos}
        payload = json.dumps(transito)
        cliente.publish(f"regiao/{REGIAO}/transito", payload, qos=1, retain=True)
        fila.enfileirar("transito", transito)

        # Mudou de estado? Congestionamento é decidido AQUI, sempre.
        for trecho in set(trechos) | set(estado_trechos):
            novo = trechos.get(trecho, {}).get("estado", "livre")
            antigo = estado_trechos.get(trecho, "livre")
            if novo == "congestionado" and antigo != "congestionado":
                alertar(cliente, "congestionamento", trecho, "fog", None)
            elif antigo == "congestionado" and novo == "livre":
                alertar(cliente, "liberado", trecho, "fog", None)
            estado_trechos[trecho] = novo

        congestionados = [t for t, i in trechos.items() if i["estado"] == "congestionado"]
        publicar_viz(cliente, "janela", mensagens_recebidas=mensagens, bytes_recebidos=bytes_recebidos,
                     bytes_enviados=len(payload), congestionados=congestionados)
        print(f"[{NOME_NO}] janela: {mensagens} posições ({bytes_recebidos} B) -> estado de "
              f"{len(trechos)} trechos ({len(payload)} B); congestionados: {congestionados}")


def enviar_lotes(cliente):
    """Store-and-forward: lote à nuvem; só apaga da fila depois da confirmação."""
    while True:
        time.sleep(LOTE_S)
        lote = fila.proximo_lote()
        if not lote:
            continue
        corpo = json.dumps({"origem": NOME_NO, "regiao": REGIAO,
                            "itens": [{"tipo": t, "dados": json.loads(d)} for _, t, d in lote]}).encode()
        try:
            requisicao = urllib.request.Request(f"{NUVEM_URL}/lotes", data=corpo, method="POST",
                                                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(requisicao, timeout=10).close()
            fila.confirmar(lote[-1][0])
            publicar_viz(cliente, "lote_enviado", itens=len(lote), bytes=len(corpo), fila=fila.tamanho())
            print(f"[{NOME_NO}] lote enviado à nuvem: {len(lote)} itens ({len(corpo)} B)")
        except Exception as erro:
            publicar_viz(cliente, "nuvem_inacessivel", fila=fila.tamanho())
            print(f"[{NOME_NO}] nuvem inacessível ({erro.__class__.__name__}); {fila.tamanho()} itens na fila")


def main():
    global fila
    fila = FilaNuvem(BANCO)

    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NOME_NO)
    cliente.on_connect = ao_conectar
    cliente.on_message = ao_receber
    cliente.reconnect_delay_set(min_delay=1, max_delay=5)
    cliente.connect_async(BROKER_HOST, 1883, keepalive=10)
    cliente.loop_start()

    print(f"[{NOME_NO}] janelas de {JANELA_S} s, lotes à nuvem a cada {LOTE_S} s")
    threading.Thread(target=fechar_janelas, args=(cliente,), daemon=True).start()
    enviar_lotes(cliente)


if __name__ == "__main__":
    main()
