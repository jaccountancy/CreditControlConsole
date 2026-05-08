const STORAGE_KEY = "hymn-credit-control-ledger";
const DEFAULT_API_BASE_URL = "https://creditcontrolconsole-production.up.railway.app";
const api = normaliseAPI(window.HYMN_PANEL_API || {});

const emptyData = {
    organisation: {
        name: "",
        status: "Awaiting live connection",
        lastSync: "Waiting for first sync",
        xeroConnected: false
    },
    dashboard: {
        totalReceivables: 0,
        totalOverdue: 0,
        openInvoices: 0,
        accountsNeedingAction: 0,
        potentialInterest: 0
    },
    customers: [],
    audit: [],
    selectedInvoice: null
};

const state = loadState();
let selectedFilter = "all";
let selectedInvoiceId = state.selectedInvoice?.id || null;
let searchTerm = "";
let managerFilter = "all";
let clientFilter = "all";
let pageSize = 25;
let currentPage = 1;

function normaliseAPI(config) {
    return {
        baseUrl: config.baseUrl ? String(config.baseUrl).replace(/\/$/, "") : DEFAULT_API_BASE_URL,
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

function normaliseState(payload) {
    return {
        organisation: payload.organisation || emptyData.organisation,
        dashboard: payload.dashboard || emptyData.dashboard,
        customers: Array.isArray(payload.customers) ? payload.customers : [],
        audit: Array.isArray(payload.audit) ? payload.audit : [],
        selectedInvoice: payload.selectedInvoice || null
    };
}

function loadState() {
    const injected = normaliseState(window.HYMN_PANEL_DATA || emptyData);

    try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (!stored) {
            return injected;
        }

        return normaliseState({
            ...injected,
            ...JSON.parse(stored),
        });
    } catch {
        return injected;
    }
}

function persistState() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

function replaceState(next) {
    const preservedInvoiceId = selectedInvoiceId;
    state.organisation = next.organisation;
    state.dashboard = next.dashboard;
    state.customers = next.customers;
    state.audit = next.audit;
    state.selectedInvoice = next.selectedInvoice;

    const invoice = findInvoiceById(preservedInvoiceId) || next.selectedInvoice || allInvoices()[0] || null;
    selectedInvoiceId = invoice?.id || null;
    persistState();
}

function endpointURL(template, params = {}) {
    const path = Object.entries(params).reduce(
        (accumulator, [key, value]) => accumulator.replace(`:${key}`, encodeURIComponent(value)),
        template
    );
    return `${api.baseUrl}${path}`;
}

function loginURL() {
    const separator = api.endpoints.login.includes("?") ? "&" : "?";
    return `${endpointURL(api.endpoints.login)}${separator}redirect_to=${encodeURIComponent(window.location.href)}`;
}

async function requestJSON(template, options = {}, params = {}) {
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
    try {
        const payload = await requestJSON(api.endpoints.panel);
        if (payload) {
            replaceState(normaliseState(payload));
        }
    } catch (error) {
        console.error("Unable to load panel data from API", error);
    }
}

function allInvoices() {
    return state.customers.flatMap((customer) =>
        (customer.invoices || []).map((invoice) => ({
            ...invoice,
            customerId: customer.id,
            customerName: customer.name,
            customerContact: customer.contact || "",
            customerStatus: customer.status || "",
            manager: customer.manager || "Unassigned",
            description: invoice.description || invoice.invoiceNumber || "Xero invoice",
            notesSummary: noteSnippet(invoice),
        }))
    );
}

function findInvoiceById(invoiceId) {
    if (!invoiceId) {
        return null;
    }
    return allInvoices().find((invoice) => invoice.id === invoiceId) || null;
}

function invoiceCategory(invoice) {
    const control = `${invoice.controlStatus || invoice.status || ""}`.toLowerCase();
    const amountDue = Number(invoice.amountDue || 0);
    const dueDate = invoice.dueDate ? new Date(invoice.dueDate) : null;
    const now = new Date();

    if (amountDue <= 0 || control.includes("paid")) {
        return "paid";
    }
    if (control.includes("court") || control.includes("legal")) {
        return "court";
    }
    if (amountDue > 0 && dueDate && dueDate < now) {
        return "overdue";
    }
    return "outstanding";
}

function invoiceStatusLabel(invoice) {
    const category = invoiceCategory(invoice);
    return {
        paid: "Paid",
        outstanding: "Outstanding",
        overdue: "Overdue",
        court: "Court"
    }[category];
}

function formatCurrency(value) {
    return new Intl.NumberFormat("en-GB", {
        style: "currency",
        currency: "GBP",
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    }).format(Number(value || 0));
}

function formatDate(value) {
    if (!value) {
        return "--";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
        return value;
    }
    return new Intl.DateTimeFormat("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric"
    }).format(date);
}

function noteSnippet(invoice) {
    if (invoice.notes?.length) {
        return invoice.notes[0].body || invoice.notes[0].title || "";
    }
    if (invoice.statuses?.length) {
        return invoice.statuses[0].body || "";
    }
    return "";
}

function descriptionGlyph(invoice) {
    const category = invoiceCategory(invoice);
    return {
        paid: "✓",
        outstanding: "◌",
        overdue: "!",
        court: "§"
    }[category];
}

function managerValues() {
    return [...new Set(state.customers.map((customer) => customer.manager || "Unassigned"))];
}

function filteredInvoices() {
    return allInvoices().filter((invoice) => {
        const matchesFilter = selectedFilter === "all" || invoiceCategory(invoice) === selectedFilter;
        const blob = [
            invoice.customerName,
            invoice.customerContact,
            invoice.invoiceNumber,
            invoice.description,
            invoice.notesSummary,
            invoice.status,
            invoice.controlStatus
        ].join(" ").toLowerCase();
        const matchesSearch = blob.includes(searchTerm.toLowerCase());
        const matchesManager = managerFilter === "all" || invoice.manager === managerFilter;
        const needsAction = invoiceCategory(invoice) === "overdue" || invoiceCategory(invoice) === "court" || invoiceCategory(invoice) === "outstanding";
        const matchesClient = clientFilter === "all" || (clientFilter === "action" ? needsAction : !needsAction);
        return matchesFilter && matchesSearch && matchesManager && matchesClient;
    });
}

function renderSummaryCounts() {
    const invoices = allInvoices();
    const counts = {
        all: invoices.length,
        paid: invoices.filter((invoice) => invoiceCategory(invoice) === "paid").length,
        overdue: invoices.filter((invoice) => invoiceCategory(invoice) === "overdue").length,
        court: invoices.filter((invoice) => invoiceCategory(invoice) === "court").length,
    };

    document.getElementById("countAll").textContent = counts.all.toLocaleString("en-GB");
    document.getElementById("countPaid").textContent = counts.paid.toLocaleString("en-GB");
    document.getElementById("countOverdue").textContent = counts.overdue.toLocaleString("en-GB");
    document.getElementById("countCourt").textContent = counts.court.toLocaleString("en-GB");

    document.querySelectorAll(".summary-chip").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.filter === selectedFilter);
    });
}

function renderManagerFilter() {
    const select = document.getElementById("managerFilter");
    const currentValue = managerFilter;
    select.innerHTML = `<option value="all">All Managers</option>`;
    managerValues().forEach((manager) => {
        const option = document.createElement("option");
        option.value = manager;
        option.textContent = manager;
        select.appendChild(option);
    });
    select.value = currentValue;
}

function renderInvoiceTable() {
    const tbody = document.getElementById("invoiceTableBody");
    const invoices = filteredInvoices();
    const pages = Math.max(1, Math.ceil(invoices.length / pageSize));
    currentPage = Math.min(currentPage, pages);

    const start = (currentPage - 1) * pageSize;
    const paged = invoices.slice(start, start + pageSize);

    tbody.innerHTML = "";

    if (!paged.length) {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td colspan="8">
                <div class="empty-state">
                    <p class="eyebrow">No invoices</p>
                    <h4>No ledger rows match the current filters</h4>
                    <p>Connect Xero and sync live data, or widen the search and filter settings.</p>
                </div>
            </td>
        `;
        tbody.appendChild(row);
    } else {
        paged.forEach((invoice) => {
            const category = invoiceCategory(invoice);
            const row = document.createElement("tr");
            row.className = invoice.id === selectedInvoiceId ? "is-selected" : "";
            row.innerHTML = `
                <td>
                    <div class="client-name">${invoice.customerName || "Unnamed client"}</div>
                    <div class="client-subline">${invoice.customerContact || invoice.customerId || ""}</div>
                </td>
                <td>
                    <div class="client-name">${invoice.invoiceNumber || "--"}</div>
                    <div class="invoice-subline">${invoice.id || ""}</div>
                </td>
                <td>
                    <div class="description-cell">
                        <div class="description-icon">${descriptionGlyph(invoice)}</div>
                        <div>
                            <div class="description-title">${invoice.description || "Xero invoice"}</div>
                            <div class="invoice-subline">${invoice.customerStatus || "Live Xero contact"}</div>
                        </div>
                    </div>
                </td>
                <td class="amount-cell">${formatCurrency(invoice.total || invoice.amountDue || 0)}</td>
                <td>
                    <div class="payment-cell">
                        <div class="payment-status">
                            <span class="payment-dot ${category}">${category === "paid" ? "✓" : category === "court" ? "!" : "○"}</span>
                            <span class="paid-amount">${formatCurrency((invoice.total || 0) - (invoice.amountDue || 0))}</span>
                        </div>
                        <div class="paid-date">${category === "paid" ? formatDate(invoice.invoiceDate || invoice.dueDate) : category === "overdue" ? "Payment overdue" : formatDate(invoice.promisedDate || invoice.dueDate)}</div>
                    </div>
                </td>
                <td><span class="status-pill ${category}">${invoiceStatusLabel(invoice)}</span></td>
                <td><div class="note-snippet">${invoice.notesSummary || "No notes yet."}</div></td>
                <td><button class="row-menu" type="button">⋮</button></td>
            `;
            row.addEventListener("click", () => {
                selectedInvoiceId = invoice.id;
                renderAll();
            });
            tbody.appendChild(row);
        });
    }

    renderPagination(invoices.length, pages, start, paged.length);
}

function renderPagination(totalResults, totalPages, start, count) {
    document.getElementById("resultsText").textContent = totalResults
        ? `Showing ${start + 1} to ${start + count} of ${totalResults.toLocaleString("en-GB")} results`
        : "Showing 0 results";

    document.getElementById("previousPageButton").disabled = currentPage <= 1;
    document.getElementById("nextPageButton").disabled = currentPage >= totalPages;

    const target = document.getElementById("pagePills");
    target.innerHTML = "";

    const pages = [];
    for (let page = 1; page <= totalPages; page += 1) {
        if (page <= 3 || page > totalPages - 2 || Math.abs(page - currentPage) <= 1) {
            pages.push(page);
        }
    }

    [...new Set(pages)].forEach((page) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `page-pill${page === currentPage ? " is-active" : ""}`;
        button.textContent = String(page);
        button.addEventListener("click", () => {
            currentPage = page;
            renderInvoiceTable();
        });
        target.appendChild(button);
    });
}

function renderDetailPanel() {
    const invoice = findInvoiceById(selectedInvoiceId) || filteredInvoices()[0] || allInvoices()[0] || null;
    selectedInvoiceId = invoice?.id || null;

    document.getElementById("invoiceTitle").textContent = invoice ? `${invoice.customerName} · ${invoice.invoiceNumber}` : "No invoice selected";
    document.getElementById("invoiceMeta").textContent = invoice
        ? `${invoice.description || "Xero invoice"} · due ${formatDate(invoice.dueDate)}`
        : "Select a row above to inspect status, notes, promise to pay and late-payment guidance.";
    document.getElementById("invoiceStatusBadge").textContent = invoice ? invoiceStatusLabel(invoice) : "Awaiting data";
    document.getElementById("invoiceStatusBadge").className = `status-badge ${invoice ? invoiceCategory(invoice) : ""}`.trim();

    const stats = document.getElementById("detailStats");
    stats.innerHTML = "";

    const items = invoice ? [
        ["Amount due", formatCurrency(invoice.amountDue || 0)],
        ["Amount paid", formatCurrency((invoice.total || 0) - (invoice.amountDue || 0))],
        ["Overdue days", `${invoice.overdueDays || 0}`],
        ["Promised date", invoice.promisedDate ? formatDate(invoice.promisedDate) : "--"],
    ] : [];

    items.forEach(([label, value]) => {
        const card = document.createElement("article");
        card.className = "detail-stat";
        card.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
        stats.appendChild(card);
    });

    document.getElementById("interestInfoText").textContent = invoice
        ? `If this went to court there would be £${Number(invoice.latePayment?.court_cost || invoice.latePayment?.courtCost || 35).toFixed(0)} in court costs and £${Number(invoice.latePayment?.interest || 0).toFixed(2)} in statutory interest so far.`
        : "Statutory interest and court-cost guidance will appear here for the selected invoice.";

    document.getElementById("statusSelect").value = invoice?.controlStatus || invoiceStatusLabel(invoice) || "Outstanding";

    renderTimeline("statusTimeline", invoice?.statuses || [], {
        eyebrow: "No status history",
        title: "Status changes will appear here",
        body: "Save a status change to build the credit-control history."
    });
    renderTimeline("notesTimeline", invoice?.notes || [], {
        eyebrow: "No notes",
        title: "Internal notes will appear here",
        body: "Use the note form to log calls, promises or chasing updates."
    });
    renderTimeline("promiseTimeline", invoice?.promises || [], {
        eyebrow: "No promises",
        title: "Promise-to-pay entries will appear here",
        body: "Record a commitment to track expected cash collection."
    });
}

function renderTimeline(targetId, items, empty) {
    const target = document.getElementById(targetId);
    target.innerHTML = "";

    if (!items.length) {
        const emptyState = document.getElementById("emptyStateTemplate").content.firstElementChild.cloneNode(true);
        emptyState.querySelector(".eyebrow").textContent = empty.eyebrow;
        emptyState.querySelector("h4").textContent = empty.title;
        emptyState.querySelector("p:last-child").textContent = empty.body;
        target.appendChild(emptyState);
        return;
    }

    items.forEach((item) => {
        const row = document.createElement("article");
        row.className = "timeline-item";
        row.innerHTML = `
            <div class="timeline-head">
                <span class="timeline-title">${item.title || "Update"}</span>
                <span class="timeline-stamp">${item.stamp || ""}</span>
            </div>
            <div class="timeline-body">${item.body || ""}</div>
        `;
        target.appendChild(row);
    });
}

function renderChrome() {
    document.getElementById("syncStamp").textContent = state.organisation.lastSync || "Waiting for first sync";
    const connected = isXeroConnected();
    const label = connected ? "Reconnect Xero" : "Connect Xero";
    document.getElementById("connectXeroButton").textContent = label;
    syncButtonIds.forEach((buttonId) => {
        const button = document.getElementById(buttonId);
        if (!button) {
            return;
        }
        button.disabled = !connected;
        button.textContent = connected ? (buttonId === "primarySyncButton" ? "Resync from Xero" : "Run sync") : "Connect Xero to sync";
        button.title = connected ? "Sync invoices from Xero" : "Connect Xero before syncing";
        button.setAttribute("aria-disabled", String(!connected));
    });
    const sidebarConnectButton = document.getElementById("sidebarConnectButton");
    if (sidebarConnectButton) {
        sidebarConnectButton.textContent = connected ? "Reconnect Xero" : "Login with Xero";
    }
}

const syncButtonIds = ["primarySyncButton", "sidebarSyncButton"];

function isXeroConnected() {
    return state.organisation.xeroConnected === true || Boolean(state.organisation.name);
}

function renderAll() {
    renderChrome();
    renderSummaryCounts();
    renderManagerFilter();
    renderInvoiceTable();
    renderDetailPanel();
}

function wireFilters() {
    document.querySelectorAll(".summary-chip").forEach((button) => {
        button.addEventListener("click", () => {
            selectedFilter = button.dataset.filter;
            currentPage = 1;
            renderAll();
        });
    });

    document.getElementById("ledgerSearch").addEventListener("input", (event) => {
        searchTerm = event.target.value;
        currentPage = 1;
        renderInvoiceTable();
    });

    document.getElementById("managerFilter").addEventListener("change", (event) => {
        managerFilter = event.target.value;
        currentPage = 1;
        renderInvoiceTable();
    });

    document.getElementById("clientFilter").innerHTML = `
        <option value="all">Active Clients</option>
        <option value="action">Needs Action</option>
        <option value="current">Current Only</option>
    `;
    document.getElementById("clientFilter").addEventListener("change", (event) => {
        clientFilter = event.target.value;
        currentPage = 1;
        renderInvoiceTable();
    });

    document.getElementById("pageSizeSelect").addEventListener("change", (event) => {
        pageSize = Number(event.target.value);
        currentPage = 1;
        renderInvoiceTable();
    });

    document.getElementById("previousPageButton").addEventListener("click", () => {
        currentPage = Math.max(1, currentPage - 1);
        renderInvoiceTable();
    });

    document.getElementById("nextPageButton").addEventListener("click", () => {
        currentPage += 1;
        renderInvoiceTable();
    });
}

function wireLoginButtons() {
    const connect = () => {
        const url = loginURL();
        if (!url) {
            window.alert("Unable to start the Xero login flow.");
            return;
        }
        window.location.href = url;
    };

    document.getElementById("connectXeroButton").addEventListener("click", connect);
}

function wireSyncButtons() {
    const sync = async () => {
        if (!isXeroConnected()) {
            renderChrome();
            return;
        }
        state.organisation.lastSync = "Sync requested just now";
        renderChrome();
        persistState();

        try {
            await requestJSON(api.endpoints.sync, { method: "POST" });
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to sync panel data", error);
        }
    };

    document.getElementById("primarySyncButton").addEventListener("click", sync);
    document.getElementById("sidebarSyncButton").addEventListener("click", sync);
}

function wireForms() {
    document.getElementById("noteForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const invoice = findInvoiceById(selectedInvoiceId);
        const body = document.getElementById("noteInput").value.trim();
        if (!invoice || !body) {
            return;
        }

        invoice.notes = [{ title: "Internal note", body, stamp: new Date().toISOString() }, ...(invoice.notes || [])];
        document.getElementById("noteInput").value = "";
        persistState();
        renderDetailPanel();
        renderInvoiceTable();

        try {
            await requestJSON(api.endpoints.note, {
                method: "POST",
                body: JSON.stringify({ body })
            }, { invoiceId: invoice.id });
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save note", error);
        }
    });

    document.getElementById("promiseForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const invoice = findInvoiceById(selectedInvoiceId);
        const promisedAmount = document.getElementById("promiseAmount").value.trim();
        const promisedDate = document.getElementById("promiseDate").value;
        const note = document.getElementById("promiseNote").value.trim();
        if (!invoice || !promisedAmount || !promisedDate) {
            return;
        }

        invoice.promises = [{
            title: `Promise for ${formatCurrency(promisedAmount)}`,
            body: note,
            stamp: promisedDate
        }, ...(invoice.promises || [])];
        invoice.promisedDate = promisedDate;
        document.getElementById("promiseAmount").value = "";
        document.getElementById("promiseDate").value = "";
        document.getElementById("promiseNote").value = "";
        persistState();
        renderDetailPanel();

        try {
            await requestJSON(api.endpoints.promise, {
                method: "POST",
                body: JSON.stringify({ promisedAmount, promisedDate, note })
            }, { invoiceId: invoice.id });
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save promise", error);
        }
    });

    document.getElementById("statusForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const invoice = findInvoiceById(selectedInvoiceId);
        const statusValue = document.getElementById("statusSelect").value;
        const note = document.getElementById("statusNote").value.trim();
        if (!invoice) {
            return;
        }

        invoice.controlStatus = statusValue;
        invoice.statuses = [{
            title: statusValue,
            body: note,
            stamp: new Date().toISOString()
        }, ...(invoice.statuses || [])];
        document.getElementById("statusNote").value = "";
        persistState();
        renderAll();

        try {
            await requestJSON(api.endpoints.status, {
                method: "POST",
                body: JSON.stringify({ statusValue, note })
            }, { invoiceId: invoice.id });
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save status", error);
        }
    });
}

async function init() {
    await hydrateFromAPI();
    renderAll();
    wireFilters();
    wireLoginButtons();
    wireSyncButtons();
    wireForms();
}

init();
