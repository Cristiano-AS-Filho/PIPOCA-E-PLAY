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
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/landing/posters";
  var MARK = "data-pp-poster";
  var AVATAR_MARK = "data-pp-depo-avatar";
  var slots = null;
  var testimonials = null;
  var scheduled = 0;

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

  function applyText(node, value) {
    if (node && value && node.textContent !== value) node.textContent = value;
  }

  function applyPosters() {
    if (!slots) return;
    Object.keys(slots).forEach(function (id) {
      var saved = slots[id];
      if (!saved) return;
      var slot = document.getElementById(id);
      if (!slot) return;
      if (saved.image) applyImage(slot, saved.image);
      var card = cardOf(slot);
      var title = titleOf(card);
      applyText(title, saved.title);
      applyText(metaOf(card, title), saved.meta);
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

    if (saved.image && circle) applyAvatar(circle, saved.image);
    applyText(lines[0], saved.name);
    applyText(lines[1], saved.handle);
    applyText(card.querySelector("blockquote"), saved.quote);
    applyText(card.querySelector("figcaption"), saved.context);

    // Sem foto, o círculo mostra a inicial do nome: ela acompanha o nome salvo.
    if (!saved.image && saved.name && circle && !circle.querySelector("img")) {
      var initial = saved.name.trim().charAt(0).toUpperCase();
      if (circle.textContent !== initial) circle.textContent = initial;
    }

    // O selo fica na marcação da página em todos os cartões, então marcar um
    // depoimento como real é escondê-lo — e voltar atrás é mostrá-lo de novo.
    if (typeof saved.placeholder === "boolean" && header) {
      var badge = header.querySelector(":scope > span");
      if (badge) badge.hidden = !saved.placeholder;
    }
  }

  function applyTestimonials() {
    if (!testimonials) return;
    Object.keys(testimonials).forEach(function (id) {
      if (testimonials[id]) applyTestimonial(id, testimonials[id]);
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
    // Observa o <html>, não o <body>: o runtime de design monta a página em
    // passadas e uma troca do próprio <body> deixaria o observador órfão.
    var root = document.documentElement;
    if (!window.MutationObserver || !root) return;
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

  function start() {
    fetch(ENDPOINT, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        slots = (data && data.slots) || null;
        testimonials = (data && data.testimonials) || null;
        var nothingSaved =
          !(slots && Object.keys(slots).length) &&
          !(testimonials && Object.keys(testimonials).length);
        if (nothingSaved) return;
        schedule();
        watch();
        // Rede de segurança para o render que chega depois da primeira passada:
        // schedule() já se agrupa, e aplicar de novo o mesmo conteúdo não muda nada.
        [200, 700, 1600, 3200].forEach(function (delay) {
          window.setTimeout(schedule, delay);
        });
      })
      .catch(function () {
        /* Sem backend alcançável a página segue com o conteúdo que já tem. */
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
