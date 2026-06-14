(() => {
  // =========================
  // A) 登录页自动填充
  // =========================
  const FIXED_PASSWORD = "f!XsS$J2WneOkMyUgQ"; 

  function extractSiteName(hostname) {
    const m = hostname.match(/^www\.([a-z0-9-]+)\.com$/i);
    return m ? m[1] : null;
  }

  function buildUsername(siteName) {
    return `ad${siteName}min`;
  }

  function setValueAndTrigger(el, value) {
    if (!el) return false;
    el.focus();
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur();
    return true;
  }

  function tryFillOnce() {
    const userEl = document.querySelector("#user_login, input[name='log']");
    const passEl = document.querySelector("#user_pass, input[name='pwd'], input[type='password']");
    if (!userEl || !passEl) return false;

    const site = extractSiteName(location.hostname);
    if (!site) return false;

    const username = buildUsername(site);

    if (!userEl.value) setValueAndTrigger(userEl, username);
    if (!passEl.value) setValueAndTrigger(passEl, FIXED_PASSWORD);
    return true;
  }

  function fillWithRetry() {
    let attempts = 0;
    const maxAttempts = 30;
    const timer = setInterval(() => {
      attempts += 1;
      const ok = tryFillOnce();
      if (ok || attempts >= maxAttempts) clearInterval(timer);
    }, 500);
  }

  function runLoginAutofill() {
    if (location.pathname.startsWith("/bbwllogin")) {
      fillWithRetry();
    }
  }

  // =========================
  // B) plugins.php：按钮 + 一键启用/配置（纯 content script）
  // =========================
  function isPluginsPage() {
    return location.pathname.includes("/wp-admin/plugins.php");
  }

  function absUrl(href) {
    try {
      return new URL(href, location.href).toString();
    } catch {
      return href;
    }
  }

  async function fetchWithTimeout(url, init = {}, timeoutMs = 60000) {
    // timeoutMs <= 0 表示不超时（一般不建议）
    if (!timeoutMs || timeoutMs <= 0) {
      return fetch(url, { credentials: "include", redirect: "follow", ...init });
    }

    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);

    try {
      return await fetch(url, {
        credentials: "include",
        redirect: "follow",
        signal: controller.signal,
        ...init
      });
    } catch (e) {
      const msg = (e && e.message) ? e.message : String(e);
      if (e?.name === "AbortError" || /aborted/i.test(msg)) {
        throw new Error(`请求超时（${Math.round(timeoutMs / 1000)}s）：${url}`);
      }
      throw e;
    } finally {
      clearTimeout(t);
    }
  }

  function runPluginsButtons() {
    if (!isPluginsPage()) return;
    if (document.getElementById("wp-oneclick-panel")) return;

    // ---- UI ----
    const wrap = document.createElement("div");
    wrap.id = "wp-oneclick-panel";
    wrap.style.cssText =
      "margin:12px 0;padding:12px;border:1px solid #c3c4c7;background:#fff;border-radius:6px;";

    const title = document.createElement("div");
    title.textContent = "一键启用 + 配置（Yoast / WP Rocket）";
    title.style.cssText = "font-weight:600;margin-bottom:8px;";
    wrap.appendChild(title);

    const btnRow = document.createElement("div");
    btnRow.style.cssText =
      "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px;";
    wrap.appendChild(btnRow);

    function makeBtn(text, primary = false) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = primary ? "button button-primary" : "button";
      b.textContent = text;
      return b;
    }

    const btnYoast = makeBtn("Yoast 一键启用 + 配置", true);
    const btnRocket = makeBtn("WP Rocket 一键启用 + 配置", true);
    const btnClear = makeBtn("清空日志", false);

    btnRow.appendChild(btnYoast);
    btnRow.appendChild(btnRocket);
    btnRow.appendChild(btnClear);

    const logBox = document.createElement("pre");
    logBox.style.cssText =
      "margin:0;max-height:260px;overflow:auto;padding:10px;background:#f6f7f7;border:1px solid #dcdcde;border-radius:6px;font-size:12px;line-height:1.4;white-space:pre-wrap;";
    wrap.appendChild(logBox);

    function uiLog(line) {
      const ts = new Date().toLocaleTimeString();
      logBox.textContent += `[${ts}] ${line}\n`;
      logBox.scrollTop = logBox.scrollHeight;
    }

    btnClear.addEventListener("click", () => (logBox.textContent = ""));

    const anchor = document.querySelector(".wrap > h1") || document.querySelector(".wrap");
    if (anchor && anchor.parentElement) {
      anchor.parentElement.insertBefore(wrap, anchor.nextSibling);
    } else {
      document.body.prepend(wrap);
    }

    // ---- 激活工具 ----
    function findActivateLink(linkId, pluginFile) {
      let a = document.getElementById(linkId);
      if (a && a.getAttribute("href")) return a;

      if (pluginFile) {
        const encoded = encodeURIComponent(pluginFile);
        a = document.querySelector(
          `a[href*="action=activate"][href*="plugin="][href*="${encoded}"]`
        );
        if (a && a.getAttribute("href")) return a;

        const dir = pluginFile.split("/")[0];
        a = document.querySelector(`a[href*="action=activate"][href*="${dir}"]`);
        if (a && a.getAttribute("href")) return a;
      }
      return null;
    }

    async function maybeActivate(linkId, pluginFile) {
      const a = findActivateLink(linkId, pluginFile);
      if (!a) {
        uiLog(`未找到激活链接：${linkId}（可能已启用，或插件未安装）`);
        return true;
      }
      const url = absUrl(a.getAttribute("href"));
      uiLog(`请求激活：${linkId}`);

      // 激活可能会比较慢（解压/写入/缓存等），给足时间
      const res = await fetchWithTimeout(url, { method: "GET" }, 180000);

      uiLog(`激活返回：HTTP ${res.status}`);
      return res.ok;
    }

    // ---- Yoast 配置 ----
    async function getYoastNonce() {
      const url = `${location.origin}/wp-admin/admin.php?page=wpseo_dashboard#/first-time-configuration`;
      uiLog("拉取 Yoast 配置页 nonce...");
    
      const res = await fetchWithTimeout(url, { method: "GET" }, 60000);
      const html = await res.text();
      if (!res.ok) throw new Error(`无法访问 Yoast 配置页，HTTP ${res.status}`);
    
      // 关键：只在包含 wpApiSettings 的 script 里取 nonce（避免误抓其它 nonce）
      const doc = new DOMParser().parseFromString(html, "text/html");
      const scripts = Array.from(doc.querySelectorAll("script"));
    
      for (const s of scripts) {
        const txt = s.textContent || "";
        if (!txt.includes("wpApiSettings")) continue;
    
        // 优先解析 wpApiSettings = {...}
        const mObj = txt.match(/wpApiSettings\s*=\s*({[\s\S]*?})\s*;?/);
        if (mObj) {
          try {
            const obj = JSON.parse(mObj[1]);
            if (obj && obj.nonce) return obj.nonce;
          } catch (_) {}
        }
    
        // 回退：按 python 脚本的方式在该 script 中抓 nonce":"xxx"
        const m = txt.match(/nonce"\s*:\s*"([a-zA-Z0-9]+)"/);
        if (m) return m[1];
      }
    
      throw new Error("未在包含 wpApiSettings 的脚本中找到 REST nonce（可能页面结构/安全策略/Yoast 版本差异）");
    }

    async function getLogoId(yoastNonce) {
      uiLog("查询媒体库 logo.png（可选）...");
      const url = `${location.origin}/wp-json/wp/v2/media?search=${encodeURIComponent("logo.png")}`;
      const res = await fetchWithTimeout(
        url,
        { method: "GET", headers: { "X-WP-Nonce": yoastNonce } },
        60000
      );
      if (!res.ok) return { id: 0, url: '' };
      const data = await res.json().catch(() => null);
      if (!Array.isArray(data)) return { id: 0, url: '' };
      const hit = data.find((x) => x && x.source_url && String(x.source_url).toLowerCase().includes("logo.png"));
      return hit ? { id: hit.id, url: hit.source_url } : { id: 0, url: '' };
    }

    async function debugRestAuth(yoastNonce) {
      uiLog("自检：/wp-json/wp/v2/users/me ...");
      const res = await fetchWithTimeout(
        `${location.origin}/wp-json/wp/v2/users/me?_wpnonce=${encodeURIComponent(yoastNonce)}`,
        { method: "GET", headers: { "X-WP-Nonce": yoastNonce, "X-Requested-With": "XMLHttpRequest" } },
        60000
      );
    
      const t = await res.text();
      uiLog(`自检返回：HTTP ${res.status}`);
      uiLog(`自检响应前120字：${t.slice(0, 120).replace(/\s+/g, " ")}`);
    
      if (!res.ok) {
        throw new Error("REST 认证自检失败：说明 nonce 不对或 REST 请求没带上登录态 cookie");
      }
    }
    

    async function postYoast(path, yoastNonce, payload) {
      const joiner = path.includes("?") ? "&" : "?";
      const url = `${location.origin}${path}${joiner}_wpnonce=${encodeURIComponent(yoastNonce)}`;
    
      const res = await fetchWithTimeout(
        url,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-WP-Nonce": yoastNonce,
            "X-Requested-With": "XMLHttpRequest"
          },
          body: JSON.stringify(payload)
        },
        60000
      );
    
      const text = await res.text();
      if (!res.ok) {
        throw new Error(`Yoast API 失败：${path} HTTP ${res.status} ${text.slice(0, 200)}`);
      }
    }
    

    async function runYoast() {
      uiLog("=== Yoast：开始 ===");
      await maybeActivate("activate-wordpress-seo", "wordpress-seo/wp-seo.php");
      await maybeActivate("activate-yoast-seo-premium", "yoast-seo-premium/yoast-seo-premium.php");

      const nonce = await getYoastNonce();
      uiLog("已获取 Yoast nonce");
      await debugRestAuth(nonce);

      const siteDomain = location.hostname.replace(/^www\./i, "");
      const logo = await getLogoId(nonce); // 修改为返回 {id, url}
      uiLog(logo.id > 0 ? `找到 logo.png，ID: ${logo.id}, URL: ${logo.url}` : `未找到 logo.png，使用默认 0`);

      await postYoast(
        "/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user",
        nonce,
        { finishedSteps: ["optimizeSeoData"] }
      );

      await postYoast(
        "/wp-json/yoast/v1/configuration/site_representation?_locale=user",
        nonce,
        {
          company_or_person: "company",
          company_name: siteDomain,
          company_logo: logo.url || `${location.origin}/wp-content/uploads/logo.png`,
          company_logo_id: logo.id || 0,
          person_logo: "",
          person_logo_id: 0,
          website_name: siteDomain
        }
      );

      await postYoast(
        "/wp-json/yoast/v1/configuration/save_configuration_state?_locale=user",
        nonce,
        { finishedSteps: ["optimizeSeoData", "siteRepresentation", "socialProfiles", "personalPreferences"] }
      );

      await postYoast(
        "/wp-json/yoast/v1/configuration/social_profiles?_locale=user",
        nonce,
        { facebook_site: "", twitter_site: "", other_social_urls: [] }
      );

      await postYoast(
        "/wp-json/yoast/v1/configuration/enable_tracking?_locale=user",
        nonce,
        { tracking: 0 }
      );

      // ==== 新增：更新首页标题 & 元描述（使用专用 settings nonce） ====
      uiLog("=== 更新 Yoast 全局首页标题 & 元描述（使用专用 settings nonce） ===");

      // 1. 拉取 Settings 主页（yoast-seo-new-settings-js-extra 脚本通常在这里）
      const settingsUrl = `${location.origin}/wp-admin/admin.php?page=wpseo_page_settings`;
      uiLog(`拉取 Yoast Settings 页以提取 wpseoScriptData...`);

      const res = await fetchWithTimeout(settingsUrl, { method: "GET" }, 60000);
      if (!res.ok) throw new Error(`拉取失败 HTTP ${res.status}`);

      const html = await res.text();

      // 2. 提取 <script id="yoast-seo-new-settings-js-extra"> 中的 wpseoScriptData
      const scriptMatch = html.match(/<script id="yoast-seo-new-settings-js-extra">\s*([\s\S]*?)\s*<\/script>/);
      if (!scriptMatch) throw new Error("未找到 yoast-seo-new-settings-js-extra 脚本");

      const scriptContent = scriptMatch[1];

      // 提取 var wpseoScriptData = {...};
      const dataMatch = scriptContent.match(/var\s+wpseoScriptData\s*=\s*({[\s\S]*?})\s*;/);
      if (!dataMatch) throw new Error("未提取到 wpseoScriptData 对象");

      let settingsData;
      try {
        // 清理常见 JSON 问题（模仿 Python clean_json_string）
        let jsonStr = dataMatch[1].trim();
        jsonStr = jsonStr.replace(/,\s*([}\]])/g, '$1'); // 移除尾随逗号
        jsonStr = jsonStr.replace(/^\s*,/, ''); // 开头逗号
        jsonStr = jsonStr.replace(/(?<!\\)'/g, '"'); // 单引号转双引号

        settingsData = JSON.parse(jsonStr);
      } catch (e) {
        throw new Error(`JSON 解析失败: ${e.message}`);
      }

      const settingsObj = settingsData.settings || {};
      if (!settingsObj) throw new Error("settingsData 中无 settings 字段");

      // 提取专用 nonce（通常在 settings.nonce 或类似位置）
      let yoastNonce = settingsObj.nonce || settingsObj.wpnonce || null;  // 用 let

      if (!yoastNonce) {
        // fallback: 从 script 全局搜索 nonce
        const nonceMatch = scriptContent.match(/"nonce"\s*:\s*"([a-zA-Z0-9\-_]+)"/);
        if (nonceMatch) {
          yoastNonce = nonceMatch[1];
          uiLog(`从 script fallback 找到 nonce: ${yoastNonce}`);
        }
      }

      if (!yoastNonce) throw new Error("未找到专用 nonce");

      uiLog(`提取到专用 nonce: ${yoastNonce}`);

      // 3. 修改首页字段（示例针对电商站）
      settingsObj.wpseo_titles = settingsObj.wpseo_titles || {};
      settingsObj.wpseo_titles["title-home-wpseo"] = "%%sitename%%";
      settingsObj.wpseo_titles["metadesc-home-wpseo"] = "%%sitedesc%%";

      // 4. 设置首页专用 Open Graph 图片（针对首页分享时优先使用）
      settingsObj.wpseo_titles.open_graph_frontpage_image = logo.url || `${location.origin}/wp-content/uploads/logo.png`;
      settingsObj.wpseo_titles.open_graph_frontpage_image_id = logo.id || 0;

      // 5. 设置全站默认 OG 图片（fallback，社交分享最常用这个）
      settingsObj.wpseo_social = settingsObj.wpseo_social || {};
      settingsObj.wpseo_social.og_default_image = logo.url || `${location.origin}/wp-content/uploads/logo.png`;
      settingsObj.wpseo_social.og_default_image_id = logo.id || 0;

      uiLog("已设置首页 & 默认 OG 图片为 logo.png");

      // 6. 转成表单字符串（模仿 convert_wpseo_json_to_query_string）
      let formParts = [];
      function flatten(prefix, obj) {
        if (typeof obj === 'object' && obj !== null) {
          for (const [key, val] of Object.entries(obj)) {
            flatten(`${prefix}[${key}]`, val);
          }
        } else {
          const encodedKey = encodeURIComponent(prefix);
          const encodedVal = encodeURIComponent(String(obj));
          formParts.push(`${encodedKey}=${encodedVal}`);
        }
      }

      for (const [mainKey, mainVal] of Object.entries(settingsObj)) {
        flatten(mainKey, mainVal);
      }

      const queryString = formParts.join('&');

      // 7. 构建完整 POST body（模仿 Python replace）
      const postBody = `option_page=wpseo_page_settings&action=update&_wpnonce=${encodeURIComponent(yoastNonce)}&_wp_http_referer=${encodeURIComponent('/wp-admin/admin.php?page=wpseo_page_settings')}&${queryString}`;

      // 8. 提交
      const saveUrl = `${location.origin}/wp-admin/options.php`;
      uiLog("提交更新到 options.php...");

      const saveRes = await fetchWithTimeout(saveUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "Referer": settingsUrl,
          "Origin": location.origin
        },
        body: postBody
      }, 90000);

      if (!saveRes.ok) {
        const errText = await saveRes.text();
        throw new Error(`保存失败 HTTP ${saveRes.status}: ${errText.slice(0, 300)}`);
      }

      uiLog("全局首页标题 & 元描述更新成功！");

      uiLog("=== Yoast：完成 ===");
    }

    async function runRocket() {
      uiLog("=== WP Rocket：开始 ===");
      await maybeActivate("activate-wp-rocket", "wp-rocket/wp-rocket.php");

      const settingUrl = `${location.origin}/wp-admin/options-general.php?page=wprocket`;
      uiLog("拉取 WP Rocket 设置页...");
      const res = await fetchWithTimeout(settingUrl, { method: "GET" }, 60000);
      const text = await res.text();
      if (!res.ok) throw new Error(`无法访问 WP Rocket 设置页，HTTP ${res.status}`);

      const doc = new DOMParser().parseFromString(text, "text/html");
      const wpnonce = doc.querySelector("input#_wpnonce")?.value || "";
      const secret_key = doc.querySelector("input#secret_key")?.value || "";
      const minify_js_key = doc.querySelector("input#minify_js_key")?.value || "";
      const consumer_email = doc.querySelector("input#consumer_email")?.value || "";
      const consumer_key = doc.querySelector("input#consumer_key")?.value || "";
      const version = doc.querySelector("input#version")?.value || "";
      const minify_css_key = doc.querySelector("input#minify_css_key")?.value || "";
      const wplicense = doc.querySelector("input#license")?.value || "";

      if (!wpnonce) throw new Error("未获取到 WP Rocket _wpnonce（可能页面结构变了或权限不足）");

      const settingData = {
        option_page: "wprocket",
        action: "update",
        _wpnonce: wpnonce,
        _wp_http_referer: "/wp-admin/options-general.php?page=wprocket",
        "wp_rocket_settings[cache_mobile]": "1",
        "wp_rocket_settings[do_caching_mobile_files]": "1",
        "wp_rocket_settings[purge_cron_interval]": "0",
        "wp_rocket_settings[purge_cron_unit]": "HOUR_IN_SECONDS",
        "wp_rocket_settings[minify_css]": "1",
        "wp_rocket_settings[exclude_css]": "",
        "wp_rocket_settings[optimize_css_delivery]": "1",
        "wp_rocket_settings[remove_unused_css_safelist]": "",
        "wp_rocket_settings[critical_css]": "",
        "wp_rocket_settings[minify_js]": "1",
        "wp_rocket_settings[exclude_inline_js]": "",
        "wp_rocket_settings[exclude_js]": "",
        "wp_rocket_settings[exclude_defer_js]": "",
        "wp_rocket_settings[delay_js_exclusions]": "",
        "wp_rocket_settings[lazyload]": "1",
        "wp_rocket_settings[exclude_lazyload]": "",
        "wp_rocket_settings[image_dimensions]": "1",
        "wp_rocket_settings[manual_preload]": "1",
        "wp_rocket_settings[preload_excluded_uri]": "",
        "wp_rocket_settings[preload_links]": "1",
        "wp_rocket_settings[dns_prefetch]": "",
        "wp_rocket_settings[preload_fonts]": "",
        "wp_rocket_settings[cache_reject_uri]": "",
        "wp_rocket_settings[cache_reject_cookies]": "",
        "wp_rocket_settings[cache_reject_ua]": "",
        "wp_rocket_settings[cache_purge_pages]": "",
        "wp_rocket_settings[cache_query_strings]": "",
        "wp_rocket_settings[automatic_cleanup_frequency]": "daily",
        "wp_rocket_settings[cdn_cnames][]": "",
        "wp_rocket_settings[cdn_zone][]": "all",
        "wp_rocket_settings[cdn_reject_files]": "",
        "wp_rocket_settings[heartbeat_admin_behavior]": "",
        "wp_rocket_settings[heartbeat_editor_behavior]": "",
        "wp_rocket_settings[heartbeat_site_behavior]": "",
        "wp_rocket_settings[cloudflare_api_key]": "",
        "wp_rocket_settings[cloudflare_email]": "",
        "wp_rocket_settings[cloudflare_zone_id]": "",
        "wp_rocket_settings[sucury_waf_api_key]": "",
        "wp_rocket_settings[consumer_key]": consumer_key,
        "wp_rocket_settings[consumer_email]": consumer_email,
        "wp_rocket_settings[secret_key]": secret_key,
        "wp_rocket_settings[license]": "",
        "wp_rocket_settings[secret_cache_key]": "",
        "wp_rocket_settings[minify_css_key]": minify_css_key,
        "wp_rocket_settings[minify_js_key]": minify_js_key,
        "wp_rocket_settings[version]": version,
        "wp_rocket_settings[cloudflare_old_settings]": "",
        "wp_rocket_settings[cache_ssl]": "1",
        "wp_rocket_settings[minify_google_fonts]": "0",
        "wp_rocket_settings[emoji]": "0",
        "wp_rocket_settings[remove_unused_css]": "1",
        "wp_rocket_settings[async_css]": "0",
        "wp_rocket_settings[async_css_mobile]": ""
      };

      const body = new URLSearchParams();
      for (const [k, v] of Object.entries(settingData)) body.append(k, String(v));

      uiLog("提交 WP Rocket 设置到 options.php ...");
      const res2 = await fetchWithTimeout(
        `${location.origin}/wp-admin/options.php`,
        {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" },
          body: body.toString()
        },
        60000
      );

      const text2 = await res2.text();
      if (!res2.ok) throw new Error(`WP Rocket 设置提交失败：HTTP ${res2.status} ${text2.slice(0, 200)}`);

      uiLog("=== WP Rocket：完成 ===");
    }

    // ---- 运行控制（避免并发）----
    let running = false;

    async function runJob(jobFn, name) {
      if (running) {
        uiLog("已有任务在执行中，请稍后");
        return;
      }
      running = true;
      btnYoast.disabled = true;
      btnRocket.disabled = true;
      try {
        await jobFn();
        uiLog(`${name}：任务结束：成功`);
      } catch (e) {
        uiLog(`${name}：任务结束：失败`);
        uiLog(`错误：${e && e.message ? e.message : String(e)}`);
      } finally {
        btnYoast.disabled = false;
        btnRocket.disabled = false;
        running = false;
      }
    }

    btnYoast.addEventListener("click", () => {
      uiLog("收到指令：Yoast 一键启用 + 配置");
      runJob(runYoast, "Yoast");
    });

    btnRocket.addEventListener("click", () => {
      uiLog("收到指令：WP Rocket 一键启用 + 配置");
      runJob(runRocket, "WP Rocket");
    });

    uiLog("面板已加载：仅在 /wp-admin/plugins.php 生效");
  }

  // =========================
  // 执行入口
  // =========================
  runLoginAutofill();
  runPluginsButtons();
})();
