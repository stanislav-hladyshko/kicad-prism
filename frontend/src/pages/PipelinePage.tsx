import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { fetchJson } from '@/lib/api';
import { cn } from '@/lib/utils';

/** One stage of one board, as the repository's `.prism-pipeline.json` states it (backend pipeline_service). */
export interface PipelineCell {
    state: 'ok' | 'warn' | 'bad' | 'human' | 'none' | 'na';
    text: string;
    why: string;
}

export interface PipelineRow {
    id: string;
    dir: string | null;
    project_id: string | null;
    board: string;
    kind: string;
    stop: string;
    cells: PipelineCell[];
    human: PipelineCell;
}

export interface Pipeline {
    repo_id: string;
    repo_name: string;
    as_of: string;
    columns: string[];
    rows: PipelineRow[];
}

const STATE_CLASS: Record<PipelineCell['state'], string> = {
    ok: 'bg-success/15 text-success',
    warn: 'bg-warning/15 text-warning',
    bad: 'bg-destructive/15 text-destructive',
    human: 'bg-primary/10 text-primary',
    none: 'text-muted-foreground',
    na: 'text-muted-foreground',
};

function Cell({ cell }: { cell: PipelineCell }) {
    return (
        <td className={cn('whitespace-nowrap px-2 py-2 text-center text-xs', STATE_CLASS[cell.state])} title={cell.why}>
            {cell.text}
        </td>
    );
}

function PipelineTable({ pipeline }: { pipeline: Pipeline }) {
    return (
        <section className="space-y-2">
            <h2 className="text-sm text-muted-foreground">
                {pipeline.repo_name}, last gate run {pipeline.as_of || '-'}
            </h2>
            <div className="overflow-x-auto rounded-lg border">
                <table className="w-full min-w-[64rem] border-collapse text-sm">
                    <thead>
                        <tr className="border-b text-left text-xs text-muted-foreground">
                            <th scope="col" className="px-2 py-2 font-medium">Board</th>
                            <th scope="col" className="px-2 py-2 font-medium">Stopped at</th>
                            {pipeline.columns.map((name) => (
                                <th key={name} scope="col" className="px-2 py-2 font-medium">{name}</th>
                            ))}
                            <th scope="col" className="px-2 py-2 font-medium">Sign-off</th>
                        </tr>
                    </thead>
                    <tbody>
                        {pipeline.rows.map((row) => (
                            <tr key={row.id} className="border-b last:border-b-0 align-top">
                                <th scope="row" className="min-w-[12rem] px-2 py-2 text-left font-semibold">
                                    {row.project_id ? (
                                        <Link to={`/project/${row.project_id}`} className="underline-offset-4 hover:underline">
                                            {row.id}
                                        </Link>
                                    ) : (
                                        row.id
                                    )}
                                    <span className="block text-xs font-normal text-muted-foreground">{row.board}</span>
                                    <span className="block text-xs font-normal text-muted-foreground">{row.kind}</span>
                                </th>
                                <td className="whitespace-nowrap px-2 py-2 font-semibold">{row.stop}</td>
                                {row.cells.map((cell, index) => (
                                    <Cell key={pipeline.columns[index] ?? index} cell={cell} />
                                ))}
                                <Cell cell={row.human} />
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </section>
    );
}

/** Where every board stands: the repository's own pipeline, with the release sign-off from Release Studio. */
export function PipelinePage() {
    const navigate = useNavigate();
    const [pipelines, setPipelines] = useState<Pipeline[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let active = true;
        const load = async () => {
            try {
                const data = await fetchJson<{ pipelines: Pipeline[] }>(
                    '/api/workspace/pipeline', undefined, 'Could not load the pipeline');
                if (active) setPipelines(data.pipelines);
            } catch (err: unknown) {
                if (active) setError(err instanceof Error ? err.message : String(err));
            }
        };
        void load();
        return () => { active = false; };
    }, []);

    return (
        <div className="min-h-app-viewport bg-background text-foreground">
            <header className="sticky top-0 z-10 flex h-16 items-center gap-4 border-b bg-background/95 px-3 backdrop-blur md:px-4">
                <Button variant="ghost" size="sm" onClick={() => navigate('/')}>
                    <ArrowLeft className="mr-2 h-4 w-4" />
                    Back
                </Button>
                <h1 className="text-xl font-bold">Pipeline</h1>
            </header>
            <main className="space-y-6 p-3 md:p-4">
                {error && <p className="text-destructive">{error}</p>}
                {!error && pipelines === null && <p className="text-muted-foreground">Loading...</p>}
                {pipelines?.length === 0 && (
                    <p className="text-muted-foreground">No repository commits .prism-pipeline.json yet.</p>
                )}
                {pipelines?.map((pipeline) => <PipelineTable key={pipeline.repo_id} pipeline={pipeline} />)}
            </main>
        </div>
    );
}
