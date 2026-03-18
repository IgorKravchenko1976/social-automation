(function () {
  "use strict";

  var API_BASE = document.currentScript
    ? document.currentScript.src.replace(/\/static\/chat-widget\.js.*$/, "")
    : "";
  var TG_BOT_URL = "https://t.me/iminapp_bot";

  var style = document.createElement("style");
  style.textContent = [
    "#imin-chat-fab{position:fixed;bottom:24px;right:24px;z-index:99999;display:flex;gap:10px;align-items:flex-end}",
    "#imin-chat-fab button{width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;transition:transform .2s,box-shadow .2s}",
    "#imin-chat-fab button:hover{transform:scale(1.08);box-shadow:0 6px 24px rgba(0,0,0,.4)}",
    "#imin-btn-tg{background:#2AABEE}",
    "#imin-btn-chat{background:linear-gradient(135deg,#6ee7b7,#3b82f6)}",
    "#imin-chat-window{position:fixed;bottom:92px;right:24px;width:380px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 120px);background:#1a1a2e;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.5);z-index:99999;display:none;flex-direction:column;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}",
    "#imin-chat-window.open{display:flex}",
    "#imin-chat-header{background:linear-gradient(135deg,#6ee7b7,#3b82f6);padding:16px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}",
    "#imin-chat-header .title{color:#fff;font-size:16px;font-weight:700;display:flex;align-items:center;gap:8px}",
    "#imin-chat-header .title .dot{width:8px;height:8px;border-radius:50%;background:#4ade80;flex-shrink:0}",
    "#imin-chat-header .close{background:none;border:none;color:rgba(255,255,255,.8);font-size:22px;cursor:pointer;width:32px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:50%;transition:background .2s}",
    "#imin-chat-header .close:hover{background:rgba(255,255,255,.15)}",
    "#imin-chat-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}",
    "#imin-chat-messages::-webkit-scrollbar{width:4px}",
    "#imin-chat-messages::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:4px}",
    ".imin-msg{max-width:82%;padding:10px 14px;border-radius:16px;font-size:14px;line-height:1.5;word-wrap:break-word;animation:iminFadeIn .25s ease}",
    ".imin-msg.bot{background:#262640;color:#e2e8f0;align-self:flex-start;border-bottom-left-radius:4px}",
    ".imin-msg.user{background:linear-gradient(135deg,#6ee7b7,#3b82f6);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}",
    ".imin-msg.typing{background:#262640;color:#94a3b8;align-self:flex-start;border-bottom-left-radius:4px;font-style:italic}",
    ".imin-tg-link{display:flex;align-items:center;gap:6px;color:#2AABEE;text-decoration:none;font-size:13px;padding:6px 12px;background:rgba(42,171,238,.1);border-radius:10px;margin-top:4px;align-self:flex-start;transition:background .2s}",
    ".imin-tg-link:hover{background:rgba(42,171,238,.2)}",
    "#imin-chat-input{display:flex;padding:12px;gap:8px;border-top:1px solid rgba(255,255,255,.06);flex-shrink:0;background:#1a1a2e}",
    "#imin-chat-input input{flex:1;background:#262640;border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:10px 16px;color:#e2e8f0;font-size:14px;outline:none;transition:border-color .2s}",
    "#imin-chat-input input:focus{border-color:rgba(110,231,183,.4)}",
    "#imin-chat-input input::placeholder{color:#64748b}",
    "#imin-chat-input button{width:40px;height:40px;border-radius:50%;border:none;background:linear-gradient(135deg,#6ee7b7,#3b82f6);color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:opacity .2s}",
    "#imin-chat-input button:disabled{opacity:.4;cursor:default}",
    "@keyframes iminFadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}",
    "@media(max-width:480px){#imin-chat-window{bottom:0;right:0;width:100%;max-width:100%;height:100%;max-height:100%;border-radius:0}#imin-chat-fab{bottom:16px;right:16px}}"
  ].join("\n");
  document.head.appendChild(style);

  var fab = document.createElement("div");
  fab.id = "imin-chat-fab";
  fab.innerHTML =
    '<a href="' + TG_BOT_URL + '" target="_blank" rel="noopener" id="imin-btn-tg" style="width:56px;height:56px;border-radius:50%;display:flex;align-items:center;justify-content:center;text-decoration:none;box-shadow:0 4px 16px rgba(0,0,0,.3);transition:transform .2s">' +
      '<svg width="28" height="28" viewBox="0 0 24 24" fill="#fff"><path d="M11.944 0A12 12 0 1 0 24 12.056A12.014 12.014 0 0 0 11.944 0ZM16.906 7.224c.1-.002.321.039.465.178a.66.66 0 0 1 .193.44c.01.066.023.21.013.326c-.123 1.28-.654 4.382-.924 5.813c-.114.606-.338.809-.555.829c-.473.044-.832-.312-1.29-.612c-.714-.469-1.118-.761-1.812-1.22c-.8-.529-.282-.82.174-1.294c.12-.124 2.19-2.008 2.231-2.179a.166.166 0 0 0-.039-.152c-.053-.044-.13-.029-.186-.017c-.08.017-1.346.855-3.8 2.51c-.36.247-.685.367-.978.361c-.322-.007-.94-.182-1.4-.331c-.566-.184-.929-.28-.893-.593c.018-.163.218-.33.6-.498c2.35-1.023 3.917-1.698 4.702-2.023c2.24-.93 2.704-1.092 3.008-1.097Z"/></svg>' +
    "</a>" +
    '<button id="imin-btn-chat" title="Чат з I\'M IN">' +
      '<svg width="26" height="26" viewBox="0 0 24 24" fill="#fff"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.2L4 17.2V4h16v12z"/><path d="M7 9h2v2H7zm4 0h2v2h-2zm4 0h2v2h-2z"/></svg>' +
    "</button>";
  document.body.appendChild(fab);

  var win = document.createElement("div");
  win.id = "imin-chat-window";
  win.innerHTML =
    '<div id="imin-chat-header">' +
      '<div class="title"><span class="dot"></span>I\'M IN Помічник</div>' +
      '<button class="close">&times;</button>' +
    "</div>" +
    '<div id="imin-chat-messages"></div>' +
    '<div id="imin-chat-input">' +
      '<input type="text" placeholder="Напишіть повідомлення..." />' +
      '<button title="Надіслати"><svg width="18" height="18" viewBox="0 0 24 24" fill="#fff"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button>' +
    "</div>";
  document.body.appendChild(win);

  var chatBtn = document.getElementById("imin-btn-chat");
  var closeBtn = win.querySelector(".close");
  var messagesDiv = document.getElementById("imin-chat-messages");
  var inputField = win.querySelector("#imin-chat-input input");
  var sendBtn = win.querySelector("#imin-chat-input button");
  var isOpen = false;

  function toggle() {
    isOpen = !isOpen;
    win.classList.toggle("open", isOpen);
    if (isOpen && messagesDiv.children.length === 0) showWelcome();
    if (isOpen) inputField.focus();
  }

  chatBtn.addEventListener("click", toggle);
  closeBtn.addEventListener("click", toggle);

  function addMessage(text, cls) {
    var el = document.createElement("div");
    el.className = "imin-msg " + cls;
    el.textContent = text;
    messagesDiv.appendChild(el);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
    return el;
  }

  function showWelcome() {
    addMessage(
      "Привіт! 👋 Я AI-помічник додатку I'M IN. Запитайте мене будь-що про додаток!",
      "bot"
    );
    var link = document.createElement("a");
    link.className = "imin-tg-link";
    link.href = TG_BOT_URL;
    link.target = "_blank";
    link.rel = "noopener";
    link.innerHTML =
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="#2AABEE"><path d="M11.944 0A12 12 0 1 0 24 12.056A12.014 12.014 0 0 0 11.944 0ZM16.906 7.224c.1-.002.321.039.465.178a.66.66 0 0 1 .193.44c.01.066.023.21.013.326c-.123 1.28-.654 4.382-.924 5.813c-.114.606-.338.809-.555.829c-.473.044-.832-.312-1.29-.612c-.714-.469-1.118-.761-1.812-1.22c-.8-.529-.282-.82.174-1.294c.12-.124 2.19-2.008 2.231-2.179a.166.166 0 0 0-.039-.152c-.053-.044-.13-.029-.186-.017c-.08.017-1.346.855-3.8 2.51c-.36.247-.685.367-.978.361c-.322-.007-.94-.182-1.4-.331c-.566-.184-.929-.28-.893-.593c.018-.163.218-.33.6-.498c2.35-1.023 3.917-1.698 4.702-2.023c2.24-.93 2.704-1.092 3.008-1.097Z"/></svg>' +
      "Написати в Telegram";
    messagesDiv.appendChild(link);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
  }

  async function sendMessage() {
    var text = inputField.value.trim();
    if (!text) return;

    inputField.value = "";
    sendBtn.disabled = true;
    addMessage(text, "user");

    var typing = addMessage("Друкую...", "typing");

    try {
      var resp = await fetch(API_BASE + "/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, sender_name: "website-visitor" }),
      });
      var data = await resp.json();
      typing.remove();
      addMessage(data.reply || "Помилка. Спробуйте ще раз.", "bot");
    } catch (e) {
      typing.remove();
      addMessage("Не вдалося з'єднатися. Спробуйте пізніше або напишіть у Telegram.", "bot");
    }

    sendBtn.disabled = false;
    inputField.focus();
  }

  sendBtn.addEventListener("click", sendMessage);
  inputField.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendMessage();
  });
})();
