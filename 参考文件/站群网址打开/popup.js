const form = document.getElementById("loginForm");
const input = document.getElementById("siteUrls");
const statusText = document.getElementById("status");
const submitButton = document.getElementById("submitButton");
const resultsList = document.getElementById("results");

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const siteUrlsText = input.value.trim();
  const siteUrls = parseSiteUrls(siteUrlsText);

  if (siteUrls.length === 0) {
    showStatus("请输入至少一个网站 URL 或域名。", "error");
    return;
  }

  submitButton.disabled = true;
  resultsList.replaceChildren();
  showStatus(`正在处理 ${siteUrls.length} 个域名...`, "");

  try {
    const response = await chrome.runtime.sendMessage({
      type: "LOGIN_AND_OPEN_MENU_BATCH",
      siteUrls
    });

    if (!response?.ok) {
      throw new Error(response?.error || "批量登录失败。");
    }

    renderResults(response.results || []);
    showStatus(`处理完成：成功 ${response.successCount} 个，失败 ${response.failureCount} 个。`, response.failureCount ? "error" : "success");
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    submitButton.disabled = false;
  }
});

function showStatus(message, type) {
  statusText.textContent = message;
  statusText.className = type;
}

function parseSiteUrls(value) {
  return [...new Set(value.split(/[\n,，\s]+/).map((item) => item.trim()).filter(Boolean))];
}

function renderResults(results) {
  const fragment = document.createDocumentFragment();

  for (const result of results) {
    const item = document.createElement("li");
    item.className = result.ok ? "success" : "error";
    item.textContent = result.ok ? `${result.input}：已打开` : `${result.input}：${result.error}`;
    fragment.appendChild(item);
  }

  resultsList.replaceChildren(fragment);
}
