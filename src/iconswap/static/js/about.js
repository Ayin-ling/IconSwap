/* 「关于」弹窗：读取 /api/about，渲染版本与第三方组件 */
(function () {
  "use strict";

  var open = document.getElementById("about-open");
  var modal = document.getElementById("about-modal");
  var close = document.getElementById("about-close");
  var versionEl = document.getElementById("about-version");
  var rowsEl = document.getElementById("about-rows");
  var noticeEl = document.getElementById("about-notice");

  if (!open || !modal) {
    return;
  }

  var loaded = false;

  function render(info) {
    versionEl.textContent =
      info.name + " v" + info.version + " · " + (info.platform || "");
    rowsEl.innerHTML = "";
    (info.components || []).forEach(function (item) {
      var tr = document.createElement("tr");

      var name = document.createElement("td");
      if (item.url) {
        var link = document.createElement("a");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = item.name;
        name.appendChild(link);
      } else {
        name.textContent = item.name;
      }

      var version = document.createElement("td");
      version.textContent = item.version || "—";

      var license = document.createElement("td");
      license.textContent = item.license || "—";

      var purpose = document.createElement("td");
      purpose.textContent = item.purpose || "";

      tr.appendChild(name);
      tr.appendChild(version);
      tr.appendChild(license);
      tr.appendChild(purpose);
      rowsEl.appendChild(tr);
    });
    noticeEl.textContent = info.notice || "";
  }

  function show() {
    modal.hidden = false;
    if (loaded) {
      return;
    }
    fetch("/api/about")
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (info) {
        loaded = true;
        render(info);
      })
      .catch(function (error) {
        versionEl.textContent = "读取版本信息失败：" + (error.message || error);
      });
  }

  function hide() {
    modal.hidden = true;
  }

  open.addEventListener("click", show);
  close.addEventListener("click", hide);
  modal.addEventListener("click", function (event) {
    if (event.target === modal) {
      hide();
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !modal.hidden) {
      hide();
    }
  });
})();
