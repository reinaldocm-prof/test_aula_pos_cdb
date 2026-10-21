# Demonstrações de edge, fog e cloud

Material de demonstração do curso **Arquitetura de Soluções Distribuídas para Edge Computing** (Ânima Educação).

São **dois projetos independentes**. Cada um roda sozinho com `docker compose`, tem um painel animado no navegador e um dashboard no Grafana. Os dois mostram as duas formas de organizar a camada fog previstas pelo NIST SP 500-325 (*Fog Computing Conceptual Model*):

| | [Fábrica Inteligente](fabrica/) | [Waze](waze/) |
|---|---|---|
| Organização do fog | **Vertical**: em níveis (gateway da linha → servidor da fábrica) | **Horizontal**: um datacenter por região |
| Edge | Controlador embarcado de cada máquina | Celular de cada motorista |
| Decisão que justifica o fog | Controle da **demanda de energia** da fábrica | Detecção de **congestionamento** e alertas regionais |
| Painel | <http://localhost:8080> | <http://localhost:8090> |
| Grafana | <http://localhost:3000> | <http://localhost:3010> |

Os cenários são **modelos didáticos inspirados** em casos reais, não a arquitetura real das empresas.

## Terminologia (NIST SP 500-325)

| Camada | Definição |
|---|---|
| **Edge** | Os próprios dispositivos finais com capacidade de processamento (o controlador da máquina, o celular) |
| **Fog** | Camada intermediária e hierárquica de nós (gateways, servidores, datacenters próximos) entre os dispositivos e a nuvem. Pode se organizar em níveis (vertical) ou lado a lado por região (horizontal). O gateway é um nó fog, embora o mercado costume chamá-lo de "edge gateway" |
| **Cloud** | Serviços centralizados, num datacenter distante |

**Critério para separar as camadas:**
- **escopo:** quanto do sistema o nó enxerga;
- **escala de tempo:** em quanto tempo ele precisa responder.

## Como rodar

### Na sua máquina

Pré-requisitos: Docker com Docker Compose, e Git.

```bash
git clone <url-deste-repositório>
cd edge-fog-cloud-demos

cd fabrica && docker compose up -d --build && cd ..    # Fábrica: http://localhost:8080
cd waze    && docker compose up -d --build && cd ..    # Waze:    http://localhost:8090
```

A primeira execução baixa as imagens e leva alguns minutos. Os dois projetos podem rodar ao mesmo tempo. Para parar, entre na pasta do projeto e rode `docker compose down`.

### Sem instalar nada (GitHub Codespaces)

1. No GitHub, clique em **Code → Codespaces → Create codespace**.
2. Quando o ambiente abrir, no terminal, rode os mesmos comandos acima.
3. Abra a aba **Portas** e clique no endereço da porta 8080 (Fábrica) ou 8090 (Waze).

## O que há em cada projeto

Os dois seguem os mesmos padrões, para que se reconheça a mesma arquitetura em dois contextos:

- **Cada caixa do diagrama é um nó no `docker-compose.yml`**, com o mesmo nome: o código é a arquitetura.
- **Redes Docker separadas**, imitando a rede física: um dispositivo edge não alcança a nuvem diretamente.
- **Toxiproxy** nos enlaces manipulados nos experimentos: latência, banda e quedas controladas pelo painel.
- **Dois caminhos de dados:** eventos (alertas, na hora) e lotes (resumos, economizando banda).
- **Store-and-forward** onde a rede pode cair (SQLite, fila da bridge MQTT).
- **Seletor "onde decidir"**, que move uma decisão entre as camadas e mostra o custo em latência e o risco.
- **Painel fora de banda**: observa o sistema sem depender dos enlaces que os experimentos derrubam.
- **Experimentos no ciclo prever → observar → explicar**, no roteiro de cada projeto ([fabrica/ROTEIRO.md](fabrica/ROTEIRO.md) e [waze/ROTEIRO.md](waze/ROTEIRO.md)).

| Tecnologia | Papel |
|---|---|
| Python | Simuladores edge e processadores fog |
| Mosquitto (MQTT) | Brokers nos nós fog |
| Toxiproxy | Simulação dos enlaces de rede |
| SQLite | Buffers de store-and-forward |
| FastAPI + Postgres | Serviço da nuvem |
| Grafana | Dashboard da nuvem |
| nginx + HTML/JS | Painel da demonstração |

## Estrutura

```
edge-fog-cloud-demos/
├── README.md            # este arquivo
├── .devcontainer/       # GitHub Codespaces
├── fabrica/             # Projeto Fábrica Inteligente (ver fabrica/README.md)
└── waze/                # Projeto Waze (ver waze/README.md)
```

## Para ir além

Tecnologias citadas na teoria, sem implementação aqui:
- **Processamento no edge e no fog:** TinyML, K3s, KubeEdge, MEC/5G.
- **Plataformas de IoT na borda:** AWS IoT Greengrass, Azure IoT Edge.
- **Simuladores em escala:** iFogSim, PureEdgeSim.
