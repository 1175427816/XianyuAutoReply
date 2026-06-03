const state = {
  summary: null,
  items: [],
  rules: [],
  captured: [],
  runtimeStatus: null,
  runtimeLogs: [],
  verificationEvents: [],
  activeTab: "rules",
};

const $ = (id) => document.getElementById(id);

function params(extra = {}) {
  const query = new URLSearchParams();
  const start = $("startDate").value;
  const end = $("endDate").value;
  const search = $("searchInput").value.trim();
  if (start) query.set("start", start);
  if (end) query.set("end", end);
  if (search) query.set("query", search);
  Object.entries(extra).forEach(([key, value]) => {
    if (value) query.set(key, value);
  });
  const text = query.toString();
  return text ? `?${text}` : "";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `请求失败: ${response.status}`);
  }
  return payload;
}

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 2600);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function number(value) {
  return Number(value || 0).toLocaleString("zh-CN");
}

function displayValue(value) {
  return value === null || value === undefined || value === "" ? "-" : escapeHtml(value);
}

function stateText(state) {
  const labels = {
    running: "运行中",
    stopped: "已停止",
    restarting: "重启中",
    waiting_verification: "等待验证",
    error: "异常",
  };
  return labels[state] || state || "未知";
}

function stateClass(state) {
  if (state === "running") return "ok";
  if (state === "waiting_verification" || state === "restarting") return "warn";
  return "off";
}

function renderStats() {
  const totals = state.summary?.totals || {};
  const stats = [
    ["累计自动回复", totals.assistant_replies],
    ["累计验证次数", totals.verifications],
    ["累计已处理消息", totals.processed_messages],
    ["累计用户消息", totals.user_messages],
    ["涉及会话", totals.chats],
    ["未分配图片", totals.unassigned_images],
  ];
  $("stats").innerHTML = stats
    .map(
      ([label, value]) => `
        <div class="stat">
          <div class="label">${label}</div>
          <div class="value">${number(value)}</div>
        </div>
      `
    )
    .join("");
}

function renderDaily() {
  const rows = [...(state.summary?.daily || [])].reverse();
  $("dailyRows").innerHTML =
    rows
      .map(
        (row) => `
          <tr>
            <td>${escapeHtml(row.day)}</td>
            <td>${number(row.assistant_replies)}</td>
            <td>${number(row.user_messages)}</td>
            <td>${number(row.processed_messages)}</td>
            <td>${number(row.verifications)}</td>
            <td>${number(row.chats)}</td>
            <td>${number(row.items)}</td>
          </tr>
        `
      )
      .join("") || `<tr><td colspan="7" class="empty">没有符合条件的数据</td></tr>`;
}

function renderRuntime() {
  const status = state.runtimeStatus || {};
  const runtimeBadge = $("runtimeBadge");
  runtimeBadge.textContent = stateText(status.state);
  runtimeBadge.className = `badge ${stateClass(status.state)}`;

  $("runtimeDetails").innerHTML = `
    <div class="runtime-kv"><strong>Monitor</strong><span>${status.monitor_running ? "运行中" : "未运行"} · PID ${displayValue(status.monitor_pid)}</span></div>
    <div class="runtime-kv"><strong>主程序</strong><span>${status.main_running ? "运行中" : "未运行"} · PID ${displayValue(status.main_pid)}</span></div>
    <div class="runtime-kv"><strong>最近启动</strong><span>${displayValue(status.last_start_at)}</span></div>
    <div class="runtime-kv"><strong>最近重启</strong><span>${displayValue(status.last_restart_at)}</span></div>
    <div class="runtime-kv"><strong>最近错误</strong><span>${displayValue(status.last_error)}</span></div>
  `;

  const verificationBadge = $("verificationBadge");
  verificationBadge.textContent = status.verification_required ? "需要处理" : "无验证";
  verificationBadge.className = `badge ${status.verification_required ? "warn" : "ok"}`;
  const event = status.last_verification_event || {};
  $("verificationDetails").innerHTML = `
    <div class="runtime-kv"><strong>状态</strong><span>${status.verification_required ? "等待完成验证或刷新 Cookie" : "当前无需验证"}</span></div>
    <div class="runtime-kv"><strong>原因</strong><span>${displayValue(status.verification_reason)}</span></div>
    <div class="runtime-kv"><strong>Cookie 刷新</strong><span>${displayValue(status.last_cookie_refresh_at)}</span></div>
    <div class="runtime-kv"><strong>最近事件</strong><span>${displayValue(event.action)} ${displayValue(event.result)} ${displayValue(event.time)}</span></div>
  `;
}

function renderLogs() {
  const logs = [...(state.runtimeLogs || [])].slice(-80).reverse();
  const verificationEvents = [...(state.verificationEvents || [])].slice(-20).reverse();
  const lines = [
    ...verificationEvents.map((entry) => ({
      time: entry.time,
      event: `verification:${entry.action || ""}`,
      message: `${entry.result || ""} ${entry.reason || entry.message || ""}`.trim(),
    })),
    ...logs,
  ].slice(0, 120);

  $("runtimeLogs").innerHTML =
    lines
      .map(
        (entry) =>
          `<div class="log-line">${escapeHtml(entry.time || "")} [${escapeHtml(entry.event || entry.level || "log")}] ${escapeHtml(entry.message || "")}</div>`
      )
      .join("") || `<div class="log-line">暂无运行日志</div>`;
}

function itemTitle(itemId) {
  const item = state.items.find((entry) => entry.item_id === itemId);
  return item ? item.title : itemId;
}

function itemType(itemId) {
  const item = state.items.find((entry) => entry.item_id === itemId);
  return item ? item.product_type : "";
}

function productTypes() {
  return [...new Set(state.items.map((item) => item.product_type || "其他商品"))].sort((a, b) =>
    a.localeCompare(b, "zh-CN")
  );
}

function renderProductSelectors(selectedIds = [], preferredType = "") {
  const typeSelect = $("ruleProductType");
  const itemSelect = $("ruleItemIds");
  const selected = new Set(selectedIds.map(String));
  const selectedTypes = [...new Set(selectedIds.map((id) => itemType(String(id))).filter(Boolean))];
  const firstSelectedType = selectedTypes[0] || "";
  const currentType =
    preferredType ||
    (selectedTypes.length > 1 ? "__all__" : "") ||
    typeSelect.value ||
    firstSelectedType ||
    productTypes()[0] ||
    "__all__";

  const types = [["__all__", "全部类型"], ...productTypes().map((type) => [type, type])];
  typeSelect.innerHTML = types
    .map(
      ([value, label]) =>
        `<option value="${escapeHtml(value)}"${value === currentType ? " selected" : ""}>${escapeHtml(label)}</option>`
    )
    .join("");

  const filteredItems =
    currentType === "__all__"
      ? state.items
      : state.items.filter((item) => (item.product_type || "其他商品") === currentType);
  itemSelect.innerHTML = filteredItems
    .map((item) => {
      const label = `${item.title} · ${item.item_id}`;
      return `<option value="${escapeHtml(item.item_id)}"${selected.has(item.item_id) ? " selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");

  if (!filteredItems.length) {
    itemSelect.innerHTML = `<option value="">该类型暂无商品</option>`;
  }
}

function imagePreview(url, label = "图片预览") {
  if (!url) {
    return `<div class="thumb placeholder">无图</div>`;
  }
  const safeUrl = escapeHtml(url);
  const safeLabel = escapeHtml(label);
  return `
    <a class="thumb" href="${safeUrl}" target="_blank" rel="noreferrer" title="打开原图">
      <img src="${safeUrl}" alt="${safeLabel}" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest('.thumb').classList.add('broken'); this.remove();">
      <span>预览失败</span>
    </a>
  `;
}

function renderRules() {
  const list = $("rulesList");
  if (!state.rules.length) {
    list.innerHTML = `<div class="empty">没有符合条件的图片回复规则</div>`;
    return;
  }
  list.innerHTML = state.rules
    .map((rule) => {
      const urls = (rule.images || []).map((image) => image.url);
      const itemNames = (rule.item_ids || []).map((id) => `${id} · ${itemTitle(id)}`).join("<br>");
      return `
        <article class="row image-row">
          ${imagePreview(urls[0], rule.name || "规则图片预览")}
          <div>
            <div class="row-title">${escapeHtml(rule.name || "未命名规则")}</div>
            <div class="meta">${itemNames || (rule.default ? "默认兜底规则" : "未绑定商品")}</div>
            <div class="meta">关键词：${escapeHtml((rule.keywords || []).join("，"))}</div>
          </div>
          <div>
            <span class="badge ${rule.enabled ? "ok" : "off"}">${rule.enabled ? "已启用" : "已禁用"}</span>
            <span class="badge">${escapeHtml(rule.match || "contains")}</span>
            <div class="meta">${escapeHtml(rule.text || "无回复文案")}</div>
            <div class="url" title="${escapeHtml(urls.join("\n"))}">${escapeHtml(urls[0] || "无图片 URL")}</div>
          </div>
          <div class="row-actions">
            <button data-action="toggle" data-id="${rule.id}">${rule.enabled ? "禁用" : "启用"}</button>
            <button data-action="edit" data-id="${rule.id}" class="primary">编辑</button>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderCaptured() {
  const list = $("capturedList");
  if (!state.captured.length) {
    list.innerHTML = `<div class="empty">没有符合条件的捕获图片</div>`;
    return;
  }
  list.innerHTML = state.captured
    .map(
      (image) => `
        <article class="row image-row">
          ${imagePreview(image.url, `捕获图片 ${image.item_id || ""}`)}
          <div>
            <div class="row-title">${escapeHtml(image.item_id || "未识别商品")}</div>
            <div class="meta">${escapeHtml(itemTitle(image.item_id))}</div>
            <div class="meta">捕获时间：${escapeHtml(image.captured_at || "-")}</div>
          </div>
          <div>
            <span class="badge ${image.assigned ? "ok" : "warn"}">${image.assigned ? "已分配" : "未分配"}</span>
            <span class="badge">${escapeHtml(`${image.width || 0}x${image.height || 0}`)}</span>
            <div class="url" title="${escapeHtml(image.url)}">${escapeHtml(image.url)}</div>
            <div class="inline-actions">
              <button data-action="deleteCapture" data-url="${escapeHtml(image.url)}" class="danger">删除这张捕获图</button>
            </div>
          </div>
          <div class="row-actions">
            <button data-action="assign" data-url="${escapeHtml(image.url)}" data-item="${escapeHtml(image.item_id)}" data-width="${escapeHtml(image.width)}" data-height="${escapeHtml(image.height)}" data-type="${escapeHtml(image.type)}" class="primary">创建规则</button>
          </div>
        </article>
      `
    )
    .join("");
}

function renderItems() {
  const list = $("itemsList");
  if (!state.items.length) {
    list.innerHTML = `<div class="empty">没有符合条件的商品缓存</div>`;
    return;
  }
  list.innerHTML = state.items
    .map(
      (item) => `
        <article class="row">
          <div>
            <div class="row-title">${escapeHtml(item.title)}</div>
            <div class="meta">类型：${escapeHtml(item.product_type || "其他商品")} · 商品 ID：${escapeHtml(item.item_id)} · 价格：${item.price ?? "-"}</div>
          </div>
          <div>
            <div class="meta">${escapeHtml(item.description_short || "无描述缓存")}</div>
            <div class="meta">更新时间：${escapeHtml(item.last_updated || "-")}</div>
          </div>
          <div class="row-actions">
            <button data-action="itemRule" data-item="${escapeHtml(item.item_id)}">新增规则</button>
          </div>
        </article>
      `
    )
    .join("");
}

function renderAll() {
  renderRuntime();
  renderLogs();
  renderStats();
  renderDaily();
  renderRules();
  renderCaptured();
  renderItems();
}

async function loadData() {
  const ruleStatus = $("ruleStatus").value;
  const captureStatus = $("captureStatus").value;
  const [summary, items, rules, captured, runtimeStatus, runtimeLogs] = await Promise.all([
    api(`/api/summary${params()}`),
    api(`/api/items${params()}`),
    api(`/api/image-rules${params({ status: ruleStatus })}`),
    api(`/api/captured-images${params({ status: captureStatus })}`),
    api("/api/runtime/status"),
    api("/api/runtime/logs?limit=100"),
  ]);
  state.summary = summary;
  state.items = items.items || [];
  state.rules = rules.rules || [];
  state.captured = captured.images || [];
  state.runtimeStatus = runtimeStatus.status || {};
  state.runtimeLogs = runtimeLogs.logs || [];
  state.verificationEvents = runtimeLogs.verification_events || [];
  renderAll();
}

async function runtimeAction(path, message) {
  const result = await api(path, { method: "POST", body: "{}" });
  toast(result.message || message);
  await loadData();
}

function openRuleDialog(rule = null, seed = {}) {
  $("formError").textContent = "";
  $("ruleId").value = rule?.id ?? "";
  $("dialogTitle").textContent = rule ? "编辑图片回复规则" : "新增图片回复规则";
  $("ruleName").value = rule?.name ?? seed.name ?? "商品图片自动回复";
  const selectedItemIds = rule?.item_ids ?? (seed.item_id ? [seed.item_id] : []);
  renderProductSelectors(selectedItemIds);
  $("ruleKeywords").value = (rule?.keywords ?? ["颜色", "色卡", "有什么颜色", "可选颜色"]).join(",");
  $("ruleMatch").value = rule?.match ?? "contains";
  $("ruleText").value = rule?.text ?? "颜色可以参考这张图。";
  $("ruleEnabled").checked = rule ? !!rule.enabled : !!seed.enabled;
  $("ruleDefault").checked = rule ? !!rule.default : false;
  $("ruleImages").value = (rule?.images ?? (seed.url ? [{ url: seed.url }] : []))
    .map((image) => image.url)
    .join("\n");
  $("deleteRuleBtn").classList.toggle("hidden", !rule);
  $("ruleDialog").showModal();
}

function collectRulePayload() {
  const selectedItemIds = [...$("ruleItemIds").selectedOptions]
    .map((option) => option.value)
    .filter(Boolean);
  const images = $("ruleImages")
    .value.split(/\n|,/)
    .map((url) => url.trim())
    .filter(Boolean)
    .map((url) => ({ url }));
  return {
    name: $("ruleName").value.trim(),
    item_ids: selectedItemIds,
    keywords: $("ruleKeywords").value,
    match: $("ruleMatch").value,
    text: $("ruleText").value,
    enabled: $("ruleEnabled").checked,
    default: $("ruleDefault").checked,
    images,
  };
}

function setActiveTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node.dataset.tab === tab));
  $("rulesTab").classList.toggle("hidden", tab !== "rules");
  $("capturedTab").classList.toggle("hidden", tab !== "captured");
  $("itemsTab").classList.toggle("hidden", tab !== "items");
}

document.addEventListener("click", async (event) => {
  const action = event.target?.dataset?.action;
  if (!action) return;
  try {
    if (action === "edit") {
      const rule = state.rules.find((entry) => entry.id === event.target.dataset.id);
      openRuleDialog(rule);
    }
    if (action === "toggle") {
      const rule = state.rules.find((entry) => entry.id === event.target.dataset.id);
      await api(`/api/image-rules/${rule.id}/enabled`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !rule.enabled }),
      });
      toast("规则状态已更新");
      await loadData();
    }
    if (action === "assign") {
      openRuleDialog(null, {
        url: event.target.dataset.url,
        item_id: event.target.dataset.item,
        enabled: true,
      });
    }
    if (action === "itemRule") {
      openRuleDialog(null, {
        item_id: event.target.dataset.item,
        enabled: false,
      });
    }
    if (action === "deleteCapture") {
      const url = event.target.dataset.url;
      if (!confirm("确认删除这张捕获图片记录？已创建的图片回复规则不会被删除。")) return;
      await api("/api/captured-images", {
        method: "DELETE",
        body: JSON.stringify({ url }),
      });
      toast("捕获图片已删除");
      await loadData();
    }
  } catch (error) {
    toast(error.message);
  }
});

$("ruleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("formError").textContent = "";
  const id = $("ruleId").value;
  try {
    await api(id ? `/api/image-rules/${id}` : "/api/image-rules", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(collectRulePayload()),
    });
    $("ruleDialog").close();
    toast("规则已保存");
    await loadData();
  } catch (error) {
    $("formError").textContent = error.message;
  }
});

$("deleteRuleBtn").addEventListener("click", async () => {
  const id = $("ruleId").value;
  if (!id || !confirm("确认删除这条图片回复规则？")) return;
  try {
    await api(`/api/image-rules/${id}`, { method: "DELETE" });
    $("ruleDialog").close();
    toast("规则已删除");
    await loadData();
  } catch (error) {
    $("formError").textContent = error.message;
  }
});

$("closeDialog").addEventListener("click", () => $("ruleDialog").close());
$("cancelRule").addEventListener("click", () => $("ruleDialog").close());
$("newRuleBtn").addEventListener("click", () => openRuleDialog(null, { enabled: false }));
$("refreshBtn").addEventListener("click", loadData);
$("refreshLogsBtn").addEventListener("click", loadData);
$("startMonitorBtn").addEventListener("click", () => runtimeAction("/api/runtime/start", "启动请求已发送"));
$("stopMonitorBtn").addEventListener("click", async () => {
  if (!confirm("确认停止自动回复监控？")) return;
  await runtimeAction("/api/runtime/stop", "停止请求已发送");
});
$("restartMonitorBtn").addEventListener("click", () => runtimeAction("/api/runtime/restart", "重启请求已发送"));
$("openVerificationBtn").addEventListener("click", () => runtimeAction("/api/verification/open-browser", "验证页面已打开"));
$("refreshCookieBtn").addEventListener("click", () => runtimeAction("/api/verification/refresh-cookie", "Cookie 刷新请求已发送"));
$("applyFilters").addEventListener("click", loadData);
$("clearFilters").addEventListener("click", () => {
  $("startDate").value = "";
  $("endDate").value = "";
  $("searchInput").value = "";
  $("ruleStatus").value = "";
  $("captureStatus").value = "";
  loadData();
});
$("ruleStatus").addEventListener("change", loadData);
$("captureStatus").addEventListener("change", loadData);
$("ruleProductType").addEventListener("change", () => renderProductSelectors([], $("ruleProductType").value));
document.querySelectorAll(".tab").forEach((node) => {
  node.addEventListener("click", () => setActiveTab(node.dataset.tab));
});

loadData().catch((error) => toast(error.message));
