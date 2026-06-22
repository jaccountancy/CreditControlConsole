const STORAGE_KEY = "hymn-credit-control-ledger";
const STORAGE_PREFIXES = [STORAGE_KEY, "creditControl.", "jenius-"];
const CLIENT_MATCH_STORAGE_KEY = "hymn-credit-control-client-matches";
const HMRC_SETTINGS_STORAGE_KEY = "hymn-credit-control-hmrc-settings";
const CLIENT_WORKFLOW_STORAGE_KEY = "hymn-credit-control-client-workflow";
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
let taskSidebarOpen = false;
let taskSidebarTab = "task";
let searchTerm = "";
let matchingSearchTerm = "";
let clientFilter = "all";
let sortMode = "priority";
let pageSize = 25;
let currentPage = 1;
const ALL_TASKS_INITIAL_LOAD = 10;
const ALL_TASKS_LOAD_STEP = 10;
let visibleAllTasksCount = ALL_TASKS_INITIAL_LOAD;
let searchRenderFrame = null;
let invoiceStateVersion = 0;
let decoratedInvoicesCache = null;
let decoratedInvoicesCacheVersion = -1;
let filteredInvoicesCache = null;
let filteredInvoicesCacheKey = "";
let tableRenderSignature = "";
let clientProfileBaseline = null;
let clientProfileDraft = null;
let clientProfileClientId = null;
let clientProfileSaving = false;
let persistStateTimer = null;
let persistClientMatchesTimer = null;
let searchDebounceTimer = null;
let hmrcWizardState = loadHmrcWizardState();
let clientWorkflowStateByClient = loadClientWorkflowStateByClient();
const appLoadingState = { visible: true, message: "Loading client profile..." };
const PERSIST_DEBOUNCE_MS = 180;
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

function invalidateInvoiceCaches() {
    invoiceStateVersion += 1;
    decoratedInvoicesCache = null;
    decoratedInvoicesCacheVersion = -1;
    filteredInvoicesCache = null;
    filteredInvoicesCacheKey = "";
    tableRenderSignature = "";
}

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
const taskSidebarTabs = ["task", "notes", "history"];

function normaliseTaskSidebarTab(value) {
    return taskSidebarTabs.includes(value) ? value : "task";
}

function renderTaskSidebarTabs() {
    const activeTab = normaliseTaskSidebarTab(taskSidebarTab);
    taskSidebarTab = activeTab;
    document.querySelectorAll("[data-sidebar-tab]").forEach((button) => {
        const isActive = button.dataset.sidebarTab === activeTab;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-selected", String(isActive));
    });
    const panelByTab = {
        task: document.getElementById("sidebarPanelTask"),
        notes: document.getElementById("sidebarPanelNotes"),
        history: document.getElementById("sidebarPanelHistory")
    };
    Object.entries(panelByTab).forEach(([tab, panel]) => {
        if (!panel) return;
        panel.hidden = tab !== activeTab;
    });
}

function applyTaskSidebarState() {
    const sidebar = document.getElementById("taskSidebar");
    const backdrop = document.getElementById("taskSidebarBackdrop");
    const toggleButton = document.getElementById("openTaskSidebarButton");
    const shouldShow = activeView === "client" && taskSidebarOpen;
    if (sidebar) {
        sidebar.classList.toggle("is-open", shouldShow);
        sidebar.setAttribute("aria-hidden", String(!shouldShow));
    }
    if (backdrop) {
        backdrop.classList.toggle("is-visible", shouldShow);
        backdrop.setAttribute("aria-hidden", String(!shouldShow));
    }
    if (toggleButton) {
        toggleButton.textContent = shouldShow ? "Hide task sidebar" : "Open task sidebar";
        toggleButton.setAttribute("aria-expanded", String(shouldShow));
    }
}

function setTaskSidebarOpen(nextOpen) {
    taskSidebarOpen = Boolean(nextOpen);
    applyTaskSidebarState();
}

function setTaskSidebarTab(nextTab, options = {}) {
    taskSidebarTab = normaliseTaskSidebarTab(nextTab);
    if (options.open !== false) taskSidebarOpen = true;
    renderTaskSidebarTabs();
    applyTaskSidebarState();
}

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
            login: config.endpoints?.login || "/auth/xero/start?include_all_scopes=1",
            hmrcOAuthStatus: config.endpoints?.hmrcOAuthStatus || "/api/hmrc-64-8/oauth/status",
            hmrcOAuthStart: config.endpoints?.hmrcOAuthStart || "/api/hmrc-64-8/oauth/start",
            hmrcOAuthDisconnect: config.endpoints?.hmrcOAuthDisconnect || "/api/hmrc-64-8/oauth/disconnect",
            hmrcVatGatewayClientDetail: config.endpoints?.hmrcVatGatewayClientDetail || "/api/hmrc-64-8/vat-gateway/clients/:clientId",
            hmrcVatAuthorisationStart: config.endpoints?.hmrcVatAuthorisationStart || "/api/hmrc-64-8/vat-authorisations/start",
            hmrcVatAuthorisationCheck: config.endpoints?.hmrcVatAuthorisationCheck || "/api/hmrc-64-8/vat-authorisations/check",
            hmrcVatObligations: config.endpoints?.hmrcVatObligations || "/api/hmrc-64-8/vat/obligations",
            hmrcVatReturns: config.endpoints?.hmrcVatReturns || "/api/hmrc-64-8/vat/returns",
            hmrcVatLiabilities: config.endpoints?.hmrcVatLiabilities || "/api/hmrc-64-8/vat/liabilities",
            hmrcVatPayments: config.endpoints?.hmrcVatPayments || "/api/hmrc-64-8/vat/payments",
            hmrc64Payload: config.endpoints?.hmrc64Payload || "/api/hmrc-64-8",
            hmrc64Create: config.endpoints?.hmrc64Create || "/api/hmrc-64-8/requests",
            hmrc64Submit: config.endpoints?.hmrc64Submit || "/api/hmrc-64-8/requests/:requestId/submit",
            hmrc64CaptureCode: config.endpoints?.hmrc64CaptureCode || "/api/hmrc-64-8/requests/:requestId/capture-code"
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

function setAppLoadingState(visible, message = "") {
    appLoadingState.visible = Boolean(visible);
    if (message) appLoadingState.message = String(message);
    renderAppLoadingOverlay();
}

function renderAppLoadingOverlay() {
    const overlay = document.getElementById("appLoadingOverlay");
    const text = document.getElementById("appLoadingText");
    if (!overlay || !text) return;
    text.textContent = appLoadingState.message || "Loading client profile...";
    overlay.hidden = !appLoadingState.visible;
}

function loadHmrcWizardState() {
    try {
        const raw = localStorage.getItem(HMRC_SETTINGS_STORAGE_KEY);
        if (!raw) {
            return {
                gatewayClientId: "",
                asaId: "",
                arn: "",
                appClientId: "",
                appClientSecret: "",
                vrn: "",
                invitationId: "",
                clientAuthorisationUrl: "",
                handshakeStatus: "",
                hmrc64WizardStatus: "Ready",
                hmrc64Requests: [],
                hmrc64Summary: null,
                hmrc64ClientName: "",
                hmrc64ClientId: "",
                hmrc64Postcode: "",
                hmrc64SaUtr: "",
                hmrc64CtUtr: "",
                hmrc64TaxOfficeNumber: "",
                hmrc64TaxOfficeReference: "",
                hmrc64AccountsOfficeReference: "",
                oauthConnected: false,
                oauthConfigured: false,
                detail: null,
                lastPulledAt: "",
                gatewayPullStatus: "",
                vatDataSnapshot: null
            };
        }
        const parsed = JSON.parse(raw);
        return {
            gatewayClientId: String(parsed?.gatewayClientId || ""),
            asaId: String(parsed?.asaId || ""),
            arn: String(parsed?.arn || ""),
            appClientId: String(parsed?.appClientId || ""),
            appClientSecret: String(parsed?.appClientSecret || ""),
            vrn: String(parsed?.vrn || ""),
            invitationId: String(parsed?.invitationId || ""),
            clientAuthorisationUrl: String(parsed?.clientAuthorisationUrl || ""),
            handshakeStatus: String(parsed?.handshakeStatus || ""),
            hmrc64WizardStatus: String(parsed?.hmrc64WizardStatus || "Ready"),
            hmrc64Requests: Array.isArray(parsed?.hmrc64Requests) ? parsed.hmrc64Requests : [],
            hmrc64Summary: parsed?.hmrc64Summary && typeof parsed.hmrc64Summary === "object" ? parsed.hmrc64Summary : null,
            hmrc64ClientName: String(parsed?.hmrc64ClientName || ""),
            hmrc64ClientId: String(parsed?.hmrc64ClientId || ""),
            hmrc64Postcode: String(parsed?.hmrc64Postcode || ""),
            hmrc64SaUtr: String(parsed?.hmrc64SaUtr || ""),
            hmrc64CtUtr: String(parsed?.hmrc64CtUtr || ""),
            hmrc64TaxOfficeNumber: String(parsed?.hmrc64TaxOfficeNumber || ""),
            hmrc64TaxOfficeReference: String(parsed?.hmrc64TaxOfficeReference || ""),
            hmrc64AccountsOfficeReference: String(parsed?.hmrc64AccountsOfficeReference || ""),
            oauthConnected: parsed?.oauthConnected === true,
            oauthConfigured: parsed?.oauthConfigured === true,
            detail: parsed?.detail && typeof parsed.detail === "object" ? parsed.detail : null,
            lastPulledAt: String(parsed?.lastPulledAt || ""),
            gatewayPullStatus: String(parsed?.gatewayPullStatus || ""),
            vatDataSnapshot: parsed?.vatDataSnapshot && typeof parsed.vatDataSnapshot === "object" ? parsed.vatDataSnapshot : null
        };
    } catch {
        return {
            gatewayClientId: "",
            asaId: "",
            arn: "",
            appClientId: "",
            appClientSecret: "",
            vrn: "",
            invitationId: "",
            clientAuthorisationUrl: "",
            handshakeStatus: "",
            hmrc64WizardStatus: "Ready",
            hmrc64Requests: [],
            hmrc64Summary: null,
            hmrc64ClientName: "",
            hmrc64ClientId: "",
            hmrc64Postcode: "",
            hmrc64SaUtr: "",
            hmrc64CtUtr: "",
            hmrc64TaxOfficeNumber: "",
            hmrc64TaxOfficeReference: "",
            hmrc64AccountsOfficeReference: "",
            oauthConnected: false,
            oauthConfigured: false,
            detail: null,
            lastPulledAt: "",
            gatewayPullStatus: "",
            vatDataSnapshot: null
        };
    }
}

function buildHmrcGatewayPullSummary(detail, errorMessage = "") {
    const successful = [];
    const failed = [];

    if (hmrcWizardState.oauthConnected) successful.push("Step 2 HMRC account connection confirmed");
    else failed.push("Step 2 HMRC account is not connected");

    if (errorMessage) {
        failed.push(errorMessage);
    } else {
        if (detail?.gatewayClientId) successful.push(`Gateway record ${detail.gatewayClientId} loaded`);
        else failed.push("Gateway record was not returned");
        if (detail?.clientName) successful.push("Client name loaded");
        else failed.push("Client name missing");
        if (detail?.status) successful.push("Gateway status loaded");
        else failed.push("Gateway status missing");
        if (detail?.hmrcSubmissionReference) successful.push("Submission reference loaded");
        else failed.push("Submission reference missing");
    }

    const successText = successful.length ? successful.join("; ") : "None";
    const failedText = failed.length ? failed.join("; ") : "None";
    return `Successful: ${successText}. Failed: ${failedText}.`;
}

function persistHmrcWizardState() {
    try {
        localStorage.setItem(HMRC_SETTINGS_STORAGE_KEY, JSON.stringify(hmrcWizardState));
    } catch {
        // Ignore localStorage write failures in private browsing contexts.
    }
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

function loadClientWorkflowStateByClient() {
    try {
        const raw = localStorage.getItem(CLIENT_WORKFLOW_STORAGE_KEY);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
        return {};
    }
}

function persistClientWorkflowStateByClient() {
    try {
        localStorage.setItem(CLIENT_WORKFLOW_STORAGE_KEY, JSON.stringify(clientWorkflowStateByClient));
    } catch {
        // Ignore localStorage write failures in private browsing contexts.
    }
}

function workflowStateForClient(clientId) {
    const raw = clientId ? clientWorkflowStateByClient[clientId] : null;
    const reviewerStatus = raw?.reviewerStatus === "approved"
        ? "approved"
        : raw?.reviewerStatus === "changes-requested"
            ? "changes-requested"
            : "pending";
    return {
        reviewerName: String(raw?.reviewerName || ""),
        reviewerStatus,
        reviewerDecisionAt: String(raw?.reviewerDecisionAt || "")
    };
}

function updateWorkflowStateForClient(clientId, patch) {
    if (!clientId) return;
    const current = workflowStateForClient(clientId);
    const next = { ...current, ...patch };
    clientWorkflowStateByClient[clientId] = {
        reviewerName: String(next.reviewerName || "").trim(),
        reviewerStatus: next.reviewerStatus === "approved"
            ? "approved"
            : next.reviewerStatus === "changes-requested"
                ? "changes-requested"
                : "pending",
        reviewerDecisionAt: String(next.reviewerDecisionAt || "")
    };
    persistClientWorkflowStateByClient();
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
                vatGatewayClientId: customer.vatGatewayClientId || "",
                xeroConnected: isTruthyConnectionFlag(customer.xeroConnected),
                ignitionConnected: isTruthyConnectionFlag(customer.ignitionConnected),
                vatGatewayConnected: isTruthyConnectionFlag(customer.vatGatewayConnected)
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
    clientWorkflowStateByClient = {};
    state.selectedInvoice = null;
    selectedInvoiceId = null;
    selectedClientId = null;
    activeView = "ledger";
    currentPage = 1;
    visibleAllTasksCount = ALL_TASKS_INITIAL_LOAD;
    searchTerm = "";
    invalidateInvoiceCaches();
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
        localStorage.removeItem(HMRC_SETTINGS_STORAGE_KEY);
        localStorage.removeItem(CLIENT_WORKFLOW_STORAGE_KEY);
    } catch {
        localStorage.removeItem(STORAGE_KEY);
        localStorage.removeItem(CLIENT_MATCH_STORAGE_KEY);
        localStorage.removeItem(HMRC_SETTINGS_STORAGE_KEY);
        localStorage.removeItem(CLIENT_WORKFLOW_STORAGE_KEY);
    }
}

function replaceState(next) {
    const preservedInvoiceId = selectedInvoiceId;
    const localClientNotes = new Map(state.customers.map((customer) => [customer.id, customer.clientNotes || []]));
    const localMatches = new Map(state.customers.map((customer) => [customer.id, {
        xeroOrganisationId: customer.xeroOrganisationId || "",
        ignitionClientId: customer.ignitionClientId || "",
        vatGatewayClientId: customer.vatGatewayClientId || "",
        xeroConnected: isTruthyConnectionFlag(customer.xeroConnected),
        ignitionConnected: isTruthyConnectionFlag(customer.ignitionConnected),
        vatGatewayConnected: isTruthyConnectionFlag(customer.vatGatewayConnected)
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
    invalidateInvoiceCaches();
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
    const vatGateway = integrations.vatGateway || customer?.vatGateway || {};
    const vatGatewayClientId = customer?.vatGatewayClientId
        || customer?.vat_gateway_client_id
        || vatGateway.clientId
        || vatGateway.gatewayClientId
        || "";
    const xeroConnected = isTruthyConnectionFlag(customer?.xeroConnected)
        || isTruthyConnectionFlag(customer?.xero_connected)
        || isTruthyConnectionFlag(xero?.connected)
        || Boolean(xeroOrganisationId);
    const ignitionConnected = isTruthyConnectionFlag(customer?.ignitionConnected)
        || isTruthyConnectionFlag(customer?.ignition_connected)
        || isTruthyConnectionFlag(ignition?.connected)
        || Boolean(ignitionClientId);
    const vatGatewayConnected = isTruthyConnectionFlag(customer?.vatGatewayConnected)
        || isTruthyConnectionFlag(customer?.vat_gateway_connected)
        || isTruthyConnectionFlag(vatGateway?.connected)
        || Boolean(vatGatewayClientId);
    return {
        ...customer,
        xeroOrganisationId,
        ignitionClientId,
        vatGatewayClientId,
        xeroConnected,
        ignitionConnected,
        vatGatewayConnected
    };
}

function applyStoredMatch(customer, stored) {
    if (!stored) return customer;
    const xeroOrganisationId = stored.xeroOrganisationId || customer.xeroOrganisationId || "";
    const ignitionClientId = stored.ignitionClientId || customer.ignitionClientId || "";
    const vatGatewayClientId = stored.vatGatewayClientId || customer.vatGatewayClientId || "";
    const xeroConnected = isTruthyConnectionFlag(stored.xeroConnected) || Boolean(xeroOrganisationId) || customer.xeroConnected === true;
    const ignitionConnected = isTruthyConnectionFlag(stored.ignitionConnected) || Boolean(ignitionClientId) || customer.ignitionConnected === true;
    const vatGatewayConnected = isTruthyConnectionFlag(stored.vatGatewayConnected) || Boolean(vatGatewayClientId) || customer.vatGatewayConnected === true;
    return {
        ...customer,
        xeroOrganisationId,
        ignitionClientId,
        vatGatewayClientId,
        xeroConnected,
        ignitionConnected,
        vatGatewayConnected
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
    if (decoratedInvoicesCache && decoratedInvoicesCacheVersion === invoiceStateVersion) {
        return decoratedInvoicesCache;
    }
    decoratedInvoicesCache = state.customers.flatMap((customer) => (customer.invoices || []).map((invoice) => decorateInvoice(customer, invoice)));
    decoratedInvoicesCacheVersion = invoiceStateVersion;
    return decoratedInvoicesCache;
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

function parseDateValue(value) {
    if (!value) return null;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed;
}

function selfAssessmentPeriodEndFromTitle(title) {
    const value = String(title || "");
    const match = value.match(/tax\s*year\s*(\d{2,4})\s*\/\s*(\d{2,4})/i);
    if (!match) return null;
    const endYearRaw = Number(match[2]);
    if (!Number.isFinite(endYearRaw)) return null;
    const endYear = endYearRaw < 100 ? 2000 + endYearRaw : endYearRaw;
    return new Date(endYear, 3, 5);
}

function resolveTaskPeriodEndBadge(invoice) {
    const title = String(invoice?.description || invoice?.invoiceNumber || "");
    const titleLower = title.toLowerCase();
    const explicitPeriodEnd = invoice?.periodEndDate
        || invoice?.periodEnd
        || invoice?.period_end
        || invoice?.taxPeriodEnd
        || invoice?.tax_period_end
        || invoice?.yearEndDate
        || invoice?.year_end_date
        || invoice?.vatPeriodEnd
        || invoice?.vat_period_end;
    const explicitPeriodEndDate = parseDateValue(explicitPeriodEnd);

    const isSelfAssessment = titleLower.includes("self assessment");
    const isVatReturn = titleLower.includes("vat");
    const isYearEndAccounts = titleLower.includes("year end accounts") || titleLower.includes("year-end accounts");
    const isPayrollTask = titleLower.includes("payroll");
    if (isPayrollTask) return "";

    let periodEndDate = null;
    if (isSelfAssessment) periodEndDate = selfAssessmentPeriodEndFromTitle(title) || explicitPeriodEndDate;
    else if (isVatReturn || isYearEndAccounts) periodEndDate = explicitPeriodEndDate;
    if (!periodEndDate) return "";

    return `PERIOD END ${formatDate(periodEndDate).toUpperCase()}`;
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

function normaliseStatusSelectValue(value) {
    const allowed = new Set(["Paid", "Outstanding", "Query", "Overdue", "Legal", "Bad debt", "Promise Received"]);
    return allowed.has(value) ? value : "Outstanding";
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
    const canUseCache = invoices === allInvoices();
    const cacheKey = `${invoiceStateVersion}|${selectedFilter}|${clientFilter}|${sortMode}|${searchTermLower}`;
    if (canUseCache && filteredInvoicesCache && filteredInvoicesCacheKey === cacheKey) {
        return filteredInvoicesCache;
    }
    const result = invoices.filter((invoice) => {
        const category = invoice.category || invoiceCategory(invoice);
        const matchesFilter = selectedFilter === "all" || category === selectedFilter;
        const matchesSearch = (invoice.searchableText || "").includes(searchTermLower);
        const needsAction = ["outstanding", "query", "overdue", "court", "bad-debt"].includes(category);
        const matchesClient = clientFilter === "all" || (clientFilter === "action" ? needsAction : !needsAction);
        return matchesFilter && matchesSearch && matchesClient;
    }).sort(compareInvoices);
    if (canUseCache) {
        filteredInvoicesCache = result;
        filteredInvoicesCacheKey = cacheKey;
    }
    return result;
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
    const loadMoreMode = selectedFilter === "all";
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    currentPage = Math.min(currentPage, pages);
    const start = loadMoreMode ? 0 : (currentPage - 1) * pageSize;
    const pageLimit = loadMoreMode ? visibleAllTasksCount : pageSize;
    const paged = filtered.slice(start, start + pageLimit);
    const pageInvoiceIds = paged.map((invoice) => invoice.id || "").join("|");
    const signature = `${invoiceStateVersion}|${selectedFilter}|${clientFilter}|${sortMode}|${searchTerm.trim().toLowerCase()}|${currentPage}|${selectedInvoiceId || ""}|${filtered.length}|${pageInvoiceIds}|${loadMoreMode ? visibleAllTasksCount : pageSize}`;
    if (signature !== tableRenderSignature) {
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
                const periodEndBadgeText = resolveTaskPeriodEndBadge(invoice);
                const descriptionMeta = periodEndBadgeText
                    ? `<div class="period-end-badge">${escapeHTML(periodEndBadgeText)}</div>`
                    : `<div class="invoice-subline">${invoice.customerStatus || "Live Xero contact"}</div>`;
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
                    <td><div class="description-cell"><div class="description-icon">${descriptionGlyph(invoice)}</div><div><div class="description-title">${invoice.description || "Xero invoice"}</div>${descriptionMeta}</div></div></td>
                    <td class="amount-cell">${formatCurrency(invoice.total || invoice.amountDue || 0)}</td>
                    <td><div class="payment-cell"><div class="payment-status"${paidHoverText ? ` title="${escapeHTML(paidHoverText)}"` : ""}><span class="payment-dot ${category}">${category === "paid" ? "✓" : category === "court" ? "!" : "○"}</span><span class="paid-amount ${category === "paid" ? "is-paid" : "is-outstanding"}">${formatCurrency((invoice.total || 0) - (invoice.amountDue || 0))}</span></div><div class="paid-date">${paidDateLabel}</div></div></td>
                    <td><span class="status-pill ${category}">${invoiceStatusLabel(invoice)}</span></td>
                    <td><div class="note-snippet">${invoice.notesSummary || "No notes yet."}</div></td>
                    <td><button class="row-action-button" type="button" data-open-sidebar-tab="history" aria-label="Open task history sidebar" title="Open task history">⧉</button></td>
                `;
                fragment.appendChild(row);
            });
            tbody.appendChild(fragment);
        }
        tableRenderSignature = signature;
    }
    renderPagination({
        totalResults: filtered.length,
        totalPages: pages,
        start,
        count: paged.length,
        loadMoreMode
    });
}

function renderPagination({ totalResults, totalPages, start, count, loadMoreMode }) {
    document.getElementById("resultsText").textContent = totalResults ? `Showing ${start + 1} to ${start + count} of ${totalResults.toLocaleString("en-GB")} results` : "Showing 0 results";
    const previousPageButton = document.getElementById("previousPageButton");
    const nextPageButton = document.getElementById("nextPageButton");
    const target = document.getElementById("pagePills");
    const pagination = document.querySelector(".pagination");
    const loadMoreWrap = document.getElementById("loadMoreWrap");
    const loadMoreButton = document.getElementById("loadMoreButton");
    if (loadMoreMode) {
        if (pagination) pagination.hidden = true;
        target.innerHTML = "";
        if (loadMoreWrap && loadMoreButton) {
            const hasMore = count < totalResults;
            loadMoreWrap.hidden = !hasMore;
            loadMoreButton.disabled = !hasMore;
            loadMoreButton.textContent = hasMore ? "Load more" : "All tasks loaded";
        }
        previousPageButton.disabled = true;
        nextPageButton.disabled = true;
        return;
    }
    if (pagination) pagination.hidden = false;
    if (loadMoreWrap) loadMoreWrap.hidden = true;
    previousPageButton.disabled = currentPage <= 1;
    nextPageButton.disabled = currentPage >= totalPages;
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
    const hmrcSettingsScreen = document.getElementById("hmrcSettingsScreen");
    ledgerView.hidden = activeView !== "ledger";
    clientScreen.hidden = activeView !== "client";
    settingsScreen.hidden = activeView !== "settings";
    hmrcSettingsScreen.hidden = activeView !== "hmrcSettings";
    if (activeView !== "client") {
        applyTaskSidebarState();
        return;
    }

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
    const vatGatewayConnected = client ? resolveIntegrationConnection(client, "vatGateway") : false;
    renderConnectionBadge("clientXeroStatusBadge", "Connected to Xero", xeroConnected);
    renderConnectionBadge("clientIgnitionStatusBadge", "Connected to Ignition", ignitionConnected);
    renderConnectionBadge("clientVatGatewayStatusBadge", "VAT Gateway linked", vatGatewayConnected);

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

    renderTaskSidebar(client, selectedInvoice, invoices);
    renderClientProfilePanel(client);
    renderClientInvoicePicker(invoices);
    renderClientInvoiceList(invoices);
    renderReviewerWorkflowPanel(client);
    renderTimeline("statusTimeline", clientStatusItems(invoices), { eyebrow: "No status history", title: "Status changes will appear here", body: "Bulk updates will build the client credit-control history." });
    renderTimeline("clientNotesTimeline", client?.clientNotes || [], { eyebrow: "No client notes", title: "Client notes will appear here", body: "Add notes for account-level calls, chasing updates and context." });
    renderTimeline("invoiceNotesTimeline", selectedInvoice?.notes || [], { eyebrow: "No invoice notes", title: "Invoice notes will appear here", body: "Select an invoice above, then add a note attached to that invoice." });
    renderTaskSidebarTabs();
    applyTaskSidebarState();
}

function toDateInputValue(value) {
    if (!value) return "";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    const year = parsed.getFullYear();
    const month = String(parsed.getMonth() + 1).padStart(2, "0");
    const day = String(parsed.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function renderTaskSidebar(client, selectedInvoice, invoices) {
    const managerSelect = document.getElementById("sidebarManagerSelect");
    const managerCustomInput = document.getElementById("sidebarManagerCustomInput");
    const statusSelect = document.getElementById("sidebarStatusSelect");
    const statusNote = document.getElementById("sidebarStatusNote");
    const applyAllCheck = document.getElementById("sidebarApplyAllOpenCheck");
    const promiseDateInput = document.getElementById("sidebarPromiseDateInput");
    const quickStats = document.getElementById("sidebarQuickStats");
    const taskMeta = document.getElementById("sidebarTaskMeta");
    const hasContext = Boolean(client && selectedInvoice);
    if (!managerSelect || !managerCustomInput || !statusSelect || !statusNote || !applyAllCheck || !promiseDateInput || !quickStats || !taskMeta) return;

    managerSelect.innerHTML = "";
    const allManagers = managerValues().filter(Boolean);
    const currentManager = String(client?.manager || "").trim();
    const uniqueManagers = [...new Set([...allManagers, currentManager].filter(Boolean))].sort((a, b) => a.localeCompare(b));
    const unassignedOption = document.createElement("option");
    unassignedOption.value = "";
    unassignedOption.textContent = "Unassigned";
    managerSelect.appendChild(unassignedOption);
    uniqueManagers.forEach((manager) => {
        const option = document.createElement("option");
        option.value = manager;
        option.textContent = manager;
        managerSelect.appendChild(option);
    });
    const customOption = document.createElement("option");
    customOption.value = "__custom";
    customOption.textContent = "Custom...";
    managerSelect.appendChild(customOption);

    const selectedManagerMatchesPreset = currentManager && uniqueManagers.includes(currentManager);
    managerSelect.value = selectedManagerMatchesPreset ? currentManager : (currentManager ? "__custom" : "");
    managerCustomInput.value = selectedManagerMatchesPreset ? "" : currentManager;
    managerCustomInput.disabled = managerSelect.value !== "__custom";

    statusSelect.value = normaliseStatusSelectValue(hasContext ? statusSelectValue(selectedInvoice) : "Outstanding");
    statusNote.value = "";
    applyAllCheck.checked = true;
    promiseDateInput.value = hasContext ? toDateInputValue(selectedInvoice.promisedDate || "") : "";

    const openInvoices = (invoices || []).filter((item) => (item.category || invoiceCategory(item)) !== "paid");
    const overdueInvoices = (invoices || []).filter((item) => (item.category || invoiceCategory(item)) === "overdue");
    const selectedDue = Number(selectedInvoice?.amountDue || 0);
    taskMeta.textContent = hasContext
        ? `${selectedInvoice.invoiceNumber || selectedInvoice.id || "Selected task"} · manager ${client?.manager || "Unassigned"}`
        : "Select a task to manage status and ownership.";
    quickStats.innerHTML = hasContext
        ? `<p><strong>${openInvoices.length}</strong> open tasks</p><p><strong>${overdueInvoices.length}</strong> overdue</p><p><strong>${formatCurrency(selectedDue)}</strong> selected due</p>`
        : "<p>No task selected.</p>";
}

function reviewerStatusLabel(status) {
    if (status === "approved") return "Approved";
    if (status === "changes-requested") return "Changes requested";
    return "Awaiting review";
}

function reviewerStatusClass(status) {
    if (status === "approved") return "status-approved";
    if (status === "changes-requested") return "status-changes";
    return "status-pending";
}

function bookkeepingEmailDraft(client) {
    const clientName = client?.name || "Client";
    return [
        `Hi ${clientName},`,
        "",
        "Please find your latest Bookkeeping Snapshot attached.",
        "",
        "As part of this update we have included:",
        "- Corporation Tax estimates",
        "- Director loan account position",
        "- Current dividend availability",
        "",
        "If you want us to walk through any item, reply to this email and we will schedule it.",
        "",
        "Kind regards,",
        "Jaccountancy"
    ].join("\n");
}

function renderReviewerWorkflowPanel(client) {
    const statusBadge = document.getElementById("reviewerWorkflowStatusBadge");
    const statusText = document.getElementById("reviewerWorkflowStatusText");
    const reviewerNameInput = document.getElementById("reviewerNameInput");
    const decisionSelect = document.getElementById("reviewerDecisionSelect");
    const finalStage = document.getElementById("finalWorkflowStage");
    const emailDraft = document.getElementById("bookkeepingEmailDraft");
    if (!statusBadge || !statusText || !reviewerNameInput || !decisionSelect || !finalStage || !emailDraft) return;

    if (!client?.id) {
        statusBadge.className = "workflow-status-pill status-pending";
        statusBadge.textContent = "Awaiting review";
        statusText.textContent = "Set reviewer decision to unlock the final client email stage.";
        reviewerNameInput.value = "";
        decisionSelect.value = "pending";
        emailDraft.value = "";
        finalStage.hidden = true;
        return;
    }

    const workflow = workflowStateForClient(client.id);
    const label = reviewerStatusLabel(workflow.reviewerStatus);
    statusBadge.className = `workflow-status-pill ${reviewerStatusClass(workflow.reviewerStatus)}`;
    statusBadge.textContent = label;
    reviewerNameInput.value = workflow.reviewerName;
    decisionSelect.value = workflow.reviewerStatus;
    finalStage.hidden = workflow.reviewerStatus !== "approved";
    emailDraft.value = bookkeepingEmailDraft(client);

    const decidedBy = workflow.reviewerName ? ` by ${workflow.reviewerName}` : "";
    const decidedAt = workflow.reviewerDecisionAt ? ` on ${formatDate(workflow.reviewerDecisionAt)}` : "";
    if (workflow.reviewerStatus === "approved") {
        statusText.textContent = `Reviewer approved${decidedBy}${decidedAt}. Final client email step is now open.`;
    } else if (workflow.reviewerStatus === "changes-requested") {
        statusText.textContent = `Reviewer requested changes${decidedBy}${decidedAt}. Final client email remains locked.`;
    } else {
        statusText.textContent = "Awaiting reviewer decision. Final client email remains locked.";
    }
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
            customer.ignitionClientId,
            customer.vatGatewayClientId
        ].join(" ").toLowerCase();
        return haystack.includes(term);
    }).sort((a, b) => `${a.name || ""}`.localeCompare(`${b.name || ""}`));
    tbody.innerHTML = "";
    if (!customers.length) {
        const row = document.createElement("tr");
        row.innerHTML = `<td colspan="6"><p class="settings-empty">No clients match the current search.</p></td>`;
        tbody.appendChild(row);
        return;
    }
    customers.forEach((customer) => {
        const row = document.createElement("tr");
        const xeroConnected = resolveIntegrationConnection(customer, "xero");
        const ignitionConnected = resolveIntegrationConnection(customer, "ignition");
        const vatGatewayConnected = resolveIntegrationConnection(customer, "vatGateway");
        row.innerHTML = `
            <td>
                <div class="settings-client-title">${customer.name || "Unnamed client"}</div>
                <div class="settings-client-meta">${customer.manager || "Unassigned"} · ${customer.contact || customer.id || ""}</div>
            </td>
            <td><span class="status-badge ${xeroConnected ? "connected" : "disconnected"}">${xeroConnected ? "Connected" : "Not connected"}</span></td>
            <td><span class="status-badge ${ignitionConnected ? "connected" : "disconnected"}">${ignitionConnected ? "Connected" : "Not connected"}</span></td>
            <td><span class="status-badge ${vatGatewayConnected ? "connected" : "disconnected"}">${vatGatewayConnected ? "Linked" : "Not linked"}</span></td>
            <td>
                <div class="settings-match-inputs">
                    <input type="text" data-field="xeroOrganisationId" value="${escapeHTML(customer.xeroOrganisationId || "")}" placeholder="Xero organisation ID">
                    <input type="text" data-field="ignitionClientId" value="${escapeHTML(customer.ignitionClientId || "")}" placeholder="Ignition client ID">
                    <input type="text" data-field="vatGatewayClientId" value="${escapeHTML(customer.vatGatewayClientId || "")}" placeholder="HMRC gateway client ID">
                </div>
            </td>
            <td><button class="settings-save-match" type="button">Save match</button></td>
        `;
        const saveButton = row.querySelector(".settings-save-match");
        saveButton.addEventListener("click", async () => {
            const xeroInput = row.querySelector('[data-field="xeroOrganisationId"]');
            const ignitionInput = row.querySelector('[data-field="ignitionClientId"]');
            const vatGatewayInput = row.querySelector('[data-field="vatGatewayClientId"]');
            customer.xeroOrganisationId = xeroInput?.value.trim() || "";
            customer.ignitionClientId = ignitionInput?.value.trim() || "";
            customer.vatGatewayClientId = vatGatewayInput?.value.trim() || "";
            customer.xeroConnected = Boolean(customer.xeroOrganisationId);
            customer.ignitionConnected = Boolean(customer.ignitionClientId);
            customer.vatGatewayConnected = Boolean(customer.vatGatewayClientId);
            saveButton.disabled = true;
            saveButton.textContent = "Saving...";
            if (customer.vatGatewayClientId) {
                try {
                    const gatewayClient = await fetchHmrcGatewayClientDetail(customer.vatGatewayClientId);
                    customer.vatGatewayDetails = gatewayClient;
                    customer.vatGatewayConnected = true;
                    if (customer.clientProfile && gatewayClient?.postalAddress && !customer.clientProfile.address) {
                        customer.clientProfile.address = gatewayClient.postalAddress;
                    }
                } catch (error) {
                    console.error("Unable to pull HMRC VAT gateway details for matched client", error);
                }
            } else {
                customer.vatGatewayDetails = null;
            }
            invalidateInvoiceCaches();
            persistState();
            persistClientMatches();
            saveButton.disabled = false;
            saveButton.textContent = "Save match";
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
    if (provider === "ignition") return client.ignitionConnected === true || Boolean(client.ignitionClientId);
    if (provider === "vatGateway") return client.vatGatewayConnected === true || Boolean(client.vatGatewayClientId);
    return false;
}

async function fetchHmrcGatewayClientDetail(gatewayClientId) {
    const payload = await requestJSON(
        api.endpoints.hmrcVatGatewayClientDetail,
        { method: "GET" },
        { clientId: gatewayClientId }
    );
    return payload?.gatewayClient || null;
}

function currentHmrcWizardFormState() {
    hmrcWizardState.gatewayClientId = (document.getElementById("hmrcGatewayClientIdInput")?.value || "").trim();
    hmrcWizardState.vrn = (document.getElementById("hmrcVrnInput")?.value || "").trim();
    hmrcWizardState.asaId = (document.getElementById("hmrcAsaInput")?.value || "").trim();
    hmrcWizardState.arn = (document.getElementById("hmrcArnInput")?.value || "").trim();
    hmrcWizardState.appClientId = (document.getElementById("hmrcAppClientIdInput")?.value || "").trim();
    hmrcWizardState.appClientSecret = (document.getElementById("hmrcAppClientSecretInput")?.value || "").trim();
    return {
        gatewayClientId: hmrcWizardState.gatewayClientId,
        vrn: hmrcWizardState.vrn,
        agentReferenceNumber: hmrcWizardState.arn,
        asaId: hmrcWizardState.asaId
    };
}

function currentHmrc64FormState() {
    hmrcWizardState.hmrc64ClientName = (document.getElementById("hmrc64ClientNameInput")?.value || "").trim();
    hmrcWizardState.hmrc64ClientId = (document.getElementById("hmrc64ClientIdInput")?.value || "").trim();
    hmrcWizardState.hmrc64Postcode = (document.getElementById("hmrc64PostcodeInput")?.value || "").trim();
    hmrcWizardState.hmrc64SaUtr = (document.getElementById("hmrc64SaUtrInput")?.value || "").trim();
    hmrcWizardState.hmrc64CtUtr = (document.getElementById("hmrc64CtUtrInput")?.value || "").trim();
    hmrcWizardState.hmrc64TaxOfficeNumber = (document.getElementById("hmrc64TaxOfficeNumberInput")?.value || "").trim();
    hmrcWizardState.hmrc64TaxOfficeReference = (document.getElementById("hmrc64TaxOfficeReferenceInput")?.value || "").trim();
    hmrcWizardState.hmrc64AccountsOfficeReference = (document.getElementById("hmrc64AccountsOfficeReferenceInput")?.value || "").trim();
    const includeSa = document.getElementById("hmrc64IncludeSa")?.checked === true;
    const includeCt = document.getElementById("hmrc64IncludeCt")?.checked === true;
    const includePaye = document.getElementById("hmrc64IncludePaye")?.checked === true;
    return {
        clientName: hmrcWizardState.hmrc64ClientName,
        clientId: hmrcWizardState.hmrc64ClientId,
        postcode: hmrcWizardState.hmrc64Postcode,
        saUtr: hmrcWizardState.hmrc64SaUtr,
        ctUtr: hmrcWizardState.hmrc64CtUtr,
        taxOfficeNumber: hmrcWizardState.hmrc64TaxOfficeNumber,
        taxOfficeReference: hmrcWizardState.hmrc64TaxOfficeReference,
        accountsOfficeReference: hmrcWizardState.hmrc64AccountsOfficeReference,
        includeSa,
        includeCt,
        includePaye,
    };
}

function hmrc64SelectedServices(form) {
    const services = [];
    if (form.includeSa) services.push({ flag: "includeSa", label: "SA" });
    if (form.includeCt) services.push({ flag: "includeCt", label: "CT" });
    if (form.includePaye) services.push({ flag: "includePaye", label: "PAYE" });
    return services;
}

function hmrc64StatusForOutcomeText(statusValue) {
    const value = String(statusValue || "").toLowerCase();
    if (value === "awaiting_code" || value === "submitted" || value === "code_received") return "awaiting code";
    if (value === "authorised") return "authorised";
    if (value === "draft") return "draft";
    return value || "unknown";
}

function hmrc64MatchesClient(row, form) {
    const rowClientId = String(row?.clientId || "").trim();
    const rowClientName = String(row?.clientName || "").trim().toLowerCase();
    const formClientId = String(form?.clientId || "").trim();
    const formClientName = String(form?.clientName || "").trim().toLowerCase();
    if (formClientId && rowClientId && rowClientId === formClientId) return true;
    return Boolean(formClientName) && rowClientName === formClientName;
}

function hmrc64FormatServiceOutcomeSummary(outcomes) {
    const successful = outcomes.filter((item) => item.outcome === "successful").map((item) => item.message);
    const skipped = outcomes.filter((item) => item.outcome === "skipped").map((item) => item.message);
    const failed = outcomes.filter((item) => item.outcome === "failed").map((item) => item.message);
    return [
        `Successful: ${successful.length ? successful.join("; ") : "None"}.`,
        `Skipped: ${skipped.length ? skipped.join("; ") : "None"}.`,
        `Failed: ${failed.length ? failed.join("; ") : "None"}.`
    ].join(" ");
}

async function recoverExistingHmrc64Requests(form) {
    await refreshHmrc64Tracker();
    const selectedServices = hmrc64SelectedServices(form);
    const clientRequests = (Array.isArray(hmrcWizardState.hmrc64Requests) ? hmrcWizardState.hmrc64Requests : [])
        .filter((row) => hmrc64MatchesClient(row, form));
    const outcomes = [];

    for (const service of selectedServices) {
        const matchingRequest = clientRequests.find((row) => row?.[service.flag] === true);
        if (!matchingRequest?.id) {
            outcomes.push({
                outcome: "failed",
                message: `${service.label} has no existing request to continue with`
            });
            continue;
        }
        const statusValue = String(matchingRequest.status || "").toLowerCase();
        if (statusValue === "draft") {
            try {
                await requestJSON(
                    api.endpoints.hmrc64Submit,
                    {
                        method: "POST",
                        body: JSON.stringify({ submissionChannel: "online" })
                    },
                    { requestId: matchingRequest.id }
                );
                outcomes.push({
                    outcome: "successful",
                    message: `${service.label} submitted using existing draft request ${matchingRequest.id}`
                });
            } catch (error) {
                outcomes.push({
                    outcome: "failed",
                    message: `${service.label} draft ${matchingRequest.id} submit failed (${error?.message || "unknown error"})`
                });
            }
            continue;
        }
        if (["submitted", "awaiting_code", "code_received"].includes(statusValue)) {
            outcomes.push({
                outcome: "skipped",
                message: `${service.label} already ${hmrc64StatusForOutcomeText(statusValue)} on request ${matchingRequest.id}`
            });
            continue;
        }
        if (statusValue === "authorised") {
            outcomes.push({
                outcome: "skipped",
                message: `${service.label} already authorised on request ${matchingRequest.id}`
            });
            continue;
        }
        outcomes.push({
            outcome: "skipped",
            message: `${service.label} already created on request ${matchingRequest.id} (status ${hmrc64StatusForOutcomeText(statusValue)})`
        });
    }

    await refreshHmrc64Tracker();
    return outcomes;
}

function hmrcWizardRedirectTarget() {
    const url = new URL(window.location.href);
    url.searchParams.set("hmrcWizard", "1");
    url.searchParams.delete("hmrcMtdConnected");
    return `${url.pathname}${url.search}${url.hash}`;
}

function consumeHmrcCallbackParams() {
    const url = new URL(window.location.href);
    const connected = url.searchParams.get("hmrcMtdConnected") === "1";
    const wizard = url.searchParams.get("hmrcWizard") === "1";
    if (connected || wizard) activeView = "hmrcSettings";
    if (connected) hmrcWizardState.oauthConnected = true;
    persistHmrcWizardState();
    if (url.searchParams.has("hmrcMtdConnected")) {
        url.searchParams.delete("hmrcMtdConnected");
        window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    }
}

async function refreshHmrcOauthStatus() {
    try {
        const payload = await requestJSON(api.endpoints.hmrcOAuthStatus);
        const oauth = payload?.oauth || {};
        hmrcWizardState.oauthConnected = oauth.connected === true;
        hmrcWizardState.oauthConfigured = oauth.configured === true;
        persistHmrcWizardState();
    } catch (error) {
        console.error("Unable to refresh HMRC OAuth status", error);
    }
}

async function refreshHmrc64Tracker() {
    try {
        const payload = await requestJSON(api.endpoints.hmrc64Payload);
        hmrcWizardState.hmrc64Requests = Array.isArray(payload?.requests) ? payload.requests : [];
        hmrcWizardState.hmrc64Summary = payload?.summary && typeof payload.summary === "object" ? payload.summary : null;
        persistHmrcWizardState();
    } catch (error) {
        console.error("Unable to load HMRC 64-8 tracker", error);
    }
}

function renderHmrcSettingsScreen() {
    if (activeView !== "hmrcSettings") return;
    const gatewayInput = document.getElementById("hmrcGatewayClientIdInput");
    const vrnInput = document.getElementById("hmrcVrnInput");
    const asaInput = document.getElementById("hmrcAsaInput");
    const arnInput = document.getElementById("hmrcArnInput");
    const appClientInput = document.getElementById("hmrcAppClientIdInput");
    const appSecretInput = document.getElementById("hmrcAppClientSecretInput");
    const oauthStatusText = document.getElementById("hmrcOauthStatusText");
    const pullStatusText = document.getElementById("hmrcGatewayPullStatusText");
    const handshakeStatusText = document.getElementById("hmrcHandshakeStatusText");
    const clientAuthorisationUrlInput = document.getElementById("hmrcClientAuthorisationUrlInput");
    const vatDataOutput = document.getElementById("hmrcVatDataOutput");
    const meta = document.getElementById("hmrcGatewayRecordMeta");
    const clientName = document.getElementById("hmrcGatewayClientName");
    const clientStatus = document.getElementById("hmrcGatewayClientStatus");
    const submissionRef = document.getElementById("hmrcGatewaySubmissionRef");
    const hmrc64ClientNameInput = document.getElementById("hmrc64ClientNameInput");
    const hmrc64ClientIdInput = document.getElementById("hmrc64ClientIdInput");
    const hmrc64PostcodeInput = document.getElementById("hmrc64PostcodeInput");
    const hmrc64SaUtrInput = document.getElementById("hmrc64SaUtrInput");
    const hmrc64CtUtrInput = document.getElementById("hmrc64CtUtrInput");
    const hmrc64TaxOfficeNumberInput = document.getElementById("hmrc64TaxOfficeNumberInput");
    const hmrc64TaxOfficeReferenceInput = document.getElementById("hmrc64TaxOfficeReferenceInput");
    const hmrc64AccountsOfficeReferenceInput = document.getElementById("hmrc64AccountsOfficeReferenceInput");
    const hmrc64WizardStatusText = document.getElementById("hmrc64WizardStatusText");
    const hmrc64TrackerMeta = document.getElementById("hmrc64TrackerMeta");
    const hmrc64AwaitingCodeCount = document.getElementById("hmrc64AwaitingCodeCount");
    const hmrc64AuthorisedCount = document.getElementById("hmrc64AuthorisedCount");
    const hmrc64TotalCount = document.getElementById("hmrc64TotalCount");
    const hmrc64TrackerTableBody = document.getElementById("hmrc64TrackerTableBody");
    if (!gatewayInput || !vrnInput || !oauthStatusText || !pullStatusText || !meta || !clientName || !clientStatus || !submissionRef) return;

    gatewayInput.value = hmrcWizardState.gatewayClientId || "";
    vrnInput.value = hmrcWizardState.vrn || "";
    if (asaInput) asaInput.value = hmrcWizardState.asaId || "";
    if (arnInput) arnInput.value = hmrcWizardState.arn || "";
    if (appClientInput) appClientInput.value = hmrcWizardState.appClientId || "";
    if (appSecretInput) appSecretInput.value = hmrcWizardState.appClientSecret || "";

    const oauthConfigured = hmrcWizardState.oauthConfigured !== false;
    if (!oauthConfigured) oauthStatusText.value = "HMRC developer app is not configured on the backend.";
    else oauthStatusText.value = hmrcWizardState.oauthConnected ? "Connected to HMRC agent services account." : "Not connected.";

    const detail = hmrcWizardState.detail;
    if (hmrcWizardState.gatewayPullStatus) {
        const timestamp = hmrcWizardState.lastPulledAt
            ? ` Last pull completed ${new Date(hmrcWizardState.lastPulledAt).toLocaleString("en-GB")}.`
            : "";
        pullStatusText.value = `${hmrcWizardState.gatewayPullStatus}${timestamp}`;
    } else {
        pullStatusText.value = "No VAT gateway pull run yet.";
    }
    meta.textContent = detail?.gatewayClientId ? `Gateway client ${detail.gatewayClientId} linked` : "No HMRC record linked yet";
    clientName.textContent = detail?.clientName || "--";
    clientStatus.textContent = detail?.status || "--";
    submissionRef.textContent = detail?.hmrcSubmissionReference || "--";
    if (handshakeStatusText) handshakeStatusText.value = hmrcWizardState.handshakeStatus || "No handshake started.";
    if (clientAuthorisationUrlInput) clientAuthorisationUrlInput.value = hmrcWizardState.clientAuthorisationUrl || "";
    if (vatDataOutput) {
        vatDataOutput.value = hmrcWizardState.vatDataSnapshot
            ? JSON.stringify(hmrcWizardState.vatDataSnapshot, null, 2)
            : "";
    }

    if (hmrc64ClientNameInput) hmrc64ClientNameInput.value = hmrcWizardState.hmrc64ClientName || detail?.clientName || "";
    if (hmrc64ClientIdInput) hmrc64ClientIdInput.value = hmrcWizardState.hmrc64ClientId || hmrcWizardState.gatewayClientId || "";
    if (hmrc64PostcodeInput) hmrc64PostcodeInput.value = hmrcWizardState.hmrc64Postcode || detail?.postcode || "";
    if (hmrc64SaUtrInput) hmrc64SaUtrInput.value = hmrcWizardState.hmrc64SaUtr || "";
    if (hmrc64CtUtrInput) hmrc64CtUtrInput.value = hmrcWizardState.hmrc64CtUtr || "";
    if (hmrc64TaxOfficeNumberInput) hmrc64TaxOfficeNumberInput.value = hmrcWizardState.hmrc64TaxOfficeNumber || "";
    if (hmrc64TaxOfficeReferenceInput) hmrc64TaxOfficeReferenceInput.value = hmrcWizardState.hmrc64TaxOfficeReference || "";
    if (hmrc64AccountsOfficeReferenceInput) hmrc64AccountsOfficeReferenceInput.value = hmrcWizardState.hmrc64AccountsOfficeReference || "";
    if (hmrc64WizardStatusText) hmrc64WizardStatusText.value = hmrcWizardState.hmrc64WizardStatus || "Ready";

    const requests = Array.isArray(hmrcWizardState.hmrc64Requests) ? hmrcWizardState.hmrc64Requests : [];
    const summary = hmrcWizardState.hmrc64Summary || {};
    const awaitingCode = Number(summary.awaitingCode ?? requests.filter((row) => ["awaiting_code", "submitted", "code_received"].includes(String(row?.status || "").toLowerCase())).length);
    const authorised = Number(summary.authorised ?? requests.filter((row) => String(row?.status || "").toLowerCase() === "authorised").length);
    const total = Number(summary.total ?? requests.length);
    if (hmrc64AwaitingCodeCount) hmrc64AwaitingCodeCount.textContent = String(awaitingCode);
    if (hmrc64AuthorisedCount) hmrc64AuthorisedCount.textContent = String(authorised);
    if (hmrc64TotalCount) hmrc64TotalCount.textContent = String(total);
    if (hmrc64TrackerMeta) hmrc64TrackerMeta.textContent = `${awaitingCode} awaiting code · ${authorised} authorised`;
    if (hmrc64TrackerTableBody) {
        hmrc64TrackerTableBody.innerHTML = "";
        if (!requests.length) {
            hmrc64TrackerTableBody.innerHTML = `<tr><td colspan="6">No HMRC 64-8 requests yet.</td></tr>`;
        } else {
            requests.forEach((row) => {
                const status = String(row?.status || "").toLowerCase();
                const services = Array.isArray(row?.services) ? row.services.join(", ") : "";
                const canCapture = ["awaiting_code", "code_received", "submitted"].includes(status);
                const tr = document.createElement("tr");
                tr.innerHTML = `
                    <td>${escapeHTML(row?.clientName || "")}</td>
                    <td>${escapeHTML(services)}</td>
                    <td>${escapeHTML(status || "draft")}</td>
                    <td>${escapeHTML(row?.submittedAt || "--")}</td>
                    <td>${escapeHTML(row?.expectedCodeBy || "--")}</td>
                    <td>
                        ${canCapture ? `
                            <input type="text" data-hmrc64-code-input="${escapeHTML(row?.id || "")}" placeholder="Authority code">
                            <button class="ghost-button" type="button" data-hmrc64-code-received="${escapeHTML(row?.id || "")}">Code Received</button>
                        ` : "<span>--</span>"}
                    </td>
                `;
                hmrc64TrackerTableBody.appendChild(tr);
            });
        }
    }
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
    renderHmrcSettingsScreen();
    renderAppLoadingOverlay();
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
                vatGatewayClientId: customer.vatGatewayClientId || "",
                xeroConnected: isTruthyConnectionFlag(customer.xeroConnected),
                ignitionConnected: isTruthyConnectionFlag(customer.ignitionConnected),
                vatGatewayConnected: isTruthyConnectionFlag(customer.vatGatewayConnected)
            };
        });
        localStorage.setItem(CLIENT_MATCH_STORAGE_KEY, JSON.stringify(payload));
    }
    persistHmrcWizardState();
}

function scheduleLedgerTableRender() {
    if (searchDebounceTimer !== null) {
        window.clearTimeout(searchDebounceTimer);
        searchDebounceTimer = null;
    }
    if (searchRenderFrame !== null) {
        window.cancelAnimationFrame(searchRenderFrame);
    }
    searchRenderFrame = window.requestAnimationFrame(() => {
        searchRenderFrame = null;
        renderInvoiceTable();
    });
}

function wireFilters() {
    document.querySelectorAll(".summary-chip").forEach((button) => {
        button.addEventListener("click", () => {
            selectedFilter = button.dataset.filter;
            currentPage = 1;
            visibleAllTasksCount = ALL_TASKS_INITIAL_LOAD;
            renderAll();
        });
    });
    document.getElementById("ledgerSearch").addEventListener("input", (event) => {
        searchTerm = event.target.value;
        currentPage = 1;
        visibleAllTasksCount = ALL_TASKS_INITIAL_LOAD;
        scheduleLedgerTableRender();
    });
    document.getElementById("sortButton").addEventListener("click", () => toggleToolbarMenu("sortMenu", "sortButton"));
    document.getElementById("filterButton").addEventListener("click", () => toggleToolbarMenu("filterMenu", "filterButton"));
    document.querySelectorAll("[data-sort-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            sortMode = button.dataset.sortMode;
            currentPage = 1;
            visibleAllTasksCount = ALL_TASKS_INITIAL_LOAD;
            closeToolbarMenus();
            renderAll();
        });
    });
    document.querySelectorAll("[data-filter-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            clientFilter = button.dataset.filterMode;
            currentPage = 1;
            visibleAllTasksCount = ALL_TASKS_INITIAL_LOAD;
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
    document.getElementById("loadMoreButton").addEventListener("click", () => {
        visibleAllTasksCount += ALL_TASKS_LOAD_STEP;
        renderInvoiceTable();
    });
    document.getElementById("invoiceTableBody").addEventListener("click", (event) => {
        const requestedTab = event.target.closest("[data-open-sidebar-tab]")?.dataset.openSidebarTab;
        const row = event.target.closest("tr[data-invoice-id]");
        if (!row) return;
        selectedInvoiceId = row.dataset.invoiceId || null;
        selectedClientId = row.dataset.customerId || null;
        if (!selectedInvoiceId || !selectedClientId) return;
        taskSidebarTab = normaliseTaskSidebarTab(requestedTab || "task");
        taskSidebarOpen = true;
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
        setTaskSidebarOpen(false);
        activeView = "settings";
        renderAll();
    });
    document.getElementById("openHmrcSettingsButton").addEventListener("click", async () => {
        setTaskSidebarOpen(false);
        activeView = "hmrcSettings";
        await refreshHmrcOauthStatus();
        await refreshHmrc64Tracker();
        renderAll();
    });
    document.getElementById("backToSettingsFromHmrcButton").addEventListener("click", () => {
        setTaskSidebarOpen(false);
        activeView = "settings";
        renderAll();
    });
    document.getElementById("backToLedgerFromSettingsButton").addEventListener("click", () => {
        setTaskSidebarOpen(false);
        activeView = "ledger";
        renderAll();
    });
    document.getElementById("backToLedgerButton").addEventListener("click", () => {
        setTaskSidebarOpen(false);
        activeView = "ledger";
        renderAll();
    });
    document.getElementById("openTaskSidebarButton")?.addEventListener("click", () => {
        setTaskSidebarOpen(!taskSidebarOpen);
    });
    document.getElementById("closeTaskSidebarButton")?.addEventListener("click", () => {
        setTaskSidebarOpen(false);
    });
    document.getElementById("taskSidebarBackdrop")?.addEventListener("click", () => {
        setTaskSidebarOpen(false);
    });
    document.getElementById("sidebarGoHistoryButton")?.addEventListener("click", () => {
        setTaskSidebarTab("history", { open: true });
    });
    document.querySelectorAll("[data-sidebar-tab]").forEach((button) => {
        button.addEventListener("click", () => {
            setTaskSidebarTab(button.dataset.sidebarTab || "task", { open: true });
        });
    });
    document.getElementById("matchingSearch").addEventListener("input", (event) => {
        matchingSearchTerm = event.target.value || "";
        renderSettingsScreen();
    });
    document.getElementById("saveHmrcGatewayClientIdButton").addEventListener("click", () => {
        currentHmrcWizardFormState();
        persistHmrcWizardState();
        renderHmrcSettingsScreen();
    });
    document.getElementById("connectHmrcButton").addEventListener("click", async () => {
        currentHmrcWizardFormState();
        persistHmrcWizardState();
        try {
            const payload = await requestJSON(
                api.endpoints.hmrcOAuthStart,
                { method: "POST", body: JSON.stringify({ redirectTo: hmrcWizardRedirectTarget() }) }
            );
            if (payload?.authorizeUrl) {
                window.location.assign(payload.authorizeUrl);
                return;
            }
            window.alert("HMRC login URL could not be generated.");
        } catch (error) {
            console.error("Unable to start HMRC OAuth", error);
            window.alert("Unable to start HMRC OAuth. Check HMRC credentials and try again.");
        }
    });
    document.getElementById("disconnectHmrcButton").addEventListener("click", async () => {
        try {
            await requestJSON(api.endpoints.hmrcOAuthDisconnect, { method: "POST", body: JSON.stringify({}) });
            hmrcWizardState.oauthConnected = false;
            persistHmrcWizardState();
            await refreshHmrcOauthStatus();
            renderAll();
        } catch (error) {
            console.error("Unable to disconnect HMRC OAuth", error);
            window.alert("Unable to disconnect HMRC currently.");
        }
    });
    document.getElementById("pullHmrcGatewayDetailsButton").addEventListener("click", async () => {
        currentHmrcWizardFormState();
        if (!hmrcWizardState.gatewayClientId) {
            window.alert("Enter a Gateway client ID first.");
            return;
        }
        persistHmrcWizardState();
        try {
            const detail = await fetchHmrcGatewayClientDetail(hmrcWizardState.gatewayClientId);
            hmrcWizardState.detail = detail;
            hmrcWizardState.lastPulledAt = new Date().toISOString();
            hmrcWizardState.gatewayPullStatus = buildHmrcGatewayPullSummary(detail);
            persistHmrcWizardState();
            state.customers.forEach((customer) => {
                if (!customer?.vatGatewayClientId) return;
                if (String(customer.vatGatewayClientId).trim() !== hmrcWizardState.gatewayClientId) return;
                customer.vatGatewayDetails = detail;
                customer.vatGatewayConnected = true;
            });
            persistState();
            persistClientMatches();
            renderAll();
        } catch (error) {
            console.error("Unable to pull HMRC gateway details", error);
            hmrcWizardState.gatewayPullStatus = buildHmrcGatewayPullSummary(
                null,
                `HMRC VAT gateway pull failed (${error?.message || "unknown error"})`
            );
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
            window.alert("HMRC VAT gateway details could not be pulled. Check the client ID and authorisation status.");
        }
    });
    document.getElementById("startHmrcHandshakeButton").addEventListener("click", async () => {
        const wizardPayload = currentHmrcWizardFormState();
        if (!wizardPayload.gatewayClientId || !wizardPayload.vrn) {
            window.alert("Gateway client ID and VRN are required.");
            return;
        }
        if (!wizardPayload.agentReferenceNumber) {
            window.alert("Agent Reference Number (ARN) is required.");
            return;
        }
        hmrcWizardState.handshakeStatus = "Creating HMRC authorisation link...";
        persistHmrcWizardState();
        renderHmrcSettingsScreen();
        try {
            const payload = await requestJSON(
                api.endpoints.hmrcVatAuthorisationStart,
                {
                    method: "POST",
                    body: JSON.stringify({
                        gatewayClientId: wizardPayload.gatewayClientId,
                        vrn: wizardPayload.vrn,
                        agentReferenceNumber: wizardPayload.agentReferenceNumber,
                        asaId: wizardPayload.asaId,
                    })
                }
            );
            const authorisation = payload?.authorisation || {};
            hmrcWizardState.invitationId = String(authorisation.invitationId || "");
            hmrcWizardState.clientAuthorisationUrl = String(payload?.handshake?.authorisationUrl || authorisation.authorisationUrl || "");
            hmrcWizardState.handshakeStatus = String(authorisation.status || "pending");
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
        } catch (error) {
            console.error("Unable to start HMRC VAT handshake", error);
            hmrcWizardState.handshakeStatus = "Failed to create handshake link.";
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
            window.alert("Could not create HMRC client authorisation link.");
        }
    });
    document.getElementById("checkHmrcHandshakeButton").addEventListener("click", async () => {
        const wizardPayload = currentHmrcWizardFormState();
        if (!wizardPayload.gatewayClientId || !wizardPayload.vrn) {
            window.alert("Gateway client ID and VRN are required.");
            return;
        }
        try {
            const payload = await requestJSON(
                api.endpoints.hmrcVatAuthorisationCheck,
                {
                    method: "POST",
                    body: JSON.stringify({
                        gatewayClientId: wizardPayload.gatewayClientId,
                        vrn: wizardPayload.vrn,
                        invitationId: hmrcWizardState.invitationId || "",
                    })
                }
            );
            const authorisation = payload?.authorisation || {};
            hmrcWizardState.handshakeStatus = String(authorisation.status || "pending");
            hmrcWizardState.clientAuthorisationUrl = String(authorisation.authorisationUrl || hmrcWizardState.clientAuthorisationUrl || "");
            hmrcWizardState.invitationId = String(authorisation.invitationId || hmrcWizardState.invitationId || "");
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
        } catch (error) {
            console.error("Unable to check HMRC VAT handshake status", error);
            window.alert("Unable to check HMRC authorisation status right now.");
        }
    });
    document.getElementById("openHmrcClientAuthorisationLinkButton").addEventListener("click", () => {
        const url = String(hmrcWizardState.clientAuthorisationUrl || "").trim();
        if (!url) {
            window.alert("No client authorisation link has been generated yet.");
            return;
        }
        window.open(url, "_blank", "noopener");
    });
    document.getElementById("fetchHmrcVatDataButton").addEventListener("click", async () => {
        const wizardPayload = currentHmrcWizardFormState();
        if (!wizardPayload.gatewayClientId || !wizardPayload.vrn) {
            window.alert("Gateway client ID and VRN are required.");
            return;
        }
        try {
            const [obligationsPayload, liabilitiesPayload, paymentsPayload] = await Promise.all([
                requestJSON(
                    api.endpoints.hmrcVatObligations,
                    { method: "POST", body: JSON.stringify({ gatewayClientId: wizardPayload.gatewayClientId, vrn: wizardPayload.vrn, status: "A" }) }
                ),
                requestJSON(
                    api.endpoints.hmrcVatLiabilities,
                    { method: "POST", body: JSON.stringify({ gatewayClientId: wizardPayload.gatewayClientId, vrn: wizardPayload.vrn }) }
                ),
                requestJSON(
                    api.endpoints.hmrcVatPayments,
                    { method: "POST", body: JSON.stringify({ gatewayClientId: wizardPayload.gatewayClientId, vrn: wizardPayload.vrn }) }
                ),
            ]);
            let latestReturn = null;
            const obligations = Array.isArray(obligationsPayload?.obligations) ? obligationsPayload.obligations : [];
            if (obligations.length) {
                const latestObligation = obligations[0];
                if (latestObligation?.periodKey) {
                    const returnPayload = await requestJSON(
                        api.endpoints.hmrcVatReturns,
                        { method: "POST", body: JSON.stringify({ gatewayClientId: wizardPayload.gatewayClientId, vrn: wizardPayload.vrn, periodKey: latestObligation.periodKey }) }
                    );
                    latestReturn = returnPayload?.return || null;
                }
            }
            hmrcWizardState.vatDataSnapshot = {
                fetchedAt: new Date().toISOString(),
                gatewayClientId: wizardPayload.gatewayClientId,
                vrn: wizardPayload.vrn,
                obligations: obligationsPayload?.obligations || [],
                latestReturn,
                liabilities: liabilitiesPayload?.liabilities || [],
                payments: paymentsPayload?.payments || [],
            };
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
        } catch (error) {
            console.error("Unable to fetch HMRC VAT data", error);
            window.alert("VAT data fetch failed. Ensure the client handshake is authorised and HMRC API config is complete.");
        }
    });
    document.getElementById("sendHmrc64AuthorisationButton").addEventListener("click", async () => {
        const form = currentHmrc64FormState();
        if (!form.clientName) {
            window.alert("Client name is required.");
            return;
        }
        if (!form.includeSa && !form.includeCt && !form.includePaye) {
            window.alert("Select at least one service: SA, CT, or PAYE.");
            return;
        }
        hmrcWizardState.hmrc64WizardStatus = "Creating HMRC 64-8 request...";
        persistHmrcWizardState();
        renderHmrcSettingsScreen();
        try {
            const createPayload = await requestJSON(
                api.endpoints.hmrc64Create,
                {
                    method: "POST",
                    body: JSON.stringify({
                        clientName: form.clientName,
                        clientId: form.clientId || hmrcWizardState.gatewayClientId || "",
                        postcode: form.postcode,
                        saUtr: form.saUtr,
                        ctUtr: form.ctUtr,
                        taxOfficeNumber: form.taxOfficeNumber,
                        taxOfficeReference: form.taxOfficeReference,
                        accountsOfficeReference: form.accountsOfficeReference,
                        includeSa: form.includeSa,
                        includeCt: form.includeCt,
                        includePaye: form.includePaye,
                    })
                }
            );
            const requestRow = createPayload?.request || null;
            if (!requestRow?.id) throw new Error("HMRC 64-8 request was created without an ID.");
            await requestJSON(
                api.endpoints.hmrc64Submit,
                {
                    method: "POST",
                    body: JSON.stringify({ submissionChannel: "online" })
                },
                { requestId: requestRow.id }
            );
            hmrcWizardState.hmrc64WizardStatus = "Submitted to HMRC. Status moved to Awaiting Code.";
            await refreshHmrc64Tracker();
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
        } catch (error) {
            console.error("Unable to send HMRC 64-8 authorisation", error);
            const message = String(error?.message || "");
            const likelyDuplicate = /already|exists|duplicate/i.test(message);
            if (likelyDuplicate) {
                try {
                    const outcomes = await recoverExistingHmrc64Requests(form);
                    hmrcWizardState.hmrc64WizardStatus = `Existing request detected. ${hmrc64FormatServiceOutcomeSummary(outcomes)}`;
                    persistHmrcWizardState();
                    renderHmrcSettingsScreen();
                    return;
                } catch (recoveryError) {
                    console.error("Unable to recover existing HMRC 64-8 request flow", recoveryError);
                    hmrcWizardState.hmrc64WizardStatus = `Existing request recovery failed (${recoveryError?.message || "unknown error"}).`;
                    persistHmrcWizardState();
                    renderHmrcSettingsScreen();
                    window.alert("64-8 request already exists, but recovery failed. Refresh tracker and retry.");
                    return;
                }
            }
            hmrcWizardState.hmrc64WizardStatus = `Failed to submit 64-8 authorisation (${message || "unknown error"}).`;
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
            window.alert("64-8 submission failed. Check required SA/CT/PAYE fields.");
        }
    });
    document.getElementById("refreshHmrc64TrackerButton").addEventListener("click", async () => {
        await refreshHmrc64Tracker();
        renderHmrcSettingsScreen();
    });
    document.getElementById("hmrc64TrackerTableBody").addEventListener("click", async (event) => {
        const button = event.target.closest("[data-hmrc64-code-received]");
        if (!button) return;
        const requestId = button.getAttribute("data-hmrc64-code-received") || "";
        if (!requestId) return;
        const codeInput = document.querySelector(`[data-hmrc64-code-input="${requestId}"]`);
        const authorityCode = (codeInput?.value || "").trim().toUpperCase();
        if (!authorityCode) {
            window.alert("Enter the authority code first.");
            return;
        }
        button.disabled = true;
        button.textContent = "Updating...";
        try {
            await requestJSON(
                api.endpoints.hmrc64CaptureCode,
                {
                    method: "POST",
                    body: JSON.stringify({ authorityCode, activateNow: true })
                },
                { requestId }
            );
            hmrcWizardState.hmrc64WizardStatus = "Code received and client marked authorised.";
            await refreshHmrc64Tracker();
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
        } catch (error) {
            console.error("Unable to mark HMRC 64-8 as code received", error);
            hmrcWizardState.hmrc64WizardStatus = "Failed to capture code.";
            persistHmrcWizardState();
            renderHmrcSettingsScreen();
            window.alert("Could not mark code received. Check code format for selected service.");
        } finally {
            button.disabled = false;
            button.textContent = "Code Received";
        }
    });
    document.getElementById("clientInvoiceSelect").addEventListener("change", (event) => {
        selectedInvoiceId = event.target.value;
        renderClientScreen();
    });
    document.getElementById("reviewerNameInput").addEventListener("input", (event) => {
        if (!selectedClientId) return;
        updateWorkflowStateForClient(selectedClientId, { reviewerName: event.target.value });
        renderReviewerWorkflowPanel(findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId));
    });
    document.getElementById("reviewerDecisionSelect").addEventListener("change", (event) => {
        if (!selectedClientId) return;
        const reviewerName = document.getElementById("reviewerNameInput").value || "";
        updateWorkflowStateForClient(selectedClientId, {
            reviewerName,
            reviewerStatus: event.target.value,
            reviewerDecisionAt: new Date().toISOString()
        });
        renderReviewerWorkflowPanel(findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId));
    });
    document.getElementById("approveWorkflowButton").addEventListener("click", () => {
        if (!selectedClientId) return;
        const reviewerName = document.getElementById("reviewerNameInput").value || "";
        updateWorkflowStateForClient(selectedClientId, {
            reviewerName,
            reviewerStatus: "approved",
            reviewerDecisionAt: new Date().toISOString()
        });
        renderReviewerWorkflowPanel(findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId));
    });
    document.getElementById("openBookkeepingEmailButton").addEventListener("click", () => {
        if (!selectedClientId) return;
        const client = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
        const workflow = workflowStateForClient(selectedClientId);
        if (workflow.reviewerStatus !== "approved") {
            window.alert("Reviewer approval is required before opening the final email step.");
            return;
        }
        const recipient = encodeURIComponent(String(client?.clientProfile?.email || client?.email || "").trim());
        const subject = encodeURIComponent(`${client?.name || "Client"} bookkeeping snapshot and planning update`);
        const body = encodeURIComponent(bookkeepingEmailDraft(client));
        window.location.href = `mailto:${recipient}?subject=${subject}&body=${body}`;
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
        invalidateInvoiceCaches();
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

    document.getElementById("sidebarManagerSelect").addEventListener("change", (event) => {
        const customInput = document.getElementById("sidebarManagerCustomInput");
        if (!customInput) return;
        customInput.disabled = event.target.value !== "__custom";
        if (customInput.disabled) customInput.value = "";
    });
    document.getElementById("sidebarSaveManagerButton").addEventListener("click", async () => {
        const client = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
        if (!client) return;
        const managerSelect = document.getElementById("sidebarManagerSelect");
        const managerCustomInput = document.getElementById("sidebarManagerCustomInput");
        const selectedManager = managerSelect?.value === "__custom"
            ? (managerCustomInput?.value || "").trim()
            : (managerSelect?.value || "").trim();
        client.manager = selectedManager || "Unassigned";
        if (client.clientProfile && typeof client.clientProfile === "object") {
            client.clientProfile.clientManager = client.manager;
        }
        if (clientProfileDraft && selectedClientId === clientProfileClientId) {
            clientProfileDraft.clientManager = client.manager;
        }
        persistState();
        renderAll();
        try {
            await requestJSON(
                api.endpoints.customerProfile,
                { method: "PATCH", body: JSON.stringify({ clientManager: client.manager }) },
                { customerId: selectedClientId }
            );
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save client manager", error);
            if (error instanceof AuthRequiredError) {
                markAuthenticationRequired(error.message);
            }
        }
    });
    document.getElementById("sidebarApplyStatusButton").addEventListener("click", async () => {
        const client = findCustomerById(selectedClientId) || findCustomerByInvoiceId(selectedInvoiceId);
        const selectedInvoice = findInvoiceById(selectedInvoiceId);
        if (!client || !selectedInvoice) return;
        const statusValue = document.getElementById("sidebarStatusSelect")?.value || "Outstanding";
        const noteInput = document.getElementById("sidebarStatusNote");
        const note = (noteInput?.value || "").trim();
        const applyToAllOpen = document.getElementById("sidebarApplyAllOpenCheck")?.checked === true;
        const invoiceIds = applyToAllOpen
            ? (client.invoices || []).filter((invoice) => invoiceCategory(invoice) !== "paid").map((invoice) => invoice.id)
            : [selectedInvoice.id];
        invoiceIds.forEach((invoiceId) => {
            const match = mutableInvoiceById(invoiceId);
            if (!match) return;
            match.invoice.controlStatus = statusValue;
            match.invoice.statuses = [{ title: statusValue, body: note, stamp: new Date().toISOString() }, ...(match.invoice.statuses || [])];
        });
        if (noteInput) noteInput.value = "";
        state.panelSummary = null;
        invalidateInvoiceCaches();
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
    document.getElementById("sidebarSavePromiseButton").addEventListener("click", async () => {
        const match = mutableInvoiceById(selectedInvoiceId);
        const promiseInput = document.getElementById("sidebarPromiseDateInput");
        const promisedDate = promiseInput?.value || "";
        if (!match || !promisedDate) return;
        match.invoice.promisedDate = promisedDate;
        match.invoice.controlStatus = "Promise Received";
        match.invoice.statuses = [{
            title: "Promise Received",
            body: `Promised payment date set to ${formatDate(promisedDate)}.`,
            stamp: new Date().toISOString()
        }, ...(match.invoice.statuses || [])];
        invalidateInvoiceCaches();
        persistState();
        renderAll();
        try {
            await requestJSON(
                api.endpoints.promise,
                { method: "POST", body: JSON.stringify({ promisedDate }) },
                { invoiceId: match.invoice.id }
            );
            await hydrateFromAPI();
            renderAll();
        } catch (error) {
            console.error("Unable to save promised payment date", error);
            if (error instanceof AuthRequiredError) {
                markAuthenticationRequired(error.message);
            }
        }
    });
}

async function init() {
    consumeHmrcCallbackParams();
    const hasWarmLedger = Array.isArray(state.customers) && state.customers.length > 0;
    setAppLoadingState(true, hasWarmLedger ? "Refreshing client profile..." : "Loading client profile...");
    renderAll();
    const hmrcRefreshPromise = Promise.all([refreshHmrcOauthStatus(), refreshHmrc64Tracker()]);
    const longLoadHintTimer = window.setTimeout(() => {
        setAppLoadingState(true, "Still loading client profile. Large ledgers can take a moment.");
    }, 1600);
    await hydrateFromAPI();
    window.clearTimeout(longLoadHintTimer);
    renderAll();
    setAppLoadingState(false);
    wireFilters();
    wireLoginButtons();
    wireSyncButtons();
    wireForms();
    window.addEventListener("beforeunload", flushPendingPersistence);
    void hmrcRefreshPromise.then(() => {
        if (activeView === "hmrcSettings") renderHmrcSettingsScreen();
    });
}
init();
