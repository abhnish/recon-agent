import React from 'react';
import { type MetricsResponse } from '../api';
import { PlayCircle, CheckCircle, AlertTriangle, Clock } from 'lucide-react';

interface DashboardProps {
  metrics: MetricsResponse | null;
  onRunReconciliation: () => void;
  isRunLoading: boolean;
}

export const Dashboard: React.FC<DashboardProps> = ({ metrics, onRunReconciliation, isRunLoading }) => {
  if (!metrics) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center space-y-4">
        <h2 className="text-2xl font-semibold text-text">No Reconciliation Run Found</h2>
        <p className="text-text-muted">Run the reconciliation pipeline to view metrics and exceptions.</p>
        <button
          onClick={onRunReconciliation}
          disabled={isRunLoading}
          className="flex items-center px-4 py-2 bg-primary text-white rounded-md hover:bg-primary-hover disabled:opacity-50 transition-colors"
        >
          {isRunLoading ? <span className="animate-spin mr-2">⏳</span> : <PlayCircle className="w-5 h-5 mr-2" />}
          Run Pipeline
        </button>
      </div>
    );
  }

  const formatCurrency = (val: number) => new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' }).format(val);

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-text">Reconciliation Summary</h1>
          <p className="text-text-muted text-sm mt-1">Run #{metrics.last_run_id}</p>
        </div>
        <button
          onClick={onRunReconciliation}
          disabled={isRunLoading}
          className="flex items-center px-4 py-2 bg-white border border-border shadow-sm text-text rounded-md hover:bg-slate-50 disabled:opacity-50 transition-colors"
        >
          {isRunLoading ? <span className="animate-spin mr-2">⏳</span> : <PlayCircle className="w-4 h-4 mr-2" />}
          Re-run Pipeline
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricCard title="Match Rate" value={`${metrics.match_rate_pct}%`} icon={<CheckCircle className="w-6 h-6" />} />
        <MetricCard title="Auto-Matched Value" value={formatCurrency(metrics.value_auto_matched)} icon={<CheckCircle className="w-6 h-6" />} />
        <MetricCard title="Value in Exceptions" value={formatCurrency(metrics.value_in_exceptions)} icon={<AlertTriangle className="w-6 h-6" />} />
        <MetricCard title="Avg Processing Time" value={`${metrics.avg_runtime_ms} ms`} icon={<Clock className="w-6 h-6" />} />
      </div>

      <div className="bg-white p-8 rounded-xl border border-slate-200/60 shadow-sm space-y-5">
        <h2 className="text-lg font-semibold text-text">Status Breakdown</h2>
        
        <div className="w-full h-10 flex rounded-lg overflow-hidden bg-slate-100 shadow-inner">
          {metrics.total_processed > 0 && (
            <>
              <div style={{ width: `${(metrics.auto_matched / metrics.total_processed) * 100}%` }} className="bg-status-match h-full" title={`Auto Matched: ${metrics.auto_matched}`} />
              <div style={{ width: `${(metrics.needs_review / metrics.total_processed) * 100}%` }} className="bg-status-review h-full" title={`Needs Review: ${metrics.needs_review}`} />
              <div style={{ width: `${(metrics.unresolved / metrics.total_processed) * 100}%` }} className="bg-status-unresolved h-full" title={`Unresolved: ${metrics.unresolved}`} />
            </>
          )}
        </div>
        
        <div className="flex items-center space-x-6 text-sm">
          <div className="flex items-center"><span className="w-3 h-3 rounded-full bg-status-match mr-2"></span>{metrics.auto_matched} Auto Matched</div>
          <div className="flex items-center"><span className="w-3 h-3 rounded-full bg-status-review mr-2"></span>{metrics.needs_review} Needs Review</div>
          <div className="flex items-center"><span className="w-3 h-3 rounded-full bg-status-unresolved mr-2"></span>{metrics.unresolved} Unresolved</div>
        </div>
      </div>
    </div>
  );
};

const MetricCard = ({ title, value, icon }: { title: string, value: string, icon: React.ReactNode }) => (
  <div className="bg-white p-8 rounded-xl border border-slate-200/60 shadow-sm flex items-center justify-between transition-shadow hover:shadow-md">
    <div>
      <p className="text-sm font-medium text-text-muted">{title}</p>
      <p className="text-3xl font-semibold text-text mt-2">{value}</p>
    </div>
    <div className="p-3 bg-indigo-50/50 rounded-full text-primary">
      {icon}
    </div>
  </div>
);
