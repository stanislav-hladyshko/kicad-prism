import { Fragment, useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
    ChevronDown,
    ChevronRight,
    Copy,
    FlaskConical,
    KeyRound,
    Pencil,
    Plus,
    Power,
    PowerOff,
    Trash2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ConfirmDialog, useConfirmTarget } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { fetchApi, readApiError } from "@/lib/api";

/** Every event kind a webhook may subscribe to; keep in step with event_log.EVENT_KINDS. */
const EVENT_KINDS = [
    "revision.synced",
    "bundle.built",
    "bundle.failed",
    "job.progress",
    "job.finished",
    "comparison.done",
    "comment.changed",
    "release.candidate_built",
    "release.approved",
] as const;

type WebhookStatus = "active" | "failing" | "disabled";

interface WebhookRegistration {
    id: string;
    url: string;
    eventKinds: string[];
    projectIds: string[] | null;
    status: WebhookStatus;
    cursor: number;
    attempts: number;
    failingSince: string | null;
    nextAttemptAt: string | null;
    lastStatus: number | null;
    lastError: string | null;
    lastDeliveryAt: string | null;
    createdBy: string | null;
    createdAt: string;
}

interface CreatedWebhook extends WebhookRegistration {
    secret: string;
}

interface WebhookTestResult {
    statusCode: number | null;
    ok: boolean;
    error: string | null;
    durationMs: number;
}

interface WebhookDelivery {
    id: number;
    eventId: number | null;
    at: string;
    statusCode: number | null;
    ok: boolean;
    error: string | null;
    durationMs: number | null;
}

interface ProjectOption {
    id: string;
    name: string;
}

interface WebhookFormValues {
    url: string;
    eventKinds: string[];
    projectIds: string[] | null;
}

function timeLabel(value: string | null): string {
    if (!value) return "Never";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** Settings → Webhooks: outbound endpoints that receive the event log (ADR §3.10). */
export function WebhookSettings() {
    const [webhooks, setWebhooks] = useState<WebhookRegistration[]>([]);
    const [projects, setProjects] = useState<ProjectOption[]>([]);
    const [loading, setLoading] = useState(true);
    const [formId, setFormId] = useState<string | "new" | null>(null);
    const [saving, setSaving] = useState(false);
    const [secret, setSecret] = useState<string | null>(null);
    const [testing, setTesting] = useState<string | null>(null);
    const [testResults, setTestResults] = useState<Record<string, WebhookTestResult>>({});
    const [expanded, setExpanded] = useState<string | null>(null);
    const [deliveries, setDeliveries] = useState<Record<string, WebhookDelivery[]>>({});
    const [deliveriesLoading, setDeliveriesLoading] = useState<string | null>(null);
    const deleteTarget = useConfirmTarget<WebhookRegistration>();

    const load = useCallback(async (signal?: AbortSignal) => {
        setLoading(true);
        try {
            const response = await fetchApi("/api/settings/webhooks", { signal });
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to load webhooks"));
            }
            const value = (await response.json()) as WebhookRegistration[];
            if (!signal?.aborted) setWebhooks(value);
        } catch (error) {
            if (error instanceof DOMException && error.name === "AbortError") return;
            toast.error(error instanceof Error ? error.message : "Failed to load webhooks");
        } finally {
            if (!signal?.aborted) {
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
        void load(controller.signal);
        return () => controller.abort();
    }, [load]);

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

    const editing = formId && formId !== "new"
        ? webhooks.find((webhook) => webhook.id === formId) ?? null
        : null;

    const save = async (values: WebhookFormValues) => {
        setSaving(true);
        try {
            const creating = formId === "new";
            const response = await fetchApi(
                creating ? "/api/settings/webhooks" : `/api/settings/webhooks/${encodeURIComponent(formId ?? "")}`,
                {
                    method: creating ? "POST" : "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(values),
                },
            );
            if (!response.ok) {
                throw new Error(await readApiError(response, creating ? "Failed to create webhook" : "Failed to update webhook"));
            }
            if (creating) {
                const created = (await response.json()) as CreatedWebhook;
                setSecret(created.secret);
            } else {
                await response.json();
            }
            setFormId(null);
            await load();
            toast.success(creating ? "Webhook created" : "Webhook updated");
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to save webhook");
        } finally {
            setSaving(false);
        }
    };

    const setStatus = async (webhook: WebhookRegistration, status: "active" | "disabled") => {
        try {
            const response = await fetchApi(`/api/settings/webhooks/${encodeURIComponent(webhook.id)}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ status }),
            });
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to update webhook"));
            }
            await response.json();
            await load();
            toast.success(status === "active" ? "Webhook enabled" : "Webhook disabled");
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to update webhook");
        }
    };

    const rotate = async (webhook: WebhookRegistration) => {
        try {
            const response = await fetchApi(
                `/api/settings/webhooks/${encodeURIComponent(webhook.id)}/rotate-secret`,
                { method: "POST" },
            );
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to rotate the signing secret"));
            }
            const rotated = (await response.json()) as { secret: string };
            setSecret(rotated.secret);
            toast.success("Signing secret rotated");
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to rotate the signing secret");
        }
    };

    const test = async (webhook: WebhookRegistration) => {
        setTesting(webhook.id);
        try {
            const response = await fetchApi(
                `/api/settings/webhooks/${encodeURIComponent(webhook.id)}/test`,
                { method: "POST" },
            );
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to test the webhook"));
            }
            const result = (await response.json()) as WebhookTestResult;
            setTestResults((current) => ({ ...current, [webhook.id]: result }));
        } catch (error) {
            setTestResults((current) => ({
                ...current,
                [webhook.id]: {
                    statusCode: null,
                    ok: false,
                    error: error instanceof Error ? error.message : "Failed to test the webhook",
                    durationMs: 0,
                },
            }));
        } finally {
            setTesting(null);
        }
    };

    const toggleDeliveries = async (webhook: WebhookRegistration) => {
        if (expanded === webhook.id) {
            setExpanded(null);
            return;
        }
        setExpanded(webhook.id);
        setDeliveriesLoading(webhook.id);
        try {
            const response = await fetchApi(
                `/api/settings/webhooks/${encodeURIComponent(webhook.id)}/deliveries`,
            );
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to load deliveries"));
            }
            const rows = (await response.json()) as WebhookDelivery[];
            setDeliveries((current) => ({ ...current, [webhook.id]: rows }));
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to load deliveries");
        } finally {
            setDeliveriesLoading(null);
        }
    };

    const remove = async (webhook: WebhookRegistration) => {
        try {
            const response = await fetchApi(`/api/settings/webhooks/${encodeURIComponent(webhook.id)}`, {
                method: "DELETE",
            });
            if (!response.ok) {
                throw new Error(await readApiError(response, "Failed to delete webhook"));
            }
            toast.success("Webhook deleted");
            await load();
        } catch (error) {
            toast.error(error instanceof Error ? error.message : "Failed to delete webhook");
        }
    };

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between">
                <h3 className="text-lg font-medium">Webhooks</h3>
                <Button size="sm" onClick={() => setFormId("new")} disabled={formId === "new"}>
                    <Plus aria-hidden="true" />
                    Add webhook
                </Button>
            </div>

            {formId !== null && (
                <WebhookForm
                    key={formId}
                    webhook={editing}
                    projects={projects}
                    saving={saving}
                    onCancel={() => setFormId(null)}
                    onSubmit={(values) => void save(values)}
                />
            )}

            {secret !== null && <SecretPanel secret={secret} onDismiss={() => setSecret(null)} />}

            <WebhookTable
                webhooks={webhooks}
                projects={projects}
                loading={loading}
                testing={testing}
                testResults={testResults}
                expanded={expanded}
                deliveries={deliveries}
                deliveriesLoading={deliveriesLoading}
                onEdit={(webhook) => setFormId(webhook.id)}
                onDelete={(webhook) => deleteTarget.request(webhook)}
                onSetStatus={(webhook, status) => void setStatus(webhook, status)}
                onRotate={(webhook) => void rotate(webhook)}
                onTest={(webhook) => void test(webhook)}
                onToggleDeliveries={(webhook) => void toggleDeliveries(webhook)}
            />

            <ConfirmDialog
                open={deleteTarget.open}
                onOpenChange={(next) => {
                    if (!next) deleteTarget.clear();
                }}
                title="Delete webhook"
                description={
                    deleteTarget.target
                        ? `${deleteTarget.target.url} stops receiving events immediately.`
                        : ""
                }
                confirmLabel="Hold to delete"
                requireHold
                onConfirm={() => {
                    const target = deleteTarget.target;
                    deleteTarget.clear();
                    if (target) void remove(target);
                }}
            />
        </div>
    );
}

interface WebhookFormProps {
    webhook: WebhookRegistration | null;
    projects: ProjectOption[];
    saving: boolean;
    onCancel: () => void;
    onSubmit: (values: WebhookFormValues) => void;
}

function WebhookForm({ webhook, projects, saving, onCancel, onSubmit }: WebhookFormProps) {
    const [url, setUrl] = useState(webhook?.url ?? "");
    const [eventKinds, setEventKinds] = useState<string[]>(webhook?.eventKinds ?? []);
    const [scope, setScope] = useState<"all" | "listed">(webhook?.projectIds ? "listed" : "all");
    const [projectIds, setProjectIds] = useState<string[]>(webhook?.projectIds ?? []);

    const toggleEvent = (kind: string, checked: boolean) => {
        setEventKinds((current) => checked ? [...current, kind] : current.filter((value) => value !== kind));
    };

    const toggleProject = (id: string, checked: boolean) => {
        setProjectIds((current) => checked ? [...current, id] : current.filter((value) => value !== id));
    };

    const selectedEvents = new Set(eventKinds);
    const selectedProjects = new Set(projectIds);

    return (
        <form
            onSubmit={(event) => {
                event.preventDefault();
                if (!url.trim()) return;
                onSubmit({
                    url: url.trim(),
                    eventKinds,
                    projectIds: scope === "listed" ? projectIds : null,
                });
            }}
        >
            <div className="space-y-3 rounded-lg border p-4">
                <div className="space-y-1.5">
                    <Label htmlFor="webhook-url">URL</Label>
                    <Input
                        id="webhook-url"
                        aria-label="Webhook URL"
                        placeholder="https://example.com/hooks/prism"
                        value={url}
                        onChange={(event) => setUrl(event.target.value)}
                    />
                </div>

                <div className="space-y-1.5">
                    <Label>Event kinds</Label>
                    <div className="flex flex-wrap gap-x-4 gap-y-2">
                        {EVENT_KINDS.map((kind) => (
                            <label key={kind} className="flex items-center gap-1.5 text-xs font-mono">
                                <Checkbox
                                    aria-label={kind}
                                    checked={selectedEvents.has(kind)}
                                    onCheckedChange={(checked) => toggleEvent(kind, checked === true)}
                                />
                                {kind}
                            </label>
                        ))}
                    </div>
                </div>

                <div className="space-y-1.5">
                    <Label>Projects</Label>
                    <Select value={scope} onValueChange={(value) => setScope(value as "all" | "listed")}>
                        <SelectTrigger className="w-48" aria-label="Webhook project scope">
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
                                        aria-label={project.name}
                                        checked={selectedProjects.has(project.id)}
                                        onCheckedChange={(checked) => toggleProject(project.id, checked === true)}
                                    />
                                    <span className="truncate" title={project.name}>{project.name}</span>
                                </label>
                            ))}
                        </div>
                    )}
                </div>

                <div className="flex justify-end gap-2">
                    <Button type="button" variant="outline" size="sm" onClick={onCancel} disabled={saving}>
                        Cancel
                    </Button>
                    <Button type="submit" size="sm" disabled={saving || !url.trim()}>
                        {saving ? "Saving…" : webhook ? "Save" : "Create"}
                    </Button>
                </div>
            </div>
        </form>
    );
}

function SecretPanel({ secret, onDismiss }: { secret: string; onDismiss: () => void }) {
    const copy = () => {
        void navigator.clipboard.writeText(secret);
        toast.success("Webhook secret copied to clipboard");
    };

    return (
        <div className="flex items-center gap-2">
            <Input
                readOnly
                aria-label="New webhook secret"
                className="font-mono text-xs"
                value={secret}
            />
            <Button type="button" size="icon" variant="ghost" title="Copy secret" aria-label="Copy webhook secret" onClick={copy}>
                <Copy className="h-4 w-4" aria-hidden="true" />
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={onDismiss}>
                Dismiss
            </Button>
        </div>
    );
}

interface WebhookTableProps {
    webhooks: WebhookRegistration[];
    projects: ProjectOption[];
    loading: boolean;
    testing: string | null;
    testResults: Record<string, WebhookTestResult>;
    expanded: string | null;
    deliveries: Record<string, WebhookDelivery[]>;
    deliveriesLoading: string | null;
    onEdit: (webhook: WebhookRegistration) => void;
    onDelete: (webhook: WebhookRegistration) => void;
    onSetStatus: (webhook: WebhookRegistration, status: "active" | "disabled") => void;
    onRotate: (webhook: WebhookRegistration) => void;
    onTest: (webhook: WebhookRegistration) => void;
    onToggleDeliveries: (webhook: WebhookRegistration) => void;
}

function WebhookTable({
    webhooks,
    projects,
    loading,
    testing,
    testResults,
    expanded,
    deliveries,
    deliveriesLoading,
    onEdit,
    onDelete,
    onSetStatus,
    onRotate,
    onTest,
    onToggleDeliveries,
}: WebhookTableProps) {
    const projectNames = new Map(projects.map((project) => [project.id, project.name]));

    return (
        <div className="overflow-x-auto rounded-md border">
            <table className="w-full min-w-[52rem] table-fixed text-xs">
                <colgroup>
                    <col />
                    <col className="w-16" />
                    <col className="w-20" />
                    <col className="w-20" />
                    <col className="w-36" />
                    <col className="w-32" />
                </colgroup>
                <thead className="border-b bg-muted/30 text-left text-muted-foreground">
                    <tr>
                        <th scope="col" className="px-2 py-1.5 font-medium">URL</th>
                        <th scope="col" className="px-2 py-1.5 font-medium">Events</th>
                        <th scope="col" className="px-2 py-1.5 font-medium">Projects</th>
                        <th scope="col" className="px-2 py-1.5 font-medium">Status</th>
                        <th scope="col" className="px-2 py-1.5 font-medium">Last delivery</th>
                        <th scope="col" className="px-2 py-1.5"><span className="sr-only">Actions</span></th>
                    </tr>
                </thead>
                <tbody className="divide-y">
                    {loading ? (
                        <tr>
                            <td colSpan={6} className="px-2 py-2 text-muted-foreground">Loading…</td>
                        </tr>
                    ) : webhooks.length === 0 ? (
                        <tr>
                            <td colSpan={6} className="px-2 py-2 text-muted-foreground">No webhooks.</td>
                        </tr>
                    ) : webhooks.map((webhook) => {
                        const result = testResults[webhook.id];
                        const deliveryRows = deliveries[webhook.id] ?? [];
                        const projectLabel = webhook.projectIds === null
                            ? "All"
                            : String(webhook.projectIds.length);
                        const projectTitle = webhook.projectIds === null
                            ? "All projects"
                            : webhook.projectIds.map((id) => projectNames.get(id) ?? id).join(", ");
                        const statusVariant = webhook.status === "active"
                            ? "success"
                            : webhook.status === "failing" ? "destructive" : "secondary";
                        const statusTitle = webhook.status === "failing"
                            ? webhook.lastError ?? "Failing"
                            : webhook.lastStatus === null ? undefined : `Last status ${webhook.lastStatus}`;
                        return (
                            <Fragment key={webhook.id}>
                                <tr>
                                    <td className="px-2 py-1.5">
                                        <span className="block truncate" title={webhook.url}>{webhook.url}</span>
                                    </td>
                                    <td className="px-2 py-1.5">
                                        <span className="block truncate" title={webhook.eventKinds.join(", ")}>
                                            {webhook.eventKinds.length}
                                        </span>
                                    </td>
                                    <td className="px-2 py-1.5">
                                        <span className="block truncate" title={projectTitle}>{projectLabel}</span>
                                    </td>
                                    <td className="px-2 py-1.5">
                                        <Badge variant={statusVariant} title={statusTitle}>{webhook.status}</Badge>
                                    </td>
                                    <td className="px-2 py-1.5">
                                        <div className="flex items-center gap-1">
                                            <span
                                                className="min-w-0 flex-1 truncate text-muted-foreground"
                                                title={webhook.lastDeliveryAt ?? undefined}
                                            >
                                                {timeLabel(webhook.lastDeliveryAt)}
                                            </span>
                                            <Button
                                                type="button"
                                                size="icon-xs"
                                                variant="ghost"
                                                title="Deliveries"
                                                aria-label={`Deliveries for ${webhook.url}`}
                                                onClick={() => onToggleDeliveries(webhook)}
                                            >
                                                {expanded === webhook.id
                                                    ? <ChevronDown aria-hidden="true" />
                                                    : <ChevronRight aria-hidden="true" />}
                                            </Button>
                                        </div>
                                    </td>
                                    <td className="px-2 py-1.5">
                                        <div className="flex justify-end gap-1">
                                            <Button
                                                type="button"
                                                size="icon-xs"
                                                variant="ghost"
                                                title="Test"
                                                aria-label={`Test ${webhook.url}`}
                                                disabled={testing === webhook.id}
                                                onClick={() => onTest(webhook)}
                                            >
                                                <FlaskConical aria-hidden="true" />
                                            </Button>
                                            <Button
                                                type="button"
                                                size="icon-xs"
                                                variant="ghost"
                                                title="Rotate secret"
                                                aria-label={`Rotate secret for ${webhook.url}`}
                                                onClick={() => onRotate(webhook)}
                                            >
                                                <KeyRound aria-hidden="true" />
                                            </Button>
                                            <Button
                                                type="button"
                                                size="icon-xs"
                                                variant="ghost"
                                                title={webhook.status === "active" ? "Disable" : "Enable"}
                                                aria-label={`${webhook.status === "active" ? "Disable" : "Enable"} ${webhook.url}`}
                                                onClick={() => onSetStatus(
                                                    webhook,
                                                    webhook.status === "active" ? "disabled" : "active",
                                                )}
                                            >
                                                {webhook.status === "active"
                                                    ? <PowerOff aria-hidden="true" />
                                                    : <Power aria-hidden="true" />}
                                            </Button>
                                            <Button
                                                type="button"
                                                size="icon-xs"
                                                variant="ghost"
                                                title="Edit"
                                                aria-label={`Edit ${webhook.url}`}
                                                onClick={() => onEdit(webhook)}
                                            >
                                                <Pencil aria-hidden="true" />
                                            </Button>
                                            <Button
                                                type="button"
                                                size="icon-xs"
                                                variant="ghost"
                                                title="Delete"
                                                aria-label={`Delete ${webhook.url}`}
                                                onClick={() => onDelete(webhook)}
                                            >
                                                <Trash2 aria-hidden="true" />
                                            </Button>
                                        </div>
                                    </td>
                                </tr>
                                {result && (
                                    <tr>
                                        <td colSpan={6} className="bg-muted/20 px-2 py-1.5">
                                            <span
                                                className={result.ok ? "text-success" : "text-destructive"}
                                                title={result.error ?? undefined}
                                            >
                                                {result.ok ? result.statusCode ?? "OK" : result.error ?? "Failed"}
                                                {" · "}
                                                {result.durationMs} ms
                                            </span>
                                        </td>
                                    </tr>
                                )}
                                {expanded === webhook.id && (
                                    <tr>
                                        <td colSpan={6} className="bg-muted/20 px-2 py-2">
                                            <DeliveriesPanel
                                                rows={deliveryRows}
                                                loading={deliveriesLoading === webhook.id}
                                            />
                                        </td>
                                    </tr>
                                )}
                            </Fragment>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}

function DeliveriesPanel({ rows, loading }: { rows: WebhookDelivery[]; loading: boolean }) {
    return (
        <div className="rounded-md border">
            <table className="w-full table-fixed text-xs">
                <colgroup>
                    <col />
                    <col className="w-20" />
                    <col className="w-28" />
                    <col className="w-20" />
                </colgroup>
                <thead className="border-b bg-muted/30 text-left text-muted-foreground">
                    <tr>
                        <th scope="col" className="px-2 py-1 font-medium">Time</th>
                        <th scope="col" className="px-2 py-1 font-medium">Event</th>
                        <th scope="col" className="px-2 py-1 font-medium">Status</th>
                        <th scope="col" className="px-2 py-1 font-medium">Duration</th>
                    </tr>
                </thead>
                <tbody className="divide-y">
                    {loading ? (
                        <tr>
                            <td colSpan={4} className="px-2 py-1.5 text-muted-foreground">Loading…</td>
                        </tr>
                    ) : rows.length === 0 ? (
                        <tr>
                            <td colSpan={4} className="px-2 py-1.5 text-muted-foreground">No deliveries.</td>
                        </tr>
                    ) : rows.map((row) => (
                        <tr key={row.id}>
                            <td className="px-2 py-1">
                                <span className="block truncate" title={timeLabel(row.at)}>{timeLabel(row.at)}</span>
                            </td>
                            <td className="px-2 py-1">{row.eventId ?? "—"}</td>
                            <td className="px-2 py-1" title={row.error ?? undefined}>
                                <span className="block truncate">
                                    {row.ok ? row.statusCode ?? "OK" : row.error ?? "Failed"}
                                </span>
                            </td>
                            <td className="px-2 py-1">{row.durationMs === null ? "—" : `${row.durationMs} ms`}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}
