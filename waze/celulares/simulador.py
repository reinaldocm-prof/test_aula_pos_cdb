"""
EDGE: simulador dos celulares de uma região (um app de navegação por carro).

Cada celular ENXERGA SÓ A PRÓPRIA VIAGEM. Ele:
  1. navega localmente, com o mapa em cache: segue dirigindo mesmo sem sinal;
  2. publica posição e velocidade a 1 Hz para o fog da região;
  3. reporta acidentes que encontra no caminho;
  4. recebe alertas do fog e DESVIA dos trechos com problema.

Ponto para a aula: um celular sozinho NÃO SABE que há congestionamento
(pode estar parado num semáforo). Só o fog, que vê muitos carros juntos,
percebe o padrão.

STORE-AND-FORWARD: sem sinal (túnel, ou rede celular da região fora), o
celular guarda o rastro e os reports num buffer SQLite e envia tudo quando
o sinal volta.

Dois clientes MQTT:
  rede            via Toxiproxy (a rede 4G/5G, manipulada nos experimentos)
  instrumentação  direto ao broker, fora de banda, SÓ para o painel
                  (verdade de campo da simulação: onde os carros realmente estão)

Tópicos MQTT (cliente "rede"):
  regiao/{r}/carro/{id}/posicao    publica  (1 Hz por carro)
  regiao/{r}/report/acidente       publica  (quando um carro encontra um acidente)
  regiao/{r}/carro/{id}/trilha     publica  (rastro acumulado sem sinal)
  regiao/{r}/alerta                assina   (alertas do fog)
  regiao/{r}/transito              assina   (estado dos trechos calculado pelo fog)
Tópicos MQTT (cliente "instrumentação"):
  regiao/{r}/mapa                  publica  (retido: desenho do mapa)
  viz/carros, viz/eventos          publica  (painel)
  regiao/{r}/simulacao/...         assina   (disparos da demonstração)
"""

import json
import os
import queue
import random
import sqlite3
import time

import paho.mqtt.client as mqtt

REGIAO = os.getenv("REGIAO", "a")
NUM_CARROS = int(os.getenv("NUM_CARROS", "12"))
BROKER_HOST = os.getenv("BROKER_HOST", "toxiproxy")       # rede celular
BROKER_PORT = int(os.getenv("BROKER_PORT", "1883"))
VIZ_HOST = os.getenv("VIZ_HOST", f"fog-{REGIAO}-broker")   # instrumentação
BANCO = os.getenv("BANCO", "/dados/celulares.db")

NOME_NO = f"celulares-{REGIAO}"
R = REGIAO.upper()

FREQUENCIA_HZ = 10
COLUNAS, FILEIRAS = 4, 3          # cruzamentos do mapa da região
COMPRIMENTO_M = 100               # comprimento de cada trecho (mapa estilizado)
VEL_ACIDENTE_KMH = 5
VEL_LENTIDAO_KMH = 12
DURACAO_OCORRENCIA_S = 120        # acidente ou lentidão (dá tempo de o fog detectar e os carros desviarem)
DURACAO_TUNEL_S = 20
VALIDADE_ALERTA_S = 60            # por quanto tempo o celular evita um trecho alertado


# -----------------------------------------------------------------------------
# Mapa (o "mapa em cache" de cada celular)
# -----------------------------------------------------------------------------
def montar_mapa():
    """Grade de COLUNAS x FILEIRAS cruzamentos; cada ligação é um trecho (A01, A02...)."""
    trechos, k = {}, 1
    for f in range(FILEIRAS):
        for c in range(COLUNAS - 1):
            trechos[f"{R}{k:02d}"] = ((c, f), (c + 1, f))
            k += 1
    for c in range(COLUNAS):
        for f in range(FILEIRAS - 1):
            trechos[f"{R}{k:02d}"] = ((c, f), (c, f + 1))
            k += 1
    vizinhos = {}
    for trecho, (a, b) in trechos.items():
        vizinhos.setdefault(a, []).append((trecho, b))
        vizinhos.setdefault(b, []).append((trecho, a))
    return trechos, vizinhos


TRECHOS, VIZINHOS = montar_mapa()


# -----------------------------------------------------------------------------
# O "mundo real" (o que acontece nas ruas, independente da rede)
# -----------------------------------------------------------------------------
class Mundo:

    def __init__(self):
        self.acidentes = {}    # trecho -> {"fim": ts}
        self.lentidoes = {}    # trecho -> fim

    def limite_kmh(self, trecho):
        if trecho in self.acidentes:
            return VEL_ACIDENTE_KMH
        if trecho in self.lentidoes:
            return VEL_LENTIDAO_KMH
        return None

    def expirar(self, agora):
        encerrados = [t for t, a in self.acidentes.items() if a["fim"] < agora]
        encerrados += [t for t, fim in self.lentidoes.items() if fim < agora]
        for t in encerrados:
            self.acidentes.pop(t, None)
            self.lentidoes.pop(t, None)
        return encerrados


class Carro:

    def __init__(self, id_carro):
        self.id = id_carro
        self.trecho, (a, b) = random.choice(list(TRECHOS.items()))
        self.de, self.para = random.choice([(a, b), (b, a)])
        self.pos_m = random.uniform(0, COMPRIMENTO_M)
        self.vel_alvo = random.uniform(45, 60)
        self.vel = self.vel_alvo
        self.tunel_ate = 0.0
        self.alertas = {}            # trecho -> até quando evitar
        self.reportados = set()      # acidentes que este carro já reportou

    def em_tunel(self, agora):
        return agora < self.tunel_ate

    def xy(self):
        fracao = self.pos_m / COMPRIMENTO_M
        (x1, y1), (x2, y2) = self.de, self.para
        return x1 + (x2 - x1) * fracao, y1 + (y2 - y1) * fracao

    def passo(self, dt, mundo):
        """Dirige por dt segundos. Devolve o trecho evitado, se desviou."""
        limite = mundo.limite_kmh(self.trecho)
        desejada = min(self.vel_alvo, limite) if limite else self.vel_alvo
        self.vel += (desejada - self.vel) * min(1.0, dt * 1.5)   # acelera/freia
        self.pos_m += self.vel / 3.6 * dt
        if self.pos_m >= COMPRIMENTO_M:
            return self.escolher_proximo()
        return None

    def escolher_proximo(self):
        """No cruzamento: escolhe o próximo trecho, evitando os alertados."""
        sobra = self.pos_m - COMPRIMENTO_M
        opcoes = [(t, n) for t, n in VIZINHOS[self.para] if n != self.de] or VIZINHOS[self.para]
        agora = time.time()
        livres = [(t, n) for t, n in opcoes if self.alertas.get(t, 0) < agora]
        evitado = None
        if livres and len(livres) < len(opcoes):
            evitado = next(t for t, n in opcoes if (t, n) not in livres)
        trecho, proximo = random.choice(livres or opcoes)
        self.de, self.para, self.trecho, self.pos_m = self.para, proximo, trecho, sobra
        return evitado


# -----------------------------------------------------------------------------
# Buffer de store-and-forward (SQLite)
# -----------------------------------------------------------------------------
class Buffer:

    def __init__(self, caminho):
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        self.conexao = sqlite3.connect(caminho)
        self.conexao.execute("DROP TABLE IF EXISTS buffer")   # cada execução começa limpa
        self.conexao.execute("CREATE TABLE buffer (id INTEGER PRIMARY KEY, carro TEXT, tipo TEXT, dados TEXT)")

    def guardar(self, carro, tipo, dados):
        self.conexao.execute("INSERT INTO buffer (carro, tipo, dados) VALUES (?, ?, ?)",
                             (carro, tipo, json.dumps(dados)))
        self.conexao.commit()

    def retirar(self, carro):
        linhas = self.conexao.execute("SELECT tipo, dados FROM buffer WHERE carro = ? ORDER BY id",
                                      (carro,)).fetchall()
        self.conexao.execute("DELETE FROM buffer WHERE carro = ?", (carro,))
        self.conexao.commit()
        return [(tipo, json.loads(dados)) for tipo, dados in linhas]

    def tamanho(self, carro):
        return self.conexao.execute("SELECT COUNT(*) FROM buffer WHERE carro = ?", (carro,)).fetchone()[0]


# -----------------------------------------------------------------------------
# MQTT
# -----------------------------------------------------------------------------
# Mensagens chegam pelas threads de rede do paho; o laço principal as aplica.
mensagens = queue.Queue()


def criar_cliente(nome, host, port, topicos):
    cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{NOME_NO}-{nome}")

    def ao_conectar(c, userdata, flags, reason_code, properties):
        print(f"[{NOME_NO}] cliente '{nome}' conectado a {host}:{port}")
        if topicos:
            c.subscribe([(t, 1) for t in topicos])
        mensagens.put(("conectado", nome))

    def ao_desconectar(c, userdata, flags, reason_code, properties):
        print(f"[{NOME_NO}] cliente '{nome}' SEM CONEXÃO; os carros seguem navegando")

    cliente.on_connect = ao_conectar
    cliente.on_disconnect = ao_desconectar
    cliente.on_message = lambda c, u, msg: mensagens.put((msg.topic, msg.payload.decode()))
    cliente.reconnect_delay_set(min_delay=1, max_delay=3)
    cliente.connect_async(host, port, keepalive=5)
    cliente.loop_start()
    return cliente


class Simulador:

    def __init__(self):
        self.mundo = Mundo()
        self.carros = [Carro(f"{REGIAO}{i:02d}") for i in range(1, NUM_CARROS + 1)]
        self.buffer = Buffer(BANCO)
        self.sem_sinal = set()       # carros que estavam sem sinal no último passo
        self.ultimo_transito = {}    # estado dos trechos recebido do fog
        self.rede = criar_cliente("rede", BROKER_HOST, BROKER_PORT,
                                  [f"regiao/{REGIAO}/alerta", f"regiao/{REGIAO}/transito"])
        self.viz = criar_cliente("instrumentacao", VIZ_HOST, 1883, [f"regiao/{REGIAO}/simulacao/#"])

    # -- sinal --------------------------------------------------------------
    def tem_sinal(self, carro, agora):
        return self.rede.is_connected() and not carro.em_tunel(agora)

    def evento(self, tipo, **dados):
        """Evento para o painel (instrumentação, fora de banda)."""
        evento = {"ts": time.time(), "no": NOME_NO, "camada": "edge", "regiao": REGIAO, "tipo": tipo, **dados}
        self.viz.publish("viz/eventos", json.dumps(evento), qos=0)
        print(f"[{NOME_NO}] {tipo} {dados}")

    # -- mensagens recebidas ------------------------------------------------
    def processar(self, topico, conteudo):
        agora = time.time()
        if topico == f"regiao/{REGIAO}/alerta":
            self.receber_alerta(json.loads(conteudo), agora)
        elif topico == f"regiao/{REGIAO}/transito":
            self.ultimo_transito = json.loads(conteudo).get("trechos", {})
            for carro in self.carros:
                if self.tem_sinal(carro, agora):
                    self.aplicar_transito(carro, agora)
        elif topico.startswith(f"regiao/{REGIAO}/simulacao/"):
            self.disparo(topico.rsplit("/", 1)[1], conteudo.strip(), agora)
        elif topico == "conectado" and conteudo == "instrumentacao":
            self.publicar_mapa()

    def receber_alerta(self, alerta, agora):
        """Alerta do fog: só chega aos celulares COM SINAL."""
        avisados = [c for c in self.carros if self.tem_sinal(c, agora)]
        for carro in avisados:
            if alerta["tipo"] in ("acidente", "congestionamento"):
                carro.alertas[alerta["trecho"]] = agora + VALIDADE_ALERTA_S
            elif alerta["tipo"] == "liberado":
                carro.alertas.pop(alerta["trecho"], None)
        latencia_ms = round((agora - alerta["ts_evento"]) * 1000, 1) if alerta.get("ts_evento") else None
        self.evento("alerta_recebido", alerta=alerta["tipo"], trecho=alerta["trecho"],
                    decidido_em=alerta.get("decidido_em"), latencia_ms=latencia_ms,
                    carros_avisados=len(avisados), carros_sem_sinal=len(self.carros) - len(avisados))

    def aplicar_transito(self, carro, agora):
        """Atualiza o 'mapa de trânsito' do celular com o último estado do fog."""
        for trecho, info in self.ultimo_transito.items():
            if info["estado"] == "congestionado":
                carro.alertas[trecho] = max(carro.alertas.get(trecho, 0), agora + VALIDADE_ALERTA_S)

    def disparo(self, tipo, alvo, agora):
        """Disparos da demonstração (vêm do painel)."""
        if tipo == "limpar":
            self.mundo.acidentes.clear()
            self.mundo.lentidoes.clear()
            for carro in self.carros:
                carro.reportados.clear()
            self.evento("ocorrencias_limpas")
            return
        if tipo == "tunel":
            candidatos = [c for c in self.carros if not c.em_tunel(agora)]
            carro = next((c for c in candidatos if c.id == alvo), None) or random.choice(candidatos)
            carro.tunel_ate = agora + DURACAO_TUNEL_S
            self.evento("tunel_entrou", carro=carro.id, duracao_s=DURACAO_TUNEL_S)
            return
        trecho = alvo if alvo in TRECHOS else random.choice(list(TRECHOS))
        if tipo == "acidente":
            self.mundo.acidentes[trecho] = {"fim": agora + DURACAO_OCORRENCIA_S}
            self.evento("acidente_ocorreu", trecho=trecho)
        elif tipo == "lentidao":
            self.mundo.lentidoes[trecho] = agora + DURACAO_OCORRENCIA_S
            self.evento("lentidao_iniciou", trecho=trecho)

    # -- publicações -------------------------------------------------------
    def publicar_mapa(self):
        mapa = {"regiao": REGIAO, "colunas": COLUNAS, "fileiras": FILEIRAS,
                "trechos": {t: [list(a), list(b)] for t, (a, b) in TRECHOS.items()}}
        self.viz.publish(f"regiao/{REGIAO}/mapa", json.dumps(mapa), qos=1, retain=True)

    def publicar_posicao(self, carro, agora):
        x, y = carro.xy()
        posicao = {"ts": agora, "carro": carro.id, "trecho": carro.trecho,
                   "x": round(x, 3), "y": round(y, 3), "vel_kmh": round(carro.vel, 1)}
        if self.tem_sinal(carro, agora):
            self.rede.publish(f"regiao/{REGIAO}/carro/{carro.id}/posicao", json.dumps(posicao), qos=0)
        else:
            # Sem sinal: o rastro fica guardado no celular.
            self.buffer.guardar(carro.id, "trilha", posicao)

    def reportar_acidentes(self, agora):
        """O motorista que passa por um acidente o reporta (se tiver sinal, na hora)."""
        for trecho in self.mundo.acidentes:
            for carro in self.carros:
                if carro.trecho != trecho or trecho in carro.reportados:
                    continue
                carro.reportados.add(trecho)
                report = {"ts": agora, "carro": carro.id, "trecho": trecho}
                if self.tem_sinal(carro, agora):
                    self.rede.publish(f"regiao/{REGIAO}/report/acidente", json.dumps(report), qos=1)
                    self.evento("acidente_reportado", carro=carro.id, trecho=trecho)
                else:
                    self.buffer.guardar(carro.id, "report", report)
                    self.evento("report_guardado", carro=carro.id, trecho=trecho)

    def sincronizar(self, carro, agora):
        """O sinal voltou: envia o que ficou guardado (store-and-forward)."""
        itens = self.buffer.retirar(carro.id)
        pontos = [d for tipo, d in itens if tipo == "trilha"]
        reports = [d for tipo, d in itens if tipo == "report"]
        for report in reports:
            self.rede.publish(f"regiao/{REGIAO}/report/acidente", json.dumps({**report, "atrasado": True}), qos=1)
        if pontos:
            self.rede.publish(f"regiao/{REGIAO}/carro/{carro.id}/trilha",
                              json.dumps({"ts": agora, "carro": carro.id, "pontos": pontos}), qos=1)
        self.aplicar_transito(carro, agora)
        if pontos or reports:
            self.evento("sincronizado", carro=carro.id, pontos=len(pontos), reports=len(reports))

    def publicar_viz_carros(self, agora):
        carros = []
        for c in self.carros:
            x, y = c.xy()
            carros.append({"id": c.id, "x": round(x, 3), "y": round(y, 3), "vel": round(c.vel),
                           "sinal": self.tem_sinal(c, agora), "buffer": self.buffer.tamanho(c.id)
                           if not self.tem_sinal(c, agora) else 0})
        self.viz.publish("viz/carros", json.dumps({"ts": agora, "regiao": REGIAO, "carros": carros}), qos=0)

    # -- laço principal -----------------------------------------------------
    def executar(self):
        print(f"[{NOME_NO}] {NUM_CARROS} carros, {len(TRECHOS)} trechos")
        dt = 1.0 / FREQUENCIA_HZ
        proximo = time.monotonic()
        tique = 0
        while True:
            agora = time.time()
            for trecho in self.mundo.expirar(agora):
                for carro in self.carros:
                    carro.reportados.discard(trecho)
                self.evento("ocorrencia_encerrada", trecho=trecho)

            for i, carro in enumerate(self.carros):
                evitado = carro.passo(dt, self.mundo)
                if evitado:
                    self.evento("desvio", carro=carro.id, evitou=evitado)
                # Sinal voltou (saiu do túnel ou a rede voltou): sincroniza.
                com_sinal = self.tem_sinal(carro, agora)
                if com_sinal and carro.id in self.sem_sinal:
                    self.sem_sinal.discard(carro.id)
                    if not carro.em_tunel(agora) and carro.tunel_ate:
                        carro.tunel_ate = 0
                        self.evento("tunel_saiu", carro=carro.id)
                    self.sincronizar(carro, agora)
                elif not com_sinal:
                    self.sem_sinal.add(carro.id)
                # Posição a 1 Hz, com os carros espalhados ao longo do segundo.
                if tique % FREQUENCIA_HZ == i % FREQUENCIA_HZ:
                    self.publicar_posicao(carro, agora)

            self.reportar_acidentes(agora)
            if tique % 5 == 0:
                self.publicar_viz_carros(agora)

            # Até o próximo tique, atende as mensagens assim que chegam.
            tique += 1
            proximo += dt
            while (restante := proximo - time.monotonic()) > 0:
                try:
                    topico, conteudo = mensagens.get(timeout=restante)
                except queue.Empty:
                    break
                self.processar(topico, conteudo)


if __name__ == "__main__":
    Simulador().executar()
