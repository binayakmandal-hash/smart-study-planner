/* ==========================================================================
   SMART STUDY PLANNER — UI interactions
   ========================================================================== */

// 1. THEME ---------------------------------------------------------------------
function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "light";
}

function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("theme", theme); } catch (e) { /* private mode */ }
    document.querySelectorAll(".theme-label").forEach(el => {
        el.textContent = theme === "dark" ? "Light mode" : "Dark mode";
    });
}

function toggleTheme() {
    setTheme(currentTheme() === "dark" ? "light" : "dark");
}

// 2. TOASTS --------------------------------------------------------------------
function showToast(message, type = "info", title = "") {
    let container = document.querySelector(".toast-container");
    if (!container) {
        container = document.createElement("div");
        container.className = "toast-container";
        document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    const icons = { success: "✅", error: "⚠️", warning: "⏰", info: "💡" };
    const titles = { success: "Success", error: "Error", warning: "Reminder", info: "Notification" };
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <div class="toast-content">
            <div class="toast-title"></div>
            <div class="toast-message"></div>
        </div>
        <button class="toast-close" aria-label="Close">&times;</button>`;
    toast.querySelector(".toast-title").textContent = title || titles[type] || titles.info;
    toast.querySelector(".toast-message").textContent = message;
    container.appendChild(toast);
    toast.querySelector(".toast-close").addEventListener("click", () => removeToast(toast));
    setTimeout(() => removeToast(toast), 6000);
}

function removeToast(toast) {
    if (!toast || toast.classList.contains("toast-hiding")) return;
    toast.classList.add("toast-hiding");
    setTimeout(() => toast.remove(), 350);
}

// 3. TASK FILTERS --------------------------------------------------------------
function filterTasks() {
    const body = document.getElementById("taskTableBody");
    if (!body) return;
    const search = (document.getElementById("searchInput") || {}).value || "";
    const priority = (document.getElementById("priorityFilter") || {}).value || "All";
    const status = (document.getElementById("statusFilter") || {}).value || "All";
    const text = search.toLowerCase().trim();
    let visible = 0;
    const rows = body.querySelectorAll(".task-row");
    rows.forEach(row => {
        const ok = (!text || (row.dataset.subject || "").includes(text) || (row.dataset.task || "").includes(text))
            && (priority === "All" || row.dataset.priority === priority)
            && (status === "All" || row.dataset.status === status);
        row.style.display = ok ? "" : "none";
        if (ok) visible++;
    });
    const empty = document.getElementById("noTasksMessage");
    if (empty) empty.style.display = visible === 0 && rows.length > 0 ? "block" : "none";
}

// 4. IN-APP STUDY REMINDERS (Google Calendar sends the phone notification) -----
function checkStudyAlarm() {
    const now = new Date();
    const pad = n => String(n).padStart(2, "0");
    const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    const hm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
    document.querySelectorAll("[data-alarm='1'][data-start]").forEach(el => {
        const date = el.dataset.date || today;
        if (date !== today) return;
        const key = `alarm-${el.dataset.id}-${date}-${el.dataset.start}`;
        if (el.dataset.start === hm && !sessionStorage.getItem(key)) {
            try { sessionStorage.setItem(key, "1"); } catch (e) { /* ignore */ }
            showToast(`${el.dataset.topic} (${el.dataset.subject})`, "warning", "Study session starting now");
        }
    });
    // highlight the session happening right now
    document.querySelectorAll(".session[data-start][data-end]").forEach(el => {
        const date = el.dataset.date || today;
        el.classList.toggle("is-now", date === today && el.dataset.start <= hm && hm < el.dataset.end);
    });
}

// 5. TABS ----------------------------------------------------------------------
function initTabs() {
    document.querySelectorAll("[data-tabs]").forEach(group => {
        const tabs = group.querySelectorAll(".tab");
        tabs.forEach(tab => tab.addEventListener("click", () => {
            tabs.forEach(t => t.classList.toggle("active", t === tab));
            document.querySelectorAll(`[data-tab-panel="${group.dataset.tabs}"]`).forEach(panel => {
                panel.classList.toggle("active", panel.id === tab.dataset.target);
            });
        }));
    });
}

// 6. DROPZONE ------------------------------------------------------------------
function initDropzone() {
    document.querySelectorAll(".dropzone").forEach(zone => {
        const input = zone.querySelector("input[type='file']");
        const label = zone.querySelector(".file-name span");
        if (!input) return;
        const update = () => {
            if (input.files && input.files.length) {
                zone.classList.add("has-file");
                if (label) label.textContent = `${input.files[0].name} · ${(input.files[0].size / 1048576).toFixed(1)} MB`;
            }
        };
        input.addEventListener("change", update);
        ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("is-over"); }));
        ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, () => zone.classList.remove("is-over")));
        zone.addEventListener("drop", e => {
            e.preventDefault();
            if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; update(); }
        });
    });
}

// 7. FOCUS TIMER (Pomodoro) ----------------------------------------------------
function initFocusTimer() {
    const root = document.getElementById("focusTimer");
    if (!root) return;
    const display = root.querySelector("[data-time]");
    const label = root.querySelector("[data-mode-label]");
    const ring = root.querySelector(".value");
    const startBtn = root.querySelector("[data-start]");
    const resetBtn = root.querySelector("[data-reset]");
    const radius = 70, circumference = 2 * Math.PI * radius;
    ring.style.strokeDasharray = circumference;

    let total = 25 * 60, left = total, timer = null, mode = "Focus";

    const render = () => {
        display.textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
        ring.style.strokeDashoffset = circumference * (1 - left / total);
        label.textContent = mode;
        startBtn.innerHTML = timer
            ? '<svg class="icon icon-fill"><use href="#i-pause"/></svg> Pause'
            : '<svg class="icon icon-fill"><use href="#i-play"/></svg> Start';
    };
    const stop = () => { clearInterval(timer); timer = null; render(); };

    root.querySelectorAll("[data-minutes]").forEach(btn => btn.addEventListener("click", () => {
        root.querySelectorAll("[data-minutes]").forEach(b => b.classList.toggle("active", b === btn));
        stop();
        total = left = parseInt(btn.dataset.minutes, 10) * 60;
        mode = btn.dataset.label;
        render();
    }));
    startBtn.addEventListener("click", () => {
        if (timer) { stop(); return; }
        timer = setInterval(() => {
            left -= 1;
            if (left <= 0) {
                stop();
                left = total;
                showToast(mode === "Focus" ? "Great focus! Take a short break." : "Break over — back to it!", "success", `${mode} finished`);
            }
            render();
        }, 1000);
        render();
    });
    resetBtn.addEventListener("click", () => { stop(); left = total; render(); });
    render();
}

// 8. BOOT ----------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
    setTheme(currentTheme());
    document.querySelectorAll(".theme-toggle-btn").forEach(btn => btn.addEventListener("click", toggleTheme));

    const menuBtn = document.getElementById("menuBtn");
    const backdrop = document.getElementById("sidebarBackdrop");
    if (menuBtn) menuBtn.addEventListener("click", () => document.body.classList.toggle("nav-open"));
    if (backdrop) backdrop.addEventListener("click", () => document.body.classList.remove("nav-open"));

    document.querySelectorAll(".flash-close").forEach(btn => btn.addEventListener("click", () => btn.closest(".flash").remove()));

    ["searchInput", "priorityFilter", "statusFilter"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener(el.tagName === "INPUT" ? "input" : "change", filterTasks);
    });
    filterTasks();

    document.querySelectorAll("form").forEach(form => form.addEventListener("submit", () => {
        const btn = form.querySelector("button[type='submit']:not(.no-disable)");
        if (btn) btn.classList.add("btn-loading");
    }));

    document.querySelectorAll("[data-confirm]").forEach(el => el.addEventListener("submit", e => {
        if (!window.confirm(el.dataset.confirm)) {
            e.preventDefault();
            const btn = el.querySelector(".btn-loading");
            if (btn) btn.classList.remove("btn-loading");
        }
    }));

    initTabs();
    initDropzone();
    initFocusTimer();
    checkStudyAlarm();
    setInterval(checkStudyAlarm, 20000);
});
