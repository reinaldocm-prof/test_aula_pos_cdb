# Roteiro do experimento: Projeto Fábrica Inteligente

Neste roteiro você vai observar uma fábrica com duas linhas de produção, organizada em camadas:

- **Edge:** o controlador eletrônico embarcado em cada máquina.
- **Fog, nível inferior:** um gateway por linha de produção.
- **Fog, nível superior:** o servidor da fábrica, que enxerga todas as linhas.
- **Cloud:** um datacenter remoto, que atende várias fábricas.

Diferente do Projeto Waze, aqui o fog tem **dois níveis**, um acima do outro. Ao final, você deve conseguir explicar **por que cada decisão fica num nível e não em outro**, e o que acontece quando a decisão é colocada no lugar errado.

**Tempo estimado:** 40 a 50 minutos.

Na aula, o professor faz os experimentos na tela compartilhada e você acompanha. Depois, refaça tudo sozinho.

> **Como usar este roteiro**
>
> - 🔮 **Antes de clicar:** escreva a sua previsão. Não tem problema errar; o importante é comparar depois.
> - 👀 **Observe:** o que olhar na tela.
> - ✍️ **Responda:** explique o que aconteceu. Anote as respostas num documento à parte.

---

## Parte 0: Preparação

### 0.1 Subir o sistema

Você precisa de **uma** destas opções:

- **Na sua máquina:** Docker instalado (Docker Desktop no Windows/Mac, ou Docker Engine no Linux).
- **Sem instalar nada:** uma conta no GitHub, usando o Codespaces (veja o [README geral](../README.md)).

No terminal, dentro da pasta do repositório:

```bash
cd fabrica
docker compose up -d --build
```

Na primeira vez, o download leva alguns minutos. Quando terminar, abra no navegador:

- **Painel do experimento:** <http://localhost:8080>
- **Grafana (a visão da nuvem):** <http://localhost:3000>

No Codespaces, abra a aba **Portas** e clique no endereço da porta **8080**.

✅ **Confira:** no canto superior direito do painel, "linha 1", "linha 2", "servidor" e "controle" devem estar com a bolinha **verde**. Se alguma estiver vermelha, espere 10 segundos e recarregue a página.

### 0.2 Conheça a tela

O painel segue o desenho da arquitetura, da esquerda (edge) para a direita (cloud):

| Coluna | O que mostra |
|---|---|
| **EDGE** | As duas linhas de produção, com 3 máquinas cada. Em cada máquina: temperatura (barra colorida), consumo de energia em kW e estado (ligada, desligada ou danificada). A marca amarela na barra é **90 °C**, onde a máquina deve ser desligada; a vermelha é **110 °C**, onde ela se danifica |
| **FOG, nível inferior** | Um gateway por linha. Mostra quantas mensagens **entram** e quantas **saem** a cada janela de 5 segundos |
| **FOG, nível superior** | O servidor da fábrica. O medidor mostra o **consumo total de energia** da fábrica comparado com o **limite contratado** (a marca vermelha) |
| **CLOUD** | A nuvem, que recebe os dados em lotes |

**Outras áreas:**

| Onde | O que é |
|---|---|
| **Faixa do topo ("Onde decidir")** | Escolhe em qual camada cada decisão é tomada e mostra quanto tempo o último desligamento levou em cada uma |
| **Bolinhas nas linhas** | Mensagens viajando. Cinza: telemetria; azul: resumo; vermelha: alerta; laranja: comando; roxa: lote para a nuvem |
| **Enlaces de rede** (embaixo, à esquerda) | Liga e desliga conexões e muda a velocidade delas. **lan_linha1** e **lan_linha2** ligam cada gateway ao servidor (a rede interna da fábrica); **wan** liga a fábrica à nuvem (a internet) |
| **Eventos** (embaixo, à direita) | A história do que está acontecendo, com a camada responsável por cada coisa |

### 0.3 Estado inicial

Antes de começar (e sempre que quiser recomeçar):

1. Na faixa do topo:
   - em **"Desligamento por superaquecimento decidido no"**, clique em **edge**;
   - em **"Controle de demanda decidido no"**, clique em **servidor**.
2. Na caixa **Enlaces de rede**, clique em **Restaurar padrão**.
3. Clique em **🔧 Reparar linha** nas duas linhas. Se o botão **⚡ Pico de consumo** estiver laranja, clique nele para desligar o pico.

---

## Parte 1: Observando o sistema funcionando

Observe o sistema por 1 minuto, sem clicar em nada.

👀 **Observe**
- As bolinhas cinzas entre as máquinas e o gateway: cada máquina envia temperatura, vibração e consumo **10 vezes por segundo**.
- Nas caixas dos gateways, as linhas **"entrou (janela)"** e **"saiu (janela)"**, e o número **"N× menos dados"**.
- As bolinhas azuis entre os gateways e o servidor, e as roxas entre o servidor e a nuvem.
- O medidor de demanda no servidor da fábrica.

✍️ **Responda**

**1.1** Quantas mensagens entram num gateway a cada janela de 5 s, e quantas saem? Quantas vezes menos dados seguem para o servidor?

**1.2** A telemetria a 10 vezes por segundo nunca sai da linha de produção. Cite duas vantagens de resumir os dados já no gateway.

**1.3** Qual é o consumo total da fábrica agora, e qual é o limite? Alguma linha, sozinha, consegue saber se a fábrica está perto do limite?

---

## Experimento 1: Demanda de energia (para que serve o servidor da fábrica?)

**Objetivo:** ver uma decisão que só o nível superior do fog consegue tomar.

**Contexto:** a fábrica tem um contrato com a companhia de energia. Se o consumo total passar do limite, a fábrica paga multa. Cada linha consome cerca de 50 kW; o limite é de 110 kW. A **linha 1 tem prioridade alta** e a **linha 2, prioridade baixa**.

🔮 **Antes de clicar:** a linha 1 vai entrar num pico de produção. **O que vai acontecer com o consumo total? Quem vai perceber? O que deveria ser feito, e com qual linha?**

**Passos**
1. Na linha 1, clique em **⚡ Pico de consumo** (o botão fica laranja).
2. Observe o medidor de demanda e os Eventos por 20 segundos.
3. Clique de novo em **⚡ Pico de consumo** para encerrar o pico e observe por mais 20 segundos.
4. Repita os passos 1 a 3, agora com o pico na **linha 2**.

👀 **Observe**
- O valor máximo que o total atingiu, e se apareceu "acima do limite".
- Nos Eventos, a ordem dada pelo servidor e para qual linha.
- O consumo da linha que recebeu a ordem, na caixa do gateway dela.
- O que acontece quando o pico termina.

✍️ **Responda**

**1.4** Sua previsão estava certa? Qual linha teve a carga reduzida quando o pico foi na linha 1? E quando foi na linha 2?

**1.5** Por que essa decisão **não** pode ser tomada pelo gateway de uma linha?

**1.6** O servidor fica dentro da fábrica, e a nuvem fora. Por que é melhor que essa decisão fique no servidor e não na nuvem? (O Experimento 4 vai testar isso.)

---

## Experimento 2: Superaquecimento (onde desligar a máquina?)

**Objetivo:** medir quanto tempo leva para proteger uma máquina quando a decisão é tomada em cada camada.

**Contexto:** ao clicar em **🔥 Falha**, a refrigeração da máquina para e a temperatura sobe cerca de 2 °C por segundo. A **90 °C** a máquina precisa ser desligada; se chegar a **110 °C** ligada, ela se **danifica**.

🔮 **Antes de clicar:** quanto tempo, em milissegundos, você acha que leva entre a máquina passar de 90 °C e ser desligada, se a decisão for tomada:
- na própria máquina (edge)?
- no servidor da fábrica?
- na nuvem?

**Passos.** Faça as quatro medições e preencha a tabela.

| Medição | "Desligamento por superaquecimento decidido no" | Enlace **wan** | Resultado |
|---|---|---|---|
| A | **edge** | ativo | desligou em ______ ms |
| B | **servidor** | ativo | desligou em ______ ms |
| C | **nuvem** | ativo | desligou em ______ ms |
| D | **nuvem** | **✕ fora** | ______ |

Para cada medição:
1. Configure o modo na faixa do topo e, se for o caso, a **wan** na caixa **Enlaces de rede**.
2. Clique em **🔥 Falha** numa máquina **ligada**, sempre uma máquina diferente.
3. Acompanhe a barra de temperatura; leva cerca de 8 a 15 segundos para chegar a 90 °C.
4. Leia o tempo na faixa do topo, em **"Último desligamento por superaquecimento"**, ou no próprio cartão da máquina.

👀 **Observe**
- Nas medições B e C, o caminho das bolinhas: vermelha (alerta) subindo e laranja (comando) descendo.
- Na medição C, o aviso amarelo que aparece no topo.
- Na medição D, a temperatura e o estado final da máquina, e as mensagens vermelhas nos Eventos.

Ao terminar, volte para **edge**, religue a **wan** (ou clique em **Restaurar padrão**) e clique em **🔧 Reparar linha** nas duas linhas.

✍️ **Responda**

**2.1** Por que a medição A é praticamente instantânea?

**2.2** Explique a diferença entre B e C. Por quais conexões a mensagem passou em cada caso?

**2.3** O que aconteceu na medição D? O que isso significaria numa fábrica de verdade?

**2.4** O painel avisa que decidir o desligamento na nuvem é um **antipadrão**. Com base no que você viu, explique por que um intertravamento de segurança deve ficar sempre no edge.

---

## Experimento 3: A rede interna da fábrica cai

**Objetivo:** ver o que acontece quando um gateway perde a conexão com o servidor.

🔮 **Antes de clicar:** a conexão entre o gateway da linha 1 e o servidor vai cair por 30 segundos. **A linha 1 para de produzir? Os resumos produzidos nesse tempo se perdem?**

**Passos**
1. Na linha **lan_linha1** dos Enlaces de rede, clique em **● ativo** (vira **✕ fora**).
2. Observe por 30 segundos a linha entre o gateway-linha1 e o servidor, e as máquinas da linha 1.
3. Religue a **lan_linha1** e observe a mesma linha logo em seguida.

👀 **Observe**
- As máquinas da linha 1 continuam funcionando?
- As bolinhas azuis param de chegar ao servidor durante a queda?
- O que acontece com as bolinhas azuis logo depois de religar?

✍️ **Responda**

**3.1** A linha 1 continuou produzindo sem o servidor? Que proteção continuou funcionando?

**3.2** Os resumos produzidos durante a queda se perderam? Onde ficaram guardados?

**3.3** Esse "guardar e enviar depois" se chama **store-and-forward**. Ele tem um limite de tamanho. O que você acha que aconteceria numa queda de várias horas?

---

## Experimento 4: A internet da fábrica cai

**Objetivo:** descobrir o que depende da nuvem e o que não depende.

🔮 **Antes de clicar:** a conexão da fábrica com a nuvem (WAN) vai cair. **A fábrica para? O controle de demanda continua funcionando?**

**Parte A: decisão de demanda no servidor**
1. Confira que **"Controle de demanda decidido no"** está em **servidor**.
2. Derrube a **wan** nos Enlaces de rede.
3. Ligue o **⚡ Pico de consumo** na linha 1 e observe o medidor por 20 segundos.
4. Desligue o pico.

**Parte B: decisão de demanda na nuvem**
1. Em **"Controle de demanda decidido no"**, clique em **nuvem**. A **wan** continua fora.
2. Ligue o **⚡ Pico de consumo** na linha 1 e observe o medidor por 30 segundos.
3. Religue a **wan** e observe por mais 15 segundos.
4. Desligue o pico e volte o controle de demanda para **servidor**.

**Parte C: o que a nuvem vê**
1. Abra o **Grafana** (<http://localhost:3000>).
2. Observe o gráfico **"Idade dos dados ao chegar na nuvem"** e a tabela **"Alertas recebidos"**.

👀 **Observe**
- Na parte A: o total ficou dentro do limite?
- Na parte B: a mensagem **"⚠ acima do limite há N s"** no servidor, e o que acontece quando a WAN volta.
- A **"fila para a nuvem"** no servidor durante a queda, e o tamanho do lote enviado quando a WAN voltou.
- No Grafana: os pontos no período da queda, e as colunas **"ocorrido"** e **"chegou na nuvem"** da tabela de alertas.

✍️ **Responda**

**4.1** Na parte A, a fábrica continuou se protegendo da multa mesmo sem internet? Por quê?

**4.2** Na parte B, quanto tempo a fábrica ficou acima do limite? O que isso custaria a ela?

**4.3** Durante a queda, os dados se perderam? Onde ficaram esperando?

**4.4** Na tabela de alertas do Grafana, compare "ocorrido" e "chegou na nuvem". Com essa diferença de tempo, que tipo de trabalho faz sentido na nuvem?

---

## Síntese

**S.1** Preencha a tabela com o que você observou neste projeto:

| Camada | O que ela enxerga | Em quanto tempo precisa responder | Um exemplo de decisão neste projeto |
|---|---|---|---|
| Edge (controlador da máquina) | | | |
| Fog inferior (gateway da linha) | | | |
| Fog superior (servidor da fábrica) | | | |
| Cloud (nuvem) | | | |

**S.2** Aqui o fog tem **dois níveis**, um acima do outro. Isso é o que o NIST chama de organização **vertical** do fog. No Projeto Waze, o fog era **horizontal** (um por região). Por que cada cenário pede uma organização diferente?

**S.3** Pense num sistema que você conhece (um hospital, uma rede de supermercados, uma usina solar). Dê um exemplo de decisão para cada camada: edge, fog e nuvem.

---

## Para encerrar

Para desligar o sistema, rode `docker compose down` dentro da pasta `fabrica`.

Se algo não funcionar como descrito, veja a seção "Problemas comuns" do [README do projeto](README.md).
