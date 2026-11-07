for (const form of document.querySelectorAll("form[data-duty-max]")) {
  const feedback = form.querySelector("[data-duty-feedback]");
  const limit = Number(form.dataset.dutyMax);

  function update() {
    const selected = [...form.querySelectorAll('input[name="employees"]:checked')];
    const conflicts = selected.map((input) => input.dataset.warning).filter(Boolean);
    if (selected.length > limit) {
      conflicts.unshift(`Đã chọn ${selected.length} người. Tối đa ${limit}; hãy bỏ bớt người.`);
    }
    const ready = conflicts.length === 0;
    feedback.textContent = ready
      ? `Đã chọn ${selected.length}/${limit} người${selected.length < limit ? ". Có thể lưu nháp và bổ sung sau" : ""}.`
      : conflicts.join(" ");
    feedback.className = ready ? "hint" : "alert error";
    return ready;
  }

  form.addEventListener("change", update);
  form.addEventListener("submit", (event) => {
    if (!update()) event.preventDefault();
  });
  update();
}
