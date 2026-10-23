export function createUI({ I, esc, act }) {
  const layer = document.getElementById("floating-ui");
  let pop = null,
    sub = null,
    corridor = null,
    closeTimer = null,
    buffer = "",
    typeTimer;
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  function place(panel, anchor, side = false) {
    const a = anchor.getBoundingClientRect(),
      r = panel.getBoundingClientRect(),
      viewport = document.documentElement.clientWidth;
    panel.style.left =
      clamp(
        side
          ? a.right + r.width + 8 < viewport
            ? a.right + 8
            : a.left - r.width - 8
          : a.right - r.width,
        12,
        Math.max(12, viewport - r.width - 12),
      ) + "px";
    panel.style.top =
      clamp(
        side
          ? a.top
          : a.bottom + r.height + 8 < innerHeight
            ? a.bottom + 8
            : a.top - r.height - 8,
        12,
        Math.max(12, innerHeight - r.height - 12),
      ) + "px";
  }
  function closeSub() {
    clearTimeout(closeTimer);
    sub?.panel.remove();
    sub?.anchor.setAttribute("aria-expanded", "false");
    sub = null;
    corridor = null;
  }
  function close(focus = false) {
    closeSub();
    if (!pop) return;
    const p = pop;
    pop = null;
    p.panel.remove();
    layer.querySelector(".mobile-backdrop")?.remove();
    p.anchor.setAttribute("aria-expanded", "false");
    p.anchor.removeAttribute("aria-activedescendant");
    if (focus && p.anchor.isConnected) p.anchor.focus();
  }
  function show(
    anchor,
    html,
    label = "Действия",
    kind = "menu",
    extra = "",
    focus = true,
  ) {
    if (pop?.panel.contains(anchor) || sub?.panel.contains(anchor))
      anchor = pop.anchor;
    close();
    const panel = document.createElement("div");
    panel.className = "popover " + extra;
    panel.style.maxWidth = document.documentElement.clientWidth - 24 + "px";
    panel.setAttribute("role", kind);
    panel.setAttribute("aria-label", label);
    panel.innerHTML = html;
    layer.append(panel);
    anchor.setAttribute("aria-expanded", "true");
    pop = { anchor, panel, kind };
    place(panel, anchor);
    if (focus)
      requestAnimationFrame(() =>
        panel.querySelector("button:not(:disabled),input")?.focus(),
      );
    return panel;
  }
  const item = (label, action, icon = "", extra = "") =>
    `<button type="button" class="menu-item" role="menuitem" data-action="${esc(action)}" ${extra}>${icon ? I(icon) : ""}<span>${label}</span></button>`;
  function options(button) {
    const opts = JSON.parse(button.dataset.options),
      value = document.getElementById(button.dataset.field).value;
    const panel = show(
      button,
      opts
        .map(
          ([v, l], i) =>
            `<button type="button" class="select-option" role="option" aria-selected="${v === value}" data-ui="choose" data-value="${esc(v)}" data-index="${i}"><span>${esc(l)}</span>${v === value ? I("check") : ""}</button>`,
        )
        .join(""),
      button.getAttribute("aria-label"),
      "listbox",
      "select-popover",
      false,
    );
    panel.style.width =
      Math.min(
        innerWidth - 24,
        Math.max(240, button.getBoundingClientRect().width),
      ) + "px";
    panel.id = "listbox-" + button.dataset.field;
    button.setAttribute("aria-controls", panel.id);
    pop.options = opts;
    place(panel, button);
    const selected =
      panel.querySelector("[aria-selected=true]") || panel.firstElementChild;
    selected?.focus();
  }
  function calendar(anchor, month) {
    const field = document.getElementById(anchor.dataset.field),
      value = field.value || new Date().toISOString().slice(0, 10),
      cur = month || value.slice(0, 7),
      [y, m] = cur.split("-").map(Number),
      offset = (new Date(Date.UTC(y, m - 1, 1)).getUTCDay() + 6) % 7,
      days = new Date(Date.UTC(y, m, 0)).getUTCDate(),
      min = anchor.dataset.min || "2020-01-01",
      max = anchor.dataset.max || "2036-12-31";
    const delta = (n) =>
      new Date(Date.UTC(y, m - 1 + n, 1)).toISOString().slice(0, 7);
    show(
      anchor,
      `<div class="calendar-head"><button type="button" class="iconbtn" data-ui="month" data-month="${delta(-1)}" aria-label="Предыдущий месяц" ${cur <= min.slice(0, 7) ? "disabled" : ""}>${I("left")}</button><strong>${new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("ru-RU", { month: "long", year: "numeric" })}</strong><button type="button" class="iconbtn" data-ui="month" data-month="${delta(1)}" aria-label="Следующий месяц" ${cur >= max.slice(0, 7) ? "disabled" : ""}>${I("right")}</button></div><div class="calendar-grid">${["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((d) => `<span>${d}</span>`).join("")}${"<i></i>".repeat(offset)}${Array.from(
        { length: days },
        (_, i) => {
          const d = cur + "-" + String(i + 1).padStart(2, "0");
          return `<button type="button" data-ui="date" data-value="${d}" ${d < min || d > max ? "disabled" : ""} class="${d === value ? "selected" : ""}" aria-label="${new Date(d + "T12:00Z").toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" })}">${i + 1}</button>`;
        },
      ).join(
        "",
      )}</div><div class="calendar-foot">${anchor.dataset.min ? "Показатели: апрель — октябрь 2026" : "Выберите дату"}</div>`,
      "Календарь",
      "dialog",
      "calendar",
      false,
    );
    pop.month = cur;
    pop.panel.querySelector(".selected")?.focus();
  }
  function nested(anchor, html) {
    closeSub();
    const panel = document.createElement("div");
    panel.className = "popover nested-menu";
    panel.setAttribute("role", "menu");
    panel.setAttribute("aria-label", "Уведомления о ситуации");
    panel.innerHTML = html;
    layer.append(panel);
    sub = { anchor, panel };
    anchor.setAttribute("aria-expanded", "true");
    place(panel, anchor, true);
  }
  const inside = (p, a, b, c) => {
    const sign = (p, a, b) =>
        (p.x - b.x) * (a.y - b.y) - (a.x - b.x) * (p.y - b.y),
      d = [sign(p, a, b), sign(p, b, c), sign(p, c, a)];
    return !d.some((v) => v < 0) || !d.some((v) => v > 0);
  };
  const inRect = (r, p) =>
    p.x >= r.left - 4 &&
    p.x <= r.right + 4 &&
    p.y >= r.top - 4 &&
    p.y <= r.bottom + 4;
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-ui]");
    if (el) {
      const t = el.dataset.ui;
      e.preventDefault();
      if (t === "close") close(true);
      if (t === "select") {
        if (pop?.anchor === el) close(true);
        else options(el);
      }
      if (t === "choose") {
        const a = pop.anchor,
          f = document.getElementById(a.dataset.field);
        f.value = el.dataset.value;
        a.querySelector("span").textContent =
          el.querySelector("span").textContent;
        close(true);
        f.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (t === "calendar") calendar(el);
      if (t === "month") calendar(pop.anchor, el.dataset.month);
      if (t === "date") {
        const a = pop.anchor,
          f = document.getElementById(a.dataset.field);
        f.value = el.dataset.value;
        a.querySelector("span").textContent = new Date(
          f.value + "T12:00Z",
        ).toLocaleDateString("ru-RU", {
          day: "numeric",
          month: "long",
          year: "numeric",
        });
        close(true);
        f.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (t === "sub") {
        act("notify-sub", el);
      }
      return;
    }
    if (
      pop &&
      !pop.panel.contains(e.target) &&
      !sub?.panel.contains(e.target) &&
      !pop.anchor.contains(e.target)
    )
      close();
  });
  document.addEventListener("pointerover", (e) => {
    const a = e.target.closest("[data-ui=sub]");
    if (a && pop) {
      act("notify-sub", a);
      clearTimeout(closeTimer);
    }
    if (sub?.panel.contains(e.target)) {
      clearTimeout(closeTimer);
      corridor = null;
    }
  });
  document.addEventListener("pointerout", (e) => {
    if (!sub) return;
    if (
      sub.anchor.contains(e.target) &&
      !sub.anchor.contains(e.relatedTarget)
    ) {
      const p = { x: e.clientX, y: e.clientY },
        r = sub.panel.getBoundingClientRect(),
        x = r.left > p.x ? r.left - 8 : r.right + 8;
      corridor = { a: p, b: { x, y: r.top - 12 }, c: { x, y: r.bottom + 12 } };
      closeTimer = setTimeout(closeSub, 600);
    } else if (
      sub.panel.contains(e.target) &&
      !sub.panel.contains(e.relatedTarget) &&
      !sub.anchor.contains(e.relatedTarget)
    ) {
      closeTimer = setTimeout(closeSub, 180);
    }
  });
  document.addEventListener("pointermove", (e) => {
    if (!sub || !corridor) return;
    const p = { x: e.clientX, y: e.clientY };
    if (
      inRect(sub.panel.getBoundingClientRect(), p) ||
      inRect(sub.anchor.getBoundingClientRect(), p)
    ) {
      clearTimeout(closeTimer);
      return;
    }
    if (inside(p, corridor.a, corridor.b, corridor.c)) {
      clearTimeout(closeTimer);
      closeTimer = setTimeout(closeSub, 500);
    } else closeSub();
  });
  document.addEventListener(
    "keydown",
    (e) => {
      if (!pop) {
        if (
          ["ArrowDown", "ArrowUp"].includes(e.key) &&
          document.activeElement?.dataset.ui === "select"
        ) {
          e.preventDefault();
          options(document.activeElement);
        }
        return;
      }
      const panel = sub?.panel.contains(document.activeElement)
        ? sub.panel
        : pop.panel;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (sub) {
          const a = sub.anchor;
          closeSub();
          a.focus();
        } else close(true);
        return;
      }
      if (e.key === "Tab") {
        if (pop.kind === "dialog") {
          const buttons = [
            ...panel.querySelectorAll("button:not(:disabled),a,input"),
          ];
          if (e.shiftKey && document.activeElement === buttons[0]) {
            e.preventDefault();
            buttons.at(-1)?.focus();
          } else if (!e.shiftKey && document.activeElement === buttons.at(-1)) {
            e.preventDefault();
            buttons[0]?.focus();
          }
        } else close();
        return;
      }
      if (
        e.key === "ArrowRight" &&
        document.activeElement?.dataset.ui === "sub"
      ) {
        e.preventDefault();
        act("notify-sub", document.activeElement);
        sub?.panel.querySelector("button")?.focus();
        return;
      }
      if (e.key === "ArrowLeft" && sub) {
        e.preventDefault();
        const a = sub.anchor;
        closeSub();
        a.focus();
        return;
      }
      if (
        pop.panel.classList.contains("calendar") &&
        document.activeElement?.dataset.ui === "date" &&
        [
          "ArrowLeft",
          "ArrowRight",
          "ArrowUp",
          "ArrowDown",
          "Home",
          "End",
        ].includes(e.key)
      ) {
        e.preventDefault();
        const d = new Date(document.activeElement.dataset.value + "T12:00:00Z"),
          weekday = (d.getUTCDay() + 6) % 7,
          delta =
            e.key === "ArrowLeft"
              ? -1
              : e.key === "ArrowRight"
                ? 1
                : e.key === "ArrowUp"
                  ? -7
                  : e.key === "ArrowDown"
                    ? 7
                    : e.key === "Home"
                      ? -weekday
                      : 6 - weekday;
        d.setUTCDate(d.getUTCDate() + delta);
        const value = d.toISOString().slice(0, 10),
          a = pop.anchor;
        if (
          value < (a.dataset.min || "2020-01-01") ||
          value > (a.dataset.max || "2036-12-31")
        )
          return;
        if (value.slice(0, 7) !== pop.month) calendar(a, value.slice(0, 7));
        pop.panel
          .querySelector('[data-ui=date][data-value="' + value + '"]')
          ?.focus();
        return;
      }
      if (pop.kind === "dialog") return;
      const buttons = [...panel.querySelectorAll("button:not(:disabled)")];
      let i = buttons.indexOf(document.activeElement);
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
        e.preventDefault();
        i =
          e.key === "Home"
            ? 0
            : e.key === "End"
              ? buttons.length - 1
              : (i + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) %
                buttons.length;
        buttons[i]?.focus();
      } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
        buffer += e.key.toLowerCase();
        clearTimeout(typeTimer);
        typeTimer = setTimeout(() => (buffer = ""), 600);
        buttons
          .find((b) => b.textContent.trim().toLowerCase().startsWith(buffer))
          ?.focus();
      }
    },
    true,
  );
  addEventListener("resize", () => {
    if (pop) place(pop.panel, pop.anchor);
    if (sub) place(sub.panel, sub.anchor, true);
  });
  addEventListener(
    "scroll",
    (e) => {
      if (pop && !layer.contains(e.target)) close();
    },
    true,
  );
  return { show, close, item, nested, isOpen: () => !!pop };
}
