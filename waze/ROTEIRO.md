# Roteiro do experimento: Projeto Waze

Neste roteiro você vai observar, na prática, como um sistema parecido com o Waze divide o trabalho entre três camadas:

- **Edge:** o celular de cada motorista.
- **Fog:** um datacenter próximo, um por região da cidade.
- **Cloud:** um datacenter distante, que atende todas as regiões.

Ao final, você deve conseguir explicar **por que cada decisão fica numa camada e não em outra**.

**Tempo estimado:** 40 a 50 minutos.

Na aula, o professor faz os experimentos na tela compartilhada e você acompanha. Depois, refaça tudo sozinho: só assim você vai conseguir responder às perguntas com segurança.

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
cd waze
docker compose up -d --build
```

Na primeira vez, o download leva alguns minutos. Quando terminar, abra no navegador:

- **Painel do experimento:** <http://localhost:8090>
- **Grafana (a visão da nuvem):** <http://localhost:3010>

No Codespaces, abra a aba **Portas** e clique no endereço da porta **8090**.

✅ **Confira:** no canto superior direito do painel, "região A", "região B" e "controle" devem estar com a bolinha **verde**. Se alguma estiver vermelha, espere 10 segundos e recarregue a página.

### 0.2 Conheça a tela

| Onde | O que é |
|---|---|
| **Faixa do topo** | Escolhe **onde** o alerta de acidente é decidido (fog ou nuvem) e mostra quanto tempo o último alerta levou. Também tem as **ferramentas** para provocar situações no mapa |
| **Dois mapas** (Região A e Região B) | Cada ponto azul é um carro. Cada rua (trecho) tem um nome, como A05 ou B12 |
| **Faixa de caixas** (celulares → fog → nuvem ← fog ← celulares) | As camadas do sistema. As bolinhas que andam nas linhas são mensagens viajando pela rede |
| **Enlaces de rede** (embaixo, à esquerda) | Liga e desliga as conexões de rede e muda a velocidade delas |
| **Eventos** (embaixo, à direita) | A história do que está acontecendo, com a camada responsável por cada coisa |

**Dois detalhes importantes do mapa:**

- **Os carros** mostram onde eles **realmente** estão. Um carro **cinza** está sem sinal de celular.
- **As cores das ruas** mostram o que o **fog concluiu** a partir das informações que recebeu:

  | Cor | Significado |
  |---|---|
  | Cinza | Nenhum carro visto ali nos últimos 10 s |
  | Verde | Livre |
  | Amarelo | Lento |
  | Vermelho | Congestionado |

  O fog não "vê" a rua: ele só sabe o que os celulares contam a ele.

### 0.3 Estado inicial

Antes de começar (e sempre que quiser recomeçar):

1. Na faixa do topo, em **"Alerta de acidente decidido no"**, clique em **fog**.
2. Clique em **Limpar ocorrências**.
3. Na caixa **Enlaces de rede**, clique em **Restaurar padrão**.

---

## Parte 1: Observando o sistema funcionando

Antes de provocar qualquer coisa, observe o sistema por 1 minuto.

👀 **Observe**
- Os carros andando nos dois mapas.
- As bolinhas cinzas saindo de **celulares-a** em direção ao **fog-a**: cada celular envia sua posição e velocidade uma vez por segundo.
- Na caixa **fog-a**, as linhas **"entrou (janela)"** e **"saiu (janela)"**. A cada 10 segundos (uma "janela"), o fog junta tudo o que recebeu e publica um resumo: o estado de cada rua.
- Nos rótulos das linhas entre as caixas, quantos dados passam por minuto em cada conexão.

✍️ **Responda**

**1.1** Anote quantas posições e quantos KB entram no fog-a por janela, e quantos KB saem. Quantas vezes menor é o que sai?

**1.2** Compare os dados por minuto na conexão **4G/5G** (celulares → fog) e na **WAN** (fog → nuvem). Por que a nuvem não precisa receber cada posição de cada carro?

---

## Experimento 1: Trânsito lento (quem descobre?)

**Objetivo:** descobrir qual camada consegue perceber um congestionamento.

🔮 **Antes de clicar:** um trecho vai ficar lento, como numa hora de pico. Nenhum motorista vai reportar nada. **Algum celular vai descobrir sozinho que há congestionamento? Quem vai descobrir?** Escreva sua previsão.

**Passos**
1. Na faixa do topo, em **"Clique no mapa para provocar"**, clique em **🐢 Lentidão**. O botão fica laranja: essa é a ferramenta ativa.
2. No mapa da Região A, clique **em cima de uma rua** (a linha entre dois cruzamentos) que tenha **2 ou mais carros** por perto. Prefira uma rua do meio do mapa, onde passam mais carros. Aparece um 🐢 na rua. Anote o nome do trecho (ex.: A05).
3. Espere **até 1 minuto**, olhando o mapa e os **Eventos**. O 🐢 aparece na hora, mas a rua só muda de cor quando o fog conclui algo.

👀 **Observe**
- Quanto tempo leva até a rua ficar **vermelha**?
- Qual mensagem aparece nos Eventos? Qual camada aparece ao lado dela (edge, fog ou cloud)?
- O número de **desvios** na caixa **celulares-a**.
- Os carros que chegam ao cruzamento escolhem a rua lenta ou outra?

✍️ **Responda**

**1.3** Sua previsão estava certa? Quem detectou o congestionamento?

**1.4** Um carro parado sozinho numa rua pode estar apenas num semáforo. Por que **um celular sozinho** não pode concluir que há congestionamento?

**1.5** O fog levou algumas dezenas de segundos para detectar. Por que essa decisão **não** é tomada na nuvem, que também receberia os dados?

**1.6** O que os carros fizeram depois de receber o alerta? Quem decidiu o desvio: o fog ou o celular?

Ao terminar, clique em **Limpar ocorrências**.

---

## Experimento 2: Acidente (decidir perto ou longe?)

**Objetivo:** medir quanto custa tomar uma decisão urgente longe dos carros.

Desta vez, um motorista que passar pelo acidente vai **reportá-lo** pelo aplicativo. A pergunta é: quem decide avisar os outros carros, e quanto tempo isso leva?

🔮 **Antes de clicar:** o alerta vai chegar mais rápido aos outros carros se for decidido no **fog** ou na **nuvem**? Quanto mais rápido, na sua estimativa?

**Passos.** Faça as quatro medições e preencha a tabela.

| Medição | Como configurar | Tempo até os carros serem avisados |
|---|---|---|
| A | Decidido no **fog** | ______ ms |
| B | Decidido na **nuvem** | ______ ms |
| C | Na **nuvem**, com WAN lenta (RTT 3000 ms) | ______ ms |
| D | Na **nuvem**, com a **WAN fora** | ______ |

Para cada medição:
1. Configure como indicado na tabela:
   - o modo fica na faixa do topo;
   - o RTT e o estado da WAN ficam na linha **wan** da caixa **Enlaces de rede** (a terceira linha, abaixo de celular_a e celular_b).
2. Clique em **🚧 Acidente** e depois numa rua **com carros se aproximando**, sempre uma rua diferente. (Num trecho que já tem acidente, os carros desviam e ninguém mais reporta.)
3. Espere um carro passar pela rua. Nos Eventos aparece "Carro … reportou o acidente".
4. Leia o tempo em **"Último alerta de acidente"**, na faixa do topo. Na medição D, leia os Eventos.

👀 **Observe**
- Na medição B, as bolinhas laranjas (alerta) passando pela **WAN** antes de descer para os celulares.
- Na medição D, as mensagens em vermelho nos Eventos e o que os carros fazem perto do 🚧.
- Nas medições B, C e D: alguns segundos depois do alerta de acidente, aparece outro alerta para a mesma rua. De que tipo ele é, e onde foi decidido?

Ao terminar, volte para **fog**, clique em **Restaurar padrão** e em **Limpar ocorrências**.

✍️ **Responda**

**2.1** Por que a medição B é mais lenta que a A? Por onde a mensagem passou a mais?

**2.2** Na medição D, os carros foram avisados do acidente? O que aconteceu com quem estava chegando?

**2.3** Na medição D, se você esperou mais um pouco, o trecho pode ter ficado vermelho mesmo sem a nuvem. Que mecanismo do Experimento 1 ajudou?

**2.4** Para um alerta de acidente, alguns segundos fazem diferença? Onde essa decisão deve ficar, e por quê?

**2.5** Mesmo com a decisão na **nuvem**, apareceu um alerta de **congestionamento** decidido no **fog**. Por que o seletor não mudou esse alerta? Por que faz sentido que o congestionamento seja sempre decidido no fog?

---

## Experimento 3: Túnel (o celular sem sinal)

**Objetivo:** ver o que o edge faz quando perde a conexão.

🔮 **Antes de clicar:** um carro vai entrar num túnel e ficar 20 segundos sem sinal. **O aplicativo vai parar de funcionar? O que acontece com a posição que ele deveria enviar a cada segundo?**

**Passos**
1. Clique em **🚇 Túnel (clique num carro)**.
2. No mapa, clique em **um carro**.
3. Acompanhe esse carro por 25 segundos.
4. *Desafio opcional:* enquanto ele estiver no túnel, mude para **🚧 Acidente** e clique na rua para onde ele vai. Se ele passar pelo acidente sem sinal, observe o que acontece com o report.

👀 **Observe**
- A cor do carro e o texto "sem sinal · N guardados".
- O carro continua andando?
- Nos Eventos: "entrou num túnel", "saiu do túnel" e "sincronizou".

✍️ **Responda**

**3.1** O carro continuou navegando sem sinal? Como isso é possível, se o mapa está "na internet"?

**3.2** O que significa o número "N guardados"? Para onde foram esses dados quando o sinal voltou?

**3.3** Enquanto estava no túnel, esse carro recebeu os alertas que os outros receberam? Que risco isso traz?

**3.4** Esse comportamento ("guardar e enviar depois") se chama **store-and-forward**. Cite outro aplicativo do seu dia a dia que faz isso.

---

## Experimento 4: Rede celular da região fora

**Objetivo:** ver o fog perder a visão de uma região inteira.

🔮 **Antes de clicar:** a rede celular de toda a Região A vai cair. **O que vai acontecer com os carros? E com o mapa do fog?**

**Passos**
1. Na caixa **Enlaces de rede**, na linha **celular_a**, clique em **● ativo** (vira **✕ fora**).
2. Observe o mapa da Região A por 15 segundos.
3. Clique em **🚧 Acidente** e numa rua da Região A com carros chegando.
4. Espere mais 10 segundos e religue **celular_a** (clique em **✕ fora**).

👀 **Observe**
- Os carros da Região A (cor e movimento) e as cores das ruas da Região A.
- A linha entre **celulares-a** e **fog-a** na faixa de caixas.
- Nos Eventos, depois de religar: "sincronizaram", "report ATRASADO" e o alerta do acidente.

✍️ **Responda**

**4.1** Os carros pararam? Por que o mapa do fog-a ficou cinza, se os carros continuavam lá?

**4.2** Quanto tempo depois do acidente o fog recebeu o report? Por que tanto?

**4.3** A Região B foi afetada? O que isso mostra sobre ter um fog **por região**?

---

## Experimento 5: Nuvem fora

**Objetivo:** descobrir o que depende da nuvem e o que não depende.

🔮 **Antes de clicar:** a conexão com a nuvem vai cair. **Os alertas de trânsito vão continuar funcionando?**

**Passos**
1. Confira que o modo está em **fog**.
2. Na linha **wan** dos Enlaces de rede, clique em **● ativo** (vira **✕ fora**).
3. Provoque uma **🐢 Lentidão** e um **🚧 Acidente** em qualquer região.
4. Observe por 1 minuto e depois religue a **wan**.
5. Abra o **Grafana** (<http://localhost:3010>).

👀 **Observe**
- Os alertas continuam chegando aos carros?
- A **"fila p/ nuvem"** nas caixas **fog-a** e **fog-b** durante a queda.
- Nos Eventos, depois de religar: "enviou à nuvem um lote de N itens".
- No Grafana, o gráfico **"Idade dos dados ao chegar na nuvem"**: o que acontece com os pontos no período da queda?
- No Grafana, a tabela **"Histórico: trechos que mais travam"**.

✍️ **Responda**

**5.1** A cidade continuou funcionando sem a nuvem? O que parou de funcionar?

**5.2** Os dados gerados durante a queda se perderam? Onde ficaram esperando?

**5.3** A tabela "trechos que mais travam" junta as duas regiões e um período longo. Por que essa análise faz sentido na **nuvem** e não no fog?

**5.4** O gráfico de "idade dos dados" mostra que a nuvem recebe tudo com dezenas de segundos de atraso. Que tipo de decisão a nuvem **pode** tomar bem, e que tipo **não pode**?

---

## Síntese

**S.1** Preencha a tabela com o que você observou neste projeto:

| Camada | O que ela enxerga | Em quanto tempo precisa responder | Um exemplo de decisão neste projeto |
|---|---|---|---|
| Edge (celular) | | | |
| Fog (datacenter da região) | | | |
| Cloud (nuvem) | | | |

**S.2** Neste projeto existe um fog **por região**, lado a lado. Isso é o que o NIST chama de organização **horizontal** do fog. Que vantagem você observou nos experimentos 4 e 5?

**S.3** Pense num sistema que você conhece (um app de entregas, uma rede de lojas, um carro conectado). Dê um exemplo de decisão que deveria ficar no edge, um no fog e um na nuvem.

---

## Para encerrar

Para desligar o sistema, rode `docker compose down` dentro da pasta `waze`.

Se algo não funcionar como descrito, veja a seção "Problemas comuns" do [README do projeto](README.md).
