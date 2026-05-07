const STORAGE_KEY = "hymn-credit-control-panel";
const api = normaliseAPI(window.HYMN_PANEL_API || null);

const emptyData = {
    organisation: {
        name: "",
        status: "Awaiting live connection"
    },
    dashboard: {
        totalReceivables: null,
        totalOverdue: null,
        openInvoices: null,
        accountsNeedingAction: null,
        potentialInterest: null
    },
    customers: [],
    audit: [],
    selectedInvoice: null
};

const state = loadState();

const views = {
    dashboard: {
        eyebrow: "Live workflow",
        title: "Credit Control Dashboard",
        subtitle: "Headline numbers, grouped customer risk, and operational actions without seeded demo content."
    },
    customers: {
        eyebrow: "Customer balances",
        title: "Grouped customer ledger",
        subtitle: "A customer-first view of open invoices, overdue balances and control status."
    },
    invoice: {
        eyebrow: "Invoice workflow",
        title: "Invoice detail",
        subtitle: "Notes, status changes, promises to pay and late-payment guidance for the selected invoice."
    },
    activity: {
        eyebrow: "Audit trail",
        title: "Full history",
        subtitle: "A chronological record of syncs, notes, promises and status movement."
    }
};

let currentCustomer = state.customers[0] || null;
let currentInvoice = state.selectedInvoice || firstInvoice(currentCustomer);

function normaliseState(payload) {
    return {
        organisation: payload.organisation || emptyData.organisation,
        dashboard: payload.dashboard || emptyData.dashboard,
        customers: Array.isArray(payload.customers) ? payload.customers : [],
        audit: Array.isArray(payload.audit) ? payload.audit : [],
        selectedInvoice: payload.selectedInvoice || null
    };
}

function normaliseAPI(config) {
    if (!config || !config.baseUrl) {
        return null;
    }

    return {
        baseUrl: String(config.baseUrl).replace(/\/$/, ""),
        headers: config.headers || {},
        endpoints: {
            panel: config.endpoints?.panel || "/api/panel",
            sync: config.endpoints?.sync || "/api/panel/sync",
            note: config.endpoints?.note || "/api/invoices/:invoiceId/notes",
            promise: config.endpoints?.promise || "/api/invoices/:invoiceId/promises",
            status: config.endpoints?.status || "/api/invoices/:invoiceId/status",
            login: config.endpoints?.login || "/auth/xero/start"
        }
    };
}

function loadState() {
    const injected = normaliseState(window.HYMN_PANEL_DATA || emptyData);

    try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (!stored) {
            return injected;
        }

        const parsed = JSON.parse(stored);
        return normaliseState({
            ...injected,
            ...parsed,
        });
    } catch {
        return injected;
    }
}

function persistState() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function replaceState(next) {
    state.organisation = next.organisation;
    state.dashboard = next.dashboard;
    state.customers = next.customers;
    state.audit = next.audit;
    state.selectedInvoice = next.selectedInvoice;
    currentCustomer = state.customers[0] || null;
    currentInvoice = state.selectedInvoice || firstInvoice(currentCustomer);
    persistState();
}

function endpointURL(template, params = {}) {
    if (!api) {
        return "";
    }

    const path = Object.entries(params).reduce(
        (accumulator, [key, value]) => accumulator.replace(`:${key}`, encodeURIComponent(value)),
        template
    );
    return `${api.baseUrl}${path}`;
}

function loginURL() {
    if (!api) {
        return "";
    }

    const separator = api.endpoints.login.includes("?") ? "&" : "?";
    return `${endpointURL(api.endpoints.login)}${separator}redirect_to=${encodeURIComponent(window.location.href)}`;
}

async function requestJSON(template, options = {}, params = {}) {
    if (!api) {
        return null;
    }

    const response = await fetch(endpointURL(template, params), {
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...api.headers,
            ...(options.headers || {})
        }
    });

    if (!response.ok) {
        throw new Error(`API request failed with status ${response.status}`);
    }

    const contentType = response.headers.get("content-type") || "";
    return contentType.includes("application/json") ? response.json() : null;
}

async function hydrateFromAPI() {
    if (!api) {
        return;
    }

    try {
        const payload = await requestJSON(api.endpoints.panel);
        if (payload) {
            replaceState(normaliseState(payload));
        }
    } catch (error) {
        console.error("Unable to load panel data from API", error);
    }
}

function firstInvoice(customer) {
    return customer && Array.isArray(customer.invoices) ? customer.invoices[0] || null : null;
}

function formatCurrency(value, precise = false) {
    if (value === null || value === undefined || value === "") {
        return "--";
    }

    return new Intl.NumberFormat("en-GB", {
        style: "currency",
        currency: "GBP",
        minimumFractionDigits: precise ? 2 : 0,
        maximumFractionDigits: precise ? 2 : 0
    }).format(Number(value));
}

function renderEmptyState(target, eyebrow, title, body) {
    const template = document.getElementById("emptyStateTemplate");
    const node = template.content.firstElementChild.cloneNode(true);
    node.querySelector(".eyebrow").textContent = eyebrow;
    node.querySelector("h4").textContent = title;
    node.querySelector(".muted").textContent = body;
    target.innerHTML = "";
    target.appendChild(node);
}

function renderTimeline(items, target, emptyMessage) {
    target.innerHTML = "";

    if (!items.length) {
        renderEmptyState(target, "No activity", emptyMessage.title, emptyMessage.body);
        return;
    }

    const template = document.getElementById("timelineItemTemplate");
    items.forEach((item) => {
        const node = template.content.firstElementChild.cloneNode(true);
        node.querySelector("strong").textContent = item.title || "Update";
        node.querySelector("span").textContent = item.stamp || "";
        node.querySelector("p").textContent = item.body || "";
        target.appendChild(node);
    });
}

function renderMetrics() {
    const metrics = [
        ["Total receivables", formatCurrency(state.dashboard.totalReceivables), "Open receivables across the full ledger"],
        ["Total overdue", formatCurrency(state.dashboard.totalOverdue), "Current overdue exposure"],
        ["Open invoices", state.dashboard.openInvoices ?? "--", "Invoices not yet fully settled"],
        ["Accounts needing action", state.dashboard.accountsNeedingAction ?? "--", "Customers currently requiring follow-up"],
        ["Potential interest", formatCurrency(state.dashboard.potentialInterest, true), "Statutory late-payment estimate"],
    ];

    const grid = document.getElementById("metricGrid");
    grid.innerHTML = "";

    metrics.forEach(([label, value, caption]) => {
        const card = document.createElement("article");
        card.className = "metric-card";
        card.innerHTML = `<span>${label}</span><strong>${value}</strong><small>${caption}</small>`;
        grid.appendChild(card);
    });
}

function renderPriorityAccounts() {
    const target = document.getElementById("priorityAccounts");

    if (!state.customers.length) {
        renderEmptyState(
            target,
            "No accounts",
            "No live customers loaded yet",
            "Populate window.HYMN_PANEL_DATA.customers from your backend to show real action lists."
        );
        return;
    }

    target.innerHTML = "";
    [...state.customers]
        .sort((a, b) => Number(b.overdue || 0) - Number(a.overdue || 0))
        .slice(0, 5)
        .forEach((customer) => {
            const row = document.createElement("article");
            row.className = "timeline-item";
            row.innerHTML = `
                <div class="timeline-marker"></div>
                <div class="timeline-copy">
                    <div class="timeline-head">
                        <strong>${customer.name || "Unnamed customer"}</strong>
                        <span>${formatCurrency(customer.overdue)}</span>
                    </div>
                    <p>${customer.status || "Awaiting status"} · ${customer.openInvoices ?? 0} open invoices · ${formatCurrency(customer.totalDue)} total due</p>
                </div>
            `;
            row.addEventListener("click", () => {
                currentCustomer = customer;
                currentInvoice = firstInvoice(customer);
                renderInvoiceView();
                activateView("invoice");
            });
            target.appendChild(row);
        });
}

function renderCustomersTable(searchTerm = "") {
    const body = document.getElementById("customerTableBody");
    body.innerHTML = "";

    const customers = state.customers.filter((customer) =>
        (customer.name || "").toLowerCase().includes(searchTerm.toLowerCase())
    );

    if (!customers.length) {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td colspan="5">
                <div class="empty-state">
                    <p class="eyebrow">No live rows</p>
                    <h4>Customer data will appear here</h4>
                    <p class="muted">Inject customers into window.HYMN_PANEL_DATA.customers or bind this table to your live API.</p>
                </div>
            </td>
        `;
        body.appendChild(row);
        return;
    }

    customers.forEach((customer) => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td><strong>${customer.name || "Unnamed customer"}</strong><br><span class="muted">${customer.contact || ""}</span></td>
            <td>${customer.openInvoices ?? 0}</td>
            <td>${formatCurrency(customer.totalDue)}</td>
            <td>${formatCurrency(customer.overdue)}</td>
            <td>${customer.status || "Awaiting status"}</td>
        `;
        row.addEventListener("click", () => {
            currentCustomer = customer;
            currentInvoice = firstInvoice(customer);
            renderInvoiceView();
            activateView("invoice");
        });
        body.appendChild(row);
    });
}

function calculateInterest(invoice) {
    if (!invoice || !invoice.amountDue || !invoice.dueDate) {
        return null;
    }

    const overdueDays = Math.max(
        0,
        Math.floor((new Date() - new Date(invoice.dueDate)) / (1000 * 60 * 60 * 24))
    );
    return Math.round((Number(invoice.amountDue) * 0.08 * overdueDays / 365) * 100) / 100;
}

function calculateCourtCost(invoice) {
    if (!invoice || !invoice.amountDue) {
        return null;
    }

    const amount = Number(invoice.amountDue);
    if (amount <= 300) return 35;
    if (amount <= 500) return 50;
    if (amount <= 1000) return 70;
    if (amount <= 1500) return 80;
    if (amount <= 3000) return 115;
    if (amount <= 5000) return 205;
    return 455;
}

function renderInvoiceView() {
    const invoice = currentInvoice;
    const metricGrid = document.getElementById("invoiceMetricGrid");
    const title = document.getElementById("invoiceTitle");
    const meta = document.getElementById("invoiceMeta");
    const statusPill = document.getElementById("invoiceStatusPill");
    const infoText = document.getElementById("interestInfoText");

    if (!invoice) {
        title.textContent = "Select a live invoice";
        meta.textContent = "Choose a customer once live data is loaded.";
        statusPill.textContent = "Awaiting data";
        metricGrid.innerHTML = "";
        [
            ["Amount due", "--"],
            ["Interest so far", "--"],
            ["Court costs", "--"],
            ["Promise date", "--"]
        ].forEach(([label, value]) => {
            const card = document.createElement("article");
            card.className = "mini-metric";
            card.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
            metricGrid.appendChild(card);
        });
        infoText.textContent = "Late-payment interest and court-cost guidance will appear here once a real invoice is selected.";
        renderTimeline([], document.getElementById("notesTimeline"), {
            title: "No notes yet",
            body: "Invoice notes will appear when a real invoice is selected."
        });
        renderTimeline([], document.getElementById("promiseTimeline"), {
            title: "No promises yet",
            body: "Payment promises will appear when a real invoice is selected."
        });
        renderTimeline([], document.getElementById("statusTimeline"), {
            title: "No status history yet",
            body: "Status changes will appear when a real invoice is selected."
        });
        return;
    }

    const interest = calculateInterest(invoice);
    const courtCost = calculateCourtCost(invoice);
    title.textContent = invoice.id || "Unnamed invoice";
    meta.textContent = `${currentCustomer?.name || "Customer"} · Due ${invoice.dueDate || "Unknown due date"}`;
    statusPill.textContent = invoice.status || "Awaiting status";
    metricGrid.innerHTML = "";

    [
        ["Amount due", formatCurrency(invoice.amountDue)],
        ["Interest so far", formatCurrency(interest, true)],
        ["Court costs", formatCurrency(courtCost)],
        ["Promise date", invoice.promiseDate || "--"]
    ].forEach(([label, value]) => {
        const card = document.createElement("article");
        card.className = "mini-metric";
        card.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
        metricGrid.appendChild(card);
    });

    infoText.textContent = `If this proceeded to court today, estimated court costs would be ${formatCurrency(courtCost)} and statutory interest accrued so far would be ${formatCurrency(interest, true)}.`;

    renderTimeline(invoice.notes || [], document.getElementById("notesTimeline"), {
        title: "No notes yet",
        body: "Add internal commentary when your workflow is live."
    });
    renderTimeline(invoice.promises || [], document.getElementById("promiseTimeline"), {
        title: "No promises yet",
        body: "Promise-to-pay entries will appear here."
    });
    renderTimeline(invoice.statuses || [], document.getElementById("statusTimeline"), {
        title: "No status history yet",
        body: "Status updates will appear here."
    });
}

function activateView(viewName) {
    document.querySelectorAll(".view").forEach((view) => {
        view.classList.toggle("is-active", view.id === `${viewName}View`);
    });
    document.querySelectorAll(".nav-link").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.view === viewName);
    });

    document.getElementById("viewEyebrow").textContent = views[viewName].eyebrow;
    document.getElementById("viewTitle").textContent = views[viewName].title;
    document.getElementById("viewSubtitle").textContent = views[viewName].subtitle;
}

function wireForms() {
    document.getElementById("noteForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!currentInvoice) return;

        const input = document.getElementById("noteInput");
        const value = input.value.trim();
        if (!value) return;

        currentInvoice.notes = currentInvoice.notes || [];
        currentInvoice.notes.unshift({
            title: "Manual note",
            stamp: "Just now",
            body: value
        });
        input.value = "";
        persistState();
        renderInvoiceView();

        if (api) {
            try {
                await requestJSON(
                    api.endpoints.note,
                    {
                        method: "POST",
                        body: JSON.stringify({ body: value })
                    },
                    { invoiceId: currentInvoice.id }
                );
                await hydrateFromAPI();
            } catch (error) {
                console.error("Unable to save note to API", error);
            }
        }
    });

    document.getElementById("promiseForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!currentInvoice) return;

        const amount = document.getElementById("promiseAmount").value.trim();
        const date = document.getElementById("promiseDate").value;
        const note = document.getElementById("promiseNote").value.trim();
        if (!amount || !date) return;

        currentInvoice.promises = currentInvoice.promises || [];
        currentInvoice.promiseDate = date;
        currentInvoice.promises.unshift({
            title: `Promise to pay ${formatCurrency(amount)}`,
            stamp: `Due ${date}`,
            body: note || "Promise recorded."
        });
        document.getElementById("promiseAmount").value = "";
        document.getElementById("promiseDate").value = "";
        document.getElementById("promiseNote").value = "";
        persistState();
        renderInvoiceView();

        if (api) {
            try {
                await requestJSON(
                    api.endpoints.promise,
                    {
                        method: "POST",
                        body: JSON.stringify({
                            promisedAmount: amount,
                            promisedDate: date,
                            note
                        })
                    },
                    { invoiceId: currentInvoice.id }
                );
                await hydrateFromAPI();
            } catch (error) {
                console.error("Unable to save promise to API", error);
            }
        }
    });

    document.getElementById("statusForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!currentInvoice) return;

        const status = document.getElementById("statusSelect").value;
        const note = document.getElementById("statusNote").value.trim();

        currentInvoice.status = status;
        currentInvoice.statuses = currentInvoice.statuses || [];
        currentInvoice.statuses.unshift({
            title: status,
            stamp: "Just now",
            body: note || "Status updated."
        });
        document.getElementById("statusNote").value = "";
        persistState();
        renderInvoiceView();

        if (api) {
            try {
                await requestJSON(
                    api.endpoints.status,
                    {
                        method: "POST",
                        body: JSON.stringify({
                            status,
                            note
                        })
                    },
                    { invoiceId: currentInvoice.id }
                );
                await hydrateFromAPI();
            } catch (error) {
                console.error("Unable to save status to API", error);
            }
        }
    });
}

function wireSearch() {
    document.getElementById("customerSearch").addEventListener("input", (event) => {
        renderCustomersTable(event.target.value);
    });
}

function wireNavigation() {
    document.querySelectorAll(".nav-link").forEach((button) => {
        button.addEventListener("click", () => activateView(button.dataset.view));
    });
}

function wireSyncButtons() {
    const syncHandler = async () => {
        document.getElementById("syncStamp").textContent = "Sync requested";
        state.organisation.lastSync = "Sync requested just now";
        persistState();

        if (api) {
            try {
                await requestJSON(api.endpoints.sync, { method: "POST" });
                await hydrateFromAPI();
                return;
            } catch (error) {
                console.error("Unable to trigger sync via API", error);
            }
        }

        if (typeof window.HYMN_PANEL_RESYNC === "function") {
            await window.HYMN_PANEL_RESYNC();
        }
    };

    document.getElementById("primarySyncButton").addEventListener("click", syncHandler);
    document.getElementById("sidebarSyncButton").addEventListener("click", syncHandler);
}

function wireLoginButtons() {
    const connect = () => {
        const url = loginURL();
        if (!url) {
            alert("Set window.HYMN_PANEL_API.baseUrl to enable Xero login.");
            return;
        }

        window.location.href = url;
    };

    document.getElementById("connectXeroButton").addEventListener("click", connect);
    document.getElementById("sidebarConnectButton").addEventListener("click", connect);
}

function renderChrome() {
    document.getElementById("organisationName").textContent = state.organisation.name || "No organisation connected";
    document.getElementById("organisationStatus").textContent = state.organisation.status || "Awaiting live connection";
    if (state.organisation.lastSync) {
        document.getElementById("syncStamp").textContent = state.organisation.lastSync;
    }

    const connectButtons = [
        document.getElementById("connectXeroButton"),
        document.getElementById("sidebarConnectButton")
    ];
    const connected = Boolean(state.organisation.name);
    connectButtons.forEach((button) => {
        button.textContent = connected ? "Reconnect Xero" : "Connect Xero";
    });
}

async function init() {
    await hydrateFromAPI();
    renderChrome();
    renderMetrics();
    renderPriorityAccounts();
    renderCustomersTable();
    renderInvoiceView();
    renderTimeline(state.audit, document.getElementById("auditTimeline"), {
        title: "No audit events yet",
        body: "Live sync history and operational actions will appear here."
    });
    renderTimeline(state.audit.slice(0, 4), document.getElementById("dashboardTimeline"), {
        title: "No recent activity yet",
        body: "Once the workflow is live, the latest actions will appear here."
    });
    wireForms();
    wireSearch();
    wireNavigation();
    wireSyncButtons();
    wireLoginButtons();
}

init();
