# Projeto Fábrica Inteligente

Demonstração de uma arquitetura **edge, fog e cloud** com **fog hierárquico** (organização vertical, em níveis), seguindo a terminologia do NIST SP 500-325.

É um **modelo didático** inspirado em fábricas reais, não a arquitetura de uma empresa específica.

```
[ maquinas-linha1 ] --MQTT--> [ gateway-linha1 ] --LAN--+
                                                        +--> [ servidor-fabrica ] --WAN--> [ nuvem ]
[ maquinas-linha2 ] --MQTT--> [ gateway-linha2 ] --LAN--+
       EDGE                  FOG (nível inferior)          FOG (nível superior)           CLOUD
```

| Camada | Componente | Enxerga | Responde em | Papel |
|---|---|---|---|---|
| Edge | Controlador de cada máquina | Só a própria máquina | Milissegundos | Desliga a máquina se superaquecer, sem depender da rede |
| Fog, nível inferior | Gateway de cada linha | As máquinas da linha | Sub-segundo a segundos | Resume a telemetria; encaminha alertas na hora |
| Fog, nível superior | Servidor da fábrica | Todas as linhas | Segundos | Controla a demanda de energia; guarda histórico |
| Cloud | Datacenter remoto | Todas as fábricas | Minutos a dias | Histórico completo, análises (Grafana) |

**Argumento central:** cada linha só conhece o próprio consumo de energia. Só o servidor da fábrica vê o total e, quando ele passa do limite contratado, manda a linha de menor prioridade reduzir a carga, em segundos e sem depender da WAN.

---

## Como rodar

Pré-requisito: Docker com Docker Compose.

```bash
cd fabrica
docker compose up -d --build
```

A primeira execução baixa as imagens e leva alguns minutos. Depois, abra no navegador:

- **Painel da demonstração:** <http://localhost:8080>
- **Grafana (visão da nuvem):** <http://localhost:3000>, sem login

Para parar: `docker compose down`. Para parar e apagar os históricos (servidor e nuvem): `docker compose down -v`.

---

## O painel

| Área | O que mostra / faz |
|---|---|
| **Onde decidir** (topo) | Move as decisões entre as camadas e mostra a latência do último desligamento em cada uma |
| **Máquinas** (edge) | Temperatura, consumo e estado. 🔥 **Falha** simula falha de refrigeração (+2 °C/s) |
| **Linhas** | ⚡ **Pico de consumo** liga e desliga um pico de produção; 🔧 **Reparar linha** religa as máquinas |
| **Gateways** (fog inferior) | Mensagens que entram e saem a cada janela de 5 s e o fator de redução |
| **Servidor** (fog superior) | Medidor de demanda contra o limite, linhas reduzidas, fila para a nuvem, último alerta |
| **Nuvem** (cloud) | Último lote recebido e total de itens |
| **Enlaces de rede** | Derruba e religa cada enlace, muda o RTT e limita a banda (via Toxiproxy) |
| **Eventos** | Tudo o que aconteceu, com a camada que decidiu |

Os pontos que percorrem os enlaces mostram as mensagens:

| Cor | Mensagem |
|---|---|
| Cinza | Telemetria |
| Azul | Resumo |
| Vermelho | Alerta |
| Laranja | Comando |
| Roxo | Lote |

O painel é um observador **fora de banda**: assina os brokers por uma rede de gerência separada, então derrubar um enlace não afeta o que ele mostra.

---

## Experimentos

O passo a passo dos experimentos, com perguntas para responder, está no **[roteiro do experimento](ROTEIRO.md)**: volume de dados, demanda de energia, superaquecimento (edge × servidor × nuvem), queda da rede interna e queda da internet.

---

## Arquitetura

### Serviços

Cada caixa do diagrama é um **nó** com o mesmo nome no `docker-compose.yml`. Num nó com broker e lógica, são dois contêineres que compartilham a mesma identidade de rede (`network_mode: service:...`): mesmo IP, comunicação por `localhost`, como dois processos num mesmo equipamento.

| Nó | Contêineres | Código |
|---|---|---|
| `maquinas-linhaN` | `maquinas-linhaN` | [maquinas/simulador.py](maquinas/simulador.py) |
| `gateway-linhaN` | `gateway-linhaN-broker` (Mosquitto + bridge) e `gateway-linhaN` (agregador) | [gateway/agregador.py](gateway/agregador.py), [mosquitto/](mosquitto/) |
| `servidor-fabrica` | `servidor-fabrica-broker` (Mosquitto) e `servidor-fabrica` (coordenador) | [servidor-fabrica/coordenador.py](servidor-fabrica/coordenador.py) |
| `nuvem` | `nuvem` (FastAPI), `nuvem-db` (Postgres), `grafana` | [nuvem/api.py](nuvem/api.py), [grafana/](grafana/) |
| enlaces | `toxiproxy` | [toxiproxy/toxiproxy.json](toxiproxy/toxiproxy.json) |
| controle | `painel-api` | [painel-api/app.py](painel-api/app.py) |
| visualização | `painel` (nginx) | [painel/](painel/) |

### Redes Docker (imitando a rede física)

| Rede | Quem está nela | Papel |
|---|---|---|
| `rede_linha1`, `rede_linha2` | Máquinas e gateway de cada linha | Rede local da linha; isoladas entre si |
| `rede_fabrica` | Gateways, Toxiproxy, servidor | LAN da fábrica (a bridge passa pelo Toxiproxy) |
| `rede_wan` | Servidor, Toxiproxy | A "internet" do lado da fábrica |
| `rede_nuvem` | Toxiproxy, nuvem, banco, Grafana | Rede interna do datacenter |
| `rede_gerencia` | Brokers, Toxiproxy, `painel-api`, painel | Gerência fora de banda (observação e controle da demonstração) |

O servidor não alcança a nuvem diretamente: só pelo Toxiproxy. Uma máquina não alcança nada fora da sua linha.

### Dois caminhos de dados

| Caminho | Exemplo | Por quê |
|---|---|---|
| **Lotes** | Resumos a cada 5 s; lotes à nuvem a cada 30 s | Economizam banda |
| **Eventos** | Alertas encaminhados na hora | Sem esperar a janela: senão, a comparação "onde decidir" mediria o tamanho da janela, não a distância |

No modo nuvem, o servidor envia o alerta (ou o estado da demanda) por HTTP, e a resposta já traz o comando.

### Tópicos MQTT

| Tópico | De → para | Tipo |
|---|---|---|
| `linhaN/maquina/{id}/telemetria` | máquina → gateway | lote (10 Hz, fica na linha) |
| `linhaN/maquina/{id}/evento` | máquina → gateway | evento |
| `linhaN/comando` | gateway → máquinas | comando |
| `fabrica/linhaN/resumo` | gateway → servidor (bridge) | lote (5 s) |
| `fabrica/linhaN/alerta` | gateway → servidor (bridge) | evento (imediato) |
| `fabrica/linhaN/comando` | servidor → gateway (bridge) | comando |
| `config/modo` | painel → todos (retido) | onde decidir |
| `linhaN/simulacao/superaquecer`, `.../religar`, `.../pico` | painel → máquinas | disparos da demonstração |
| `viz/eventos` | todos → painel | visualização |

Só o prefixo `fabrica/linhaN/` atravessa a bridge.

### Portas no host

| Porta | Serviço |
|---|---|
| 8080 | **Painel da demonstração** |
| 3000 | Grafana |
| 8081 | `painel-api` (opcional, para controlar os enlaces com `curl`) |
| 9001, 9002, 9003 | WebSocket dos brokers (linha 1, linha 2, servidor), para ferramentas como o MQTT Explorer |

---

## Sem o painel: pelo terminal

Tudo o que o painel faz também pode ser feito pelo terminal:

```bash
B1="docker compose exec gateway-linha1-broker"
API=localhost:8081/api/enlaces

$B1 mosquitto_pub -t linha1/simulacao/superaquecer -m m2           # falha numa máquina
$B1 mosquitto_pub -t linha1/simulacao/pico -m on                   # pico (on/off)
$B1 mosquitto_pub -t linha1/simulacao/religar -m todas             # reparar

curl -XPOST $API/wan -H 'Content-Type: application/json' -d '{"ativo":false}'           # derrubar a WAN
curl -XPOST $API/wan -H 'Content-Type: application/json' -d '{"rtt_ms":1000,"banda_kbps":64}'
curl -XPOST $API/restaurar

$B1 mosquitto_sub -v -t 'linha1/maquina/+/telemetria'              # telemetria bruta
docker compose exec servidor-fabrica-broker mosquitto_sub -v -t 'fabrica/#'   # o que chega ao servidor
docker compose logs -f servidor-fabrica                            # decisões do servidor
```

O modo "onde decidir" é uma mensagem retida em `config/modo`, publicada nos três brokers:

```bash
for b in gateway-linha1-broker gateway-linha2-broker servidor-fabrica-broker; do
  docker compose exec $b mosquitto_pub -r -t config/modo -m '{"superaquecimento":"servidor","demanda":"servidor"}'
done
```

---

## Parâmetros

Definidos no [docker-compose.yml](docker-compose.yml).

**`maquinas`**

| Variável | Padrão | Significado |
|---|---|---|
| `LINHA` | 1 | Número da linha |
| `NUM_MAQUINAS` | 3 | Máquinas simuladas na linha |
| `FREQUENCIA_HZ` | 10 | Frequência da telemetria |
| `TAXA_FALHA` | 2.0 | °C/s de aquecimento com refrigeração falha (90 °C → 110 °C em cerca de 10 s) |

**`gateway`**

| Variável | Padrão | Significado |
|---|---|---|
| `LINHA` | 1 | Número da linha |
| `JANELA_S` | 5 | Janela de agregação, em segundos |

**`servidor-fabrica`** (e `nuvem`, para o modo nuvem)

| Variável | Padrão | Significado |
|---|---|---|
| `LIMITE_DEMANDA_KW` | 110 | Limite de demanda contratado |
| `PRIORIDADE_LINHAS` | 1,2 | Da mais para a menos importante; a última é cortada primeiro |
| `FATOR_REDUCAO` | 0.5 | Fração da carga mantida numa linha reduzida |
| `ESPERA_ENTRE_ACOES_S` | 10 | Tempo mínimo entre duas ações (o efeito leva cerca de 1 janela) |
| `LOTE_S` | 30 | Intervalo entre lotes à nuvem |

**Enlaces** (perfil padrão, em [painel-api/app.py](painel-api/app.py)): LAN com RTT de 4 ms; WAN com RTT de 200 ms.

---

## Problemas comuns

| Sintoma | Causa provável e solução |
|---|---|
| Painel com conexões em vermelho no topo | Algum serviço ainda está subindo. Aguarde alguns segundos ou veja `docker compose ps` |
| Um gateway ou o servidor parou de processar após reiniciar só o broker | O contêiner Python compartilha a rede do broker. Reinicie os dois juntos: `docker compose restart gateway-linha1-broker gateway-linha1`. Para simular falhas de rede, use os enlaces do painel, não o Docker |
| Grafana vazio | A nuvem só recebe lotes a cada 30 s. Aguarde, ou verifique se a WAN está ativa |
| Números diferentes do esperado após muitos experimentos | Restaure o padrão (enlaces, modos, reparar linhas) ou recomece com `docker compose down -v && docker compose up -d` |
| Porta já em uso (8080, 3000, ...) | Outro programa usa a porta. Pare-o ou mude o mapeamento em `ports:` no compose |
