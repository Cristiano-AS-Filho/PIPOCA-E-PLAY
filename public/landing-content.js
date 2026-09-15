/* Conteúdo da landing gravado pelo administrador.
 *
 * A landing (index.html) é um pacote gerado no Claude Web Design: a marcação
 * vem pronta, com espaços de imagem identificados por id (pp-poster-1…9,
 * pp-top3-1…3, pp-custo-still) e com os cartões da seção "O que dizem sobre o
 * tempo" marcados por data-pp-depo (pp-depo-1…3). Este script lê
 * /api/landing/posters — uma requisição só, que traz os dois blocos — e aplica
 * sobre a página o que o painel administrativo salvou: imagem, título e linha
 * de apoio dos pôsteres; foto, nome, @, depoimento, legenda e selo
 * "Placeholder" dos depoimentos. Espaço sem nada salvo fica exatamente como
 * está.
 *
 * O runtime de design pode redesenhar trechos da página (o Top 3 de exemplo,
 * por exemplo, some quando chega uma busca real) e um novo render do React
 * pode devolver o texto original de um cartão. Por isso a aplicação é
 * idempotente e volta a rodar quando o DOM muda.
 *
 * Por que a leitura acontece mais de uma vez
 * -----------------------------------------
 * A versão anterior lia a rota **uma única vez**, no carregamento, e engolia
 * em silêncio qualquer falha. Isso produzia exatamente o sintoma relatado — a
 * alteração do admin só aparecia depois de um F5 — em três situações reais:
 *
 *   1. a leitura falhava (partida a frio da função, rede instável) e nada
 *      era aplicado até um novo carregamento;
 *   2. o armazenamento respondia com erro temporário; a rota devolve 200 com
 *      ``unavailable: true`` e nenhum espaço, e a página tratava isso como
 *      "não há nada publicado" pelo resto da visita;
 *   3. a aba já estava aberta (ou voltava pelo botão "voltar", restaurada do
 *      bfcache): o conteúdo continuava o do momento da abertura.
 *
 * A correção é só de dados, não de página: em vez de recarregar, o script
 * repete a leitura com espera crescente quando ela falha e relê quando a aba
 * volta a ficar visível ou é restaurada do histórico. Nunca há recarga
 * automática, nunca há laço infinito e a releitura espontânea respeita um piso
 * de tempo (MIN_REFRESH_MS), de modo que voltar para a aba não vira enxurrada
 * de requisições. Aplicar de novo o mesmo conteúdo não muda nada no DOM.
 */
(function () {
  "use strict";

  // Este arquivo é carregado duas vezes de propósito: uma pelo <head> do
  // próprio index.html e outra pelo template que o pacote monta. A segunda
  // sozinha não bastava — o desempacotador do pacote revive os <script> um a
  // um e, quando o runtime de design já redesenhou o corpo da página, o
  // ``replaceWith`` do último script vira operação sem efeito e ele nunca
  // executa. Era assim que, de vez em quando, a landing abria com o conteúdo
  // do layout e só um F5 trazia o que o administrador havia publicado. A
  // cópia do <head> não depende desse laço; a primeira que rodar assume, e a
  // outra encontra esta marca e sai.
  if (window.__pipocaLandingContent) return;
  window.__pipocaLandingContent = true;

  var ENDPOINT = "/api/landing/posters";
  var MARK = "data-pp-poster";
  var AVATAR_MARK = "data-pp-depo-avatar";

  // Esperas entre as tentativas quando a leitura falha ou volta indisponível.
  // A lista é finita de propósito: esgotada, a página segue com o layout.
  var RETRY_DELAYS = [1500, 4000, 10000];
  // Piso entre duas releituras espontâneas (aba visível, volta do histórico).
  var MIN_REFRESH_MS = 15000;
  // Depois disso, uma leitura em andamento é dada como perdida. Uma requisição
  // congelada junto com a página (bfcache) pode nunca se resolver, e sem esta
  // saída ``loading`` ficaria preso em true — a landing não releria mais nada
  // pelo resto da visita, que é exatamente o sintoma que se quer eliminar.
  var STUCK_LOAD_MS = 30000;

  var slots = null;
  var testimonials = null;
  var scheduled = 0;
  var watching = false;
  var loading = false;
  var lastLoadAt = 0;

  // O que a página trazia antes de o conteúdo publicado entrar, por espaço.
  // É o que permite o "Restaurar" do painel valer na aba já aberta.
  var originals = {};
  var written = {};

  function frameOf(slot) {
    var frame = slot.parentElement;
    return frame && frame.nodeType === 1 ? frame : null;
  }

  function cardOf(slot) {
    var rail = slot.closest ? slot.closest('[data-fx="railcard"]') : null;
    if (rail) return rail;
    var frame = frameOf(slot);
    return frame ? frame.parentElement : null;
  }

  function titleOf(card) {
    return card ? card.querySelector("h4, h3") : null;
  }

  function metaOf(card, title) {
    if (!card) return null;
    var meta = card.querySelector('[data-card="meta"]');
    if (meta) return meta;
    // Top 3: a linha de apoio é o <span> logo depois do título.
    var next = title && title.nextElementSibling;
    return next && next.tagName === "SPAN" ? next : null;
  }

  function applyImage(slot, source) {
    var frame = frameOf(slot);
    if (!frame) return;
    var img = frame.querySelector("img[" + MARK + '="' + slot.id + '"]');
    if (!img) {
      img = document.createElement("img");
      img.setAttribute(MARK, slot.id);
      img.alt = "";
      img.decoding = "async";
      img.loading = "lazy";
      // Primeiro filho: os selos posicionados (nota, #1) continuam por cima.
      img.style.cssText =
        "position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;";
      frame.insertBefore(img, frame.firstChild);
    }
    if (img.getAttribute("src") !== source) img.setAttribute("src", source);
    // O placeholder do espaço sai de cena; a altura vem do aspect-ratio do quadro.
    if (slot.style.display !== "none") slot.style.display = "none";
  }

  function removeImage(slot) {
    var frame = frameOf(slot);
    var img = frame && frame.querySelector("img[" + MARK + '="' + slot.id + '"]');
    if (img && img.parentNode) img.parentNode.removeChild(img);
    if (slot.style.display === "none") slot.style.display = "";
  }

  function applyText(node, value) {
    if (node && value && node.textContent !== value) node.textContent = value;
  }

  /* Guarda o texto que a própria página trazia, no instante anterior à
   * primeira escrita naquele campo. Capturar antes disso seria arriscado: o
   * runtime de design ainda pode não ter montado o nó, e um original vazio
   * apagaria a legenda na hora de restaurar. */
  function rememberOriginal(id, field, node) {
    if (!node) return;
    var box = originals[id] || (originals[id] = {});
    if (typeof box[field] !== "string") box[field] = node.textContent;
  }

  /* Devolve o texto original ao nó, mas só quando ele ainda mostra o que este
   * script escreveu: se o React já redesenhou o trecho, o original voltou
   * sozinho e mexer de novo seria apagar conteúdo que não é nosso. */
  function restoreText(id, field, node, wrote) {
    var original = (originals[id] || {})[field];
    if (!node || !wrote || !original || node.textContent !== wrote) return;
    if (node.textContent !== original) node.textContent = original;
  }

  function applyPoster(id, saved) {
    var slot = document.getElementById(id);
    if (!slot) return;
    var card = cardOf(slot);
    var title = titleOf(card);
    var meta = metaOf(card, title);

    var wrote = written[id] || (written[id] = {});

    if (saved.image) {
      applyImage(slot, saved.image);
      wrote.image = true;
    } else if (wrote.image) {
      removeImage(slot);
      wrote.image = false;
    }
    if (saved.title) {
      rememberOriginal(id, "title", title);
      applyText(title, saved.title);
      wrote.title = saved.title;
    } else if (wrote.title) {
      restoreText(id, "title", title, wrote.title);
      wrote.title = "";
    }
    if (saved.meta) {
      rememberOriginal(id, "meta", meta);
      applyText(meta, saved.meta);
      wrote.meta = saved.meta;
    } else if (wrote.meta) {
      restoreText(id, "meta", meta, wrote.meta);
      wrote.meta = "";
    }
  }

  function applyPosters() {
    if (!slots) return;
    // Percorre tudo o que já foi tocado, não só o que está publicado agora:
    // é assim que o "Restaurar" do painel chega à aba que já estava aberta.
    var ids = Object.keys(slots);
    Object.keys(written).forEach(function (id) {
      if (id.indexOf("pp-depo-") !== 0 && ids.indexOf(id) < 0) ids.push(id);
    });
    ids.forEach(function (id) {
      applyPoster(id, slots[id] || {});
    });
  }

  /* Cartão montado pelo componente Testimonial do design system:
   *
   *   <figure data-pp-depo="pp-depo-1">
   *     <div>                       cabeçalho
   *       <div>foto ou inicial</div>
   *       <div><span>nome</span><span>@</span></div>
   *       <span>Placeholder</span>  selo, único <span> direto do cabeçalho
   *     </div>
   *     <blockquote>depoimento</blockquote>
   *     <figcaption>legenda</figcaption>
   *   </figure>
   */
  function applyAvatar(circle, source) {
    // Quando a página já traz uma foto, o componente rende o próprio <img>:
    // nesse caso só o endereço muda.
    var img = circle.querySelector("img");
    if (!img) {
      img = document.createElement("img");
      img.setAttribute(AVATAR_MARK, "");
      img.alt = "";
      img.decoding = "async";
      // O círculo já recorta (border-radius + overflow); falta a referência
      // para o posicionamento absoluto cobrir os 40 px inteiros.
      img.style.cssText =
        "position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;";
      circle.style.position = "relative";
      circle.insertBefore(img, circle.firstChild);
    }
    if (img.getAttribute("src") !== source) img.setAttribute("src", source);
  }

  function applyTestimonial(id, saved) {
    var card = document.querySelector('figure[data-pp-depo="' + id + '"]');
    if (!card) return;
    var header = card.firstElementChild;
    var circle = header && header.firstElementChild;
    var identity = circle && circle.nextElementSibling;
    var lines = identity ? identity.children : [];
    var quote = card.querySelector("blockquote");
    var context = card.querySelector("figcaption");

    var wrote = written[id] || (written[id] = {});

    if (saved.image && circle) {
      applyAvatar(circle, saved.image);
      wrote.image = true;
    } else if (wrote.image && circle) {
      var avatar = circle.querySelector("img[" + AVATAR_MARK + "]");
      if (avatar && avatar.parentNode) avatar.parentNode.removeChild(avatar);
      wrote.image = false;
    }

    [
      ["name", lines[0]],
      ["handle", lines[1]],
      ["quote", quote],
      ["context", context],
    ].forEach(function (pair) {
      var field = pair[0];
      var node = pair[1];
      if (saved[field]) {
        rememberOriginal(id, field, node);
        applyText(node, saved[field]);
        wrote[field] = saved[field];
      } else if (wrote[field]) {
        restoreText(id, field, node, wrote[field]);
        wrote[field] = "";
      }
    });

    // Sem foto, o círculo mostra a inicial do nome: ela acompanha o nome salvo.
    if (!saved.image && saved.name && circle && !circle.querySelector("img")) {
      var initial = saved.name.trim().charAt(0).toUpperCase();
      if (circle.textContent !== initial) circle.textContent = initial;
    }

    // O selo fica na marcação da página em todos os cartões, então marcar um
    // depoimento como real é escondê-lo — e voltar atrás é mostrá-lo de novo.
    if (header) {
      var badge = header.querySelector(":scope > span");
      if (badge) {
        if (typeof saved.placeholder === "boolean") {
          badge.hidden = !saved.placeholder;
          wrote.placeholder = true;
        } else if (wrote.placeholder) {
          badge.hidden = false;
          wrote.placeholder = false;
        }
      }
    }
  }

  function applyTestimonials() {
    if (!testimonials) return;
    var ids = Object.keys(testimonials);
    Object.keys(written).forEach(function (id) {
      if (id.indexOf("pp-depo-") === 0 && ids.indexOf(id) < 0) ids.push(id);
    });
    ids.forEach(function (id) {
      applyTestimonial(id, testimonials[id] || {});
    });
  }

  function apply() {
    applyPosters();
    applyTestimonials();
  }

  function schedule() {
    if (scheduled) return;
    scheduled = window.setTimeout(function () {
      scheduled = 0;
      try {
        apply();
      } catch (error) {
        // A landing nunca deve quebrar por causa do conteúdo administrável.
        if (window.console) console.warn("[landing] conteúdo administrável:", error);
      }
    }, 40);
  }

  function watch() {
    if (watching) return;
    // Observa o próprio documento, não o <html> nem o <body>: o pacote monta a
    // página trocando o elemento <html> inteiro (``documentElement.replaceWith``)
    // e o runtime de design ainda redesenha trechos depois disso. Observando o
    // Document, a troca da raiz é só mais uma mutação de filhos — o observador
    // sobrevive a ela e continua enxergando a árvore nova.
    var root = document;
    if (!window.MutationObserver || !root) return;
    watching = true;
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var record = records[i];
        // Nós novos: o runtime de design redesenhou um trecho da página.
        // Texto trocado: um render do React devolveu o conteúdo original.
        if (record.type === "characterData" || (record.addedNodes && record.addedNodes.length)) {
          schedule();
          return;
        }
      }
    }).observe(root, { childList: true, subtree: true, characterData: true });
  }

  /* Uma leitura da rota pública.
   *
   * ``attempt`` é o índice em RETRY_DELAYS: cada falha agenda a próxima espera
   * e a lista acabando encerra a tentativa — sem laço, sem recarga de página.
   */
  function load(attempt) {
    if (loading && Date.now() - lastLoadAt < STUCK_LOAD_MS) return;
    loading = true;
    lastLoadAt = Date.now();
    fetch(ENDPOINT, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        loading = false;
        // ``unavailable`` é a rota dizendo que o armazenamento não respondeu:
        // conteúdo vazio aqui não significa "nada publicado".
        if (!data || data.unavailable) {
          retry(attempt);
          return;
        }
        slots = data.slots || {};
        testimonials = data.testimonials || {};
        schedule();
        watch();
        // Rede de segurança para o render que chega depois da primeira passada:
        // schedule() já se agrupa, e aplicar de novo o mesmo conteúdo não muda nada.
        [200, 700, 1600, 3200].forEach(function (delay) {
          window.setTimeout(schedule, delay);
        });
      })
      .catch(function () {
        loading = false;
        retry(attempt);
      });
  }

  function retry(attempt) {
    var delay = RETRY_DELAYS[attempt || 0];
    if (typeof delay !== "number") return; // tentativas esgotadas: fica o layout
    window.setTimeout(function () {
      load((attempt || 0) + 1);
    }, delay);
  }

  /* Releitura espontânea: aba que volta a ficar visível, página restaurada do
   * histórico (bfcache) ou janela que recebe o foco. É o que faz a alteração
   * do administrador alcançar quem já estava com a landing aberta, sem
   * recarregar nada. O piso de tempo evita repetir a leitura a cada alternância
   * de aba. */
  function refresh() {
    if (Date.now() - lastLoadAt < MIN_REFRESH_MS) return;
    load(0);
  }

  function start() {
    // Armado antes da primeira resposta: carregado pelo <head>, este script roda
    // antes de o pacote montar a página, e é o observador que avisa quando os
    // espaços finalmente existem.
    watch();
    load(0);
    window.addEventListener("pageshow", function (event) {
      // Restaurada do bfcache: o JavaScript não roda de novo, então a leitura
      // precisa ser pedida aqui explicitamente.
      if (event && event.persisted) load(0);
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") refresh();
    });
    window.addEventListener("focus", refresh);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
