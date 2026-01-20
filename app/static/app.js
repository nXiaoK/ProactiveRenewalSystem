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
});
