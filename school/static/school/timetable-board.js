const workbench = document.querySelector("[data-timetable-workbench]");

if (workbench) {
  const sundayToggle = document.querySelector("[data-toggle-sunday]");
  if (sundayToggle) {
    document.querySelector("[data-sunday-control]").hidden = false;
    const showSunday = () => workbench.classList.toggle("hide-sunday", !sundayToggle.checked);
    sundayToggle.addEventListener("change", showSunday);
    showSunday();
  }

  const quickTabs = workbench.querySelector("[data-quick-tabs]");
  const quickButtons = [...quickTabs.querySelectorAll("[data-quick-tab]")];
  const quickSections = [...workbench.querySelectorAll("[data-quick-section]")];
  const openQuickSection = (section) => {
    quickButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.quickTab === section)));
    quickSections.forEach((panel) => { panel.hidden = panel.dataset.quickSection !== section; });
  };
  quickTabs.hidden = false;
  const quickSectionForHash = () => {
    if (window.location.hash === "#thong-ke-tkb") return "stats";
    if (window.location.hash === "#huong-dan-tkb") return "help";
    return workbench.hasAttribute("data-lesson-selected") ? "lesson" : "stats";
  };
  openQuickSection(quickSectionForHash());
  window.addEventListener("hashchange", () => openQuickSection(quickSectionForHash()));
  quickButtons.forEach((button) => button.addEventListener("click", () => openQuickSection(button.dataset.quickTab)));
  document.querySelectorAll("[data-open-quick]").forEach((link) => {
    link.addEventListener("click", () => openQuickSection(link.dataset.openQuick));
  });

  const lessons = JSON.parse(document.getElementById("comparison-lessons").textContent);
  const canEdit = workbench.hasAttribute("data-can-edit");
  for (const panel of workbench.querySelectorAll("[data-comparison]")) {
    const primaryKey = panel.dataset.comparison;
    const primary = panel.querySelector("[data-comparison-primary]");
    const secondary = panel.querySelector("[data-comparison-secondary]");
    const status = panel.querySelector("[data-comparison-status]");
    const cells = new Map([...panel.querySelectorAll("[data-comparison-slot]")].map((cell) => [cell.dataset.comparisonSlot, cell]));

    const render = () => {
      cells.forEach((cell) => cell.replaceChildren());
      const selected = primary.value.toLocaleLowerCase("vi");
      const filter = secondary.value.toLocaleLowerCase("vi");
      const scheduleLessons = lessons.filter((lesson) => lesson[primaryKey].toLocaleLowerCase("vi") === selected);
      const visible = scheduleLessons.filter((lesson) => !filter ||
        lesson[secondary.dataset.comparisonSecondary].toLocaleLowerCase("vi") === filter);
      for (const lesson of visible) {
        const cell = cells.get(`${lesson.day}:${lesson.period}`);
        if (!cell) continue;
        const link = document.createElement("a");
        const url = new URL(workbench.dataset.weekUrl, window.location.href);
        url.searchParams.set("focus", lesson.id);
        url.hash = "lien-quan";
        link.href = url.href;
        link.className = "comparison-lesson";
        link.title = `${lesson.subject} · ${lesson.class} · ${lesson.teacherName} · ${lesson.room}`;
        const subject = document.createElement("strong");
        subject.textContent = lesson.subject;
        const detail = document.createElement("small");
        detail.textContent = primaryKey === "class" ? lesson.teacherName : lesson.class;
        link.append(subject, detail);
        cell.append(link);
      }
      if (canEdit && selected) {
        const occupiedSlots = new Set(scheduleLessons.map((lesson) => `${lesson.day}:${lesson.period}`));
        for (const [slot, cell] of cells) {
          if (occupiedSlots.has(slot)) continue;
          const [day, period] = slot.split(":");
          const url = new URL(workbench.dataset.weekUrl, window.location.href);
          url.search = "";
          url.searchParams.set("scope", primaryKey);
          url.searchParams.set(primaryKey, primary.value);
          if (secondary.value) {
            const key = secondary.dataset.comparisonSecondary === "subject" ? "slot_subject" : "class";
            url.searchParams.set(key, secondary.value);
          }
          url.searchParams.set("day", day);
          url.searchParams.set("period", period);
          url.hash = "lesson-editor";
          const link = document.createElement("a");
          link.href = url.href;
          link.className = "add-cell";
          link.textContent = "+";
          link.setAttribute("aria-label", `Thêm tiết ${period} ngày ${day}`);
          cell.append(link);
        }
      }
      status.textContent = selected ? `${visible.length} tiết trên lưới` : "Chọn đối tượng để xem lịch.";
    };
    primary.addEventListener("change", render);
    secondary.addEventListener("change", render);
    render();
  }
}

const schedule = document.querySelector(".timetable-grid");

if (schedule) {
  let editor = null;
  let highlighted = null;

  const clearTarget = () => {
    if (highlighted) highlighted.classList.remove("drop-target");
    highlighted = null;
  };

  schedule.addEventListener("dragstart", (event) => {
    const lesson = event.target.closest("[data-drag-lesson]");
    if (!lesson || !event.dataTransfer) return;
    editor = new URL(lesson.href);
    event.dataTransfer.setData("text/plain", "timetable-lesson");
    event.dataTransfer.effectAllowed = "move";
  });

  schedule.addEventListener("dragover", (event) => {
    const cell = event.target.closest("[data-target-day]");
    if (!editor || !cell || !event.dataTransfer) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (highlighted !== cell) {
      clearTarget();
      highlighted = cell;
      highlighted.classList.add("drop-target");
    }
  });

  schedule.addEventListener("drop", (event) => {
    const cell = event.target.closest("[data-target-day]");
    if (!editor || !cell) return;
    event.preventDefault();
    editor.searchParams.set("target_day", cell.dataset.targetDay);
    editor.searchParams.set("target_period", cell.dataset.targetPeriod);
    window.location.assign(editor.href);
  });

  schedule.addEventListener("dragend", () => {
    editor = null;
    clearTarget();
  });
}
