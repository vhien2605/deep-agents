// Webview side of the chat. Talks to extension.ts via postMessage:
//   out: {type:"send", text} | {type:"stop"}
//   in:  {type:"text", text: delta} | {type:"tool", text} | {type:"error", text} | {type:"done"} | {type:"clear"}
(function () {
  const vscode = acquireVsCodeApi();
  const log = document.getElementById("log");
  const input = document.getElementById("input");
  const send = document.getElementById("send");

  // Own mark: code brackets around a check — "tests pass". Same paths as media/icon.svg.
  const MARK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3.5H5.5a1 1 0 0 0-1 1v15a1 1 0 0 0 1 1H8"/><path d="M16 3.5h2.5a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H16"/><path class="tick" d="M8.5 12.5l2.5 2.5 4.5-5.5"/></svg>`;
  const ICON_SEND = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 13V3M3.5 7.5 8 3l4.5 4.5"/></svg>`;
  const ICON_STOP = `<svg viewBox="0 0 16 16" fill="currentColor"><rect x="4" y="4" width="8" height="8" rx="1.5"/></svg>`;
  const ICON_CHEV = `<svg class="chev" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 2.5 8 6l-3.5 3.5"/></svg>`;
  const ICON_CHECK = `<svg class="check" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.5 5 9l4.5-6"/></svg>`;

  const SUGGESTIONS = [
    "Viết unit test cho project này, coverage 80%",
    "Liệt kê các project trong workspace",
    "Test project demo_calculator, chỉ file calculator.cpp",
  ];

  // Raw HTML from the model is shown as text, never injected.
  marked.use({
    gfm: true,
    breaks: false,
    renderer: { html: ({ text }) => escapeHtml(text) },
  });

  let busy = false;
  let turn = null; // { root, body, blocks: [{kind, el, ...}] }
  let scheduled = false;
  let skills = []; // [{name, description}] from the extension
  let menuItems = [];
  let menuIndex = 0;

  // Built-in "/" commands. `args` = takes text after the name, so picking it
  // from the menu fills the input instead of running right away.
  const COMMANDS = [
    { name: "test", args: "[coverage %]", description: "Viết unit test cho project đang mở",
      run: (arg) => sendMessage(`/test ${arg}`.trim(), `Viết unit test cho project này, coverage ${parseFloat(arg) || 80}%`) },
    { name: "projects", description: "Liệt kê các project trong workspace",
      run: () => sendMessage("/projects", "Liệt kê các project trong workspace") },
    { name: "new", description: "Bắt đầu cuộc trò chuyện mới", run: () => vscode.postMessage({ type: "newChat" }) },
    { name: "stop", description: "Dừng agent đang chạy", run: () => vscode.postMessage({ type: "stop" }) },
    { name: "help", description: "Xem danh sách lệnh và skill", run: () => showHelp() },
  ];

  const menu = document.createElement("div");
  menu.id = "menu";
  menu.hidden = true;
  document.getElementById("composer").prepend(menu);

  function allCommands() {
    return [
      ...COMMANDS.map((c) => ({ ...c, group: "Lệnh" })),
      ...skills.map((s) => ({
        name: s.name, args: "[yêu cầu]", description: s.description, group: "Skill",
        run: (arg) => sendMessage(`/${s.name} ${arg}`.trim()),
      })),
    ];
  }

  function updateMenu() {
    const m = /^\/(\S*)$/.exec(input.value);
    if (!m) return hideMenu();
    const q = m[1].toLowerCase();
    const all = allCommands();
    menuItems = [
      ...all.filter((c) => c.name.startsWith(q)),
      ...all.filter((c) => !c.name.startsWith(q) && c.name.includes(q)),
    ];
    if (!menuItems.length) return hideMenu();
    menuIndex = Math.min(menuIndex, menuItems.length - 1);
    let group = null;
    menu.innerHTML = menuItems.map((c, i) => {
      const head = c.group !== group ? `<div class="group">${(group = c.group)}</div>` : "";
      return `${head}<div class="item${i === menuIndex ? " active" : ""}" data-i="${i}">
        <span class="name">/${escapeHtml(c.name)}${c.args ? ` <span class="args">${escapeHtml(c.args)}</span>` : ""}</span>
        <span class="desc">${escapeHtml(c.description)}</span></div>`;
    }).join("");
    menu.hidden = false;
    menu.querySelector(".item.active")?.scrollIntoView({ block: "nearest" });
  }

  function hideMenu() {
    menu.hidden = true;
    menuIndex = 0;
  }

  function pick(i) {
    const c = menuItems[i];
    hideMenu();
    if (c.args) {
      input.value = `/${c.name} `;
      autosize();
      input.focus();
    } else {
      input.value = "";
      autosize();
      c.run("");
    }
  }

  menu.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".item");
    if (!item) return;
    e.preventDefault();
    pick(Number(item.dataset.i));
  });

  function showHelp() {
    if (log.querySelector(".welcome")) log.innerHTML = "";
    const rows = allCommands().map((c) => `| \`/${c.name}${c.args ? " " + c.args : ""}\` | ${c.group} | ${c.description} |`);
    const el = document.createElement("div");
    el.className = "turn prose help";
    el.innerHTML = marked.parse(["| Lệnh | Loại | Mô tả |", "|---|---|---|", ...rows].join("\n"));
    log.appendChild(el);
    scroll(true);
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  function nearBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  }
  function scroll(force) {
    if (force || nearBottom()) log.scrollTop = log.scrollHeight;
  }

  function welcome() {
    log.innerHTML = `
      <div class="welcome">
        <div class="mark">${MARK}</div>
        <h1>Hôm nay viết test cho project nào?</h1>
        <div class="suggestions">${SUGGESTIONS.map((s) => `<button>${escapeHtml(s)}</button>`).join("")}</div>
      </div>`;
    log.querySelectorAll(".suggestions button").forEach((b) =>
      b.addEventListener("click", () => submit(b.textContent)));
  }

  function setBusy(value) {
    busy = value;
    send.className = value ? "stop" : input.value.trim() ? "ready" : "";
    send.innerHTML = value ? ICON_STOP : ICON_SEND;
    send.title = value ? "Dừng" : "Gửi";
    if (turn) turn.root.classList.toggle("busy", value);
  }

  function lastBlock() {
    return turn && turn.blocks[turn.blocks.length - 1];
  }

  function startTurn(text) {
    if (log.querySelector(".welcome")) log.innerHTML = "";
    const u = document.createElement("div");
    u.className = "turn user";
    u.innerHTML = `<div class="pill"></div>`;
    const cmd = /^\/(\S+)/.exec(text);
    if (cmd) {
      const chip = document.createElement("span");
      chip.className = "cmd";
      chip.textContent = cmd[0];
      u.firstChild.append(chip, text.slice(cmd[0].length));
    } else {
      u.firstChild.textContent = text;
    }
    log.appendChild(u);

    const a = document.createElement("div");
    a.className = "turn assistant busy";
    a.innerHTML = `<div class="avatar">${MARK}</div><div class="body"></div>`;
    log.appendChild(a);
    turn = { root: a, body: a.querySelector(".body"), blocks: [] };
    scroll(true);
  }

  function finishTools(block) {
    if (block.done) return;
    block.done = true;
    block.el.querySelector(".spinner").outerHTML = ICON_CHECK;
    const n = block.steps.length;
    block.el.querySelector(".label").textContent = `Đã chạy ${n} bước`;
  }

  function addTool(label) {
    let block = lastBlock();
    if (block && block.kind === "text") renderText(block, false);
    if (!block || block.kind !== "tools") {
      const el = document.createElement("details");
      el.className = "tools";
      el.innerHTML = `<summary><span class="spinner"></span><span class="label"></span>${ICON_CHEV}</summary><ol></ol>`;
      turn.body.appendChild(el);
      block = { kind: "tools", el, steps: [], done: false };
      turn.blocks.push(block);
    }
    block.steps.push(label);
    const li = document.createElement("li");
    li.textContent = label;
    li.title = label;
    block.el.querySelector("ol").appendChild(li);
    block.el.querySelector(".label").textContent = label;
    scroll();
  }

  function addText(delta) {
    let block = lastBlock();
    if (!block || block.kind !== "text") {
      if (block && block.kind === "tools") finishTools(block);
      const el = document.createElement("div");
      el.className = "prose";
      turn.body.appendChild(el);
      block = { kind: "text", el, src: "" };
      turn.blocks.push(block);
    }
    block.src += delta;
    if (!scheduled) {
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        // The turn may have ended, or a tool block started, since this frame was
        // queued; both already rendered the text final, so don't put a caret back.
        const current = lastBlock();
        if (current && current.kind === "text") renderText(current, true);
        scroll();
      });
    }
  }

  function renderText(block, streaming) {
    if (!block) return;
    block.el.innerHTML = marked.parse(block.src);
    if (streaming) {
      const caret = document.createElement("span");
      caret.className = "caret";
      let target = block.el.lastElementChild || block.el;
      while (target.lastElementChild && !["PRE", "TABLE"].includes(target.tagName) &&
             ["P", "LI", "UL", "OL", "BLOCKQUOTE", "H1", "H2", "H3", "H4"].includes(target.lastElementChild.tagName)) {
        target = target.lastElementChild;
      }
      target.appendChild(caret);
    } else {
      block.el.querySelectorAll("pre").forEach((pre) => {
        const btn = document.createElement("button");
        btn.className = "copy";
        btn.textContent = "Copy";
        btn.addEventListener("click", () => {
          navigator.clipboard.writeText(pre.querySelector("code")?.innerText ?? pre.innerText);
          btn.textContent = "Copied";
          setTimeout(() => (btn.textContent = "Copy"), 1200);
        });
        pre.appendChild(btn);
      });
    }
  }

  function endTurn() {
    if (!turn) return setBusy(false);
    turn.blocks.forEach((b) => (b.kind === "tools" ? finishTools(b) : renderText(b, false)));
    setBusy(false);
    turn = null;
  }

  function submit(text) {
    text = (text ?? input.value).trim();
    const m = /^\/(\S+)\s*([\s\S]*)$/.exec(text);
    const command = m && allCommands().find((c) => c.name === m[1]);
    // While the agent runs only instant commands (/stop, /new, /help) go through.
    if (!text || (busy && (!command || command.args))) return;
    input.value = "";
    autosize();
    hideMenu();
    if (command) command.run(m[2]);
    else sendMessage(text);
  }

  // `display` is what the user sees; `payload` what the agent receives.
  function sendMessage(display, payload = display) {
    if (busy) return;
    startTurn(display);
    setBusy(true);
    vscode.postMessage({ type: "send", text: payload });
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
    if (!busy) send.className = input.value.trim() ? "ready" : "";
  }

  send.addEventListener("click", () => (busy ? vscode.postMessage({ type: "stop" }) : submit()));
  input.addEventListener("input", () => {
    autosize();
    updateMenu();
  });
  input.addEventListener("keydown", (e) => {
    if (!menu.hidden) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        menuIndex = (menuIndex + (e.key === "ArrowDown" ? 1 : menuItems.length - 1)) % menuItems.length;
        updateMenu();
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        pick(menuIndex);
        return;
      }
      if (e.key === "Escape") {
        hideMenu();
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });

  window.addEventListener("message", ({ data }) => {
    switch (data.type) {
      case "skills":
        skills = data.items;
        break;
      case "clear":
        turn = null;
        setBusy(false);
        welcome();
        break;
      case "text":
        if (turn) addText(data.text);
        break;
      case "tool":
        if (turn) addTool(data.text);
        break;
      case "error": {
        const el = document.createElement("div");
        el.className = "error";
        el.textContent = data.text;
        (turn ? turn.body : log).appendChild(el);
        scroll(true);
        break;
      }
      case "done":
        endTurn();
        break;
    }
  });

  welcome();
  setBusy(false);
  input.focus();
  vscode.postMessage({ type: "ready" });
})();
