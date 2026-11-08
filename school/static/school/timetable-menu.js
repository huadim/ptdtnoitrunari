for (const bar of document.querySelectorAll("[data-tkb-menu]")) {
  const menus = [...bar.querySelectorAll(":scope > details")];
  const close = (menu) => {
    menu.open = false;
    for (const child of menu.querySelectorAll("details[open]")) child.open = false;
  };

  bar.addEventListener("toggle", (event) => {
    const menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.open) return;
    for (const sibling of menu.parentElement.children) {
      if (sibling !== menu && sibling instanceof HTMLDetailsElement) close(sibling);
    }
  }, true);

  for (const submenu of bar.querySelectorAll("[data-tkb-flyout]")) {
    const summary = submenu.querySelector("summary");
    let openedByPointer = false;
    submenu.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "mouse" || !window.matchMedia("(min-width: 801px)").matches || submenu.open) return;
      openedByPointer = true;
      submenu.open = true;
    });
    submenu.addEventListener("pointerleave", () => {
      if (openedByPointer && !submenu.contains(document.activeElement)) close(submenu);
      openedByPointer = false;
    });
    summary.addEventListener("click", (event) => {
      if (openedByPointer && submenu.open) {
        event.preventDefault();
        openedByPointer = false;
      }
    });
  }

  bar.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (!link) return;
    const menu = menus.find((item) => item.contains(link));
    if (menu) window.setTimeout(() => close(menu), 0);
  });

  bar.addEventListener("keydown", (event) => {
    const submenu = event.target.closest("[data-tkb-flyout]");
    if (submenu && submenu.open && event.key === "ArrowLeft") {
      close(submenu);
      submenu.querySelector("summary").focus();
      event.preventDefault();
      return;
    }
    const trigger = event.target.closest("summary");
    if (!trigger) return;
    const menu = trigger.parentElement;
    const index = menus.indexOf(menu);
    if (index !== -1 && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      const offsets = {ArrowLeft: -1, ArrowRight: 1};
      const next = event.key === "Home" ? menus[0] : event.key === "End" ? menus.at(-1)
        : menus[(index + offsets[event.key] + menus.length) % menus.length];
      const wasOpen = menu.open;
      close(menu);
      if (wasOpen) next.open = true;
      next.querySelector("summary").focus();
      event.preventDefault();
    }
    if (event.key === "ArrowDown" || submenu && event.key === "ArrowRight") {
      menu.open = true;
      menu.querySelector(":scope > .tkb-popup :is(a, button, summary)")?.focus();
      event.preventDefault();
    }
  });

  document.addEventListener("click", (event) => {
    if (!bar.contains(event.target)) menus.forEach(close);
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.closest?.("dialog[open]")) return;
    if (event.key === "Escape") {
      const open = bar.querySelector("details[open] details[open]") || menus.find((item) => item.open);
      if (open) {
        close(open);
        open.querySelector("summary").focus();
        event.preventDefault();
      }
      return;
    }
    if (event.defaultPrevented || event.repeat || event.altKey || event.shiftKey || event.metaKey ||
        event.target.closest?.("input, select, textarea, [contenteditable]")) return;
    const key = event.ctrlKey ? `Ctrl+${event.key.toUpperCase()}` : event.key;
    const link = [...bar.querySelectorAll("[data-tkb-key]")].find((item) => item.dataset.tkbKey === key);
    if (link) {
      event.preventDefault();
      window.location.assign(link.href);
    }
  });
}

for (const dialog of document.querySelectorAll("[data-tkb-info]")) {
  if (!dialog.showModal) continue;
  dialog.removeAttribute("open");
  dialog.showModal();
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    window.location.assign(dialog.querySelector("[data-tkb-info-close]").href);
  });
}

for (const button of document.querySelectorAll("[data-tkb-print]")) {
  button.hidden = false;
  button.addEventListener("click", () => window.print());
}
