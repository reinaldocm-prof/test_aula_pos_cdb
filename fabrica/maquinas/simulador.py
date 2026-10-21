"""
EDGE: simulador dos controladores embarcados das máquinas de uma linha.

Cada máquina tem um controlador próprio (a camada edge). Ele:
  1. mede temperatura, vibração e consumo de energia;
  2. publica a telemetria em alta frequência para o gateway da linha;
  3. aplica o INTERTRAVAMENTO DE SEGURANÇA: se superaquecer, desliga sozinha,
     na hora, SEM DEPENDER DA REDE.

Ponto para a aula: a rede pode cair que a máquina continua protegida.
A telemetria se perde, mas a decisão de segurança não sai do edge.

Tópicos MQTT usados:
  linha{n}/maquina/{id}/telemetria   publica  (edge -> gateway, alta frequência)
  linha{n}/maquina/{id}/evento       publica  (edge -> gateway, imediato: superaquecimento etc.)
  linha{n}/comando                   assina   (gateway -> máquinas: desligar, reduzir carga)
  linha{n}/simulacao/superaquecer    assina   (disparo da demo: payload = id)
  linha{n}/simulacao/religar         assina   (reparo da demo: payload = id ou "todas")
  linha{n}/simulacao/pico            assina   (pico de produção na linha: "on" ou "off")
  config/modo                        assina   (onde decidir; retido, definido pelo painel)
  viz/eventos                        publica  (eventos para o painel)
"""

import json
import os
import queue
import random
import time

import paho.mqtt.client as mqtt

# -----------------------------------------------------------------------------
# Configuração (vem do docker-compose.yml)
# -----------------------------------------------------------------------------
LINHA = int(os.getenv("LINHA", "1"))
NUM_MAQUINAS = int(os.getenv("NUM_MAQUINAS", "3"))
BROKER_HOST = os.getenv("BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))
FREQUENCIA_HZ = float(os.getenv("FREQUENCIA_HZ", "10"))

# Onde decidir o desligamento por superaquecimento (tópico config/modo,
# definido pelo painel). Só com "edge" o intertravamento é local; nos outros
# modos a máquina apenas avisa e espera o comando de uma camada superior.
modo_superaquecimento = "edge"

NOME_NO = f"maquinas-linha{LINHA}"

# -----------------------------------------------------------------------------
# "Física" simplificada da máquina
# -----------------------------------------------------------------------------
TEMP_AMBIENTE = 25.0       # °C
GANHO_TERMICO = 55.0       # °C acima do ambiente com carga 100%
TAU_AQUECIMENTO = 20.0     # s: rapidez com que a temperatura busca o regime
TAU_RESFRIAMENTO = 30.0    # s: rapidez com que esfria depois de desligada
TAXA_FALHA = float(os.getenv("TAXA_FALHA", "2.0"))  # °C/s com refrigeração falha

LIMITE_DESLIGAMENTO = 90.0  # °C: o intertravamento desliga aqui
LIMITE_CRITICO = 110.0      # °C: acima disso, ligada, a máquina se danifica

# Perfis fixos (potência nominal em kW, carga de operação), para que os
# números da demonstração sejam previsíveis: cada linha consome ~50 kW.
PERFIS = [(22.0, 0.80), (30.0, 0.70), (15.0, 0.75)]
MULTIPLICADOR_PICO = 1.5    # carga durante um pico de produção (limitada a 100%)


class Maquina:
    """Estado e comportamento de uma máquina (o que o controlador embarcado vê)."""

    def __init__(self, id_maquina, perfil):
        self.id = id_maquina
        self.estado = "ligada"            # ligada | desligada | danificada
        self.potencia_nominal_kw, self.carga_nominal = perfil
        self.pico = False                 # pico de produção (disparo da demo)
        self.fator_carga = 1.0            # < 1.0 quando o servidor manda reduzir
        self.temperatura = TEMP_AMBIENTE + self.carga * GANHO_TERMICO
        self.falha_refrigeracao = False
        self.ts_limite_ultrapassado = None  # quando passou do limite (p/ latência)

    @property
    def carga(self):
        """Carga efetiva: nominal, aumentada no pico, reduzida por comando."""
        base = self.carga_nominal * (MULTIPLICADOR_PICO if self.pico else 1.0)
        return min(1.0, base) * self.fator_carga

    def passo(self, dt):
        """Avança a simulação em dt segundos e devolve eventos que ocorreram."""
        eventos = []

        # 1) Evolução da temperatura
        if self.estado == "ligada":
            if self.falha_refrigeracao:
                self.temperatura += TAXA_FALHA * dt
            else:
                regime = TEMP_AMBIENTE + self.carga * GANHO_TERMICO
                self.temperatura += (regime - self.temperatura) * dt / TAU_AQUECIMENTO
        else:
            self.temperatura += (TEMP_AMBIENTE - self.temperatura) * dt / TAU_RESFRIAMENTO

        if self.estado != "ligada":
            return eventos

        agora = time.time()

        # 2) Detecção do superaquecimento (o "evento" cuja latência medimos)
        if self.temperatura >= LIMITE_DESLIGAMENTO and self.ts_limite_ultrapassado is None:
            self.ts_limite_ultrapassado = agora
            eventos.append(("superaquecimento_detectado", {"temperatura": round(self.temperatura, 1)}))

        # 3) INTERTRAVAMENTO DE SEGURANÇA (decisão no edge)
        #    Nenhuma mensagem de rede é necessária para esta decisão.
        if modo_superaquecimento == "edge" and self.temperatura >= LIMITE_DESLIGAMENTO:
            eventos.append(self.desligar(origem="edge"))

        # 4) Sem intertravamento a tempo, a máquina se danifica
        elif self.temperatura >= LIMITE_CRITICO:
            self.estado = "danificada"
            eventos.append(("danificada", {"temperatura": round(self.temperatura, 1)}))

        return eventos

    def desligar(self, origem):
        """Desliga a máquina e mede a latência desde o superaquecimento."""
        self.estado = "desligada"
        latencia_ms = None
        if self.ts_limite_ultrapassado is not None:
            latencia_ms = round((time.time() - self.ts_limite_ultrapassado) * 1000, 1)
        return ("desligada", {
            "decidido_em": origem,          # edge | servidor | nuvem
            "latencia_ms": latencia_ms,     # superaquecimento -> desligamento
            "temperatura": round(self.temperatura, 1),
        })

    def religar(self):
        """Reparo da demonstração: zera a falha e liga de novo.

        Não mexe no pico nem na redução de carga: quem decide a carga é o
        servidor da fábrica, e ele precisa continuar sabendo o estado real.
        """
        self.estado = "ligada"
        self.falha_refrigeracao = False
        self.ts_limite_ultrapassado = None
        self.temperatura = min(self.temperatura, TEMP_AMBIENTE + self.carga * GANHO_TERMICO)

    def telemetria(self):
        """Leitura dos sensores, como o controlador publicaria."""
        ligada = self.estado == "ligada"
        excesso = max(0.0, self.temperatura - 70.0)
        return {
            "ts": time.time(),
            "maquina": self.id,
            "estado": self.estado,
            "temperatura": round(self.temperatura + random.gauss(0, 0.2), 2),
            "vibracao": round((2.0 + 0.15 * excesso + random.gauss(0, 0.1)) if ligada else 0.0, 2),
            "consumo_kw": round(self.potencia_nominal_kw * self.carga * random.uniform(0.97, 1.03) if ligada else 0.0, 2),
            "carga": round(self.carga, 2),
            "pico": self.pico,
        }


# -----------------------------------------------------------------------------
# MQTT
# -----------------------------------------------------------------------------
# Mensagens chegam pela thread de rede do MQTT. Para não mexer no estado das
# máquinas de duas threads ao mesmo tempo, elas entram numa fila e o laço
# principal as aplica (ver processar()).
comandos = queue.Queue()

TOPICO_SUPERAQUECER = f"linha{LINHA}/simulacao/superaquecer"
TOPICO_RELIGAR = f"linha{LINHA}/simulacao/religar"
TOPICO_PICO = f"linha{LINHA}/simulacao/pico"
TOPICO_COMANDO = f"linha{LINHA}/comando"


def ao_conectar(cliente, userdata, flags, reason_code, properties):
    print(f"[{NOME_NO}] conectado ao broker {BROKER_HOST}:{BROKER_PORT} ({reason_code})")
    # Assina de novo a cada (re)conexão.
    cliente.subscribe([(TOPICO_SUPERAQUECER, 1), (TOPICO_RELIGAR, 1),
                       (TOPICO_PICO, 1), (TOPICO_COMANDO, 1), ("config/modo", 1)])


def ao_desconectar(cliente, userdata, flags, reason_code, properties):
    # A rede caiu, mas as máquinas seguem protegidas: o intertravamento é local.
    print(f"[{NOME_NO}] desconectado do broker ({reason_code}); seguindo em operação local")


def ao_receber(cliente, userdata, msg):
    comandos.put((msg.topic, msg.payload.decode().strip()))


def publicar_evento(cliente, tipo, maquina, dados):
    """Evento discreto: vai ao gateway (caminho imediato) e ao painel."""
    evento = {"ts": time.time(), "no": NOME_NO, "camada": "edge",
              "tipo": tipo, "maquina": maquina, **dados}
    payload = json.dumps(evento)
    # Caminho de EVENTOS: o gateway encaminha na hora, sem esperar a janela.
    cliente.publish(f"linha{LINHA}/maquina/{maquina}/evento", payload, qos=1)
    # Visualização (fora de banda).
    cliente.publish("viz/eventos", payload, qos=0)
    print(f"[{NOME_NO}] {maquina}: {tipo} {dados}")


def aplicar_comando(cliente, maquinas, payload):
    """Comando vindo de uma camada superior (servidor da fábrica ou nuvem)."""
    try:
        comando = json.loads(payload)
    except json.JSONDecodeError:
        print(f"[{NOME_NO}] comando inválido: {payload}")
        return
    acao = comando.get("acao")
    alvo = comando.get("maquina", "todas")
    alvos = list(maquinas) if alvo == "todas" else [alvo]

    for id_maquina in alvos:
        maquina = maquinas.get(id_maquina)
        if maquina is None or maquina.estado != "ligada":
            continue
        if acao == "desligar":
            # O edge tem a palavra final: um "desligue por superaquecimento" que
            # chega quando a máquina NÃO está superaquecendo é um comando atrasado
            # (ex.: decidido longe, preso numa WAN lenta). Ignora.
            if comando.get("motivo") == "superaquecimento" and maquina.ts_limite_ultrapassado is None:
                publicar_evento(cliente, "comando_obsoleto_ignorado", maquina.id,
                                {"decidido_em": comando.get("decidido_em")})
                continue
            tipo, dados = maquina.desligar(origem=comando.get("decidido_em", "desconhecido"))
            publicar_evento(cliente, tipo, maquina.id, dados)
        elif acao == "reduzir_carga":
            maquina.fator_carga = float(comando.get("fator", 0.5))
            publicar_evento(cliente, "carga_reduzida", maquina.id, {"carga": round(maquina.carga, 2)})
        elif acao == "restaurar_carga":
            maquina.fator_carga = 1.0
            publicar_evento(cliente, "carga_restaurada", maquina.id, {"carga": round(maquina.carga, 2)})


def main():
    maquinas = {f"m{i}": Maquina(f"m{i}", PERFIS[(i - 1) % len(PERFIS)])
                for i in range(1, NUM_MAQUINAS + 1)}

    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NOME_NO)
    cliente.on_connect = ao_conectar
    cliente.on_disconnect = ao_desconectar
    cliente.on_message = ao_receber
    cliente.reconnect_delay_set(min_delay=1, max_delay=5)

    # connect_async + loop_start: se o broker ainda não subiu (ou cair),
    # o paho tenta reconectar sozinho, e a simulação não para.
    cliente.connect_async(BROKER_HOST, BROKER_PORT, keepalive=10)
    cliente.loop_start()

    print(f"[{NOME_NO}] {NUM_MAQUINAS} máquinas, {FREQUENCIA_HZ} Hz")

    dt = 1.0 / FREQUENCIA_HZ
    proximo = time.monotonic()

    while True:
        # 1) Avança a física e o controle de cada máquina
        for maquina in maquinas.values():
            for tipo, dados in maquina.passo(dt):
                publicar_evento(cliente, tipo, maquina.id, dados)

            # 2) Telemetria em alta frequência (fica dentro da linha)
            cliente.publish(f"linha{LINHA}/maquina/{maquina.id}/telemetria",
                            json.dumps(maquina.telemetria()), qos=0)

        # 3) Até o próximo tique, atende cada comando ASSIM QUE CHEGA (sem esperar
        #    o tique), para que a latência medida seja a da rede, não a do laço.
        proximo += dt
        while (restante := proximo - time.monotonic()) > 0:
            try:
                topico, conteudo = comandos.get(timeout=restante)
            except queue.Empty:
                break
            processar(cliente, maquinas, topico, conteudo)


def processar(cliente, maquinas, topico, conteudo):
    """Aplica uma mensagem recebida (comando, configuração ou disparo da demo)."""
    global modo_superaquecimento

    if topico == TOPICO_COMANDO:
        aplicar_comando(cliente, maquinas, conteudo)
        return
    if topico == "config/modo":
        modo_superaquecimento = json.loads(conteudo).get("superaquecimento", "edge")
        print(f"[{NOME_NO}] desligamento por superaquecimento decidido no: {modo_superaquecimento}")
        return
    if topico == TOPICO_PICO:
        # Pico de produção na linha inteira: "on" ou "off".
        for maquina in maquinas.values():
            maquina.pico = conteudo == "on"
        publicar_evento(cliente, "pico_" + conteudo, "todas", {})
        return

    alvos = list(maquinas) if conteudo == "todas" else [conteudo]
    for id_maquina in alvos:
        maquina = maquinas.get(id_maquina)
        if maquina is None:
            continue
        if topico == TOPICO_SUPERAQUECER and maquina.estado == "ligada":
            maquina.falha_refrigeracao = True
            publicar_evento(cliente, "falha_refrigeracao", maquina.id, {})
        elif topico == TOPICO_RELIGAR:
            maquina.religar()
            publicar_evento(cliente, "religada", maquina.id, {})

if __name__ == "__main__":
    main()
