import React, { useState, useEffect } from 'react';
import { api, type AuditLogEntrySchema } from '../api';
import { Activity, Clock, ShieldAlert, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react';

export const AuditTrail: React.FC = () => {
  const [logs, setLogs] = useState<AuditLogEntrySchema[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [eventType, setEventType] = useState<string>('');

  useEffect(() => {
    fetchLogs();
  }, [page, eventType]);

  const fetchLogs = async () => {
    try {
      setLoading(true);
      const data = await api.getAuditLog(page, eventType || undefined);
      setLogs(data.items);
      setTotalPages(Math.max(1, Math.ceil(data.total / data.page_size)));
      setError(null);
    } catch (err: any) {
      setError(err.message || 'Failed to load audit log');
    } finally {
      setLoading(false);
    }
  };

  const getStatusIcon = (status: string) => {
    switch(status) {
      case 'ok': return <CheckCircle2 className="w-4 h-4 text-green-500" />;
      case 'cached': return <Clock className="w-4 h-4 text-blue-500" />;
      case 'fallback': return <AlertTriangle className="w-4 h-4 text-amber-500" />;
      default: return <XCircle className="w-4 h-4 text-slate-400" />;
    }
  };

  return (
    <div className="bg-white rounded-lg border border-border overflow-hidden shadow-sm flex flex-col h-[calc(100vh-8rem)]">
      <div className="p-4 border-b border-border bg-slate-50 flex justify-between items-center">
        <h2 className="font-semibold text-text flex items-center">
          <Activity className="w-5 h-5 mr-2 text-text-muted" />
          Audit Trail
        </h2>
        
        <select 
          value={eventType} 
          onChange={(e) => { setEventType(e.target.value); setPage(1); }}
          className="bg-white border border-border text-sm rounded-md px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-primary"
        >
          <option value="">All Events</option>
          <option value="llm_explanation">Explanations</option>
          <option value="llm_qa_query">Q&A Queries</option>
          <option value="reconcile_run">Pipeline Runs</option>
        </select>
      </div>

      <div className="flex-1 overflow-auto">
        {loading && logs.length === 0 ? (
          <div className="flex items-center justify-center h-full text-text-muted">Loading audit logs...</div>
        ) : error ? (
          <div className="m-4 text-status-unresolved bg-status-unresolved-bg p-4 rounded-md">{error}</div>
        ) : logs.length === 0 ? (
          <div className="flex items-center justify-center h-full text-text-muted">No audit logs found.</div>
        ) : (
          <table className="w-full text-left text-sm text-text">
            <thead className="bg-white border-b border-border sticky top-0 shadow-sm z-10 text-xs uppercase tracking-wider font-semibold">
              <tr>
                <th className="px-6 py-4 text-text-muted">Timestamp (UTC)</th>
                <th className="px-6 py-4 text-text-muted">Event Type</th>
                <th className="px-6 py-4 text-text-muted">Target</th>
                <th className="px-6 py-4 text-text-muted">Status</th>
                <th className="px-6 py-4 text-text-muted">Latency</th>
                <th className="px-6 py-4 text-text-muted">Hallucination</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/50">
              {logs.map((log, idx) => (
                <tr key={idx} className="hover:bg-slate-50 transition-colors">
                  <td className="px-6 py-4 whitespace-nowrap text-xs text-text-muted">
                    {new Date(log.timestamp_utc).toLocaleString()}
                  </td>
                  <td className="px-6 py-4">
                    <span className="px-2.5 py-1 bg-indigo-50 text-indigo-700 border border-indigo-100/50 rounded-full text-[10px] font-bold uppercase tracking-wider">
                      {log.event_type.replace('_', ' ')}
                    </span>
                  </td>
                  <td className="px-6 py-4 font-medium text-xs">
                    {log.order_id || 'System'}
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex items-center space-x-1.5">
                      {getStatusIcon(log.llm_status)}
                      <span className="text-xs font-medium text-text-muted capitalize">{log.llm_status}</span>
                    </div>
                  </td>
                  <td className="px-6 py-4 text-xs font-mono text-text-muted">
                    {log.latency_ms > 0 ? `${log.latency_ms}ms` : '-'}
                  </td>
                  <td className="px-6 py-4">
                    {log.potential_hallucination ? (
                      <span className="flex items-center text-status-unresolved text-xs font-medium bg-status-unresolved-bg px-2 py-0.5 rounded-full w-fit">
                        <ShieldAlert className="w-3 h-3 mr-1" /> Flagged
                      </span>
                    ) : (
                      <span className="text-text-muted text-xs">-</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="p-3 border-t border-border bg-white flex justify-between items-center text-sm">
        <span className="text-text-muted">Page {page} of {totalPages}</span>
        <div className="space-x-2">
          <button 
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={page === 1 || loading}
            className="px-3 py-1 border border-border rounded hover:bg-slate-50 disabled:opacity-50"
          >
            Prev
          </button>
          <button 
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            disabled={page === totalPages || loading}
            className="px-3 py-1 border border-border rounded hover:bg-slate-50 disabled:opacity-50"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
};
