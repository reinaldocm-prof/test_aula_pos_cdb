/*
 * Painel da demonstração (Fábrica Inteligente).
 *
 * O painel é um OBSERVADOR FORA DE BANDA: assina cada broker por WebSocket
 * (via nginx em /mqtt/...), sem passar pelos enlaces que os experimentos
 * derrubam. Assim, quando a LAN ou a WAN cai, a turma vê o sistema falhando,
 * não o observador.
 *
 * O que ele assina:
 *   broker da linha N:  linhaN/maquina/+/telemetria   (estado das máquinas)
 *                       fabrica/linhaN/comando        (comando chegando ao gateway)
 *                       viz/eventos                   (eventos de máquinas e gateway)
 *   broker do servidor: fabrica/+/resumo, fabrica/+/alerta (chegando ao servidor)
 *                       viz/eventos                   (demanda, lotes, fila)
 *
 * Os disparos (falha, pico, reparo) são publicados direto nos brokers das
 * linhas. O controle dos enlaces passa pela painel-api (/api/enlaces).
 *
 * "Onde decidir" é publicado como mensagem RETIDA em config/modo nos três
 * brokers (máquinas e servidor leem dali; sobrevive a reinícios).
 */

const LINHAS = [1, 2];
const MAQUINAS = ["m1", "m2", "m3"];
const ESCALA_DEMANDA_KW = 140;   // fundo de escala do medidor de demanda

const $ = (seletor, raiz = document) => raiz.querySelector(seletor);

const estado = {
  enlaces: {},                       // nome -> {ativo, rtt_ms, banda_kbps, descricao}
  pico: { 1: false, 2: false },
  bytes: { local1: [], local2: [], lan_linha1: [], lan_linha2: [], wan: [] },  // [t, bytes]
  ultimoLote: null,
  totalNuvem: 0,
  contTelemetria: { 1: 0, 2: 0 },
  modo: { superaquecimento: "edge", demanda: "servidor" },
};
const clientes = {};

// =============================================================================
// Montagem da página
// =============================================================================
for (const linha of LINHAS) {
  const grupo = $(`#maq-l${linha}`);
  for (const id of MAQUINAS) {
    const card = $("#tpl-maquina").content.firstElementChild.cloneNode(true);
    card.id = `l${linha}-${id}`;
    $(".m-id", card).textContent = id;
    $(".fogo", card).onclick = () => publicar(linha, `linha${linha}/simulacao/superaquecer`, id);
    $(".maquinas", grupo).appendChild(card);
  }
  $(".pico", grupo).onclick = () =>
    publicar(linha, `linha${linha}/simulacao/pico`, estado.pico[linha] ? "off" : "on");
  $(".religar", grupo).onclick = () => publicar(linha, `linha${linha}/simulacao/religar`, "todas");
}

for (const seletor of document.querySelectorAll(".seletor")) {
  for (const botao of seletor.querySelectorAll("button")) {
    botao.onclick = () => definirModo(seletor.dataset.chave, botao.dataset.valor);
  }
}

// No GitHub Codespaces cada porta tem um endereço próprio (...-3000.app.github.dev).
function enderecoDaPorta(porta) {
  if (/-\d+\.app\.github\.dev$/.test(location.hostname)) {
    return `${location.protocol}//${location.hostname.replace(/-\d+\.app\.github\.dev$/, `-${porta}.app.github.dev`)}`;
  }
  return `${location.protocol}//${location.hostname}:${porta}`;
}
const urlGrafana = enderecoDaPorta(3000);
$("#link-grafana").href = urlGrafana;
$("#nv-grafana").href = urlGrafana;

// =============================================================================
// Registro de eventos
// =============================================================================
function registrar(texto, camada = "", nivel = "") {
  const li = document.createElement("li");
  li.className = nivel;
  const hora = new Date().toLocaleTimeString("pt-BR");
  li.innerHTML = `<time>${hora}</time><span class="camada">${camada}</span>${texto}`;
  const log = $("#log");
  log.prepend(li);
  while (log.children.length > 60) log.lastChild.remove();
}

// =============================================================================
// MQTT
// =============================================================================
function conectar(nome, caminho, topicos, aoReceber) {
  const protocolo = location.protocol === "https:" ? "wss:" : "ws:";
  const cliente = mqtt.connect(`${protocolo}//${location.host}${caminho}`, {
    clientId: `painel-${nome}-${Math.random().toString(16).slice(2, 8)}`,
    reconnectPeriod: 2000,
  });
  cliente.on("connect", () => {
    cliente.subscribe(topicos);
    $(`#con-${nome}`).classList.add("ok");
  });
  cliente.on("close", () => $(`#con-${nome}`).classList.remove("ok"));
  cliente.on("message", (topico, payload) => {
    let dados;
    try { dados = JSON.parse(payload.toString()); } catch { return; }
    aoReceber(topico, dados, payload.length);
  });
  return cliente;
}

function publicar(linha, topico, mensagem) {
  clientes[`linha${linha}`]?.publish(topico, mensagem, { qos: 1 });
}

for (const linha of LINHAS) {
  clientes[`linha${linha}`] = conectar(`linha${linha}`, `/mqtt/linha${linha}`,
    [`linha${linha}/maquina/+/telemetria`, `fabrica/linha${linha}/comando`, "viz/eventos"],
    (topico, dados) => mensagemDaLinha(linha, topico, dados));
}
clientes.servidor = conectar("servidor", "/mqtt/servidor",
  ["fabrica/+/resumo", "fabrica/+/alerta", "viz/eventos", "config/modo"], mensagemDoServidor);

// =============================================================================
// Onde decidir
// =============================================================================
const AVISOS = {
  edge: "",
  servidor: "⚠ O desligamento agora depende da LAN e do servidor da fábrica.",
  nuvem: "⚠ ANTIPADRÃO: o intertravamento de segurança saiu do edge e depende da WAN. Em sistemas reais, isso nunca é feito.",
};

function definirModo(chave, valor) {
  const novo = { ...estado.modo, [chave]: valor };
  // Retido em todos os brokers: máquinas (linhas) e servidor recebem.
  for (const cliente of Object.values(clientes)) {
    cliente.publish("config/modo", JSON.stringify(novo), { qos: 1, retain: true });
  }
  const nomes = { superaquecimento: "Desligamento por superaquecimento", demanda: "Controle de demanda" };
  registrar(`${nomes[chave]} agora é decidido no <b>${valor}</b>`, "modo", "decisao");
  mostrarModo(novo);
}

function mostrarModo(modo) {
  estado.modo = modo;
  for (const seletor of document.querySelectorAll(".seletor")) {
    for (const botao of seletor.querySelectorAll("button")) {
      const escolhido = modo[seletor.dataset.chave] === botao.dataset.valor;
      botao.classList.toggle("escolhido", escolhido);
      botao.classList.toggle("longe", escolhido && botao.dataset.valor === "nuvem");
    }
  }
  $("#aviso-modo").textContent = AVISOS[modo.superaquecimento] ||
    (modo.demanda === "nuvem" ? "⚠ O controle de demanda agora depende da WAN." : "");
}
mostrarModo(estado.modo);

// =============================================================================
// Mensagens vistas no broker de cada LINHA
// =============================================================================
function mensagemDaLinha(linha, topico, dados) {
  if (topico.endsWith("/telemetria")) {
    atualizarMaquina(linha, dados);
    // 30 mensagens/s por linha: anima só uma parte, para não poluir.
    if (++estado.contTelemetria[linha] % 5 === 0) animar(`local${linha}`, "ida", "telemetria");
    return;
  }
  if (topico === `fabrica/linha${linha}/comando`) {
    // O comando do servidor chegou ao gateway (atravessou a LAN).
    animar(`lan_linha${linha}`, "volta", "comando");
    return;
  }
  if (topico !== "viz/eventos") return;

  if (dados.no.startsWith("maquinas")) eventoDeMaquina(linha, dados);
  else if (dados.no.startsWith("gateway")) eventoDeGateway(linha, dados);
}

function atualizarMaquina(linha, t) {
  const card = $(`#l${linha}-${t.maquina}`);
  if (!card) return;
  if (t.pico !== undefined && t.pico !== estado.pico[linha]) {
    estado.pico[linha] = t.pico;
    $(`#maq-l${linha} .pico`).classList.toggle("ativo", t.pico);
  }
  card.className = `maquina ${t.estado}`;
  $(".m-estado", card).textContent = t.estado;
  $(".m-temp", card).textContent = t.temperatura.toFixed(1);
  $(".m-kw", card).textContent = t.consumo_kw.toFixed(1);
  const nivel = $(".nivel", card);
  nivel.style.width = `${Math.min(100, (t.temperatura / 120) * 100)}%`;
  nivel.style.background = t.temperatura >= 90 ? "var(--vermelho)"
    : t.temperatura >= 80 ? "var(--amarelo)" : "var(--verde)";
}

function eventoDeMaquina(linha, e) {
  const card = $(`#l${linha}-${e.maquina}`);
  const nome = `L${linha} ${e.maquina}`;
  switch (e.tipo) {
    case "falha_refrigeracao":
      if (card) $(".m-decisao", card).textContent = "refrigeração falhou";
      registrar(`${nome}: falha de refrigeração, temperatura subindo`, "edge", "aviso");
      break;
    case "superaquecimento_detectado":
      registrar(`${nome}: passou de 90 °C`, "edge", "aviso");
      break;
    case "desligada": {
      const ms = e.latencia_ms == null ? "" : ` em ${e.latencia_ms} ms`;
      if (e.latencia_ms != null) {
        for (const c of ["edge", "servidor", "nuvem"]) $(`#lat-${c}`).classList.remove("recente");
        const alvo = $(`#lat-${e.decidido_em}`);
        if (alvo) {
          alvo.textContent = `${e.decidido_em} ${e.latencia_ms} ms`;
          alvo.classList.add("recente");
        }
      }
      if (card) $(".m-decisao", card).textContent = `desligou${ms} · ${e.decidido_em}`;
      registrar(`${nome}: desligada${ms}, decidido no <b>${e.decidido_em}</b>`, "edge", "decisao");
      break;
    }
    case "comando_obsoleto_ignorado":
      registrar(`${nome}: comando atrasado de desligar (decidido no ${e.decidido_em}) IGNORADO: ` +
        "a máquina não está superaquecendo", "edge", "aviso");
      break;
    case "danificada":
      if (card) $(".m-decisao", card).textContent = "DANIFICADA";
      registrar(`${nome}: DANIFICADA a ${e.temperatura} °C (ninguém desligou a tempo)`, "edge", "erro");
      break;
    case "religada":
      if (card) $(".m-decisao", card).textContent = "";
      if (e.maquina === "m1") registrar(`Linha ${linha}: máquinas reparadas`, "edge", "ok");
      break;
    case "pico_on":
    case "pico_off":
      estado.pico[linha] = e.tipo === "pico_on";
      $(`#maq-l${linha} .pico`).classList.toggle("ativo", estado.pico[linha]);
      registrar(`Linha ${linha}: pico de consumo ${estado.pico[linha] ? "iniciado" : "encerrado"}`,
        "edge", estado.pico[linha] ? "aviso" : "ok");
      break;
  }
}

function eventoDeGateway(linha, e) {
  if (e.tipo === "resumo_publicado") {
    const gw = $(`#gw-l${linha}`);
    $(".g-kw", gw).textContent = `${e.consumo_total_kw.toFixed(1)} kW`;
    $(".g-in", gw).textContent = `${e.mensagens_recebidas} msgs · ${formatarBytes(e.bytes_recebidos)}`;
    $(".g-out", gw).textContent = `1 msg · ${formatarBytes(e.bytes_enviados)}`;
    $(".g-reducao", gw).textContent = `${e.reducao}× menos dados`;
    contarBytes(`local${linha}`, e.bytes_recebidos);
  } else if (e.tipo === "comando_repassado") {
    animar(`local${linha}`, "volta", "comando");
  }
}

// =============================================================================
// Mensagens vistas no broker do SERVIDOR DA FÁBRICA
// =============================================================================
function mensagemDoServidor(topico, dados, tamanho) {
  if (topico === "config/modo") {
    mostrarModo({ ...estado.modo, ...dados });
    return;
  }
  // Bytes da LAN contados na CHEGADA ao servidor: é o que de fato atravessou
  // o enlace (com o enlace fora, nada; ao voltar, a rajada da fila da bridge).
  if (topico.endsWith("/resumo")) {
    animar(`lan_linha${dados.linha}`, "ida", "resumo");
    contarBytes(`lan_linha${dados.linha}`, tamanho);
    return;
  }
  if (topico.endsWith("/alerta")) {
    animar(`lan_linha${dados.linha}`, "ida", "alerta");
    contarBytes(`lan_linha${dados.linha}`, tamanho);
    const atrasoMs = Math.max(0, Date.now() - dados.ts * 1000).toFixed(0);
    if (dados.tipo === "superaquecimento_detectado" || dados.tipo === "danificada") {
      $("#srv-alerta").innerHTML =
        `<b>L${dados.linha} ${dados.maquina}: ${dados.tipo.replace("_", " ")}</b><br>` +
        `chegou ao servidor ${atrasoMs} ms após o evento`;
      registrar(`Alerta L${dados.linha} ${dados.maquina} chegou ao servidor (${atrasoMs} ms)`, "fog");
    }
    return;
  }
  if (topico !== "viz/eventos" || dados.no !== "servidor-fabrica") return;

  switch (dados.tipo) {
    case "demanda":
      atualizarDemanda(dados);
      break;
    case "comando_enviado": {
      const c = dados.comando;
      const onde = c.decidido_em;
      if (onde === "nuvem") animar("wan", "volta", "comando");   // a resposta veio da nuvem
      if (c.acao === "desligar") {
        registrar(`Comando: desligar L${dados.linha} ${c.maquina} (decidido no <b>${onde}</b>)`,
          onde === "nuvem" ? "cloud" : "fog", "decisao");
      } else {
        const acao = c.acao === "reduzir_carga" ? "reduzir a carga" : "restaurar a carga";
        const tempo = dados.acima_do_limite_s != null ? `, ${dados.acima_do_limite_s} s acima do limite` : "";
        registrar(`Linha ${dados.linha}: <b>${acao}</b> (decidido no <b>${onde}</b>${tempo})`,
          onde === "nuvem" ? "cloud" : "fog", "decisao");
      }
      break;
    }
    case "decisao_nuvem_falhou":
      if (dados.assunto === "demanda") {
        // Uma consulta por resumo: registra no máximo uma vez a cada 10 s.
        if (Date.now() - (estado.ultimaFalhaDemanda ?? 0) > 10_000) {
          estado.ultimaFalhaDemanda = Date.now();
          registrar("Nuvem não respondeu: controle de demanda sem decisão", "cloud", "erro");
        }
      } else if (dados.tentativa === 1 || dados.tentativa % 4 === 0) {
        registrar(`Nuvem não respondeu: L${dados.linha} ${dados.maquina} continua sem comando de desligamento ` +
          `(tentativa ${dados.tentativa})`, "cloud", "erro");
      }
      break;
    case "lote_enviado":
      animar("wan", "ida", "lote");
      estado.ultimoLote = Date.now();
      estado.totalNuvem += dados.itens;
      $("#nv-itens").textContent = `${dados.itens} (${formatarBytes(dados.bytes)})`;
      $("#nv-total").textContent = estado.totalNuvem;
      $("#srv-fila").textContent = `${dados.fila} itens`;
      contarBytes("wan", dados.bytes);
      registrar(`Lote enviado à nuvem: ${dados.itens} itens`, "cloud", dados.itens > 15 ? "ok" : "");
      break;
    case "nuvem_inacessivel":
      $("#srv-fila").textContent = `${dados.fila} itens`;
      registrar(`Nuvem inacessível: ${dados.fila} itens aguardando na fila do servidor`, "fog", "aviso");
      break;
  }
}

function atualizarDemanda(d) {
  const l1 = d.linhas_kw["1"] ?? 0;
  const l2 = d.linhas_kw["2"] ?? 0;
  $("#dem-l1").style.width = `${(l1 / ESCALA_DEMANDA_KW) * 100}%`;
  $("#dem-l2").style.width = `${(l2 / ESCALA_DEMANDA_KW) * 100}%`;
  $("#dem-limite").style.left = `${(d.limite_kw / ESCALA_DEMANDA_KW) * 100}%`;
  $("#dem-max").textContent = d.limite_kw.toFixed(0);
  const excedido = d.total_kw > d.limite_kw;
  $("#dem-total").textContent = d.total_kw.toFixed(1);
  $("#dem-total").classList.toggle("excedido", excedido);
  $(".medidor").classList.toggle("excedido", excedido);
  $("#dem-acima").textContent = d.acima_do_limite_s > 0
    ? `⚠ acima do limite há ${d.acima_do_limite_s.toFixed(0)} s` : "";
  $("#dem-reduzidas").innerHTML = d.linhas_reduzidas
    .map((l) => `<span class="chip">linha ${l} com carga reduzida</span>`).join("");
}

// =============================================================================
// Enlaces: desenho (SVG) e pontos animados
// =============================================================================
// Cada enlace liga a borda direita de um elemento à borda esquerda de outro.
const DEFINICAO_ENLACES = {
  local1:     { de: "#maq-l1", para: "#gw-l1", nome: "rede da linha", local: true },
  local2:     { de: "#maq-l2", para: "#gw-l2", nome: "rede da linha", local: true },
  lan_linha1: { de: "#gw-l1",  para: "#srv",   nome: "LAN", desvio: -24 },
  lan_linha2: { de: "#gw-l2",  para: "#srv",   nome: "LAN", desvio: 24 },
  wan:        { de: "#srv",    para: "#nuvem", nome: "WAN" },
};
const geometria = {};
const pontos = [];

function calcularGeometria() {
  const base = $("#topologia").getBoundingClientRect();
  for (const [nome, def] of Object.entries(DEFINICAO_ENLACES)) {
    const a = $(def.de).getBoundingClientRect();
    const b = $(def.para).getBoundingClientRect();
    geometria[nome] = {
      x1: a.right - base.left, y1: a.top + a.height / 2 - base.top,
      x2: b.left - base.left,  y2: b.top + b.height / 2 - base.top + (def.desvio ?? 0),
    };
  }
  desenharEnlaces();
}

function enlaceFora(nome) {
  return estado.enlaces[nome] && !estado.enlaces[nome].ativo;
}

function desenharEnlaces() {
  let linhas = "";
  let rotulos = "";
  for (const [nome, def] of Object.entries(DEFINICAO_ENLACES)) {
    const g = geometria[nome];
    if (!g) continue;
    const classe = def.local ? "enlace local" : `enlace${enlaceFora(nome) ? " fora" : ""}`;
    linhas += `<line class="${classe}" x1="${g.x1}" y1="${g.y1}" x2="${g.x2}" y2="${g.y2}"/>`;

    const mx = (g.x1 + g.x2) / 2;
    const my = (g.y1 + g.y2) / 2;
    const info = estado.enlaces[nome];
    let titulo = def.nome;
    if (info) titulo += info.ativo ? ` · ${info.rtt_ms} ms` : " · FORA";
    const classeTitulo = enlaceFora(nome) ? "rotulo-enlace fora" : "rotulo-enlace nome";
    rotulos += `<text class="${classeTitulo}" x="${mx}" y="${my - 18}">${titulo}</text>`;
    rotulos += `<text class="rotulo-enlace" x="${mx}" y="${my - 6}">${formatarBytes(bytesPorMinuto(nome))}/min</text>`;
  }
  $("#linhas-enlace").innerHTML = linhas;
  $("#rotulos").innerHTML = rotulos;
}

function animar(enlace, sentido, tipo) {
  if (!geometria[enlace] || enlaceFora(enlace)) return;
  // A duração é só visual, mas cresce com o RTT: a WAN lenta "parece" lenta.
  const rtt = estado.enlaces[enlace]?.rtt_ms ?? 0;
  pontos.push({ enlace, sentido, tipo, inicio: performance.now(), duracao: 700 + rtt / 2 });
}

function quadro(agora) {
  let html = "";
  for (let i = pontos.length - 1; i >= 0; i--) {
    const p = pontos[i];
    const progresso = (agora - p.inicio) / p.duracao;
    if (progresso >= 1) { pontos.splice(i, 1); continue; }
    const g = geometria[p.enlace];
    const f = p.sentido === "ida" ? progresso : 1 - progresso;
    const x = g.x1 + (g.x2 - g.x1) * f;
    const y = g.y1 + (g.y2 - g.y1) * f;
    const raio = p.tipo === "telemetria" ? 3 : 7;
    const classe = p.tipo === "alerta" ? "alerta-p" : p.tipo;
    html += `<circle class="${classe}" cx="${x}" cy="${y}" r="${raio}"/>`;
  }
  $("#pontos").innerHTML = html;
  requestAnimationFrame(quadro);
}
requestAnimationFrame(quadro);

window.addEventListener("resize", calcularGeometria);
new ResizeObserver(calcularGeometria).observe($("#topologia"));

// =============================================================================
// Bytes por enlace (a partir dos eventos de visualização)
// =============================================================================
function contarBytes(enlace, bytes) {
  estado.bytes[enlace].push([Date.now(), bytes]);
}

function bytesPorMinuto(enlace) {
  const limite = Date.now() - 60_000;
  const lista = estado.bytes[enlace] ?? [];
  while (lista.length && lista[0][0] < limite) lista.shift();
  return lista.reduce((soma, [, b]) => soma + b, 0);
}

function formatarBytes(b) {
  if (b >= 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

// =============================================================================
// Controle dos enlaces (painel-api -> Toxiproxy)
// =============================================================================
const OPCOES_RTT = { lan_linha1: [4, 50, 500], lan_linha2: [4, 50, 500], wan: [200, 1000, 3000] };
const OPCOES_BANDA = [0, 256, 64, 16];

async function chamarApi(caminho, corpo) {
  try {
    const resposta = await fetch(`/api/enlaces${caminho}`, corpo === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
    });
    if (!resposta.ok) throw new Error(resposta.status);
    const lista = await resposta.json();
    estado.enlaces = Object.fromEntries(lista.map((e) => [e.nome, e]));
    $("#con-api").classList.add("ok");
    desenharTabela();
    desenharEnlaces();
  } catch {
    $("#con-api").classList.remove("ok");
  }
}

async function alterarEnlace(nome, alteracao) {
  await chamarApi(`/${nome}`, alteracao);
  const e = estado.enlaces[nome];
  if (alteracao.ativo !== undefined) {
    registrar(`Enlace ${nome} ${alteracao.ativo ? "religado" : "DERRUBADO"}`, "rede",
      alteracao.ativo ? "ok" : "erro");
  } else if (e) {
    registrar(`Enlace ${nome}: RTT ${e.rtt_ms} ms, banda ${e.banda_kbps ? e.banda_kbps + " kbps" : "sem limite"}`, "rede", "aviso");
  }
}

function desenharTabela() {
  const corpo = $("#tabela-enlaces");
  corpo.innerHTML = "";
  for (const e of Object.values(estado.enlaces)) {
    const tr = document.createElement("tr");
    const rtts = [...new Set([...OPCOES_RTT[e.nome], e.rtt_ms])].sort((a, b) => a - b);
    const bandas = [...new Set([...OPCOES_BANDA, e.banda_kbps])];
    tr.innerHTML = `
      <td><b>${e.nome}</b><small>${e.descricao}</small></td>
      <td><button class="alternar ${e.ativo ? "" : "ativo"}">${e.ativo ? "● ativo" : "✕ fora"}</button></td>
      <td><select class="rtt">${rtts.map((v) =>
        `<option value="${v}" ${v === e.rtt_ms ? "selected" : ""}>${v} ms</option>`).join("")}</select></td>
      <td><select class="banda">${bandas.map((v) =>
        `<option value="${v}" ${v === e.banda_kbps ? "selected" : ""}>${v ? v + " kbps" : "sem limite"}</option>`).join("")}</select></td>`;
    $(".alternar", tr).onclick = () => alterarEnlace(e.nome, { ativo: !e.ativo });
    $(".rtt", tr).onchange = (ev) => alterarEnlace(e.nome, { rtt_ms: Number(ev.target.value) });
    $(".banda", tr).onchange = (ev) => alterarEnlace(e.nome, { banda_kbps: Number(ev.target.value) });
    corpo.appendChild(tr);
  }
}

$("#restaurar").onclick = async () => {
  await chamarApi("/restaurar", {});
  registrar("Enlaces restaurados ao padrão", "rede", "ok");
};

// =============================================================================
// Atualizações periódicas
// =============================================================================
chamarApi("");
setInterval(() => chamarApi(""), 5000);   // outro professor/aba pode ter mudado algo

setInterval(() => {
  if (estado.ultimoLote) {
    const s = Math.round((Date.now() - estado.ultimoLote) / 1000);
    $("#nv-ultimo").textContent = `há ${s} s`;
  }
  desenharEnlaces();   // atualiza bytes/min nos rótulos
}, 1000);

registrar("Painel iniciado. Aguardando dados dos brokers…", "", "");
