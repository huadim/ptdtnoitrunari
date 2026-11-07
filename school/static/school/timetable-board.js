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
