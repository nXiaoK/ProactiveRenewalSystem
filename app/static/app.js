document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-confirm]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      const message = btn.getAttribute("data-confirm") || "确认操作？";
      if (!window.confirm(message)) {
        event.preventDefault();
      }
    });
  });

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.getAttribute("data-copy");
      if (!text) {
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "已复制";
        setTimeout(() => {
          btn.textContent = "复制链接";
        }, 1500);
      } catch (err) {
        window.alert("复制失败，请手动复制。" + err);
      }
    });
  });

  const selectBindings = [
    { select: "[data-select=\"category\"]", input: "[data-input=\"category\"]" },
    { select: "[data-select=\"currency\"]", input: "[data-input=\"currency\"]" },
  ];

  const hasOption = (selectEl, value) =>
    Array.from(selectEl.options).some((option) => option.value === value);

  const syncField = (selectEl, inputEl) => {
    const selectValue = selectEl.value;
    if (selectValue === "__custom__") {
      inputEl.hidden = false;
      if (inputEl.dataset.autofill === "true") {
        inputEl.value = "";
      }
      inputEl.dataset.autofill = "false";
      return;
    }

    inputEl.hidden = true;
    if (selectValue) {
      inputEl.value = selectValue;
      inputEl.dataset.autofill = "true";
    }
  };

  selectBindings.forEach(({ select, input }) => {
    const selectEl = document.querySelector(select);
    const inputEl = document.querySelector(input);
    if (!selectEl || !inputEl) {
      return;
    }

    if (!selectEl.value && inputEl.value) {
      if (hasOption(selectEl, inputEl.value)) {
        selectEl.value = inputEl.value;
      } else if (hasOption(selectEl, "__custom__")) {
        selectEl.value = "__custom__";
      }
    }

    syncField(selectEl, inputEl);

    selectEl.addEventListener("change", () => {
      syncField(selectEl, inputEl);
    });
  });
});
