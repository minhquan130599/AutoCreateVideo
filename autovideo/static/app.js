"use strict";
const $ = (id) => document.getElementById(id);
let settings = {};
let products = [];
let accounts = [];
const selected = new Set(JSON.parse(localStorageGet("acv-selected") || "[]"));
let lastState = null;

function localStorageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function localStorageSet(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } }

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || ("HTTP " + response.status));
  return data;
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), 5000);
}

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// ------------------------------------------------------------ settings
function fillSettings() {
  document.querySelectorAll("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    if (el.type === "checkbox") el.checked = Boolean(settings[key]);
    else if (key === "vs_api_key") {
      el.value = "";
      el.placeholder = settings.vs_api_key_set
        ? "đã lưu " + (settings.vs_api_key_hint || "") + " — để trống = giữ nguyên"
        : "chưa có key (VideoScript sẽ dùng OPENCODE_API_KEY trong .env nếu có)";
    } else el.value = settings[key] ?? "";
  });
  updateImageHint();
  renderAssign();
}

let saveTimer = null;
function scheduleSave() {
  $("saveState").textContent = "Đang lưu…";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveSettings, 500);
}

async function saveSettings() {
  const body = {};
  document.querySelectorAll("[data-key]").forEach((el) => {
    const key = el.dataset.key;
    body[key] = el.type === "checkbox" ? el.checked
      : el.type === "number" ? Number(el.value) : el.value;
  });
  try {
    const previousDirs = [settings.products_dir, settings.muse_accounts_dir, settings.videoscript_url];
    settings = await api("/api/settings", {
      method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    });
    $("saveState").textContent = "Đã lưu cấu hình";
    const keyInput = document.querySelector('[data-key="vs_api_key"]');
    if (keyInput.value) fillSettings();
    if (previousDirs[0] !== settings.products_dir) loadProducts();
    if (previousDirs[1] !== settings.muse_accounts_dir) loadAccounts();
    if (previousDirs[2] !== settings.videoscript_url) loadVsOptions();
    renderAssign();
  } catch (error) {
    $("saveState").textContent = "Lỗi lưu: " + error.message;
  }
}

function updateImageHint() {
  const scenes = Number(document.querySelector('[data-key="vs_num_scenes"]').value) || 5;
  if (document.querySelector('[data-key="skip_images"]').checked) {
    $("imageHint").textContent = "→ Chế độ không tạo ảnh: bỏ qua bước ảnh, Muse dựng video thẳng từ ảnh sản phẩm gốc.";
  } else $("imageHint").textContent = "→ Mỗi kịch bản cần " + (scenes + 1) +
    " ảnh (" + scenes + " cảnh + ảnh mở thùng 1B). Muse làm tối đa 4 ảnh/lượt, tool sẽ tự gửi tiếp cho đủ.";
  const duration = Number(document.querySelector('[data-key="vs_video_duration"]').value) || 15;
  const clipSec = Number(document.querySelector('[data-key="vs_muse_clip_seconds"]').value) || 10;
  // Mirrors VideoScript: the video is cut into blocks of this length, each block is one
  // 10s Muse generation (used in full), the last shorter block keeps only its first seconds.
  const mode = document.querySelector('[data-key="vs_muse_mode"]').value;
  if (mode === "shot") {
    $("clipHint").textContent = "→ " + scenes + " lượt tạo video/kịch bản (mỗi cảnh 1 lượt, bắt đầu từ đúng ảnh của cảnh đó; " +
      "cảnh mở thùng neo cả ảnh 1A → 1B). Giữ sản phẩm sát ảnh nhất nhưng tốn lượt tạo hơn.";
    return;
  }
  const block = Math.min(10, Math.max(3, clipSec));
  const parts = [];
  for (let t = 0; t < duration - 1e-6; t += block) parts.push(Math.min(block, duration - t));
  $("clipHint").textContent = "→ " + parts.length + " lượt tạo video/kịch bản: " +
    parts.map((p, i) => "clip " + (i + 1) + (p >= 9.95 ? " trọn 10s" : " lấy " + p + "s đầu")).join(", ") +
    ". Không tăng tốc; các cảnh theo đúng mốc giây của prompt Grok, mở thùng 5–6s đầu video.";
}

document.querySelectorAll("[data-key]").forEach((el) => {
  el.addEventListener(el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input", () => {
    if (["vs_num_scenes", "vs_video_duration", "vs_muse_clip_seconds", "vs_muse_mode", "skip_images"].includes(el.dataset.key)) updateImageHint();
    scheduleSave();
  });
});

async function loadVsOptions() {
  try {
    const data = await api("/api/videoscript/options");
    $("vsStatus").innerHTML = "";
    $("vsStatus").append(data.online
      ? h("span", {class: "tag ok"}, "VideoScript đang chạy")
      : h("span", {class: "tag bad"}, "Không kết nối được VideoScript — hãy chạy node server.js"));
    const bg = $("bgSelect");
    bg.replaceChildren(h("option", {value: "auto"}, "Tự động"));
    for (const item of data.backgrounds || []) {
      bg.append(h("option", {value: item.id}, (item.icon ? item.icon + " " : "") + (item.theme_name || item.id)));
    }
    const bag = $("bagSelect");
    bag.replaceChildren(h("option", {value: "random"}, "Ngẫu nhiên"));
    for (const item of data.bag_shapes || []) bag.append(h("option", {value: item.id}, item.name_vi || item.id));
    bg.value = settings.vs_background_option || "auto";
    bag.value = settings.vs_packaging_shape || "random";
  } catch (error) {
    $("vsStatus").textContent = error.message;
  }
}

// ------------------------------------------------------------ products
async function loadProducts() {
  const data = await api("/api/products");
  products = data.products;
  $("productsDir").textContent = data.exists
    ? "Thư mục: " + data.dir + " · " + products.length + " sản phẩm"
    : "Không tìm thấy thư mục: " + data.dir;
  for (const name of [...selected]) if (!products.some(p => p.name === name)) selected.delete(name);
  renderProducts();
}

function renderProducts() {
  const grid = $("productGrid");
  grid.replaceChildren();
  for (const product of products) {
    const isSel = selected.has(product.name);
    const card = h("div", {class: "product" + (isSel ? " selected" : ""), title: product.name},
      h("input", {type: "checkbox", checked: isSel, disabled: !product.image_count}),
      product.thumb ? h("img", {src: product.thumb, loading: "lazy", alt: ""}) : h("div", {class: "pname"}, "(không có ảnh)"),
      h("div", {class: "pname"}, product.name + " · " + product.image_count + " ảnh"));
    card.addEventListener("click", () => {
      if (!product.image_count) return;
      if (selected.has(product.name)) selected.delete(product.name); else selected.add(product.name);
      localStorageSet("acv-selected", JSON.stringify([...selected]));
      renderProducts();
    });
    grid.append(card);
  }
  $("selCount").textContent = selected.size + "/" + products.length + " đã chọn";
}

$("selAll").addEventListener("click", () => {
  products.filter(p => p.image_count).forEach(p => selected.add(p.name));
  localStorageSet("acv-selected", JSON.stringify([...selected]));
  renderProducts();
});
$("selNone").addEventListener("click", () => {
  selected.clear();
  localStorageSet("acv-selected", "[]");
  renderProducts();
});
$("reloadProducts").addEventListener("click", loadProducts);
document.querySelectorAll('input[name="mode"]').forEach((el) => el.addEventListener("change", () => {
  const random = document.querySelector('input[name="mode"]:checked').value === "random";
  $("randomBox").classList.toggle("hidden", !random);
  $("manualBox").classList.toggle("hidden", random);
}));

// ------------------------------------------------------------ accounts
async function loadAccounts() {
  const data = await api("/api/accounts");
  accounts = data.accounts;
  const list = $("accountList");
  list.replaceChildren();
  if (!accounts.length) {
    list.append(h("p", {class: "small muted"}, "Chưa có tài khoản. Đăng nhập Muse trong MuseAI-API trước."));
  }
  for (const account of accounts) {
    const tag = account.usable ? h("span", {class: "tag ok"}, "Dùng được")
      : !account.enabled ? h("span", {class: "tag idle"}, "Đang tắt")
      : h("span", {class: "tag bad"}, "Chưa đăng nhập");
    list.append(h("div", {class: "account"}, h("span", {}, account.label), tag));
  }
  renderAssign();
}

function renderAssign() {
  const box = $("assignPreview");
  box.replaceChildren();
  const usable = accounts.filter(a => a.usable);
  const threads = Number(document.querySelector('[data-key="threads"]').value) || 1;
  if (!usable.length) return;
  const perAccount = new Map();
  for (let i = 0; i < threads; i++) {
    const account = usable[i % usable.length];
    perAccount.set(account.label, [...(perAccount.get(account.label) || []), i + 1]);
  }
  box.append(h("span", {}, "Phân luồng:"));
  for (const [label, ids] of perAccount) {
    box.append(h("span", {}, "• " + label + " → luồng " + ids.join(", ") +
      (ids.length > 1 ? " (" + ids.length + " đoạn chat song song)" : "")));
  }
}
$("reloadAccounts").addEventListener("click", loadAccounts);

document.querySelectorAll("[data-template]").forEach((button) => button.addEventListener("click", async () => {
  const key = button.dataset.template;
  let templates;
  try {
    templates = await api("/api/settings/templates");
  } catch (error) {
    return toast("Không lấy được mẫu (" + error.message + "). Hãy khởi động lại server AutoCreateVideo rồi F5.");
  }
  const field = document.querySelector('[data-key="' + key + '"]');
  if (field.value.trim() && !confirm("Thay nội dung hiện tại bằng mẫu khuyến nghị?")) return;
  field.value = templates[key];
  scheduleSave();
}));

// ------------------------------------------------------------ run control
$("btnStart").addEventListener("click", async () => {
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const body = mode === "random"
    ? {mode, count: Number($("randomCount").value) || 1}
    : {mode, products: [...selected]};
  if (mode === "manual" && !body.products.length) return toast("Hãy chọn ít nhất 1 sản phẩm.");
  clearTimeout(saveTimer);
  await saveSettings();
  try {
    render(await api("/api/run", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}));
  } catch (error) { toast(error.message); }
});
$("btnStop").addEventListener("click", async () => {
  if (!confirm("Dừng toàn bộ phiên đang chạy?")) return;
  try { render(await api("/api/run/stop", {method: "POST"})); } catch (error) { toast(error.message); }
});
$("btnDownloadAll").addEventListener("click", () => {
  const run = lastState && lastState.run;
  const done = run ? run.products.flatMap(p => p.variants).filter(v => v.video).length : 0;
  if (!done) return toast("Chưa có video nào hoàn tất để tải.");
  window.location.href = "/api/run/download";
});
$("btnRetry").addEventListener("click", async () => {
  clearTimeout(saveTimer);
  await saveSettings();
  try { render(await api("/api/run/retry", {method: "POST"})); } catch (error) { toast(error.message); }
});

// ------------------------------------------------------------ dashboard
const STAGE_TAG = {
  script_queued: ["Chờ kịch bản", "idle"], script: ["Đang viết kịch bản", "run"],
  scripted: ["Đã có kịch bản", "run"], producing: ["Đang sản xuất", "run"],
  done: ["Hoàn tất", "ok"], partial: ["Xong một phần", "warn"], failed: ["Lỗi", "bad"],
  queued: ["Chờ luồng", "idle"], images: ["Đang tạo ảnh", "run"], video: ["Đang tạo video", "run"],
  stopped: ["Đã dừng", "warn"],
};
const tag = (stage) => {
  const [label, cls] = STAGE_TAG[stage] || [stage, "idle"];
  return h("span", {class: "tag " + cls}, label);
};

function variantSteps(variant) {
  const noImages = Boolean(variant.skip_images);
  const order = noImages ? ["script", "video", "done"] : ["script", "images1", "images2", "video", "done"];
  let current;
  if (variant.stage === "queued") current = noImages ? "video" : "images1";
  else if (variant.stage === "images") current = variant.image_rounds > 1 ? "images2" : "images1";
  else if (variant.stage === "video") current = "video";
  else if (variant.stage === "done") current = "end";
  else current = noImages || variant.video ? "video" : variant.images.length >= variant.image_target ? "video"
    : variant.image_rounds > 1 ? "images2" : "images1";
  const failed = ["failed", "stopped"].includes(variant.stage);
  const idx = current === "end" ? order.length : order.indexOf(current);
  const labels = {
    script: "Kịch bản", images1: "Ảnh lượt 1",
    images2: "Ảnh lượt tiếp" + (variant.image_rounds > 1 ? " (" + (variant.image_rounds - 1) + ")" : ""),
    video: "Video", done: "Xong",
  };
  return h("div", {class: "steps"}, order.map((step, i) => {
    let cls = "stp";
    if (i < idx) cls += " done";
    else if (i === idx) cls += failed ? " fail" : (variant.stage === "queued" ? "" : " active");
    return h("span", {class: cls}, labels[step]);
  }));
}

const variantCache = new Map();
function variantNode(product, variant) {
  const sig = JSON.stringify([variant.stage, variant.message, variant.images.length, variant.video,
    variant.error, variant.logs.length, variant.worker, variant.muse_reply, (variant.clips || []).length, Boolean(variant.skip_images)]);
  const cached = variantCache.get(variant.id);
  if (cached && cached.sig === sig) return cached.node;
  const count = variant.images.length;
  const thumbs = h("div", {class: "thumbs"});
  const slotKeys = ["1A", "1B"];
  for (let i = 2; i < variant.image_target; i++) slotKeys.push(String(i));
  const byKey = new Map(variant.images.filter(img => img.key).map(img => [img.key, img]));
  const loose = variant.images.filter(img => !img.key);
  for (const key of slotKeys) {
    const img = byKey.get(key) || loose.shift();
    thumbs.append(img
      ? h("a", {href: img.url, target: "_blank", title: img.label || ("Ảnh " + key)},
          h("img", {src: img.url, loading: "lazy", alt: img.label || ("Ảnh " + key)}))
      : h("div", {class: "ph"}, "Ảnh " + key));
  }
  const node = h("div", {class: "variant"},
    h("div", {class: "vhead"},
      h("span", {class: "vtitle"}, variant.title),
      h("span", {},
        variant.worker ? h("span", {class: "tag idle"}, "Luồng " + variant.worker + " · " + variant.account_label) : null,
        " ", tag(variant.stage))),
    variant.concept ? h("div", {class: "small muted"}, variant.concept) : null,
    variantSteps(variant),
    h("div", {class: "vmsg"}, variant.message || ""),
    variant.skip_images ? h("div", {class: "small muted"}, "Không tạo ảnh — video tạo thẳng từ ảnh sản phẩm gốc") : null,
    variant.skip_images ? null : h("div", {class: "bar"}, h("i", {style: "width:" + Math.round(100 * Math.min(count, variant.image_target) / variant.image_target) + "%"})),
    variant.skip_images ? null : h("div", {class: "small muted"}, "Ảnh: " + count + "/" + variant.image_target),
    variant.skip_images ? null : thumbs,
    variant.video ? h("video", {src: variant.video, controls: true, preload: "metadata"}) : null,
    variant.video ? h("a", {href: variant.video, download: ""}, "Tải video ↓") : null,
    (variant.clips || []).length ? h("div", {class: "small muted"}, "Clip cảnh Muse đã trả: ",
      variant.clips.map((clip, i) => [i ? " · " : "", h("a", {href: clip.url, target: "_blank"},
        "clip " + (i + 1) + (clip.seconds ? " (" + Math.round(clip.seconds) + "s)" : ""))])) : null,
    variant.error ? h("div", {class: "verror"}, variant.error) : null,
    h("details", {class: "log"}, h("summary", {}, "Prompt & nhật ký"),
      h("pre", {}, "PROMPT ẢNH:\n" + variant.image_prompt + "\n\nPROMPT VIDEO:\n" + variant.video_prompt),
      variant.muse_reply ? h("pre", {}, "PHẢN HỒI MUSE GẦN NHẤT:\n" + variant.muse_reply) : null,
      h("pre", {}, (variant.logs || []).join("\n"))));
  variantCache.set(variant.id, {sig, node});
  return node;
}

function render(state) {
  lastState = state;
  const run = state.run;
  const pill = $("runPill");
  pill.textContent = state.running ? "Đang chạy" : run ? "Phiên " + run.id + " · đã dừng/xong" : "Chưa chạy";
  pill.className = "pill" + (state.running ? " running" : "");
  $("btnStart").disabled = state.running;
  $("btnStop").disabled = !state.running;
  $("btnRetry").disabled = state.running || !run;
  $("btnDownloadAll").disabled = !run || !run.products.some(p => p.variants.some(v => v.video));

  const workers = $("workers");
  workers.replaceChildren();
  for (const worker of state.workers || []) {
    workers.append(h("div", {class: "worker"},
      h("div", {class: "row-between"}, h("strong", {}, "Luồng " + worker.id),
        h("span", {class: "tag " + (worker.status === "busy" ? "run" : "idle")},
          worker.status === "busy" ? "Đang chạy" : worker.status === "idle" ? "Rảnh" : "Đã dừng")),
      h("div", {class: "muted"}, worker.account_label),
      h("div", {}, worker.current || "—"),
      h("div", {class: "muted"}, "Xong " + worker.done + " · Lỗi " + worker.failed)));
  }

  const box = $("products");
  $("emptyBoard").classList.toggle("hidden", Boolean(run));
  if (!run) { box.replaceChildren(); $("summary").replaceChildren(); return; }

  const variants = run.products.flatMap(p => p.variants);
  const count = (fn) => variants.filter(fn).length;
  $("summary").replaceChildren(
    h("div", {class: "stat"}, h("b", {}, run.products.length), "Sản phẩm"),
    h("div", {class: "stat"}, h("b", {}, variants.length), "Kịch bản / video"),
    h("div", {class: "stat"}, h("b", {}, count(v => ["images", "video"].includes(v.stage))), "Đang chạy"),
    h("div", {class: "stat"}, h("b", {}, count(v => v.stage === "done")), "Video xong"),
    h("div", {class: "stat"}, h("b", {}, count(v => ["failed", "stopped"].includes(v.stage)) +
      run.products.filter(p => p.stage === "failed" && !p.variants.length).length), "Lỗi / dừng"));

  const nodes = run.products.map((product) => {
    let entry = productCache.get(product.id);
    if (!entry) {
      entry = {head: h("div"), list: h("div")};
      entry.node = h("article", {class: "pcard"}, entry.head, entry.list);
      productCache.set(product.id, entry);
    }
    // replaceChildren() would render null as the text "null", so build via h().
    entry.head.replaceChildren(...h("div", {},
      h("div", {class: "phead"},
        h("h3", {}, product.name + (product.product_name ? " — " + product.product_name : "")),
        h("span", {}, product.scripts_url ? h("a", {href: product.scripts_url, target: "_blank", class: "small"}, "scripts.json") : null,
          product.variants.some(v => v.video) ? [" · ", h("a", {href: "/api/run/download?product=" + encodeURIComponent(product.name), class: "small"}, "Tải video sản phẩm ⬇")] : null,
          " ", tag(product.stage))),
      h("div", {class: "pmsg"}, product.message || ""),
      product.error ? h("div", {class: "verror"}, product.error) : null).childNodes);
    syncChildren(entry.list, product.variants.map((variant) => variantNode(product, variant)));
    return entry.node;
  });
  syncChildren(box, nodes);
}

const productCache = new Map();
// Only move/insert nodes that changed so playing <video> elements are not detached.
function syncChildren(parent, nodes) {
  nodes.forEach((node, i) => {
    if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null);
  });
  while (parent.children.length > nodes.length) parent.lastElementChild.remove();
}

async function poll() {
  try { render(await api("/api/state")); } catch { /* server restarting */ }
  setTimeout(poll, lastState && lastState.running ? 2000 : 5000);
}

(async function init() {
  settings = await api("/api/settings");
  fillSettings();
  await Promise.all([loadProducts(), loadAccounts(), loadVsOptions()]);
  poll();
})();
