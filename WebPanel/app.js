const STORAGE_KEY = "hymn-credit-control-ledger";
const STORAGE_PREFIXES = [STORAGE_KEY, "creditControl.", "jenius-"];
const CLIENT_MATCH_STORAGE_KEY = "hymn-credit-control-client-matches";
const DEFAULT_API_BASE_URL = window.location.origin;
const api = normaliseAPI(window.HYMN_PANEL_API || {});
const emptyData = {
    organisation: { name: "", status: "Awaiting live connection", lastSync: "Waiting for first sync", xeroConnected: false },
    dashboard: { totalReceivables: 0, totalOverdue: 0, openInvoices: 0, accountsNeedingAction: 0, potentialInterest: 0 },
    panelSummary: {
        counts: { all: 0, paid: 0, query: 0, overdue: 0, court: 0, badDebt: 0 },
        totals: { all: 0, paid: 0, query: 0, overdue: 0, court: 0, badDebt: 0 }
    },
    customers: [],
    audit: [],
    selectedInvoice: null
};
const state = loadState();
const initialStoredMatches = loadClientMatches();
state.customers = (state.customers || [])
    .map((customer) => normaliseCustomerIntegrations(customer))
    .map((customer) => applyStoredMatch(customer, initialStoredMatches[customer.id]));
let authRedirectScheduled = false;
let selectedFilter = "all";
let selectedInvoiceId = state.selectedInvoice?.id || null;
let selectedClientId = null;
let activeView = "ledger";
let searchTerm = "";
let matchingSearchTerm = "";
let clientFilter = "all";
let sortMode = "priority";
let pageSize = 25;
let currentPage = 1;
let searchRenderFrame = null;
let clientProfileBaseline = null;
let clientProfileDraft = null;
let clientProfileClientId = null;
let clientProfileSaving = false;
let persistStateTimer = null;
let persistClientMatchesTimer = null;
let searchDebounceTimer = null;
const PERSIST_DEBOUNCE_MS = 180;
const SEARCH_DEBOUNCE_MS = 90;
const currencyFormatter = new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: "GBP",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
});
const dateFormatter = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" });
const compactDecimalFormatter = new Intl.NumberFormat("en-GB", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1
});
const confirmationState = {
    clientId: "",
    companyNumber: "",
    company: null,
    detail: null,
    loading: false,
    error: "",
    filingInProgress: false,
    bulkJobId: "",
    bulkJobPollTimer: null,
};

function installNonBlockingBrowserDialogOverrides() {
    if (window.__nonBlockingDialogsInstalled) return;
    window.__nonBlockingDialogsInstalled = true;
    window.alert = (message) => {
        const text = String(message || "Notice").trim() || "Notice";
        if (typeof showToast === "function") showToast(text, "error");
        else console.warn(text);
    };
    window.confirm = (message) => {
        const text = String(message || "Action requested").trim() || "Action requested";
        if (typeof showToast === "function") showToast(text, "info");
        else console.warn(text);
        return true;
    };
}
installNonBlockingBrowserDialogOverrides();

const sortLabels = { priority: "Priority", due: "Due date", amount: "Amount due", client: "Client" };
const filterLabels = { all: "All invoices", action: "Needs action", current: "Current only" };

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
            bulkStatus: config.endpoints?.bulkStatus || "/api/invoices/bulk-status",
            customerProfile: config.endpoints?.customerProfile || "/api/customers/:customerId/profile",
            companiesHouseCompanies: config.endpoints?.companiesHouseCompanies || "/api/companies-house/companies",
            companiesHouseCompanyDetail: config.endpoints?.companiesHouseCompanyDetail || "/api/companies-house/companies/:companyId",
            companiesHouseBulkSubmit: config.endpoints?.companiesHouseBulkSubmit || "/api/companies-house/submissions/bulk",
            companiesHouseBulkJob: config.endpoints?.companiesHouseBulkJob || "/api/companies-house/submissions/bulk-jobs/:jobId",
            login: config.endpoints?.login || "/auth/xero/start?include_all_scopes=1"
        }
    };
}

function normaliseState(payload) {
    return {
        organisation: payload.organisation || emptyData.organisation,
        dashboard: payload.dashboard || emptyData.dashboard,
        panelSummary: payload.panelSummary || emptyData.panelSummary,
        customers: Array.isArray(payload.customers) ? payload.customers : [],
        audit: Array.isArray(payload.audit) ? payload.audit : [],
        selectedInvoice: payload.selectedInvoice || null
    };
}

function loadState() {
    const injected = normaliseState(window.HYMN_PANEL_DATA || emptyData);
    return injected;
}

function persistState() {
    if (persistStateTimer !== null) window.clearTimeout(persistStateTimer);
    persistStateTimer = window.setTimeout(() => {
        persistStateTimer = null;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }, PERSIST_DEBOUNCE_MS);
}

function loadClientMatches() {
    try {
        const raw = localStorage.getItem(CLIENT_MATCH_STORAGE_KEY);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
        return {};
    }
}

function persistClientMatches() {
    if (persistClientMatchesTimer !== null) window.clearTimeout(persistClientMatchesTimer);
    persistClientMatchesTimer = window.setTimeout(() => {
        persistClientMatchesTimer = null;
        const payload = {};
        state.customers.forEach((customer) => {
            if (!customer?.id) return;
            payload[customer.id] = {
                xeroOrganisationId: customer.xeroOrganisationId || "",
                ignitionClientId: customer.ignitionClientId || "",
                xeroConnected: isTruthyConnectionFlag(customer.xeroConnected),
                ignitionConnected: isTruthyConnectionFlag(customer.ignitionConnected)
            };
        });
        localStorage.setItem(CLIENT_MATCH_STORAGE_KEY, JSON.stringify(payload));
    }, PERSIST_DEBOUNCE_MS);
}

function isTruthyConnectionFlag(value) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value > 0;
    if (typeof value === "string") {
        const normalised = value.trim().toLowerCase();
        return ["connected", "active", "true", "yes", "1"].includes(normalised);
    }
    return false;
}

function clearSensitiveState() {
    state.organisation = { ...emptyData.organisation };
    state.dashboard = { ...emptyData.dashboard };
    state.customers = [];
    state.audit = [];
    state.selectedInvoice = null;
    selectedInvoiceId = null;
    selectedClientId = null;
    activeView = "ledger";
    currentPage = 1;
    searchTerm = "";
    clearSensitiveStorage();
}

function clearSensitiveStorage() {
    try {
        if (searchDebounceTimer !== null) window.clearTimeout(searchDebounceTimer);
        if (searchRenderFrame !== null) window.cancelAnimationFrame(searchRenderFrame);
        searchDebounceTimer = null;
        searchRenderFrame = null;
        if (persistStateTimer !== null) window.clearTimeout(persistStateTimer);
        if (persistClientMatchesTimer !== null) window.clearTimeout(persistClientMatchesTimer);
        persistStateTimer = null;
        persistClientMatchesTimer = null;
        const keys = [];
        for (let index = 0; index < localStorage.length; index += 1) {
            const key = localStorage.key(index);
            if (!key) continue;
            const shouldRemove = STORAGE_PREFIXES.some((prefix) => key.startsWith(prefix));
            if (shouldRemove) keys.push(key);
        }
        keys.forEach((key) => localStorage.removeItem(key));
        localStorage.removeItem(CLIENT_MATCH_STORAGE_KEY);
    } catch {
        localStorage.removeItem(STORAGE_KEY);
        localStorage.removeItem(CLIENT_MATCH_STORAGE_KEY);
    }
}

function replaceState(next) {
    const preservedInvoiceId = selectedInvoiceId;
    const localClientNotes = new Map(state.customers.map((customer) => [customer.id, customer.clientNotes || []]));
    const localMatches = new Map(state.customers.map((customer) => [customer.id, {
        xeroOrganisationId: customer.xeroOrganisationId || "",
        ignitionClientId: customer.ignitionClientId || "",
        xeroConnected: isTruthyConnectionFlag(customer.xeroConnected),
        ignitionConnected: isTruthyConnectionFlag(customer.ignitionConnected)
    }]));
    const storedMatches = loadClientMatches();
    state.organisation = next.organisation;
    state.dashboard = next.dashboard;
    state.panelSummary = next.panelSummary || emptyData.panelSummary;
    state.customers = next.customers.map((customer) => ({
        ...normaliseCustomerIntegrations(customer),
        clientNotes: customer.clientNotes || localClientNotes.get(customer.id) || []
    })).map((customer) => applyStoredMatch(customer, storedMatches[customer.id] || localMatches.get(customer.id)));
    state.audit = next.audit;
    state.selectedInvoice = next.selectedInvoice;
    const nextInvoiceId = next.selectedInvoice?.id || next.selectedInvoice?.invoiceId;
    const invoice = findInvoiceById(preservedInvoiceId) || findInvoiceById(nextInvoiceId) || allInvoices()[0] || null;
    selectedInvoiceId = invoice?.id || null;
    selectedClientId = invoice?.customerId || selectedClientId;
    persistState();
    persistClientMatches();
}

function normaliseCustomerIntegrations(customer) {
    const integrations = customer?.integrations || {};
    const xero = integrations.xero || customer?.xero || {};
    const ignition = integrations.ignition || customer?.ignition || {};
    const xeroOrganisationId = customer?.xeroOrganisationId
        || customer?.xero_org_id
        || customer?.xeroTenantId
        || customer?.xero_tenant_id
        || xero.organisationId
        || xero.orgId
        || xero.tenantId
        || "";
    const ignitionClientId = customer?.ignitionClientId
        || customer?.ignition_client_id
        || ignition.clientId
        || ignition.id
        || "";
    const xeroConnected = isTruthyConnectionFlag(customer?.xeroConnected)
        || isTruthyConnectionFlag(customer?.xero_connected)
        || isTruthyConnectionFlag(xero?.connected)
        || Boolean(xeroOrganisationId);
    const ignitionConnected = isTruthyConnectionFlag(customer?.ignitionConnected)
        || isTruthyConnectionFlag(customer?.ignition_connected)
        || isTruthyConnectionFlag(ignition?.connected)
        || Boolean(ignitionClientId);
    return {
        ...customer,
        xeroOrganisationId,
        ignitionClientId,
        xeroConnected,
        ignitionConnected
    };
}

function applyStoredMatch(customer, stored) {
    if (!stored) return customer;
    const xeroOrganisationId = stored.xeroOrganisationId || customer.xeroOrganisationId || "";
    const ignitionClientId = stored.ignitionClientId || customer.ignitionClientId || "";
    const xeroConnected = isTruthyConnectionFlag(stored.xeroConnected) || Boolean(xeroOrganisationId) || customer.xeroConnected === true;
    const ignitionConnected = isTruthyConnectionFlag(stored.ignitionConnected) || Boolean(ignitionClientId) || customer.ignitionConnected === true;
    return {
        ...customer,
        xeroOrganisationId,
        ignitionClientId,
        xeroConnected,
        ignitionConnected
    };
}

function endpointURL(template, params = {}) {
    const path = Object.entries(params).reduce((accumulator, [key, value]) => accumulator.replace(`:${key}`, encodeURIComponent(value)), template);
    return `${api.baseUrl}${path}`;
}

function loginURL() {
    const loginEndpoint = endpointURL(api.endpoints.login);
    const isHostedPage = window.location.protocol === "https:" || window.location.protocol === "http:";
    if (!isHostedPage) return loginEndpoint;
    const separator = api.endpoints.login.includes("?") ? "&" : "?";
    return `${loginEndpoint}${separator}redirect_to=${encodeURIComponent(window.location.href)}`;
}

class AuthRequiredError extends Error {}

function responsePath(url) {
    try {
        return new URL(url).pathname;
    } catch {
        return "";
    }
}

async function requestJSON(template, options = {}, params = {}) {
    const response = await fetch(endpointURL(template, params), {
        credentials: "include",
        ...options,
        headers: { "Content-Type": "application/json", ...api.headers, ...(options.headers || {}) }
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
        ? await response.json().catch(() => null)
        : await response.text().catch(() => "");
    const redirectedToLogin = response.redirected && responsePath(response.url) === "/login";
    if (redirectedToLogin || response.status === 401 || response.status === 403) {
        const detail = payload && typeof payload === "object" ? payload.detail : payload;
        const message = typeof detail === "string"
            ? detail
            : detail?.message || "Your session has expired. Sign in with Xero again before syncing.";
        throw new AuthRequiredError(message);
    }
    if (!response.ok) {
        const detail = payload && typeof payload === "object" ? payload.detail : payload;
        const message = typeof detail === "string"
            ? detail
            : detail?.message || payload?.message || `API request failed with status ${response.status}`;
        throw new Error(message);
    }
    return contentType.includes("application/json") ? payload : null;
}

function markAuthenticationRequired(message) {
    clearSensitiveState();
    state.organisation.status = "Sign in required";
    state.organisation.lastSync = "Redirecting to login…";
    renderAll();
    console.warn(message || "Sign in with Xero again before syncing.");
    if (!authRedirectScheduled) {
        authRedirectScheduled = true;
        window.setTimeout(() => {
            window.location.replace("/login");
        }, 80);
    }
}

async function hydrateFromAPI() {
    try {
        const payload = await requestJSON(api.endpoints.panel);
        if (payload) replaceState(normaliseState(payload));
    } catch (error) {
        if (error instanceof AuthRequiredError) {
            markAuthenticationRequired(error.message);
            return;
        }
        console.error("Unable to load panel data from API", error);
    }
}

function decorateInvoice(customer, invoice) {
    const notesSummary = noteSnippet(invoice);
    const description = invoice.description || invoice.invoiceNumber || "Xero invoice";
    const category = invoiceCategory(invoice);
    const dueSortValue = dateSortValue(invoice.dueDate);
    const searchableText = [
        customer.name,
        customer.contact,
        invoice.invoiceNumber,
        description,
        notesSummary,
        invoice.status,
        invoice.controlStatus
    ].join(" ").toLowerCase();
    return {
        ...invoice,
        customerId: customer.id,
        customerName: customer.name,
        customerContact: customer.contact || "",
        customerStatus: customer.status || "",
        manager: customer.manager || "Unassigned",
        category,
        dueSortValue,
        description,
        notesSummary,
        searchableText,
    };
}

function allInvoices() {
    return state.customers.flatMap((customer) => (customer.invoices || []).map((invoice) => decorateInvoice(customer, invoice)));
}

function findCustomerById(customerId) {
    if (!customerId) return null;
    return state.customers.find((customer) => customer.id === customerId) || null;
}

function findCustomerByInvoiceId(invoiceId) {
    if (!invoiceId) return null;
    return state.customers.find((customer) => (customer.invoices || []).some((invoice) => invoice.id === invoiceId)) || null;
}

function mutableInvoiceById(invoiceId) {
    const customer = findCustomerByInvoiceId(invoiceId);
    const invoice = customer ? (customer.invoices || []).find((item) => item.id === invoiceId) : null;
    return invoice ? { customer, invoice } : null;
}

function clientInvoices(customer) {
    return customer ? (customer.invoices || []).map((invoice) => decorateInvoice(customer, invoice)) : [];
}

function findInvoiceById(invoiceId) {
    const match = mutableInvoiceById(invoiceId);
    return match ? decorateInvoice(match.customer, match.invoice) : null;
}

function invoiceCategory(invoice) {
    const cachedCategory = String(invoice?.category || "").trim().toLowerCase();
    if (["paid", "outstanding", "query", "overdue", "court", "bad-debt"].includes(cachedCategory)) {
        return cachedCategory;
    }
    const control = `${invoice.controlStatus || invoice.status || ""}`.toLowerCase();
    const amountDue = Number(invoice.amountDue || 0);
    const dueDate = invoice.dueDate ? new Date(invoice.dueDate) : null;
    const now = new Date();
    if (amountDue <= 0 || control.includes("paid")) return "paid";
    if (control.includes("bad debt") || control.includes("bad-debt") || control.includes("bad_debt")) return "bad-debt";
    if (control.includes("court") || control.includes("legal")) return "court";
    if (control.includes("query") || control.includes("queried") || control.includes("dispute") || control.includes("disputed")) return "query";
    if (amountDue > 0 && dueDate && dueDate < now) return "overdue";
    return "outstanding";
}

function invoiceStatusLabel(invoice) {
    const category = invoiceCategory(invoice);
    return { paid: "Paid", outstanding: "Outstanding", query: "Query", overdue: "Overdue", court: "Legal", "bad-debt": "Bad debt" }[category];
}

function formatCurrency(value) {
    return currencyFormatter.format(Number(value || 0));
}

function formatDate(value) {
    if (!value) return "--";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return dateFormatter.format(date);
}

function invoicePayments(invoice) {
    const customer = findCustomerById(invoice?.customerId);
    if (!customer || !Array.isArray(customer.payments)) return [];
    const invoiceId = String(invoice?.id || "").trim();
    const xeroInvoiceId = String(invoice?.xeroInvoiceId || "").trim();
    const invoiceNumber = String(invoice?.invoiceNumber || "").trim().toLowerCase();
    const matches = customer.payments.filter((payment) => {
        const paymentInvoiceId = String(payment?.invoiceId || "").trim();
        const paymentXeroInvoiceId = String(payment?.xeroInvoiceId || "").trim();
        const paymentInvoiceNumber = String(payment?.invoiceNumber || "").trim().toLowerCase();
        return (invoiceId && paymentInvoiceId === invoiceId)
            || (xeroInvoiceId && paymentXeroInvoiceId === xeroInvoiceId)
            || (invoiceNumber && paymentInvoiceNumber === invoiceNumber);
    });
    return matches.sort((a, b) => dateSortValue(b?.date) - dateSortValue(a?.date));
}

function paymentChannelLabel(payment) {
    const accountName = String(payment?.accountName || "").toLowerCase();
    const reference = String(payment?.reference || "").toLowerCase();
    const statusText = String(payment?.status || "").toLowerCase();
    const combined = `${accountName} ${reference} ${statusText}`;

    if (combined.includes("stripe")) return "Stripe";
    if (combined.includes("juk trading") || combined.includes("trading account") || combined.includes("jaccountancy trading")) {
        return "JUK Trading Account";
    }

    const ignitionHint = combined.includes("ignition") || combined.includes("pi clearing") || combined.includes("pi-clearing");
    if (ignitionHint && (combined.includes("direct debit") || /\bdd\b/.test(combined))) return "Ignition (direct debit)";
    if (ignitionHint && combined.includes("card")) return "Ignition (card)";
    if (ignitionHint) return "Ignition (direct debit)";

    if (combined.includes("direct debit")) return "Ignition (direct debit)";
    if (combined.includes("card")) return "Ignition (card)";

    return "JUK Trading Account";
}

function paidInvoiceHoverText(invoice) {
    const payments = invoicePayments(invoice);
    const latestPayment = payments[0] || null;
    const paidOnValue = latestPayment?.date || invoice?.paidDate || invoice?.paidAt || invoice?.invoiceDate || invoice?.dueDate;
    const paidOnLabel = formatDate(paidOnValue);
    const channelLabel = paymentChannelLabel(latestPayment);
    if (paidOnLabel && paidOnLabel !== "--") return `Paid on ${paidOnLabel} via ${channelLabel}`;
    return `Paid via ${channelLabel}`;
}

function noteSnippet(invoice) {
    if (invoice.notes?.length) return invoice.notes[0].body || invoice.notes[0].title || "";
    if (invoice.statuses?.length) return invoice.statuses[0].body || "";
    return "";
}

function descriptionGlyph(invoice) {
    const category = invoiceCategory(invoice);
    return { paid: "✓", outstanding: "◷", query: "◌?", overdue: "⚠", court: "⚖", "bad-debt": "✕" }[category];
}

function statusSelectValue(invoice) {
    if (!invoice) return "Outstanding";
    const value = invoice.controlStatus || invoiceStatusLabel(invoice) || "Outstanding";
    return `${value}`.toLowerCase().includes("court") ? "Legal" : value;
}

function profileFallbackAddress(client) {
    if (client?.clientProfile?.address) return client.clientProfile.address;
    const addresses = Array.isArray(client?.addresses) ? client.addresses : [];
    const first = addresses.find((item) => item && typeof item === "object");
    if (!first) return "";
    return [
        first.address_line_1,
        first.address_line_2,
        first.city,
        first.region,
        first.postal_code,
        first.country,
    ].filter(Boolean).join(", ");
}

function defaultClientProfile(client) {
    const source = client?.clientProfile || {};
    return {
        clientName: source.clientName || client?.name || "",
        clientManager: source.clientManager || client?.manager || "",
        clientId: source.clientId || "",
        clientType: source.clientType || "",
        companyNumber: source.companyNumber || "",
        companyStatus: source.companyStatus || client?.status || "",
        companyUtr: source.companyUtr || "",
        vatNumber: source.vatNumber || "",
        email: source.email || client?.email || "",
        phone: source.phone || client?.phone || "",
        authCode: source.authCode || "",
        address: source.address || profileFallbackAddress(client),
    };
}

function profileDirty() {
    return JSON.stringify(clientProfileDraft || {}) !== JSON.stringify(clientProfileBaseline || {});
}

function setSaveButtonState() {
    const button = document.getElementById("saveClientChangesButton");
    if (!button) return;
    const dirty = profileDirty();
    button.hidden = !dirty;
    button.disabled = clientProfileSaving || !dirty;
    button.textContent = clientProfileSaving ? "Saving changes…" : "Save Changes";
}

function initials(value) {
    const chars = String(value || "")
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((part) => part[0]?.toUpperCase() || "")
        .join("");
    return chars || "•";
}

function renderPeopleStack(targetId, rows, roleKey) {
    const target = document.getElementById(targetId);
    if (!target) return;
    const list = Array.isArray(rows) ? rows.filter((row) => row?.name) : [];
    target.innerHTML = "";
    if (!list.length) {
        target.innerHTML = `<p class="group-empty">No records</p>`;
        return;
    }
    list.slice(0, 6).forEach((row) => {
        const item = document.createElement("article");
        item.className = "people-row";
        item.innerHTML = `
            <span class="person-avatar">${initials(row.name)}</span>
            <div>
                <strong>${row.name}</strong>
                <small>${row[roleKey] || "On record"}</small>
            </div>
        `;
        target.appendChild(item);
    });
}

function companiesHouseURL(companyNumber) {
    const normalised = String(companyNumber || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!normalised) return "";
    return `https://find-and-update.company-information.service.gov.uk/company/${encodeURIComponent(normalised)}`;
}

function renderClientProfilePanel(client) {
    if (!client) return;
    if (clientProfileClientId !== client.id) {
        clientProfileClientId = client.id;
        clientProfileBaseline = defaultClientProfile(client);
        clientProfileDraft = { ...clientProfileBaseline };
        clientProfileSaving = false;
    }
    const mapping = {
        clientProfileName: "clientName",
        clientProfileManager: "clientManager",
        clientProfileId: "clientId",
        clientProfileType: "clientType",
        clientProfileCompanyNumber: "companyNumber",
        clientProfileCompanyStatus: "companyStatus",
        clientProfileCompanyUtr: "companyUtr",
        clientProfileVatNumber: "vatNumber",
        clientProfileEmail: "email",
        clientProfilePhone: "phone",
        clientProfileAuthCode: "authCode",
        clientProfileAddress: "address",
    };
    Object.entries(mapping).forEach(([id, key]) => {
        const input = document.getElementById(id);
        if (!input) return;
        input.value = clientProfileDraft?.[key] || "";
    });
    const structure = client.companyStructure || {};
    const meta = document.getElementById("groupStructureMeta");
    if (meta) {
        const source = structure.source === "companies_house" ? "Synced from Companies House" : "Not synced with Companies House";
        const stamp = structure.syncedAt ? ` · ${formatDate(structure.syncedAt)}` : "";
        meta.textContent = `${source}${stamp}`;
    }
    const companiesHouseButton = document.getElementById("openCompaniesHouseButton");
    if (companiesHouseButton) {
        const url = companiesHouseURL(clientProfileDraft?.companyNumber || "");
        companiesHouseButton.hidden = !url;
        companiesHouseButton.href = url || "#";
    }
    renderPeopleStack("groupDirectors", structure.directors || [], "role");
    renderPeopleStack("groupShareholders", structure.shareholders || [], "holding");
    renderPeopleStack("groupPscs", structure.pscs || [], "kind");
    setSaveButtonState();
}

function managerValues() {
    return [...new Set(state.customers.map((customer) => customer.manager || "Unassigned"))];
}

function filteredInvoices(invoices = allInvoices()) {
    const searchTermLower = searchTerm.trim().toLowerCase();
    return invoices.filter((invoice) => {
        const category = invoice.category || invoiceCategory(invoice);
        const matchesFilter = selectedFilter === "all" || category === selectedFilter;
        const matchesSearch = (invoice.searchableText || "").includes(searchTermLower);
        const needsAction = ["outstanding", "query", "overdue", "court", "bad-debt"].includes(category);
        const matchesClient = clientFilter === "all" || (clientFilter === "action" ? needsAction : !needsAction);
        return matchesFilter && matchesSearch && matchesClient;
    }).sort(compareInvoices);
}

function compareInvoices(a, b) {
    const categoryPriority = { overdue: 0, court: 1, query: 2, outstanding: 3, "bad-debt": 4, paid: 5 };
    const aCategory = a.category || invoiceCategory(a);
    const bCategory = b.category || invoiceCategory(b);
    const aDueSortValue = a.dueSortValue ?? dateSortValue(a.dueDate);
    const bDueSortValue = b.dueSortValue ?? dateSortValue(b.dueDate);
    if (sortMode === "amount") return Number(b.amountDue || 0) - Number(a.amountDue || 0);
    if (sortMode === "client") return `${a.customerName || ""}`.localeCompare(`${b.customerName || ""}`);
    if (sortMode === "due") return aDueSortValue - bDueSortValue;
    return (categoryPriority[aCategory] ?? 9) - (categoryPriority[bCategory] ?? 9)
        || aDueSortValue - bDueSortValue;
}

function dateSortValue(value) {
    if (!value) return Number.MAX_SAFE_INTEGER;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? Number.MAX_SAFE_INTEGER : date.getTime();
}

function formatCompactCurrency(value) {
    const amount = Number(value || 0);
    const absolute = Math.abs(amount);
    const sign = amount < 0 ? "-" : "";
    if (absolute >= 1000000) return `${sign}£${compactDecimalFormatter.format(Math.trunc((absolute / 1000000) * 10) / 10)}M`;
    if (absolute >= 1000) return `${sign}£${Math.trunc(absolute / 1000).toLocaleString("en-GB")}K`;
    return `${sign}${formatCurrency(absolute).replace(".00", "")}`;
}

function renderSummaryCounts(invoices = allInvoices()) {
    const panelSummary = state.panelSummary || {};
    const summaryCounts = panelSummary.counts || {};
    const summaryTotals = panelSummary.totals || {};
    const hasPrecomputedSummary = typeof summaryCounts.all === "number" && typeof summaryTotals.all === "number";
    const counts = {
        all: hasPrecomputedSummary ? Number(summaryCounts.all || 0) : invoices.length,
        paid: hasPrecomputedSummary ? Number(summaryCounts.paid || 0) : 0,
        query: hasPrecomputedSummary ? Number(summaryCounts.query || 0) : 0,
        overdue: hasPrecomputedSummary ? Number(summaryCounts.overdue || 0) : 0,
        court: hasPrecomputedSummary ? Number(summaryCounts.court || 0) : 0,
        badDebt: hasPrecomputedSummary ? Number(summaryCounts.badDebt || 0) : 0,
    };
    const totals = {
        all: hasPrecomputedSummary ? Number(summaryTotals.all || 0) : 0,
        paid: hasPrecomputedSummary ? Number(summaryTotals.paid || 0) : 0,
        query: hasPrecomputedSummary ? Number(summaryTotals.query || 0) : 0,
        overdue: hasPrecomputedSummary ? Number(summaryTotals.overdue || 0) : 0,
        court: hasPrecomputedSummary ? Number(summaryTotals.court || 0) : 0,
        badDebt: hasPrecomputedSummary ? Number(summaryTotals.badDebt || 0) : 0,
    };
    if (!hasPrecomputedSummary) {
        invoices.forEach((invoice) => {
            const category = invoice.category || invoiceCategory(invoice);
            const amountDue = Number(invoice.amountDue || 0);
            totals.all += amountDue;
            if (category === "paid") {
                counts.paid += 1;
                totals.paid += Number(invoice.total || 0);
            } else if (category === "query") {
                counts.query += 1;
                totals.query += amountDue;
            } else if (category === "overdue") {
                counts.overdue += 1;
                totals.overdue += amountDue;
            } else if (category === "court") {
                counts.court += 1;
                totals.court += amountDue;
            } else if (category === "bad-debt") {
                counts.badDebt += 1;
                totals.badDebt += amountDue;
            }
        });
    }
    document.getElementById("countAll").textContent = counts.all.toLocaleString("en-GB");
    document.getElementById("countPaid").textContent = counts.paid.toLocaleString("en-GB");
    document.getElementById("countQuery").textContent = counts.query.toLocaleString("en-GB");
    document.getElementById("countOverdue").textContent = counts.overdue.toLocaleString("en-GB");
    document.getElementById("countCourt").textContent = counts.court.toLocaleString("en-GB");
    document.getElementById("countBadDebt").textContent = counts.badDebt.toLocaleString("en-GB");
    document.getElementById("valueAll").textContent = formatCompactCurrency(totals.all);
    document.getElementById("valuePaid").textContent = formatCompactCurrency(totals.paid);
    document.getElementById("valueQuery").textContent = formatCompactCurrency(totals.query);
    document.getElementById("valueOverdue").textContent = formatCompactCurrency(totals.overdue);
    document.getElementById("valueCourt").textContent = formatCompactCurrency(totals.court);
    document.getElementById("valueBadDebt").textContent = formatCompactCurrency(totals.badDebt);
    document.querySelectorAll(".summary-chip").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.filter === selectedFilter);
    });
}

function renderToolbarControls() {
    document.getElementById("sortButton").textContent = `Sort: ${sortLabels[sortMode]}`;
    document.getElementById("filterButton").textContent = `Filter: ${filterLabels[clientFilter]}`;
    document.querySelectorAll("[data-sort-mode]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.sortMode === sortMode);
    });
    document.querySelectorAll("[data-filter-mode]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.filterMode === clientFilter);
    });
}

function renderInvoiceTable(invoices = allInvoices()) {
    const tbody = document.getElementById("invoiceTableBody");
    const filtered = filteredInvoices(invoices);
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    currentPage = Math.min(currentPage, pages);
    const start = (currentPage - 1) * pageSize;
    const paged = filtered.slice(start, start + pageSize);
    tbody.innerHTML = "";
    if (!paged.length) {
        const row = document.createElement("tr");
        row.innerHTML = `<td colspan="8"><div class="empty-state"><p class="eyebrow">No invoices</p><h4>No ledger rows match the current filters</h4><p>Connect Xero and sync live data, or widen the search and filter settings.</p></div></td>`;
        tbody.appendChild(row);
    } else {
        const fragment = document.createDocumentFragment();
        paged.forEach((invoice) => {
            const category = invoice.category || invoiceCategory(invoice);
            const paidHoverText = category === "paid" ? paidInvoiceHoverText(invoice) : "";
            const paidDateLabel = category === "paid"
                ? paidHoverText.replace(/^Paid on\s*/i, "")
                : category === "overdue"
                    ? "Payment overdue"
                    : formatDate(invoice.promisedDate || invoice.dueDate);
            const row = document.createElement("tr");
            row.className = invoice.id === selectedInvoiceId ? "is-selected" : "";
            row.dataset.invoiceId = invoice.id || "";
            row.dataset.customerId = invoice.customerId || "";
            row.innerHTML = `
                <td><div class="client-name">${invoice.customerName || "Unnamed client"}</div><div class="client-subline">${invoice.customerContact || invoice.customerId || ""}</div></td>
                <td><div class="client-name">${invoice.invoiceNumber || "--"}</div><div class="invoice-subline">${invoice.id || ""}</div></td>
                <td><div class="description-cell"><div class="description-icon">${descriptionGlyph(invoice)}</div><div><div class="description-title">${invoice.description || "Xero invoice"}</div><div class="invoice-subline">${invoice.customerStatus || "Live Xero contact"}</div></div></div></td>
                <td class="amount-cell">${formatCurrency(invoice.total || invoice.amountDue || 0)}</td>
                <td><div class="payment-cell"><div class="payment-status"${paidHoverText ? ` title="${escapeHTML(paidHoverText)}"` : ""}><span class="payment-dot ${category}">${category === "paid" ? "✓" : category === "court" ? "!" : "○"}</span><span class="paid-amount ${category === "paid" ? "is-paid" : "is-outstanding"}">${formatCurrency((invoice.total || 0) - (invoice.amountDue || 0))}</span></div><div class="paid-date">${paidDateLabel}</div></div></td>
                <td><span class="status-pill ${category}">${invoiceStatusLabel(invoice)}</span></td>
                <td><div class="note-snippet">${invoice.notesSummary || "No notes yet."}</div></td>
                <td><button class="row-menu" type="button">⋮</button></td>
            `;
            fragment.appendChild(row);
        });
        tbody.appendChild(fragment);
    }
    renderPagination(filtered.length, pages, start, paged.length);
}

function renderPagination(totalResults, totalPages, start, count) {
    document.getElementById("resultsText").textContent = totalResults ? `Showing ${start + 1} to ${start + count} of ${totalResults.toLocaleString("en-GB")} results` : "Showing 0 results";
    document.getElementById("previousPageButton").disabled = currentPage <= 1;
    document.getElementById("nextPageButton").disabled = currentPage >= totalPages;
    const target = document.getElementById("pagePills");
    target.innerHTML = "";
    const fragment = document.createDocumentFragment();
    const pages = [];
    for (let page = 1; page <= totalPages; page += 1) {
        if (page <= 3 || page > totalPages - 2 || Math.abs(page - currentPage) <= 1) pages.push(page);
    }
    [...new Set(pages)].forEach((page) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `page-pill${page === currentPage ? " is-active" : ""}`;
        button.textContent = String(page);
        button.dataset.page = String(page);
        fragment.appendChild(button);
    });
    target.appendChild(fragment);
}

function renderClientScreen() {
    const ledgerView = document.getElementById("ledgerView");
    const clientScreen = document.getElementById("clientScreen");
    const settingsScreen = document.getElementById("settingsScreen");
    ledgerView.hidden = activeView !== "ledger";
    clientScreen.hidden = activeView !== "client";
    settingsScreen.hidden = activeView !== "settings";
    if (activeView !== "client") return;

    const invoice = findInvoiceById(selectedInvoiceId);
    const client = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
    const invoices = clientInvoices(client);
    const selectedInvoice = invoice || invoices[0] || null;
    selectedInvoiceId = selectedInvoice?.id || null;
    selectedClientId = client?.id || selectedInvoice?.customerId || null;

    document.getElementById("clientTitle").textContent = client?.name || "No client selected";
    document.getElementById("clientMeta").textContent = client
        ? `${client.contact || "Xero contact"} · ${invoices.length} invoice${invoices.length === 1 ? "" : "s"} · ${client.manager || "Unassigned"}`
        : "Select an invoice from the ledger to open the client workspace.";

    const openInvoices = invoices.filter((item) => (item.category || invoiceCategory(item)) !== "paid");
    const totalDue = invoices.reduce((sum, item) => sum + Number(item.amountDue || 0), 0);
    const overdueCount = invoices.filter((item) => (item.category || invoiceCategory(item)) === "overdue").length;
    document.getElementById("clientStatusBadge").textContent = client ? `${openInvoices.length} open` : "Awaiting data";
    document.getElementById("clientStatusBadge").className = "status-badge";
    const xeroConnected = client ? resolveIntegrationConnection(client, "xero") : false;
    const ignitionConnected = client ? resolveIntegrationConnection(client, "ignition") : false;
    renderConnectionBadge("clientXeroStatusBadge", "Connected to Xero", xeroConnected);
    renderConnectionBadge("clientIgnitionStatusBadge", "Connected to Ignition", ignitionConnected);

    const stats = document.getElementById("clientStats");
    stats.innerHTML = "";
    [
        ["Total due", formatCurrency(totalDue)],
        ["Open invoices", `${openInvoices.length}`],
        ["Overdue", `${overdueCount}`],
        ["Selected invoice", selectedInvoice?.invoiceNumber || "--"],
    ].forEach(([label, value]) => {
        const card = document.createElement("article");
        card.className = "detail-stat";
        card.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
        stats.appendChild(card);
    });

    document.getElementById("interestInfoText").textContent = selectedInvoice
        ? `For ${selectedInvoice.invoiceNumber || "this invoice"}, court costs would be £${Number(selectedInvoice.latePayment?.court_cost || selectedInvoice.latePayment?.courtCost || 35).toFixed(0)} and statutory interest so far is £${Number(selectedInvoice.latePayment?.interest || 0).toFixed(2)}.`
        : "Statutory interest and court-cost guidance will appear here for the selected invoice.";

    renderClientProfilePanel(client);
    renderClientInvoicePicker(invoices);
    renderClientInvoiceList(invoices);
    renderTimeline("statusTimeline", clientStatusItems(invoices), { eyebrow: "No status history", title: "Status changes will appear here", body: "Bulk updates will build the client credit-control history." });
    renderTimeline("clientNotesTimeline", client?.clientNotes || [], { eyebrow: "No client notes", title: "Client notes will appear here", body: "Add notes for account-level calls, chasing updates and context." });
    renderTimeline("invoiceNotesTimeline", selectedInvoice?.notes || [], { eyebrow: "No invoice notes", title: "Invoice notes will appear here", body: "Select an invoice above, then add a note attached to that invoice." });
}

function renderSettingsScreen() {
    if (activeView !== "settings") return;
    const tbody = document.getElementById("matchingTableBody");
    if (!tbody) return;
    const term = matchingSearchTerm.trim().toLowerCase();
    const customers = state.customers.filter((customer) => {
        if (!term) return true;
        const haystack = [
            customer.name,
            customer.manager,
            customer.contact,
            customer.xeroOrganisationId,
            customer.ignitionClientId
        ].join(" ").toLowerCase();
        return haystack.includes(term);
    }).sort((a, b) => `${a.name || ""}`.localeCompare(`${b.name || ""}`));
    tbody.innerHTML = "";
    if (!customers.length) {
        const row = document.createElement("tr");
        row.innerHTML = `<td colspan="5"><p class="settings-empty">No clients match the current search.</p></td>`;
        tbody.appendChild(row);
        return;
    }
    customers.forEach((customer) => {
        const row = document.createElement("tr");
        const xeroConnected = resolveIntegrationConnection(customer, "xero");
        const ignitionConnected = resolveIntegrationConnection(customer, "ignition");
        row.innerHTML = `
            <td>
                <div class="settings-client-title">${customer.name || "Unnamed client"}</div>
                <div class="settings-client-meta">${customer.manager || "Unassigned"} · ${customer.contact || customer.id || ""}</div>
            </td>
            <td><span class="status-badge ${xeroConnected ? "connected" : "disconnected"}">${xeroConnected ? "Connected" : "Not connected"}</span></td>
            <td><span class="status-badge ${ignitionConnected ? "connected" : "disconnected"}">${ignitionConnected ? "Connected" : "Not connected"}</span></td>
            <td>
                <div class="settings-match-inputs">
                    <input type="text" data-field="xeroOrganisationId" value="${escapeHTML(customer.xeroOrganisationId || "")}" placeholder="Xero organisation ID">
                    <input type="text" data-field="ignitionClientId" value="${escapeHTML(customer.ignitionClientId || "")}" placeholder="Ignition client ID">
                </div>
            </td>
            <td><button class="settings-save-match" type="button">Save match</button></td>
        `;
        const saveButton = row.querySelector(".settings-save-match");
        saveButton.addEventListener("click", () => {
            const xeroInput = row.querySelector('[data-field="xeroOrganisationId"]');
            const ignitionInput = row.querySelector('[data-field="ignitionClientId"]');
            customer.xeroOrganisationId = xeroInput?.value.trim() || "";
            customer.ignitionClientId = ignitionInput?.value.trim() || "";
            customer.xeroConnected = Boolean(customer.xeroOrganisationId);
            customer.ignitionConnected = Boolean(customer.ignitionClientId);
            persistState();
            persistClientMatches();
            renderAll();
        });
        tbody.appendChild(row);
    });
}

function escapeHTML(value) {
    return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function renderConnectionBadge(targetId, label, connected) {
    const badge = document.getElementById(targetId);
    if (!badge) return;
    badge.className = `status-badge ${connected ? "connected" : "disconnected"}`;
    badge.textContent = `${label}: ${connected ? "Yes" : "No"}`;
}

function resolveIntegrationConnection(client, provider) {
    if (!client) return false;
    if (provider === "xero") return client.xeroConnected === true || Boolean(client.xeroOrganisationId);
    return client.ignitionConnected === true || Boolean(client.ignitionClientId);
}

function renderClientInvoicePicker(invoices) {
    const select = document.getElementById("clientInvoiceSelect");
    select.innerHTML = "";
    invoices.forEach((invoice) => {
        const option = document.createElement("option");
        option.value = invoice.id;
        option.textContent = `${invoice.invoiceNumber || invoice.id} · ${formatCurrency(invoice.amountDue || 0)} due`;
        select.appendChild(option);
    });
    select.value = selectedInvoiceId || "";
}

function renderClientInvoiceList(invoices) {
    const target = document.getElementById("clientInvoiceList");
    target.innerHTML = "";
    if (!invoices.length) {
        const emptyState = document.getElementById("emptyStateTemplate").content.firstElementChild.cloneNode(true);
        emptyState.querySelector(".eyebrow").textContent = "No invoices";
        emptyState.querySelector("h4").textContent = "No Xero invoices for this client";
        emptyState.querySelector("p:last-child").textContent = "Sync from Xero to populate the client ledger.";
        target.appendChild(emptyState);
        return;
    }
    invoices.forEach((invoice) => {
        const button = document.createElement("button");
        const category = invoiceCategory(invoice);
        button.type = "button";
        button.className = `client-invoice-row${invoice.id === selectedInvoiceId ? " is-selected" : ""}`;
        button.innerHTML = `
            <div>
                <div class="client-name">${invoice.invoiceNumber || "Invoice"}</div>
                <div class="invoice-subline">${invoice.description || "Xero invoice"} · due ${formatDate(invoice.dueDate)}</div>
            </div>
            <span class="status-pill ${category}">${invoiceStatusLabel(invoice)}</span>
            <strong>${formatCurrency(invoice.amountDue || 0)}</strong>
        `;
        button.addEventListener("click", () => {
            selectedInvoiceId = invoice.id;
            renderClientScreen();
        });
        target.appendChild(button);
    });
}

function clientStatusItems(invoices) {
    return invoices.flatMap((invoice) => (invoice.statuses || []).map((item) => ({
        ...item,
        title: `${invoice.invoiceNumber || "Invoice"} · ${item.title || "Status update"}`
    })));
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
        row.innerHTML = `<div class="timeline-head"><span class="timeline-title">${item.title || "Update"}</span><span class="timeline-stamp">${item.stamp || ""}</span></div><div class="timeline-body">${item.body || ""}</div>`;
        target.appendChild(row);
    });
}

function renderChrome() {
    document.getElementById("syncStamp").textContent = state.organisation.lastSync || "Waiting for first sync";
    const organisationName = state.organisation.name || "Xero organisation not connected";
    const lastSync = state.organisation.lastSync || "Waiting for first sync";
    const syncCopy = lastSync.toLowerCase().startsWith("last sync") || lastSync.toLowerCase().startsWith("waiting")
        ? lastSync
        : `Last sync: ${lastSync}`;
    document.getElementById("ledgerConnectionMeta").textContent = `${organisationName} · ${syncCopy}`;
    const connected = isXeroConnected();
    const label = connected ? "Reconnect Xero" : "Connect Xero";
    document.getElementById("connectXeroButton").textContent = label;
    syncButtonIds.forEach((buttonId) => {
        const button = document.getElementById(buttonId);
        if (!button) return;
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
    return state.organisation.xeroConnected === true;
}

function renderAll() {
    renderChrome();
    if (activeView === "ledger") {
        const invoices = allInvoices();
        renderSummaryCounts(invoices);
        renderToolbarControls();
        renderInvoiceTable(invoices);
    }
    renderClientScreen();
    renderSettingsScreen();
}

function flushPendingPersistence() {
    if (searchDebounceTimer !== null) window.clearTimeout(searchDebounceTimer);
    if (searchRenderFrame !== null) window.cancelAnimationFrame(searchRenderFrame);
    searchDebounceTimer = null;
    searchRenderFrame = null;
    if (persistStateTimer !== null) {
        window.clearTimeout(persistStateTimer);
        persistStateTimer = null;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }
    if (persistClientMatchesTimer !== null) {
        window.clearTimeout(persistClientMatchesTimer);
        persistClientMatchesTimer = null;
        const payload = {};
        state.customers.forEach((customer) => {
            if (!customer?.id) return;
            payload[customer.id] = {
                xeroOrganisationId: customer.xeroOrganisationId || "",
                ignitionClientId: customer.ignitionClientId || "",
                xeroConnected: isTruthyConnectionFlag(customer.xeroConnected),
                ignitionConnected: isTruthyConnectionFlag(customer.ignitionConnected)
            };
        });
        localStorage.setItem(CLIENT_MATCH_STORAGE_KEY, JSON.stringify(payload));
    }
}

function scheduleLedgerTableRender() {
    if (searchDebounceTimer !== null) {
        window.clearTimeout(searchDebounceTimer);
    }
    searchDebounceTimer = window.setTimeout(() => {
        searchDebounceTimer = null;
        if (searchRenderFrame !== null) {
            window.cancelAnimationFrame(searchRenderFrame);
        }
        searchRenderFrame = window.requestAnimationFrame(() => {
            searchRenderFrame = null;
            renderInvoiceTable();
        });
    }, SEARCH_DEBOUNCE_MS);
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
        scheduleLedgerTableRender();
    });
    document.getElementById("sortButton").addEventListener("click", () => toggleToolbarMenu("sortMenu", "sortButton"));
    document.getElementById("filterButton").addEventListener("click", () => toggleToolbarMenu("filterMenu", "filterButton"));
    document.querySelectorAll("[data-sort-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            sortMode = button.dataset.sortMode;
            currentPage = 1;
            closeToolbarMenus();
            renderAll();
        });
    });
    document.querySelectorAll("[data-filter-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            clientFilter = button.dataset.filterMode;
            currentPage = 1;
            closeToolbarMenus();
            renderAll();
        });
    });
    document.addEventListener("click", (event) => {
        if (!event.target.closest(".toolbar-menu-wrap")) closeToolbarMenus();
    });
    document.getElementById("previousPageButton").addEventListener("click", () => {
        currentPage = Math.max(1, currentPage - 1);
        renderInvoiceTable();
    });
    document.getElementById("nextPageButton").addEventListener("click", () => {
        currentPage += 1;
        renderInvoiceTable();
    });
    document.getElementById("pagePills").addEventListener("click", (event) => {
        const button = event.target.closest(".page-pill");
        if (!button) return;
        const page = Number(button.dataset.page);
        if (!Number.isFinite(page) || page < 1) return;
        currentPage = page;
        renderInvoiceTable();
    });
    document.getElementById("invoiceTableBody").addEventListener("click", (event) => {
        const row = event.target.closest("tr[data-invoice-id]");
        if (!row) return;
        selectedInvoiceId = row.dataset.invoiceId || null;
        selectedClientId = row.dataset.customerId || null;
        if (!selectedInvoiceId || !selectedClientId) return;
        activeView = "client";
        renderAll();
        window.scrollTo({ top: 0, behavior: "smooth" });
    });
}

function toggleToolbarMenu(menuId, buttonId) {
    const menu = document.getElementById(menuId);
    const button = document.getElementById(buttonId);
    const willOpen = menu.hidden;
    closeToolbarMenus();
    menu.hidden = !willOpen;
    button.setAttribute("aria-expanded", String(willOpen));
}

function closeToolbarMenus() {
    ["sortMenu", "filterMenu"].forEach((menuId) => {
        const menu = document.getElementById(menuId);
        if (menu) menu.hidden = true;
    });
    ["sortButton", "filterButton"].forEach((buttonId) => {
        const button = document.getElementById(buttonId);
        if (button) button.setAttribute("aria-expanded", "false");
    });
}

function wireLoginButtons() {
    const connect = (event) => {
        event.preventDefault();
        const url = loginURL();
        if (!url) {
            window.alert("Unable to start the Xero login flow.");
            return;
        }
        console.info("Starting Xero login:", url);
        window.location.assign(url);
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
            const payload = await requestJSON(api.endpoints.sync, { method: "POST" });
            if (payload?.panel) {
                replaceState(normaliseState(payload.panel));
            } else {
                await hydrateFromAPI();
            }
            renderAll();
        } catch (error) {
            console.error("Unable to sync panel data", error);
            if (error instanceof AuthRequiredError) {
                markAuthenticationRequired(error.message);
            }
        }
    };
    document.getElementById("primarySyncButton").addEventListener("click", sync);
    document.getElementById("sidebarSyncButton").addEventListener("click", sync);
}

function wireForms() {
    document.getElementById("openSettingsButton").addEventListener("click", () => {
        activeView = "settings";
        renderAll();
    });
    document.getElementById("backToLedgerFromSettingsButton").addEventListener("click", () => {
        activeView = "ledger";
        renderAll();
    });
    document.getElementById("backToLedgerButton").addEventListener("click", () => {
        activeView = "ledger";
        renderAll();
    });
    document.getElementById("matchingSearch").addEventListener("input", (event) => {
        matchingSearchTerm = event.target.value || "";
        renderSettingsScreen();
    });
    document.getElementById("clientInvoiceSelect").addEventListener("change", (event) => {
        selectedInvoiceId = event.target.value;
        renderClientScreen();
    });

    document.querySelectorAll(".client-profile-panel [data-field]").forEach((input) => {
        input.addEventListener("input", () => {
            if (!clientProfileDraft) return;
            clientProfileDraft[input.dataset.field] = input.value;
            setSaveButtonState();
        });
    });
    document.getElementById("saveClientChangesButton").addEventListener("click", async () => {
        if (!selectedClientId || !clientProfileDraft || !profileDirty() || clientProfileSaving) return;
        clientProfileSaving = true;
        setSaveButtonState();
        const payload = { ...clientProfileDraft, syncCompaniesHouse: true };
        try {
            const response = await requestJSON(
                api.endpoints.customerProfile,
                { method: "PATCH", body: JSON.stringify(payload) },
                { customerId: selectedClientId }
            );
            if (response?.panel) replaceState(normaliseState(response.panel));
            const refreshedClient = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
            clientProfileBaseline = defaultClientProfile(refreshedClient);
            clientProfileDraft = { ...clientProfileBaseline };
            renderAll();
        } catch (error) {
            console.error("Unable to save client profile", error);
        } finally {
            clientProfileSaving = false;
            setSaveButtonState();
        }
    });

    document.getElementById("clientNoteForm").addEventListener("submit", (event) => {
        event.preventDefault();
        const client = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
        const body = document.getElementById("clientNoteInput").value.trim();
        if (!client || !body) return;
        client.clientNotes = [{ title: "Client note", body, stamp: new Date().toISOString() }, ...(client.clientNotes || [])];
        document.getElementById("clientNoteInput").value = "";
        persistState();
        renderClientScreen();
    });

    document.getElementById("invoiceNoteForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const match = mutableInvoiceById(selectedInvoiceId);
        const body = document.getElementById("invoiceNoteInput").value.trim();
        if (!match || !body) return;
        match.invoice.notes = [{ title: "Invoice note", body, stamp: new Date().toISOString() }, ...(match.invoice.notes || [])];
        document.getElementById("invoiceNoteInput").value = "";
        persistState();
        renderAll();
        try {
            await requestJSON(api.endpoints.note, { method: "POST", body: JSON.stringify({ body }) }, { invoiceId: match.invoice.id });
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save invoice note", error);
            if (error instanceof AuthRequiredError) {
                markAuthenticationRequired(error.message);
            }
        }
    });

    document.getElementById("bulkStatusForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        const client = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
        const statusValue = document.getElementById("bulkStatusSelect").value;
        const note = document.getElementById("bulkStatusNote").value.trim();
        if (!client) return;
        const invoiceIds = (client.invoices || [])
            .filter((invoice) => invoiceCategory(invoice) !== "paid")
            .map((invoice) => invoice.id);
        invoiceIds.forEach((invoiceId) => {
            const match = mutableInvoiceById(invoiceId);
            if (!match) return;
            match.invoice.controlStatus = statusValue;
            match.invoice.statuses = [{ title: statusValue, body: note, stamp: new Date().toISOString() }, ...(match.invoice.statuses || [])];
        });
        document.getElementById("bulkStatusNote").value = "";
        state.panelSummary = null;
        persistState();
        renderAll();
        try {
            await requestJSON(
                api.endpoints.bulkStatus,
                { method: "POST", body: JSON.stringify({ invoiceIds, statusValue, note }) }
            );
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save bulk status", error);
            if (error instanceof AuthRequiredError) {
                markAuthenticationRequired(error.message);
            }
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
    window.addEventListener("beforeunload", flushPendingPersistence);
}
init();
