(function () {
  const themes = [
    { key: "mo", label: "苍青", color: "#175641" },
    { key: "azure", label: "云蓝", color: "#0f5f8f" },
    { key: "graphite", label: "岩灰", color: "#59b3a0" },
    { key: "vermilion", label: "朱砂", color: "#a63d2a" },
    { key: "violet", label: "紫罗兰", color: "#5d55ae" },
    { key: "tide", label: "潮汐", color: "#22696d" },
  ];
  const storageKey = "wanren_theme";

  function applyTheme(themeKey) {
    const selected = themes.find(theme => theme.key === themeKey) || themes[0];
    document.documentElement.dataset.theme = selected.key;
    localStorage.setItem(storageKey, selected.key);
    document.querySelectorAll(".theme-switcher").forEach(container => {
      container.innerHTML = themes.map(theme => `
        <button type="button" class="theme-dot ${theme.key === selected.key ? "active" : ""}"
          data-theme-key="${theme.key}" title="${theme.label}主题" aria-label="${theme.label}主题"
          style="background:${theme.color}"></button>`).join("");
      container.querySelectorAll(".theme-dot").forEach(button => {
        button.addEventListener("click", () => applyTheme(button.dataset.themeKey));
      });
    });
  }

  window.WanRenTheme = { applyTheme, themes, storageKey };
  applyTheme(localStorage.getItem(storageKey) || "mo");
})();
