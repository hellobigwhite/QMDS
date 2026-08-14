const PASSWORD = "f!XsS$J2WneOkMyUgQ";
const RETRY_COUNT = 3;
const RETRY_DELAY_MS = 800;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "LOGIN_AND_OPEN_MENU_BATCH") {
    return false;
  }

  loginAndOpenMenuBatch(message.siteUrls)
    .then((result) => sendResponse({ ok: true, ...result }))
    .catch((error) => sendResponse({ ok: false, error: error.message }));

  return true;
});

async function loginAndOpenMenuBatch(rawSiteUrls) {
  if (!Array.isArray(rawSiteUrls) || rawSiteUrls.length === 0) {
    throw new Error("请输入至少一个网站 URL 或域名。");
  }

  const results = await Promise.all(rawSiteUrls.map(async (rawSiteUrl) => {
    try {
      const menuUrl = await prepareMenuUrl(rawSiteUrl);
      return { input: rawSiteUrl, ok: true, menuUrl };
    } catch (error) {
      return { input: rawSiteUrl, ok: false, error: error.message };
    }
  }));

  await Promise.all(results.map(async (result) => {
    if (!result.ok) {
      return;
    }

    try {
      await chrome.tabs.create({ url: result.menuUrl });
    } catch (error) {
      result.ok = false;
      result.error = error.message;
    }
  }));

  const successCount = results.filter((result) => result.ok).length;

  return {
    results: results.map(({ menuUrl: _menuUrl, ...result }) => result),
    successCount,
    failureCount: results.length - successCount
  };
}

async function prepareMenuUrl(rawSiteUrl) {
  const site = normalizeSite(rawSiteUrl);
  const username = `Ad${site.namePart}min`;
  const loginUrl = `${site.origin}/bbwllogin/`;
  const adminUrl = `${site.origin}/wp-admin/`;
  const menuUrl = `${site.origin}/wp-admin/nav-menus.php`;

  const body = new URLSearchParams();

  body.set("log", username);
  body.set("pwd", PASSWORD);
  body.set("wp-submit", "Log In");
  body.set("redirect_to", adminUrl);
  body.set("testcookie", "1");

  const response = await requestWithRetry(loginUrl, {
    method: "POST",
    credentials: "include",
    redirect: "follow",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded"
    },
    body: body.toString()
  });

  if (!response) {
    throw new Error("登录请求失败，已重试。请检查域名是否可访问。");
  }

  if (!response.ok) {
    throw new Error(`登录请求失败：HTTP ${response.status}`);
  }

  if (!isLoginPageUrl(response.url)) {
    return menuUrl;
  }

  const adminCheckResponse = await requestWithRetry(adminUrl, {
    method: "GET",
    credentials: "include",
    redirect: "follow",
    cache: "no-store"
  });

  if (adminCheckResponse?.ok && !isLoginPageUrl(adminCheckResponse.url)) {
    return menuUrl;
  }

  throw new Error("登录失败，无法访问 /wp-admin/。请检查域名、用户名规则或密码。");
}

async function requestWithRetry(url, options) {
  for (let attempt = 1; attempt <= RETRY_COUNT; attempt += 1) {
    try {
      return await fetch(url, options);
    } catch (error) {
      if (attempt === RETRY_COUNT) {
        return null;
      }

      await delay(RETRY_DELAY_MS * attempt);
    }
  }

  return null;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isLoginPageUrl(url) {
  return url.includes("/bbwllogin/") || url.includes("wp-login.php");
}

function normalizeSite(rawSiteUrl) {
  const input = rawSiteUrl.trim();
  let url;

  try {
    url = new URL(input.includes("://") ? input : `https://${input}`);
  } catch (_error) {
    throw new Error("请输入有效域名，例如 example.com。");
  }

  const hostParts = url.hostname.toLowerCase().split(".").filter(Boolean);

  if (hostParts.length < 2) {
    throw new Error("请输入有效域名，例如 example.com。");
  }

  const domainParts = hostParts[0] === "www" ? hostParts.slice(1) : hostParts;
  const domain = domainParts.join(".");
  const namePart = domain.replace(/\.com$/i, "");
  const hostname = `www.${domainParts.join(".")}`;

  return {
    namePart,
    origin: `https://${hostname}`
  };
}
