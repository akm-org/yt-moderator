(function () {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const root = document.documentElement;
  const storedTheme = localStorage.getItem("ytModeratorTheme");
  if (storedTheme) {
    root.setAttribute("data-bs-theme", storedTheme);
  }

  const themeButton = document.getElementById("themeToggle");
  if (themeButton) {
    themeButton.addEventListener("click", () => {
      const next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      localStorage.setItem("ytModeratorTheme", next);
    });
  }

  function toast(message) {
    const toastEl = document.getElementById("appToast");
    if (!toastEl || !window.bootstrap) return;
    toastEl.querySelector(".toast-body").textContent = message;
    window.bootstrap.Toast.getOrCreateInstance(toastEl).show();
  }

  function chartColors() {
    const style = getComputedStyle(document.documentElement);
    return {
      blue: style.getPropertyValue("--app-blue").trim(),
      green: style.getPropertyValue("--app-green").trim(),
      amber: style.getPropertyValue("--app-amber").trim(),
      red: style.getPropertyValue("--app-red").trim(),
      muted: style.getPropertyValue("--app-muted").trim()
    };
  }

  function renderDashboardChart() {
    const canvas = document.getElementById("messageChart");
    if (!canvas || !window.dashboardChartData || !window.Chart) return;
    const colors = chartColors();
    new Chart(canvas, {
      type: "line",
      data: {
        labels: window.dashboardChartData.labels,
        datasets: [{
          label: "Messages",
          data: window.dashboardChartData.messages,
          borderColor: colors.blue,
          backgroundColor: "rgba(90,169,255,0.14)",
          fill: true,
          tension: 0.35
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: colors.muted }, grid: { display: false } },
          y: { ticks: { color: colors.muted }, grid: { color: "rgba(155,167,184,0.16)" } }
        }
      }
    });
  }

  function renderStatisticsCharts() {
    if (!window.statisticsData || !window.Chart) return;
    const colors = chartColors();
    const actionCanvas = document.getElementById("actionChart");
    if (actionCanvas) {
      new Chart(actionCanvas, {
        type: "bar",
        data: {
          labels: Object.keys(window.statisticsData.actions),
          datasets: [{
            data: Object.values(window.statisticsData.actions),
            backgroundColor: [colors.green, colors.amber, colors.blue, colors.red, "#8f7af3"]
          }]
        },
        options: { plugins: { legend: { display: false } } }
      });
    }
    const categoryCanvas = document.getElementById("categoryChart");
    if (categoryCanvas) {
      new Chart(categoryCanvas, {
        type: "doughnut",
        data: {
          labels: Object.keys(window.statisticsData.categories),
          datasets: [{
            data: Object.values(window.statisticsData.categories),
            backgroundColor: [colors.red, colors.amber, colors.blue, colors.green, "#8f7af3", "#45c4b0"]
          }]
        }
      });
    }
  }

  function renderAnalyticsChart() {
    const canvas = document.getElementById("hourlyChart");
    if (!canvas || !window.analyticsData || !window.Chart) return;
    const colors = chartColors();
    new Chart(canvas, {
      data: {
        labels: window.analyticsData.labels,
        datasets: [
          {
            type: "bar",
            label: "Messages",
            data: window.analyticsData.messages,
            backgroundColor: "rgba(90,169,255,0.35)"
          },
          {
            type: "line",
            label: "Avg spam",
            data: window.analyticsData.avgSpam,
            borderColor: colors.red,
            tension: 0.35,
            yAxisID: "risk"
          }
        ]
      },
      options: {
        scales: {
          risk: { position: "right", min: 0, max: 100, grid: { drawOnChartArea: false } }
        }
      }
    });
  }

  async function refreshStats() {
    if (!document.querySelector("[data-stat]")) return;
    try {
      const response = await fetch("/stats", { headers: { "X-CSRF-Token": csrfToken } });
      if (!response.ok) return;
      const stats = await response.json();
      const mapping = {
        "messages_min": stats.messages_per_minute,
        "spam_detected": stats.spam_detected,
        "warnings": stats.warnings,
        "timeouts": stats.timeouts,
        "bans": stats.bans,
        "gemini_requests": stats.gemini_requests,
        "api_usage": stats.api_usage,
        "uptime": stats.uptime
      };
      for (const [key, value] of Object.entries(mapping)) {
        const node = document.querySelector(`[data-stat="${key}"]`);
        if (node) node.textContent = value;
      }
      const cpu = document.getElementById("cpuValue");
      const ram = document.getElementById("ramValue");
      if (cpu) cpu.textContent = `${stats.system.cpu_percent}%`;
      if (ram) ram.textContent = `${stats.system.ram_percent}%`;
    } catch (_) {
      // Keep the dashboard quiet during transient network failures.
    }
  }

  function connectLiveSocket() {
    if (!window.enableLiveSocket) return;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/livechat`);
    socket.addEventListener("message", (event) => {
      const data = JSON.parse(event.data);
      if (data.event === "message") appendLiveMessage(data.payload);
      if (data.event === "action") appendAction(data.payload);
      if (data.event === "status") updateConnection(data.payload);
    });
    socket.addEventListener("close", () => {
      setTimeout(connectLiveSocket, 3000);
    });
  }

  function updateConnection(payload) {
    const pill = document.getElementById("connection-pill");
    if (!pill) return;
    pill.className = `badge rounded-pill ${payload.connected ? "text-bg-success" : "text-bg-secondary"}`;
    pill.textContent = payload.connected ? "Connected" : "Offline";
  }

  function scoreClass(score) {
    if (score >= 70) return "high";
    if (score >= 40) return "mid";
    return "low";
  }

  function appendLiveMessage(message) {
    const list = document.getElementById("liveChatList");
    if (list) {
      const empty = list.querySelector(".empty-state");
      if (empty) empty.remove();
      const row = document.createElement("article");
      row.className = `chat-row action-${message.final_action}`;
      row.innerHTML = `
        <div class="chat-meta">
          <a href="/users/${encodeURIComponent(message.author_channel_id || "")}"></a>
          <span>${new Date(message.received_at).toLocaleString()}</span>
          <span class="score-pill score-${scoreClass(message.spam_score)}">${Math.round(message.spam_score)}</span>
          <span class="action-pill action-${message.final_action}">${message.final_action}</span>
        </div>
        <p></p>`;
      row.querySelector("a").textContent = message.username;
      row.querySelector("p").textContent = message.message;
      list.appendChild(row);
      list.scrollTop = list.scrollHeight;
    }

    const table = document.getElementById("dashboardMessages");
    if (table) {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><a href="/users/${encodeURIComponent(message.author_channel_id || "")}"></a></td>
        <td class="text-truncate table-message"></td>
        <td><span class="score-pill score-${scoreClass(message.spam_score)}">${Math.round(message.spam_score)}</span></td>
        <td><span class="action-pill action-${message.final_action}">${message.final_action}</span></td>`;
      row.querySelector("a").textContent = message.username;
      row.querySelector(".table-message").textContent = message.message;
      table.prepend(row);
      while (table.children.length > 25) table.lastElementChild.remove();
    }
  }

  function appendAction(action) {
    const feed = document.getElementById("actionFeed");
    if (!feed) return;
    const row = document.createElement("article");
    row.innerHTML = `
      <span class="action-pill action-${action.action}">${action.action}</span>
      <div><strong></strong><p></p></div>`;
    row.querySelector("strong").textContent = action.username;
    row.querySelector("p").textContent = action.reason;
    feed.prepend(row);
    while (feed.children.length > 12) feed.lastElementChild.remove();
  }

  document.querySelectorAll(".moderate-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      const messageId = button.dataset.messageId;
      const action = button.dataset.action;
      const response = await fetch("/moderate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken
        },
        body: JSON.stringify({
          message_id: messageId,
          action,
          reason: `Manual ${action}`
        })
      });
      if (response.ok) {
        toast(`${action} applied`);
      } else {
        toast("Moderation action failed");
      }
    });
  });

  const settingsForm = document.getElementById("settingsForm");
  if (settingsForm?.dataset.autosave === "true") {
    let autosaveTimer = null;
    settingsForm.addEventListener("input", () => {
      clearTimeout(autosaveTimer);
      autosaveTimer = setTimeout(async () => {
        const response = await fetch(settingsForm.action, {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken },
          body: new FormData(settingsForm)
        });
        if (response.ok) toast("Settings saved");
      }, 1200);
    });
  }

  renderDashboardChart();
  renderStatisticsCharts();
  renderAnalyticsChart();
  connectLiveSocket();
  setInterval(refreshStats, 10000);
})();
