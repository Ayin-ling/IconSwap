/* IconSwap 前端交互：选择图片 → 勾选尺寸 → 调用 /api/convert → 下载 ICO
   无第三方依赖，直接由浏览器加载。 */
(function () {
  "use strict";

  var ACCEPT_EXT = ["png", "jpg", "jpeg", "bmp", "webp", "gif", "tif", "tiff"];
  var ENDPOINT = "/api/convert";

  var el = {
    dropzone: document.getElementById("dropzone"),
    input: document.getElementById("file-input"),
    fileCard: document.getElementById("file-card"),
    filePreview: document.getElementById("file-preview"),
    fileName: document.getElementById("file-name"),
    fileInfo: document.getElementById("file-info"),
    fileRemove: document.getElementById("file-remove"),
    chips: document.getElementById("size-chips"),
    summary: document.getElementById("sizes-summary"),
    all: document.getElementById("sizes-all"),
    none: document.getElementById("sizes-none"),
    reset: document.getElementById("sizes-default"),
    custom: document.getElementById("custom-size"),
    customAdd: document.getElementById("custom-add"),
    convert: document.getElementById("convert"),
    status: document.getElementById("status"),
    alert: document.getElementById("alert"),
    result: document.getElementById("result"),
    resultPreview: document.getElementById("result-preview"),
    resultFile: document.getElementById("result-file"),
    resultSizes: document.getElementById("result-sizes"),
    resultBytes: document.getElementById("result-bytes"),
    download: document.getElementById("download"),
    removeBg: document.getElementById("remove-bg")
  };

  var MAX_BYTES = Number(document.body.dataset.maxBytes || 8 * 1024 * 1024);
  var MAX_MB = document.body.dataset.maxMb || "8";
  var DEFAULT_SIZES = Array.prototype.map.call(
    el.chips.querySelectorAll("input"),
    function (input) { return Number(input.value); }
  );

  var state = {
    file: null,
    previewUrl: null,
    resultUrl: null,
    busy: false
  };

  /* ---------------------------- 工具函数 ---------------------------- */

  function formatBytes(bytes) {
    if (bytes < 1024) { return bytes + " B"; }
    if (bytes < 1024 * 1024) { return (bytes / 1024).toFixed(1) + " KB"; }
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

  function extensionOf(name) {
    var index = name.lastIndexOf(".");
    return index < 0 ? "" : name.slice(index + 1).toLowerCase();
  }

  function showAlert(message) {
    el.alert.textContent = message;
    el.alert.hidden = false;
  }

  function clearAlert() {
    el.alert.hidden = true;
    el.alert.textContent = "";
  }

  function setStatus(message) {
    el.status.textContent = message;
  }

  function selectedSizes() {
    var sizes = [];
    Array.prototype.forEach.call(
      el.chips.querySelectorAll("input:checked"),
      function (input) { sizes.push(Number(input.value)); }
    );
    return sizes.sort(function (a, b) { return a - b; });
  }

  function syncState() {
    var sizes = selectedSizes();
    el.summary.textContent = sizes.length
      ? "已选 " + sizes.length + " 个尺寸：" + sizes.join(", ")
      : "至少选择一个尺寸";
    el.convert.disabled = state.busy || !state.file || sizes.length === 0;
  }

  /* ---------------------------- 选择文件 ---------------------------- */

  function revoke(url) { if (url) { URL.revokeObjectURL(url); } }

  function selectFile(file) {
    clearAlert();
    if (!file) { return; }

    var ext = extensionOf(file.name || "");
    if (ACCEPT_EXT.indexOf(ext) < 0) {
      showAlert("不支持的文件格式：" + (ext ? "." + ext : "未知") +
        "。请选择 " + ACCEPT_EXT.join(" / ") + " 格式的图片。");
      return;
    }
    if (file.size > MAX_BYTES) {
      showAlert("文件过大（" + formatBytes(file.size) + "），上限 " + MAX_MB + " MB。");
      return;
    }

    state.file = file;
    revoke(state.previewUrl);
    state.previewUrl = URL.createObjectURL(file);
    el.filePreview.src = state.previewUrl;
    el.fileName.textContent = file.name;
    el.fileCard.hidden = false;

    var info = formatBytes(file.size);
    var probe = new Image();
    probe.onload = function () {
      el.fileInfo.textContent = info + " · " + probe.naturalWidth + " × " + probe.naturalHeight +
        " px" + (probe.naturalWidth === probe.naturalHeight ? "" : "（非正方形，将居中补透明边）");
    };
    probe.onerror = function () { el.fileInfo.textContent = info; };
    probe.src = state.previewUrl;

    el.dropzone.hidden = true;
    el.result.hidden = true;
    setStatus("");
    syncState();
  }

  function clearFile() {
    revoke(state.previewUrl);
    state.previewUrl = null;
    state.file = null;
    el.filePreview.removeAttribute("src");
    el.fileCard.hidden = true;
    el.dropzone.hidden = false;
    el.input.value = "";
    el.result.hidden = true;
    setStatus("");
    clearAlert();
    syncState();
  }

  /* ---------------------------- 尺寸 chips ---------------------------- */

  function hasSize(size) {
    return Array.prototype.some.call(
      el.chips.querySelectorAll("input"),
      function (input) { return Number(input.value) === size; }
    );
  }

  function addSize(size) {
    if (hasSize(size)) { return; }
    var label = document.createElement("label");
    label.className = "chip";
    var input = document.createElement("input");
    input.type = "checkbox";
    input.name = "sizes";
    input.value = String(size);
    input.checked = true;
    var box = document.createElement("span");
    box.className = "chip__box";
    box.textContent = String(size);
    label.appendChild(input);
    label.appendChild(box);
    el.chips.appendChild(label);
  }

  function setAll(checked) {
    Array.prototype.forEach.call(
      el.chips.querySelectorAll("input"),
      function (input) { input.checked = checked; }
    );
    syncState();
  }

  function restoreDefaults() {
    Array.prototype.slice.call(el.chips.querySelectorAll("input")).forEach(function (input) {
      if (DEFAULT_SIZES.indexOf(Number(input.value)) < 0) {
        input.parentNode.remove();
      } else {
        input.checked = true;
      }
    });
    DEFAULT_SIZES.forEach(addSize);
    syncState();
  }

  function addCustom() {
    clearAlert();
    var text = (el.custom.value || "").trim();
    if (!text) { return; }
    var parts = text.split(/[,\s]+/).filter(Boolean);
    var added = [];
    for (var i = 0; i < parts.length; i += 1) {
      var value = Number(parts[i]);
      if (!/^\d+$/.test(parts[i]) || !isFinite(value) || value < 1 || value > 256) {
        showAlert("自定义尺寸必须是 1–256 之间的整数，收到：“" + parts[i] + "”。");
        return;
      }
      addSize(value);
      added.push(value);
    }
    el.custom.value = "";
    setStatus("已添加尺寸：" + added.join(", "));
    syncState();
  }

  /* ---------------------------- 上传转换 ---------------------------- */

  function filenameFromDisposition(header, fallback) {
    if (!header) { return fallback; }
    var utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
    if (utf8) {
      try { return decodeURIComponent(utf8[1].trim()); } catch (err) { /* 忽略 */ }
    }
    var plain = /filename="?([^";]+)"?/i.exec(header);
    return plain ? plain[1].trim() : fallback;
  }

  function convert() {
    if (!state.file || state.busy) { return; }
    var sizes = selectedSizes();
    if (!sizes.length) {
      showAlert("请至少选择一个图标尺寸。");
      return;
    }

    clearAlert();
    state.busy = true;
    el.convert.disabled = true;
    el.convert.classList.add("is-busy");
    el.result.hidden = true;
    setStatus("正在生成，请稍候…");

    var form = new FormData();
    form.append("file", state.file, state.file.name);
    form.append("sizes", sizes.join(","));
    // 去白边：像素级处理在服务端线程完成，前端只显示"处理中"，界面不会卡
    var removeBg = !!(el.removeBg && el.removeBg.checked);
    form.append("remove_bg", removeBg ? "1" : "0");

    fetch(ENDPOINT, { method: "POST", body: form })
      .then(function (response) {
        if (!response.ok) {
          return response.json().catch(function () { return {}; }).then(function (payload) {
            throw new Error(payload.error || ("请求失败（HTTP " + response.status + "）"));
          });
        }
        var disposition = response.headers.get("Content-Disposition");
        var sizeHeader = response.headers.get("X-Icon-Sizes") || sizes.join(",");
        var bgRemoved = response.headers.get("X-Background-Removed") === "1";
        var bgRatio = response.headers.get("X-Background-Ratio") || "";
        return response.blob().then(function (blob) {
          return {
            blob: blob,
            disposition: disposition,
            sizesHeader: sizeHeader,
            bgRemoved: bgRemoved,
            bgRatio: bgRatio
          };
        });
      })
      .then(function (result) {
        revoke(state.resultUrl);
        state.resultUrl = URL.createObjectURL(result.blob);
        var name = filenameFromDisposition(result.disposition, "icon.ico");

        el.download.href = state.resultUrl;
        el.download.download = name;
        el.resultPreview.src = state.resultUrl;
        el.resultFile.textContent = name;
        el.resultSizes.textContent = "包含尺寸：" + result.sizesHeader.split(",").join(" / ") + " px";
        var bytesText = "文件大小：" + formatBytes(result.blob.size);
        if (result.bgRemoved) {
          bytesText += " · 已去除背景";
        }
        el.resultBytes.textContent = bytesText;
        el.result.hidden = false;
        setStatus("生成完成，可下载。");
        el.result.scrollIntoView({ behavior: "smooth", block: "nearest" });
      })
      .catch(function (error) {
        showAlert(error && error.message ? error.message : "生成失败，请稍后重试。");
        setStatus("");
      })
      .then(function () {
        state.busy = false;
        el.convert.classList.remove("is-busy");
        syncState();
      });
  }

  /* ---------------------------- 事件绑定 ---------------------------- */

  el.dropzone.addEventListener("click", function () { el.input.click(); });

  el.dropzone.addEventListener("keydown", function (event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      el.input.click();
    }
  });

  ["dragenter", "dragover"].forEach(function (type) {
    el.dropzone.addEventListener(type, function (event) {
      event.preventDefault();
      el.dropzone.classList.add("is-dragover");
    });
  });

  ["dragleave", "dragend"].forEach(function (type) {
    el.dropzone.addEventListener(type, function () {
      el.dropzone.classList.remove("is-dragover");
    });
  });

  el.dropzone.addEventListener("drop", function (event) {
    event.preventDefault();
    el.dropzone.classList.remove("is-dragover");
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) { selectFile(files[0]); }
  });

  el.input.addEventListener("change", function () {
    if (el.input.files && el.input.files.length) { selectFile(el.input.files[0]); }
  });

  el.fileRemove.addEventListener("click", clearFile);
  el.convert.addEventListener("click", convert);
  el.all.addEventListener("click", function () { setAll(true); });
  el.none.addEventListener("click", function () { setAll(false); });
  el.reset.addEventListener("click", restoreDefaults);
  el.customAdd.addEventListener("click", addCustom);
  el.custom.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      addCustom();
    }
  });

  el.chips.addEventListener("change", syncState);

  document.addEventListener("paste", function (event) {
    var items = event.clipboardData && event.clipboardData.items;
    if (!items) { return; }
    for (var i = 0; i < items.length; i += 1) {
      if (items[i].type && items[i].type.indexOf("image/") === 0) {
        var file = items[i].getAsFile();
        if (file) {
          event.preventDefault();
          selectFile(file);
          return;
        }
      }
    }
  });

  // 拖到页面其他地方时不要让浏览器直接打开图片
  ["dragover", "drop"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      if (!el.dropzone.contains(event.target)) { event.preventDefault(); }
    });
  });

  syncState();
})();
