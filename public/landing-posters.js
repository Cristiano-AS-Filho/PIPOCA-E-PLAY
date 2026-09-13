/* Pôsteres da landing gravados pelo administrador.
 *
 * A landing (index.html) é um pacote gerado no Claude Web Design: a marcação
 * vem pronta, com espaços de imagem identificados por id (pp-poster-1…9,
 * pp-top3-1…3, pp-custo-still). Este script lê /api/landing/posters e aplica
 * sobre esses espaços a imagem, o título e a linha de apoio que o painel
 * administrativo salvou. Espaço sem nada salvo fica exatamente como está.
 *
 * O runtime de design pode redesenhar trechos da página (o Top 3 de exemplo,
 * por exemplo, some quando chega uma busca real). Por isso a aplicação é
 * idempotente e volta a rodar quando o DOM muda.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/landing/posters";
  var MARK = "data-pp-poster";
  var slots = null;
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

  function apply() {
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

  function schedule() {
    if (scheduled) return;
    scheduled = window.setTimeout(function () {
      scheduled = 0;
      try {
        apply();
      } catch (error) {
        // A landing nunca deve quebrar por causa do conteúdo administrável.
        if (window.console) console.warn("[landing] pôsteres:", error);
      }
    }, 40);
  }

  function watch() {
    if (!window.MutationObserver || !document.body) return;
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        if (records[i].addedNodes && records[i].addedNodes.length) {
          schedule();
          return;
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  function start() {
    fetch(ENDPOINT, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        slots = (data && data.slots) || null;
        if (!slots || !Object.keys(slots).length) return;
        schedule();
        watch();
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
