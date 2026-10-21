(function () {
  "use strict";

  var manifestUrl = "/api/downloads/prospector/latest";
  var loading = document.getElementById("loading");
  var error = document.getElementById("error");
  var content = document.getElementById("content");
  var version = document.getElementById("version");
  var requirements = document.getElementById("requirements");
  var size = document.getElementById("size");
  var published = document.getElementById("published");
  var checksum = document.getElementById("sha256");
  var downloadLink = document.getElementById("download-link");
  var notes = document.getElementById("notes");
  var copyButton = document.getElementById("copy-sha");
  var checksumValue = "";

  function formatBytes(value) {
    if (!Number.isFinite(value) || value <= 0) return "--";
    var units = ["B", "KB", "MB", "GB"];
    var amount = value;
    var index = 0;
    while (amount >= 1024 && index < units.length - 1) {
      amount /= 1024;
      index += 1;
    }
    return amount.toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }

  function formatDate(value) {
    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value || "--";
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(parsed);
  }

  function showError(message) {
    loading.hidden = true;
    content.hidden = true;
    error.hidden = false;
    error.textContent = message || "客户端安装包暂不可用";
    version.textContent = "暂不可下载";
  }

  function render(data) {
    version.textContent = "版本 " + data.version;
    requirements.textContent = data.minimumWindows;
    size.textContent = formatBytes(data.sizeBytes);
    published.textContent = formatDate(data.publishedAt);
    checksumValue = data.sha256 || "";
    checksum.textContent = checksumValue;
    checksum.title = checksumValue;
    downloadLink.href = data.downloadUrl;
    downloadLink.setAttribute("download", data.fileName || "prospector-setup.exe");
    downloadLink.removeAttribute("aria-disabled");
    notes.replaceChildren();
    var items = Array.isArray(data.notes) && data.notes.length ? data.notes : ["当前版本包含常规修复与稳定性更新。"];
    items.forEach(function (note) {
      var item = document.createElement("li");
      item.textContent = note;
      notes.appendChild(item);
    });
    loading.hidden = true;
    error.hidden = true;
    content.hidden = false;
  }

  function fallbackCopy(value) {
    var textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    var copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_error) {
      copied = false;
    }
    textarea.remove();
    return copied;
  }

  async function copyChecksum() {
    if (!checksumValue) return;
    var copied = false;
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(checksumValue);
        copied = true;
      } catch (_error) {
        copied = false;
      }
    }
    if (!copied) copied = fallbackCopy(checksumValue);
    copyButton.textContent = copied ? "已复制" : "复制失败";
    window.setTimeout(function () { copyButton.textContent = "复制"; }, 1400);
  }

  async function load() {
    downloadLink.setAttribute("aria-disabled", "true");
    try {
      var response = await fetch(manifestUrl, { cache: "no-store", credentials: "same-origin" });
      var payload = await response.json();
      if (!response.ok || !payload.ok || !payload.data) {
        throw new Error(payload.error || "客户端安装包暂不可用");
      }
      render(payload.data);
    } catch (loadError) {
      showError(loadError instanceof Error ? loadError.message : "");
    }
  }

  copyButton.addEventListener("click", copyChecksum);
  void load();
}());

