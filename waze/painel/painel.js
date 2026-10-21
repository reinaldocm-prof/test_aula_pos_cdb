/*
 * Painel da demonstração (Waze).
 *
 * Dois pontos de vista na mesma tela:
 *   - os CARROS no mapa vêm da instrumentação da simulação (viz/carros):
 *     é a "verdade de campo", onde os carros realmente estão, inclusive os
 *     que estão sem sinal;
 *   - as CORES dos trechos vêm do fog (regiao/{r}/transito): é o que o fog
 *     CONCLUIU a partir das posições que recebeu.
 * A diferença entre os dois é parte da aula (ex.: o carro no túnel some
 * para o fog, mas continua andando).
 *
 * O painel é um observador FORA DE BANDA: assina os brokers dos fogs por
 * WebSocket (via nginx em /mqtt/a e /mqtt/b), sem passar pelos enlaces que
 * os experimentos derrubam.
 */

const REGIOES = ["a", "b"];
const ESCALA = { x0: 40, y0: 28, dx: 130, dy: 92 };   // grade do mapa -> pixels do SVG

const $ = (seletor, raiz = document) => raiz.querySelector(seletor);
const svgNS = "http://www.w3.org/2000/svg";

const estado = {
  enlaces: {},
  modo: { acidente: "fog" },
  ferramenta: "acidente",
  mapas: {},                         // regiao -> {trechos}
  ocorrencias: { a: {}, b: {} },     // trecho -> "acidente" | "lentidao"
  desvios: { a: 0, b: 0 },
  ultimoDesvioLog: {},               // trecho -> ts (evita poluir o registro)
  bytes: { celular_a: [], celular_b: [], wan_a: [], wan_b: [] },
  contPosicoes: { a: 0, b: 0 },
  ultimoLote: null,
  totalNuvem: 0,
};
const clientes = {};

// =============================================================================
// Registro de eventos
// =============================================================================
function registrar(texto, camada = "", nivel = "") {
  const li = document.createElement("li");
  li.className = nivel;
  li.innerHTML = `<time>${new Date().toLocaleTimeString("pt-BR")}</time><span class="camada">${camada}</span>${texto}`;
  const log = $("#log");
  log.prepend(li);
  while (log.children.length > 60) log.lastChild.remove();
}

// =============================================================================
// MQTT
// =============================================================================
function conectar(regiao) {
  const protocolo = location.protocol === "https:" ? "wss:" : "ws:";
  const cliente = mqtt.connect(`${protocolo}//${location.host}/mqtt/${regiao}`, {
    clientId: `painel-${regiao}-${Math.random().toString(16).slice(2, 8)}`,
    reconnectPeriod: 2000,
  });
  cliente.on("connect", () => {
    cliente.subscribe([
      `regiao/${regiao}/mapa`, `regiao/${regiao}/transito`, `regiao/${regiao}/alerta`,
      `regiao/${regiao}/carro/+/posicao`, `regiao/${regiao}/report/acidente`,
      `regiao/${regiao}/carro/+/trilha`, "viz/carros", "viz/eventos", "config/modo",
    ]);
    $(`#con-${regiao}`).classList.add("ok");
  });
  cliente.on("close", () => $(`#con-${regiao}`).classList.remove("ok"));
  cliente.on("message", (topico, payload) => {
    let dados;
    try { dados = JSON.parse(payload.toString()); } catch { return; }
    mensagem(regiao, topico, dados, payload.length);
  });
  return cliente;
}

for (const r of REGIOES) clientes[r] = conectar(r);

function disparar(regiao, tipo, alvo) {
  clientes[regiao].publish(`regiao/${regiao}/simulacao/${tipo}`, alvo, { qos: 1 });
}

// =============================================================================
// Mensagens
// =============================================================================
function mensagem(r, topico, d, tamanho) {
  if (topico === `regiao/${r}/mapa`) return desenharMapa(r, d);
  if (topico === "viz/carros") return moverCarros(r, d.carros);
  if (topico === `regiao/${r}/transito`) return colorirTrechos(r, d.trechos);
  if (topico === "config/modo") return mostrarModo({ ...estado.modo, ...d });

  if (topico.endsWith("/posicao")) {
    // 12 posições/s por região: anima só uma parte.
    if (++estado.contPosicoes[r] % 4 === 0) animar(`celular_${r}`, "subida", "posicao");
    return;
  }
  if (topico.endsWith("/report/acidente")) {
    animar(`celular_${r}`, "subida", "report");
    return;
  }
  if (topico.endsWith("/trilha")) {
    animar(`celular_${r}`, "subida", "estado");
    contarBytes(`celular_${r}`, tamanho);
    return;
  }
  if (topico === `regiao/${r}/alerta`) {
    animar(`celular_${r}`, "descida", "alerta");
    if (d.decidido_em === "nuvem") animar(`wan_${r}`, "descida", "alerta");
    const via = document.querySelector(`#mapa-${r} .via[data-trecho="${d.trecho}"]`);
    if (via) { via.classList.remove("piscar"); void via.getBBox(); via.classList.add("piscar"); }
    return;
  }
  if (topico === "viz/eventos") evento(r, d);
}

function evento(r, e) {
  const R = r.toUpperCase();
  switch (e.tipo) {
    // ---- edge (celulares) ----
    case "acidente_ocorreu":
      marcarOcorrencia(r, e.trecho, "acidente");
      registrar(`Região ${R}: acidente em <b>${e.trecho}</b> (na rua; ninguém sabe ainda)`, "rua", "aviso");
      break;
    case "lentidao_iniciou":
      marcarOcorrencia(r, e.trecho, "lentidao");
      registrar(`Região ${R}: trânsito lento em <b>${e.trecho}</b> (nenhum carro vai reportar)`, "rua", "aviso");
      break;
    case "ocorrencia_encerrada":
      marcarOcorrencia(r, e.trecho, null);
      registrar(`Região ${R}: ocorrência em ${e.trecho} encerrada`, "rua", "ok");
      break;
    case "ocorrencias_limpas":
      for (const t of Object.keys(estado.ocorrencias[r])) marcarOcorrencia(r, t, null);
      break;
    case "acidente_reportado":
      registrar(`Carro ${e.carro} reportou o acidente em ${e.trecho}`, "edge");
      break;
    case "report_guardado":
      registrar(`Carro ${e.carro} viu o acidente em ${e.trecho}, mas está SEM SINAL: report guardado`, "edge", "aviso");
      break;
    case "alerta_recebido":
      avisarCarros(r);
      if (e.alerta === "acidente" && e.latencia_ms != null) {
        $(`#lat-${e.decidido_em}`).textContent = `${e.decidido_em} ${e.latencia_ms.toFixed(0)} ms`;
        for (const c of ["fog", "nuvem"]) $(`#lat-${c}`).classList.toggle("recente", c === e.decidido_em);
      }
      const nomes = { acidente: "acidente", congestionamento: "congestionamento", liberado: "trecho liberado" };
      const lat = e.latencia_ms != null ? ` em ${e.latencia_ms.toFixed(0)} ms após o report` : "";
      const sem = e.carros_sem_sinal ? ` (${e.carros_sem_sinal} sem sinal NÃO receberam)` : "";
      registrar(`Alerta de ${nomes[e.alerta]} em <b>${e.trecho}</b> chegou a ${e.carros_avisados} carros${lat}` +
        ` · decidido no <b>${e.decidido_em}</b>${sem}`, "edge", e.alerta === "liberado" ? "ok" : "decisao");
      break;
    case "desvio":
      estado.desvios[r]++;
      $(`#no-cel-${r} .n-desvios`).textContent = estado.desvios[r];
      if (Date.now() - (estado.ultimoDesvioLog[e.evitou] ?? 0) > 30_000) {
        estado.ultimoDesvioLog[e.evitou] = Date.now();
        registrar(`Carros começaram a desviar de ${e.evitou}`, "edge", "ok");
      }
      break;
    case "tunel_entrou":
      registrar(`Carro ${e.carro} entrou num túnel: sem sinal por ${e.duracao_s} s (segue navegando com o mapa em cache)`, "edge", "aviso");
      break;
    case "tunel_saiu":
      registrar(`Carro ${e.carro} saiu do túnel`, "edge");
      break;
    case "sincronizado":
      agruparSincronizacao(r, e);
      break;

    // ---- fog ----
    case "janela": {
      const no = $(`#no-fog-${r}`);
      $(".f-in", no).textContent = `${e.mensagens_recebidas} pos. · ${formatarBytes(e.bytes_recebidos)}`;
      $(".f-out", no).textContent = `estado · ${formatarBytes(e.bytes_enviados)}`;
      $(".f-cong", no).textContent = e.congestionados.length ? e.congestionados.join(", ") : "nenhum";
      contarBytes(`celular_${r}`, e.bytes_recebidos);
      break;
    }
    case "report_recebido":
      if (e.atrasado) registrar(`fog-${r} recebeu report ATRASADO de ${e.carro} (${(e.atraso_ms / 1000).toFixed(1)} s depois)`, "fog", "aviso");
      break;
    case "alerta_publicado":
      if (e.alerta === "congestionamento") {
        registrar(`fog-${r} DETECTOU congestionamento em <b>${e.trecho}</b> pela velocidade dos carros`, "fog", "decisao");
      }
      break;
    case "decisao_nuvem_falhou":
      if (e.tentativa === 1 || e.tentativa % 4 === 0) {
        registrar(`Nuvem não respondeu: região ${R} continua SEM alerta do acidente em ${e.trecho} (tentativa ${e.tentativa})`, "cloud", "erro");
      }
      break;
    case "lote_enviado":
      animar(`wan_${r}`, "subida", "lote");
      estado.ultimoLote = Date.now();
      estado.totalNuvem += e.itens;
      $("#nv-total").textContent = estado.totalNuvem;
      $(`#no-fog-${r} .f-fila`).textContent = e.fila;
      contarBytes(`wan_${r}`, e.bytes);
      if (e.itens > 10) registrar(`fog-${r} enviou à nuvem um lote de ${e.itens} itens (fila acumulada)`, "cloud", "ok");
      break;
    case "nuvem_inacessivel":
      $(`#no-fog-${r} .f-fila`).textContent = e.fila;
      registrar(`fog-${r}: nuvem inacessível, ${e.fila} itens aguardando na fila`, "fog", "aviso");
      break;
  }
}

// Quando a rede da região volta, todos os carros sincronizam juntos:
// junta os eventos do mesmo instante numa linha só.
const sincronizacoes = {};
function agruparSincronizacao(r, e) {
  const grupo = sincronizacoes[r] ??= { carros: [], pontos: 0, reports: 0 };
  grupo.carros.push(e.carro);
  grupo.pontos += e.pontos;
  grupo.reports += e.reports;
  clearTimeout(grupo.timer);
  grupo.timer = setTimeout(() => {
    const quem = grupo.carros.length === 1 ? `Carro ${grupo.carros[0]}` : `${grupo.carros.length} carros da região ${r.toUpperCase()}`;
    registrar(`${quem} sincronizaram o que guardaram sem sinal: ${grupo.pontos} pontos de rastro` +
      (grupo.reports ? `, ${grupo.reports} report(s) de acidente` : ""), "edge", "ok");
    delete sincronizacoes[r];
  }, 500);
}

// =============================================================================
// Mapa
// =============================================================================
const px = (c, f) => [ESCALA.x0 + c * ESCALA.dx, ESCALA.y0 + f * ESCALA.dy];

function el(nome, atributos = {}, pai = null) {
  const e = document.createElementNS(svgNS, nome);
  for (const [k, v] of Object.entries(atributos)) e.setAttribute(k, v);
  if (pai) pai.appendChild(e);
  return e;
}

function desenharMapa(r, mapa) {
  estado.mapas[r] = mapa;
  const svg = $(`#mapa-${r}`);
  svg.innerHTML = "";
  const gVias = el("g", {}, svg);
  const gNomes = el("g", {}, svg);
  el("g", { class: "ocorrencias" }, svg);
  el("g", { class: "carros" }, svg);

  for (const [trecho, [[c1, f1], [c2, f2]]] of Object.entries(mapa.trechos)) {
    const [x1, y1] = px(c1, f1);
    const [x2, y2] = px(c2, f2);
    el("line", { class: "via", "data-trecho": trecho, x1, y1, x2, y2 }, gVias);
    const alvo = el("line", { class: "alvo-clique", x1, y1, x2, y2 }, gVias);
    alvo.addEventListener("click", () => cliqueTrecho(r, trecho));
    const horizontal = f1 === f2;
    const nome = el("text", { class: "nome-trecho",
      x: (x1 + x2) / 2 + (horizontal ? 0 : 16), y: (y1 + y2) / 2 + (horizontal ? -10 : 4) }, gNomes);
    nome.textContent = trecho;
  }
  for (let c = 0; c < mapa.colunas; c++) {
    for (let f = 0; f < mapa.fileiras; f++) {
      const [x, y] = px(c, f);
      el("circle", { class: "cruzamento", cx: x, cy: y, r: 7 }, gVias);
    }
  }
  // Ocorrências que já existiam antes do redesenho
  for (const [t, tipo] of Object.entries(estado.ocorrencias[r])) marcarOcorrencia(r, t, tipo);
}

function cliqueTrecho(r, trecho) {
  if (estado.ferramenta === "tunel") return;   // túnel: clique num carro
  disparar(r, estado.ferramenta, trecho);
}

function colorirTrechos(r, trechos) {
  for (const via of document.querySelectorAll(`#mapa-${r} .via`)) {
    const info = trechos[via.dataset.trecho];
    via.classList.remove("livre", "lento", "congestionado");
    if (info) via.classList.add(info.estado);
  }
}

function marcarOcorrencia(r, trecho, tipo) {
  const mapa = estado.mapas[r];
  if (tipo) estado.ocorrencias[r][trecho] = tipo; else delete estado.ocorrencias[r][trecho];
  if (!mapa) return;
  const g = $(`#mapa-${r} .ocorrencias`);
  g.querySelector(`[data-trecho="${trecho}"]`)?.remove();
  if (!tipo) return;
  const [[c1, f1], [c2, f2]] = mapa.trechos[trecho];
  const [x1, y1] = px(c1, f1);
  const [x2, y2] = px(c2, f2);
  const t = el("text", { class: "ocorrencia", "data-trecho": trecho, x: (x1 + x2) / 2, y: (y1 + y2) / 2 }, g);
  t.textContent = tipo === "acidente" ? "🚧" : "🐢";
}

function moverCarros(r, carros) {
  const g = $(`#mapa-${r} .carros`);
  if (!g) return;
  let semSinal = carros.filter((c) => !c.sinal).length;
  // Rótulo "sem sinal" só quando são poucos (ex.: túnel); com a região
  // inteira fora, os rótulos se sobrepõem e bastam os pontos cinza.
  const rotular = semSinal <= 3;
  for (const c of carros) {
    let carro = g.querySelector(`[data-carro="${c.id}"]`);
    if (!carro) {
      carro = el("g", { class: "carro", "data-carro": c.id }, g);
      el("circle", { class: "onda", r: 6 }, carro);
      el("circle", { r: 6 }, carro);
      el("title", {}, carro);
      const rotulo = el("text", { y: -9 }, carro);
      rotulo.textContent = "";
      carro.addEventListener("click", () => {
        if (estado.ferramenta === "tunel") disparar(r, "tunel", c.id);
      });
    }
    const [x, y] = px(c.x, c.y);
    carro.style.transform = `translate(${x}px, ${y}px)`;
    carro.classList.toggle("sem-sinal", !c.sinal);
    $("title", carro).textContent = `${c.id} · ${c.vel} km/h${c.sinal ? "" : ` · SEM SINAL (${c.buffer} itens guardados)`}`;
    $("text", carro).textContent = !c.sinal && rotular ? `${c.id} sem sinal · ${c.buffer} guardados` : "";
  }
  const no = $(`#no-cel-${r}`);
  $(".n-carros", no).textContent = carros.length;
  $(".n-sem-sinal", no).textContent = semSinal;
}

function avisarCarros(r) {
  for (const carro of document.querySelectorAll(`#mapa-${r} .carro:not(.sem-sinal)`)) {
    carro.classList.remove("avisado");
    void carro.getBBox();
    carro.classList.add("avisado");
  }
}

// =============================================================================
// Onde decidir e ferramentas
// =============================================================================
function mostrarModo(modo) {
  estado.modo = modo;
  for (const botao of document.querySelectorAll('.seletor[data-chave="acidente"] button')) {
    const escolhido = modo.acidente === botao.dataset.valor;
    botao.classList.toggle("escolhido", escolhido);
    botao.classList.toggle("longe", escolhido && botao.dataset.valor === "nuvem");
  }
  $("#aviso-modo").textContent = modo.acidente === "nuvem"
    ? "⚠ O alerta agora depende da WAN: cada aviso paga a ida e a volta até a nuvem." : "";
}

for (const botao of document.querySelectorAll('.seletor[data-chave="acidente"] button')) {
  botao.onclick = () => {
    const novo = { ...estado.modo, acidente: botao.dataset.valor };
    for (const c of Object.values(clientes)) c.publish("config/modo", JSON.stringify(novo), { qos: 1, retain: true });
    registrar(`Alerta de acidente agora é decidido no <b>${novo.acidente}</b>`, "modo", "decisao");
    mostrarModo(novo);
  };
}

function escolherFerramenta(nome) {
  estado.ferramenta = nome;
  for (const b of document.querySelectorAll("#ferramentas button")) {
    b.classList.toggle("escolhido", b.dataset.ferramenta === nome);
  }
}
for (const b of document.querySelectorAll("#ferramentas button")) {
  b.onclick = () => escolherFerramenta(b.dataset.ferramenta);
}
escolherFerramenta("acidente");
mostrarModo(estado.modo);

$("#limpar").onclick = () => {
  for (const r of REGIOES) disparar(r, "limpar", "todas");
  registrar("Ocorrências limpas nas duas regiões", "rua", "ok");
};

// No GitHub Codespaces cada porta tem um endereço próprio (...-3000.app.github.dev).
function enderecoDaPorta(porta) {
  if (/-\d+\.app\.github\.dev$/.test(location.hostname)) {
    return `${location.protocol}//${location.hostname.replace(/-\d+\.app\.github\.dev$/, `-${porta}.app.github.dev`)}`;
  }
  return `${location.protocol}//${location.hostname}:${porta}`;
}
const urlGrafana = enderecoDaPorta(3010);
$("#link-grafana").href = urlGrafana;
$("#nv-grafana").href = urlGrafana;

// =============================================================================
// Topologia: enlaces e pontos animados
// =============================================================================
// "subida" = em direção à nuvem; "descida" = em direção aos celulares.
const ENLACES_VISUAIS = {
  celular_a: { esq: "#no-cel-a", dir: "#no-fog-a", subida: "dir", nome: "4G/5G", toxi: "celular_a" },
  wan_a:     { esq: "#no-fog-a", dir: "#no-nuvem", subida: "dir", nome: "WAN",   toxi: "wan" },
  wan_b:     { esq: "#no-nuvem", dir: "#no-fog-b", subida: "esq", nome: "WAN",   toxi: "wan" },
  celular_b: { esq: "#no-fog-b", dir: "#no-cel-b", subida: "esq", nome: "4G/5G", toxi: "celular_b" },
};
const geometria = {};
const pontos = [];

function calcularGeometria() {
  const base = $("#topologia").getBoundingClientRect();
  for (const [nome, def] of Object.entries(ENLACES_VISUAIS)) {
    const a = $(def.esq).getBoundingClientRect();
    const b = $(def.dir).getBoundingClientRect();
    const y = Math.min(a.top + a.height / 2, b.top + b.height / 2) - base.top;
    geometria[nome] = { x1: a.right - base.left, x2: b.left - base.left, y };
  }
  desenharEnlaces();
}

const infoEnlace = (nome) => estado.enlaces[ENLACES_VISUAIS[nome].toxi];
const enlaceFora = (nome) => infoEnlace(nome) && !infoEnlace(nome).ativo;

function desenharEnlaces() {
  let linhas = "", rotulos = "";
  for (const [nome, def] of Object.entries(ENLACES_VISUAIS)) {
    const g = geometria[nome];
    if (!g) continue;
    const fora = enlaceFora(nome);
    linhas += `<line class="enlace${fora ? " fora" : ""}" x1="${g.x1}" y1="${g.y}" x2="${g.x2}" y2="${g.y}"/>`;
    const info = infoEnlace(nome);
    const mx = (g.x1 + g.x2) / 2;
    let titulo = def.nome;
    if (info) titulo += info.ativo ? ` · ${info.rtt_ms} ms` : " · FORA";
    rotulos += `<text class="rotulo-enlace ${fora ? "fora" : "nome"}" x="${mx}" y="${g.y - 16}">${titulo}</text>`;
    rotulos += `<text class="rotulo-enlace" x="${mx}" y="${g.y + 20}">${formatarBytes(bytesPorMinuto(nome))}/min</text>`;
  }
  $("#linhas-enlace").innerHTML = linhas;
  $("#rotulos").innerHTML = rotulos;
}

function animar(nome, sentido, tipo) {
  if (!geometria[nome] || enlaceFora(nome)) return;
  const rtt = infoEnlace(nome)?.rtt_ms ?? 0;
  const def = ENLACES_VISUAIS[nome];
  // Da esquerda para a direita?
  const paraDireita = (sentido === "subida") === (def.subida === "dir");
  pontos.push({ nome, paraDireita, tipo, inicio: performance.now(), duracao: 700 + rtt / 2 });
}

function quadro(agora) {
  let html = "";
  for (let i = pontos.length - 1; i >= 0; i--) {
    const p = pontos[i];
    const progresso = (agora - p.inicio) / p.duracao;
    if (progresso >= 1) { pontos.splice(i, 1); continue; }
    const g = geometria[p.nome];
    const f = p.paraDireita ? progresso : 1 - progresso;
    const raio = p.tipo === "posicao" ? 3 : 7;
    html += `<circle class="${p.tipo}" cx="${g.x1 + (g.x2 - g.x1) * f}" cy="${g.y}" r="${raio}"/>`;
  }
  $("#pontos").innerHTML = html;
  requestAnimationFrame(quadro);
}
requestAnimationFrame(quadro);
window.addEventListener("resize", calcularGeometria);
new ResizeObserver(calcularGeometria).observe($("#topologia"));

// =============================================================================
// Bytes por enlace
// =============================================================================
function contarBytes(enlace, bytes) { estado.bytes[enlace].push([Date.now(), bytes]); }

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
const OPCOES_RTT = { celular_a: [30, 300, 1000], celular_b: [30, 300, 1000], wan: [250, 1000, 3000] };
const OPCOES_BANDA = [0, 256, 64, 16];

async function chamarApi(caminho, corpo) {
  try {
    const resposta = await fetch(`/api/enlaces${caminho}`, corpo === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo),
    });
    if (!resposta.ok) throw new Error(resposta.status);
    estado.enlaces = Object.fromEntries((await resposta.json()).map((e) => [e.nome, e]));
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
    registrar(`Enlace ${nome} ${alteracao.ativo ? "religado" : "DERRUBADO"}`, "rede", alteracao.ativo ? "ok" : "erro");
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

chamarApi("");
setInterval(() => chamarApi(""), 5000);
setInterval(() => {
  if (estado.ultimoLote) $("#nv-ultimo").textContent = `há ${Math.round((Date.now() - estado.ultimoLote) / 1000)} s`;
  desenharEnlaces();
}, 1000);

registrar("Painel iniciado. Aguardando dados dos fogs…");
