/* IconSwap 一键智能替换（单一面板）
   ---------------------------------------------------------------
   一个全局图标源 + 一个目标列表 + 一个按钮。

   目标可以来自「扫描文件夹」或「多选文件」，合并进同一个列表；
   点「🚀 一键智能替换」后由后台按后缀自动分流：

     .exe -> rcedit 直接改内嵌图标
             失败（签名 / 自解压包 / 正在运行）自动降级：
             桌面生成「原名 - 快捷方式.lnk」并替换其图标
     .lnk -> 不看 TargetPath，直接把 IconLocation 覆盖成 <图标>,0
             保存后回读校验，绝不新建任何快捷方式
     其它 -> 记一行「跳过」，绝不中断整批任务

   两套旧面板已合并：整页只有一个 Panel（状态 / 日志 / 忙碌锁）。 */
(function () {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function jsonOrThrow(response) {
    return response
      .json()
      .catch(function () {
        return {};
      })
      .then(function (payload) {
        if (!response.ok) {
          throw new Error(payload.error || ("请求失败（HTTP " + response.status + "）"));
        }
        return payload;
      });
  }

  function webviewApi() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  /* ------------------------------------------------------------------ */
  /* 面板：日志 / 状态 / 忙碌锁                                          */
  /* ------------------------------------------------------------------ */
  function Panel(options) {
    this.logEl = options.log;
    this.statusEl = options.status;
    this.startBtn = options.start;
    this.lockables = options.lockables || [];
    this.logCount = 0;
    this.busy = false;
  }

  Panel.prototype.status = function (text) {
    if (this.statusEl) {
      this.statusEl.textContent = text || "";
    }
  };

  Panel.prototype.log = function (line) {
    if (!this.logEl) {
      return;
    }
    this.logEl.hidden = false;
    this.logEl.textContent += line + "\n";
    this.logEl.scrollTop = this.logEl.scrollHeight;
  };

  Panel.prototype.clearLog = function () {
    if (!this.logEl) {
      return;
    }
    this.logEl.textContent = "";
    this.logEl.hidden = true;
    this.logCount = 0;
  };

  /** 由外部传入“是否可执行”，面板自己不猜业务状态。 */
  Panel.prototype.sync = function (canRun) {
    if (!this.startBtn) {
      return;
    }
    this.startBtn.disabled = this.busy || !canRun;
  };

  Panel.prototype.setBusy = function (flag, canRun) {
    this.busy = flag;
    this.lockables.forEach(function (node) {
      if (node) {
        node.disabled = flag;
      }
    });
    this.sync(canRun);
  };

  /* ------------------------------------------------------------------ */
  /* 主体                                                                */
  /* ------------------------------------------------------------------ */
  var el = {
    icoPath: byId("smart-ico-path"),
    icoPick: byId("smart-ico-pick"),
    folderBtn: byId("smart-folder"),
    filesBtn: byId("smart-files"),
    folderInput: byId("smart-folder-path"),
    folderScan: byId("smart-folder-scan"),
    recursive: byId("smart-recursive"),
    list: byId("smart-list"),
    items: byId("smart-items"),
    summary: byId("smart-summary"),
    selectAll: byId("smart-select-all"),
    selectNone: byId("smart-select-none"),
    clear: byId("smart-clear"),
    start: byId("smart-start"),
    status: byId("smart-status"),
    log: byId("smart-log")
  };

  if (!el.start || !el.items) {
    return; // 不在本页面
  }

  //: [{path, name, kind, note, target}]
  var targets = [];

  var panel = new Panel({
    log: el.log,
    status: el.status,
    start: el.start,
    lockables: [el.icoPick, el.folderBtn, el.filesBtn, el.folderScan, el.selectAll,
                el.selectNone, el.clear]
  });

  function icoPath() {
    return el.icoPath ? String(el.icoPath.value || "").trim().replace(/^"|"$/g, "") : "";
  }

  function keyOf(path) {
    return String(path || "").toLowerCase();
  }

  function kindOf(path) {
    var lower = String(path || "").toLowerCase();
    if (lower.endsWith(".exe")) {
      return "exe";
    }
    if (lower.endsWith(".lnk")) {
      return "lnk";
    }
    return "other";
  }

  function nameOf(path) {
    return String(path || "").split(/[\\/]/).pop();
  }

  function addTargets(fresh) {
    var seen = {};
    targets.forEach(function (item) {
      seen[keyOf(item.path)] = true;
    });
    var added = 0;
    (fresh || []).forEach(function (item) {
      if (!item || !item.path || seen[keyOf(item.path)]) {
        return;
      }
      seen[keyOf(item.path)] = true;
      targets.push({
        path: item.path,
        name: item.name || nameOf(item.path),
        kind: item.kind || kindOf(item.path),
        note: item.note || "",
        target: item.target || null
      });
      added += 1;
    });
    return added;
  }

  function checkedBoxes() {
    return el.items.querySelectorAll('input[type="checkbox"][data-path]');
  }

  function checkedPaths() {
    var picked = [];
    Array.prototype.forEach.call(checkedBoxes(), function (box) {
      if (box.checked) {
        picked.push(box.getAttribute("data-path"));
      }
    });
    return picked;
  }

  function canRun() {
    return !!icoPath() && checkedPaths().length > 0;
  }

  function badgeFor(item) {
    var badge = document.createElement("span");
    if (item.kind === "exe") {
      badge.className = "badge " + (item.note ? "badge--warn" : (item.signed ? "badge--signed" : "badge--free"));
      badge.textContent = item.note ? "EXE · 将跳过" : (item.signed ? "EXE · 签名文件走降级" : "EXE · rcedit 直接替换");
      return badge;
    }
    if (item.kind === "lnk") {
      badge.className = "badge badge--lnk";
      var target = String(item.target || "").split(/[\\/]/).pop();
      badge.textContent = target ? "LNK · " + target : "LNK · 不看目标";
      return badge;
    }
    badge.className = "badge badge--warn";
    badge.textContent = "跳过 · 不是 EXE/LNK";
    return badge;
  }

  function renderItem(item) {
    var li = document.createElement("li");
    li.className = "batch-item" + (item.note ? " batch-item--warn" : "");

    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !item.note;
    box.className = "batch-item__check";
    box.setAttribute("data-path", item.path);
    box.setAttribute("aria-label", "选择 " + item.name);
    box.addEventListener("change", sync);

    var name = document.createElement("span");
    name.className = "batch-item__name";
    name.textContent = item.name;
    name.title = item.path;

    li.appendChild(box);
    li.appendChild(name);
    li.appendChild(badgeFor(item));
    return li;
  }

  function render() {
    el.items.innerHTML = "";
    targets.forEach(function (item) {
      el.items.appendChild(renderItem(item));
    });
    el.list.hidden = targets.length === 0;
    sync();
  }

  function sync() {
    var picked = checkedPaths().length;
    var exeCount = targets.filter(function (i) { return i.kind === "exe"; }).length;
    var lnkCount = targets.filter(function (i) { return i.kind === "lnk"; }).length;
    if (el.summary) {
      el.summary.textContent = targets.length
        ? "已选 " + picked + " / " + targets.length + " 个目标（EXE " + exeCount + "，LNK " + lnkCount + "）"
        : "已选 0 个目标";
    }
    el.start.textContent = picked ? ("🚀 一键智能替换（" + picked + "）") : "🚀 一键智能替换";
    panel.sync(canRun());
  }

  function setAll(flag) {
    Array.prototype.forEach.call(checkedBoxes(), function (box) {
      box.checked = flag;
    });
    sync();
  }

  /* ---------------------------- 图标选择 ---------------------------- */
  if (el.icoPick) {
    el.icoPick.addEventListener("click", function () {
      if (panel.busy) {
        return;
      }
      var api = webviewApi();
      if (!api || typeof api.choose_icon !== "function") {
        panel.status("浏览器里取不到绝对路径，请手动粘贴 .ico 的完整路径");
        if (el.icoPath) {
          el.icoPath.focus();
        }
        return;
      }
      panel.status("正在打开 .ico 选择框…");
      api
        .choose_icon()
        .then(function (path) {
          if (path) {
            el.icoPath.value = path;
            panel.status("全局图标：" + path);
            sync();
          } else {
            panel.status("");
          }
        })
        .catch(function () {
          panel.status("无法打开选择框，请手动粘贴路径");
        });
    });
  }

  if (el.icoPath) {
    el.icoPath.addEventListener("input", sync);
  }

  /* ---------------------------- 目标选择 ---------------------------- */
  function addPaths(paths) {
    var fresh = (paths || []).map(function (path) {
      return { path: path, name: nameOf(path), kind: kindOf(path) };
    });
    var added = addTargets(fresh);
    render();
    return added;
  }

  if (el.folderBtn) {
    el.folderBtn.addEventListener("click", function () {
      if (panel.busy) {
        return;
      }
      var api = webviewApi();
      if (!api || typeof api.choose_folder !== "function") {
        panel.status("浏览器里不能弹目录选择框，请粘贴路径后点「扫描该文件夹」");
        return;
      }
      panel.status("正在打开文件夹选择框…");
      api
        .choose_folder()
        .then(function (path) {
          if (path) {
            el.folderInput.value = path;
            scanFolder();
          } else {
            panel.status("");
          }
        })
        .catch(function () {
          panel.status("无法打开选择框，请粘贴路径后点「扫描该文件夹」");
        });
    });
  }

  if (el.folderScan) {
    el.folderScan.addEventListener("click", function () {
      if (!panel.busy) {
        scanFolder();
      }
    });
  }

  function scanFolder() {
    var folder = el.folderInput ? String(el.folderInput.value || "").trim() : "";
    if (!folder) {
      panel.status("请先选择或粘贴一个目标文件夹");
      if (el.folderInput) {
        el.folderInput.focus();
      }
      return;
    }
    panel.setBusy(true, canRun());
    panel.status("正在扫描 " + folder + " …");
    fetch("/api/batch/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder: folder,
        recursive: !!(el.recursive && el.recursive.checked),
        mode: "all"
      })
    })
      .then(jsonOrThrow)
      .then(function (payload) {
        var added = addTargets(payload.files || []);
        render();
        panel.status(
          "扫描完成：exe " + (payload.exe_count || 0) + " 个，lnk " + (payload.lnk_count || 0) +
          " 个，新增 " + added + " 个目标"
        );
      })
      .catch(function (error) {
        panel.status(error.message || "扫描失败");
      })
      .then(function () {
        panel.setBusy(false, canRun());
      });
  }

  if (el.filesBtn) {
    el.filesBtn.addEventListener("click", function () {
      if (panel.busy) {
        return;
      }
      var api = webviewApi();
      if (!api || typeof api.choose_files !== "function") {
        panel.status("浏览器里取不到绝对路径：请用桌面版，或扫描文件夹");
        return;
      }
      panel.status("正在打开文件选择框…");
      api
        .choose_files()
        .then(function (paths) {
          if (!paths || !paths.length) {
            panel.status("");
            return;
          }
          var added = addPaths(paths);
          panel.status("新增 " + added + " 个目标，共 " + targets.length + " 个");
        })
        .catch(function () {
          panel.status("无法打开选择框，请改用「选择目标文件夹」");
        });
    });
  }

  if (el.selectAll) {
    el.selectAll.addEventListener("click", function () {
      if (!panel.busy) {
        setAll(true);
        panel.status("已全选 " + checkedPaths().length + " 个");
      }
    });
  }

  if (el.selectNone) {
    el.selectNone.addEventListener("click", function () {
      if (!panel.busy) {
        setAll(false);
        panel.status("已取消全部勾选");
      }
    });
  }

  if (el.clear) {
    el.clear.addEventListener("click", function () {
      if (panel.busy) {
        return;
      }
      targets = [];
      render();
      panel.clearLog();
      panel.status("");
    });
  }

  /* ---------------------------- 一键执行 ---------------------------- */
  el.start.addEventListener("click", function () {
    if (panel.busy) {
      return; // 任务进行中忽略重复点击
    }
    var icon = icoPath();
    if (!icon) {
      panel.status("请先选择全局图标（.ico）");
      if (el.icoPath) {
        el.icoPath.focus();
      }
      return;
    }
    var picked = checkedPaths();
    if (!picked.length) {
      panel.status("请先扫描文件夹或选择目标文件");
      return;
    }

    panel.setBusy(true, canRun());
    panel.clearLog();
    panel.status("正在启动…");

    fetch("/api/batch/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        files: picked,          // exe + lnk 混在同一个清单里，后端按后缀分流
        ico_path: icon,
        mode: "all",
        policy: "shortcut"
      })
    })
      .then(jsonOrThrow)
      .then(function (payload) {
        panel.log("任务已启动：" + payload.job_id + "（" + picked.length + " 个目标）");
        panel.status("处理中…");
        poll(payload.job_id);
      })
      .catch(function (error) {
        panel.setBusy(false, canRun());
        panel.status(error.message || "启动失败");
        panel.log("[失败] " + (error.message || "启动失败"));
      });

    function poll(jobId) {
      fetch("/api/batch/jobs/" + jobId)
        .then(jsonOrThrow)
        .then(function (job) {
          var logs = job.logs || [];
          if (logs.length > panel.logCount) {
            logs.slice(panel.logCount).forEach(function (line) {
              panel.log(line);
            });
            panel.logCount = logs.length;
          }
          if (job.total) {
            panel.status("处理中 " + job.done + " / " + job.total);
          }
          if (job.state === "running") {
            setTimeout(function () {
              poll(jobId);
            }, 500);
            return;
          }
          panel.setBusy(false, canRun());
          if (job.state === "error") {
            panel.status(job.error || "任务失败");
            return;
          }
          panel.status("完成：" + (job.result ? job.result.summary : "任务结束"));
        })
        .catch(function (error) {
          panel.status(error.message || "查询进度失败，正在重试…");
          setTimeout(function () {
            poll(jobId);
          }, 1200);
        });
    }
  });

  // 初始状态：没有目标/图标时按钮禁用
  render();
})();
