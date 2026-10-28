import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { fetchApi, readApiError } from "@/lib/api";

interface McpTool {
    name: string;
    title: string;
}

interface McpGovernance {
    projectScope: "all" | "listed";
    projectIds: string[];
    disabledTools: string[];
}

interface McpGovernanceView extends McpGovernance {
    tools: McpTool[];
}

interface ProjectOption {
    id: string;
    name: string;
}

interface McpAuditRow {
    id: number;
    at: string;
    user_email: string;
    auth_type: string;
    client_id: string;
    token_id: string;
    method: string;
    tool: string;
    project_id: string | null;
    revision: string | null;
    outcome: string;
    code: string | null;
    duration_ms: number;
}

interface McpAuditPage {
    rows: McpAuditRow[];
    next: number | null;
}

const AUDIT_LIMIT = 100;

const EMPTY_GOVERNANCE: McpGovernanceView = {
    projectScope: "all",
    projectIds: [],
    disabledTools: [],
    tools: [],
};

function timeLabel(value: string): string {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** Settings → MCP: what clients may reach, and what they did (ADR §3.11, D7/D19). */
export function McpGovernanceSettings() {
    const [governance, setGovernance] = useState<McpGovernanceView>(EMPTY_GOVERNANCE);
    const [projects, setProjects] = useState<ProjectOption[]>([]);
    const [scope, setScope] = useState<"all" | "listed">("all");
    const [selected, setSelected] = useState<string[]>([]);
    const [disabled, setDisabled] = useState<string[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);

    const loadSettings = useCallback(async (signal: AbortSignal) => {
        setLoading(true);
        try {
            const response = await fetchApi("/api/settings/mcp", { signal });
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to load MCP settings"));
            }
            const value = (await response.json()) as McpGovernanceView;
            if (signal.aborted) return;
            setGovernance(value);
            setScope(value.projectScope);
            setSelected(value.projectIds);
            setDisabled(value.disabledTools);
        } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") return;
            toast.error(error instanceof Error ? error.message : "Failed to load MCP settings");
        } finally {
            if (!signal.aborted) {
                // Already in `finally`; the rule reads the abort guard as a
                // success-only branch. An aborted load has a successor in flight
                // that owns the flag.
                // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally
                setLoading(false);
            }
        }
    }, []);

    useEffect(() => {
        const controller = new AbortController();
        void loadSettings(controller.signal);
        return () => controller.abort();
    }, [loadSettings]);

    useEffect(() => {
        const controller = new AbortController();
        void fetchApi("/api/projects/", { signal: controller.signal })
            .then((response) => (response.ok ? response.json() : []))
            .then((value: ProjectOption[]) => {
                if (!controller.signal.aborted) setProjects(value);
            })
            .catch(() => undefined);
        return () => controller.abort();
    }, []);

    const toggleTool = (name: string, next: boolean) => {
        setDisabled((current) => next ? [...current, name] : current.filter((tool) => tool !== name));
    };

    const toggleProject = (id: string, next: boolean) => {
        setSelected((current) => next ? [...current, id] : current.filter((project) => project !== id));
    };

    const save = async () => {
        setSaving(true);
        try {
            const response = await fetchApi("/api/settings/mcp", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    projectScope: scope,
                    projectIds: scope === "listed" ? selected : [],
                    disabledTools: disabled,
                }),
            });
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to save MCP settings"));
            }
            const saved = (await response.json()) as McpGovernance;
            setScope(saved.projectScope);
            setSelected(saved.projectIds);
            setDisabled(saved.disabledTools);
            toast.success("MCP settings saved");
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to save MCP settings");
        } finally {
            setSaving(false);
        }
    };

    const selectedProjects = new Set(selected);
    const disabledTools = new Set(disabled);
    const enabledTools = new Set<string>();
    for (const tool of governance.tools) {
        if (!disabledTools.has(tool.name)) enabledTools.add(tool.name);
    }

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <h3 className="text-lg font-medium">MCP</h3>
                <Button size="sm" onClick={() => void save()} disabled={loading || saving}>
                    {saving ? "Saving…" : "Save"}
                </Button>
            </div>

            <div className="space-y-2">
                <Label>Project scope</Label>
                <Select value={scope} onValueChange={(value) => setScope(value as "all" | "listed")}>
                    <SelectTrigger className="w-48" aria-label="Project scope">
                        <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                        <SelectItem value="all">All projects</SelectItem>
                        <SelectItem value="listed">Listed projects</SelectItem>
                    </SelectContent>
                </Select>
                {scope === "listed" && (
                    <div className="max-h-56 overflow-y-auto rounded-md border">
                        {projects.length === 0 ? (
                            <p className="px-3 py-2 text-xs text-muted-foreground">No projects</p>
                        ) : projects.map((project) => (
                            <label
                                key={project.id}
                                className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-xs"
                            >
                                <Checkbox
                                    checked={selectedProjects.has(project.id)}
                                    onCheckedChange={(checked) => toggleProject(project.id, checked === true)}
                                />
                                <span className="truncate" title={project.name}>{project.name}</span>
                            </label>
                        ))}
                    </div>
                )}
            </div>

            <div className="space-y-1">
                <Label>Tools</Label>
                <div className="divide-y rounded-md border">
                    {governance.tools.map((tool) => (
                        <div key={tool.name} className="flex items-center gap-3 px-3 py-1.5 text-xs">
                            <span className="min-w-0 flex-1 truncate" title={tool.name}>{tool.title}</span>
                            <Switch
                                checked={enabledTools.has(tool.name)}
                                aria-label={tool.name}
                                onCheckedChange={(checked) => toggleTool(tool.name, !checked)}
                            />
                        </div>
                    ))}
                </div>
            </div>

            <AuditTable
                projects={projects}
                tools={governance.tools}
            />
        </div>
    );
}

interface AuditTableProps {
    projects: ProjectOption[];
    tools: McpTool[];
}

function AuditTable({ projects, tools }: AuditTableProps) {
    const [rows, setRows] = useState<McpAuditRow[]>([]);
    const [next, setNext] = useState<number | null>(null);
    const [loading, setLoading] = useState(false);
    const [client, setClient] = useState("");
    const [project, setProject] = useState("");
    const [tool, setTool] = useState("");
    const [token, setToken] = useState("");
    const [kind, setKind] = useState("");

    const load = useCallback(
        async (options: { reset: boolean; before?: number | null; signal?: AbortSignal }) => {
            const params = new URLSearchParams({ limit: String(AUDIT_LIMIT) });
            if (client.trim()) params.set("client_id", client.trim());
            if (project) params.set("project_id", project);
            if (tool) params.set("tool", tool);
            if (token.trim()) params.set("token_id", token.trim());
            if (kind) params.set("kind", kind);
            if (!options.reset && options.before !== null && options.before !== undefined) {
                params.set("before", String(options.before));
            }
            setLoading(true);
            try {
                const response = await fetchApi(`/api/admin/mcp-audit?${params.toString()}`, {
                    signal: options.signal,
                });
                if (!response.ok) {
                    throw new Error(await readApiError(response, "Failed to load the MCP audit"));
                }
                const page = (await response.json()) as McpAuditPage;
                if (options.signal?.aborted) return;
                setRows((current) => options.reset ? page.rows : [...current, ...page.rows]);
                setNext(page.next);
            } catch (error) {
                if (error instanceof DOMException && error.name === "AbortError") return;
                toast.error(error instanceof Error ? error.message : "Failed to load the MCP audit");
            } finally {
                if (!options.signal?.aborted) {
                    // Already in `finally`; the rule reads the abort guard as a
                    // success-only branch. An aborted load has a successor in
                    // flight that owns the flag.
                    // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally
                    setLoading(false);
                }
            }
        },
        [client, project, tool, token, kind],
    );

    useEffect(() => {
        const controller = new AbortController();
        void load({ reset: true, signal: controller.signal });
        return () => controller.abort();
    }, [load]);

    return (
        <div className="space-y-2">
            <Label>Audit</Label>
            <div className="flex flex-wrap items-center gap-2">
                <Input
                    value={client}
                    onChange={(event) => setClient(event.target.value)}
                    placeholder="Client"
                    className="h-8 w-44 text-xs"
                    aria-label="Audit client filter"
                />
                <Input
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                    placeholder="Token"
                    className="h-8 w-44 text-xs"
                    aria-label="Audit token filter"
                />
                <Select value={project || "all"} onValueChange={(value) => setProject(value === "all" ? "" : value)}>
                    <SelectTrigger size="sm" className="w-44" aria-label="Audit project filter">
                        <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                        <SelectItem value="all">All projects</SelectItem>
                        {projects.map((option) => (
                            <SelectItem key={option.id} value={option.id}>{option.name}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
                <Select value={tool || "all"} onValueChange={(value) => setTool(value === "all" ? "" : value)}>
                    <SelectTrigger size="sm" className="w-44" aria-label="Audit tool filter">
                        <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                        <SelectItem value="all">All tools</SelectItem>
                        {tools.map((option) => (
                            <SelectItem key={option.name} value={option.name}>{option.name}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
                <Select value={kind || "all"} onValueChange={(value) => setKind(value === "all" ? "" : value)}>
                    <SelectTrigger size="sm" className="w-36" aria-label="Audit kind filter">
                        <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                        <SelectItem value="all">Reads and writes</SelectItem>
                        <SelectItem value="read">Reads</SelectItem>
                        <SelectItem value="write">Writes</SelectItem>
                    </SelectContent>
                </Select>
            </div>

            <div className="overflow-x-auto rounded-md border">
                <table className="w-full min-w-[58rem] table-fixed text-xs">
                    <colgroup>
                        <col className="w-32" />
                        <col />
                        <col className="w-36" />
                        <col className="w-24" />
                        <col className="w-28" />
                        <col className="w-28" />
                        <col className="w-20" />
                        <col className="w-20" />
                    </colgroup>
                    <thead className="border-b bg-muted/30 text-left text-muted-foreground">
                        <tr>
                            <th scope="col" className="px-2 py-1.5 font-medium">Time</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">User</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">Client</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">Token</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">Tool</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">Project</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">Revision</th>
                            <th scope="col" className="px-2 py-1.5 font-medium">Outcome</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y">
                        {rows.length === 0 ? (
                            <tr>
                                <td colSpan={8} className="px-2 py-2 text-muted-foreground">
                                    {loading ? "Loading…" : "No activity"}
                                </td>
                            </tr>
                        ) : rows.map((row) => (
                            <tr key={row.id}>
                                <Cell value={timeLabel(row.at)} label={timeLabel(row.at)} />
                                <Cell value={row.user_email} label={row.user_email} />
                                <Cell value={row.client_id} label={row.client_id} />
                                <Cell value={row.token_id.slice(0, 8)} label={row.token_id} />
                                <Cell value={row.tool} label={row.tool} />
                                <Cell value={row.project_id ?? ""} label={row.project_id ?? ""} />
                                <Cell value={row.revision ? row.revision.slice(0, 8) : ""} label={row.revision ?? ""} />
                                <Cell value={row.outcome} label={row.code ?? row.outcome} />
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {next !== null && (
                <Button
                    variant="outline"
                    size="sm"
                    disabled={loading}
                    onClick={() => void load({ reset: false, before: next })}
                >
                    Load more
                </Button>
            )}
        </div>
    );
}

function Cell({ value, label }: { value: string; label: string }) {
    return (
        <td className="px-2 py-1.5">
            <span className="block truncate" title={label}>{value}</span>
        </td>
    );
}
