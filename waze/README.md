# Projeto Waze

Demonstração de uma arquitetura **edge, fog e cloud** com **fog distribuído por região** (organização horizontal: um datacenter próximo por região), seguindo a terminologia do NIST SP 500-325.

É um **modelo didático** inspirado no Waze, não a arquitetura real do aplicativo.

```
[ celulares-a ] --4G/5G--> [ fog-a ] --WAN--+
                                            +--> [ nuvem ]
[ celulares-b ] --4G/5G--> [ fog-b ] --WAN--+
     EDGE                     FOG              CLOUD
```

| Camada | Componente | Enxerga | Responde em | Papel |
|---|---|---|---|---|
| Edge | Celular de cada motorista | Só a própria viagem | Milissegundos | Navega com o mapa em cache, mesmo sem sinal; guarda reports para enviar depois |
| Fog | Datacenter próximo, um por região | Todos os carros da região | Segundos | Detecta congestionamento pela velocidade de muitos carros; alerta quem se aproxima |
| Cloud | Datacenter distante | Todas as regiões | Minutos a dias | Histórico ("essa via trava sexta às 18h"), modelos de previsão |

**Argumento central:** um celular sozinho **não sabe** que há congestionamento: pode estar parado num semáforo. Só quem vê muitos carros juntos percebe o padrão, e o aviso precisa chegar em segundos a quem se aproxima.

---

## Como rodar

Pré-requisito: Docker com Docker Compose.

```bash
cd waze
docker compose up -d --build
```

Abra no navegador:

- **Painel da demonstração:** <http://localhost:8090>
- **Grafana (visão da nuvem):** <http://localhost:3010>, sem login

As portas são diferentes das do projeto Fábrica (8080 e 3000), então os dois podem rodar ao mesmo tempo.

Para parar: `docker compose down`. Para parar e apagar os históricos: `docker compose down -v`.

---

## O painel

| Área | O que mostra / faz |
|---|---|
| **Onde decidir** (topo) | Alerta de acidente decidido no **fog** ou na **nuvem**, com a latência do último alerta em cada modo |
| **Ferramentas** | Escolha 🚧 **Acidente** ou 🐢 **Lentidão** e clique num trecho do mapa; ou 🚇 **Túnel** e clique num carro |
| **Mapas** (regiões A e B) | Carros em movimento e trechos coloridos pelo fog |
| **Topologia** | Celulares → fog → nuvem ← fog ← celulares, com mensagens animadas, RTT e bytes por minuto |
| **Enlaces de rede** | Derruba e religa cada enlace, muda o RTT e limita a banda (via Toxiproxy) |
| **Eventos** | Tudo o que aconteceu, com a camada que decidiu |

O mapa mostra **dois pontos de vista ao mesmo tempo**:

| No mapa | De onde vem | O que representa |
|---|---|---|
| **Carros** | Instrumentação da simulação | Onde os carros **realmente** estão, inclusive os que estão sem sinal (cinza) |
| **Cores dos trechos** | Fog da região | O que o fog **concluiu** a partir das posições que recebeu. Cinza = nenhum carro visto na janela |
| 🚧 / 🐢 | Simulação | O que de fato acontece na rua (o fog não vê isso diretamente) |

A diferença entre os dois é parte da aula: com a rede da região fora, os carros continuam andando, mas o mapa do fog fica cinza.

---

## Experimentos

O passo a passo dos experimentos, com perguntas para responder, está no **[roteiro do experimento](ROTEIRO.md)**: trânsito lento, acidente (fog × nuvem), túnel, rede celular fora e nuvem fora.

---

## Arquitetura

### Serviços

| Nó | Contêineres | Código |
|---|---|---|
| `celulares-a`, `celulares-b` | 1 por região (12 carros) | [celulares/simulador.py](celulares/simulador.py) |
| `fog-a`, `fog-b` | `fog-X-broker` (Mosquitto) e `fog-X` (processador) | [fog/processador.py](fog/processador.py), [mosquitto/](mosquitto/) |
| `nuvem` | `nuvem` (FastAPI), `nuvem-db` (Postgres), `grafana` | [nuvem/api.py](nuvem/api.py), [grafana/](grafana/) |
| enlaces | `toxiproxy` | [toxiproxy/toxiproxy.json](toxiproxy/toxiproxy.json) |
| controle | `painel-api` | [painel-api/app.py](painel-api/app.py) |
| visualização | `painel` (nginx) | [painel/](painel/) |

No nó fog, o processador compartilha a rede do broker (`network_mode: service:fog-X-broker`): mesmo IP, comunicação por `localhost`.

### Redes Docker

| Rede | Quem está nela | Papel |
|---|---|---|
| `rede_regiao_a`, `rede_regiao_b` | Celulares, fog e Toxiproxy de cada região | Rede de cada região; isoladas entre si |
| `rede_wan` | Fogs, Toxiproxy | A internet entre os datacenters regionais e a nuvem |
| `rede_nuvem` | Toxiproxy, nuvem, banco, Grafana | Rede interna do datacenter da nuvem |
| `rede_gerencia` | Brokers, celulares (instrumentação), Toxiproxy, `painel-api`, painel | Gerência fora de banda (observação e controle da demonstração) |

O simulador dos celulares tem **dois clientes MQTT**:
- **rede:** passa pelo Toxiproxy. É a rede 4G/5G de verdade, a que os experimentos manipulam.
- **instrumentação:** fora de banda. Publica só a "verdade de campo" para o painel (`viz/carros`, `viz/eventos`, mapa) e recebe os disparos da demonstração. Não faz parte da arquitetura demonstrada.

### Enlaces (Toxiproxy)

| Enlace | De → para | RTT padrão |
|---|---|---|
| `celular_a` | celulares-a → fog-a (rede 4G/5G da região inteira) | 30 ms |
| `celular_b` | celulares-b → fog-b | 30 ms |
| `wan` | fogs → nuvem | 250 ms |

O túnel de **um** carro é simulado no próprio simulador, porque o Toxiproxy atua no enlace da região inteira.

### Dois caminhos de dados

| Caminho | Exemplo | Por quê |
|---|---|---|
| **Lotes** | Estado dos trechos a cada 10 s; lotes à nuvem a cada 30 s | Economizam banda |
| **Eventos** | Report de acidente → alerta na hora | Sem esperar a janela |

### Tópicos MQTT

| Tópico | De → para | Tipo |
|---|---|---|
| `regiao/{r}/carro/{id}/posicao` | celular → fog | lote (1 Hz por carro) |
| `regiao/{r}/report/acidente` | celular → fog | evento |
| `regiao/{r}/carro/{id}/trilha` | celular → fog | rastro guardado sem sinal |
| `regiao/{r}/alerta` | fog → celulares da região | evento |
| `regiao/{r}/transito` | fog → celulares (retido) | estado de cada trecho |
| `config/modo` | painel → fogs (retido) | onde decidir |
| `regiao/{r}/mapa` | instrumentação → painel (retido) | desenho do mapa |
| `regiao/{r}/simulacao/acidente`, `lentidao`, `tunel`, `limpar` | painel → simulador | disparos |
| `viz/carros`, `viz/eventos` | instrumentação e fog → painel | visualização |

### Portas no host

| Porta | Serviço |
|---|---|
| 8090 | **Painel da demonstração** |
| 3010 | Grafana |
| 8091 | `painel-api` (opcional, para controlar os enlaces com `curl`) |
| 9011, 9012 | WebSocket dos brokers dos fogs A e B (ferramentas como o MQTT Explorer) |

---

## Sem o painel: pelo terminal

```bash
PA="docker compose exec fog-a-broker mosquitto_pub"
API=localhost:8091/api/enlaces

$PA -t regiao/a/simulacao/acidente -m A05      # acidente no trecho A05 ("auto" = aleatório)
$PA -t regiao/a/simulacao/lentidao -m A08      # lentidão
$PA -t regiao/a/simulacao/tunel -m a03         # carro a03 no túnel ("auto" = aleatório)
$PA -t regiao/a/simulacao/limpar -m x          # limpa as ocorrências

curl -XPOST $API/celular_a -H 'Content-Type: application/json' -d '{"ativo":false}'
curl -XPOST $API/wan -H 'Content-Type: application/json' -d '{"rtt_ms":3000}'
curl -XPOST $API/restaurar

docker compose logs -f fog-a celulares-a
```

Onde decidir (mensagem retida nos dois brokers):

```bash
for b in fog-a-broker fog-b-broker; do
  docker compose exec $b mosquitto_pub -r -t config/modo -m '{"acidente":"nuvem"}'
done
```

---

## Parâmetros

**`celulares`**

| Variável | Padrão | Significado |
|---|---|---|
| `REGIAO` | a | Região simulada |
| `NUM_CARROS` | 12 | Carros na região |

No código: mapa de 4 × 3 cruzamentos (17 trechos de 100 m); acidente a 5 km/h; lentidão a 12 km/h; ocorrências duram 120 s; túnel dura 20 s.

**`fog`**

| Variável | Padrão | Significado |
|---|---|---|
| `REGIAO` | a | Região atendida |
| `JANELA_S` | 10 | Janela de agregação por trecho |
| `LOTE_S` | 30 | Intervalo entre lotes à nuvem |

No código, um trecho é congestionado com velocidade média abaixo de 30% da livre (50 km/h) e pelo menos 2 carros distintos; e é lento abaixo de 60%.

---

## Problemas comuns

| Sintoma | Causa provável e solução |
|---|---|
| Mapa vazio | O mapa é publicado pelo simulador ao conectar. Aguarde alguns segundos ou recarregue a página |
| Conexões em vermelho no topo | Algum serviço ainda está subindo: `docker compose ps` |
| Um fog parou de processar após reiniciar só o broker | O processador compartilha a rede do broker. Reinicie os dois juntos: `docker compose restart fog-a-broker fog-a` |
| Porta em uso | Mude o mapeamento em `ports:` no compose |
