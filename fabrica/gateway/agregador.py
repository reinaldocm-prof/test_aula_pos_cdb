"""
FOG (nível inferior): agregador do gateway de uma linha de produção.

Roda no mesmo nó que o broker da linha (mesmo IP, fala com ele por localhost).
O gateway ENXERGA A LINHA INTEIRA, mas só ela. Ele:
  1. recebe a telemetria bruta das máquinas (alta frequência);
  2. LOTES: a cada janela (ex.: 5 s), resume a linha e publica o resumo;
  3. EVENTOS: encaminha alertas das máquinas NA HORA, sem esperar a janela;
  4. COMANDOS: traduz comandos da fábrica para as máquinas da linha.

Ponto para a aula: compare o volume que entra (telemetria bruta) com o que
sai (resumo). É o fog economizando banda do enlace acima.

Separação de "mundos" pelos tópicos:
  linha{n}/...   fica DENTRO da linha (nunca entra na bridge)
  fabrica/linha{n}/...   é o que sobe/desce entre a linha e o servidor da fábrica
                         (no passo 3, só este prefixo atravessa a bridge)

Tópicos MQTT usados:
  linha{n}/maquina/+/telemetria   assina   (bruto, alta frequência)
  linha{n}/maquina/+/evento       assina   (eventos das máquinas)
  fabrica/linha{n}/resumo         publica  (LOTE, a cada janela)
  fabrica/linha{n}/alerta         publica  (EVENTO, imediato)
  fabrica/linha{n}/comando        assina   (vem do servidor da fábrica)
  linha{n}/comando                publica  (repassa às máquinas)
  viz/eventos                     publica  (painel)
"""

import json
import os
import threading
import time

import paho.mqtt.client as mqtt

LINHA = int(os.getenv("LINHA", "1"))
BROKER_HOST = os.getenv("BROKER_HOST", "localhost")  # mesmo nó: localhost
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))
JANELA_S = float(os.getenv("JANELA_S", "5"))

NOME_NO = f"gateway-linha{LINHA}"

# Eventos das máquinas que viram alerta para o servidor da fábrica.
EVENTOS_QUE_SOBEM = {"superaquecimento_detectado", "desligada", "danificada"}

TOPICO_TELEMETRIA = f"linha{LINHA}/maquina/+/telemetria"
TOPICO_EVENTO = f"linha{LINHA}/maquina/+/evento"
TOPICO_COMANDO_FABRICA = f"fabrica/linha{LINHA}/comando"


class Janela:
    """Acumula a telemetria de uma janela de tempo (protegida por trava)."""

    def __init__(self):
        self.trava = threading.Lock()
        self.reiniciar()

    def reiniciar(self):
        self.inicio = time.time()
        self.leituras = {}        # maquina -> lista de leituras
        self.mensagens = 0
        self.bytes_recebidos = 0

    def adicionar(self, leitura, tamanho):
        with self.trava:
            self.leituras.setdefault(leitura["maquina"], []).append(leitura)
            self.mensagens += 1
            self.bytes_recebidos += tamanho

    def fechar(self):
        """Devolve o conteúdo da janela e começa outra."""
        with self.trava:
            conteudo = (self.inicio, self.leituras, self.mensagens, self.bytes_recebidos)
            self.reiniciar()
        return conteudo


janela = Janela()


def resumir(inicio, leituras):
    """Transforma centenas de leituras num resumo pequeno da linha."""
    maquinas = {}
    for id_maquina, lista in sorted(leituras.items()):
        temperaturas = [l["temperatura"] for l in lista]
        consumos = [l["consumo_kw"] for l in lista]
        maquinas[id_maquina] = {
            "estado": lista[-1]["estado"],          # último estado visto
            "temp_media": round(sum(temperaturas) / len(temperaturas), 1),
            "temp_max": round(max(temperaturas), 1),
            "consumo_medio_kw": round(sum(consumos) / len(consumos), 2),
        }
    return {
        "ts": time.time(),
        "linha": LINHA,
        "janela_s": round(time.time() - inicio, 1),
        "maquinas": maquinas,
        # O que o servidor da fábrica precisa para controlar a demanda:
        "consumo_total_kw": round(sum(m["consumo_medio_kw"] for m in maquinas.values()), 2),
    }


def publicar_viz(cliente, tipo, dados):
    evento = {"ts": time.time(), "no": NOME_NO, "camada": "fog", "tipo": tipo, **dados}
    cliente.publish("viz/eventos", json.dumps(evento), qos=0)


# -----------------------------------------------------------------------------
# Callbacks MQTT (rodam na thread de rede do paho)
# -----------------------------------------------------------------------------
def ao_conectar(cliente, userdata, flags, reason_code, properties):
    print(f"[{NOME_NO}] conectado ao broker local ({reason_code})")
    cliente.subscribe([(TOPICO_TELEMETRIA, 0), (TOPICO_EVENTO, 1), (TOPICO_COMANDO_FABRICA, 1)])


def ao_receber(cliente, userdata, msg):
    # LOTE: telemetria só entra na janela.
    if msg.topic.endswith("/telemetria"):
        janela.adicionar(json.loads(msg.payload), len(msg.payload))

    # EVENTO: encaminhado na hora, sem esperar a janela.
    elif msg.topic.endswith("/evento"):
        evento = json.loads(msg.payload)
        if evento["tipo"] in EVENTOS_QUE_SOBEM:
            alerta = {**evento, "linha": LINHA, "encaminhado_por": NOME_NO,
                      "ts_encaminhado": time.time()}
            cliente.publish(f"fabrica/linha{LINHA}/alerta", json.dumps(alerta), qos=1)
            print(f"[{NOME_NO}] ALERTA encaminhado: {evento['maquina']} {evento['tipo']}")

    # COMANDO: vem da fábrica, desce para as máquinas da linha.
    elif msg.topic == TOPICO_COMANDO_FABRICA:
        cliente.publish(f"linha{LINHA}/comando", msg.payload, qos=1)
        comando = json.loads(msg.payload)
        publicar_viz(cliente, "comando_repassado", {"comando": comando})
        print(f"[{NOME_NO}] COMANDO repassado às máquinas: {comando}")


def main():
    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NOME_NO)
    cliente.on_connect = ao_conectar
    cliente.on_message = ao_receber
    cliente.reconnect_delay_set(min_delay=1, max_delay=5)
    cliente.connect_async(BROKER_HOST, BROKER_PORT, keepalive=10)
    cliente.loop_start()

    print(f"[{NOME_NO}] agregando em janelas de {JANELA_S} s")

    while True:
        time.sleep(JANELA_S)
        inicio, leituras, mensagens, bytes_recebidos = janela.fechar()
        if not leituras:
            continue

        resumo = resumir(inicio, leituras)
        payload = json.dumps(resumo)
        cliente.publish(f"fabrica/linha{LINHA}/resumo", payload, qos=1)

        # Indicador do experimento "volume de dados": entra x sai.
        volume = {
            "mensagens_recebidas": mensagens,
            "bytes_recebidos": bytes_recebidos,
            "bytes_enviados": len(payload),
            "reducao": round(bytes_recebidos / len(payload), 1),
        }
        publicar_viz(cliente, "resumo_publicado", {"consumo_total_kw": resumo["consumo_total_kw"], **volume})
        print(f"[{NOME_NO}] resumo: {resumo['consumo_total_kw']} kW | "
              f"{mensagens} msgs ({bytes_recebidos} B) -> 1 msg ({len(payload)} B), "
              f"{volume['reducao']}x menor")


if __name__ == "__main__":
    main()
