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

  const viewToggle = document.querySelector("[data-view-toggle]");
  if (viewToggle) {
    const viewRoot = document.querySelector(".view-root");
    const viewInput = document.querySelector("input[name=\"view\"]");
    const viewButtons = Array.from(viewToggle.querySelectorAll("[data-view]"));
    const allowedViews = new Set(
      viewButtons.map((btn) => btn.getAttribute("data-view")).filter(Boolean),
    );
    const storageKey = "renewal_view";
    const url = new URL(window.location.href);
    const paramView = url.searchParams.get("view");
    let storedView = null;
    try {
      storedView = window.localStorage.getItem(storageKey);
    } catch (err) {
      storedView = null;
    }

    const setActiveButton = (value) => {
      viewButtons.forEach((btn) => {
        if (btn.getAttribute("data-view") === value) {
          btn.classList.add("active");
        } else {
          btn.classList.remove("active");
        }
      });
    };

    const setRootClass = (value) => {
      if (!viewRoot) {
        return;
      }
      Array.from(viewRoot.classList).forEach((name) => {
        if (name.startsWith("view-") && name !== "view-root") {
          viewRoot.classList.remove(name);
        }
      });
      viewRoot.classList.add(`view-${value}`);
    };

    const applyView = (value, persist = true) => {
      if (!allowedViews.has(value)) {
        return;
      }
      setRootClass(value);
      setActiveButton(value);
      if (viewInput) {
        viewInput.value = value;
      }
      if (persist) {
        try {
          window.localStorage.setItem(storageKey, value);
        } catch (err) {
          // ignore storage failures
        }
      }
      if (url.searchParams.get("view") !== value) {
        url.searchParams.set("view", value);
        window.history.replaceState({}, "", url.toString());
      }
    };

    let fallbackView = "card";
    if (viewRoot) {
      const viewClass = Array.from(viewRoot.classList).find(
        (name) => name.startsWith("view-") && name !== "view-root",
      );
      if (viewClass) {
        fallbackView = viewClass.replace("view-", "");
      }
    }

    if (paramView && allowedViews.has(paramView)) {
      applyView(paramView, true);
    } else if (storedView && allowedViews.has(storedView)) {
      applyView(storedView, false);
    } else {
      applyView(fallbackView, false);
    }

    viewButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.getAttribute("data-view");
        if (!value) {
          return;
        }
        applyView(value, true);
      });
    });
  }

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
