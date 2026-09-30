(function () {
  const loginForm = document.getElementById("login-form");
  const registerForm = document.getElementById("register-form");
  const title = document.getElementById("auth-title");
  const copy = document.getElementById("auth-copy");
  const switcher = document.getElementById("auth-switch");
  const registerDisabled = document.getElementById("register-disabled");
  let config = { registration_enabled: true };

  function mode() {
    return window.location.pathname === "/register" ? "register" : "login";
  }

  function renderMode() {
    const isLogin = mode() === "login";
    loginForm.hidden = !isLogin;
    registerForm.hidden = isLogin;
    title.textContent = isLogin ? "登录万仞" : "创建万仞账号";
    copy.textContent = isLogin ? "使用邮箱和密码进入工作台。" : "注册后可创建内容任务、上传素材并生成文章。";
    switcher.innerHTML = isLogin
      ? '还没有账号？<a class="site-link" href="/register">注册</a>'
      : '已有账号？<a class="site-link" href="/login">登录</a>';
    document.title = isLogin ? "万仞 | 登录" : "万仞 | 注册";
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const text = await response.text();
    let payload;
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
    if (!response.ok) {
      const error = new Error(payload.detail || response.statusText);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function setLoading(button, loading) {
    button.classList.toggle("loading", loading);
    button.disabled = loading;
  }

  async function loadConfig() {
    try {
      config = await request("/api/auth/config");
    } catch {
      config.registration_enabled = false;
    }
    const enabled = Boolean(config.registration_enabled);
    registerDisabled.hidden = enabled || mode() !== "register";
    ["register-email", "register-name", "register-password", "register-confirm", "register-button"].forEach(id => {
      document.getElementById(id).disabled = !enabled;
    });
  }

  async function checkSession() {
    try {
      await request("/api/auth/me");
      window.location.href = `/?auth=${Date.now()}`;
      return true;
    } catch (error) {
      if (error.status === 401) return false;
      throw error;
    }
  }

  loginForm.addEventListener("submit", async event => {
    event.preventDefault();
    const button = document.getElementById("login-button");
    const error = document.getElementById("login-error");
    error.textContent = "";
    setLoading(button, true);
    try {
      await request("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email: document.getElementById("login-email").value,
          password: document.getElementById("login-password").value,
        }),
      });
      // Ensure the browser has committed the HttpOnly cookie before navigation.
      await request("/api/auth/me");
      window.location.href = `/?auth=${Date.now()}`;
    } catch (exc) {
      error.textContent = exc.message;
      setLoading(button, false);
    }
  });

  registerForm.addEventListener("submit", async event => {
    event.preventDefault();
    const button = document.getElementById("register-button");
    const error = document.getElementById("register-error");
    error.textContent = "";
    setLoading(button, true);
    try {
      await request("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({
          email: document.getElementById("register-email").value,
          name: document.getElementById("register-name").value,
          password: document.getElementById("register-password").value,
          confirm_password: document.getElementById("register-confirm").value,
        }),
      });
      await request("/api/auth/me");
      window.location.href = "/";
    } catch (exc) {
      error.textContent = exc.message;
      setLoading(button, false);
    }
  });

  renderMode();
  loadConfig();
  checkSession().then(authenticated => {
    if (!authenticated) document.getElementById("login-email").focus();
  });
})();
