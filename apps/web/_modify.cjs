const fs = require("fs");
const f = "d:/MyCode/WorkAMA/apps/web/src/App.tsx";
let c = fs.readFileSync(f, "utf8");

// 1. Add lazy imports after FreeProvidersPage import
const newImports = "\n" +
"const AdminLayout = lazy(() => import('./admin-layout').then(m => ({ default: m.AdminLayout })))\n" +
"const AdminDashboardPage = lazy(() => import('./admin-dashboard-page'))\n" +
"const AdminWorkspacesPage = lazy(() => import('./workspaces-page'))\n" +
"const AdminAssistantsPage = lazy(() => import('./assistants-page'))\n" +
"const AdminWorkflowsPage = lazy(() => import('./workflows-page'))\n" +
"const AdminKnowledgeBasesPage = lazy(() => import('./knowledge-bases-page'))\n" +
"const AdminDevicesPage = lazy(() => import('./devices-page'))\n" +
"const AdminBillingPage = lazy(() => import('./billing-page'))\n" +
"const AdminAuditLogsPage = lazy(() => import('./audit-logs-page'))\n" +
"const AdminMcpToolsPage = lazy(() => import('./mcp-tools-page'))\n" +
"const AdminNotificationsPage = lazy(() => import('./notifications-page'))\n" +
"const AdminFilesPage = lazy(() => import('./files-page'))\n" +
"const AdminMemoryVectorsPage = lazy(() => import('./memory-vectors-page'))";

const fpMarker = "const FreeProvidersPage = lazy(() => import('./free-providers-page'))";
if (!c.includes(fpMarker)) { console.error("MARKER NOT FOUND: FreeProvidersPage import"); process.exit(1); }
c = c.replace(fpMarker, fpMarker + newImports);

// 2. Remove old admin routes from ConsoleLayout block (will be re-added under AdminLayout)
const routesToRemove = [
  '<Route path="/admin/workspaces" element={<WorkspacesPage />} />',
  '<Route path="/admin/notifications" element={<NotificationsPage />} />',
  '<Route path="/admin/devices" element={<DevicesPage />} />',
  '<Route path="/admin/billing" element={<BillingPage />} />',
  '<Route path="/admin/free-providers" element={<FreeProvidersPage />} />',
];
for (const route of routesToRemove) {
  const idx = c.indexOf(route);
  if (idx === -1) { console.error("ROUTE NOT FOUND: " + route); process.exit(1); }
  // Remove the line including leading whitespace
  const lineStart = c.lastIndexOf("\n", idx) + 1;
  const lineEnd = c.indexOf("\n", idx);
  c = c.substring(0, lineStart) + c.substring(lineEnd + 1);
}

// 3. Add AdminLayout route block before RequireAuth closing
const adminBlock = "      <Route element={<AdminLayout />}>\\n" +
"        <Route element={<SuspendedOutlet />}>\\n" +
"          <Route path=\"/admin\" element={<AdminDashboardPage />} />\\n" +
"          <Route path=\"/admin/workspaces\" element={<AdminWorkspacesPage />} />\\n" +
"          <Route path=\"/admin/assistants\" element={<AdminAssistantsPage />} />\\n" +
"          <Route path=\"/admin/workflows\" element={<AdminWorkflowsPage />} />\\n" +
"          <Route path=\"/admin/knowledge-bases\" element={<AdminKnowledgeBasesPage />} />\\n" +
"          <Route path=\"/admin/devices\" element={<AdminDevicesPage />} />\\n" +
"          <Route path=\"/admin/billing\" element={<AdminBillingPage />} />\\n" +
"          <Route path=\"/admin/audit-logs\" element={<AdminAuditLogsPage />} />\\n" +
"          <Route path=\"/admin/mcp-tools\" element={<AdminMcpToolsPage />} />\\n" +
"          <Route path=\"/admin/notifications\" element={<AdminNotificationsPage />} />\\n" +
"          <Route path=\"/admin/files\" element={<AdminFilesPage />} />\\n" +
"          <Route path=\"/admin/memory-vectors\" element={<AdminMemoryVectorsPage />} />\\n" +
"          <Route path=\"/admin/free-providers\" element={<FreeProvidersPage />} />\\n" +
"        </Route>\\n" +
"      </Route>\\n";

// Insert before the RequireAuth closing </Route> that precedes <Route path="*"
const closePattern = "    </Route>\n    <Route path=\"*\" element={<NotFound />} />";
if (!c.includes(closePattern)) { console.error("CLOSE PATTERN NOT FOUND"); process.exit(1); }
c = c.replace(closePattern, "      " + adminBlock.replace(/\\\\n/g, "\n").replace(/\\n/g, "\n") + "    </Route>\n    <Route path=\"*\" element={<NotFound />} />");

fs.writeFileSync(f, c, "utf8");
console.log("App.tsx modified successfully");
