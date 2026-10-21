"""
FOG (nível superior): coordenador do servidor da fábrica.

Roda no mesmo nó que o broker da fábrica (mesmo IP, fala com ele por localhost).
Os resumos e alertas das linhas chegam aqui pelas bridges MQTT dos gateways.

O servidor ENXERGA TODAS AS LINHAS. É o único lugar que conhece o consumo
total da fábrica, por isso é aqui que se controla a DEMANDA DE ENERGIA:
  - cada linha só sabe o próprio consumo;
  - se o total passa do limite contratado, o servidor manda a linha de menor
    prioridade reduzir a carga, em segundos, SEM DEPENDER DA WAN;
  - quando há folga, restaura a linha.

Além disso:
  - guarda o histórico localmente (SQLite), "armazena dias";
  - envia lotes à nuvem com STORE-AND-FORWARD: se a nuvem estiver fora,
    os itens ficam numa fila em disco e são enviados quando ela voltar.

ONDE DECIDIR (configurado pelo painel, tópico config/modo):
  superaquecimento: edge     -> a própria máquina desliga (servidor só registra)
                    servidor -> o servidor manda desligar ao receber o alerta
                    nuvem    -> o servidor pergunta à nuvem (HTTP) e repassa a resposta
  demanda:          servidor -> decisão local, a cada resumo
                    nuvem    -> o servidor envia o estado à nuvem e aplica a resposta
O modo "nuvem" existe para mostrar o custo (e o risco) de decidir longe.

Tópicos MQTT usados:
  fabrica/+/resumo           assina   (LOTE das linhas, a cada janela)
  fabrica/+/alerta           assina   (EVENTO das linhas, imediato)
  config/modo                assina   (onde decidir, retido)
  fabrica/linha{n}/comando   publica  (desce pela bridge até as máquinas)
  viz/eventos                publica  (painel)
"""

import json
import os
import sqlite3
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import paho.mqtt.client as mqtt

BROKER_HOST = os.getenv("BROKER_HOST", "localhost")  # mesmo nó: localhost
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))

# Controle de demanda
LIMITE_DEMANDA_KW = float(os.getenv("LIMITE_DEMANDA_KW", "110"))
# Ordem de prioridade: a primeira é a mais importante; a última é cortada antes.
PRIORIDADE_LINHAS = [int(x) for x in os.getenv("PRIORIDADE_LINHAS", "1,2").split(",")]
FATOR_REDUCAO = float(os.getenv("FATOR_REDUCAO", "0.5"))
MARGEM_RESTAURAR = 0.95   # só restaura se o total estimado ficar abaixo de 95% do limite
ESPERA_ENTRE_ACOES_S = float(os.getenv("ESPERA_ENTRE_ACOES_S", "10"))  # efeito leva ~1 janela

# Nuvem: lotes (store-and-forward) e decisões (modo nuvem)
NUVEM_URL = os.getenv("NUVEM_URL", "http://nuvem:8000")
LOTE_S = float(os.getenv("LOTE_S", "30"))
MAX_ITENS_POR_LOTE = 500
TIMEOUT_NUVEM_S = 10              # maior que o RTT máximo da WAN no painel (3000 ms)
BANCO = os.getenv("BANCO", "/dados/servidor.db")

NOME_NO = "servidor-fabrica"

# Onde decidir (atualizado pelo tópico config/modo)
modo = {"superaquecimento": "edge", "demanda": "servidor"}

# Chamadas à nuvem rodam fora da thread do MQTT: a WAN pode levar segundos.
tarefas = ThreadPoolExecutor(max_workers=4)

# Máquinas aguardando decisão da nuvem: (linha, maquina). Sai daqui quando a
# máquina desliga ou se danifica, para não mandar um comando já obsoleto.
aguardando_nuvem = set()


# -----------------------------------------------------------------------------
# Armazenamento local (SQLite)
# -----------------------------------------------------------------------------
# Duas tabelas:
#   historico: tudo o que a fábrica viu (fica no servidor, "armazena dias");
#   saida:     fila de store-and-forward, o que ainda não chegou à nuvem.
class Armazenamento:

    def __init__(self, caminho):
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        # Usado por várias threads: uma trava protege a conexão.
        self.conexao = sqlite3.connect(caminho, check_same_thread=False)
        self.trava = threading.Lock()
        with self.trava:
            self.conexao.executescript("""
                CREATE TABLE IF NOT EXISTS historico (
                    id INTEGER PRIMARY KEY, ts REAL, tipo TEXT, linha INTEGER, dados TEXT);
                CREATE TABLE IF NOT EXISTS saida (
                    id INTEGER PRIMARY KEY, tipo TEXT, dados TEXT);
            """)

    def registrar(self, tipo, linha, dados):
        """Guarda no histórico local E enfileira para a nuvem."""
        texto = json.dumps(dados)
        with self.trava:
            self.conexao.execute("INSERT INTO historico (ts, tipo, linha, dados) VALUES (?, ?, ?, ?)",
                                 (time.time(), tipo, linha, texto))
            self.conexao.execute("INSERT INTO saida (tipo, dados) VALUES (?, ?)", (tipo, texto))
            self.conexao.commit()

    def proximo_lote(self):
        with self.trava:
            return self.conexao.execute("SELECT id, tipo, dados FROM saida ORDER BY id LIMIT ?",
                                        (MAX_ITENS_POR_LOTE,)).fetchall()

    def confirmar_envio(self, ultimo_id):
        """A nuvem confirmou: agora sim, remove da fila."""
        with self.trava:
            self.conexao.execute("DELETE FROM saida WHERE id <= ?", (ultimo_id,))
            self.conexao.commit()

    def tamanho_fila(self):
        with self.trava:
            return self.conexao.execute("SELECT COUNT(*) FROM saida").fetchone()[0]


# -----------------------------------------------------------------------------
# Controle de demanda de energia
# -----------------------------------------------------------------------------
def decidir_demanda(consumo_linha, reduzidas):
    """A regra de decisão (a mesma que a nuvem usa no modo nuvem).

    Devolve (linha, comando) ou None.
    """
    total = sum(consumo_linha.values())

    # 1) Acima do limite: corta a linha de menor prioridade ainda não cortada.
    if total > LIMITE_DEMANDA_KW:
        for candidata in reversed(PRIORIDADE_LINHAS):
            if candidata not in reduzidas:
                return candidata, {"acao": "reduzir_carga", "fator": FATOR_REDUCAO}

    # 2) Com folga: restaura a última linha cortada, se couber.
    elif reduzidas:
        candidata = reduzidas[-1]
        atual = consumo_linha.get(candidata, 0.0)
        # Quanto a linha consumiria sem a redução:
        total_estimado = total - atual + atual / FATOR_REDUCAO
        if total_estimado < LIMITE_DEMANDA_KW * MARGEM_RESTAURAR:
            return candidata, {"acao": "restaurar_carga"}

    return None


class ControleDemanda:
    """Estado do controle de demanda, visto pelo servidor da fábrica."""

    def __init__(self):
        self.trava = threading.Lock()
        self.consumo_linha = {}      # linha -> último consumo total informado (kW)
        self.reduzidas = []          # linhas com carga reduzida, na ordem do corte
        self.ultima_acao = 0.0
        self.ts_excedeu = None       # desde quando o total está acima do limite
        self.consulta_pendente = False  # modo nuvem: uma consulta por vez

    def registrar_consumo(self, linha, consumo_kw):
        with self.trava:
            self.consumo_linha[linha] = consumo_kw
            total = sum(self.consumo_linha.values())
            if total > LIMITE_DEMANDA_KW:
                self.ts_excedeu = self.ts_excedeu or time.time()
            else:
                self.ts_excedeu = None
            return total

    def pode_agir(self):
        # O efeito de um comando só aparece no resumo seguinte: espera.
        return time.time() - self.ultima_acao >= ESPERA_ENTRE_ACOES_S

    def retrato(self):
        with self.trava:
            return dict(self.consumo_linha), list(self.reduzidas)

    def aplicar(self, linha, comando):
        """Registra a decisão e devolve há quanto tempo o limite estava estourado."""
        with self.trava:
            if comando["acao"] == "reduzir_carga" and linha not in self.reduzidas:
                self.reduzidas.append(linha)
            elif comando["acao"] == "restaurar_carga" and linha in self.reduzidas:
                self.reduzidas.remove(linha)
            self.ultima_acao = time.time()
            if comando["acao"] == "reduzir_carga" and self.ts_excedeu:
                return round(time.time() - self.ts_excedeu, 1)
            return None


armazenamento = None
demanda = ControleDemanda()


# -----------------------------------------------------------------------------
# Comunicação
# -----------------------------------------------------------------------------
def publicar_viz(cliente, tipo, dados):
    evento = {"ts": time.time(), "no": NOME_NO, "camada": "fog", "tipo": tipo, **dados}
    cliente.publish("viz/eventos", json.dumps(evento), qos=0)


def enviar_comando(cliente, linha, comando, decidido_em, **extra):
    comando = {**comando, "decidido_em": decidido_em, "ts": time.time()}
    cliente.publish(f"fabrica/linha{linha}/comando", json.dumps(comando), qos=1)
    publicar_viz(cliente, "comando_enviado", {"linha": linha, "comando": comando, **extra})
    print(f"[{NOME_NO}] COMANDO para linha {linha}: {comando['acao']} "
          f"{comando.get('maquina', '')} (decidido em {decidido_em})")


def perguntar_a_nuvem(rota, corpo):
    """POST síncrono à nuvem, através da WAN (Toxiproxy)."""
    requisicao = urllib.request.Request(f"{NUVEM_URL}{rota}", data=json.dumps(corpo).encode(),
                                        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(requisicao, timeout=TIMEOUT_NUVEM_S) as resposta:
        return json.loads(resposta.read())


# -----------------------------------------------------------------------------
# Decisões
# -----------------------------------------------------------------------------
def superaquecimento_na_nuvem(cliente, alerta):
    """Modo nuvem: pergunta à nuvem o que fazer. Tenta de novo enquanto a
    máquina ainda pode ser salva (de 90 a 110 °C são ~10 s)."""
    linha, maquina = alerta["linha"], alerta["maquina"]
    aguardando_nuvem.add((linha, maquina))
    for tentativa in range(12):
        if (linha, maquina) not in aguardando_nuvem:
            return   # a máquina já desligou ou se danificou: desiste
        try:
            resposta = perguntar_a_nuvem("/decidir/superaquecimento", alerta)
            if (linha, maquina) in aguardando_nuvem:
                enviar_comando(cliente, linha, resposta["comando"], "nuvem")
            aguardando_nuvem.discard((linha, maquina))
            return
        except Exception as erro:
            print(f"[{NOME_NO}] nuvem não respondeu ({erro.__class__.__name__}); "
                  f"L{linha} {maquina} segue sem comando de desligamento")
            publicar_viz(cliente, "decisao_nuvem_falhou",
                         {"linha": linha, "maquina": maquina, "tentativa": tentativa + 1})
            time.sleep(1)


def demanda_na_nuvem(cliente):
    """Modo nuvem: envia o estado à nuvem e aplica a decisão que voltar."""
    try:
        consumo_linha, reduzidas = demanda.retrato()
        resposta = perguntar_a_nuvem("/decidir/demanda",
                                     {"consumo_linha": consumo_linha, "reduzidas": reduzidas})
        decisao = resposta.get("decisao")
        if decisao and modo["demanda"] == "nuvem":
            linha, comando = decisao["linha"], decisao["comando"]
            atraso = demanda.aplicar(linha, comando)
            enviar_comando(cliente, linha, comando, "nuvem", acima_do_limite_s=atraso)
    except Exception as erro:
        print(f"[{NOME_NO}] nuvem não respondeu à consulta de demanda ({erro.__class__.__name__})")
        publicar_viz(cliente, "decisao_nuvem_falhou", {"assunto": "demanda"})
    finally:
        demanda.consulta_pendente = False


def tratar_resumo(cliente, linha, dados):
    armazenamento.registrar("resumo", linha, dados)
    total = demanda.registrar_consumo(linha, dados["consumo_total_kw"])
    consumo_linha, reduzidas = demanda.retrato()
    acima = round(time.time() - demanda.ts_excedeu, 1) if demanda.ts_excedeu else 0
    publicar_viz(cliente, "demanda", {
        "total_kw": round(total, 2), "limite_kw": LIMITE_DEMANDA_KW,
        "linhas_kw": consumo_linha, "linhas_reduzidas": reduzidas,
        "acima_do_limite_s": acima, "decidido_em": modo["demanda"],
    })
    print(f"[{NOME_NO}] demanda: {total:.1f} / {LIMITE_DEMANDA_KW:.0f} kW "
          f"(linhas: {consumo_linha}, reduzidas: {reduzidas}, decide: {modo['demanda']})")

    if not demanda.pode_agir():
        return
    if modo["demanda"] == "servidor":
        decisao = decidir_demanda(consumo_linha, reduzidas)
        if decisao:
            atraso = demanda.aplicar(*decisao)
            enviar_comando(cliente, *decisao, "servidor", acima_do_limite_s=atraso)
    elif not demanda.consulta_pendente:
        demanda.consulta_pendente = True
        tarefas.submit(demanda_na_nuvem, cliente)


def tratar_alerta(cliente, linha, dados):
    armazenamento.registrar("alerta", linha, dados)
    atraso_ms = (time.time() - dados["ts"]) * 1000
    print(f"[{NOME_NO}] ALERTA linha {linha}: {dados['maquina']} {dados['tipo']} "
          f"(máquina -> servidor: {atraso_ms:.1f} ms)")

    if dados["tipo"] in ("desligada", "danificada"):
        aguardando_nuvem.discard((linha, dados["maquina"]))
        return
    if dados["tipo"] != "superaquecimento_detectado":
        return
    if modo["superaquecimento"] == "servidor":
        enviar_comando(cliente, linha, {"acao": "desligar", "maquina": dados["maquina"],
                                        "motivo": "superaquecimento"}, "servidor")
    elif modo["superaquecimento"] == "nuvem":
        tarefas.submit(superaquecimento_na_nuvem, cliente, dados)
    # modo edge: a máquina já desligou sozinha; aqui só registramos.


# -----------------------------------------------------------------------------
# Callbacks MQTT (rodam na thread de rede do paho)
# -----------------------------------------------------------------------------
def ao_conectar(cliente, userdata, flags, reason_code, properties):
    print(f"[{NOME_NO}] conectado ao broker local ({reason_code})")
    cliente.subscribe([("fabrica/+/resumo", 1), ("fabrica/+/alerta", 1), ("config/modo", 1)])


def ao_receber(cliente, userdata, msg):
    dados = json.loads(msg.payload)
    if msg.topic == "config/modo":
        modo.update({k: v for k, v in dados.items() if k in modo})
        print(f"[{NOME_NO}] onde decidir: {modo}")
    elif msg.topic.endswith("/resumo"):
        tratar_resumo(cliente, dados["linha"], dados)
    elif msg.topic.endswith("/alerta"):
        tratar_alerta(cliente, dados["linha"], dados)


# -----------------------------------------------------------------------------
# Store-and-forward: envio periódico de lotes à nuvem
# -----------------------------------------------------------------------------
def enviar_lotes(cliente):
    while True:
        time.sleep(LOTE_S)
        lote = armazenamento.proximo_lote()
        if not lote:
            continue
        corpo = json.dumps({
            "origem": NOME_NO,
            "itens": [{"tipo": tipo, "dados": json.loads(dados)} for _, tipo, dados in lote],
        }).encode()
        try:
            requisicao = urllib.request.Request(f"{NUVEM_URL}/lotes", data=corpo, method="POST",
                                                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(requisicao, timeout=10).close()
            # Só remove da fila DEPOIS que a nuvem confirmou o recebimento.
            armazenamento.confirmar_envio(lote[-1][0])
            print(f"[{NOME_NO}] lote enviado à nuvem: {len(lote)} itens ({len(corpo)} B)")
            publicar_viz(cliente, "lote_enviado", {"itens": len(lote), "bytes": len(corpo),
                                                   "fila": armazenamento.tamanho_fila()})
        except Exception as erro:
            fila = armazenamento.tamanho_fila()
            print(f"[{NOME_NO}] nuvem inacessível ({erro.__class__.__name__}); "
                  f"{fila} itens aguardando na fila local")
            publicar_viz(cliente, "nuvem_inacessivel", {"fila": fila})


def main():
    global armazenamento
    armazenamento = Armazenamento(BANCO)

    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NOME_NO)
    cliente.on_connect = ao_conectar
    cliente.on_message = ao_receber
    cliente.reconnect_delay_set(min_delay=1, max_delay=5)
    cliente.connect_async(BROKER_HOST, BROKER_PORT, keepalive=10)
    cliente.loop_start()

    print(f"[{NOME_NO}] limite de demanda {LIMITE_DEMANDA_KW} kW, prioridade das linhas "
          f"{PRIORIDADE_LINHAS}, lotes à nuvem a cada {LOTE_S} s "
          f"({armazenamento.tamanho_fila()} itens já na fila)")

    enviar_lotes(cliente)


if __name__ == "__main__":
    main()
